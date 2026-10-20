#!/usr/bin/env python3
"""Deterministic data model and storage for the local machine-readable backlog.

Storage layout decision: a single diff-friendly `backlog/backlog.yaml`
collection file (a YAML list of items, always re-serialized sorted by `id`)
rather than one-file-per-task. Rationale: the backlog is small (dozens, not
thousands, of items), most operations need to reason about the whole
dependency graph at once (cycle detection, "what's ready next", reporting),
and a single sorted file keeps `git diff` reviewable (each mutation touches
one list entry, and stable sort-by-id ordering means unrelated items never
move) without the directory-listing/merge overhead of one-file-per-task.
Run provenance, by contrast, is genuinely append-only and per-run, so it uses
one file per run under provenance/runs/ (see tools/provenance.py) -- a
different shape for a different access pattern, not an inconsistency.

This module contains no GitHub/`gh` calls and no route/execution-plan
predicates of its own -- it is imported by tools/backlog.py, which is the
only place `gh` is invoked from (via tools/github_sync.py).
"""

from __future__ import annotations

import copy
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - surfaced by callers
    raise ImportError(
        "Missing dependency: jsonschema. Run: python3 -m pip install -r requirements-dev.txt"
    ) from exc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BacklogError(Exception):
    """Base class for all backlog store errors."""


