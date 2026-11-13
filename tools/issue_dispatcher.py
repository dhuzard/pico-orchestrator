#!/usr/bin/env python3
"""Opt-in GitHub Issues intake for PICO headless runs.

This adapter is deliberately not an LLM or a GitHub webhook server. Each task
needs an operator approval tied to its exact title and body; issue labels and
comments are *not* authority. The existing PICO autonomy policy remains binding.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time

import yaml
from dataclasses import dataclass
from pathlib import Path

REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEADING = re.compile(r"^## ([^\n]+)\s*$", re.MULTILINE)
REQUIRED = ("Target repository", "Objective", "Acceptance criteria")
ACTIVE = {"approved", "launching", "running"}
TERMINAL = {"completed", "partial", "blocked", "interrupted", "cancelled", "stale"}


class DispatcherError(Exception):
    pass


GITHUB_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR", "GH_HOST")


def clean_github_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in GITHUB_ENV}


def require_private(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.stat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        required = 0o700 if directory else 0o600
        if not valid_type or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != required:
            raise ValueError()
    except (OSError, ValueError) as exc:
        raise DispatcherError("credential path must be owned by the service user and have private permissions") from exc


def queue_env(cfg: dict) -> dict | None:
    if "queue_token_file" not in cfg:
        return None  # legacy configuration
    require_private(cfg["queue_token_file"])
    try:
        token = cfg["queue_token_file"].read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise DispatcherError("queue credential unavailable") from exc
    if not token or "\n" in token or "\r" in token:
        raise DispatcherError("queue credential unavailable")
    env = clean_github_env()
    env["GH_TOKEN"] = token
    return env


def target_env(cfg: dict) -> dict | None:
    if "target_gh_config_dir" not in cfg:
        return None
    # Require file-based auth: a missing entry must not fall back to a shared
    # system keychain or an inherited credential for the same GitHub host.
    require_private(cfg["target_gh_config_dir"], directory=True)
    require_private(cfg["target_gh_config_dir"] / "hosts.yml")
    try:
        hosts = yaml.safe_load((cfg["target_gh_config_dir"] / "hosts.yml").read_text(encoding="utf-8"))
        token = hosts["github.com"]["oauth_token"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError()
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise DispatcherError("target credential unavailable in target gh config") from exc
    env = clean_github_env()
    env["GH_CONFIG_DIR"] = str(cfg["target_gh_config_dir"])
    # The sandbox uses this path to deny worker reads; it is not a token.
    env["PI_DISPATCHER_QUEUE_CREDENTIAL_PATH"] = str(cfg["queue_token_file"])
    return env


def command(argv: list[str], *, timeout: int = 40, env: dict | None = None) -> dict | list:
    """Execute one fixed-argument CLI operation; never use a shell."""
    try:
        run = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatcherError(f"command unavailable or timed out: {argv[0]}") from exc
    try:
        value = json.loads(run.stdout)
    except ValueError as exc:
        raise DispatcherError(f"invalid JSON from {argv[0]} (exit {run.returncode})") from exc
    # `pico exec status` returns a structured BLOCKED result with exit 1.
    if run.returncode and argv[0].endswith("gh"):
        raise DispatcherError(f"GitHub operation failed (exit {run.returncode})")
    if not isinstance(value, (dict, list)):
        raise DispatcherError("expected a JSON object or array")
    return value


def load_config(path: Path) -> dict:
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not REPO.fullmatch(str(data.get("queue_repository", ""))):
        raise DispatcherError("queue_repository must be owner/name")
    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise DispatcherError("targets must map repository identities to local absolute paths")
    normalized = {}
    for identity, raw in targets.items():
        if not REPO.fullmatch(identity) or not isinstance(raw, str):
            raise DispatcherError("invalid target identity or path")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise DispatcherError("target checkout paths must be absolute")
        normalized[identity.casefold()] = str(p.resolve())
    state = Path(data.get("state_path", "~/.local/state/pi-coding-orchestrator/issue-dispatcher.sqlite3")).expanduser()
    if not state.is_absolute():
        raise DispatcherError("state_path must be absolute")
    poll = int(data.get("poll_seconds", 60))
    if not 10 <= poll <= 3600:
        raise DispatcherError("poll_seconds must be between 10 and 3600")
    duration = int(data.get("max_duration_seconds", 1800))
    calls = int(data.get("max_lead_calls", 30))
    if duration < 1 or calls < 1:
        raise DispatcherError("execution limits must be positive")
    queue_file, target_dir = data.get("queue_token_file"), data.get("target_gh_config_dir")
    if (queue_file is None) != (target_dir is None):
        raise DispatcherError("queue_token_file and target_gh_config_dir must be configured together")
    credentials = {}
    if queue_file is not None:
        if not isinstance(queue_file, str) or not isinstance(target_dir, str):
            raise DispatcherError("credential paths must be absolute paths")
        queue_path, target_path = Path(queue_file).expanduser(), Path(target_dir).expanduser()
        if not queue_path.is_absolute() or not target_path.is_absolute():
            raise DispatcherError("credential paths must be absolute paths")
        queue_path, target_path = queue_path.resolve(), target_path.resolve()
        if queue_path == target_path or target_path in queue_path.parents:
            raise DispatcherError("queue credential must be outside the target gh config directory")
        credentials = {"queue_token_file": queue_path, "target_gh_config_dir": target_path}
    return {**credentials, "queue": data["queue_repository"], "targets": normalized, "state": state,
            "poll": poll, "duration": duration, "calls": calls,
            "gh": str(data.get("gh_executable", "gh")),
            "pico": str(data.get("pico_executable", "pico"))}


@dataclass(frozen=True)
class Task:
    number: int
    repo: str
    checkout: str
    objective: str
    digest: str
    execution_id: str


def parse_task(issue: dict, cfg: dict) -> Task:
    if issue.get("state", "OPEN").upper() != "OPEN":
        raise DispatcherError("issue is not open")
    labels = issue.get("labels") or []
    names = {x.get("name", "") if isinstance(x, dict) else str(x) for x in labels}
    if "pico:queued" not in names:
        raise DispatcherError("issue lacks pico:queued label")
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise DispatcherError("invalid issue number")
    title, body = issue.get("title"), issue.get("body")
    if not isinstance(title, str) or not isinstance(body, str) or len(body) > 14000:
        raise DispatcherError("invalid task content")
    matches = list(HEADING.finditer(body))
    if len(matches) != len(REQUIRED) or [m.group(1).strip() for m in matches] != list(REQUIRED):
        raise DispatcherError("use exactly ## Target repository, ## Objective and ## Acceptance criteria")
    parts = {m.group(1).strip(): body[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(body)].strip()
             for i, m in enumerate(matches)}
    repo = parts["Target repository"]
    if not REPO.fullmatch(repo) or repo.casefold() not in cfg["targets"]:
        raise DispatcherError("target repository is not on the operator allowlist")
    if not (1 <= len(parts["Objective"]) <= 3200 and 1 <= len(parts["Acceptance criteria"]) <= 700):
        raise DispatcherError("objective or acceptance criteria exceed the allowed size")
    objective = f"{parts['Objective']}\n\nAcceptance criteria:\n{parts['Acceptance criteria']}"
    digest = hashlib.sha256(json.dumps([title, body], ensure_ascii=False).encode()).hexdigest()
    execution_id = "issue-" + hashlib.sha256(f"{cfg['queue']}:{number}:{digest}".encode()).hexdigest()[:28]
    return Task(number, repo, cfg["targets"][repo.casefold()], objective, digest, execution_id)


def issue_view(cfg: dict, number: int) -> dict:
    obj = command([cfg["gh"], "issue", "view", str(number), "--repo", cfg["queue"],
                   "--json", "number,title,body,state,labels"], env=queue_env(cfg))
    if not isinstance(obj, dict):
        raise DispatcherError("GitHub returned an invalid issue")
    return obj


def queued_issues(cfg: dict) -> list:
    result = command([cfg["gh"], "issue", "list", "--repo", cfg["queue"],
                      "--state", "open", "--label", "pico:queued", "--limit", "100",
                      "--json", "number,title,body,state,labels"], env=queue_env(cfg))
    if not isinstance(result, list):
        raise DispatcherError("GitHub returned an invalid issues list")
    return result


def database(cfg: dict):
    state: Path = cfg["state"]
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(state, timeout=20)
    os.chmod(state, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS approvals (
        number INTEGER PRIMARY KEY, digest TEXT NOT NULL, repo TEXT NOT NULL,
        checkout TEXT NOT NULL, execution_id TEXT NOT NULL, mode TEXT NOT NULL,
        state TEXT NOT NULL, reported TEXT)""")
    db.commit()
    return db


