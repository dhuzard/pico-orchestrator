#!/usr/bin/env python3
"""Durable parent-run lifecycle for Pi orchestration.

Start happens before routing. Every terminal path finalizes the same run;
GitHub adapter records remain child references. This module persists only
closed metadata, never prompts, issue bodies, environment values, or stderr.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import provenance
import github_integration
import routing_engine as engine

STATUSES = {"COMPLETED", "BLOCKED", "FAILED", "PARTIAL"}
PHASE_STATUSES = {"not_started", "in_progress", "completed", "blocked", "failed", "partial"}
WORKER_STATUSES = {"not_started", "in_progress", "completed", "blocked", "failed"}
ROUTES = {"CODEX", "CLAUDE", "CLAUDE_CODEX", "CODEX_CLAUDE_CODEX"}
ESCALATION_TARGETS = {
    "CODEX": {"CLAUDE_CODEX", "CODEX_CLAUDE_CODEX"},
    "CLAUDE": {"CLAUDE_CODEX", "CODEX_CLAUDE_CODEX"},
    "CLAUDE_CODEX": {"CODEX_CLAUDE_CODEX"},
    "CODEX_CLAUDE_CODEX": set(),
}
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
VERSION_RE = re.compile(r"^(?:>=)?[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
REFERENCE_RE = re.compile(r"^checkpoints/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9]{4}\.json$")
REASONING_EFFORTS = {"low", "medium", "high"}
# Why a phase was re-routed to a stronger route/capacity. Escalation is never
# implicit: the parent records one of these when it re-classifies a phase.
ESCALATION_REASONS = {"FAILED_ATTEMPTS", "REVIEW_FINDINGS", "AMBIGUITY", "ARCHITECTURE",
                      "SECURITY_SENSITIVE", "SEMANTIC_RISK", "CONTEXT_BUDGET"}
COMPACTION_REASONS = {"budget", "threshold", "overflow", "manual"}
USAGE_BACKENDS = {"claude-code", "pi-subagent"}
USAGE_COUNTERS = ("duration_ms", "input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "total_tokens")
# A wave groups routed tasks with disjoint write scopes that run concurrently;
# it is integrated into the parent checkout only after all its workers return.
WAVE_STATUSES = ("declared", "integrating", "integrated")
LOCK_WAIT_SECONDS = 5
POLICY_PATH = ROOT / "routing" / "default-policy.yaml"
TASK_TYPES = frozenset(json.loads((ROOT / "schemas" / "task.schema.json").read_text())["properties"]["task_type"]["enum"])


class ParentRunError(Exception):
    def __init__(self, detail: str, *, error_code: str = "PARENT_EVENT_INVALID"):
        self.error_code = error_code
        super().__init__(detail)


class ParentRunBlocked(ParentRunError):
    def __init__(self, code: str):
        self.code = _safe(code, CODE_RE, "blocker code")
        super().__init__(self.code)


@contextmanager
def _run_lock(repo_root: Path, run_id: str):
    """Serialize read-modify-write operations for one parent across processes.

    The lock file is shared with provenance.add_step/update_step/
    update_orchestration_metrics, so parent events and step records of
    concurrent wave workers never overwrite one another.
    """
    try:
        with provenance.run_lock(repo_root, run_id, wait_seconds=LOCK_WAIT_SECONDS):
            yield
    except provenance.RunLockTimeout as exc:
        raise ParentRunError("parent record is busy", error_code="PARENT_LOCK_TIMEOUT") from exc


def required_review_steps(route: str, *, policy_path: Path = POLICY_PATH) -> set[str]:
    """Independent-review step ids of a route's fixed topology, across all intents."""
    try:
        execution = engine.load_yaml(policy_path).get("execution") or {}
    except (OSError, AttributeError, ValueError, engine.yaml.YAMLError) as exc:
        raise ParentRunError("routing policy is unavailable", error_code="PARENT_POLICY_UNAVAILABLE") from exc
    intents = execution.get(route, {}).get("primary_intents") or {}
    return {step["id"] for plan in intents.values() for step in plan.get("steps") or []
            if step.get("independent_review") is True}


def _missing_reviews(phases: list[dict]) -> list[str]:
    """Routed phases whose mandatory independent review never completed."""
    missing = []
    for phase in phases:
        # Blocked or failed attempts may remain in a repaired run; they claim no result.
        if phase["route"] is None or phase["status"] in {"blocked", "failed"}:
            continue
        epoch = len(phase.get("escalations") or [])
        completed = {worker["step_id"] for worker in phase["workers"]
                     if worker["status"] == "completed" and worker.get("route_epoch", 0) == epoch}
        effective_route = (phase.get("escalations") or [{}])[-1].get("to_route") or phase["route"]
        missing += [f"{phase['id']}:{step}" for step in sorted(required_review_steps(effective_route) - completed)]
    return missing


