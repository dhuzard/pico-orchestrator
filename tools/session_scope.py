#!/usr/bin/env python3
"""Repository scope check for backend worker sessions.

A parent orchestration run is bound to one target repository. A Claude worker
runs in the Pi process working directory, and a resumed backend session carries
state from wherever it was created, so the parent verifies before every worker
invocation that the worker directory belongs to the same Git repository as the
target. Linked worktrees of the target share its common Git directory and are
therefore in scope; any other repository, or a directory that is not a Git
checkout, fails closed.

Operations (JSON on stdin, closed JSON verdict on stdout):

  {"target_cwd", "worker_cwd"}                  same repository?
  {"operation": "worktree", "target_cwd", "path"}
      is `path` a linked worktree of the target, distinct from the checkout the
      parent works in? Mutating workers must be bound to one. A path with a
      `.git` component (for example `<repo>/.git/worktrees-x/s1`) is refused
      with WORKER_WORKSPACE_PROTECTED_PATH: Claude Code treats every `.git`
      directory as protected and denies headless edits there even in
      acceptEdits mode, so a worker bound to it cannot write at all.
  {"operation": "snapshot", "path"}
      digest of a checkout's HEAD, index, working-tree changes, and untracked
      files, used to detect a worker writing outside its bound worktree.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def common_dir(path: Path, *, run=subprocess.run) -> Path | None:
    try:
        result = run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                     shell=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def check(target_cwd: str, worker_cwd: str, *, run=subprocess.run) -> dict:
    target = common_dir(Path(target_cwd), run=run)
    if target is None:
        return {"ok": False, "blocker_code": "SESSION_TARGET_UNRESOLVED"}
    worker = common_dir(Path(worker_cwd), run=run)
    if worker is None or worker != target:
        return {"ok": False, "blocker_code": "SESSION_REPOSITORY_MISMATCH"}
    return {"ok": True, "blocker_code": None}


def _git_out(path: Path, *args: str, run=subprocess.run) -> bytes | None:
    try:
        result = run(["git", "-C", str(path), *args], shell=False, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _toplevel(path: Path, *, run=subprocess.run) -> Path | None:
    out = _git_out(path, "rev-parse", "--show-toplevel", run=run)
    return Path(out.decode().strip()).resolve() if out and out.strip() else None


def in_git_dir(path: Path) -> bool:
    """True when any component of the resolved path is a `.git` directory name."""
    return ".git" in path.resolve().parts


def worktree(target_cwd: str, path: str, *, run=subprocess.run) -> dict:
    if not check(target_cwd, path, run=run)["ok"]:
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_INVALID"}
    target_top, workspace_top = _toplevel(Path(target_cwd), run=run), _toplevel(Path(path), run=run)
    if workspace_top is None or workspace_top == target_top or workspace_top != Path(path).resolve():
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_INVALID"}
    if in_git_dir(workspace_top):
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_PROTECTED_PATH"}
    return {"ok": True, "blocker_code": None, "path": str(workspace_top)}


def snapshot(path: str, *, run=subprocess.run) -> dict:
    root = Path(path)
    head = _git_out(root, "rev-parse", "HEAD", run=run) or b"(no commit)"
    status = _git_out(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", run=run)
    if status is None:
        return {"ok": False, "blocker_code": "SESSION_TARGET_UNRESOLVED"}
    diff = _git_out(root, "diff", "HEAD", "--binary", run=run) or b""
    digest = hashlib.sha256(head + b"\0" + status + b"\0" + diff)
    entries = [entry for entry in status.decode(errors="replace").split("\0") if entry]
    untracked = [entry[3:] for entry in entries if entry.startswith("?? ")]
    if untracked:
        hashes = _git_out(root, "hash-object", "--", *untracked, run=run) or b""
        digest.update(hashes)
    return {"ok": True, "digest": digest.hexdigest(), "status": entries[:200]}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        operation = payload.get("operation", "check")
        if operation == "snapshot":
            if not isinstance(payload["path"], str):
                raise TypeError
            print(json.dumps(snapshot(payload["path"])))
            return 0
        target_cwd = payload["target_cwd"]
        other = payload["path"] if operation == "worktree" else payload["worker_cwd"]
        if operation not in {"check", "worktree"} or not isinstance(target_cwd, str) or not isinstance(other, str):
            raise TypeError
    except (ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"ok": False, "blocker_code": "SESSION_SCOPE_INPUT_INVALID"}))
        return 0
    print(json.dumps(worktree(target_cwd, other) if operation == "worktree" else check(target_cwd, other)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