class SchemaValidationError(BacklogError):
    """One or more backlog items failed schema validation."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


class NotFoundError(BacklogError):
    """A referenced backlog item id does not exist."""


class DuplicateIdError(BacklogError):
    """An id collides with an existing backlog item."""


class CyclicDependencyError(BacklogError):
    """The dependency graph contains a cycle."""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"circular dependency detected: {' -> '.join(cycle)}")


class TransitionError(BacklogError):
    """A requested status transition is not permitted."""


class DependencyNotCompletedError(BacklogError):
    """A transition requires dependencies to be completed, and they are not."""

    def __init__(self, item_id: str, target_status: str, unmet: list[str]):
        self.item_id = item_id
        self.target_status = target_status
        self.unmet = unmet
        super().__init__(
            f"{item_id}: cannot transition to {target_status!r}; unmet (non-completed) "
            f"dependencies: {unmet}"
        )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATUSES = (
    "proposed",
    "planned",
    "ready",
    "in_progress",
    "review",
    "blocked",
    "completed",
    "cancelled",
)
STATUS_SET = frozenset(STATUSES)
TERMINAL_STATUSES = frozenset({"completed", "cancelled"})

# Explicit, deterministic transition table. "blocked" is handled specially in
# `allowed_targets` below: it may additionally return to whichever status the
# item was in immediately before entering "blocked" (recorded in the item's
# `blocked_from` field), per the v0.3.5 contract's "blocked -> (prior state)"
# rule.
TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"planned", "cancelled"}),
    "planned": frozenset({"proposed", "ready", "blocked", "cancelled"}),
    "ready": frozenset({"planned", "in_progress", "blocked", "cancelled"}),
    "in_progress": frozenset({"ready", "review", "blocked", "cancelled"}),
    "review": frozenset({"in_progress", "completed", "blocked", "cancelled"}),
    "blocked": frozenset({"planned", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

# Entering these statuses requires every dependency to already be "completed".
DEPENDENCY_GATED_STATUSES = frozenset({"ready", "completed"})

ID_PATTERN = re.compile(r"^ORCH-([0-9]{3,})$")

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
BACKLOG_RELATIVE_PATH = Path("backlog") / "backlog.yaml"
SCHEMA_RELATIVE_PATH = Path("schemas") / "backlog-item.schema.json"


def backlog_path(repo_root: Path) -> Path:
    return repo_root / BACKLOG_RELATIVE_PATH


def schema_path(repo_root: Path) -> Path:
    return repo_root / SCHEMA_RELATIVE_PATH


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

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def load_schema(repo_root: Path = REPO_ROOT_DEFAULT) -> dict:
    path = schema_path(repo_root)
    with path.open("r", encoding="utf-8") as f:
        import json

        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def validate_item_schema(item: Any, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    issues = []
    for e in sorted(validator.iter_errors(item), key=str):
        path = "/".join(str(p) for p in e.absolute_path)
        issues.append(f"{path or '<root>'}: {e.message}")
    if isinstance(item, dict):
        for field in ("created_at", "updated_at"):
            value = item.get(field)
            if value is not None and not _is_rfc3339_datetime(value):
                issues.append(f"{field}: {value!r} is not a valid date-time")
        sync = item.get("github_sync")
        if isinstance(sync, dict):
            value = sync.get("last_synced_at")
            if value is not None and not _is_rfc3339_datetime(value):
                issues.append(f"github_sync/last_synced_at: {value!r} is not a valid date-time")
        pr_sync = item.get("github_pr_sync")
        if isinstance(pr_sync, dict):
            value = pr_sync.get("last_synced_at")
            if value is not None and not _is_rfc3339_datetime(value):
                issues.append(f"github_pr_sync/last_synced_at: {value!r} is not a valid date-time")
    return issues


# ---------------------------------------------------------------------------
# I/O: atomic load/save
# ---------------------------------------------------------------------------

def load_backlog(repo_root: Path = REPO_ROOT_DEFAULT) -> list[dict]:
    """Load backlog/backlog.yaml. A missing file is valid and returns []."""
    path = backlog_path(repo_root)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return []
    if not isinstance(data, list):
        raise BacklogError(f"{path}: top-level YAML value must be a list of backlog items")
    return data


def save_backlog(items: Iterable[dict], repo_root: Path = REPO_ROOT_DEFAULT) -> None:
    """Atomically persist `items`, always re-sorted by id for a stable,
    diff-friendly file (unrelated items never reorder between saves)."""
    path = backlog_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(items, key=lambda it: it["id"])
    fd, tmp_name = tempfile.mkstemp(
        prefix=".backlog.", suffix=".yaml.tmp", dir=str(path.parent)
    )
    try:
        with open(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                ordered, f, sort_keys=True, default_flow_style=False, allow_unicode=True
            )
        Path(tmp_name).replace(path)
    finally:
        if Path(tmp_name).exists():
            Path(tmp_name).unlink()


# ---------------------------------------------------------------------------
# Dependency graph validation
# ---------------------------------------------------------------------------

def detect_cycle(items_by_id: dict[str, dict]) -> list[str] | None:
    """Returns a cycle (list of ids, first == last) if one exists, else None.
    Deterministic: visits ids in sorted order, and within each node visits
    dependencies in the order they are declared."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {i: WHITE for i in items_by_id}
    path_stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        path_stack.append(node)
        for dep in items_by_id[node].get("dependencies", []):
            if dep not in items_by_id:
                continue  # reported separately as a dangling reference
            if color.get(dep, WHITE) == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
            elif color.get(dep) == GRAY:
                idx = path_stack.index(dep)
                return path_stack[idx:] + [dep]
        path_stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(items_by_id):
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def validate_dependency_references(items_by_id: dict[str, dict]) -> list[str]:
    issues = []
    for item_id, item in sorted(items_by_id.items()):
        for dep in item.get("dependencies", []):
            if dep == item_id:
                issues.append(f"{item_id}: cannot depend on itself")
            elif dep not in items_by_id:
                issues.append(f"{item_id}: dependency {dep!r} does not reference an existing backlog item")
    return issues