def _safe(value: str | None, pattern: re.Pattern, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value) or provenance._contains_secret_like_content(value):
        raise ParentRunError(f"invalid {field}")
    return value


def _parent(record: dict) -> dict:
    parent = record.get("parent_orchestration")
    if not isinstance(parent, dict):
        raise ParentRunError("run is not a parent orchestration run")
    return parent


def start(*, worktree: Path, explicit_repo: str | None = None, repo_root: Path = ROOT) -> dict:
    parent = {"target_repository": None, "policy_version": None, "phases": [],
              "mutations": {"github": 0, "code": 0}, "final_status": "IN_PROGRESS", "blocker_code": None,
              "context": empty_context()}
    record = provenance.start_run(repo_root, backlog_id=None, github_issue=None,
                                  user_objective="Parent orchestration run",
                                  route_task_result=None, starting_commit=None,
                                  parent_orchestration=parent)
    try:
        parent["target_repository"] = github_integration.resolve_target(worktree, explicit_repo)
        provenance.update_parent_orchestration(repo_root, record["run_id"], parent)
    except github_integration.IntegrationError as exc:
        # The record already exists even when target resolution fails.
        finish(record["run_id"], "BLOCKED", blocker_code=exc.code, repo_root=repo_root)
    except Exception:
        # Keep the trace terminal even when Git discovery itself fails.
        finish(record["run_id"], "FAILED", blocker_code="TARGET_RESOLUTION_FAILED", repo_root=repo_root)
    current = provenance.get_run(repo_root, record["run_id"])["parent_orchestration"]
    return {"run_id": record["run_id"], "target_repository": current["target_repository"],
            "status": current["final_status"], "blocker_code": current["blocker_code"]}


def status(run_id: str, *, repo_root: Path = ROOT, worktree: Path | None = None,
           explicit_repo: str | None = None) -> dict:
    """Verify that the parent record is readable from orchestrator-owned storage."""
    record = provenance.get_run(repo_root, run_id)
    parent = _parent(record)
    if worktree is not None and parent["final_status"] == "IN_PROGRESS":
        try:
            target = github_integration.resolve_target(worktree, explicit_repo)
        except github_integration.IntegrationError as exc:
            raise ParentRunError("target repository cannot be verified", error_code=exc.code) from exc
        if target != parent["target_repository"]:
            raise ParentRunError("target repository changed", error_code="SESSION_REPOSITORY_MISMATCH")
    return {"run_id": run_id, "target_repository": parent["target_repository"],
            "status": parent["final_status"], "blocker_code": parent["blocker_code"],
            "reroute_phases": sorted(phase["id"] for phase in parent["phases"]
                                     if phase.get("route") is not None and phase.get("status") == "in_progress")}


def empty_context() -> dict:
    return {"starting_commit": None, "baseline_tokens": None, "peak_context_tokens": None,
            "peak_working_tokens": None, "compactions": 0, "structured_compactions": 0,
            "checkpoints": [], "worker_usage": {}, "lead_usage": {}, "lead_calls": [], "spawned_agents": 0}