def approve(cfg: dict, number: int, mutate: bool) -> None:
    if not sys.stdin.isatty():
        raise DispatcherError("approval requires an interactive local terminal")
    task = parse_task(issue_view(cfg, number), cfg)
    with database(cfg) as db:
        current = db.execute("SELECT state FROM approvals WHERE number=?", (number,)).fetchone()
        if current and current["state"] not in {"approved", "stale"}:
            raise DispatcherError("task was already dispatched; create a new issue for a new attempt")
        print(f"Issue #{number} in {cfg['queue']}\nTarget: {task.repo}\nCheckout: {task.checkout}")
        print(f"Mode: {'LOCAL CODE MUTATION' if mutate else 'READ-ONLY'}\n\n{task.objective}\n")
        print("PICO autonomy policy still applies; this is not an OS sandbox or merge authorization.")
        phrase = f"approve {task.digest[:12]}"
        if input(f"Type '{phrase}' to authorize this exact issue version: ").strip() != phrase:
            raise DispatcherError("approval cancelled")
        db.execute("""INSERT INTO approvals
           (number,digest,repo,checkout,execution_id,mode,state,reported)
           VALUES (?,?,?,?,?,?,?,NULL)
           ON CONFLICT(number) DO UPDATE SET digest=excluded.digest,repo=excluded.repo,
           checkout=excluded.checkout,execution_id=excluded.execution_id,mode=excluded.mode,
           state='approved',reported=NULL""",
           (number, task.digest, task.repo, task.checkout, task.execution_id,
            "local-mutation" if mutate else "read-only", "approved"))
        db.commit()
    print(f"Approved issue #{number}; eligible for dispatch after service starts.")


