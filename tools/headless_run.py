#!/usr/bin/env python3
"""Headless single-repository execution: start, observe, resume and cancel one
bounded orchestration run without an interactive terminal.

A headless run is one Pi process in print mode (`pi --print --mode json`) with
the orchestrator extensions loaded and the /orchestrate prompt as its only
input. The orchestration-lifecycle extension creates the parent run, keeps all
of its guards (SESSION_REPOSITORY_MISMATCH, WORKER_WORKSPACE_INVALID,
WAVE_UNDECLARED, backend preflight) and settles the run as BLOCKED when the
process exits without an explicit outcome. Pi's session id is chosen here and
stored with the execution, so a crashed process can be resumed into the same
parent run while a normally exited one cannot be resurrected.

Headless is not autonomous. Mutation and publication stay governed by the
target's .pi/autonomy.yaml; a policy that permits any mutation is refused
unless --allow-mutation is passed explicitly.

Execution records are keyed by a caller-chosen execution id in orchestrator
state (never in the target), so repeating a `start` never launches a second
process, run, branch or PR for the same id.

Exit codes: 0 ok, 1 blocked or refused, 2 invalid invocation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autonomy_policy  # noqa: E402
import github_integration  # noqa: E402
import parent_orchestration  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402
import workspace_plan  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "orchestrate-core.md"
EXTENSIONS = ["model-router.ts", "workflow-commands.ts", "mission-controller.ts", "orchestration-lifecycle.ts"]
SESSION_ENTRY = "pi-orchestration-parent-v1"
RECORD_VERSION = 1
EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MUTATING_PERMISSIONS = ("create_worktrees", "commit", "push_branch", "create_pr", "mark_pr_ready", "merge", "deploy")
TERMINATE_GRACE_SECONDS = 10
# A resumed session must not carry the run marker: the lifecycle answers the
# marker by settling the restored run and starting a new one. Without it,
# session_start restores the IN_PROGRESS run and the agent continues it.
RESUME_PROMPT = ("Continue the active orchestration run recorded in this session. First call "
                 "orchestration_record with {\"action\":\"status\"}. Continue only when its reply says the "
                 "record is ready with ok: true and active: true; if it says finalized or BLOCKED, stop and "
                 "report the blocker. Then resume from the last recorded phase and finish the run with an "
                 "explicit outcome.")


class HeadlessError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_root() -> Path:
    explicit = os.environ.get(provenance.STATE_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise HeadlessError("STATE_DIR_INVALID", f"{provenance.STATE_ENV} must be an absolute path")
        return path
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "pi-coding-orchestrator"


def repository_key(common_dir: Path) -> str:
    name = (common_dir.parent if common_dir.name == ".git" else common_dir).name or "root"
    return f"{name}-{hashlib.sha256(str(common_dir).encode()).hexdigest()[:12]}"


def execution_path(common_dir: Path, execution_id: str) -> Path:
    if not EXECUTION_ID_RE.fullmatch(execution_id):
        raise HeadlessError("EXECUTION_ID_INVALID", "execution id must be filename-safe")
    return state_root() / "executions" / repository_key(common_dir) / f"{execution_id}.json"


def load_record(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HeadlessError("EXECUTION_RECORD_UNREADABLE", str(exc)) from exc
    if not isinstance(data, dict) or data.get("record_version") != RECORD_VERSION:
        raise HeadlessError("EXECUTION_RECORD_UNREADABLE", "unsupported execution record")
    return data


def save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = _now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def build_prompt(objective: str, prompt_path: Path = PROMPT_PATH) -> str:
    template = re.sub(r"^---[\s\S]*?---\s*", "", prompt_path.read_text(encoding="utf-8"))
    if "$ARGUMENTS" in template:
        return template.replace("$ARGUMENTS", objective)
    return f"{template.rstrip()}\n\n{objective}\n"


def mutation_permitted(worktree: Path) -> list[str]:
    """Names of mutating permissions the target's autonomy policy grants."""
    path = autonomy_policy.policy_path(worktree)
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HeadlessError("AUTONOMY_POLICY_UNREADABLE", "autonomy policy is unreadable") from exc
    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, dict):
        return []
    return [name for name in MUTATING_PERMISSIONS if permissions.get(name) is True]


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        # Reap our own exited child so a zombie never reads as alive.
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child: fall through to the signal probe
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover_run_id(session_dir: Path, session_id: str) -> str | None:
    """The lifecycle extension stores the parent run id in a custom session entry."""
    run_id = None
    for file in sorted(session_dir.glob(f"*_{session_id}.jsonl")):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (isinstance(entry, dict) and entry.get("type") == "custom" and entry.get("customType") == SESSION_ENTRY
                    and isinstance(entry.get("data"), dict) and isinstance(entry["data"].get("run_id"), str)
                    and provenance.RUN_ID_PATTERN.fullmatch(entry["data"]["run_id"])):
                run_id = entry["data"]["run_id"]
    return run_id


