#!/usr/bin/env python3
"""Read-only, loopback-only viewer for parent orchestration records."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import github_integration
import workspace_plan
import workspace_run
import garden_controls  # noqa: E402
import workspace_setup  # noqa: E402
import garden_issues  # noqa: E402
import run_lease  # noqa: E402
from usage import TOKEN_FIELDS, cost_coverage, run_tokens, sum_tokens  # noqa: E402,F401
import workspace_scheduler

RUN_ID = re.compile(r"^run-[A-Za-z0-9-]+$")
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
STAMP = re.compile(r"^[0-9T:.Z+-]{1,40}$")
SESSION_ID = re.compile(r"^[0-9TZ:-]{1,40}-[0-9]{1,10}$")
SESSION_STATUSES = {"OK", "ATTENTION", "AWAITING_AUTHORIZATION"}
PERMISSION_NAMES = {"create_worktrees", "commit"}
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/setup": ("setup.html", "text/html; charset=utf-8"),
          "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/issues.js": ("issues.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


def publication_links(record: dict, directory: Path) -> list[dict]:
    """Resolve linked publication child records into verified GitHub PR URLs."""
    parent = record.get("parent_orchestration") or {}
    repository = parent.get("target_repository")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        return []
    links: dict[int, dict] = {}
    for phase in parent.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for child_id in phase.get("child_run_ids") or []:
            if not isinstance(child_id, str) or not RUN_ID.fullmatch(child_id):
                continue
            try:
                child = json.loads((directory / f"{child_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            integration = child.get("github_integration") if isinstance(child, dict) else None
            if not isinstance(integration, dict) or not isinstance(integration.get("target_repository"), str):
                continue
            if integration["target_repository"].casefold() != repository.casefold():
                continue
            for item in integration.get("audit") or []:
                if not isinstance(item, dict):
                    continue
                number = item.get("pr")
                if (item.get("operation") in {"create_draft_pr", "create_pr"}
                        and item.get("status") == "applied" and type(number) is int and number > 0):
                    links[number] = {"kind": "pr", "number": number,
                                     "url": f"https://github.com/{repository}/pull/{number}"}
    return list(links.values())


def _merge_links(*groups: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for link in group:
            merged.setdefault(link["url"], link)
    return list(merged.values())


def workspace_pr_links(entry: dict, directory: Path) -> list[dict]:
    """The draft PR recorded by `pico workspace publish`, verified against its publication run."""
    pr = ((entry.get("observed") or {}).get("pr")) if isinstance(entry.get("observed"), dict) else None
    if not isinstance(pr, dict):
        return []
    number, repository, run_id = pr.get("number"), pr.get("repository"), pr.get("publication_run_id")
    if (type(number) is not int or number < 1 or not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
        return []
    try:
        record = json.loads((directory / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    integration = record.get("github_integration") if isinstance(record, dict) else None
    if not isinstance(integration, dict) or str(integration.get("target_repository", "")).casefold() != repository.casefold():
        return []
    applied = any(isinstance(item, dict) and item.get("operation") in {"create_draft_pr", "create_pr"}
                  and item.get("status") == "applied" and item.get("pr") == number
                  for item in integration.get("audit") or [])
    return [{"kind": "pr", "number": number, "url": f"https://github.com/{repository}/pull/{number}"}] if applied else []


def public_run(record: dict, directory: Path | None = None) -> dict:
    """Expose only display metadata, never full trace contents or prompts."""
    parent = record.get("parent_orchestration") or {}
    context = parent.get("context") or {}
    metrics = record.get("orchestration_metrics") or {}
    interactions = []
    for item in parent.get("interactions") or []:
        if not isinstance(item, dict) or item.get("type") not in {"directive", "feedback", "decision"}:
            continue
        interactions.append({key: item.get(key) for key in (
            "sequence", "at", "type", "summary", "task_id", "step_id", "subagent_id", "reply_to")})
    phases = []
    for phase in parent.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        current = (phase.get("escalations") or [{}])[-1]
        phases.append({**phase, "initial_route": phase.get("route"),
                       "route": current.get("to_route") or phase.get("route"),
                       "routing": current.get("routing") or phase.get("routing")})
    return {
        "id": record.get("run_id"), "repository": parent.get("target_repository"),
        "started_at": record.get("started_at"), "ended_at": record.get("ended_at"),
        "status": parent.get("final_status") or record.get("outcome"),
        "blocker_code": parent.get("blocker_code"),
        "phases": phases, "reconciled": parent.get("reconciled") or [], "waves": parent.get("waves") or [],
        "activity": parent.get("activity") or [],
        "interactions": interactions,
        "links": publication_links(record, directory) if directory else [],
        # Who holds the run lease (owner, pid, host, heartbeat, live/stale); null when nobody does.
        "lease": (run_lease.summary(record["run_id"], directory=directory.parent / "leases")
                  if directory and isinstance(record.get("run_id"), str) else None),
        "context": {key: context.get(key) for key in (
            "spawned_agents", "peak_concurrent_workers", "worker_active_ms",
            "peak_context_tokens", "compactions", "lead_usage", "worker_usage")},
        "metrics": {key: metrics.get(key) for key in (
            "wall_clock_ms", "total_tokens", "lead_input_tokens", "lead_output_tokens",
            "worker_input_tokens", "worker_output_tokens", "parallel_speedup",
            "estimated_cost_usd", "lead_model")} | _cost_flags(context),
    }


def _budget(plan: dict, record: dict, runs_dir: Path) -> dict | None:
    """Same figure a direct launch would gate on: running children's spend included, failing closed."""
    running, problems = workspace_scheduler.workspace_run.running_tasks(plan, workspace=record)
    return workspace_scheduler.budget_state(plan, record, runs_dir, running=running, extra_problems=problems)