def validate_backlog_collection(items: list[dict], schema: dict) -> list[str]:
    """Full validation: per-item schema validity, unique ids, dependency
    references exist, and no circular dependency."""
    issues: list[str] = []
    seen_ids: dict[str, int] = {}
    for idx, item in enumerate(items):
        item_issues = validate_item_schema(item, schema)
        label = item.get("id", f"<item[{idx}] missing id>") if isinstance(item, dict) else f"<item[{idx}]>"
        for msg in item_issues:
            issues.append(f"{label}: {msg}")
        if isinstance(item, dict) and "id" in item:
            if item["id"] in seen_ids:
                issues.append(f"duplicate backlog id: {item['id']!r}")
            else:
                seen_ids[item["id"]] = idx

    if issues:
        # Structural problems make graph analysis meaningless; stop here.
        return issues

    items_by_id = {item["id"]: item for item in items}
    issues.extend(validate_dependency_references(items_by_id))
    if not issues:
        cycle = detect_cycle(items_by_id)
        if cycle is not None:
            issues.append(f"circular dependency detected: {' -> '.join(cycle)}")

    issue_to_item: dict[int, str] = {}
    pr_to_item: dict[int, str] = {}
    for item in items:
        if item.get("github_issue") is not None:
            issue = item["github_issue"]
            if issue in issue_to_item:
                issues.append(f"GitHub issue #{issue} is mapped to multiple backlog items: {issue_to_item[issue]}, {item['id']}")
            else:
                issue_to_item[issue] = item["id"]
        if item.get("github_pr") is not None:
            if item.get("github_issue") is None:
                issues.append(f"{item['id']}: github_pr requires github_issue")
            pr = item["github_pr"]
            if pr in pr_to_item:
                issues.append(f"GitHub PR #{pr} is mapped to multiple backlog items: {pr_to_item[pr]}, {item['id']}")
            else:
                pr_to_item[pr] = item["id"]
        sync = item.get("github_sync")
        if sync:
            if item.get("github_issue") is None:
                issues.append(f"{item['id']}: github_sync requires github_issue")
            elif sync.get("issue") != item.get("github_issue"):
                issues.append(f"{item['id']}: github_sync issue does not match github_issue")
            if not sync.get("repo"):
                issues.append(f"{item['id']}: github_sync requires repo")
        pr_sync = item.get("github_pr_sync")
        if pr_sync:
            if item.get("github_pr") is None:
                issues.append(f"{item['id']}: github_pr_sync requires github_pr")
            elif pr_sync.get("pr") != item.get("github_pr"):
                issues.append(f"{item['id']}: github_pr_sync PR does not match github_pr")
            if item.get("github_issue") is None:
                issues.append(f"{item['id']}: github_pr_sync requires github_issue")
            elif pr_sync.get("issue") != item.get("github_issue"):
                issues.append(f"{item['id']}: github_pr_sync issue does not match github_issue")
            if not pr_sync.get("repo"):
                issues.append(f"{item['id']}: github_pr_sync requires repo")
        if item.get("status") == "blocked":
            if not item.get("blocked_reason"):
                issues.append(f"{item['id']}: status 'blocked' requires a non-empty blocked_reason")
            if item.get("blocked_from") not in {"proposed", "planned", "ready", "in_progress", "review"}:
                issues.append(f"{item['id']}: status 'blocked' requires a valid blocked_from")
        else:
            if item.get("blocked_reason") is not None or item.get("blocked_from") is not None:
                issues.append(f"{item['id']}: blocked_from/blocked_reason are only permitted while status is 'blocked'")
        if item.get("status") in DEPENDENCY_GATED_STATUSES:
            unmet = [
                dep for dep in item.get("dependencies", [])
                if dep in items_by_id and items_by_id[dep].get("status") != "completed"
            ]
            if unmet:
                issues.append(
                    f"{item['id']}: status {item['status']!r} requires all dependencies completed; "
                    f"unmet: {unmet}"
                )

    return issues


