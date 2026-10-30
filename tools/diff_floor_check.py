#!/usr/bin/env python3
"""Post-diff classification check: real changed paths against path floors.

The lifecycle extension calls this twice per wave, the only bracket through
which a wave's work reaches the parent checkout:

- at `integrate_begin`, once per worktree bound for each task in the wave
  (operation `worktree`, the default): the worker's changes, before the merge;
- at `integrate_end` (operation `parent`): the parent checkout's own change
  since integration began, which is what was actually merged, checked against
  every member task's route result.

This adapter only collects changed paths; the decision is
`routing_engine.check_diff_path_floors`, the single evaluator.

stdin, operation `worktree`:
  {"route_task_result": {...}, "worktree": <bound worktree>, "parent_checkout": <path>,
   "allow_missing": <bool, optional>}
  A worktree from an earlier route epoch that no longer exists is reported
  `missing` (not blocked) only with `allow_missing`: its files can no longer be
  merged, and a branch merged from it is caught at `integrate_end`.
stdin, operation `parent`:
  {"operation": "parent", "parent_checkout": <path>, "base": <commit before integration>,
   "members": [{"task_id", "route_task_result", "exclude_paths": [...]}, ...]}
  Each member is checked against the merged change minus `exclude_paths`, the
  paths only the other members' worktrees changed; a path no worktree diff
  explains is checked against every member.
stdout: the engine's verdict ("ok", "blocked", "blocker_code", ...) plus
  "base", "head" and "changed_paths" (worktree) or "verdicts" (parent).
  CLASSIFICATION_DIFF_UNVERIFIABLE (ok: false) when a checkout, its diff, a
  path name or the route result cannot be verified.

Changed paths are every path that differs between the base (the merge base of
the worktree's HEAD and the parent's HEAD, or the integration base) and the
working tree (committed, staged and unstaged, both sides of a rename,
deletions), plus untracked files that are not ignored. A tracked file marked
assume-unchanged or skip-worktree hides its edits from Git, so its presence is
unverifiable; so is a path that is not valid UTF-8. Exit 0 for every verdict;
nonzero only for a bootstrap failure of the routing assets.
"""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_engine as engine  # noqa: E402
import session_scope  # noqa: E402


def _fail(message: str) -> NoReturn:
    print(f"diff_floor_check: {message}", file=sys.stderr)
    sys.exit(1)


def _unverifiable(message: str, path: str = "worktree") -> dict:
    return {"ok": False, "blocked": True, "blocker_code": engine.CLASSIFICATION_DIFF_UNVERIFIABLE,
            "errors": [{"path": path, "message": message}]}


def _names(raw: bytes) -> list[str] | None:
    try:
        return [entry for entry in raw.decode("utf-8").split("\0") if entry]
    except UnicodeDecodeError:
        return None


def _git(root: Path, *args: str) -> bytes | None:
    return session_scope._git_out(root, *args)


