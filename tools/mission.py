#!/usr/bin/env python3
"""Mission CLI: deterministic goal-level state and reconciliation.

This CLI intentionally does not invoke route_task, agents, git, gh, push, PR, or
merge operations. It is the local durable state boundary for a mission
controller; /orchestrate remains the bounded execution engine for one task.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mission_store as store  # noqa: E402


def _json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _repo_root(args: argparse.Namespace) -> Path:
    return Path(args.target or args.repo_root).resolve()


def state_root() -> Path:
    explicit = os.environ.get("PI_ORCHESTRATOR_STATE_DIR")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise ValueError("PI_ORCHESTRATOR_STATE_DIR must be an absolute path")
        return path
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "pi-coding-orchestrator"


def target_missions_file(target: Path) -> Path:
    """Missions for an external target live in orchestrator state, keyed by its
    Git repository (linked worktrees share one file), never in the target."""
    target = target.resolve()
    try:
        common = subprocess.run(["git", "-C", str(target), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True, timeout=10)
        identity = Path(common.stdout.strip()).resolve() if common.returncode == 0 and common.stdout.strip() else target
    except (OSError, subprocess.TimeoutExpired):
        identity = target
    name = (identity.parent if identity.name == ".git" else identity).name or "root"
    key = f"{name}-{hashlib.sha256(str(identity).encode()).hexdigest()[:12]}"
    return state_root() / "missions" / key / "missions.yaml"


def _select_storage(args: argparse.Namespace) -> None:
    if not args.target:
        return
    path = target_missions_file(Path(args.target))
    legacy = Path(args.target).resolve() / store.MISSIONS_RELATIVE_PATH
    if legacy.is_file() and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, path)
        print(f"Copied legacy target-local missions to {path}; {legacy} can be removed.", file=sys.stderr)
    store.use_missions_file(path)


def cmd_create(args: argparse.Namespace) -> None:
    mission = store.mission_add(
        id=args.id,
        goal=args.goal,
        backlog_items=args.backlog_item,
        success_criteria=args.success_criterion,
        status=args.status,
        base_branch=args.base_branch,
        base_sha=args.base_sha,
        autonomy_profile=args.autonomy_profile,
        max_parallel_tasks=args.max_parallel_tasks,
        max_agent_runs=args.max_agent_runs,
        max_repair_attempts_per_task=args.max_repair_attempts_per_task,
        repo_root=_repo_root(args),
    )
    _json(mission)


def cmd_list(args: argparse.Namespace) -> None:
    _json(store.mission_list(_repo_root(args), status=args.status))


def cmd_show(args: argparse.Namespace) -> None:
    _json(store.mission_get(args.id, _repo_root(args)))


def cmd_update_status(args: argparse.Namespace, status: str) -> None:
    _json(store.mission_update(args.id, _repo_root(args), status=status))


def cmd_reconcile(args: argparse.Namespace) -> None:
    _json(store.reconcile_mission(args.id, _repo_root(args)))


def cmd_add_backlog_item(args: argparse.Namespace) -> None:
    repo_root = _repo_root(args)
    mission = store.mission_get(args.id, repo_root)
    backlog_items = list(mission.get("backlog_items", []))
    if args.backlog_item not in backlog_items:
        backlog_items.append(args.backlog_item)
    _json(store.mission_update(args.id, repo_root, backlog_items=backlog_items))


def cmd_plan_backlog(args: argparse.Namespace) -> None:
    _json(store.plan_backlog_dag(
        args.id,
        _repo_root(args),
        version=args.version,
        priority=args.priority,
        status=args.status,
        labels=args.label,
        link_existing=not args.no_link,
    ))


def cmd_validate(args: argparse.Namespace) -> None:
    repo_root = _repo_root(args)
    missions = store.load_missions(repo_root)
    issues = store.validate_mission_collection(missions, store.load_schema(repo_root), repo_root=repo_root)
    if issues:
        _json({"ok": False, "issues": issues})
        raise SystemExit(1)
    _json({"ok": True, "missions": len(missions)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage deterministic mission state")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="repository root containing schemas/, backlog/, and missions/")
    parser.add_argument("--target", help="external target repository: backlog is read from it, missions are stored in the orchestrator state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a mission")
    create.add_argument("--id")
    create.add_argument("--goal", required=True)
    create.add_argument("--backlog-item", action="append", default=[], help="ORCH-* backlog item reference; repeatable")
    create.add_argument("--success-criterion", action="append", default=[], help="mission success criterion; repeatable")
    create.add_argument("--status", choices=store.STATUSES, default="planned")
    create.add_argument("--base-branch")
    create.add_argument("--base-sha")
    create.add_argument("--autonomy-profile", choices=["manual", "guarded"], default="guarded")
    create.add_argument("--max-parallel-tasks", type=int, default=3)
    create.add_argument("--max-agent-runs", type=int, default=20)
    create.add_argument("--max-repair-attempts-per-task", type=int, default=2)
    create.set_defaults(func=cmd_create)

    list_p = sub.add_parser("list", help="list missions")
    list_p.add_argument("--status", choices=store.STATUSES)
    list_p.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one mission")
    show.add_argument("id")
    show.set_defaults(func=cmd_show)

    reconcile = sub.add_parser("reconcile", help="observe backlog and complete mission if all items are completed")
    reconcile.add_argument("id")
    reconcile.set_defaults(func=cmd_reconcile)

    add_item = sub.add_parser("add-backlog-item", help="link an existing backlog item to a non-terminal mission")
    add_item.add_argument("id")
    add_item.add_argument("backlog_item")
    add_item.set_defaults(func=cmd_add_backlog_item)

    plan = sub.add_parser("plan-backlog", help="create/link a deterministic backlog DAG from '- ORCH-###: title' mission-goal bullets")
    plan.add_argument("id")
    plan.add_argument("--version", default="mission-plan", help="version assigned to newly-created backlog items")
    plan.add_argument("--priority", choices=["low", "medium", "high", "critical"], default="high")
    plan.add_argument("--status", choices=["proposed", "planned"], default="planned")
    plan.add_argument("--label", action="append", default=None, help="label for newly-created backlog items; repeatable; default: mission, planned")
    plan.add_argument("--no-link", action="store_true", help="create missing items but do not link the mission to parsed backlog ids")
    plan.set_defaults(func=cmd_plan_backlog)

    for name, status in (("resume", "running"), ("pause", "paused"), ("cancel", "cancelled")):
        p = sub.add_parser(name, help=f"transition a mission to {status}")
        p.add_argument("id")
        p.set_defaults(func=lambda args, status=status: cmd_update_status(args, status))

    validate = sub.add_parser("validate", help="validate mission storage")
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _select_storage(args)
        args.func(args)
        return 0
    except (store.MissionError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