def run_status(run_id: str | None) -> dict | None:
    if run_id is None:
        return None
    try:
        record = provenance.get_run(ROOT, run_id)
        parent = record.get("parent_orchestration") or {}
        return {"run_id": run_id, "status": parent.get("final_status"), "blocker_code": parent.get("blocker_code"),
                "trace_path": str(provenance.run_path(ROOT, run_id))}
    except (provenance.ProvenanceError, OSError, KeyError, TypeError):
        return {"run_id": run_id, "status": None, "blocker_code": "PARENT_PROVENANCE_UNAVAILABLE", "trace_path": None}


def _backend_identity(target: Path, python: str) -> str:
    probe = subprocess.run([python, str(ROOT / "tools" / "claude_backend.py"), str(target)], shell=False,
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise HeadlessError("BACKEND_IDENTITY_UNINITIALIZED", probe.stderr.strip() or "claude backend identity unavailable")
    return probe.stdout.strip().splitlines()[-1]


def _target(target: Path, explicit_repo: str) -> tuple[Path, str, Path]:
    try:
        worktree = target.resolve(strict=True)
    except OSError as exc:
        raise HeadlessError("TARGET_REPOSITORY_UNRESOLVED", "target checkout does not exist") from exc
    try:
        identity = github_integration.resolve_target(worktree, explicit_repo)
    except github_integration.IntegrationError as exc:
        raise HeadlessError(exc.code, exc.detail) from exc
    common = session_scope.common_dir(worktree)
    if common is None:
        raise HeadlessError("SESSION_TARGET_UNRESOLVED", "git common directory cannot be resolved")
    return worktree, identity, common


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(checkout), *args], shell=False, capture_output=True, text=True, timeout=60)


def checkout_state(checkout: Path) -> dict:
    """Branch (or detached HEAD) and linked worktrees of a checkout, to undo what a run left behind."""
    branch = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or None
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip() or None
    listing = _git(checkout, "worktree", "list", "--porcelain").stdout.splitlines()
    paths = [line[len("worktree "):] for line in listing if line.startswith("worktree ")]
    main = str(checkout.resolve())
    return {"branch": branch, "head": head, "worktrees": sorted(p for p in paths if str(Path(p).resolve()) != main)}