def _cost_flags(context: dict) -> dict:
    unpriced = cost_coverage(context)[1]
    return {"cost_complete": not unpriced, "unpriced": unpriced}


def load_runs(directory: Path, repository: str | None) -> list[dict]:
    records = []
    if not directory.is_dir():
        return records
    for path in directory.glob("run-*.json"):
        if not RUN_ID.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            parent = record.get("parent_orchestration")
            if not isinstance(parent, dict):
                continue
            if repository and (parent.get("target_repository") or "").casefold() != repository.casefold():
                continue
            records.append(public_run(record, directory))
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # A damaged older record must not take down the live viewer.
    records.sort(key=lambda run: run.get("started_at") or "", reverse=True)
    return records[:40]


def load_runs_by_id(directory: Path, run_ids: list[str]) -> list[dict]:
    """The run view of a workspace: exactly its linked child runs."""
    records = []
    for run_id in dict.fromkeys(run_ids):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            continue
        try:
            record = json.loads((directory / f"{run_id}.json").read_text(encoding="utf-8"))
            if isinstance(record.get("parent_orchestration"), dict):
                records.append(public_run(record, directory))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    records.sort(key=lambda run: run.get("started_at") or "", reverse=True)
    return records


# --- Workspace mode ---------------------------------------------------------
# The workspace view reads only the workspace record
# (<state>/workspaces/<id>.json), the optional manifest plan and the child run
# records. Execution records (<state>/executions/) carry the child objective
# and are never opened here. Every served value is whitelisted and
# shape-checked: ids, codes, states, timestamps and counters, never task
# titles, goals, objectives, prompts, error details, local paths or logs.

class WorkspaceViewError(Exception):
    def __init__(self, code: str, detail: str, http_status: int):
        self.code, self.detail, self.http_status = code, detail, http_status
        super().__init__(f"{code}: {detail}")