def generate_next_id(items: Iterable[dict]) -> str:
    max_n = 0
    for item in items:
        m = ID_PATTERN.match(item.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"ORCH-{max_n + 1:03d}"


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def backlog_list(
    repo_root: Path = REPO_ROOT_DEFAULT,
    *,
    status: str | None = None,
    version: str | None = None,
) -> list[dict]:
    items = load_backlog(repo_root)
    if status is not None:
        items = [i for i in items if i.get("status") == status]
    if version is not None:
        items = [i for i in items if i.get("version") == version]
    return sorted(items, key=lambda i: i["id"])


def backlog_get(id: str, repo_root: Path = REPO_ROOT_DEFAULT) -> dict:
    for item in load_backlog(repo_root):
        if item.get("id") == id:
            return item
    raise NotFoundError(f"no backlog item with id {id!r}")


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def _assert_collection_valid_after(items: list[dict], schema: dict) -> None:
    issues = validate_backlog_collection(items, schema)
    if issues:
        raise SchemaValidationError(issues)


def backlog_add(
    *,
    title: str,
    description: str,
    version: str,
    priority: str = "medium",
    id: str | None = None,
    status: str = "proposed",
    dependencies: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    labels: list[str] | None = None,
    github_issue: int | None = None,
    github_pr: int | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    repo_root: Path = REPO_ROOT_DEFAULT,
) -> dict:
    """Validated, atomic append of a new backlog item. Returns the stored item."""
    schema = load_schema(repo_root)
    items = load_backlog(repo_root)
    existing_ids = {i["id"] for i in items if "id" in i}

    resolved_id = id if id is not None else generate_next_id(items)
    if resolved_id in existing_ids:
        raise DuplicateIdError(f"backlog id {resolved_id!r} already exists")

    if status not in STATUS_SET:
        raise TransitionError(f"unknown status {status!r}")
    if status == "blocked":
        raise TransitionError("backlog_add cannot create an item directly in 'blocked' status")

    timestamp = now_iso()
    item = {
        "id": resolved_id,
        "title": title,
        "description": description,
        "status": status,
        "version": version,
        "priority": priority,
        "dependencies": sorted(dependencies or []),
        "created_at": created_at or timestamp,
        "updated_at": updated_at or timestamp,
        "acceptance_criteria": acceptance_criteria or [],
    }
    if labels:
        item["labels"] = labels
    if github_issue is not None:
        item["github_issue"] = github_issue
    if github_pr is not None:
        item["github_pr"] = github_pr

    new_items = items + [item]
    _assert_collection_valid_after(new_items, schema)
    save_backlog(new_items, repo_root)
    return item


def _compute_blocked_fields(item: dict, new_status: str) -> dict:
    """Returns the `blocked_from`/`blocked_reason` field updates implied by a
    transition into or out of 'blocked'."""
    updates: dict = {}
    if new_status == "blocked":
        updates["blocked_from"] = item["status"]
    elif item.get("blocked_from") is not None:
        updates["blocked_from"] = None
        updates["blocked_reason"] = None
    return updates


def allowed_targets(item: dict) -> frozenset[str]:
    current = item["status"]
    targets = set(TRANSITIONS.get(current, frozenset()))
    if current == "blocked":
        prior = item.get("blocked_from")
        if prior and prior != "blocked":
            targets.add(prior)
    return frozenset(targets)


def validate_transition(
    item: dict, new_status: str, items_by_id: dict[str, dict]
) -> None:
    current = item["status"]
    if new_status not in STATUS_SET:
        raise TransitionError(f"unknown status {new_status!r}")
    if new_status == current:
        raise TransitionError(f"{item['id']}: no-op transition {current!r} -> {new_status!r} is not permitted")
    if current in TERMINAL_STATUSES:
        raise TransitionError(f"{item['id']}: status {current!r} is terminal; no outgoing transitions are permitted")
    if new_status not in allowed_targets(item):
        raise TransitionError(
            f"{item['id']}: transition {current!r} -> {new_status!r} is not permitted "
            f"(allowed: {sorted(allowed_targets(item))})"
        )
    if new_status in DEPENDENCY_GATED_STATUSES:
        unmet = [
            dep for dep in item.get("dependencies", [])
            if items_by_id.get(dep, {}).get("status") != "completed"
        ]
        if unmet:
            raise DependencyNotCompletedError(item["id"], new_status, unmet)


def backlog_update(
    id: str,
    *,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    version: str | None = None,
    dependencies: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    labels: list[str] | None = None,
    github_issue: int | None = "__unset__",  # sentinel: distinguish "not passed" from "set to None"
    github_pr: int | None = "__unset__",  # sentinel
    blocked_reason: str | None = None,
    github_sync: dict | None = "__unset__",  # sentinel
    github_pr_sync: dict | None = "__unset__",  # sentinel
    repo_root: Path = REPO_ROOT_DEFAULT,
) -> dict:
    """Validated, atomic update of an existing backlog item. Status changes
    are validated against the deterministic transition table and the
    dependency-completion gate; other field updates never bypass schema
    validation of the resulting collection."""
    schema = load_schema(repo_root)
    items = load_backlog(repo_root)
    items_by_id = {i["id"]: i for i in items}
    if id not in items_by_id:
        raise NotFoundError(f"no backlog item with id {id!r}")

    item = copy.deepcopy(items_by_id[id])

    if status is not None:
        validate_transition(item, status, items_by_id)
        if status == "blocked" and not (blocked_reason or item.get("blocked_reason")):
            raise TransitionError(f"{id}: transitioning to 'blocked' requires a non-empty blocked_reason")
        item.update(_compute_blocked_fields(item, status))
        item["status"] = status

    if title is not None:
        item["title"] = title
    if description is not None:
        item["description"] = description
    if priority is not None:
        item["priority"] = priority
    if version is not None:
        item["version"] = version
    if dependencies is not None:
        item["dependencies"] = sorted(dependencies)
    if acceptance_criteria is not None:
        item["acceptance_criteria"] = acceptance_criteria
    if labels is not None:
        item["labels"] = labels
    if github_issue != "__unset__":
        old_issue = item.get("github_issue")
        if item.get("github_sync") and github_issue != old_issue:
            raise TransitionError(f"{id}: cannot change github_issue while github_sync baseline exists; clear github_sync first")
        if github_issue is None:
            item.pop("github_issue", None)
        else:
            item["github_issue"] = github_issue
    if github_pr != "__unset__":
        old_pr = item.get("github_pr")
        if item.get("github_pr_sync") and github_pr != old_pr:
            raise TransitionError(f"{id}: cannot change github_pr while github_pr_sync baseline exists; clear github_pr_sync first")
        if github_pr is None:
            item.pop("github_pr", None)
        else:
            item["github_pr"] = github_pr
    if blocked_reason is not None:
        if item.get("status") != "blocked":
            raise TransitionError(f"{id}: blocked_reason may only be set while status is 'blocked'")
        item["blocked_reason"] = blocked_reason
    if github_sync != "__unset__":
        if github_sync is None:
            item.pop("github_sync", None)
        else:
            item["github_sync"] = github_sync
    if github_pr_sync != "__unset__":
        if github_pr_sync is None:
            item.pop("github_pr_sync", None)
        else:
            item["github_pr_sync"] = github_pr_sync

    item["updated_at"] = now_iso()

    new_items = [item if i["id"] == id else i for i in items]
    _assert_collection_valid_after(new_items, schema)
    save_backlog(new_items, repo_root)
    return item


# ---------------------------------------------------------------------------
# Queries: next actionable item(s), reporting
# ---------------------------------------------------------------------------

_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def backlog_next(repo_root: Path = REPO_ROOT_DEFAULT) -> list[dict]:
    """Deterministically ranked list of 'ready' items (dependencies already
    satisfied by the 'ready' state's own gate), highest priority first, ties
    broken by id."""
    ready = backlog_list(repo_root, status="ready")
    return sorted(ready, key=lambda i: (_PRIORITY_RANK.get(i.get("priority"), 99), i["id"]))


@dataclass(frozen=True)
class BacklogReport:
    generated_at: str
    in_progress: list[str]
    ready: list[str]
    blocked: list[dict]
    review: list[str]
    completed_by_version: dict[str, list[str]]
    github_issues: dict[str, int | None]
    github_prs: dict[str, int | None]

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "in_progress": self.in_progress,
            "ready": self.ready,
            "blocked": self.blocked,
            "review": self.review,
            "completed_by_version": self.completed_by_version,
            "github_issues": self.github_issues,
            "github_prs": self.github_prs,
        }


