#!/usr/bin/env python3
"""Post-diff classification check: a worker's real changed paths against path floors.

The lifecycle extension calls this at `integrate_begin`, the single point every
wave passes through before its workers' worktrees are merged into the parent
checkout, once per bound worktree of each task in the wave. This adapter only
collects the changed paths; the decision is
`routing_engine.check_diff_path_floors`, the single evaluator.

stdin:  {"route_task_result": {...}, "worktree": <bound worktree path>,
         "parent_checkout": <the parent's checkout path>}
stdout: the engine's verdict, plus "base" (the commit the diff is taken from):
        {"ok": true, "blocked": false, ...} no new floor, or none that raises;
        {"ok": true, "blocked": true, "blocker_code": "CLASSIFICATION_UNDERRATED_BY_DIFF", ...};
        {"ok": false, "blocked": true, "blocker_code": "CLASSIFICATION_DIFF_UNVERIFIABLE", ...}
        when the worktree, its diff or the route result cannot be verified.

Changed paths are every path that differs between the merge base of the
worktree's HEAD and the parent checkout's HEAD and the worktree's working tree
(committed, staged and unstaged, both sides of a rename, deletions included),
plus untracked files that are not ignored. Exit 0 for every verdict; nonzero
only for a bootstrap failure of the routing assets.
"""
from __future__ import annotations

import json
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


def _unverifiable(message: str) -> dict:
    return {"ok": False, "blocked": True, "blocker_code": engine.CLASSIFICATION_DIFF_UNVERIFIABLE,
            "errors": [{"path": "worktree", "message": message}]}


def changed_paths(worktree: str, parent_checkout: str) -> tuple[str, list[str]] | dict:
    """The diff base and the worktree's changed paths, or an unverifiable verdict."""
    if session_scope.worktree(parent_checkout, worktree)["ok"] is not True:
        return _unverifiable("the path is not a linked worktree of the parent checkout's repository")
    root = Path(worktree)
    parent_head = session_scope._git_out(Path(parent_checkout), "rev-parse", "--verify", "HEAD")
    worker_head = session_scope._git_out(root, "rev-parse", "--verify", "HEAD")
    if not parent_head or not worker_head:
        return _unverifiable("the worktree or the parent checkout has no commit")
    base = session_scope._git_out(root, "merge-base", worker_head.decode().strip(), parent_head.decode().strip())
    if not base:
        return _unverifiable("the worktree shares no history with the parent checkout")
    base_id = base.decode().strip()
    tracked = session_scope._git_out(root, "diff", "--name-only", "--no-renames", "-z", base_id, "--")
    untracked = session_scope._git_out(root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or untracked is None:
        return _unverifiable("the worktree diff could not be read")
    paths = {entry for entry in (tracked + b"\0" + untracked).decode(errors="replace").split("\0") if entry}
    return base_id, sorted(paths)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        payload = None
    if (not isinstance(payload, dict) or not isinstance(payload.get("worktree"), str)
            or not isinstance(payload.get("parent_checkout"), str) or "route_task_result" not in payload):
        print(json.dumps(_unverifiable("expected route_task_result, worktree and parent_checkout")))
        return 0
    try:
        assets = engine.load_policy_assets(REPO_ROOT)
    except engine.RoutingAssetError as exc:
        _fail(f"bootstrap failure: {exc}")
    collected = changed_paths(payload["worktree"], payload["parent_checkout"])
    if isinstance(collected, dict):
        print(json.dumps(collected, sort_keys=True))
        return 0
    base, paths = collected
    verdict = engine.check_diff_path_floors(payload["route_task_result"], paths, assets)
    print(json.dumps({**verdict, "base": base}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