def _match(value, pattern: re.Pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _time_limit(view):
    """Sanitized limit of a task's current launch: text for Garden, never executed or rendered as markup."""
    if not isinstance(view, dict):
        return None
    elapsed = view.get("elapsed_seconds")
    return {"max_duration_seconds": _count(view.get("max_duration_seconds")),
            "deadline_at": _match(view.get("deadline_at"), STAMP),
            "remaining_seconds": _count(view.get("remaining_seconds")),
            "stop_reason": _match(view.get("stop_reason"), CODE),
            "elapsed_seconds": elapsed if type(elapsed) in {int, float} else None}


def _lead_call_limit(view):
    """Sanitized lead calls used against the limit, or None when the run has no limit."""
    if not isinstance(view, dict) or _count(view.get("max_lead_calls")) is None:
        return None
    return {"max_lead_calls": view["max_lead_calls"], "calls": _count(view.get("calls")),
            "source": _match(view.get("source"), SLUG), "reached": view.get("reached") is True,
            "stopped": view.get("stopped") is True, "warn_at": _count(view.get("warn_at")),
            "warned": view.get("warned") is True}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _ids(values) -> list[str]:
    return [v for v in values if _match(v, SLUG)] if isinstance(values, list) else []


def _child_run(directory: Path, run_id, identity) -> tuple[dict | None, dict | None]:
    """Display summary and full record of one child run, or (None, None) when it cannot be attributed."""
    if not _match(run_id, RUN_ID):
        return None, None
    try:
        record = json.loads((directory / f"{run_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    parent = record.get("parent_orchestration") if isinstance(record, dict) else None
    if not isinstance(parent, dict):
        return None, None
    target = parent.get("target_repository")
    if isinstance(identity, str) and (not isinstance(target, str) or target.casefold() != identity.casefold()):
        return None, None  # A run bound to another repository is never attributed to this task.
    phases = [phase for phase in parent.get("phases") or [] if isinstance(phase, dict)]
    summary = {"id": run_id, "status": _match(parent.get("final_status") or record.get("outcome"), CODE),
               "blocker_code": _match(parent.get("blocker_code"), CODE),
               "started_at": _match(record.get("started_at"), STAMP), "ended_at": _match(record.get("ended_at"), STAMP),
               "phases_total": len(phases),
               "phases_completed": sum(1 for phase in phases if phase.get("status") == "completed")}
    return summary, record


def _record_plan(record: dict) -> dict:
    """A plan-shaped view of the workspace record alone: recorded tasks, no waves or declared readiness."""
    entries = record.get("tasks") if isinstance(record.get("tasks"), dict) else {}
    tasks = []
    for task_id in sorted(t for t in entries if _match(t, SLUG)):
        entry = entries[task_id] if isinstance(entries[task_id], dict) else {}
        inputs = entry.get("inputs") if isinstance(entry.get("inputs"), dict) else {}
        upstream = [item.get("task") for item in inputs.get("upstream") or [] if isinstance(item, dict)]
        tasks.append({"id": task_id, "repository": _match(entry.get("repository"), SLUG) or "unknown",
                      "depends_on": sorted(t for t in _ids(upstream) if t in entries), "blocks": [],
                      "wave": None, "readiness": "planned", "blocked_by": []})
    by_id = {task["id"]: task for task in tasks}
    for task in tasks:
        for dep in task["depends_on"]:
            by_id[dep]["blocks"].append(task["id"])
    return {"workspace_id": record.get("workspace_id"), "status": None, "max_parallel": None, "waves": [],
            "repositories": [{"id": repo_id} for repo_id in sorted({t["repository"] for t in tasks})],
            "tasks": tasks, "blockers": []}


def _sessions(record: dict) -> list[dict]:
    sessions = []
    for session in record.get("scheduler_sessions") or []:
        if not isinstance(session, dict):
            continue
        refused = session.get("refused") if isinstance(session.get("refused"), dict) else {}
        sessions.append({"id": _match(session.get("id"), SESSION_ID),
                         "started_at": _match(session.get("started_at"), STAMP),
                         "finished_at": _match(session.get("finished_at"), STAMP),
                         "cap": _count(session.get("cap")),
                         "dispatched": _ids(session.get("dispatched")), "resumed": _ids(session.get("resumed")),
                         "qualified": _ids(session.get("qualified")),
                         "held_by_budget": _ids(session.get("held_by_budget")),
                         "awaiting_authorization": _ids(session.get("awaiting_authorization")),
                         "new_attempts": _ids(session.get("new_attempts")),
                         "allow_mutation": session.get("allow_mutation") is True,
                         "observed_running": _ids(session.get("observed_running")),
                         "refused": {t: c for t, c in refused.items() if _match(t, SLUG) and _match(c, CODE)},
                         "status": session.get("status") if session.get("status") in SESSION_STATUSES else None})
    sessions.sort(key=lambda s: s["started_at"] or "", reverse=True)
    return sessions


def _manifest_plan(manifest: Path, state: Path | None = None) -> dict:
    """The manifest's plan with the workspace record's observed state overlaid, as the scheduler sees it."""
    observed = None
    try:
        raw = workspace_plan.load_manifest(manifest)
        workspace_id = raw.get("id") if isinstance(raw, dict) else None
        if state is not None and _match(workspace_id, SLUG):
            record = workspace_run.load_workspace(state / "workspaces" / f"{workspace_id}.json")
            observed = workspace_run.observed_from_record(record or {})
    except (workspace_plan.WorkspaceError, workspace_run.WorkspaceRunError):
        observed = None  # the plan call below reports an unreadable manifest
    try:
        return workspace_plan.plan(manifest, access_check=None, observed=observed)
    except workspace_plan.WorkspaceError as exc:
        # The planner's message can quote manifest content; only the code is served.
        raise WorkspaceViewError("WORKSPACE_MANIFEST_INVALID",
                                 "the workspace manifest cannot be read or violates its schema", 422) from exc


def workspace_view(state: Path, *, workspace_id: str | None = None, manifest: Path | None = None,
                   plan: dict | None = None) -> dict:
    """Display metadata of one workspace: tasks grouped by repository, scheduler states, child runs, tokens.

    With a manifest the task graph (waves, depends_on, declared readiness,
    blocked_by) comes from workspace_plan.plan(access_check=None); with an id
    alone it comes from the workspace record. States are computed by
    workspace_scheduler.report, so they are the scheduler's own states.
    """
    if plan is None and manifest is not None:
        plan = _manifest_plan(manifest, state)
    if plan is not None:
        workspace_id = plan["workspace_id"]
    if not _match(workspace_id, SLUG):
        raise WorkspaceViewError("WORKSPACE_ID_INVALID", "a workspace id is 1-64 letters, digits, - or _", 400)
    try:
        record = workspace_run.load_workspace(state / "workspaces" / f"{workspace_id}.json")
    except workspace_run.WorkspaceRunError as exc:
        raise WorkspaceViewError(exc.code, "the workspace record cannot be read", 422) from exc
    if record is None and plan is None:
        raise WorkspaceViewError("WORKSPACE_UNKNOWN", f"no workspace record for {workspace_id} in this state directory", 404)
    record = record or {}
    source = "manifest" if plan is not None else "record"
    plan = plan or _record_plan(record)
    raw_entries = record.get("tasks") if isinstance(record.get("tasks"), dict) else {}
    entries = {task_id: entry for task_id, entry in raw_entries.items() if isinstance(entry, dict)}
    sessions = _sessions(record)
    # A refusal is not persisted as a launch; the latest session's refusals
    # still describe the tasks that have not been launched since.
    latest = sessions[0]["refused"] if sessions else {}
    refused = {task_id: {"code": code} for task_id, code in latest.items()
               if workspace_scheduler.classify(entries.get(task_id)) == "not_launched"}
    # Tasks the latest session's preflight kept from launching still await authorization until launched.
    awaiting = {task_id for task_id in (sessions[0]["awaiting_authorization"] if sessions else [])
                if workspace_scheduler.classify(entries.get(task_id)) == "not_launched"}
    verdicts = None
    if source == "manifest":
        # The same read-only check the scheduler runs, under the flag the latest session used.
        verdicts = workspace_run.authorization_verdicts(plan, allow_mutation=bool(sessions and sessions[0]["allow_mutation"]))
    report = workspace_scheduler.report(plan, entries, refused, None, verdicts, awaiting)
    rows = {row["id"]: row for row in report["tasks"]}
    # A scheduler still running records its dispatches before the children return.
    open_session = next((x for x in sessions if x["finished_at"] is None), None)
    if open_session is not None:
        in_flight = set(open_session["dispatched"]) | set(open_session["resumed"]) | set(open_session["qualified"])
        settled = {"completed", "partial", "failed", "cancelled", "refused", "needs_reconciliation",
                   "validated", "ready_for_downstream", "merged", "awaiting_authorization"}
        for task_id in in_flight & set(rows):
            if rows[task_id]["state"] not in settled:
                rows[task_id] = {**rows[task_id], "state": "running"}
        if in_flight & set(rows):
            report["counts"] = {}
            for row in rows.values():
                report["counts"][row["state"]] = report["counts"].get(row["state"], 0) + 1
    runs_dir = state / "runs"
    tasks_by_repo: dict[str, list[dict]] = {}
    tokens_by_repo: dict[str, list[dict]] = {}
    for task in sorted(plan["tasks"], key=lambda t: (t["wave"] if t["wave"] is not None else 1 << 30, t["id"])):
        row, entry = rows[task["id"]], entries.get(task["id"]) or {}
        run, run_record = _child_run(runs_dir, row["run_id"], entry.get("target_repository"))
        tokens = run_tokens(run_record["parent_orchestration"]) if run_record else None
        if tokens is not None:
            tokens_by_repo.setdefault(task["repository"], []).append(tokens)
        previous = []
        for item in row.get("previous_attempts") or []:
            _, earlier = _child_run(runs_dir, item.get("run_id"), entry.get("target_repository"))
            if earlier:  # superseded attempts spent tokens too
                tokens_by_repo.setdefault(task["repository"], []).append(run_tokens(earlier["parent_orchestration"]))
            previous.append({"attempt": _count(item.get("attempt")), "run_id": _match(item.get("run_id"), RUN_ID),
                             "run_status": _match(item.get("run_status"), CODE),
                             "run_blocker_code": _match(item.get("run_blocker_code"), CODE)})
        tasks_by_repo.setdefault(task["repository"], []).append({
            "id": task["id"], "repository": task["repository"], "wave": task["wave"],
            "kind": task.get("kind") if task.get("kind") in {"implementation", "qualification"} else None,
            "readiness": task["readiness"] if source == "manifest" else None,
            "state": row["state"], "depends_on": _ids(task["depends_on"]), "blocks": _ids(task["blocks"]),
            "blocked_by": _ids(row["blocked_by"]),
            "execution_status": _match(entry.get("execution_status"), SLUG), "launches": _count(row["launches"]),
            "run_id": _match(row["run_id"], RUN_ID), "run_status": _match(row["run_status"], CODE),
            "run_blocker_code": _match(row["run_blocker_code"], CODE),
            "refusal_code": _match((row["refusal"] or {}).get("code"), CODE),
            "time_limit": _time_limit(row.get("time_limit")),
            "lead_call_limit": _lead_call_limit(row.get("lead_call_limit")),
            "attempt": _count(row.get("attempt")), "previous_attempts": previous,
            "authorization": _authorization(row.get("authorization")),
            "run": run, "links": _merge_links(publication_links(run_record, runs_dir) if run_record else [],
                                              workspace_pr_links(entry, runs_dir)), "tokens": tokens})
    repositories = []
    declared_ids = [repo["id"] for repo in plan["repositories"]]
    for repo_id in declared_ids + sorted(set(tasks_by_repo) - set(declared_ids)):
        declared = next((repo for repo in plan["repositories"] if repo["id"] == repo_id), {})
        tasks = tasks_by_repo.get(repo_id, [])
        recorded = [entries[t["id"]] for t in tasks if t["id"] in entries]
        identity = next((e.get("target_repository") for e in recorded if e.get("target_repository")), None)
        pinned = declared.get("pinned_commit") or next((e.get("pinned_commit") for e in recorded), None)
        repositories.append({"id": repo_id, "repository": _match(identity or declared.get("repository"), REPOSITORY),
                             "pinned_commit": _match(pinned, SHA), "blocker_code": _match(declared.get("blocker_code"), CODE),
                             "tasks": tasks, "tokens": sum_tokens(tokens_by_repo.get(repo_id, []))})
    return {"workspace_id": workspace_id, "source": source, "record_found": bool(record),
            "updated_at": _match(record.get("updated_at"), STAMP),
            "plan_status": plan.get("status") if plan.get("status") in {"PLANNED", "BLOCKED"} else None,
            "max_parallel": _count(plan.get("max_parallel")),
            "waves": [_ids(wave) for wave in plan.get("waves") or []],
            "status": report["status"], "counts": report["counts"], "repositories": repositories,
            "tokens": sum_tokens([part for parts in tokens_by_repo.values() for part in parts]),
            "budget": _budget(plan, record, runs_dir) if source == "manifest" else None,
            "approval_requests": [_approval_request(r) for r in report.get("approval_requests") or []],
            "repeated_authorization_stops": _ids(report.get("repeated_authorization_stops")),
            "sessions": sessions}


def _authorization(verdict) -> dict | None:
    if not isinstance(verdict, dict) or verdict.get("status") not in {"not_required", "authorized", "awaiting", "unresolved"}:
        return None
    return {"status": verdict["status"], "code": _match(verdict.get("code"), CODE),
            "missing": sorted(name for name in verdict.get("missing") or [] if name in PERMISSION_NAMES)}


def _approval_request(request: dict) -> dict:
    """One repository's approval request, reduced to fixed vocabularies and validated identifiers."""
    return {"repository": _match(request.get("repository"), SLUG), "github": _match(request.get("github"), REPOSITORY),
            "code": _match(request.get("code"), CODE), "tasks": _ids(request.get("tasks")),
            "missing": sorted(name for name in request.get("missing") or [] if name in PERMISSION_NAMES),
            "policy_source": request.get("policy_source") if request.get("policy_source") in {"default", "committed"} else None,
            "uncommitted_policy": request.get("uncommitted_policy") is True,
            "base_branch": _match(request.get("base_branch"), SLUG),
            "pinned_commit": _match(request.get("pinned_commit"), SHA),
            "never_granted": ["push_branch", "create_pr", "mark_pr_ready", "merge", "deploy"]}


class WorkspaceSource:
    """The watched workspace, resolved per request; a manifest's plan is cached until the file changes."""

    def __init__(self, state: Path, target: str | Path):
        self.state = state
        path = Path(target).expanduser()
        self.manifest = path.resolve() if path.is_file() else None
        self._id = None if self.manifest else str(target)
        self._key = None
        self._plan = None

    def plan(self) -> dict | None:
        if self.manifest is None:
            return None
        try:
            stat = self.manifest.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        record = self.state / "workspaces"
        try:
            # Observed state changes with the record, so it is part of the cache key.
            raw = workspace_plan.load_manifest(self.manifest)
            record_stat = (record / f"{raw.get('id')}.json").stat() if isinstance(raw, dict) else None
            key = (key, record_stat.st_mtime_ns if record_stat else None)
        except (OSError, workspace_plan.WorkspaceError):
            key = (key, None)
        if key[0] is None or key != self._key:
            self._key, self._plan = None, None
            self._plan = _manifest_plan(self.manifest, self.state)
            self._key = key
        return self._plan

    @property
    def workspace_id(self) -> str | None:
        if self._id is not None:
            return self._id
        try:
            return self.plan()["workspace_id"]
        except WorkspaceViewError:
            return None

    def view(self) -> dict:
        return workspace_view(self.state, workspace_id=self._id, plan=self.plan())

    def run_ids(self) -> list[str]:
        try:
            view = self.view()
        except WorkspaceViewError:
            return []
        return [task["run_id"] for repo in view["repositories"] for task in repo["tasks"] if task["run_id"]]


# With --controls on, every data endpoint needs the session token; the static
# page, script and stylesheet do not (the token arrives in the URL fragment).
TOKEN_ENDPOINTS = frozenset({"/api/controls", "/api/branches", "/api/runs", "/api/workspace", "/api/issues"})


def make_handler(directory: Path, repository: str | None, workspace: WorkspaceSource | None = None,
                 controls=None, port: int | None = None, issues: garden_issues.IssuesSource | None = None,
                 setup: workspace_setup.SetupControls | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            if not self.path.startswith("/api/"):
                super().log_message(format, *args)

        def _json(self, status: int, body: dict) -> bytes:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            return json.dumps(body).encode()

        def _host_ok(self) -> bool:
            # DNS-rebinding guard: only this server's own loopback names are served.
            bound = port if port is not None else self.server.server_address[1]
            if garden_controls.host_allowed(self.headers.get("Host"), bound):
                return True
            payload = self._json(421, {"ok": False, "error_code": "HOST_REJECTED", "error": "unexpected Host header"})
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return False

        def _send(self, payload: bytes) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if not self._host_ok():
                return
            path = urlsplit(self.path).path
            if path == "/api/setup" and setup is not None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    self._send(self._json(400, {"ok": False, "error_code": "CONTROL_BODY_INVALID"}))
                    return
                body = self.rfile.read(min(length, garden_controls.MAX_BODY + 1)) if length else b""
                try:
                    data = setup.authorize(self.headers, body)
                    payload = self._json(200, setup.act_setup(data))
                except garden_controls.ControlError as exc:
                    payload = self._json(exc.status, {"ok": False, "error_code": exc.code, "error": exc.detail})
                self._send(payload)
                return
            if path != "/api/action" or controls is None:
                self._send(self._json(404, {"ok": False, "error_code": "CONTROLS_OFF",
                                            "error": "start pico watch with --workspace <manifest> --controls"}))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._send(self._json(400, {"ok": False, "error_code": "CONTROL_BODY_INVALID",
                                            "error": "Content-Length must be a non-negative integer"}))
                return
            body = self.rfile.read(min(length, garden_controls.MAX_BODY + 1)) if length else b""
            try:
                data = controls.authorize(self.headers, body)
                result = controls.act(data)
                payload = self._json(200, result)
            except garden_controls.ControlError as exc:
                payload = self._json(exc.status, {"ok": False, "error_code": exc.code, "error": exc.detail})
            except workspace_run.WorkspaceRunError as exc:
                payload = self._json(422, {"ok": False, "error_code": exc.code, "error": "the action was refused"})
            self._send(payload)

        def _token_ok(self) -> bool:
            # With controls on, every data endpoint needs the session token too (TOKEN_ENDPOINTS).
            auth = controls or setup
            if auth is None:
                return True
            try:
                auth.check_token(self.headers)
                return True
            except garden_controls.ControlError as exc:
                self._send(self._json(exc.status, {"ok": False, "error_code": exc.code, "error": exc.detail}))
                return False

        def do_GET(self):
            if not self._host_ok():
                return
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/api/setup" and setup is not None:
                try:
                    setup.check_token(self.headers)
                    payload = self._json(200, setup.discovery())
                except garden_controls.ControlError as exc:
                    payload = self._json(exc.status, {"ok": False, "error_code": exc.code, "error": exc.detail})
                self._send(payload)
                return
            if path in TOKEN_ENDPOINTS and not self._token_ok():
                return
            if path == "/api/controls":
                if workspace is None or workspace.manifest is None:
                    payload = self._json(404, {"ok": False, "error_code": "WORKSPACE_MANIFEST_REQUIRED",
                                               "error": "panels need pico watch --workspace <manifest>"})
                else:
                    try:
                        panel_source = controls or garden_controls.Controls(workspace.manifest, 0, token="unused")
                        payload = self._json(200, {"ok": True, "enabled": controls is not None,
                                                   "actions": list(garden_controls.ACTIONS) if controls else [],
                                                   **panel_source.panels()})
                    except workspace_run.WorkspaceRunError as exc:
                        payload = self._json(422, {"ok": False, "error_code": exc.code, "error": "panels unavailable"})
                self._send(payload)
                return
            if path == "/api/branches":
                task = (dict(item.split("=", 1) for item in parts.query.split("&") if "=" in item)).get("task")
                if controls is None:
                    payload = self._json(404, {"ok": False, "error_code": "CONTROLS_OFF", "error": "controls are off"})
                else:
                    try:
                        payload = self._json(200, {"ok": True, "task": task, "branches": controls.branches(task)})
                    except garden_controls.ControlError as exc:
                        payload = self._json(exc.status, {"ok": False, "error_code": exc.code, "error": exc.detail})
                    except workspace_run.WorkspaceRunError as exc:
                        payload = self._json(422, {"ok": False, "error_code": exc.code, "error": "branches unavailable"})
                self._send(payload)
                return
            if path == "/api/runs":
                if workspace is None:
                    body = {"repository": repository, "workspace_mode": False, "workspace": None,
                            "runs": load_runs(directory, repository)}
                else:
                    body = {"repository": None, "workspace_mode": True, "workspace": workspace.workspace_id,
                            "runs": load_runs_by_id(directory, workspace.run_ids())}
                payload = self._json(200, body)
            elif path == "/api/issues":
                if issues is None:
                    payload = self._json(404, {"ok": False, "error_code": "ISSUES_TARGET_NONE",
                                               "error": "issues need pico watch on one repository or a workspace manifest"})
                else:
                    try:
                        payload = self._json(200, issues.view())
                    except WorkspaceViewError as exc:
                        payload = self._json(exc.http_status, {"ok": False, "error_code": exc.code, "error": exc.detail})
            elif path == "/api/workspace":
                if workspace is None:
                    payload = self._json(404, {"ok": False, "error_code": "WORKSPACE_MODE_OFF",
                                               "error": "start pico watch with --workspace to see a workspace"})
                else:
                    try:
                        payload = self._json(200, {"ok": True, **workspace.view()})
                    except WorkspaceViewError as exc:
                        payload = self._json(exc.http_status, {"ok": False, "error_code": exc.code, "error": exc.detail})
            elif path in ASSETS:
                filename, content_type = ASSETS[path]
                payload = (ROOT / "viewer" / filename).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
            else:
                self.send_error(404)
                return
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch pico runs in a local browser")
    parser.add_argument("target", nargs="?", default=".", help="Target Git repository")
    parser.add_argument("--repo", help="Select owner/repository when the target has multiple remotes")
    parser.add_argument("--all", action="store_true", help="Show runs from every repository in this state directory")
    parser.add_argument("--workspace", metavar="ID_OR_MANIFEST",
                        help="Show one workspace across its repositories (workspace id or manifest path)")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--controls", action="store_true",
                        help="with --workspace <manifest>: enable validate, request PR, CI and promote actions")
    parser.add_argument("--setup-root", type=Path, help="Open the workspace setup page for this directory")
    args = parser.parse_args()
    if args.all and args.repo:
        parser.error("--all and --repo cannot be combined")
    if args.workspace and (args.all or args.repo):
        parser.error("--workspace cannot be combined with --all or --repo")
    if args.setup_root and (args.workspace or args.all or args.repo or args.controls):
        parser.error("--setup-root cannot be combined with workspace, repository or control options")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    default_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pi-coding-orchestrator"
    state = args.state_dir or Path(os.environ.get("PI_ORCHESTRATOR_STATE_DIR") or
                                    os.environ.get("PI_CODING_ORCHESTRATOR_STATE_DIR") or default_state)
    directory = state / "runs"
    repository = None
    workspace = None
    if args.workspace:
        workspace = WorkspaceSource(state, args.workspace)
        if workspace.manifest is None and not SLUG.fullmatch(args.workspace):
            parser.error(f"--workspace {args.workspace}: no such manifest file, and not a workspace id")
        try:
            workspace.view()
        except WorkspaceViewError as exc:
            if exc.code != "WORKSPACE_UNKNOWN":
                parser.error(f"{exc.code}: {exc.detail}")
            print(f"Note: {exc.detail}; the page will show it once the workspace is scheduled.", flush=True)
    elif not args.all and not args.setup_root:
        try:
            repository = github_integration.resolve_target(Path(args.target).resolve(), args.repo)
        except github_integration.IntegrationError as exc:
            parser.error(f"{exc}; use --repo owner/repository or --all")
    issues = None
    if workspace is not None and workspace.manifest is not None:
        issues = garden_issues.IssuesSource(garden_issues.workspace_targets(workspace.plan), mode="workspace")
    elif repository is not None:
        issues = garden_issues.IssuesSource(
            garden_issues.repository_targets(Path(args.target).resolve(), args.repo, repository), mode="repository")
    controls = None
    if args.controls:
        if workspace is None or workspace.manifest is None:
            parser.error("--controls needs --workspace <manifest path>")
        # The contract tools resolve state through the environment; align it with this viewer.
        os.environ["PI_ORCHESTRATOR_STATE_DIR"] = str(state)
    if args.setup_root and not args.setup_root.is_dir():
        parser.error("--setup-root must be an existing directory")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(directory, repository, workspace, issues=issues))
    setup = workspace_setup.SetupControls(args.setup_root, server.server_port) if args.setup_root else None
    if setup:
        server.RequestHandlerClass = make_handler(directory, repository, port=server.server_port, setup=setup)
    if args.controls:
        controls = garden_controls.Controls(workspace.manifest, server.server_port)
        server.RequestHandlerClass = make_handler(directory, repository, workspace, controls, server.server_port,
                                                  issues=issues)
    label = f"workspace {workspace.workspace_id}" if workspace else repository or "all repositories"
    if setup:
        print(f"Pico setup: http://127.0.0.1:{server.server_port}/setup#token={setup.token}  (root {setup.root})", flush=True)
        print("Keep this URL private: it authorizes saving a new manifest under this root.", flush=True)
    elif controls:
        print(f"Pico watch: http://127.0.0.1:{server.server_port}/#token={controls.token}  ({label}, controls on)", flush=True)
        print("Keep this URL private: it authorizes actions for this session only.", flush=True)
    else:
        print(f"Pico watch: http://127.0.0.1:{server.server_port}  ({label})", flush=True)
    print(f"Reading: {state / 'workspaces' if workspace else directory}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