def backlog_report_core(repo_root: Path = REPO_ROOT_DEFAULT) -> BacklogReport:
    """The backlog-only portion of the deterministic report (no provenance
    join). tools/backlog.py's `report` command adds worker/model attribution
    from provenance/runs/ on top of this."""
    items = load_backlog(repo_root)
    by_status: dict[str, list[dict]] = {s: [] for s in STATUSES}
    for item in items:
        by_status.setdefault(item["status"], []).append(item)

    completed_by_version: dict[str, list[str]] = {}
    for item in by_status.get("completed", []):
        completed_by_version.setdefault(item["version"], []).append(item["id"])
    for version in completed_by_version:
        completed_by_version[version].sort()

    blocked = [
        {"id": i["id"], "reason": i.get("blocked_reason"), "blocked_from": i.get("blocked_from")}
        for i in sorted(by_status.get("blocked", []), key=lambda i: i["id"])
    ]

    return BacklogReport(
        generated_at=now_iso(),
        in_progress=sorted(i["id"] for i in by_status.get("in_progress", [])),
        ready=sorted(i["id"] for i in by_status.get("ready", [])),
        blocked=blocked,
        review=sorted(i["id"] for i in by_status.get("review", [])),
        completed_by_version=completed_by_version,
        github_issues={i["id"]: i.get("github_issue") for i in sorted(items, key=lambda i: i["id"])},
        github_prs={i["id"]: i.get("github_pr") for i in sorted(items, key=lambda i: i["id"])},
    )


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
