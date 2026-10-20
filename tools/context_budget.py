#!/usr/bin/env python3
"""Optional checkpoint policy and structured checkpoints for parent orchestration.

Subcommands (JSON on stdin where noted, one JSON object on stdout):

  policy                 Validated routing/context-policy.yaml.
  start RUN_ID           stdin {cwd}: record the run's starting commit.
  checkpoint RUN_ID      stdin {cwd, reason, tokens_before, files_touched, state}:
                         collect git/orchestration state deterministically,
                         validate the checkpoint, persist it under
                         <state>/checkpoints/<owner>__<repo>/<run_id>/, and
                         link it into the parent run record.

Checkpoints are operational state, never transcripts: every string is length
bounded by schemas/context-checkpoint.schema.json and secret-scanned before
anything is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import parent_orchestration  # noqa: E402
import provenance  # noqa: E402
import routing_engine as engine  # noqa: E402

POLICY_PATH = ROOT / "routing" / "context-policy.yaml"
POLICY_SCHEMA = ROOT / "schemas" / "context-policy.schema.json"
CHECKPOINT_SCHEMA = ROOT / "schemas" / "context-checkpoint.schema.json"
MAX_PATHS = 200
MAX_PATH_CHARS = 300


class ContextBudgetError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def load_policy(path: Path = POLICY_PATH) -> dict:
    policy = engine.load_yaml(path)
    issues = engine.validate_instance(policy, engine.load_json(POLICY_SCHEMA), label="context_policy")
    if issues:
        raise ContextBudgetError("CONTEXT_POLICY_INVALID", "; ".join(str(issue) for issue in issues))
    return policy


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], shell=False,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _commit(cwd: Path) -> str | None:
    value = (_git(cwd, "rev-parse", "HEAD") or "").strip()
    return value or None


def git_state(cwd: Path) -> dict:
    branch = (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    porcelain = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all") or ""
    dirty: list[str] = []
    entries = iter(porcelain.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            next(entries, None)  # skip the rename/copy source path
        dirty.append(path)
    return {"branch": branch, "current_commit": _commit(cwd), "dirty_files": _bounded_paths(dirty, cwd)}


def _bounded_paths(paths: Any, cwd: Path) -> list[str]:
    if not isinstance(paths, list):
        return []
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    result: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not value:
            continue
        if top and Path(value).is_absolute():
            try:
                value = str(Path(value).resolve().relative_to(Path(top).resolve()))
            except ValueError:
                pass
        if len(value) <= MAX_PATH_CHARS and value not in result:
            result.append(value)
        if len(result) >= MAX_PATHS:
            break
    return sorted(result)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _checkpoints_dir(repo_root: Path) -> Path:
    return provenance.runs_dir(repo_root).parent / "checkpoints"


def _repository_slug(repository: str | None) -> str:
    return repository.replace("/", "__") if repository else "_unresolved"


def start(run_id: str, cwd: Path, *, repo_root: Path = ROOT) -> dict:
    parent_orchestration.record_event(run_id, {"kind": "context", "starting_commit": _commit(cwd)},
                                      repo_root=repo_root)
    return {"ok": True}


def checkpoint(run_id: str, payload: dict, *, repo_root: Path = ROOT) -> dict:
    # Sequence selection, file publication, and the parent link are one
    # operation; concurrent checkpoints must not pick the same sequence.
    with parent_orchestration._run_lock(repo_root, run_id):
        return _checkpoint_unlocked(run_id, payload, repo_root=repo_root)


def _checkpoint_unlocked(run_id: str, payload: dict, *, repo_root: Path) -> dict:
    record = provenance.get_run(repo_root, run_id)
    if record["outcome"] != "in_progress":
        raise ContextBudgetError("CHECKPOINT_RUN_TERMINAL", "parent run is terminal")
    parent = record.get("parent_orchestration") or {}
    context = parent.get("context") or {}
    cwd = Path(payload["cwd"])
    files = payload.get("files_touched") or {}
    repository = parent.get("target_repository")
    sequence = len(context.get("checkpoints") or []) + 1
    document = {
        "schema_version": 1,
        "run_id": run_id,
        "repository": repository,
        "sequence": sequence,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": payload.get("reason"),
        "tokens_before": payload.get("tokens_before"),
        "git": {"starting_commit": context.get("starting_commit"), **git_state(cwd)},
        "orchestration": {
            "policy_version": parent.get("policy_version"),
            "phases": [{"id": phase["id"], "status": phase["status"],
                        "route": (phase.get("escalations") or [{}])[-1].get("to_route") or phase["route"]}
                       for phase in parent.get("phases") or []],
        },
        "files_touched": {"read": _bounded_paths(files.get("read"), cwd),
                          "modified": _bounded_paths(files.get("modified"), cwd)},
        "state": payload.get("state"),
    }
    issues = engine.validate_instance(document, engine.load_json(CHECKPOINT_SCHEMA), label="checkpoint")
    if issues:
        raise ContextBudgetError("CHECKPOINT_INVALID", str(issues[0]))
    if any(provenance._contains_secret_like_content(text) for text in _strings(document)):
        raise ContextBudgetError("CHECKPOINT_SECRET_LIKE_CONTENT", "checkpoint contains secret-like content")

    reference = f"checkpoints/{_repository_slug(repository)}/{run_id}/{sequence:04d}.json"
    path = _checkpoints_dir(repo_root).parent / reference
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)
    # The lock is already held; flock is per open file, so it is not re-entrant.
    parent_orchestration._record_event_unlocked(run_id, {"kind": "context", "compaction": {
        "sequence": sequence, "reference": reference, "reason": document["reason"],
        "tokens_before": document["tokens_before"], "structured": True,
    }}, repo_root=repo_root)
    return {"ok": True, "reference": reference, "checkpoint": document}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("policy")
    p_start = sub.add_parser("start")
    p_start.add_argument("run_id")
    p_checkpoint = sub.add_parser("checkpoint")
    p_checkpoint.add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "policy":
            result = {"ok": True, "policy": load_policy()}
        else:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict) or not isinstance(payload.get("cwd"), str):
                raise ContextBudgetError("CONTEXT_INPUT_INVALID", "payload requires cwd")
            if args.command == "start":
                result = start(args.run_id, Path(payload["cwd"]))
            else:
                result = checkpoint(args.run_id, payload)
    except ContextBudgetError as exc:
        print(json.dumps({"ok": False, "blocker_code": exc.code, "error": str(exc)}))
        return 1
    except parent_orchestration.ParentRunError as exc:
        print(json.dumps({"ok": False, "blocker_code": exc.error_code, "error": str(exc)}))
        return 1
    except (provenance.ProvenanceError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "blocker_code": "CONTEXT_STORAGE_UNAVAILABLE",
                          "error": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
