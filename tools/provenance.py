#!/usr/bin/env python3
"""Append-oriented run provenance: one JSON file per run under
provenance/runs/<run_id>.json, validated against schemas/run-record.schema.json.

Storage layout decision: unlike backlog/backlog.yaml (a single collection
file, see tools/backlog_store.py's module docstring for rationale), run
provenance is one file per run. Runs are genuinely append-only artifacts that
are never edited by a different run, are created and finalized independently
(possibly concurrently, for parallel orchestration), and are typically
consumed by run_id or by scanning the directory -- a shape that benefits from
per-run files (no shared-file contention, trivial to `git log --follow` an
individual run, no risk of one run's write clobbering another's) rather than
one growing collection file.

This module never invokes `gh` and never reinterprets routing/execution-plan
semantics: `route_task_result` is stored exactly as returned by
tools/route_task.py. Optional run-level `orchestration_metrics` are
observational only: they summarize delegation topology, cost/time/quality
evidence, and derived benchmark quantities without feeding back into the
active route or delegation policy.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
import posixpath
import sys
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - surfaced by callers
    raise ImportError(
        "Missing dependency: jsonschema. Run: python3 -m pip install -r requirements-dev.txt"
    ) from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backlog_store as store  # noqa: E402


class ProvenanceError(Exception):
    """Base class for all provenance errors."""


class RunNotFoundError(ProvenanceError):
    pass


class RunLockTimeout(ProvenanceError):
    """Another writer held the run's lock for longer than the bounded wait."""


class RunAlreadyExistsError(ProvenanceError):
    pass