def pico_action(cfg: dict, row: sqlite3.Row, action: str, objective: str = "") -> dict:
    args = [cfg["pico"], "exec", action, "--target", row["checkout"],
            "--repo", row["repo"], "--execution-id", row["execution_id"]]
    if action == "start":
        args += ["--objective", objective, "--no-wait", "--max-duration", str(cfg["duration"]),
                 "--max-lead-calls", str(cfg["calls"])]
        if row["mode"] == "local-mutation":
            args.append("--allow-mutation")
    # Status is local and enforces the duration limit; it must continue even
    # when a credential is revoked or removed while a run is active.
    env = target_env(cfg) if action == "start" else (clean_github_env() if "target_gh_config_dir" in cfg else None)
    if action == "start" and env is not None:
        try:
            auth = subprocess.run([cfg["gh"], "auth", "status", "--hostname", "github.com"],
                                  capture_output=True, text=True, timeout=40, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DispatcherError("target GitHub authentication unavailable") from exc
        if auth.returncode:
            raise DispatcherError("target GitHub authentication unavailable")
        try:
            repo = subprocess.run([cfg["gh"], "repo", "view", row["repo"], "--json", "nameWithOwner"],
                                  capture_output=True, text=True, timeout=40, check=False, env=env)
            identity = json.loads(repo.stdout)["nameWithOwner"]
            if repo.returncode or not isinstance(identity, str) or identity.casefold() != row["repo"].casefold():
                raise ValueError()
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            raise DispatcherError("target GitHub repository access unavailable") from exc
    result = command(args, timeout=90, env=env)
    if not isinstance(result, dict):
        raise DispatcherError("PICO returned an invalid execution record")
    return result


def classify(record: dict) -> str:
    if record.get("error_code"):
        return "blocked"
    status = record.get("status")
    if status == "launched":
        return "running"
    if status == "interrupted":
        return "interrupted"
    if status == "cancelled":
        return "cancelled"
    if status == "exited":
        outcome = (record.get("run") or {}).get("status")
        return {"COMPLETED": "completed", "PARTIAL": "partial"}.get(outcome, "blocked")
    return "blocked"


def report(cfg: dict, db: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["reported"] == row["state"]:
        return
    body = (f"PICO dispatcher: **{row['state']}**\n\n"
            f"Execution ID: `{row['execution_id']}`. "
            "No automatic merge or deployment was requested by the dispatcher.")
    try:
        result = subprocess.run([cfg["gh"], "issue", "comment", str(row["number"]),
                                 "--repo", cfg["queue"], "--body", body],
                                capture_output=True, text=True, timeout=40, check=False, env=queue_env(cfg))
        if result.returncode:
            return  # Reattempt on next poll. A crash can lead to duplicate comments.
    except (OSError, subprocess.TimeoutExpired, DispatcherError):
        return
    db.execute("UPDATE approvals SET reported=? WHERE number=?", (row["state"], row["number"]))
    db.commit()


def tick(cfg: dict, db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT * FROM approvals ORDER BY number").fetchall()
    for row in rows:
        if row["state"] not in ACTIVE:
            report(cfg, db, row)
            continue
        if row["state"] == "approved":
            # Re-read immediately before dispatch; a changed issue invalidates approval.
            try:
                task = parse_task(issue_view(cfg, row["number"]), cfg)
                valid = (task.digest == row["digest"] and task.repo == row["repo"]
                         and task.checkout == row["checkout"])
            except DispatcherError:
                valid = False
            if not valid:
                db.execute("UPDATE approvals SET state='stale' WHERE number=?", (row["number"],))
                db.commit()
                continue
            # Never dispatch a second job while the first is launching or running.
            running = db.execute("SELECT 1 FROM approvals WHERE state IN ('launching','running') LIMIT 1").fetchone()
            if running:
                continue
            db.execute("UPDATE approvals SET state='launching' WHERE number=?", (row["number"],))
            db.commit()
            row = db.execute("SELECT * FROM approvals WHERE number=?", (row["number"],)).fetchone()
        try:
            if row["state"] == "launching":
                # Idempotent start recovers a crash after launch without launching a duplicate.
                issue = parse_task(issue_view(cfg, row["number"]), cfg)
                if issue.digest != row["digest"]:
                    state = "stale"
                else:
                    state = classify(pico_action(cfg, row, "start", issue.objective))
            else:
                state = classify(pico_action(cfg, row, "status"))
        except DispatcherError as exc:
            print(f"Issue #{row['number']}: {exc}", file=sys.stderr)
            continue  # transient errors are not terminal evidence
        db.execute("UPDATE approvals SET state=? WHERE number=?", (state, row["number"]))
        db.commit()
        report(cfg, db, db.execute("SELECT * FROM approvals WHERE number=?", (row["number"],)).fetchone())


def run_service(cfg: dict, once: bool) -> None:
    lock_path = cfg["state"].with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DispatcherError("dispatcher already running") from exc
        with database(cfg) as db:
            while True:
                tick(cfg, db)
                if once:
                    return
                time.sleep(cfg["poll"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, required=True)
    sub = p.add_subparsers(dest="action", required=True)
    a = sub.add_parser("approve", help="authorize one exact issue version from the local terminal")
    a.add_argument("--issue", type=int, required=True)
    a.add_argument("--allow-mutation", action="store_true")
    sub.add_parser("once", help="reconcile/dispatch one poll; no daemon installation")
    sub.add_parser("serve", help="poll indefinitely (suitable for systemd)")
    sub.add_parser("status", help="print durable local task states")
    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.action == "approve":
            approve(cfg, args.issue, args.allow_mutation)
        elif args.action == "status":
            with database(cfg) as db:
                print(json.dumps([dict(x) for x in db.execute("SELECT * FROM approvals ORDER BY number")], indent=2))
        else:
            run_service(cfg, once=args.action == "once")
    except (DispatcherError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"pico-dispatcher: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
