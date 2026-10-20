#!/usr/bin/env python3
"""Durable mission state for goal-level delivery control.

A mission is a persistent objective that references backlog items. It does not
copy task status, routing decisions, PR data, worker attribution, or run
provenance; those remain in backlog/backlog.yaml and provenance/runs/*.json.
"""

from __future__ import annotations

import copy
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Missing dependency: jsonschema. Run: python3 -m pip install -r requirements-dev.txt"
    ) from exc

import backlog_store as backlog


class MissionError(Exception):
    """Base class for mission store errors."""


class MissionSchemaValidationError(MissionError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


class MissionNotFoundError(MissionError):
    pass


class DuplicateMissionIdError(MissionError):
    pass


class MissionTransitionError(MissionError):
    pass


class MissionCompletionError(MissionError):
    def __init__(self, mission_id: str, incomplete: list[str]):
        self.mission_id = mission_id
        self.incomplete = incomplete
        super().__init__(f"{mission_id}: cannot complete mission; incomplete backlog items: {incomplete}")


STATUSES = ("planned", "running", "paused", "completed", "cancelled")
TERMINAL_STATUSES = frozenset({"completed", "cancelled"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"running", "paused", "cancelled"}),
    "running": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

ID_PATTERN = re.compile(r"^MISSION-([0-9]{3,})$")
BACKLOG_BULLET_PATTERN = re.compile(r"^\s*[-*]\s*(ORCH-[0-9]{3,})\s*:\s*(\S.*)$")
REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
MISSIONS_RELATIVE_PATH = Path("missions") / "missions.yaml"
SCHEMA_RELATIVE_PATH = Path("schemas") / "mission.schema.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_rfc3339_datetime(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return "T" in value
    except ValueError:
        return False


# Set by `use_missions_file` when missions for an external target live in the
# orchestrator state directory instead of the repository.
_missions_file: Path | None = None


def use_missions_file(path: Path | None) -> None:
    global _missions_file
    _missions_file = path


def missions_path(repo_root: Path) -> Path:
    return _missions_file if _missions_file is not None else repo_root / MISSIONS_RELATIVE_PATH


def schema_path(repo_root: Path) -> Path:
    return repo_root / SCHEMA_RELATIVE_PATH


def load_schema(repo_root: Path = REPO_ROOT_DEFAULT) -> dict:
    path = schema_path(repo_root)
    if not path.exists():
        path = schema_path(REPO_ROOT_DEFAULT)
    with path.open("r", encoding="utf-8") as f:
        import json

        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def validate_mission_schema(mission: Any, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    issues: list[str] = []
    for e in sorted(validator.iter_errors(mission), key=str):
        path = "/".join(str(p) for p in e.absolute_path)
        issues.append(f"{path or '<root>'}: {e.message}")
    if isinstance(mission, dict):
        for field in ("created_at", "updated_at"):
            value = mission.get(field)
            if value is not None and not _is_rfc3339_datetime(value):
                issues.append(f"{field}: {value!r} is not a valid date-time")
    return issues


def load_missions(repo_root: Path = REPO_ROOT_DEFAULT) -> list[dict]:
    path = missions_path(repo_root)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return []
    if not isinstance(data, list):
        raise MissionError(f"{path}: top-level YAML value must be a list of missions")
    return data


def save_missions(missions: Iterable[dict], repo_root: Path = REPO_ROOT_DEFAULT) -> None:
    path = missions_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(missions, key=lambda mission: mission["id"])
    fd, tmp_name = tempfile.mkstemp(prefix=".missions.", suffix=".yaml.tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(ordered, f, sort_keys=True, allow_unicode=True)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def generate_next_id(missions: Iterable[dict]) -> str:
    max_n = 0
    for mission in missions:
        m = ID_PATTERN.match(mission.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"MISSION-{max_n + 1:03d}"


def _load_valid_backlog(repo_root: Path) -> list[dict]:
    items = backlog.load_backlog(repo_root)
    schema_root = repo_root if backlog.schema_path(repo_root).exists() else REPO_ROOT_DEFAULT
    schema = backlog.load_schema(schema_root)
    issues = backlog.validate_backlog_collection(items, schema)
    if issues:
        raise MissionSchemaValidationError([f"backlog: {issue}" for issue in issues])
    return items


def validate_mission_collection(missions: list[dict], schema: dict, *, repo_root: Path = REPO_ROOT_DEFAULT) -> list[str]:
    issues: list[str] = []
    seen_ids: dict[str, int] = {}
    for idx, mission in enumerate(missions):
        mission_issues = validate_mission_schema(mission, schema)
        label = mission.get("id", f"<mission[{idx}] missing id>") if isinstance(mission, dict) else f"<mission[{idx}]>"
        for msg in mission_issues:
            issues.append(f"{label}: {msg}")
        if isinstance(mission, dict) and "id" in mission:
            if mission["id"] in seen_ids:
                issues.append(f"duplicate mission id: {mission['id']!r}")
            else:
                seen_ids[mission["id"]] = idx
    if issues:
        return issues

    try:
        backlog_items = _load_valid_backlog(repo_root)
    except MissionSchemaValidationError as exc:
        return exc.issues
    backlog_by_id = {item["id"]: item for item in backlog_items}
    for mission in missions:
        for item_id in mission.get("backlog_items", []):
            if item_id not in backlog_by_id:
                issues.append(f"{mission['id']}: backlog item {item_id!r} does not exist")
        current_refs = set(mission.get("current", {}).get("running", []))
        current_refs.update(mission.get("current", {}).get("waiting_ci", []))
        current_refs.update(mission.get("current", {}).get("blocked", []))
        missing_current = sorted(current_refs.difference(mission.get("backlog_items", [])))
        if missing_current:
            issues.append(f"{mission['id']}: current references are not mission backlog_items: {missing_current}")
    return issues


def _assert_collection_valid_after(missions: list[dict], repo_root: Path) -> None:
    issues = validate_mission_collection(missions, load_schema(repo_root), repo_root=repo_root)
    if issues:
        raise MissionSchemaValidationError(issues)


def mission_list(repo_root: Path = REPO_ROOT_DEFAULT, *, status: str | None = None) -> list[dict]:
    missions = load_missions(repo_root)
    if status is not None:
        missions = [mission for mission in missions if mission.get("status") == status]
    return sorted(missions, key=lambda mission: mission["id"])


def mission_get(id: str, repo_root: Path = REPO_ROOT_DEFAULT) -> dict:
    for mission in load_missions(repo_root):
        if mission.get("id") == id:
            return copy.deepcopy(mission)
    raise MissionNotFoundError(f"no mission with id {id!r}")


def parse_backlog_bullets(goal: str) -> list[dict[str, str]]:
    """Extract an ordered backlog plan from explicit goal bullets.

    The planner is deliberately deterministic and conservative: it only treats
    lines of the form ``- ORCH-123: Title`` (or ``* ORCH-123: Title``) as
    backlog items. Free-form prose is ignored rather than guessed into tasks.
    """
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in goal.splitlines():
        match = BACKLOG_BULLET_PATTERN.match(line)
        if not match:
            continue
        item_id, title = match.groups()
        if item_id in seen:
            raise MissionSchemaValidationError([f"mission goal contains duplicate backlog item id {item_id!r}"])
        seen.add(item_id)
        parsed.append({"id": item_id, "title": title.strip()})
    return parsed


def _planned_description(mission: dict, item_id: str, title: str) -> str:
    return (
        f"Generated from {mission['id']} as part of its bounded delivery backlog DAG: {title}. "
        "This item is one bounded engineering task and must preserve /orchestrate "
        "as the one-task execution engine; scheduler, git push/branch, PR/CI, "
        "merge, deploy, and auto-merge behavior must remain within their own "
        "explicitly-scoped backlog items."
    )


def _planned_acceptance_criteria(title: str) -> list[str]:
    return [
        f"The bounded task '{title}' is implemented only within its explicit scope.",
        "The change preserves /orchestrate as a bounded one-task execution engine and does not introduce unrelated persistent-controller behavior.",
        "Backlog, mission state, GitHub/PR state, and run provenance boundaries remain separate and deterministic.",
        "Relevant deterministic tests and validation commands cover the behavior introduced by this item.",
    ]


def plan_backlog_dag(
    mission_id: str,
    repo_root: Path = REPO_ROOT_DEFAULT,
    *,
    version: str = "mission-plan",
    priority: str = "high",
    status: str = "planned",
    labels: list[str] | None = None,
    link_existing: bool = True,
) -> dict:
    """Create/link a deterministic backlog DAG from explicit mission bullets.

    Missing bullet items are created in listed order. Each newly-created item
    depends on the previous bullet item, yielding a simple deterministic DAG
    suitable for the common "next PRs" mission shape. Existing items are not
    rewritten; when ``link_existing`` is true they are only linked to the
    mission. This function never touches GitHub, git branches, PRs, routing, or
    run provenance.
    """
    if status not in {"proposed", "planned"}:
        raise MissionTransitionError("mission backlog planning can only create proposed or planned items")
    mission = mission_get(mission_id, repo_root)
    planned = parse_backlog_bullets(mission["goal"])
    if not planned:
        raise MissionSchemaValidationError([f"{mission_id}: mission goal contains no '- ORCH-###: title' backlog bullets"])

    existing_by_id = {item["id"]: item for item in _load_valid_backlog(repo_root)}
    created: list[dict] = []
    existing: list[str] = []
    labels = labels or ["mission", "planned"]

    previous_id: str | None = None
    for spec in planned:
        item_id = spec["id"]
        title = spec["title"]
        if item_id in existing_by_id:
            existing.append(item_id)
        else:
            dependencies = [previous_id] if previous_id is not None else []
            item = backlog.backlog_add(
                id=item_id,
                title=title,
                description=_planned_description(mission, item_id, title),
                version=version,
                priority=priority,
                status=status,
                dependencies=dependencies,
                acceptance_criteria=_planned_acceptance_criteria(title),
                labels=labels,
                repo_root=repo_root,
            )
            created.append(item)
            existing_by_id[item_id] = item
        previous_id = item_id

    linked = list(mission.get("backlog_items", []))
    if link_existing:
        for spec in planned:
            if spec["id"] not in linked:
                linked.append(spec["id"])
    updated_mission = mission_update(mission_id, repo_root, backlog_items=linked) if linked != mission.get("backlog_items", []) else mission
    return {
        "mission": updated_mission,
        "planned_items": planned,
        "created": created,
        "existing": existing,
        "linked_backlog_items": linked,
    }


def mission_add(
    *,
    goal: str,
    backlog_items: list[str] | None = None,
    success_criteria: list[str] | None = None,
    id: str | None = None,
    status: str = "planned",
    base_branch: str | None = None,
    base_sha: str | None = None,
    autonomy_profile: str = "guarded",
    max_parallel_tasks: int = 3,
    max_agent_runs: int = 20,
    max_repair_attempts_per_task: int = 2,
    repo_root: Path = REPO_ROOT_DEFAULT,
) -> dict:
    missions = load_missions(repo_root)
    mission_id = id or generate_next_id(missions)
    if any(mission.get("id") == mission_id for mission in missions):
        raise DuplicateMissionIdError(f"duplicate mission id: {mission_id!r}")
    now = now_iso()
    mission = {
        "id": mission_id,
        "goal": goal,
        "status": status,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "success_criteria": success_criteria or [],
        "backlog_items": backlog_items or [],
        "policy": {"autonomy_profile": autonomy_profile},
        "budgets": {
            "max_parallel_tasks": max_parallel_tasks,
            "max_agent_runs": max_agent_runs,
            "max_repair_attempts_per_task": max_repair_attempts_per_task,
        },
        "current": {"running": [], "waiting_ci": [], "blocked": []},
        "created_at": now,
        "updated_at": now,
    }
    new_missions = [*missions, mission]
    if status == "completed":
        _assert_completion_allowed(mission, repo_root)
    _assert_collection_valid_after(new_missions, repo_root)
    save_missions(new_missions, repo_root)
    return copy.deepcopy(mission)


def allowed_targets(status: str) -> frozenset[str]:
    if status not in TRANSITIONS:
        raise MissionTransitionError(f"unknown mission status: {status!r}")
    return TRANSITIONS[status]


def incomplete_backlog_items(mission: dict, repo_root: Path = REPO_ROOT_DEFAULT) -> list[str]:
    backlog_by_id = {item["id"]: item for item in _load_valid_backlog(repo_root)}
    incomplete: list[str] = []
    for item_id in mission.get("backlog_items", []):
        item = backlog_by_id.get(item_id)
        if item is None or item.get("status") != "completed":
            incomplete.append(item_id)
    return incomplete


def _assert_completion_allowed(mission: dict, repo_root: Path) -> None:
    if not mission.get("backlog_items"):
        raise MissionCompletionError(mission["id"], ["<no backlog items>"])
    incomplete = incomplete_backlog_items(mission, repo_root)
    if incomplete:
        raise MissionCompletionError(mission["id"], incomplete)


def mission_update(
    id: str,
    repo_root: Path = REPO_ROOT_DEFAULT,
    *,
    status: str | None = None,
    goal: str | None = None,
    backlog_items: list[str] | None = None,
    success_criteria: list[str] | None = None,
    current: dict | None = None,
) -> dict:
    missions = load_missions(repo_root)
    for idx, existing in enumerate(missions):  # noqa: B007 - idx is used after the loop (missions[idx])
        if existing.get("id") == id:
            mission = copy.deepcopy(existing)
            break
    else:
        raise MissionNotFoundError(f"no mission with id {id!r}")

    changed = False
    if status is not None and status != mission["status"]:
        if status not in allowed_targets(mission["status"]):
            raise MissionTransitionError(f"{id}: cannot transition from {mission['status']!r} to {status!r}")
        if status == "completed":
            _assert_completion_allowed(mission, repo_root)
        mission["status"] = status
        changed = True
    for field, value in (("goal", goal), ("backlog_items", backlog_items), ("success_criteria", success_criteria), ("current", current)):
        if value is not None and mission.get(field) != value:
            if mission["status"] in TERMINAL_STATUSES and field != "current":
                raise MissionTransitionError(f"{id}: cannot modify terminal mission")
            mission[field] = value
            changed = True
    if changed:
        mission["updated_at"] = now_iso()
    missions[idx] = mission
    _assert_collection_valid_after(missions, repo_root)
    if changed:
        save_missions(missions, repo_root)
    return copy.deepcopy(mission)


def reconcile_mission(id: str, repo_root: Path = REPO_ROOT_DEFAULT) -> dict:
    """Observe backlog state and advance mission only when deterministic.

    Reconciliation never mutates backlog, GitHub, git, routing policy, or run
    provenance. It only completes a running mission when every referenced
    backlog item is already completed.
    """
    mission = mission_get(id, repo_root)
    incomplete = incomplete_backlog_items(mission, repo_root)
    if not mission.get("backlog_items"):
        incomplete = ["<no backlog items>"]
    changed = False
    if mission["status"] == "running" and not incomplete:
        mission = mission_update(id, repo_root, status="completed")
        changed = True
    return {"mission": mission, "incomplete_backlog_items": incomplete, "changed": changed}
