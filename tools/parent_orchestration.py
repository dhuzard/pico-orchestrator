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
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import provenance
import github_integration
import routing_engine as engine
import run_lease

STATUSES = {"COMPLETED", "BLOCKED", "FAILED", "PARTIAL"}
# "abandoned" is written only by `_reconcile_open` when a run ends without a COMPLETED outcome;
# it is never accepted from an event.
PHASE_STATUSES = {"not_started", "in_progress", "completed", "blocked", "failed", "partial"}
WORKER_STATUSES = {"not_started", "in_progress", "completed", "blocked", "failed"}
RECONCILED_STATUS = "abandoned"
# Bounds on `worker_overrides` (the schema carries the same global limit).
MAX_WORKER_OVERRIDES = 200
MAX_OVERRIDES_PER_WORKER = 8
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
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
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
WAVE_STATUSES = ("declared", "integrating", "integrated", "abandoned")
# Statuses an event may set; "abandoned" is written only by `_reconcile_open` when a run ends without COMPLETED.
WAVE_EVENT_STATUSES = WAVE_STATUSES[:3]
WAVE_OPEN_STATUSES = {"declared", "integrating"}
ACTIVITY_EXCERPT_CHARS = 300
ACTIVITY_SUMMARY_CHARS = 600
ACTIVITY_GOAL_CHARS = 200
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
LOCK_WAIT_SECONDS = 5
# Resume checkpoint (audit A10): the parent checkout's path, branch and HEAD at
# the last phase boundary or worker approval, compared with the live checkout
# before a resumed run may continue.
CHECKPOINT_EVENT_REASONS = {"worker_approval"}
OWNER_RE = run_lease.OWNER_RE
CHECKPOINT_FIELDS = ("worktree", "branch", "head")
MAX_RESUME_CHECKS = 50
POLICY_PATH = ROOT / "routing" / "default-policy.yaml"
TASK_TYPES = frozenset(json.loads((ROOT / "schemas" / "task.schema.json").read_text())["properties"]["task_type"]["enum"])


class ParentRunError(Exception):
    def __init__(self, detail: str, *, error_code: str = "PARENT_EVENT_INVALID", details: dict | None = None):
        self.error_code = error_code
        # Closed, non-secret metadata (lease holder, expected/actual checkpoint) for the caller.
        self.details = details
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


def _recorded_owner(repo_root: Path, run_id: str) -> str | None:
    record = provenance.get_run(repo_root, run_id)
    parent = record.get("parent_orchestration")
    return parent.get("owner_id") if isinstance(parent, dict) else None


@contextmanager
def _owned(run_id: str, owner_id: str | None, repo_root: Path):
    """Every write to a parent run: the lease lock (outer), the owner check, then the run lock.

    The caller must hold the run's lease, or the lease must be free and the
    caller be the run's recorded owner (run_lease.guard). Lock order is always
    lease, then run: takeover and resume take them in the same order.
    """
    try:
        with run_lease.guard(run_id, owner_id, recorded_owner=lambda: _recorded_owner(repo_root, run_id),
                             repo_root=repo_root) as held:
            with _run_lock(repo_root, run_id):
                yield held
    except run_lease.LeaseError as exc:
        raise _lease_failure(exc) from exc


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


def _retry_activity(parent: dict, phase: dict, step_id: str) -> tuple[bool, bool]:
    """(started, succeeded): after the step's last recorded failure, has it been started again on its current
    route, and did that start return without error? A start without a route never counts, and a start that has
    not returned (or returned an error) is not a success."""
    route = (phase.get("escalations") or [{}])[-1].get("to_route") or phase["route"]
    last_failed = -1
    start = -1
    succeeded = False
    for index, entry in enumerate(parent.get("activity") or []):
        if entry.get("task_id") != phase["id"] or entry.get("step_id") != step_id:
            continue
        if entry["status"] == "failed":
            last_failed, start, succeeded = index, -1, False
        elif entry["status"] == "started" and entry.get("route") == route and last_failed >= 0:
            start, succeeded = index, False
        elif entry["status"] == "finished" and start > last_failed >= 0:
            succeeded = True
    return start > last_failed >= 0, succeeded


SECRET_MASK = "[withheld]"
# Credential shapes masked in recorded excerpts, on top of provenance's content patterns. Conservative: the
# whole matching span is replaced, never the surrounding text.
_EXCERPT_SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|token|secret|api[_-]?key|apikey)\b\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@\S*"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"),
)