def working_changes(root: Path, base: str) -> list[str] | dict:
    """Paths changed in `root`'s working tree since `base`, or an unverifiable verdict."""
    hidden = _git(root, "ls-files", "-v", "-z")
    tracked = _git(root, "diff", "--name-only", "--no-renames", "-z", base, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if hidden is None or tracked is None or untracked is None:
        return _unverifiable("the diff could not be read")
    flags = _names(hidden)
    if flags is None or any(entry[:1].islower() or entry[:1] == "S" for entry in flags):
        return _unverifiable("a tracked file is marked assume-unchanged or skip-worktree, so Git hides its edits")
    names = _names(tracked + b"\0" + untracked)
    if names is None:
        return _unverifiable("a changed path is not valid UTF-8")
    return sorted(set(names))


def worktree_paths(worktree: str, parent_checkout: str) -> tuple[str, str, list[str]] | dict:
    """The diff base, the worktree's HEAD, and its changed paths, or an unverifiable verdict."""
    if session_scope.worktree(parent_checkout, worktree)["ok"] is not True:
        return _unverifiable("the path is not a linked worktree of the parent checkout's repository")
    root = Path(worktree)
    parent_head = _git(Path(parent_checkout), "rev-parse", "--verify", "HEAD")
    worker_head = _git(root, "rev-parse", "--verify", "HEAD")
    if not parent_head or not worker_head:
        return _unverifiable("the worktree or the parent checkout has no commit")
    head = worker_head.decode().strip()
    base = _git(root, "merge-base", head, parent_head.decode().strip())
    if not base:
        return _unverifiable("the worktree shares no history with the parent checkout")
    base_id = base.decode().strip()
    changed = working_changes(root, base_id)
    return changed if isinstance(changed, dict) else (base_id, head, changed)


def check_worktree(payload: dict, assets: engine.PolicyAssets) -> dict:
    if payload.get("allow_missing") is True and not Path(payload["worktree"]).exists():
        return {"ok": True, "blocked": False, "blocker_code": None, "missing": True, "diff_path_floor_matches": []}
    collected = worktree_paths(payload["worktree"], payload["parent_checkout"])
    if isinstance(collected, dict):
        return collected
    base, head, paths = collected
    verdict = engine.check_diff_path_floors(payload["route_task_result"], paths, assets)
    return {**verdict, "base": base, "head": head, "changed_paths": paths}


def check_parent(payload: dict, assets: engine.PolicyAssets) -> dict:
    root = Path(payload["parent_checkout"])
    base = payload.get("base")
    members = payload.get("members")
    if (not isinstance(base, str) or not isinstance(members, list) or not members
            or not all(isinstance(m, dict) and isinstance(m.get("exclude_paths", []), list) for m in members)):
        return _unverifiable("expected base and members", "members")
    if not _git(root, "rev-parse", "--verify", f"{base}^{{commit}}"):
        return _unverifiable("the integration base is not a commit of the parent checkout", "base")
    changed = working_changes(root, base)
    if isinstance(changed, dict):
        return changed
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    head_raw = _git(root, "rev-parse", "--verify", "HEAD")
    head = head_raw.decode("ascii").strip() if head_raw else None
    # Only a clean tree is represented by HEAD. Uncommitted changes can still be checked,
    # but they cannot supply commit-bound publication evidence.
    evidence = status == b"" and head is not None and len(changed) <= 200
    verdicts = []
    for member in members:
        excluded = {path for path in member.get("exclude_paths", []) if isinstance(path, str)}
        attributed = [path for path in changed if path not in excluded]
        verdict = engine.check_diff_path_floors(member.get("route_task_result"), attributed, assets)
        digests = sorted({hashlib.sha256(path.encode("utf-8")).hexdigest() for path in attributed})
        verdicts.append({"task_id": member.get("task_id"), **verdict,
                         **({"path_digests": digests} if evidence else {})})
    unverifiable = [v for v in verdicts if v["ok"] is not True]
    blocked = [v for v in verdicts if v["blocked"]]
    code = (engine.CLASSIFICATION_DIFF_UNVERIFIABLE if unverifiable
            else engine.CLASSIFICATION_UNDERRATED_BY_DIFF if blocked else None)
    return {"ok": not unverifiable, "blocked": bool(blocked), "blocker_code": code, "base": base,
            "changed_path_count": len(changed), "verdicts": verdicts,
            **({"head": head} if evidence else {})}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        payload = None
    operation = payload.get("operation", "worktree") if isinstance(payload, dict) else None
    if operation == "parent":
        valid = isinstance(payload.get("parent_checkout"), str)
    else:
        valid = (operation == "worktree" and isinstance(payload.get("worktree"), str)
                 and isinstance(payload.get("parent_checkout"), str) and "route_task_result" in payload)
    if not valid:
        print(json.dumps(_unverifiable("expected route_task_result, worktree and parent_checkout")))
        return 0
    try:
        assets = engine.load_policy_assets(REPO_ROOT)
    except engine.RoutingAssetError as exc:
        _fail(f"bootstrap failure: {exc}")
    verdict = check_parent(payload, assets) if operation == "parent" else check_worktree(payload, assets)
    print(json.dumps(verdict, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