def restore_checkout(checkout: Path, before: dict) -> dict:
    """After a child exits: remove clean worktrees it created and return a clean checkout to its starting point.

    Nothing dirty is touched: a worktree with uncommitted or untracked files is
    kept, and a checkout with uncommitted tracked changes is left where it is.
    Branches are never deleted, because they hold the work.
    """
    after = checkout_state(checkout)
    result = {"branch_before": before["branch"], "branch_after": after["branch"], "restored": False,
              "removed_worktrees": [], "kept_worktrees": [], "left_reason": None}
    for path in sorted(set(after["worktrees"]) - set(before["worktrees"])):
        status = _git(Path(path), "status", "--porcelain")
        if status.returncode == 0 and not status.stdout.strip() and _git(checkout, "worktree", "remove", path).returncode == 0:
            result["removed_worktrees"].append(path)
        else:
            result["kept_worktrees"].append(path)
    _git(checkout, "worktree", "prune")
    moved = (after["branch"] != before["branch"]) if before["branch"] else (after["head"] != before["head"])
    if moved:
        if _git(checkout, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            result["left_reason"] = "uncommitted changes in the checkout"
        else:
            target = ["switch", "--quiet", before["branch"]] if before["branch"] else ["switch", "--quiet", "--detach", before["head"]]
            switched = _git(checkout, *target)
            result["restored"] = switched.returncode == 0
            if not result["restored"]:
                result["left_reason"] = "git switch failed"
        result["branch_after"] = checkout_state(checkout)["branch"]
    return result


def refresh(record: dict) -> dict:
    """Reconcile a record against the live process and the parent provenance."""
    session_dir = Path(record["session_dir"])
    if record.get("run_id") is None:
        record["run_id"] = discover_run_id(session_dir, record["session_id"])
    record["run"] = run_status(record.get("run_id"))
    if record["status"] == "launching":
        # A launch whose process id was never recorded: the outcome is uncertain.
        terminal = (record["run"] or {}).get("status") not in {None, "IN_PROGRESS"}
        record["status"] = "exited" if terminal else "interrupted"
    if record["status"] == "launched" and not pid_alive(record.get("pid")):
        # Gone without this process observing the exit: terminal run means it
        # ended normally, an open run means it was interrupted.
        terminal = (record["run"] or {}).get("status") not in {None, "IN_PROGRESS"}
        record["status"] = "exited" if terminal else "interrupted"
        record["pid"] = None
    # A process that is gone while its parent run is still open never settled:
    # that is a crash, resumable into the same run, not a finished execution.
    if record["status"] == "exited" and (record["run"] or {}).get("status") == "IN_PROGRESS":
        record["status"] = "interrupted"
    return record


def start(*, target: Path, explicit_repo: str, execution_id: str, objective: str, allow_mutation: bool = False,
          resume: bool = False, pi_executable: str = "pi", python: str = sys.executable,
          wait: bool = True) -> dict:
    if not objective.strip() or len(objective) > 20000:
        raise HeadlessError("OBJECTIVE_INVALID", "objective must be a non-empty string under 20000 characters")
    if provenance._contains_secret_like_content(objective):
        raise HeadlessError("OBJECTIVE_INVALID", "objective looks like it contains a secret")
    worktree, identity, common = _target(target, explicit_repo)
    path = execution_path(common, execution_id)
    record = load_record(path)
    if record is not None:
        if record["target_repository"].casefold() != identity.casefold():
            raise HeadlessError("EXECUTION_TARGET_MISMATCH", "execution id belongs to another repository")
        refresh(record)
        if record["status"] == "launched":
            record["outcome"] = "already_running"
            save_record(path, record)
            return record
        if record["status"] in {"exited", "cancelled"}:
            record["outcome"] = "already_finished"
            save_record(path, record)
            return record
        if record["status"] == "interrupted" and not resume:
            record["outcome"] = "resume_required"
            save_record(path, record)
            return record
        run = record.get("run") or {}
        if resume and run.get("status") not in {None, "IN_PROGRESS"}:
            record["status"] = "exited"
            record["outcome"] = "already_finished"
            save_record(path, record)
            return record
    elif resume:
        raise HeadlessError("EXECUTION_UNKNOWN", "nothing to resume for this execution id")

    granted = mutation_permitted(worktree)
    if granted and not allow_mutation:
        raise HeadlessError("MUTATION_NOT_AUTHORIZED",
                            "autonomy policy permits " + ", ".join(granted) + "; pass --allow-mutation to run it headless")

    session_dir = path.parent / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    if record is None:
        record = {"record_version": RECORD_VERSION, "execution_id": execution_id, "target_repository": identity,
                  "target_worktree": str(worktree), "git_common_dir": str(common), "objective": objective,
                  "session_id": str(uuid.uuid4()), "session_dir": str(session_dir),
                  "log_path": str(path.with_suffix(".log")), "mutation_allowed": bool(allow_mutation),
                  "orchestrator_revision": workspace_plan.orchestrator_revision(ROOT), "created_at": _now(),
                  "launches": 0, "run_id": None, "run": None, "pid": None, "exit_code": None, "status": "created"}
    identity_env = _backend_identity(worktree, python)
    env = dict(os.environ)
    env.update({"PI_TARGET_REPOSITORY": identity, "PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY": identity_env,
                provenance.STATE_ENV: str(state_root()), "PI_ORCHESTRATION_EXECUTION_ID": execution_id})
    run = run_status(record.get("run_id")) or {}
    continuing = resume and run.get("status") == "IN_PROGRESS"
    # Persist intent before the process exists, so a crash in between is seen
    # as an uncertain launch on restart rather than as never launched.
    record.update({"status": "launching", "pid": None})
    save_record(path, record)
    argv = [pi_executable, "--print", "--mode", "json", "--session-id", record["session_id"],
            "--session-dir", record["session_dir"]]
    for extension in EXTENSIONS:
        argv += ["-e", str(ROOT / "extensions" / extension)]
    argv += ["--", RESUME_PROMPT if continuing else build_prompt(record["objective"])]
    before = checkout_state(worktree)
    log = open(record["log_path"], "ab")
    try:
        child = subprocess.Popen(argv, cwd=str(worktree), env=env, shell=False, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        log.close()
        record["status"] = "created" if record["launches"] == 0 else "interrupted"
        save_record(path, record)
        raise HeadlessError("PI_UNAVAILABLE", f"cannot launch {pi_executable}: {exc}") from exc
    record.update({"status": "launched", "pid": child.pid, "launches": record["launches"] + 1,
                   "started_at": _now(), "exit_code": None, "outcome": "resumed" if resume else "launched"})
    save_record(path, record)
    if not wait:
        log.close()
        return record
    try:
        exit_code = child.wait()
    finally:
        log.close()
    record["exit_code"] = exit_code
    record["status"] = "exited"
    record["pid"] = None
    # Leave the checkout as the run found it, so the next task in this repository can launch.
    record["checkout"] = restore_checkout(worktree, before)
    refresh(record)  # downgrades to "interrupted" when the run is still open
    save_record(path, record)
    return record


def status(*, target: Path, explicit_repo: str, execution_id: str) -> dict:
    _, identity, common = _target(target, explicit_repo)
    path = execution_path(common, execution_id)
    record = load_record(path)
    if record is None:
        raise HeadlessError("EXECUTION_UNKNOWN", "no execution record for this id")
    if record["target_repository"].casefold() != identity.casefold():
        raise HeadlessError("EXECUTION_TARGET_MISMATCH", "execution id belongs to another repository")
    refresh(record)
    save_record(path, record)
    return record


def _finish_cancelled(run_id: str | None) -> None:
    if not run_id:
        return
    try:
        parent_orchestration.finish(run_id, "BLOCKED", blocker_code="EXECUTION_CANCELLED")
    except (parent_orchestration.ParentRunError, provenance.ProvenanceError, OSError):
        pass  # already terminal or unreadable; the refreshed run status tells which


def cancel(*, target: Path, explicit_repo: str, execution_id: str, grace: float = TERMINATE_GRACE_SECONDS) -> dict:
    """Stop the process if it is alive and close the parent run explicitly as cancelled."""
    _, identity, common = _target(target, explicit_repo)
    path = execution_path(common, execution_id)
    record = load_record(path)
    if record is None:
        raise HeadlessError("EXECUTION_UNKNOWN", "no execution record for this id")
    if record["target_repository"].casefold() != identity.casefold():
        raise HeadlessError("EXECUTION_TARGET_MISMATCH", "execution id belongs to another repository")
    refresh(record)
    if record.get("run_id") is None:
        record["run_id"] = discover_run_id(Path(record["session_dir"]), record["session_id"])
    # Close the run before signalling, so a graceful Pi shutdown that settles
    # the parent finds it already terminal with the cancellation code.
    _finish_cancelled(record.get("run_id"))
    pid = record.get("pid")
    if record["status"] == "launched" and pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + grace
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    record["pid"] = None
    if record.get("run_id") is None:
        # The process may have created its run between discovery and the signal.
        record["run_id"] = discover_run_id(Path(record["session_dir"]), record["session_id"])
        _finish_cancelled(record.get("run_id"))
    record["status"] = "cancelled"
    record["outcome"] = "cancelled"
    refresh(record)
    record["status"] = "cancelled"
    save_record(path, record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless single-repository orchestration run (start, status, resume, cancel).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("--target", type=Path, default=Path("."), help="target checkout (default: current directory)")
        p.add_argument("--repo", required=True, help="explicit owner/repository; must match a GitHub remote of the target")
        p.add_argument("--execution-id", required=True, help="caller-chosen idempotency key, filename-safe")
        if name == "start":
            p.add_argument("--objective", required=True)
            p.add_argument("--allow-mutation", action="store_true",
                           help="required when the target's autonomy policy grants any mutating permission")
            p.add_argument("--resume", action="store_true", help="relaunch an interrupted execution into the same parent run")
            p.add_argument("--no-wait", action="store_true", help="return after launching instead of waiting for exit")
            p.add_argument("--pi", default=os.environ.get("PI_EXECUTABLE", "pi"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            record = start(target=args.target, explicit_repo=args.repo, execution_id=args.execution_id,
                           objective=args.objective, allow_mutation=args.allow_mutation, resume=args.resume,
                           pi_executable=args.pi, wait=not args.no_wait)
        elif args.command == "status":
            record = status(target=args.target, explicit_repo=args.repo, execution_id=args.execution_id)
        else:
            record = cancel(target=args.target, explicit_repo=args.repo, execution_id=args.execution_id)
    except HeadlessError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}, sort_keys=True))
        return 1
    run = record.get("run") or {}
    ok = record["status"] in {"launched", "exited", "cancelled", "interrupted"} and (
        record["status"] != "exited" or run.get("status") in {"COMPLETED", "PARTIAL"})
    print(json.dumps({"ok": ok, **record}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