def _count(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ParentRunError(f"invalid {field}")
    return value


def _context_event(parent: dict, event: dict) -> None:
    context = parent.setdefault("context", empty_context())
    if "starting_commit" in event:
        value = event["starting_commit"]
        if value is not None:
            _safe(value, COMMIT_RE, "starting commit")
        if context["starting_commit"] is not None and value != context["starting_commit"]:
            raise ParentRunError("starting commit conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
        context["starting_commit"] = value
    if "baseline_tokens" in event:
        value = _count(event["baseline_tokens"], "baseline tokens")
        if context["baseline_tokens"] is not None and value != context["baseline_tokens"]:
            raise ParentRunError("baseline conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
        context["baseline_tokens"] = value
    for key, peak in (("context_tokens", "peak_context_tokens"), ("working_tokens", "peak_working_tokens")):
        if key in event:
            value = _count(event[key], key)
            context[peak] = max(value, context[peak] or 0)
    if "compaction" in event:
        compaction = event["compaction"]
        if (not isinstance(compaction, dict) or compaction.get("reason") not in COMPACTION_REASONS
                or type(compaction.get("structured")) is not bool
                or set(compaction) - {"sequence", "reference", "reason", "tokens_before", "structured"}):
            raise ParentRunError("invalid compaction event")
        tokens_before = compaction.get("tokens_before")
        if tokens_before is not None:
            _count(tokens_before, "tokens before")
        context["compactions"] += 1
        if compaction["structured"]:
            sequence = _count(compaction.get("sequence"), "checkpoint sequence")
            reference = _safe(compaction.get("reference"), REFERENCE_RE, "checkpoint reference")
            if reference is None or sequence != len(context["checkpoints"]) + 1:
                raise ParentRunError("checkpoint sequence is not contiguous")
            context["structured_compactions"] += 1
            context["checkpoints"].append({"sequence": sequence, "reference": reference,
                                           "reason": compaction["reason"],
                                           "tokens_before": tokens_before})


def _usage_event(parent: dict, event: dict) -> None:
    backend = event.get("backend")
    if backend not in USAGE_BACKENDS:
        raise ParentRunError("invalid usage backend")
    context = parent.setdefault("context", empty_context())
    usage = context["worker_usage"].setdefault(
        backend, {"invocations": 0, "total_tokens_complete": True,
                  **{counter: None for counter in USAGE_COUNTERS if counter != "cache_write_tokens"}})
    usage["invocations"] += 1
    if event.get("total_tokens") is None:
        usage["total_tokens_complete"] = False
    for counter in USAGE_COUNTERS:
        # Unobserved counters stay null; observed ones accumulate.
        if event.get(counter) is not None:
            usage[counter] = (usage.get(counter) or 0) + _count(event[counter], counter)
    # Backend-reported list-price estimate (Claude Code `total_cost_usd`); absent until observed.
    cost = event.get("estimated_cost_usd")
    if cost is not None:
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise ParentRunError("invalid estimated cost")
        usage["estimated_cost_usd"] = round(usage.get("estimated_cost_usd", 0) + cost, 6)


def _wave_event(parent: dict, event: dict) -> None:
    """Record wave membership: which routed tasks (phases) ran concurrently, and its integration state."""
    wave_id = _safe(event.get("wave_id"), ID_RE, "wave id")
    task_ids = event.get("task_ids")
    if (wave_id is None or not isinstance(task_ids, list) or not task_ids
            or len(set(map(str, task_ids))) != len(task_ids)):
        raise ParentRunError("invalid wave event")
    for task_id in task_ids:
        _safe(task_id, ID_RE, "wave task id")
    status = event.get("status")
    if status not in WAVE_STATUSES:
        raise ParentRunError("invalid wave status", error_code="PARENT_WAVE_STATUS_INVALID")
    waves = parent.setdefault("waves", [])
    wave = next((item for item in waves if item["id"] == wave_id), None)
    if wave is None:
        if status != "declared":
            raise ParentRunError("wave was never declared", error_code="PARENT_WAVE_TRANSITION_INVALID")
        wave = {"id": wave_id, "task_ids": list(task_ids), "status": status}
        waves.append(wave)
    elif wave["task_ids"] != task_ids:
        raise ParentRunError("wave tasks conflict with prior event", error_code="PARENT_EVENT_CONFLICT")
    elif WAVE_STATUSES.index(status) < WAVE_STATUSES.index(wave["status"]):
        raise ParentRunError("wave cannot move backwards", error_code="PARENT_WAVE_TRANSITION_INVALID")
    wave["status"] = status
    for task_id in task_ids:
        _phase(parent, task_id)["wave_id"] = wave_id


def _topology(peak: int) -> str:
    return ("direct" if peak == 0 else "one_worker" if peak == 1 else "two_workers" if peak == 2
            else "three_to_four_workers" if peak <= 4 else "larger_team")


def delegation_metrics(record: dict, parent: dict, *, now: datetime | None = None) -> dict:
    """Observed delegation metrics of a parent run; empty when concurrency was never observed.

    `delegation_topology` comes from the peak number of concurrent worker
    calls. The serial estimate replaces the observed time with at least one
    worker running (`worker_active_ms`) by the sum of every worker call's
    duration, so `parallel_speedup = estimated_serial_time_ms / wall_clock_ms`
    is 1 for sequential workers and grows with overlap.
    """
    context = parent.get("context") or {}
    usage = context.get("worker_usage") or {}
    peak = context.get("peak_concurrent_workers")
    try:
        started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return {}
    wall = max(0, round(((now or datetime.now(timezone.utc)) - started).total_seconds() * 1000))
    metrics: dict[str, Any] = {"delegation_topology": None if peak is None and usage else _topology(peak or 0),
                               "wall_clock_ms": wall}
    workers = [worker for phase in parent.get("phases", []) for worker in phase.get("workers", [])
               if worker.get("status") != "not_started"]
    models = {worker["model"] for worker in workers if worker.get("model")}
    metrics["worker_model_mix"] = ("none" if not workers else "same_model" if len(models) == 1 and len(models) == len(workers)
                                   else "mixed_model" if len(models) > 1 else None)
    metrics["spawned_agents"] = context.get("spawned_agents", 0)
    lead = context.get("lead_usage") or {}
    metrics["lead_model"] = lead.get("model")
    for field in ("input_tokens", "output_tokens", "cached_tokens"):
        if lead.get(field) is not None:
            metrics[f"lead_{field}"] = lead[field]
        observed = [entry[field] for entry in usage.values() if entry.get(field) is not None]
        if observed:
            metrics[f"worker_{field}"] = sum(observed)
    # Providers differ on whether cached tokens are included in input. Only
    # aggregate their explicit totals; a missing participant keeps this unknown.
    metrics["total_tokens"] = None
    if (lead.get("total_tokens_complete") is True and lead.get("total_tokens") is not None
            and all(entry.get("total_tokens_complete") is True for entry in usage.values())):
        metrics["total_tokens"] = lead["total_tokens"] + sum(entry["total_tokens"] for entry in usage.values())
    costs = [part["estimated_cost_usd"] for part in (lead, *usage.values())
             if isinstance(part, dict) and part.get("estimated_cost_usd") is not None]
    if costs:
        metrics["estimated_cost_usd"] = round(sum(costs), 6)
    durations = [entry["duration_ms"] for entry in usage.values() if entry.get("duration_ms") is not None]
    if durations:
        execution = sum(durations)
        metrics["worker_execution_ms"] = execution
        if context.get("worker_active_ms") is not None:
            active = min(context["worker_active_ms"], wall)
            metrics["estimated_serial_time_ms"] = wall - active + execution
    return metrics


def _phase(parent: dict, phase_id: str) -> dict:
    _safe(phase_id, ID_RE, "phase id")
    for phase in parent["phases"]:
        if phase["id"] == phase_id:
            return phase
    phase = {"id": phase_id, "status": "not_started", "route": None,
             "blocker_code": None, "workers": [], "child_run_ids": []}
    parent["phases"].append(phase)
    return phase


def record_event(run_id: str, event: dict, *, repo_root: Path = ROOT) -> dict:
    with _run_lock(repo_root, run_id):
        return _record_event_unlocked(run_id, event, repo_root=repo_root)


def _record_event_unlocked(run_id: str, event: dict, *, repo_root: Path = ROOT) -> dict:
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        raise ParentRunError("event must have a kind")
    kind = event["kind"]
    allowed = {
        "phase": {"kind", "phase", "status", "route", "routing", "policy_version", "blocker_code", "escalation_reason"},
        "escalation": {"kind", "phase", "from_route", "to_route", "reason"},
        "worker": {"kind", "phase", "step_id", "model", "backend", "backend_model", "reasoning_effort", "status", "blocker_code", "installed_backend_version", "required_backend_version"},
        "child": {"kind", "phase", "run_id"},
        "mutation": {"kind", "github", "code"},
        "context": {"kind", "starting_commit", "baseline_tokens", "context_tokens", "working_tokens", "compaction"},
        "usage": {"kind", "backend", "estimated_cost_usd", *USAGE_COUNTERS},
        "wave": {"kind", "wave_id", "task_ids", "status"},
        "concurrency": {"kind", "peak_workers", "active_ms"},
        "spawn": {"kind"},
        "activity": {"kind", "status", "tool", "task_id", "step_id", "subagent_id"},
        "interaction": {"kind", "type", "summary", "task_id", "step_id", "subagent_id", "reply_to"},
        "lead_usage": {"kind", "model", "phase", "task_type", "turn_index", "duration_ms", "input_tokens", "output_tokens", "cached_tokens", "total_tokens", "estimated_cost_usd"},
    }
    required = {
        "phase": {"kind", "phase", "status"},
        "escalation": {"kind", "phase", "from_route", "to_route", "reason"},
        "worker": {"kind", "phase", "step_id", "status"},
        "child": {"kind", "phase", "run_id"},
        "mutation": {"kind"},
        "context": {"kind"},
        "usage": {"kind", "backend"},
        "wave": {"kind", "wave_id", "task_ids", "status"},
        "concurrency": {"kind", "peak_workers", "active_ms"},
        "spawn": {"kind"},
        "activity": {"kind", "status", "tool"},
        "interaction": {"kind", "type", "summary", "task_id", "step_id"},
        "lead_usage": {"kind", "model"},
    }
    if kind not in allowed or set(event) - allowed[kind]:
        raise ParentRunError("unsupported event or unexpected fields")
    if not required[kind] <= set(event) or (kind == "mutation" and not {"github", "code"} & set(event)):
        raise ParentRunError("event is missing required fields", error_code="PARENT_EVENT_FIELDS_INVALID")
    record = provenance.get_run(repo_root, run_id)
    if record["outcome"] != "in_progress":
        raise ParentRunError("parent run is terminal")
    parent = _parent(record)
    if kind == "escalation":
        phase = _phase(parent, event["phase"])
        current = (phase.get("escalations") or [{}])[-1].get("to_route") or phase["route"]
        if (phase["status"] in {"completed", "blocked", "failed", "partial"}
                or event["from_route"] != current
                or event["to_route"] not in ESCALATION_TARGETS.get(current, set())
                or event["reason"] not in ESCALATION_REASONS):
            raise ParentRunError("invalid phase route escalation", error_code="PARENT_ESCALATION_INVALID")
        if any(worker["status"] == "in_progress" for worker in phase["workers"]):
            raise ParentRunError("phase workers are still in progress", error_code="PARENT_ESCALATION_WORKERS_IN_FLIGHT")
        phase.setdefault("escalations", []).append({"from_route": current,
            "to_route": event["to_route"], "reason": event["reason"],
            "at": datetime.now(timezone.utc).isoformat(), "confirmed": False})
    elif kind == "phase":
        phase = _phase(parent, event["phase"])
        status = event.get("status")
        if status not in PHASE_STATUSES:
            raise ParentRunError("invalid phase status", error_code="PARENT_PHASE_STATUS_INVALID")
        if phase["status"] in {"completed", "blocked", "failed", "partial"} and status != phase["status"]:
            raise ParentRunError("phase is already terminal", error_code="PARENT_PHASE_TRANSITION_INVALID")
        phase["status"] = status
        if "route" in event:
            if event["route"] not in ROUTES:
                raise ParentRunError("invalid route")
            current = (phase.get("escalations") or [{}])[-1].get("to_route") or phase["route"]
            if current is not None and current != event["route"]:
                raise ParentRunError("phase route conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            if phase["route"] is None:
                phase["route"] = event["route"]
            elif phase.get("escalations"):
                phase["escalations"][-1]["confirmed"] = True
        if "routing" in event:
            if "route" not in event or not isinstance(event["routing"], dict):
                raise ParentRunError("routing metadata requires a selected route")
            if phase.get("escalations"):
                latest = phase["escalations"][-1]
                if "routing" in latest and latest["routing"] != event["routing"]:
                    raise ParentRunError("escalation routing conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
                latest["routing"] = event["routing"]
            elif "routing" in phase and phase["routing"] != event["routing"]:
                raise ParentRunError("phase routing conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            else:
                phase["routing"] = event["routing"]
        if "policy_version" in event:
            if type(event["policy_version"]) is not int or event["policy_version"] < 1:
                raise ParentRunError("invalid policy version")
            if parent["policy_version"] is not None and parent["policy_version"] != event["policy_version"]:
                raise ParentRunError("policy version conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            parent["policy_version"] = event["policy_version"]
        if "blocker_code" in event:
            value = _safe(event["blocker_code"], CODE_RE, "blocker code")
            if phase["blocker_code"] is not None and phase["blocker_code"] != value:
                raise ParentRunError("phase blocker conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            phase["blocker_code"] = value
        if "escalation_reason" in event:
            if event["escalation_reason"] not in ESCALATION_REASONS:
                raise ParentRunError("invalid escalation reason", error_code="PARENT_ESCALATION_REASON_INVALID")
            phase["escalation_reason"] = event["escalation_reason"]
    elif kind == "worker":
        phase = _phase(parent, event["phase"])
        step_id = _safe(event.get("step_id"), ID_RE, "step id")
        if step_id is None or event.get("status") not in WORKER_STATUSES:
            raise ParentRunError("invalid worker event")
        epoch = len(phase.get("escalations") or [])
        worker = next((item for item in phase["workers"]
                       if item["step_id"] == step_id and item.get("route_epoch", 0) == epoch), None)
        if worker is None:
            if epoch > 0 and event["status"] in {"completed", "blocked", "failed"}:
                raise ParentRunError("escalated worker has no recorded start", error_code="PARENT_WORKER_TRANSITION_INVALID")
            worker = {"step_id": step_id, "model": None, "backend": None, "backend_model": None, "status": "not_started",
                      "route_epoch": epoch,
                      "blocker_code": None, "installed_backend_version": None,
                      "required_backend_version": None}
            phase["workers"].append(worker)
        if worker["status"] in {"completed", "blocked", "failed"} and event["status"] != worker["status"]:
            raise ParentRunError("worker is already terminal", error_code="PARENT_WORKER_TRANSITION_INVALID")
        worker["status"] = event["status"]
        if "reasoning_effort" in event:
            if event["reasoning_effort"] not in REASONING_EFFORTS:
                raise ParentRunError("invalid reasoning effort")
            if worker.get("reasoning_effort") is not None and worker["reasoning_effort"] != event["reasoning_effort"]:
                raise ParentRunError("worker field conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            worker["reasoning_effort"] = event["reasoning_effort"]
        for key, pattern in (("model", MODEL_RE), ("backend", MODEL_RE), ("backend_model", MODEL_RE), ("blocker_code", CODE_RE),
                             ("installed_backend_version", VERSION_RE),
                             ("required_backend_version", VERSION_RE)):
            if key in event:
                value = _safe(event[key], pattern, key)
                if worker.get(key) is not None and value != worker[key]:
                    raise ParentRunError("worker field conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
                worker[key] = value
    elif kind == "child":
        phase = _phase(parent, event["phase"])
        child_id = _safe(event.get("run_id"), ID_RE, "child run id")
        if child_id is None or child_id == run_id or not provenance.run_path(repo_root, child_id).exists():
            raise ParentRunError("child run does not exist or links to itself")
        if child_id not in phase["child_run_ids"]:
            phase["child_run_ids"].append(child_id)
    elif kind == "context":
        _context_event(parent, event)
    elif kind == "usage":
        _usage_event(parent, event)
    elif kind == "spawn":
        parent.setdefault("context", empty_context())["spawned_agents"] += 1
    elif kind == "activity":
        if event["status"] not in {"started", "finished", "failed", "spawned"} or event["tool"] not in {"spawn_subagent", "message_subagent", "ClaudeWorker", "AskClaude"}:
            raise ParentRunError("invalid activity event")
        entry = {"at": datetime.now(timezone.utc).isoformat(), "status": event["status"], "tool": event["tool"]}
        for field in ("task_id", "step_id", "subagent_id"):
            entry[field] = _safe(event.get(field), ID_RE, field)
        parent.setdefault("activity", []).append(entry)
        parent["activity"] = parent["activity"][-500:]
    elif kind == "interaction":
        if event["type"] not in {"directive", "feedback", "decision"}:
            raise ParentRunError("invalid interaction type")
        summary = event["summary"]
        issues: list[str] = []
        provenance._validate_compact_text(summary, "interaction.summary", issues)
        if (not isinstance(summary, str) or not summary.strip() or len(summary) > 180
                or issues or summary != summary.strip()):
            raise ParentRunError("interaction summary must be concise, single-line, and non-secret")
        task_id = _safe(event["task_id"], ID_RE, "task id")
        step_id = _safe(event["step_id"], ID_RE, "step id")
        if task_id is None or step_id is None:
            raise ParentRunError("interaction requires task and step ids")
        subagent_id = _safe(event.get("subagent_id"), ID_RE, "subagent id")
        interactions = parent.setdefault("interactions", [])
        reply_to = event.get("reply_to")
        if reply_to is not None and (type(reply_to) is not int or reply_to < 1
                                     or not any(item["sequence"] == reply_to for item in interactions)):
            raise ParentRunError("interaction reply_to must name an earlier step")
        entry = {"sequence": (interactions[-1]["sequence"] + 1 if interactions else 1),
                 "at": datetime.now(timezone.utc).isoformat(), "type": event["type"],
                 "summary": summary, "task_id": task_id, "step_id": step_id,
                 "subagent_id": subagent_id, "reply_to": reply_to}
        interactions.append(entry)
        parent["interactions"] = interactions[-500:]
    elif kind == "lead_usage":
        context = parent.setdefault("context", empty_context())
        lead = context.setdefault("lead_usage", {})
        if "total_tokens_complete" not in lead:
            lead["total_tokens_complete"] = True
        if event.get("total_tokens") is None:
            lead["total_tokens_complete"] = False
        model = _safe(event["model"], MODEL_RE, "lead model")
        if model is None:
            raise ParentRunError("lead model is required")
        if lead.get("model") not in (None, model):
            # Model changes are observable; the run-level field only names a
            # single lead model when one model served the whole run.
            lead["model"] = None
            lead["mixed_models"] = True
        elif not lead.get("mixed_models"):
            lead["model"] = model
        for field in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            if event.get(field) is not None:
                lead[field] = (lead.get(field) or 0) + _count(event[field], field)
        cost = event.get("estimated_cost_usd")
        if cost is not None:
            # Pi's own per-message list-price estimate for the lead; absent when unreported.
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise ParentRunError("invalid estimated cost")
            lead["estimated_cost_usd"] = round(lead.get("estimated_cost_usd", 0) + cost, 6)
        phase = event.get("phase")
        if phase is not None:
            _safe(phase, ID_RE, "phase id")
        task_type = event.get("task_type")
        if task_type is not None:
            if task_type not in TASK_TYPES:
                raise ParentRunError("invalid task type")
        call = {"sequence": len(context.setdefault("lead_calls", [])) + 1,
                "phase": phase, "task_type": task_type, "model": model,
                "turn_index": None if event.get("turn_index") is None else _count(event["turn_index"], "turn index"),
                "duration_ms": None if event.get("duration_ms") is None else _count(event["duration_ms"], "duration ms")}
        for field in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
            call[field] = event.get(field)
        context["lead_calls"].append(call)
    elif kind == "wave":
        _wave_event(parent, event)
    elif kind == "concurrency":
        context = parent.setdefault("context", empty_context())
        peak = _count(event["peak_workers"], "peak workers")
        context["peak_concurrent_workers"] = max(peak, context.get("peak_concurrent_workers") or 0)
        context["worker_active_ms"] = (context.get("worker_active_ms") or 0) + _count(event["active_ms"], "active ms")
        provenance.update_parent_orchestration(repo_root, run_id, parent,
                                               orchestration_metrics=delegation_metrics(record, parent))
        return parent
    else:
        for key in ("github", "code"):
            if key in event:
                value = event[key]
                if type(value) is not int or value < 0:
                    raise ParentRunError("invalid mutation count")
                parent["mutations"][key] = value
    if kind in {"worker", "usage", "spawn", "lead_usage"}:
        provenance.update_parent_orchestration(repo_root, run_id, parent,
                                               orchestration_metrics=delegation_metrics(record, parent))
    else:
        provenance.update_parent_orchestration(repo_root, run_id, parent)
    return parent


def finish(run_id: str, status: str, *, blocker_code: str | None = None,
           repo_root: Path = ROOT) -> dict:
    with _run_lock(repo_root, run_id):
        return _finish_unlocked(run_id, status, blocker_code=blocker_code, repo_root=repo_root)


def _finish_unlocked(run_id: str, status: str, *, blocker_code: str | None = None,
                     repo_root: Path = ROOT) -> dict:
    if status not in STATUSES:
        raise ParentRunError("invalid final status")
    record = provenance.get_run(repo_root, run_id)
    parent = _parent(record)
    if record["outcome"] != "in_progress":
        return record
    if status == "COMPLETED":
        phases = parent["phases"]
        unconfirmed = [phase["id"] for phase in phases if phase.get("escalations")
                       and not phase["escalations"][-1].get("confirmed")]
        if unconfirmed:
            raise ParentRunError("escalated phases still require a confirmed route: " + ", ".join(unconfirmed[:12]),
                                 error_code="PARENT_ROUTE_RECHECK_REQUIRED")
        open_states = {"not_started", "in_progress"}
        unfinished = [f"phase {phase['id']} is {phase['status']}" for phase in phases if phase["status"] in open_states]
        unfinished += [f"worker {phase['id']}/{worker['step_id']} is {worker['status']}"
                       for phase in phases for worker in phase["workers"]
                       if worker["status"] in open_states
                       and worker.get("route_epoch", 0) == len(phase.get("escalations") or [])]
        if unfinished:
            # Named so the lead can record each one and finish again in a single step.
            raise ParentRunError("unfinished: " + "; ".join(unfinished[:12]) +
                                 ". Record each as completed, blocked or failed, then finish again",
                                 error_code="PARENT_COMPLETION_INCOMPLETE")
        missing = _missing_reviews(phases)
        if missing:
            raise ParentRunError("mandatory independent review not completed for phase " + ", ".join(missing[:12]) +
                                 ". Run and record the review, or finish as PARTIAL",
                                 error_code="PARENT_REVIEW_MISSING")
    parent["final_status"] = status
    parent["blocker_code"] = _safe(blocker_code, CODE_RE, "blocker code")
    return provenance.finish_run(repo_root, run_id, outcome=status.lower(),
                                 parent_orchestration=parent,
                                 orchestration_metrics=delegation_metrics(record, parent))


def settle(run_id: str, *, repo_root: Path = ROOT) -> dict:
    """Fail closed when the agent exits without recording an explicit outcome."""
    record = provenance.get_run(repo_root, run_id)
    if record["outcome"] != "in_progress":
        return record
    phases = _parent(record)["phases"]
    observed = next((phase["blocker_code"] for phase in reversed(phases)
                     if phase["status"] == "blocked" and phase["blocker_code"]), None)
    return finish(run_id, "BLOCKED", blocker_code=observed or "ORCHESTRATION_OUTCOME_UNRECORDED",
                  repo_root=repo_root)


def terminal_response(record: dict, *, repo_root: Path = ROOT) -> dict:
    """Add the actual trace location to the CLI result without changing the stored record."""
    return {**record, "trace_path": str(provenance.run_path(repo_root, record["run_id"]))}


class ParentRun:
    """In-process orchestration scope: all exceptions finalize the parent."""
    def __init__(self, *, worktree: Path, explicit_repo: str | None = None, repo_root: Path = ROOT):
        self.repo_root = repo_root
        self.run_id = start(worktree=worktree, explicit_repo=explicit_repo,
                            repo_root=repo_root)["run_id"]

    def __enter__(self) -> "ParentRun":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        record = provenance.get_run(self.repo_root, self.run_id)
        if record["outcome"] == "in_progress":
            if isinstance(exc, ParentRunBlocked):
                finish(self.run_id, "BLOCKED", blocker_code=exc.code, repo_root=self.repo_root)
            elif exc_type is not None:
                finish(self.run_id, "FAILED", blocker_code="ORCHESTRATION_EXECUTION_FAILED", repo_root=self.repo_root)
            else:
                settle(self.run_id, repo_root=self.repo_root)
        return False

    def event(self, event: dict) -> dict:
        return record_event(self.run_id, event, repo_root=self.repo_root)

    def finish(self, status: str, blocker_code: str | None = None) -> dict:
        return finish(self.run_id, status, blocker_code=blocker_code, repo_root=self.repo_root)

    def prepare_implementation(self, route: dict, preflight: Callable[[dict], dict],
                               create_worktree: Callable[[], Any]) -> Any:
        """The sole worktree transition in this API, gated by route preflight."""
        result = preflight(route)
        if not result.get("ok"):
            raise ParentRunBlocked(result.get("blocker_code", "MODEL_BACKEND_UNAVAILABLE"))
        return create_worktree()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("--worktree", type=Path, required=True)
    p_start.add_argument("--repo")
    p_event = sub.add_parser("event")
    p_event.add_argument("run_id")
    p_finish = sub.add_parser("finish")
    p_finish.add_argument("run_id")
    p_finish.add_argument("status", choices=sorted(STATUSES))
    p_finish.add_argument("--blocker-code")
    p_settle = sub.add_parser("settle")
    p_settle.add_argument("run_id")
    p_status = sub.add_parser("status")
    p_status.add_argument("run_id")
    p_status.add_argument("--worktree", type=Path)
    p_status.add_argument("--repo")
    args = parser.parse_args()
    event_kind = None
    try:
        if args.command == "start":
            result = start(worktree=args.worktree, explicit_repo=args.repo)
        elif args.command == "event":
            payload = json.load(sys.stdin)
            event_kind = payload.get("kind") if isinstance(payload, dict) else None
            result = record_event(args.run_id, payload)
        elif args.command == "finish":
            result = terminal_response(finish(args.run_id, args.status, blocker_code=args.blocker_code))
        elif args.command == "status":
            result = status(args.run_id, worktree=args.worktree, explicit_repo=args.repo)
        else:
            result = terminal_response(settle(args.run_id))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ParentRunError, provenance.ProvenanceError, ValueError, json.JSONDecodeError,
            OSError) as exc:
        detail = str(exc) if isinstance(exc, ParentRunError) else None
        run_id = getattr(args, "run_id", None)
        if not isinstance(run_id, str) or not provenance.RUN_ID_PATTERN.fullmatch(run_id):
            run_id = None
        lifecycle_state = None
        if run_id is not None:
            try:
                lifecycle_state = provenance.get_run(ROOT, run_id)["parent_orchestration"]["final_status"]
            except (OSError, KeyError, TypeError, provenance.ProvenanceError):
                pass
        try:
            commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                    capture_output=True, text=True, timeout=5)
            revision = commit.stdout.strip() if commit.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            revision = None
        error_code = (exc.error_code if isinstance(exc, ParentRunError)
                      else "PARENT_SCHEMA_INVALID" if isinstance(exc, provenance.RunSchemaValidationError)
                      else "PARENT_STORAGE_UNAVAILABLE")
        validation_path = None
        if isinstance(exc, provenance.RunSchemaValidationError) and exc.issues:
            candidate = exc.issues[0].split(":", 1)[0]
            if re.fullmatch(r"[A-Za-z0-9_./<>-]{1,160}", candidate):
                validation_path = candidate
        print(json.dumps({"ok": False, "component": "parent_orchestration",
                          "operation": args.command, "error_category": type(exc).__name__,
                          "error_code": error_code, "error": detail, "run_id": run_id,
                          "event_kind": event_kind if isinstance(event_kind, str) and
                            event_kind in {"phase", "worker", "child", "mutation", "context", "usage", "wave", "concurrency"} else None,
                          "lifecycle_state": lifecycle_state, "validation_path": validation_path,
                          "orchestrator_commit": revision, "orchestrator_root": str(ROOT),
                          "resource_path": str(ROOT / ("schemas/run-record.schema.json" if
                            error_code in {"PARENT_SCHEMA_INVALID", "PARENT_STORAGE_UNAVAILABLE"}
                            else "tools/parent_orchestration.py"))}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