class RunSchemaValidationError(ProvenanceError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


RUNS_RELATIVE_DIR = Path("provenance") / "runs"
STATE_ENV = "PI_ORCHESTRATOR_STATE_DIR"
SCHEMA_RELATIVE_PATH = Path("schemas") / "run-record.schema.json"

STEP_STATUSES = ("pending", "in_progress", "completed", "failed", "skipped")
STEP_TRANSITIONS = {
    "pending": {"in_progress", "completed", "failed", "skipped"},
    "in_progress": {"completed", "failed", "skipped"},
    "completed": set(),
    "failed": set(),
    "skipped": set(),
}
RUN_OUTCOMES = ("in_progress", "completed", "failed", "partial", "blocked", "dry_run")
TERMINAL_RUN_OUTCOMES = ("completed", "failed", "partial", "blocked", "dry_run")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_STEP_FIELDS = (
    "task_id",
    "parent_task_id",
    "execution_plan_step_id",
    "role",
    "family",
    "backend",
    "model",
    "capacity_tier",
    "reasoning_setting",
    "context_strategy",
    "context_window_tier",
    "fresh_context",
    "mutation_permission",
    "worker_identifier",
    "status",
    "outcome",
    "files_changed",
    "validation_performed",
    "review_findings",
    "repair_of",
    "telemetry",
    "returned_artifacts",
)

TRACE_SCHEMA_VERSION = 1

_TELEMETRY_FIELDS = (
    "schema_version",
    "source",
    "started_at",
    "ended_at",
    "duration_ms",
    "queue_ms",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "input_bytes",
    "output_bytes",
)

_TELEMETRY_DEFAULTS: dict[str, Any] = {field: None for field in _TELEMETRY_FIELDS}
_TELEMETRY_DEFAULTS["schema_version"] = 1

# Usage-metadata field names recognized by `extract_token_usage`, grouped by
# the logical counter they populate. Covers snake_case, camelCase, and the
# Gemini-style `prompt/candidates/totalTokenCount` naming -- every name
# listed here is a *structured metadata field*, never a place this module
# parses free text or guesses a count.
_USAGE_CONTAINER_KEYS = ("usage", "usage_metadata", "token_usage")
_INPUT_TOKEN_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens", "promptTokenCount", "input")
_OUTPUT_TOKEN_KEYS = ("output_tokens", "outputTokens", "completion_tokens", "completionTokens", "candidatesTokenCount", "output")
_CACHED_TOKEN_KEYS = ("cached_tokens", "cachedTokens", "cache_read_input_tokens", "cacheReadInputTokens", "cachedContentTokenCount", "cacheRead")
_CACHED_TOKEN_DETAIL_PATHS = (
    ("prompt_tokens_details", "cached_tokens"),
    ("input_tokens_details", "cached_tokens"),
)
_TOTAL_TOKEN_KEYS = ("total_tokens", "totalTokens", "totalTokenCount")


def _coerce_nonnegative_int(value: Any) -> int | None:
    """Accept only an actual nonnegative integer (or an integral float, e.g.
    `12.0`) -- never a numeric string, and never `bool` (a `bool` is a `int`
    subclass in Python, so it must be excluded explicitly)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    return None


def extract_token_usage(usage_source: Any) -> dict[str, int | None]:
    """Safely extract `input_tokens`/`output_tokens`/`cached_tokens`/
    `total_tokens` from structured provider usage metadata only.

    `usage_source` may be the usage object itself, or a container that wraps
    one under a `usage`/`usage_metadata`/`token_usage` key (checked in that
    order; the first present dict wins). Each logical counter accepts several
    provider-specific field spellings (snake_case, camelCase, and Gemini's
    `promptTokenCount`/`candidatesTokenCount`/`totalTokenCount`); only
    nonnegative integers are accepted, everything else fails closed to
    `None` for that field rather than being guessed:

      - the field is absent entirely -> `None`;
      - present under exactly one recognized name with a valid value -> that
        value;
      - present under two or more recognized names that disagree (or any
        recognized name holds a non-integer/negative value) -> `None`,
        since a conflicting or malformed observation cannot be trusted.

    The raw `usage_source` is never returned or otherwise persisted -- only
    the extracted integers (or `None`)."""
    result: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "total_tokens": None,
    }
    if not isinstance(usage_source, dict):
        return result

    usage = usage_source
    for key in _USAGE_CONTAINER_KEYS:
        nested = usage_source.get(key)
        if isinstance(nested, dict):
            usage = nested
            break

    for field, keys in (
        ("input_tokens", _INPUT_TOKEN_KEYS),
        ("output_tokens", _OUTPUT_TOKEN_KEYS),
        ("cached_tokens", _CACHED_TOKEN_KEYS),
        ("total_tokens", _TOTAL_TOKEN_KEYS),
    ):
        observed: set[Any] = set()
        malformed = False
        for key in keys:
            if key in usage:
                coerced = _coerce_nonnegative_int(usage[key])
                if coerced is None:
                    malformed = True
                else:
                    observed.add(coerced)
        if field == "cached_tokens":
            for container_key, detail_key in _CACHED_TOKEN_DETAIL_PATHS:
                details = usage.get(container_key)
                if isinstance(details, dict) and detail_key in details:
                    coerced = _coerce_nonnegative_int(details[detail_key])
                    if coerced is None:
                        malformed = True
                    else:
                        observed.add(coerced)
        if not malformed and len(observed) == 1:
            result[field] = observed.pop()
    return result


def start_observation() -> dict[str, Any]:
    """Begin a timing observation: a monotonic clock reading (immune to
    system-clock adjustments, used only to derive `duration_ms`) plus the
    wall-clock UTC `started_at` timestamp actually recorded in provenance.
    Returns an opaque handle for `finish_observation`; callers must not
    inspect or persist its internal shape directly."""
    return {"_monotonic_start": time.monotonic(), "started_at": store.now_iso()}


def finish_observation(
    handle: dict[str, Any],
    *,
    source: str | None = None,
    usage: Any = None,
    input_bytes: int | None = None,
    output_bytes: int | None = None,
) -> dict[str, Any]:
    """End a timing observation started by `start_observation` and return a
    telemetry-shaped dict: observed `started_at`/`ended_at`/`duration_ms`
    (duration computed only from the monotonic clock, never guessed),
    `source`, `input_bytes`/`output_bytes` as given, and token counts safely
    extracted from `usage` via `extract_token_usage` (or all `None` if
    `usage` is `None`/unstructured). `queue_ms`/`estimated_cost_usd` are left
    absent here -- this function never estimates cost -- and every field this
    call did not observe stays an explicit `None` once passed through
    `_normalize_telemetry` by `add_step`/`update_step`/`start_run`."""
    if not isinstance(handle, dict) or "_monotonic_start" not in handle:
        raise ProvenanceError("finish_observation requires a handle returned by start_observation")
    monotonic_start = handle["_monotonic_start"]
    duration_ms = max(0.0, (time.monotonic() - monotonic_start) * 1000.0)
    telemetry: dict[str, Any] = {
        "source": source,
        "started_at": handle.get("started_at"),
        "ended_at": store.now_iso(),
        "duration_ms": duration_ms,
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
    }
    telemetry.update(extract_token_usage(usage))
    return telemetry


class ObservedCallError(ProvenanceError):
    """Wraps an exception raised by an observed call while preserving the
    closed telemetry object measured at that call boundary. The original
    exception is available as `original_exception`; callers may persist
    `telemetry` without persisting raw provider responses, prompts, or logs."""

    def __init__(self, original_exception: BaseException, telemetry: dict[str, Any]):
        self.original_exception = original_exception
        self.telemetry = telemetry
        super().__init__(str(original_exception))


def observe_call(
    func: Callable[..., Any],
    *args: Any,
    source: str | None = None,
    usage_getter: Callable[[Any], Any] | None = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """Execute one route/worker/subagent call behind an observation boundary.

    This is the executable integration point for orchestrators: timing starts
    immediately before `func(*args, **kwargs)` and ends immediately after it
    returns or raises. On success, returns `(result, telemetry)`. On failure,
    raises `ObservedCallError` with the measured telemetry attached. Token
    metadata is extracted only from structured usage returned by
    `usage_getter(result)`; if no getter is supplied the result itself is
    inspected, but only allowlisted usage fields are ever copied."""
    handle = start_observation()
    try:
        result = func(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - preserve telemetry for any boundary failure
        raise ObservedCallError(exc, finish_observation(handle, source=source)) from exc
    usage = usage_getter(result) if usage_getter is not None else result
    return result, finish_observation(handle, source=source, usage=usage)


_ORCHESTRATION_METRIC_FIELDS = (
    "schema_version",
    "lead_model",
    "delegation_topology",
    "worker_model_mix",
    "spawned_agents",
    "lead_input_tokens",
    "lead_output_tokens",
    "lead_cached_tokens",
    "worker_input_tokens",
    "worker_output_tokens",
    "worker_cached_tokens",
    "total_tokens",
    "wall_clock_ms",
    "worker_execution_ms",
    "coordination_ms",
    "conflicting_edits",
    "corrective_turns",
    "failed_attempts",
    "test_outcome",
    "ci_outcome",
    "reviewer_findings",
    "human_corrections_required",
    "final_task_success",
    "estimated_cost_usd",
    "estimated_direct_tokens",
    "estimated_serial_time_ms",
    "direct_baseline_quality",
    "delegated_quality",
    "coordination_tax_tokens",
    "coordination_tax_time_ms",
    "quality_gain",
    "parallel_speedup",
)
_ORCHESTRATION_DERIVED_FIELDS = frozenset({
    "coordination_tax_tokens",
    "coordination_tax_time_ms",
    "quality_gain",
    "parallel_speedup",
})
_ORCHESTRATION_METRIC_DEFAULTS: dict[str, Any] = {
    field: None for field in _ORCHESTRATION_METRIC_FIELDS
}
_ORCHESTRATION_METRIC_DEFAULTS["schema_version"] = 1

# Telemetry is observational counters/timing only. It must not become a side
# channel for prompts, transcripts, hidden reasoning, credentials, or tokens
# whose name implies authentication rather than usage counts.
_TELEMETRY_FORBIDDEN_KEY_RE = re.compile(
    r"(^|[_-])("
    r"transcript|conversation|messages?|prompts?|completions?|reasoning|hidden[_-]?reasoning|"
    r"secret|secrets|password|passwd|credentials?|api[_-]?keys?|authorization|auth[_-]?header|"
    r"bearer|private[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token"
    r")([_-]|$)",
    re.IGNORECASE,
)
_ROUTE_RESULT_TELEMETRY_KEYS = frozenset({"routing_telemetry", "telemetry"})

# `validation_performed`/`returned_artifacts` free-text fields (command,
# summary, reference) are meant to be short, compact, single-line
# descriptions -- never a place to paste multi-line log/stdout/transcript
# content or a credential. Control characters (including newlines) are
# rejected outright so a stored value can never inject fake trace sections
# or corrupt the fixed-section text layout produced by `render_trace_text`.
_MULTILINE_OR_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Recognizable credential/secret *content* shapes (as opposed to
# `_TELEMETRY_FORBIDDEN_KEY_RE`, which matches secret-like *key names*).
# Conservative and pattern-based, not a substitute for not persisting
# secrets in the first place -- callers must still never pass raw
# prompts/transcripts/credentials into these fields.
_SECRET_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{10,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
)


def _contains_secret_like_content(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_CONTENT_PATTERNS)


def _validate_compact_text(value: Any, path: str, issues: list[str]) -> None:
    """`value` must be a compact, single-line, non-secret-looking string, or
    None. Type errors are left to the JSON Schema check; this only adds the
    content-shape/safety rules the schema alone cannot express."""
    if value is None or not isinstance(value, str):
        return
    if _MULTILINE_OR_CONTROL_RE.search(value):
        issues.append(f"{path}: must be a compact single-line value with no control/newline characters")
    if _contains_secret_like_content(value):
        issues.append(f"{path}: must not contain secret-like content (private keys, tokens, credentials)")


def _validate_reference_value(value: Any, path: str, issues: list[str]) -> None:
    """`reference` must be a repository-relative path (that stays inside the
    repository -- no `../` escape) or a stable http(s) external reference --
    never an absolute filesystem path (which could leak local directory
    layout/usernames) and never secret-looking."""
    if value is None or not isinstance(value, str):
        return
    _validate_compact_text(value, path, issues)
    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
        issues.append(f"{path}: must be a repository-relative path or a stable external reference, not an absolute filesystem path")
        return
    if "://" in value:
        if not value.startswith(("http://", "https://")):
            issues.append(f"{path}: external references must use http:// or https://")
        return
    # A repository-relative reference must resolve to somewhere inside the
    # repository: normalize separators/`.`/`..` segments and reject anything
    # that climbs above the root (e.g. '../../etc/passwd'), which the earlier
    # leading-'/' check alone does not catch.
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        issues.append(f"{path}: repository-relative references must not traverse outside the repository (got {value!r})")


def _validate_free_text_safety(record: Any) -> list[str]:
    """Enforce the no-transcript/no-secret/compact-single-line contract on
    every free-text field a worker/orchestrator actually controls the content
    of -- not just `validation_performed`/`returned_artifacts`, which was the
    original (incomplete) scope of this check. This now also covers the
    top-level `user_objective`/`starting_commit`/`ending_commit`, the
    open-string telemetry fields `routing_telemetry.source` and each step's
    `telemetry.source` (the rest of the telemetry object is closed to a
    fixed set of numeric/enum fields already), `orchestration_metrics.
    lead_model` (its other string fields are closed enums), and each step's
    `task_id`, `parent_task_id`, `execution_plan_step_id`, `outcome`,
    `model`, `backend`, `worker_identifier`, `files_changed[]`, and
    `review_findings[].description`: any of these can hold arbitrary
    orchestrator-supplied text and is rendered verbatim by
    `render_trace_text`/the JSON trace, so each is exactly as capable of
    smuggling a transcript, hidden reasoning, or a credential as the fields
    originally covered. `role`/`family`/`capacity_tier`/`status`/etc. are
    excluded because the JSON Schema already constrains them to closed enums.
    The JSON Schema separately makes `validation_performed`/
    `returned_artifacts` closed, additionalProperties:false objects (so an
    entire forbidden *key* like `stdout`/`transcript` is rejected); this adds
    the content-level check that a permitted field's *value* cannot itself
    smuggle multi-line output, a full transcript, or a credential."""
    issues: list[str] = []
    if not isinstance(record, dict):
        return issues

    _validate_compact_text(record.get("user_objective"), "user_objective", issues)
    _validate_compact_text(record.get("starting_commit"), "starting_commit", issues)
    _validate_compact_text(record.get("ending_commit"), "ending_commit", issues)
    parent = record.get("parent_orchestration")
    if isinstance(parent, dict):
        for field in ("target_repository", "blocker_code"):
            _validate_compact_text(parent.get(field), f"parent_orchestration.{field}", issues)
        for phase_index, phase in enumerate(parent.get("phases", [])):
            if not isinstance(phase, dict):
                continue
            for field in ("id", "blocker_code"):
                _validate_compact_text(phase.get(field), f"parent_orchestration.phases[{phase_index}].{field}", issues)
            for child_index, child in enumerate(phase.get("child_run_ids", [])):
                _validate_compact_text(child, f"parent_orchestration.phases[{phase_index}].child_run_ids[{child_index}]", issues)
            for worker_index, worker in enumerate(phase.get("workers", [])):
                if isinstance(worker, dict):
                    for field in ("step_id", "model", "backend_model", "blocker_code", "installed_backend_version", "required_backend_version"):
                        _validate_compact_text(worker.get(field), f"parent_orchestration.phases[{phase_index}].workers[{worker_index}].{field}", issues)
    integration = record.get("github_integration")
    if isinstance(integration, dict):
        for field in ("target_repository", "target_worktree", "phase", "route", "worker", "model", "blocker_code", "reason"):
            _validate_compact_text(integration.get(field), f"github_integration.{field}", issues)
        for index, repository in enumerate(integration.get("discovered_repositories", [])):
            _validate_compact_text(repository, f"github_integration.discovered_repositories[{index}]", issues)
        selection = integration.get("repository_selection")
        if isinstance(selection, dict):
            for field in ("method", "requested", "resolved"):
                _validate_compact_text(selection.get(field), f"github_integration.repository_selection.{field}", issues)
        for index, entry in enumerate(integration.get("audit", [])):
            if isinstance(entry, dict):
                for field in ("operation", "label", "status"):
                    _validate_compact_text(entry.get(field), f"github_integration.audit[{index}].{field}", issues)

    routing_telemetry = record.get("routing_telemetry")
    if isinstance(routing_telemetry, dict):
        _validate_compact_text(routing_telemetry.get("source"), "routing_telemetry.source", issues)

    orchestration_metrics = record.get("orchestration_metrics")
    if isinstance(orchestration_metrics, dict):
        _validate_compact_text(
            orchestration_metrics.get("lead_model"), "orchestration_metrics.lead_model", issues
        )

    steps = record.get("steps") if isinstance(record.get("steps"), list) else []
    for step_index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_base = f"steps[{step_index}]"
        _validate_compact_text(step.get("task_id"), f"{step_base}.task_id", issues)
        _validate_compact_text(step.get("parent_task_id"), f"{step_base}.parent_task_id", issues)
        _validate_compact_text(
            step.get("execution_plan_step_id"), f"{step_base}.execution_plan_step_id", issues
        )
        _validate_compact_text(step.get("outcome"), f"{step_base}.outcome", issues)
        _validate_compact_text(step.get("model"), f"{step_base}.model", issues)
        _validate_compact_text(step.get("backend"), f"{step_base}.backend", issues)
        _validate_compact_text(step.get("worker_identifier"), f"{step_base}.worker_identifier", issues)

        files_changed = step.get("files_changed") if isinstance(step.get("files_changed"), list) else []
        for file_index, file_path in enumerate(files_changed):
            _validate_compact_text(file_path, f"{step_base}.files_changed[{file_index}]", issues)

        step_telemetry = step.get("telemetry")
        if isinstance(step_telemetry, dict):
            _validate_compact_text(step_telemetry.get("source"), f"{step_base}.telemetry.source", issues)

        findings = step.get("review_findings") if isinstance(step.get("review_findings"), list) else []
        for finding_index, finding in enumerate(findings):
            if isinstance(finding, dict):
                _validate_compact_text(
                    finding.get("description"),
                    f"{step_base}.review_findings[{finding_index}].description",
                    issues,
                )

        validations = step.get("validation_performed") if isinstance(step.get("validation_performed"), list) else []
        for entry_index, entry in enumerate(validations):
            base = f"{step_base}.validation_performed[{entry_index}]"
            if isinstance(entry, str):
                _validate_compact_text(entry, base, issues)
            elif isinstance(entry, dict):
                _validate_compact_text(entry.get("command"), f"{base}.command", issues)
                _validate_compact_text(entry.get("summary"), f"{base}.summary", issues)
        artifacts = step.get("returned_artifacts") if isinstance(step.get("returned_artifacts"), list) else []
        for artifact_index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                continue
            base = f"{step_base}.returned_artifacts[{artifact_index}]"
            _validate_compact_text(artifact.get("summary"), f"{base}.summary", issues)
            _validate_reference_value(artifact.get("reference"), f"{base}.reference", issues)
    return issues


_STEP_DEFAULTS: dict[str, Any] = {field: None for field in _STEP_FIELDS}
_STEP_DEFAULTS["status"] = "pending"


def runs_dir(repo_root: Path) -> Path:
    """Resolve run storage independently from repository assets when requested.

    Cached `pico` launches set PI_ORCHESTRATOR_STATE_DIR so mutable runtime
    provenance never dirties the cached Git checkout. Direct/local developer
    use keeps the historical repository-local provenance/runs layout.
    """
    state_root = os.environ.get(STATE_ENV)
    if state_root:
        root = Path(state_root).expanduser()
        if not root.is_absolute():
            raise ProvenanceError(f"{STATE_ENV} must be an absolute path")
        return root / "runs"
    return repo_root / RUNS_RELATIVE_DIR


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise ProvenanceError(f"invalid run_id {run_id!r}; must be filename-safe")
    return run_id


def run_path(repo_root: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    base = runs_dir(repo_root).resolve()
    path = (base / f"{run_id}.json").resolve()
    if base != path.parent:
        raise ProvenanceError(f"invalid run_id {run_id!r}; path escapes the configured runs directory")
    return path



RUN_LOCK_WAIT_SECONDS = 5


@contextmanager
def run_lock(repo_root: Path, run_id: str, *, wait_seconds: float | None = None):
    """Serialize read-modify-write operations on one run record across processes.

    Parallel waves record steps, metrics, and parent events for one run from
    several processes at once; every writer takes this lock (the
    `provenance/runs/<run_id>.lock` file shared with tools/parent_orchestration.py).
    The lock is not re-entrant: callers already holding it use the unlocked
    helpers. A bounded wait raises RunLockTimeout instead of hanging.
    """
    path = run_path(repo_root, run_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    wait = RUN_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    with path.open("a+b") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as error:
                if time.monotonic() >= deadline:
                    raise RunLockTimeout(f"run {run_id!r} is busy") from error
                time.sleep(0.02)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_rfc3339_datetime(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return "T" in value
    except ValueError:
        return False


_MIN_SORT_INSTANT = datetime.min.replace(tzinfo=timezone.utc)


def _sort_instant(value: Any) -> datetime:
    """Parse `started_at` into a timezone-aware UTC instant for chronological
    sorting. Two valid RFC3339 timestamps with different UTC offsets can
    disagree lexically even though their instants are correctly ordered
    (e.g. '2026-01-01T01:00:00+05:00' is actually earlier, in UTC, than
    '2025-12-31T23:00:00Z' despite sorting later as a plain string), so
    `list_runs`/`select_trace_runs` must never compare these strings
    lexically. Missing/unparseable values sort first (oldest), never guessed
    as "now" or dropped, so a run with a bad timestamp is still selectable
    and never silently wins `--latest`."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _MIN_SORT_INSTANT
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return _MIN_SORT_INSTANT


def _validate_rfc3339_timestamps(record: Any) -> list[str]:
    issues = []
    if isinstance(record, dict):
        for field in ("started_at", "ended_at"):
            value = record.get(field)
            if value is not None and not _is_rfc3339_datetime(value):
                issues.append(f"{field}: {value!r} is not a valid date-time")
    return issues


def _validate_telemetry_keys(value: Any, path: str) -> list[str]:
    """Reject keys that could carry transcripts, hidden reasoning, or secrets.

    The schema already makes telemetry a closed, structured object; this
    recursive check is an explicit fail-safe for helper callers and future
    schema changes that might add nested telemetry objects.
    """
    issues: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if _TELEMETRY_FORBIDDEN_KEY_RE.search(key_text):
                issues.append(f"{child_path}: telemetry keys must not contain transcript/reasoning/secret-like names")
            issues.extend(_validate_telemetry_keys(nested, child_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            issues.extend(_validate_telemetry_keys(nested, f"{path}[{index}]"))
    return issues


def _validate_route_task_result_has_no_telemetry(record: Any) -> list[str]:
    if not isinstance(record, dict) or not isinstance(record.get("route_task_result"), dict):
        return []
    forbidden = sorted(_ROUTE_RESULT_TELEMETRY_KEYS & set(record["route_task_result"]))
    if forbidden:
        return [
            "route_task_result: telemetry must be stored in provenance routing_telemetry/steps[].telemetry, "
            f"not inside the route_task result: {forbidden}"
        ]
    return []


def _validate_provenance_telemetry_safety(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return []
    issues = []
    if record.get("routing_telemetry") is not None:
        issues.extend(_validate_telemetry_keys(record.get("routing_telemetry"), "routing_telemetry"))
    for index, step in enumerate(record.get("steps", []) if isinstance(record.get("steps"), list) else []):
        if isinstance(step, dict) and step.get("telemetry") is not None:
            issues.extend(_validate_telemetry_keys(step.get("telemetry"), f"steps[{index}].telemetry"))
    return issues


def _validate_step_hierarchy(record: Any) -> list[str]:
    """`parent_task_id` (agent-tree parent) and `repair_of` (causal repair
    link) must each reference a *strictly earlier* step's `task_id` in the
    same run, or be null. This makes the agent tree acyclic and
    deterministically renderable by `_build_agent_tree` below: a step can
    only be parented to (or repair) something that was already appended,
    never itself or a step that comes after it. `task_id` values must also
    be unique within a run. Applied only at write time (`_write_validated`),
    so historical records already on disk are never retroactively rejected
    by `get_run`/trace rendering, which read without validating."""
    if not isinstance(record, dict):
        return []
    steps = record.get("steps")
    if not isinstance(steps, list):
        return []
    issues: list[str] = []
    seen: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        task_id = step.get("task_id")
        if not isinstance(task_id, str):
            continue
        if task_id in seen:
            issues.append(f"steps[{index}].task_id: duplicate task_id {task_id!r}")
        parent = step.get("parent_task_id")
        if parent is not None:
            if parent == task_id:
                issues.append(f"steps[{index}].parent_task_id: a step cannot be its own parent")
            elif parent not in seen:
                issues.append(
                    f"steps[{index}].parent_task_id: {parent!r} must reference an earlier step's task_id"
                )
        repair = step.get("repair_of")
        if repair is not None:
            if repair == task_id:
                issues.append(f"steps[{index}].repair_of: a step cannot repair itself")
            elif repair not in seen:
                issues.append(
                    f"steps[{index}].repair_of: {repair!r} must reference an earlier step's task_id"
                )
        seen.add(task_id)
    return issues


def _normalize_telemetry(telemetry: Any) -> Any:
    if telemetry is None:
        return None
    if not isinstance(telemetry, dict):
        return telemetry
    issues = _validate_telemetry_keys(telemetry, "telemetry")
    if issues:
        raise ProvenanceError("; ".join(issues))
    unknown = set(telemetry) - set(_TELEMETRY_FIELDS)
    if unknown:
        raise ProvenanceError(f"telemetry got unknown field(s): {sorted(unknown)}")
    normalized = dict(_TELEMETRY_DEFAULTS)
    normalized.update(telemetry)
    return normalized


def _normalize_orchestration_metrics(metrics: Any) -> Any:
    if metrics is None:
        return None
    if not isinstance(metrics, dict):
        return metrics

    unknown = set(metrics) - set(_ORCHESTRATION_METRIC_FIELDS)
    if unknown:
        raise ProvenanceError(f"orchestration_metrics got unknown field(s): {sorted(unknown)}")
    caller_derived = _ORCHESTRATION_DERIVED_FIELDS & set(metrics)
    if caller_derived:
        raise ProvenanceError(
            "derived orchestration metrics are computed from observed/baseline fields, "
            f"not caller-supplied: {sorted(caller_derived)}"
        )

    normalized = dict(_ORCHESTRATION_METRIC_DEFAULTS)
    normalized.update(metrics)

    total_tokens = normalized.get("total_tokens")
    estimated_direct_tokens = normalized.get("estimated_direct_tokens")
    if total_tokens is not None and estimated_direct_tokens is not None:
        normalized["coordination_tax_tokens"] = total_tokens - estimated_direct_tokens

    coordination_ms = normalized.get("coordination_ms")
    if coordination_ms is not None:
        normalized["coordination_tax_time_ms"] = coordination_ms

    delegated_quality = normalized.get("delegated_quality")
    direct_quality = normalized.get("direct_baseline_quality")
    if delegated_quality is not None and direct_quality is not None:
        normalized["quality_gain"] = delegated_quality - direct_quality

    estimated_serial_ms = normalized.get("estimated_serial_time_ms")
    wall_clock_ms = normalized.get("wall_clock_ms")
    if estimated_serial_ms is not None and wall_clock_ms not in (None, 0):
        normalized["parallel_speedup"] = estimated_serial_ms / wall_clock_ms

    return normalized


def load_schema(repo_root: Path) -> dict:
    path = repo_root / SCHEMA_RELATIVE_PATH
    with path.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def validate_run_record(record: Any, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    issues = []
    for e in sorted(validator.iter_errors(record), key=str):
        path = "/".join(str(p) for p in e.absolute_path)
        issues.append(f"{path or '<root>'}: {e.message}")
    issues.extend(_validate_rfc3339_timestamps(record))
    issues.extend(_validate_provenance_telemetry_safety(record))
    issues.extend(_validate_route_task_result_has_no_telemetry(record))
    issues.extend(_validate_step_hierarchy(record))
    issues.extend(_validate_free_text_safety(record))
    return issues


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".run.", suffix=".json.tmp", dir=str(path.parent))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        Path(tmp_name).replace(path)
    finally:
        if Path(tmp_name).exists():
            Path(tmp_name).unlink()


def _write_validated(repo_root: Path, record: dict) -> dict:
    schema = load_schema(repo_root)
    issues = validate_run_record(record, schema)
    if issues:
        raise RunSchemaValidationError(issues)
    _atomic_write_json(run_path(repo_root, record["run_id"]), record)
    return record


def start_run(
    repo_root: Path,
    *,
    run_id: str | None = None,
    backlog_id: str | None,
    github_issue: int | None,
    user_objective: str,
    route_task_result: dict | None,
    starting_commit: str | None,
    started_at: str | None = None,
    routing_telemetry: dict | None = None,
    orchestration_metrics: dict | None = None,
    github_integration: dict | None = None,
    parent_orchestration: dict | None = None,
) -> dict:
    """Create a new run record file. Fails if `run_id` already exists (runs
    are append-only artifacts; never silently overwritten)."""
    resolved_id = run_id or store.new_run_id()
    path = run_path(repo_root, resolved_id)
    if path.exists():
        raise RunAlreadyExistsError(f"run {resolved_id!r} already exists at {path}")

    record = {
        "run_id": resolved_id,
        "backlog_id": backlog_id,
        "github_issue": github_issue,
        "user_objective": user_objective,
        "started_at": started_at or store.now_iso(),
        "ended_at": None,
        "route_task_result": route_task_result,
        "routing_telemetry": _normalize_telemetry(routing_telemetry),
        "orchestration_metrics": _normalize_orchestration_metrics(orchestration_metrics),
        "starting_commit": starting_commit,
        "ending_commit": None,
        "outcome": "in_progress",
        "steps": [],
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "github_integration": github_integration,
        "parent_orchestration": parent_orchestration,
    }
    return _write_validated(repo_root, record)


def get_run(repo_root: Path, run_id: str) -> dict:
    path = run_path(repo_root, run_id)
    if not path.exists():
        raise RunNotFoundError(f"no run record for run_id {run_id!r}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def update_github_integration(repo_root: Path, run_id: str, metadata: dict) -> dict:
    record = get_run(repo_root, run_id)
    if record["outcome"] != "in_progress":
        raise ProvenanceError("cannot update a finalized run")
    record["github_integration"] = metadata
    return _write_validated(repo_root, record)


def update_parent_orchestration(repo_root: Path, run_id: str, metadata: dict,
                                *, orchestration_metrics: dict | None = None) -> dict:
    """Replace the parent metadata (callers hold `run_lock`), optionally merging observed metrics."""
    record = get_run(repo_root, run_id)
    if record["outcome"] != "in_progress":
        raise ProvenanceError("cannot update a finalized run")
    record["parent_orchestration"] = metadata
    if orchestration_metrics:
        merge_orchestration_metrics(record, orchestration_metrics)
    return _write_validated(repo_root, record)


def list_runs(repo_root: Path) -> list[dict]:
    directory = runs_dir(repo_root)
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            records.append(json.load(f))
    return sorted(records, key=lambda r: (_sort_instant(r.get("started_at")), r["run_id"]))


def summarize_token_usage(records: list[dict]) -> dict:
    """Aggregate observed lead model calls by classified task type across runs.

    Missing provider counters remain explicit; cached input is reported
    separately and must not be added to input or total tokens.
    """
    summary = {"runs": len(records), "outcomes": {}, "wall_clock_ms": 0,
               "runs_without_wall_clock": 0, "runs_without_lead_calls": 0,
               "lead_by_task_type": {}}
    for record in records:
        outcome = record.get("outcome") or "unknown"
        summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1
        wall = (record.get("orchestration_metrics") or {}).get("wall_clock_ms")
        if isinstance(wall, int) and wall >= 0:
            summary["wall_clock_ms"] += wall
        else:
            summary["runs_without_wall_clock"] += 1
        context = (record.get("parent_orchestration") or {}).get("context") or {}
        calls = context.get("lead_calls") or []
        if not calls:
            summary["runs_without_lead_calls"] += 1
        for call in calls:
            kind = call.get("task_type") or "unattributed"
            group = summary["lead_by_task_type"].setdefault(kind, {"calls": 0})
            group["calls"] += 1
            for field in ("duration_ms", "input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                value = call.get(field)
                group.setdefault(field, 0)
                group.setdefault(f"missing_{field}_calls", 0)
                if isinstance(value, int) and value >= 0:
                    group[field] += value
                else:
                    group[f"missing_{field}_calls"] += 1
    summary["lead_by_task_type"] = dict(sorted(summary["lead_by_task_type"].items()))
    return summary


def runs_for_backlog(repo_root: Path, backlog_id: str) -> list[dict]:
    return [r for r in list_runs(repo_root) if r.get("backlog_id") == backlog_id]


def add_step(repo_root: Path, run_id: str, **fields: Any) -> dict:
    """Append one step to an existing run record. Unspecified attribution
    fields default to explicit `None` (never a guessed value) except
    `status`, which defaults to 'pending'. Required: `task_id`."""
    if "task_id" not in fields or not fields["task_id"]:
        raise ProvenanceError("add_step requires a non-empty 'task_id'")
    unknown = set(fields) - set(_STEP_FIELDS)
    forbidden_private = {"reasoning", "transcript", "hidden_reasoning"} & set(fields)
    if forbidden_private:
        raise ProvenanceError(f"private/free-form reasoning or transcripts must not be persisted: {sorted(forbidden_private)}")
    if unknown:
        raise ProvenanceError(f"add_step got unknown step field(s): {sorted(unknown)}")
    if "telemetry" in fields:
        fields["telemetry"] = _normalize_telemetry(fields["telemetry"])

    with run_lock(repo_root, run_id):
        record = get_run(repo_root, run_id)
        if record.get("outcome") != "in_progress":
            raise ProvenanceError(f"run {run_id!r} is finalized; cannot add steps")
        step = dict(_STEP_DEFAULTS)
        step.update(fields)
        record["steps"].append(step)
        return _write_validated(repo_root, record)


def update_step(repo_root: Path, run_id: str, task_id: str, **fields: Any) -> dict:
    """Update the most recently appended step matching `task_id` (steps are
    append-ordered; if a task_id was repaired/re-run, a new step should
    usually be appended instead, but in-place status/outcome updates on the
    same step are supported for straightforward pending -> completed/failed
    transitions)."""
    unknown = set(fields) - set(_STEP_FIELDS)
    forbidden_private = {"reasoning", "transcript", "hidden_reasoning"} & set(fields)
    if forbidden_private:
        raise ProvenanceError(f"private/free-form reasoning or transcripts must not be persisted: {sorted(forbidden_private)}")
    if unknown:
        raise ProvenanceError(f"update_step got unknown step field(s): {sorted(unknown)}")
    if "telemetry" in fields:
        fields["telemetry"] = _normalize_telemetry(fields["telemetry"])

    with run_lock(repo_root, run_id):
        record = get_run(repo_root, run_id)
        if record.get("outcome") != "in_progress":
            raise ProvenanceError(f"run {run_id!r} is finalized; cannot update steps")
        matches = [i for i, s in enumerate(record["steps"]) if s["task_id"] == task_id]
        if not matches:
            raise ProvenanceError(f"run {run_id!r} has no step with task_id {task_id!r}")
        idx = matches[-1]
        if "status" in fields:
            current_status = record["steps"][idx]["status"]
            new_status = fields["status"]
            if new_status != current_status and new_status not in STEP_TRANSITIONS[current_status]:
                raise ProvenanceError(f"illegal step status transition {current_status!r} -> {new_status!r}")
        record["steps"][idx].update(fields)
        return _write_validated(repo_root, record)


def update_orchestration_metrics(repo_root: Path, run_id: str, **fields: Any) -> dict:
    """Merge observed run-level delegation metrics and recompute derived values.

    Metrics are observational only: this function never changes routing,
    execution plans, capacity assignments, permissions, or active delegation
    policy. Unknown/unobserved values remain explicit null.
    """
    unknown = set(fields) - (set(_ORCHESTRATION_METRIC_FIELDS) - _ORCHESTRATION_DERIVED_FIELDS)
    if unknown:
        raise ProvenanceError(f"update_orchestration_metrics got unknown field(s): {sorted(unknown)}")
    with run_lock(repo_root, run_id):
        record = get_run(repo_root, run_id)
        merge_orchestration_metrics(record, fields)
        return _write_validated(repo_root, record)


def merge_orchestration_metrics(record: dict, fields: dict) -> dict:
    """Merge observed metric fields into `record` in memory and recompute derived values."""
    current = record.get("orchestration_metrics")
    base = {} if current is None else {
        key: value
        for key, value in current.items()
        if key not in _ORCHESTRATION_DERIVED_FIELDS and key != "schema_version"
    }
    base.update(fields)
    record["orchestration_metrics"] = _normalize_orchestration_metrics(base)
    return record


def finish_run(
    repo_root: Path,
    run_id: str,
    *,
    outcome: str,
    ending_commit: str | None = None,
    ended_at: str | None = None,
    github_integration: dict | None = None,
    parent_orchestration: dict | None = None,
    orchestration_metrics: dict | None = None,
) -> dict:
    if outcome not in TERMINAL_RUN_OUTCOMES:
        raise ProvenanceError(f"unknown terminal outcome {outcome!r}; must be one of {TERMINAL_RUN_OUTCOMES}")
    record = get_run(repo_root, run_id)
    if github_integration is not None:
        record["github_integration"] = github_integration
    if parent_orchestration is not None:
        record["parent_orchestration"] = parent_orchestration
    if orchestration_metrics:
        merge_orchestration_metrics(record, orchestration_metrics)
    if record.get("outcome") != "in_progress" or record.get("ended_at") is not None:
        raise ProvenanceError(f"run {run_id!r} is already finalized")
    record["outcome"] = outcome
    record["ending_commit"] = ending_commit
    record["ended_at"] = ended_at or store.now_iso()
    return _write_validated(repo_root, record)


# ---------------------------------------------------------------------------
# Trace: deterministic, read-only selection/derivation/rendering of a run's
# agentic organization -- how the orchestrator decided, which agents/
# subagents ran, what they returned, and the final outcome. Every field
# comes from the persisted run record verbatim; nothing here re-evaluates
# routing, invents attribution, or estimates unobserved metrics.
# ---------------------------------------------------------------------------

def select_trace_runs(
    repo_root: Path,
    *,
    run_id: str | None = None,
    backlog_id: str | None = None,
    latest: bool = False,
) -> list[dict]:
    """Select run record(s) to trace. `run_id` is mutually exclusive with
    `backlog_id`/`latest`. With `run_id`, returns exactly that run (raises
    RunNotFoundError if missing). Otherwise selects from `runs_for_backlog`
    (if `backlog_id` given) or `list_runs` (all runs), both already sorted
    ascending by `(started_at, run_id)`; `latest=True` then narrows to the
    single most recent entry (deterministic tie-break: `run_id`). Returns an
    empty list, never an error, when a backlog/latest selector matches no
    runs -- callers decide how to report "no matching run"."""
    if run_id is not None and (backlog_id is not None or latest):
        raise ProvenanceError("run_id is mutually exclusive with backlog_id/latest")
    if run_id is not None:
        return [get_run(repo_root, run_id)]
    runs = runs_for_backlog(repo_root, backlog_id) if backlog_id is not None else list_runs(repo_root)
    if not runs:
        return []
    return [runs[-1]] if latest else runs


def _normalize_validation_entry(entry: Any) -> dict:
    if isinstance(entry, str):
        return {"command": entry, "result": "unknown", "exit_code": None, "summary": None}
    if isinstance(entry, dict):
        return entry
    return {"command": "unknown", "result": "unknown", "exit_code": None, "summary": None}


def _build_agent_tree(steps: list) -> tuple[list[dict], list[str]]:
    """Derive the agent/subagent hierarchy solely from `task_id`/
    `parent_task_id`, in append order (never inferred/guessed).

    A fresh write via `add_step`/`_validate_step_hierarchy` already
    guarantees unique `task_id`s and parents that reference a *strictly
    earlier* step, which makes the tree acyclic by construction. This
    function must still tolerate a historical record written before that
    guard existed (or any other externally-edited record), where those
    invariants can be violated in three ways -- and must never silently
    drop a step, mis-attach it, or attach it to the wrong node as a result:

      - a duplicate `task_id`: the *first* occurrence remains the canonical
        node, but as soon as a duplicate is seen that `task_id` becomes
        permanently ambiguous -- every occurrence from the duplicate onward
        (including the first, for any *subsequent* step naming it as parent)
        is rendered as its own separate root, since which of the two or more
        same-id steps a later child actually meant can no longer be
        determined. A child that named this id as `parent_task_id` *before*
        the duplicate appeared keeps its original (unambiguous, at the time)
        attachment to the first occurrence -- append order is never
        rewritten retroactively;
      - a forward reference (parent appears later in the list) or a
        reference to a `task_id` that never appears at all: rendered as its
        own root, because at the point this step was appended that parent
        was not yet an established, unambiguous node;
      - a cycle (A's parent is B, B's parent is A): broken deterministically
        by the same "parent must already be an established, unambiguous
        node" rule above -- the first step in append order to name the other
        as its parent loses (rendered as a root with a warning) and the
        later one attaches normally, so the result is always a tree, never a
        cycle.

    Every anomaly is reported in the returned warnings list rather than
    silently discarded."""
    established: set[str] = set()
    ambiguous: set[str] = set()
    canonical_step_by_id: dict[str, dict] = {}
    children: dict[str, list[str]] = {}
    root_keys: list[str] = []
    node_step_by_key: dict[str, dict] = {}
    warnings: list[str] = []
    duplicate_counter = 0

    for step in steps:
        if not isinstance(step, dict):
            continue
        task_id = step.get("task_id")
        if not isinstance(task_id, str):
            continue

        if task_id in canonical_step_by_id:
            duplicate_counter += 1
            dedup_key = f"{task_id}\0dup{duplicate_counter}"
            node_step_by_key[dedup_key] = step
            root_keys.append(dedup_key)
            ambiguous.add(task_id)
            warnings.append(
                f"step {task_id!r} appears more than once; the later occurrence is rendered as its own root, "
                f"and {task_id!r} is no longer a valid parent target for any subsequent step"
            )
            continue

        canonical_step_by_id[task_id] = step
        node_step_by_key[task_id] = step
        parent = step.get("parent_task_id")
        if parent is None:
            root_keys.append(task_id)
        elif parent == task_id:
            warnings.append(f"step {task_id!r} references itself as parent_task_id; rendered as a root")
            root_keys.append(task_id)
        elif parent in ambiguous:
            warnings.append(
                f"step {task_id!r} references parent_task_id {parent!r}, which is ambiguous "
                "(that task_id appears more than once); rendered as a root"
            )
            root_keys.append(task_id)
        elif parent in established:
            children.setdefault(parent, []).append(task_id)
        else:
            warnings.append(
                f"step {task_id!r} references parent_task_id {parent!r}, which is not an earlier, "
                "uniquely-identified step; rendered as a root"
            )
            root_keys.append(task_id)
        established.add(task_id)

    def node(key: str) -> dict:
        step = node_step_by_key[key]
        # `children` is keyed by the canonical `task_id` (the only thing a
        # later step's `parent_task_id` can reference); looking children up
        # by `key` rather than by the step's displayed `task_id` matters
        # only for a *duplicate* occurrence, whose key differs from the
        # canonical key even though its displayed task_id is the same
        # string -- without this, a duplicate would incorrectly inherit the
        # canonical node's children too.
        return {
            "task_id": step.get("task_id"),
            "role": step.get("role"),
            "family": step.get("family"),
            "model": step.get("model"),
            "worker_identifier": step.get("worker_identifier"),
            "status": step.get("status"),
            "outcome": step.get("outcome"),
            "repair_of": step.get("repair_of"),
            "telemetry": step.get("telemetry"),
            "children": [node(child_key) for child_key in children.get(key, [])],
        }

    return [node(key) for key in root_keys], warnings


def build_trace_report(repo_root: Path, record: dict) -> dict:
    """Derive the deterministic, human/machine-readable trace report for one
    run record. Read-only: performs no network access, no `gh` calls, no
    writes, and never re-evaluates routing. `execution_plan` is copied from
    the run's stored `route_task_result` verbatim; `route` is a curated
    subset of that same stored result's fields (`ok`, `policy_version`,
    `selected_route`, `fallback_applied`, base/effective `matches`,
    `classification`, `capacity_assignments`, `backend_bindings`,
    `project_constraints`, `path_floor_matches`, `errors`) -- every value is copied unmodified,
    but fields with no safe/relevant trace meaning are intentionally
    dropped rather than exposing the raw result shape. Safe to hand to a
    user: attribution/telemetry fields are closed, `additionalProperties:
    false` structures that reject forbidden secret/transcript-like *keys*
    (see module docstring and `_validate_telemetry_keys`), and the specific
    free-text fields a worker actually controls the content of
    (`validation_performed`/`returned_artifacts` commands/summaries/
    references) are additionally validated at write time for compact
    single-line content and secret-like *values* (see
    `_validate_free_text_safety`); `render_trace_text` also defensively
    sanitizes any stray control/newline character from a historical record
    that predates that check."""
    steps = record.get("steps") if isinstance(record.get("steps"), list) else []
    route_result = record.get("route_task_result")

    route_summary = None
    execution_plan = None
    if isinstance(route_result, dict):
        matches = route_result.get("matches") if isinstance(route_result.get("matches"), dict) else {}
        # Every field here is copied from the stored `route_task_result`
        # verbatim (never re-evaluated): `classification`/`capacity_assignments`/
        # `backend_bindings`/`project_constraints` are all already part of
        # that safe, non-secret result (see schemas/execution-plan.schema.json
        # and README's "route_task input/output" contract), and `errors` is
        # the only content present when `ok` is False. Omitting any of these
        # would silently hide the actual routing/capacity decision (e.g. an
        # authored `standard` step that was actually assigned `economy`).
        route_summary = {
            "ok": route_result.get("ok"),
            "policy_version": route_result.get("policy_version"),
            "selected_route": route_result.get("selected_route"),
            "fallback_applied": route_result.get("fallback_applied"),
            "base_matches": matches.get("base"),
            "effective_matches": matches.get("effective"),
            "classification": route_result.get("classification"),
            "capacity_assignments": route_result.get("capacity_assignments"),
            "backend_bindings": route_result.get("backend_bindings"),
            "project_constraints": route_result.get("project_constraints"),
            "path_floor_matches": route_result.get("path_floor_matches"),
            "errors": route_result.get("errors"),
        }
        execution_plan = route_result.get("execution_plan")

    agents, warnings = _build_agent_tree(steps)

    step_summaries = []
    artifacts = []
    validations = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        task_id = step.get("task_id")
        step_summaries.append({
            "task_id": task_id,
            "parent_task_id": step.get("parent_task_id"),
            "execution_plan_step_id": step.get("execution_plan_step_id"),
            "role": step.get("role"),
            "family": step.get("family"),
            "backend": step.get("backend"),
            "model": step.get("model"),
            "capacity_tier": step.get("capacity_tier"),
            "reasoning_setting": step.get("reasoning_setting"),
            "worker_identifier": step.get("worker_identifier"),
            "status": step.get("status"),
            "outcome": step.get("outcome"),
            "files_changed": step.get("files_changed"),
            "repair_of": step.get("repair_of"),
            "review_findings": step.get("review_findings"),
            "telemetry": step.get("telemetry"),
        })
        for artifact in (step.get("returned_artifacts") or []):
            if isinstance(artifact, dict):
                artifacts.append({"task_id": task_id, **artifact})
        for entry in (step.get("validation_performed") or []):
            validations.append({"task_id": task_id, **_normalize_validation_entry(entry)})

    run_id = record.get("run_id")
    try:
        resolved_trace = run_path(repo_root, run_id) if run_id else None
        if resolved_trace is None:
            trace_path = None
        else:
            try:
                trace_path = str(resolved_trace.relative_to(repo_root.resolve()))
            except ValueError:
                trace_path = str(resolved_trace)
    except ProvenanceError:
        trace_path = None

    return {
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "run_id": run_id,
        "trace_path": trace_path,
        "backlog_id": record.get("backlog_id"),
        "github_issue": record.get("github_issue"),
        "objective": record.get("user_objective"),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "starting_commit": record.get("starting_commit"),
        "ending_commit": record.get("ending_commit"),
        "outcome": record.get("outcome"),
        "parent_orchestration": record.get("parent_orchestration"),
        "route": route_summary,
        "execution_plan": execution_plan,
        "agents": agents,
        "steps": step_summaries,
        "returned_artifacts": artifacts,
        "validation_results": validations,
        "routing_telemetry": record.get("routing_telemetry"),
        "orchestration_metrics": record.get("orchestration_metrics"),
        "warnings": warnings,
    }


def _sanitize_display(value: str) -> str:
    """Defense in depth for rendering: `_validate_free_text_safety` rejects
    multi-line/control-character content at write time, but a historical
    record written before that guard existed (or read directly, since
    `get_run`/trace rendering never validate on read) could still contain
    it. Collapse any such character rather than emit it verbatim, so a
    stored value can never inject a fake section header or otherwise
    corrupt this function's fixed-section plain-text layout."""
    return _MULTILINE_OR_CONTROL_RE.sub(
        lambda m: {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(m.group(0), " "), value
    )


def _sanitize_nested(value: Any) -> Any:
    """Recursively apply `_sanitize_display` to every string inside a
    nested JSON-like structure (dict/list), so a raw value embedded in the
    text trace -- `classification`, `project_constraints`, capacity
    assignment evidence, etc. -- can never inject a fake section header via a
    stray control/newline character, even if it originates from an
    unvalidated/historical `route_task_result` that predates
    `_validate_free_text_safety`."""
    if isinstance(value, str):
        return _sanitize_display(value)
    if isinstance(value, dict):
        return {str(k): _sanitize_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_nested(v) for v in value]
    return value


def _fmt_telemetry_summary(telemetry: Any) -> str:
    """Concise one-line rendering of an `observational_telemetry` object:
    source, observed timing, and observed token counts. Every unobserved
    field renders as `unknown`, and a missing/non-dict telemetry object (no
    observation was ever recorded for this step) renders as `unknown`
    outright, never fabricated."""
    if not isinstance(telemetry, dict):
        return "unknown"
    source = _fmt(telemetry.get("source"))
    duration_ms = _fmt(telemetry.get("duration_ms"))
    input_tokens = _fmt(telemetry.get("input_tokens"))
    output_tokens = _fmt(telemetry.get("output_tokens"))
    cached_tokens = _fmt(telemetry.get("cached_tokens"))
    total_tokens = _fmt(telemetry.get("total_tokens"))
    return (
        f"source={source} duration_ms={duration_ms} "
        f"tokens(in/out/cached/total)={input_tokens}/{output_tokens}/{cached_tokens}/{total_tokens}"
    )


def _fmt(value: Any) -> Any:
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return _sanitize_display(value)
    if isinstance(value, (dict, list)):
        return _sanitize_nested(value)
    return value


# Concise, risk-relevant subset of a full task classification -- enough to
# explain *why* a route/capacity decision was made without reprinting every
# field of schemas/task.schema.json inline in the text trace (the complete
# classification remains available verbatim in the JSON trace/`--json`).
_CLASSIFICATION_SUMMARY_FIELDS = (
    "task_type",
    "failure_cost",
    "semantic_correctness",
    "ambiguity",
    "implementation_scope",
    "behavior_preservation",
    "diagnosis_required",
    "assumptions_must_be_explicit",
    "security_sensitive",
    "destructive_operation",
)


def _path_floor_lines(matches: Any, indent: str, label: str) -> list[str]:
    """One line per matched `.pi/routing.yaml` path floor: rule index, the
    globs that matched, the floors, and the matched paths (bounded, as
    recorded, with the exact count)."""
    if not isinstance(matches, list) or not matches:
        return []
    lines = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        paths = match.get("matched_paths") if isinstance(match.get("matched_paths"), list) else []
        shown = [str(path) for path in paths[:10]]
        count = match.get("matched_path_count")
        more = (count if isinstance(count, int) else len(paths)) - len(shown)
        lines.append(
            f"{indent}{label}: rule {_fmt(match.get('rule'))} {_fmt(match.get('patterns'))} "
            f"floors {_fmt(match.get('floors'))} via {_fmt(', '.join(shown))}"
            + (f" (+{more} more)" if more > 0 else "")
        )
    return lines


def _classification_summary(classification: Any) -> Any:
    if not isinstance(classification, dict):
        return None
    return {field: classification.get(field) for field in _CLASSIFICATION_SUMMARY_FIELDS if field in classification}


def _resolve_step_model(family: Any, assigned_tier: Any, backend_bindings: Any) -> Any:
    if not isinstance(family, str) or not isinstance(assigned_tier, str) or not isinstance(backend_bindings, dict):
        return None
    binding = backend_bindings.get(family)
    capacity_models = binding.get("capacity_models") if isinstance(binding, dict) else None
    tier_info = capacity_models.get(assigned_tier) if isinstance(capacity_models, dict) else None
    return tier_info.get("model") if isinstance(tier_info, dict) else None


def _resolve_backend_tool(family: Any, backend_bindings: Any) -> Any:
    if not isinstance(family, str) or not isinstance(backend_bindings, dict):
        return None
    binding = backend_bindings.get(family)
    return binding.get("tool") if isinstance(binding, dict) else None


def render_trace_text(report: dict) -> str:
    """Render `build_trace_report`'s output as fixed-section, deterministic
    plain text (no wall-clock-dependent content, so identical reports always
    render identically). Every unavailable value prints as the literal
    string `unknown`; an empty/absent collection prints as `(none observed)`
    or `(none)`, never a blank section."""
    lines: list[str] = []
    lines.append(f"Run: {report.get('run_id')}")
    lines.append(f"Trace: {_fmt(report.get('trace_path'))}")
    lines.append(f"Status: {report.get('outcome')}")
    backlog_ref = _fmt(report.get("backlog_id"))
    github_issue = report.get("github_issue")
    issue_ref = f"#{github_issue}" if github_issue is not None else "unknown"
    lines.append(f"References: backlog {backlog_ref}; GitHub issue {issue_ref}")
    lines.append(f"Objective: {_fmt(report.get('objective'))}")
    lines.append("")

    parent = report.get("parent_orchestration")
    if isinstance(parent, dict):
        lines.append("Parent orchestration:")
        lines.append(f"  Target repository: {_fmt(parent.get('target_repository'))}")
        lines.append(f"  Policy version: {_fmt(parent.get('policy_version'))}")
        lines.append(f"  Final status: {_fmt(parent.get('final_status'))}")
        lines.append(f"  Blocker: {_fmt(parent.get('blocker_code'))}")
        for phase in parent.get("phases") or []:
            lines.append(f"  Phase {_fmt(phase.get('id'))}: {_fmt(phase.get('status'))}; route {_fmt(phase.get('route'))}; blocker {_fmt(phase.get('blocker_code'))}")
            routing = phase.get("routing") if isinstance(phase.get("routing"), dict) else {}
            lines.extend(_path_floor_lines(routing.get("path_floor_matches"), "    ", "Path floor (files_or_areas)"))
            for escalation in phase.get("escalations") or []:
                if isinstance(escalation, dict) and escalation.get("reason") == "PATH_FLOOR":
                    lines.append(f"    Re-route (PATH_FLOOR): {_fmt(escalation.get('from_route'))} -> {_fmt(escalation.get('to_route'))}; confirmed {_fmt(escalation.get('confirmed'))}")
            for check in phase.get("diff_floor_checks") or []:
                if not isinstance(check, dict):
                    continue
                outcome = ("underrated (CLASSIFICATION_UNDERRATED_BY_DIFF) -> " + str(_fmt(check.get("to_route")))
                           if check.get("status") == "underrated" else _fmt(check.get("status")))
                lines.append(f"    Diff floor check (route epoch {_fmt(check.get('route_epoch'))}, route {_fmt(check.get('route'))}): {outcome}")
                lines.extend(_path_floor_lines(check.get("matches"), "      ", "Path floor (diff)"))
            for worker in phase.get("workers") or []:
                lines.append(f"    Worker {_fmt(worker.get('step_id'))}: {_fmt(worker.get('status'))}; model {_fmt(worker.get('model'))}; backend model {_fmt(worker.get('backend_model'))}; blocker {_fmt(worker.get('blocker_code'))}; backend {_fmt(worker.get('installed_backend_version'))} (required {_fmt(worker.get('required_backend_version'))})")
            for child_id in phase.get("child_run_ids") or []:
                lines.append(f"    Child run: {_fmt(child_id)}")
        mutations = parent.get("mutations") or {}
        lines.append(f"  Mutations: GitHub {_fmt(mutations.get('github'))}; code {_fmt(mutations.get('code'))}")
        lines.append("")

    lines.append("Route:")
    route = report.get("route")
    routed_phases = [ph for ph in ((parent or {}).get("phases") or []) if isinstance(ph, dict) and ph.get("route")] \
        if isinstance(parent, dict) else []
    if route is None and routed_phases:
        lines.append("  Routed per phase: " + ", ".join(f"{_fmt(ph.get('id'))} {_fmt(ph.get('route'))}" for ph in routed_phases))
    elif route is None:
        lines.append("  (no route_task_result recorded)")
    elif route.get("ok") is False:
        lines.append("  Route selection failed (ok: false)")
        errors = route.get("errors") or []
        if errors:
            for err in errors:
                if isinstance(err, dict):
                    lines.append(f"    - {_fmt(err.get('path'))}: {_fmt(err.get('message'))}")
                else:
                    lines.append(f"    - {_fmt(err)}")
        else:
            lines.append("    (no error detail recorded)")
    else:
        lines.append(f"  Selected: {_fmt(route.get('selected_route'))}")
        lines.append(f"  Policy version: {_fmt(route.get('policy_version'))}")
        lines.append(f"  Fallback applied: {_fmt(route.get('fallback_applied'))}")
        lines.append(f"  Matches (base): {_fmt(route.get('base_matches'))}")
        lines.append(f"  Matches (effective): {_fmt(route.get('effective_matches'))}")
        classification = route.get("classification")
        base_summary = _classification_summary(classification.get("base")) if isinstance(classification, dict) else None
        effective_summary = _classification_summary(classification.get("effective")) if isinstance(classification, dict) else None
        lines.append(f"  Classification (base): {_fmt(base_summary)}")
        lines.append(f"  Classification (effective): {_fmt(effective_summary)}")
        if route.get("project_constraints"):
            lines.append(f"  Project constraints: {_fmt(route.get('project_constraints'))}")
        lines.extend(_path_floor_lines(route.get("path_floor_matches"), "  ", "Path floor (files_or_areas)"))
    lines.append("")

    lines.append("Execution plan:")
    plan = report.get("execution_plan")
    plan_steps = plan.get("steps") if isinstance(plan, dict) else None
    capacity_assignments = route.get("capacity_assignments") if isinstance(route, dict) else None
    capacity_by_step = {
        a["step_id"]: a
        for a in (capacity_assignments if isinstance(capacity_assignments, list) else [])
        if isinstance(a, dict) and isinstance(a.get("step_id"), str)
    }
    backend_bindings = route.get("backend_bindings") if isinstance(route, dict) else None
    if plan_steps:
        for plan_step in plan_steps:
            step_id = plan_step.get("id")
            family = plan_step.get("family")
            authored_tier = plan_step.get("capacity_tier")
            assignment = capacity_by_step.get(step_id)
            # Show both the authored capacity tier and the actually
            # *assigned* one (post-route, from capacity_assignments), plus
            # which rule (if any) made that decision -- a rule such as
            # low-risk-worker can downgrade an authored `standard`
            # step to `economy`, and reporting only one value here would
            # either hide that decision or misrepresent which model ran.
            assigned_tier = assignment.get("assigned_capacity_tier") if assignment else authored_tier
            rule = assignment.get("rule") if isinstance(assignment, dict) else None
            selected_profile = assignment.get("selected_model_profile") if isinstance(assignment, dict) else None
            model = selected_profile.get("model") if isinstance(selected_profile, dict) else None
            context_window_tier = selected_profile.get("context_window_tier") if isinstance(selected_profile, dict) else None
            if model is None:
                model = _resolve_step_model(family, assigned_tier, backend_bindings)
            backend_tool = _resolve_backend_tool(family, backend_bindings)
            lines.append(
                f"  {_fmt(plan_step.get('order'))}. {_fmt(step_id)} - "
                f"{_fmt(plan_step.get('role'))}/{_fmt(family)} "
                f"intent={_fmt(plan_step.get('intent'))} "
                f"capacity={_fmt(authored_tier)}->{_fmt(assigned_tier)} rule={_fmt(rule)} "
                f"reasoning={_fmt(plan_step.get('reasoning_effort'))} "
                f"backend={_fmt(backend_tool)} model={_fmt(model)} context_window={_fmt(context_window_tier)}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Agents:")
    agents = report.get("agents") or []
    if agents:
        def render_node(node: dict, depth: int) -> None:
            indent = "  " * (depth + 1)
            lines.append(
                f"{indent}- {_fmt(node.get('task_id'))} [{_fmt(node.get('role'))}/{_fmt(node.get('family'))}] "
                f"status={_fmt(node.get('status'))} outcome={_fmt(node.get('outcome'))}"
            )
            lines.append(f"{indent}  telemetry: {_fmt_telemetry_summary(node.get('telemetry'))}")
            for child in node.get("children", []):
                render_node(child, depth + 1)

        for root in agents:
            render_node(root, 0)
    else:
        lines.append("  (none observed)")
    lines.append("")

    lines.append("Artifacts:")
    artifacts = report.get("returned_artifacts") or []
    if artifacts:
        for artifact in artifacts:
            lines.append(
                f"  - {_fmt(artifact.get('task_id'))} / {artifact.get('kind')}: {_fmt(artifact.get('summary'))} "
                f"({_fmt(artifact.get('reference'))})"
            )
    else:
        lines.append("  (none observed)")
    lines.append("")

    lines.append("Validation:")
    validations = report.get("validation_results") or []
    if validations:
        for v in validations:
            result = str(v.get("result") or "unknown").upper()
            lines.append(
                f"  - {_fmt(v.get('task_id'))}: {result} [exit {_fmt(v.get('exit_code'))}] "
                f"`{_fmt(v.get('command'))}` - {_fmt(v.get('summary'))}"
            )
    else:
        lines.append("  (none observed)")
    lines.append("")

    lines.append("Telemetry:")
    lines.append(f"  Routing: {_fmt_telemetry_summary(report.get('routing_telemetry'))}")
    lines.append(f"  Run metrics: {_fmt(report.get('orchestration_metrics'))}")
    lines.append("")

    warnings = report.get("warnings") or []
    if warnings:
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  - {_fmt(w)}")
    else:
        lines.append("Warnings: (none)")
    lines.append("")

    lines.append(f"Final status: {report.get('outcome')}")
    return "\n".join(lines)