def _mask_secrets(text: str) -> str:
    for pattern in (*provenance._SECRET_CONTENT_PATTERNS, *_EXCERPT_SECRET_PATTERNS):
        text = pattern.sub(SECRET_MASK, text)
    return text


def _bounded_text(value: str, limit: int) -> str:
    """Single-line, control-free text of at most `limit` characters. Secret-like spans are masked in the whole
    (bounded to 8x) text first and the cut comes last, so a token that straddles the cut is masked, not split;
    a long unbroken run left at the cut is dropped as a possible partial token."""
    text = " ".join(CONTROL_RE.sub(" ", ANSI_RE.sub("", value[:limit * 8])).split())
    text = _mask_secrets(text)
    if provenance._contains_secret_like_content(text):
        return "[withheld: secret-like content]"
    if len(text) > limit:
        cut = text[:limit]
        if not text[limit].isspace():
            tail = re.search(r"\S{16,}$", cut)
            if tail:
                cut = cut[:tail.start()]
        text = cut
    return text.strip()


def _parent(record: dict) -> dict:
    parent = record.get("parent_orchestration")
    if not isinstance(parent, dict):
        raise ParentRunError("run is not a parent orchestration run")
    return parent


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def capture_checkout(worktree: Path) -> dict:
    """Live path, branch (null when detached) and HEAD (null without commits) of a checkout."""
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(["git", "-C", str(worktree), *args], shell=False,
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (result.stdout.strip() or None) if result.returncode == 0 else None
    return {"worktree": str(Path(worktree).resolve()), "branch": git("symbolic-ref", "--quiet", "--short", "HEAD"),
            "head": git("rev-parse", "--verify", "--quiet", "HEAD")}


def _checkpoint(parent: dict, *, reason: str, phase: str | None = None, worktree: Path | None = None) -> None:
    """Persist the checkout at a boundary. A record without a known worktree (legacy) is left alone."""
    previous = parent.get("checkpoint")
    if worktree is None:
        if not isinstance(previous, dict) or not previous.get("worktree"):
            return
        worktree = Path(previous["worktree"])
    live = capture_checkout(worktree)
    phase = phase or (previous or {}).get("phase") or "start"
    if isinstance(previous, dict):
        if (previous.get("phase") == phase and previous.get("reason") == reason
                and all(previous.get(field) == live[field] for field in CHECKPOINT_FIELDS)):
            return  # nothing moved: a repeated event leaves the record byte-identical
        sequence = previous.get("sequence", 0) + 1
    else:
        sequence = 1
    parent["checkpoint"] = {**live, "phase": phase, "reason": reason, "recorded_at": _now(), "sequence": sequence}


def start(*, worktree: Path, explicit_repo: str | None = None, repo_root: Path = ROOT,
          owner_id: str | None = None, pid: int | None = None) -> dict:
    """Create the parent run; with `owner_id`, the caller is its recorded owner and takes its lease.

    Without an owner the run is unowned (in-process use and tests): it accepts
    writes from any caller, exactly like a record created before leases.
    """
    parent = {"target_repository": None, "policy_version": None, "phases": [],
              "mutations": {"github": 0, "code": 0}, "final_status": "IN_PROGRESS", "blocker_code": None,
              "context": empty_context(), "owner_id": _safe(owner_id, OWNER_RE, "owner id")}
    record = provenance.start_run(repo_root, backlog_id=None, github_issue=None,
                                  user_objective="Parent orchestration run",
                                  route_task_result=None, starting_commit=None,
                                  parent_orchestration=parent)
    lease = None
    try:
        parent["target_repository"] = github_integration.resolve_target(worktree, explicit_repo)
        _checkpoint(parent, reason="run_start", phase="start", worktree=worktree)
        provenance.update_parent_orchestration(repo_root, record["run_id"], parent)
        if owner_id is not None:
            lease = run_lease.acquire(record["run_id"], owner_id, pid=pid, repo_root=repo_root)["lease"]
    except github_integration.IntegrationError as exc:
        # The record already exists even when target resolution fails.
        finish(record["run_id"], "BLOCKED", blocker_code=exc.code, owner_id=owner_id, repo_root=repo_root)
    except run_lease.LeaseError as exc:
        finish(record["run_id"], "BLOCKED", blocker_code=exc.code, owner_id=owner_id, repo_root=repo_root)
    except Exception:
        # Keep the trace terminal even when Git discovery itself fails.
        finish(record["run_id"], "FAILED", blocker_code="TARGET_RESOLUTION_FAILED", owner_id=owner_id,
               repo_root=repo_root)
    current = provenance.get_run(repo_root, record["run_id"])["parent_orchestration"]
    result = {"run_id": record["run_id"], "target_repository": current["target_repository"],
              "status": current["final_status"], "blocker_code": current["blocker_code"]}
    if lease is not None and current["final_status"] == "IN_PROGRESS":
        result["lease"] = lease
    return result


def _lease_failure(exc: run_lease.LeaseError) -> ParentRunError:
    return ParentRunError(exc.detail, error_code=exc.code,
                          details={"lease": exc.lease, "lease_state": exc.state, "stale_reason": exc.stale_reason,
                                   "holder_alive": exc.holder_alive})


def _verify_checkpoint_unlocked(run_id: str, worktree: Path, owner_id: str, repo_root: Path) -> dict:
    """Compare the recorded checkpoint with the live checkout and record the check.

    Caller holds the lease lock and the run lock. A verified check makes the
    caller the run's recorded owner. A difference, or a missing checkpoint (a
    record from before checkpoints existed), is recorded and returned; the run
    stays IN_PROGRESS for an operator to decide. Nothing is ever re-based.
    """
    record = provenance.get_run(repo_root, run_id)
    parent = _parent(record)
    expected = parent.get("checkpoint")
    live = capture_checkout(worktree)
    if not isinstance(expected, dict):
        check = {"at": _now(), "owner_id": owner_id, "previous_owner_id": parent.get("owner_id"),
                 "status": "unverified", "reason_code": "RESUME_CHECKPOINT_MISSING", "mismatches": []}
    else:
        mismatches = [{"field": field, "expected": expected.get(field), "actual": live[field]}
                      for field in CHECKPOINT_FIELDS if expected.get(field) != live[field]]
        check = {"at": _now(), "owner_id": owner_id, "previous_owner_id": parent.get("owner_id"),
                 "status": "drift" if mismatches else "verified",
                 "reason_code": "RESUME_CHECKPOINT_DRIFT" if mismatches else None, "mismatches": mismatches}
    parent.setdefault("resume_checks", []).append(check)
    parent["resume_checks"] = parent["resume_checks"][-MAX_RESUME_CHECKS:]
    if check["status"] == "verified":
        # A verified resume makes the caller the owner; `previous_owner_id` keeps who owned it before.
        parent["owner_id"] = owner_id
    provenance.update_parent_orchestration(repo_root, run_id, parent)
    return {"check": check, "checkpoint": expected, "live": live}


def resume(run_id: str, *, worktree: Path, owner_id: str, pid: int | None = None,
           explicit_repo: str | None = None, handoff: bool = False, caller_pid: int | None = None,
           repo_root: Path = ROOT) -> dict:
    """Continue an IN_PROGRESS run in a new process: verify identity, take the lease, verify the checkpoint.

    A terminal run returns its status without a lease. A live lease of another
    owner fails closed (RUN_LEASE_HELD), a stale one too (RUN_LEASE_STALE,
    recoverable only by `run_lease.py takeover`). The run state is re-read
    under the run lock after the lease is taken. Checkpoint drift
    (RESUME_CHECKPOINT_DRIFT) or a missing checkpoint (RESUME_CHECKPOINT_MISSING)
    refuses the resume with the expected and actual values, records the check,
    releases the lease just taken and leaves the run IN_PROGRESS.
    """
    current = status(run_id, repo_root=repo_root, worktree=worktree, explicit_repo=explicit_repo)
    if current["status"] != "IN_PROGRESS":
        return current
    try:
        with run_lease.claim(run_id, owner_id, pid=pid, handoff=handoff, caller_pid=caller_pid,
                             repo_root=repo_root) as held:
            with _run_lock(repo_root, run_id):
                if provenance.get_run(repo_root, run_id)["outcome"] != "in_progress":
                    held.release()  # finished between the status read and the lease
                    return status(run_id, repo_root=repo_root)
                verdict = _verify_checkpoint_unlocked(run_id, worktree, owner_id, repo_root)
            if verdict["check"]["status"] != "verified":
                held.release()
            lease = held.lease
    except run_lease.LeaseError as exc:
        raise _lease_failure(exc) from exc
    check = verdict["check"]
    if check["status"] != "verified":
        detail = ("run has no resume checkpoint and cannot be verified" if check["status"] == "unverified"
                  else "checkout differs from the resume checkpoint: " +
                  ", ".join(f"{item['field']} expected {item['expected']} actual {item['actual']}"
                            for item in check["mismatches"]))
        raise ParentRunError(detail, error_code=check["reason_code"],
                             details={"expected": verdict["checkpoint"], "actual": verdict["live"],
                                      "mismatches": check["mismatches"], "run_status": "IN_PROGRESS"})
    return {**current, "lease": lease, "checkpoint": verdict["checkpoint"]}


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


def _event_priced(event: dict, cost) -> bool:
    """An event is fully priced when it carries a cost, and that cost is not zero despite tokens."""
    if cost is None:
        return False
    return not (cost == 0 and any((event.get(field) or 0) > 0 for field in USAGE_COUNTERS if field != "duration_ms"))


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
    # Like total_tokens_complete: false once any event could not be priced.
    usage["cost_complete"] = usage.get("cost_complete", True) and _event_priced(event, cost)


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
    if status not in WAVE_EVENT_STATUSES:
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
    elif wave["status"] == "abandoned" or WAVE_STATUSES.index(status) < WAVE_STATUSES.index(wave["status"]):
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


def record_event(run_id: str, event: dict, *, owner_id: str | None = None, repo_root: Path = ROOT) -> dict:
    with _owned(run_id, owner_id, repo_root):
        return _record_event_unlocked(run_id, event, repo_root=repo_root)


def _record_event_unlocked(run_id: str, event: dict, *, repo_root: Path = ROOT) -> dict:
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
        raise ParentRunError("event must have a kind")
    kind = event["kind"]
    allowed = {
        "phase": {"kind", "phase", "status", "route", "routing", "policy_version", "blocker_code", "escalation_reason"},
        "escalation": {"kind", "phase", "from_route", "to_route", "reason"},
        "worker": {"kind", "phase", "step_id", "only_if_open", "model", "backend", "backend_model", "reasoning_effort", "status", "blocker_code", "installed_backend_version", "required_backend_version"},
        "child": {"kind", "phase", "run_id"},
        "mutation": {"kind", "github", "code"},
        "context": {"kind", "starting_commit", "baseline_tokens", "context_tokens", "working_tokens", "compaction"},
        "usage": {"kind", "backend", "estimated_cost_usd", *USAGE_COUNTERS},
        "wave": {"kind", "wave_id", "task_ids", "status"},
        "concurrency": {"kind", "peak_workers", "active_ms"},
        "spawn": {"kind"},
        "activity": {"kind", "status", "tool", "task_id", "step_id", "subagent_id", "route", "prompt_digest",
                     "prompt_excerpt", "goal", "result_summary", "result_digest", "commit_sha"},
        "interaction": {"kind", "type", "summary", "task_id", "step_id", "subagent_id", "reply_to"},
        "lead_usage": {"kind", "model", "phase", "task_type", "turn_index", "duration_ms", "input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "total_tokens", "estimated_cost_usd"},
        "checkpoint": {"kind", "phase", "reason", "workspace_bound"},
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
        "checkpoint": {"kind", "phase", "reason"},
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
        phase.pop("workspace_bound", None)
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
        _checkpoint(parent, reason="phase_boundary", phase=phase["id"])
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
        prior = worker["status"]
        # Validate every field before anything can return or change, including the automatic no-op.
        if "only_if_open" in event and event["only_if_open"] is not True:
            raise ParentRunError("invalid worker event")
        if "reasoning_effort" in event and event["reasoning_effort"] not in REASONING_EFFORTS:
            raise ParentRunError("invalid reasoning effort")
        values = {key: _safe(event[key], pattern, key) for key, pattern in (
            ("model", MODEL_RE), ("backend", MODEL_RE), ("backend_model", MODEL_RE), ("blocker_code", CODE_RE),
            ("installed_backend_version", VERSION_RE), ("required_backend_version", VERSION_RE)) if key in event}
        retry_started, retry_succeeded = _retry_activity(parent, phase, step_id) if prior == "failed" else (False, False)
        if prior == "failed" and event["status"] == "in_progress" and retry_started:
            # The retry's own launch record: the failure stands (and stays visible) until the retry completes.
            return parent
        retried = prior == "failed" and event["status"] == "completed" and retry_succeeded
        if event.get("only_if_open") is True and prior not in {"not_started", "in_progress"} and not retried:
            # The lifecycle's automatic record of a worker's raw result: never overrides a recorded status.
            return parent
        terminal = {"completed", "blocked", "failed"}
        override = None
        if retried:
            # A later start of the same step after its failure: the retry's success replaces the failure. The
            # failure stays in `worker_overrides`, recorded by the lead or by the extension's automatic record.
            history = [item for item in parent.get("worker_overrides") or []
                       if (item["phase_id"], item["step_id"], item["route_epoch"]) == (phase["id"], step_id, epoch)]
            if (len(history) >= MAX_OVERRIDES_PER_WORKER
                    or len(parent.get("worker_overrides") or []) >= MAX_WORKER_OVERRIDES):
                raise ParentRunError("too many overrides recorded for this worker or run",
                                     error_code="PARENT_WORKER_OVERRIDE_LIMIT")
            override = {"phase_id": phase["id"], "step_id": step_id, "route_epoch": epoch,
                        "from_status": prior, "from_blocker_code": worker.get("blocker_code"),
                        "to_status": "completed", "blocker_code": None, "phase_status": phase["status"],
                        "by": "extension" if event.get("only_if_open") is True else "lead",
                        "reason": "retry_succeeded", "at": _now()}
            worker["blocker_code"] = None
        elif prior in terminal:
            # The lead's judgment wins over an earlier record, but never silently: each override is
            # appended to `worker_overrides` with what it replaced. Allowed: completed -> failed/blocked
            # and failed -> blocked (each with a blocker code), and a failed/blocked worker restated with
            # a different blocker code. Nothing else reopens or changes a terminal worker.
            reclassified = event["status"] != prior
            recoded = (not reclassified and prior in {"failed", "blocked"} and "blocker_code" in values
                       and values["blocker_code"] != worker.get("blocker_code"))
            if reclassified and not ((prior, event["status"]) in {("completed", "failed"), ("completed", "blocked"),
                                                                  ("failed", "blocked")}
                                     and values.get("blocker_code") is not None):
                raise ParentRunError("worker is already terminal", error_code="PARENT_WORKER_TRANSITION_INVALID")
            if reclassified or recoded:
                history = [item for item in parent.get("worker_overrides") or []
                           if (item["phase_id"], item["step_id"], item["route_epoch"]) == (phase["id"], step_id, epoch)]
                seen = {worker.get("blocker_code")} | {code for item in history
                                                       for code in (item["from_blocker_code"], item["blocker_code"])}
                if recoded and values["blocker_code"] in seen:
                    raise ParentRunError("worker was already recorded with this blocker code in this route epoch",
                                         error_code="PARENT_WORKER_RECODE_REPEATED")
                if (len(history) >= MAX_OVERRIDES_PER_WORKER
                        or len(parent.get("worker_overrides") or []) >= MAX_WORKER_OVERRIDES):
                    raise ParentRunError("too many overrides recorded for this worker or run",
                                         error_code="PARENT_WORKER_OVERRIDE_LIMIT")
                override = {"phase_id": phase["id"], "step_id": step_id, "route_epoch": epoch,
                            "from_status": prior, "from_blocker_code": worker.get("blocker_code")}
                details = {key: worker[key] for key in ("installed_backend_version", "required_backend_version")
                           if worker.get(key) is not None}
                if details:
                    override["from_details"] = details
                override.update(to_status=event["status"], blocker_code=values["blocker_code"],
                                phase_status=phase["status"], by="lead", at=_now())
        worker["status"] = event["status"]
        if "reasoning_effort" in event:
            if worker.get("reasoning_effort") is not None and worker["reasoning_effort"] != event["reasoning_effort"]:
                raise ParentRunError("worker field conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            worker["reasoning_effort"] = event["reasoning_effort"]
        for key, value in values.items():
            replaced = key == "blocker_code" and override is not None
            if worker.get(key) is not None and value != worker[key] and not replaced:
                raise ParentRunError("worker field conflicts with prior event", error_code="PARENT_EVENT_CONFLICT")
            worker[key] = value
        if override is not None:
            parent.setdefault("worker_overrides", []).append(override)
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
        # Optional, set by the lifecycle extension at worker start: the routed route and a SHA-256 digest of
        # the worker contract (the contract text itself is never recorded).
        if event.get("route") is not None:
            if event["route"] not in ROUTES:
                raise ParentRunError("invalid activity route")
            entry["route"] = event["route"]
        if event.get("prompt_digest") is not None:
            digest = event["prompt_digest"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ParentRunError("invalid activity prompt digest")
            entry["prompt_digest"] = digest
        # Bounded text, set by the extension from what it observes: the start of the worker contract and the
        # start of the worker's result. Sanitised here, never rejected: control characters are dropped,
        # whitespace is collapsed, and secret-like text is replaced by a marker. The full text lives only in
        # the Pi session transcript.
        for field, limit in (("prompt_excerpt", ACTIVITY_EXCERPT_CHARS), ("goal", ACTIVITY_GOAL_CHARS),
                             ("result_summary", ACTIVITY_SUMMARY_CHARS)):
            if event.get(field) is not None:
                if not isinstance(event[field], str):
                    raise ParentRunError(f"invalid activity {field}")
                entry[field] = _bounded_text(event[field], limit)
        if event.get("result_digest") is not None:
            if not isinstance(event["result_digest"], str) or not DIGEST_RE.fullmatch(event["result_digest"]):
                raise ParentRunError("invalid activity result digest")
            entry["result_digest"] = event["result_digest"]
        if event.get("commit_sha") is not None:
            if not isinstance(event["commit_sha"], str) or not COMMIT_RE.fullmatch(event["commit_sha"]):
                raise ParentRunError("invalid activity commit sha")
            entry["commit_sha"] = event["commit_sha"]
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
        for field in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "total_tokens"):
            if event.get(field) is not None:
                lead[field] = (lead.get(field) or 0) + _count(event[field], field)
        cost = event.get("estimated_cost_usd")
        lead["cost_complete"] = lead.get("cost_complete", True) and _event_priced(event, cost)
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
        if event["status"] == "integrating":
            # Before integration mutates the parent checkout: a crash mid-integration is then drift.
            _checkpoint(parent, reason="integration_started")
        elif event["status"] == "integrated":
            _checkpoint(parent, reason="wave_integrated")
    elif kind == "checkpoint":
        if event["reason"] not in CHECKPOINT_EVENT_REASONS:
            raise ParentRunError("invalid checkpoint reason")
        if "workspace_bound" in event and event["workspace_bound"] is not True:
            raise ParentRunError("invalid checkpoint event")
        if event.get("workspace_bound") is True:
            # A bound worktree is the evidence that a mutating step may have run for this task and route epoch.
            _phase(parent, event["phase"])["workspace_bound"] = True
        _checkpoint(parent, reason=event["reason"], phase=_safe(event["phase"], ID_RE, "phase id"))
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
        _checkpoint(parent, reason="mutation_recorded")
    if kind in {"worker", "usage", "spawn", "lead_usage"}:
        provenance.update_parent_orchestration(repo_root, run_id, parent,
                                               orchestration_metrics=delegation_metrics(record, parent))
    else:
        provenance.update_parent_orchestration(repo_root, run_id, parent)
    return parent


def finish(run_id: str, status: str, *, blocker_code: str | None = None, owner_id: str | None = None,
           repo_root: Path = ROOT) -> dict:
    """Finalize the run as its owner; the explicit outcome releases that owner's lease."""
    with _owned(run_id, owner_id, repo_root) as held:
        record = _finish_unlocked(run_id, status, blocker_code=blocker_code, repo_root=repo_root)
        if record.get("outcome") != "in_progress" and held.lease is not None:
            held.release()
    return record


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
        # An integration begun and never ended leaves the parent checkout in an unknown state. A wave that was
        # only declared (read-only work, nothing to merge) stays as recorded.
        integrating = [wave["id"] for wave in parent.get("waves") or []
                       if wave["status"] == "integrating" and _wave_is_current(phases, wave)]
        if integrating:
            raise ParentRunError("wave integration was begun and never ended: " + ", ".join(integrating[:12]) +
                                 ". Record integrate_end, or finish as PARTIAL",
                                 error_code="PARENT_WAVE_INTEGRATING")
        unmerged = [wave["id"] for wave in parent.get("waves") or [] if wave["status"] == "declared"
                    and _wave_is_current(phases, wave)
                    and any(phase.get("workspace_bound") for phase in phases if phase["id"] in wave["task_ids"])]
        if unmerged:
            raise ParentRunError("wave with a mutating task was never integrated: " + ", ".join(unmerged[:12]) +
                                 ". Record integrate_begin and integrate_end, or finish as PARTIAL",
                                 error_code="PARENT_WAVE_NOT_INTEGRATED")
        missing = _missing_reviews(phases)
        if missing:
            raise ParentRunError("mandatory independent review not completed for phase " + ", ".join(missing[:12]) +
                                 ". Run and record the review, or finish as PARTIAL",
                                 error_code="PARENT_REVIEW_MISSING")
    else:
        _reconcile_open(parent, status)
    parent["final_status"] = status
    parent["blocker_code"] = _safe(blocker_code, CODE_RE, "blocker code")
    return provenance.finish_run(repo_root, run_id, outcome=status.lower(),
                                 parent_orchestration=parent,
                                 orchestration_metrics=delegation_metrics(record, parent))


def _wave_is_current(phases: list[dict], wave: dict) -> bool:
    """A wave no later wave has superseded: some task of it still names it as its wave."""
    return any(phase.get("wave_id") == wave["id"] for phase in phases if phase["id"] in wave["task_ids"])


def _reconcile_open(parent: dict, status: str) -> None:
    """A run that ends without COMPLETED leaves no phase or worker open.

    Each phase, and each worker of the phase's current route epoch, still not_started or in_progress becomes `abandoned`, and the
    transition is appended to `parent.reconciled` with its prior status. Events, activity
    and every other field stay as recorded. A reconciled item is no longer open, so a
    repeated call adds nothing.
    """
    open_states = {"not_started", "in_progress"}
    at = _now()
    reason = f"run finished {status}"
    entries = parent.setdefault("reconciled", [])
    for phase in parent["phases"]:
        epoch = len(phase.get("escalations") or [])
        if phase["status"] in open_states:
            entries.append({"kind": "phase", "phase_id": phase["id"], "from_status": phase["status"],
                            "to": RECONCILED_STATUS, "reason": reason, "at": at})
            phase["status"] = RECONCILED_STATUS
        for worker in phase["workers"]:
            # Workers of an earlier route epoch were superseded by an escalation; like the COMPLETED
            # check, they are left exactly as recorded.
            if worker["status"] in open_states and worker.get("route_epoch", 0) == epoch:
                entries.append({"kind": "worker", "phase_id": phase["id"], "step_id": worker["step_id"],
                                "route_epoch": epoch, "from_status": worker["status"],
                                "to": RECONCILED_STATUS, "reason": reason, "at": at})
                worker["status"] = RECONCILED_STATUS
    for wave in parent.get("waves") or []:
        if wave["status"] in WAVE_OPEN_STATUSES:
            entries.append({"kind": "wave", "wave_id": wave["id"], "from_status": wave["status"],
                            "to": RECONCILED_STATUS, "reason": reason, "at": at})
            wave["status"] = RECONCILED_STATUS
    if not entries:
        del parent["reconciled"]


def settle(run_id: str, *, owner_id: str | None = None, repo_root: Path = ROOT) -> dict:
    """Fail closed when the agent exits without recording an explicit outcome."""
    record = provenance.get_run(repo_root, run_id)
    if record["outcome"] != "in_progress":
        return record
    phases = _parent(record)["phases"]
    observed = next((phase["blocker_code"] for phase in reversed(phases)
                     if phase["status"] == "blocked" and phase["blocker_code"]), None)
    return finish(run_id, "BLOCKED", blocker_code=observed or "ORCHESTRATION_OUTCOME_UNRECORDED",
                  owner_id=owner_id, repo_root=repo_root)


def terminal_response(record: dict, *, repo_root: Path = ROOT) -> dict:
    """Add the actual trace location to the CLI result without changing the stored record."""
    return {**record, "trace_path": str(provenance.run_path(repo_root, record["run_id"]))}


class ParentRun:
    """In-process orchestration scope: all exceptions finalize the parent."""
    def __init__(self, *, worktree: Path, explicit_repo: str | None = None, repo_root: Path = ROOT):
        self.repo_root = repo_root
        # The in-process scope owns its run and holds the lease as this process.
        self.owner_id = f"inproc-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.run_id = start(worktree=worktree, explicit_repo=explicit_repo, repo_root=repo_root,
                            owner_id=self.owner_id, pid=os.getpid())["run_id"]

    def __enter__(self) -> "ParentRun":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        record = provenance.get_run(self.repo_root, self.run_id)
        if record["outcome"] == "in_progress":
            if isinstance(exc, ParentRunBlocked):
                finish(self.run_id, "BLOCKED", blocker_code=exc.code, owner_id=self.owner_id, repo_root=self.repo_root)
            elif exc_type is not None:
                finish(self.run_id, "FAILED", blocker_code="ORCHESTRATION_EXECUTION_FAILED",
                       owner_id=self.owner_id, repo_root=self.repo_root)
            else:
                settle(self.run_id, owner_id=self.owner_id, repo_root=self.repo_root)
        return False

    def event(self, event: dict) -> dict:
        return record_event(self.run_id, event, owner_id=self.owner_id, repo_root=self.repo_root)

    def finish(self, status: str, blocker_code: str | None = None) -> dict:
        return finish(self.run_id, status, blocker_code=blocker_code, owner_id=self.owner_id,
                      repo_root=self.repo_root)

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
    p_start.add_argument("--owner", required=True, help="recorded owner; takes the run lease")
    p_start.add_argument("--pid", type=int, help="lease owner process (default: the caller)")
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("run_id")
    p_resume.add_argument("--worktree", type=Path, required=True)
    p_resume.add_argument("--repo")
    p_resume.add_argument("--owner", required=True)
    p_resume.add_argument("--pid", type=int, help="lease owner process (default: the caller)")
    p_resume.add_argument("--handoff", action="store_true",
                          help="take this owner's lease from the launcher process that started the caller")
    p_event = sub.add_parser("event")
    p_event.add_argument("run_id")
    p_event.add_argument("--owner", required=True)
    p_finish = sub.add_parser("finish")
    p_finish.add_argument("run_id")
    p_finish.add_argument("status", choices=sorted(STATUSES))
    p_finish.add_argument("--blocker-code")
    p_finish.add_argument("--owner", required=True)
    p_settle = sub.add_parser("settle")
    p_settle.add_argument("run_id")
    p_settle.add_argument("--owner", required=True)
    p_status = sub.add_parser("status")
    p_status.add_argument("run_id")
    p_status.add_argument("--worktree", type=Path)
    p_status.add_argument("--repo")
    args = parser.parse_args()
    event_kind = None
    try:
        if args.command == "start":
            result = start(worktree=args.worktree, explicit_repo=args.repo, owner_id=args.owner,
                           pid=args.pid if args.pid is not None else os.getppid())
        elif args.command == "resume":
            result = resume(args.run_id, worktree=args.worktree, owner_id=args.owner, explicit_repo=args.repo,
                            pid=args.pid if args.pid is not None else os.getppid(), handoff=args.handoff,
                            caller_pid=os.getppid())
        elif args.command == "event":
            payload = json.load(sys.stdin)
            event_kind = payload.get("kind") if isinstance(payload, dict) else None
            result = record_event(args.run_id, payload, owner_id=args.owner)
        elif args.command == "finish":
            result = terminal_response(finish(args.run_id, args.status, blocker_code=args.blocker_code,
                                              owner_id=args.owner))
        elif args.command == "status":
            result = status(args.run_id, worktree=args.worktree, explicit_repo=args.repo)
        else:
            result = terminal_response(settle(args.run_id, owner_id=args.owner))
        print(json.dumps(result, sort_keys=True))
        return 0
    except run_lease.LeaseError as exc:
        print(json.dumps({"ok": False, "component": "parent_orchestration", "operation": args.command,
                          "error_category": "LeaseError", **exc.payload(),
                          "run_id": getattr(args, "run_id", None)}, sort_keys=True))
        return 1
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
                            else "tools/parent_orchestration.py")),
                          **({"details": exc.details} if isinstance(exc, ParentRunError) and exc.details else {})}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
