#!/usr/bin/env python3
"""Repository-scoped child launcher for a workspace execution plan.

One workspace task maps to one headless execution (tools/headless_run.py)
bound to exactly one repository entry of the manifest. The coordinator decides
which child orchestrators may exist; each child is an ordinary parent
orchestration run with its own single-valued target_repository, worktrees,
state and GitHub identity, so a worker still cannot act on another repository
by changing its directory or remote.

Execution identifiers are stable (`<workspace-id>--<task-id>`), so relaunching
after a crash reconciles against the persisted execution instead of starting a
duplicate child. Child run ids are linked into the workspace record at
`<state>/workspaces/<workspace-id>.json`; no prompt, transcript or secret
crosses that boundary.

Before launching, the task is checked against the read-only plan: its
repository must resolve without blockers, the checkout HEAD must be the pinned
commit (the coordinator never checks anything out), and the task must be
dispatchable (nothing in `blocked_by`). Ordering across tasks and concurrency
limits belong to the scheduler. When a child would really start, the task's
mutation authorization is checked too (tools/workspace_authorization.py), so
an unauthorized task is refused before it spends a token; `preflight` reports
every task's verdict and the approval steps without launching anything.

A task whose run stopped to ask for mutation approval, without changing
anything, is retried once its authorization changed, as a new attempt with
its own execution id (`<workspace-id>--<task-id>--attempt-<n>`). The earlier
attempt's execution and run records stay as they are; the workspace entry
keeps its summary under `attempts`.

Exit codes: 0 ok, 1 blocked or refused, 2 invalid invocation.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402
import headless_run  # noqa: E402
import usage  # noqa: E402
import workspace_authorization  # noqa: E402
import workspace_plan  # noqa: E402

RECORD_VERSION = 1
ATTEMPT_SEPARATOR = "--attempt-"


class WorkspaceRunError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execution_id(workspace_id: str, task_id: str, attempt: int = 1) -> str:
    """`<workspace>--<task>` for the first attempt, `<workspace>--<task>--attempt-<n>` for later ones."""
    base = f"{workspace_id}--{task_id}"
    return base if attempt == 1 else f"{base}{ATTEMPT_SEPARATOR}{attempt}"


def current_execution_id(workspace_id: str, task_id: str, entry: dict | None) -> str:
    """The execution id of a task's current attempt, as its workspace entry records it.

    Only an id this module derives for that task is accepted; anything else falls back to the
    first attempt's id, so a hand-edited record cannot point a task at another execution.
    """
    attempt = (entry or {}).get("attempt") if isinstance(entry, dict) else None
    if type(attempt) is int and attempt > 1 and entry.get("execution_id") == execution_id(workspace_id, task_id, attempt):
        return entry["execution_id"]
    return execution_id(workspace_id, task_id)


def _record_entries(workspace_id: str, record: dict | None = None) -> dict:
    if record is None:
        record = load_workspace(workspace_record_path(workspace_id)) or {}
    tasks = record.get("tasks")
    return tasks if isinstance(tasks, dict) else {}


def workspace_record_path(workspace_id: str) -> Path:
    return headless_run.state_root() / "workspaces" / f"{workspace_id}.json"


def load_workspace(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceRunError("WORKSPACE_RECORD_UNREADABLE", str(exc)) from exc
    if not isinstance(data, dict) or data.get("record_version") != RECORD_VERSION:
        raise WorkspaceRunError("WORKSPACE_RECORD_UNREADABLE", "unsupported workspace record")
    return data


def save_workspace(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = _now()
    atomic_io.atomic_write_json(path, record, indent=2, sort_keys=True)


@contextlib.contextmanager
def locked_workspace(workspace_id: str):
    """Exclusive read-modify-write of the workspace record across threads and processes."""
    path = workspace_record_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".json.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _head(checkout: Path) -> str | None:
    try:
        probe = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], shell=False,
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return probe.stdout.strip() if probe.returncode == 0 else None


WORKSPACE_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


EVIDENCE_CHANGED = "WORKSPACE_TASK_DEFINITION_CHANGED"


def evidence_bindings(manifest: Path) -> dict[str, str]:
    """Bind evidence to task semantics and repository scope, without storing instructions.

    Pins and runtime limits can change during policy approval and resume. Evidence still
    names its exact validated commit; changing a task, its checks, or its upstream
    definitions requires a new task id. Publication wording is separately reviewed.
    """
    raw = workspace_plan.load_manifest(manifest)
    errors = workspace_plan.validate_manifest(raw)
    if errors:
        raise workspace_plan.WorkspaceError("invalid workspace manifest: " + "; ".join(errors))
    repos = {r["id"]: {"repository": r["repository"].casefold(),
                       "path": str((manifest.resolve().parent / r["path"]).resolve()),
                       "base_branch": r.get("base_branch", "main")} for r in raw["repositories"]}
    definitions = {}
    for task in raw["tasks"]:
        kind = task.get("kind", "implementation")
        checks = [{**c, "timeout_seconds": c.get("timeout_seconds", 600)} for c in task.get("checks", [])]
        scope = {task["repository"], *(c["repository"] for c in checks)}
        definitions[task["id"]] = {
            "repository": task["repository"], "repositories": {r: repos.get(r) for r in sorted(scope)},
            "instruction": task.get("objective") or task["title"], "kind": kind,
            "mutation": "none" if kind == "qualification" else task.get("mutation", "local"),
            "depends_on": sorted(task.get("depends_on", [])), "checks": checks,
            "provides": sorted(task.get("provides", []), key=lambda c: c["contract"]),
            "requires": sorted(task.get("requires", []), key=lambda c: c["contract"])}
    bindings = {}
    for task_id in definitions:
        ancestors, pending = {}, [task_id]
        while pending:
            current = pending.pop()
            if current in ancestors or current not in definitions:
                continue
            ancestors[current] = definitions[current]
            pending.extend(definitions[current]["depends_on"])
        bindings[task_id] = hashlib.sha256(json.dumps(ancestors, sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest()
    return bindings


def _incompatible_entries(record: dict, manifest: Path, bindings: dict | None = None) -> set[str]:
    bindings = evidence_bindings(manifest) if bindings is None else bindings
    saved = record.get("evidence_bindings") or {}
    # Legacy evidence may be adopted only while its original manifest is unchanged.
    legacy_matches = (record.get("manifest") == str(manifest.resolve()) and
                      record.get("manifest_digest") == hashlib.sha256(manifest.read_bytes()).hexdigest())
    incompatible = {tid for tid in _record_entries("", record) if tid in bindings and
                    (saved.get(tid) != bindings[tid] if tid in saved else not legacy_matches)}
    incompatible.update(tid for tid in _record_entries("", record) if tid in bindings and tid in saved
                        and tid not in (record.get("evidence_pins") or {}) and not legacy_matches)
    raw = workspace_plan.load_manifest(manifest)
    repos = {r["id"]: r for r in raw["repositories"]}
    tasks = {t["id"]: t for t in raw["tasks"]}
    for tid, pins in (record.get("evidence_pins") or {}).items():
        if tid not in bindings or tid not in _record_entries("", record) or tid in incompatible:
            continue
        for repo_id, old_pin in pins.items():
            repo = repos.get(repo_id)
            if repo is None or repo["pinned_commit"] == old_pin:
                continue
            checkout = (manifest.resolve().parent / repo["path"]).resolve()
            # Policy approval legitimately advances the base while preserving implementation
            # artifacts. Qualification checks may inspect policy too, so must run again.
            entry = _record_entries("", record)[tid]
            observed = entry.get("observed") or {}
            commits = []
            if tasks[tid]["repository"] == repo_id and (observed.get("validation") or {}).get("status") == "passed":
                commits.append(observed.get("validated_commit"))
            commits.extend(u.get("validated_commit") for u in (entry.get("inputs") or {}).get("upstream", [])
                           if u.get("repository", "").casefold() == repo["repository"].casefold())
            covered = bool(commits) and all(commit and _is_ancestor(checkout, repo["pinned_commit"], commit)
                                           for commit in commits)
            if tasks[tid].get("kind") == "qualification" or not (
                    covered or _policy_only_repin(checkout, old_pin, repo["pinned_commit"])):
                incompatible.add(tid)
                break
    changed = True
    while changed:
        before = len(incompatible)
        incompatible.update(t["id"] for t in raw["tasks"] if t["id"] in _record_entries("", record)
                            and set(t.get("depends_on", [])) & incompatible)
        changed = len(incompatible) != before
    return incompatible


def _is_ancestor(checkout: Path, older: str, newer: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", older, newer],
                              shell=False, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _policy_only_repin(checkout: Path, old_pin: str, new_pin: str) -> bool:
    if not _is_ancestor(checkout, old_pin, new_pin):
        return False
    try:
        diff = subprocess.run(["git", "-C", str(checkout), "diff", "--name-only", "-z", old_pin, new_pin, "--"],
                              shell=False, capture_output=True, text=True, timeout=10)
        return diff.returncode == 0 and set(filter(None, diff.stdout.split("\0"))) <= {".pi/autonomy.yaml"}
    except (OSError, subprocess.TimeoutExpired):
        return False


def evidence_pins(manifest: Path) -> dict[str, dict[str, str]]:
    """Original pins for each task and all repositories contributing its evidence."""
    raw = workspace_plan.load_manifest(manifest)
    tasks = {t["id"]: t for t in raw["tasks"]}
    pins = {r["id"]: r["pinned_commit"] for r in raw["repositories"]}
    result = {}
    for tid in tasks:
        visited, pending, scope = set(), [tid], set()
        while pending:
            current = pending.pop()
            if current in visited or current not in tasks:
                continue
            visited.add(current)
            task = tasks[current]
            scope.add(task["repository"])
            scope.update(c["repository"] for c in task.get("checks", []))
            pending.extend(task.get("depends_on", []))
        result[tid] = {r: pins[r] for r in sorted(scope) if r in pins}
    return result


def compatible_entries(record: dict, manifest: Path) -> dict:
    """Current task entries for presentation; preserve incompatible history on disk."""
    incompatible = _incompatible_entries(record, manifest)
    return {tid: entry for tid, entry in _record_entries("", record).items() if tid not in incompatible}


def observed_state(workspace_id: str, *, manifest: Path | None = None) -> dict[str, dict]:
    """Per-task observed state from the workspace record, as the planner overlay.

    A child run that ended COMPLETED or PARTIAL makes its task implemented;
    validated, ready_for_downstream and merged come only from the recorded
    `observed` block written by the contract tools.
    """
    if not WORKSPACE_ID_RE.fullmatch(workspace_id or ""):
        return {}
    return observed_from_record(load_workspace(workspace_record_path(workspace_id)) or {"tasks": {}}, manifest=manifest)


def observed_from_record(record: dict, *, manifest: Path | None = None) -> dict[str, dict]:
    observed: dict[str, dict] = {}
    tasks = record.get("tasks") if isinstance(record.get("tasks"), dict) else {}
    if not tasks:
        return observed
    manifest = manifest or (Path(record["manifest"]) if record.get("manifest") else None)
    if manifest is None:
        return {tid: {"evidence_error": EVIDENCE_CHANGED} for tid in tasks}
    incompatible = _incompatible_entries(record, manifest)
    for task_id, entry in tasks.items():
        if not isinstance(entry, dict):
            continue
        if task_id in incompatible:
            observed[task_id] = {"evidence_error": EVIDENCE_CHANGED}
            continue
        item = dict(entry.get("observed") or {})
        if entry.get("execution_status") == "exited" and entry.get("run_status") in {"COMPLETED", "PARTIAL"}:
            item["readiness"] = workspace_plan.effective_readiness("implemented", item.get("readiness"))
        observed[task_id] = item
    return observed


def _plan(manifest: Path, *, access_check) -> dict:
    try:
        raw = workspace_plan.load_manifest(manifest)
        workspace_id = raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else ""
        bindings = evidence_bindings(manifest)
        result = workspace_plan.plan(manifest, access_check=access_check,
                                     observed=observed_state(workspace_id, manifest=manifest))
        result["evidence_bindings"] = bindings
        result["evidence_pins"] = evidence_pins(manifest)
        return result
    except workspace_plan.WorkspaceError as exc:
        raise WorkspaceRunError("WORKSPACE_MANIFEST_INVALID", str(exc)) from exc


def _task(plan: dict, task_id: str) -> tuple[dict, dict]:
    task = next((t for t in plan["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise WorkspaceRunError("WORKSPACE_TASK_UNKNOWN", f"task {task_id} is not in the manifest")
    repo = next((r for r in plan["repositories"] if r["id"] == task["repository"]), None)
    if repo is None:
        raise WorkspaceRunError("WORKSPACE_TASK_REPOSITORY_UNKNOWN", f"repository {task['repository']} is not declared")
    return task, repo


def _workspace(plan: dict, manifest: Path) -> tuple[Path, dict]:
    """Load (or initialize) the record; callers hold locked_workspace."""
    path = workspace_record_path(plan["workspace_id"])
    record = load_workspace(path)
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if record is None:
        record = {"record_version": RECORD_VERSION, "workspace_id": plan["workspace_id"], "manifest": str(manifest.resolve()),
                  "manifest_digest": digest, "orchestrator_revision": plan["orchestrator_revision"],
                  "created_at": _now(), "tasks": {}}
    bindings = evidence_bindings(manifest)
    if plan.get("evidence_bindings", bindings) != bindings:
        raise WorkspaceRunError("WORKSPACE_MANIFEST_CHANGED", "task definitions changed while the operation was running")
    incompatible = _incompatible_entries(record, manifest, bindings)
    saved = record.setdefault("evidence_bindings", {})
    pins = evidence_pins(manifest)
    saved_pins = record.setdefault("evidence_pins", {})
    for tid, binding in bindings.items():
        if tid not in incompatible and (tid not in record["tasks"] or tid not in saved or tid not in saved_pins):
            saved[tid] = binding
            saved_pins[tid] = pins[tid]
    if not incompatible:
        record["manifest_digest"] = digest
    return path, record


def dispatch_inputs(plan: dict, task: dict, repo: dict) -> dict:
    """Immutable inputs of one dispatch: this repository's pin and each upstream task's pin and readiness."""
    tasks = {t["id"]: t for t in plan["tasks"]}
    repos = {r["id"]: r for r in plan["repositories"]}
    observed = observed_state(plan["workspace_id"], manifest=Path(plan["manifest"]))
    upstream = []
    for dep in task["depends_on"]:
        dep_repo = repos[tasks[dep]["repository"]]
        seen = observed.get(dep) or {}
        item = {"task": dep, "repository": dep_repo["repository"],
                "pinned_commit": dep_repo["pinned_commit"], "readiness": tasks[dep]["readiness"]}
        # Validated upstream work is consumed at the exact commit that passed its checks.
        if seen.get("validated_commit"):
            item["validated_commit"] = seen["validated_commit"]
            item["contracts"] = dict(sorted((seen.get("validated_contracts") or {}).items()))
        if (seen.get("pr") or {}).get("number"):
            item["pr"] = f"{dep_repo['repository']}#{seen['pr']['number']}"
        upstream.append(item)
    return {"repository": repo["repository"], "pinned_commit": repo["pinned_commit"], "upstream": upstream,
            "orchestrator_revision": plan["orchestrator_revision"]}


def _inputs_digest(plan: dict, task: dict, repo: dict) -> str:
    """SHA-256 of the declared inputs as they stand now (not the first dispatch's saved copy), so a failure
    signature changes when an upstream pin, readiness, validated commit or contract changes."""
    fresh = dispatch_inputs(plan, task, repo)
    return hashlib.sha256(json.dumps(fresh, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def objective_for(plan: dict, task: dict, repo: dict, inputs: dict | None = None,
                  authorization: str | None = None) -> str:
    """The child's objective; `authorization` is the sentence of an authorized launch, appended on its own line."""
    inputs = inputs or dispatch_inputs(plan, task, repo)
    def describe(u: dict) -> str:
        at = (f"validated commit {u['validated_commit']}" if u.get("validated_commit")
              else f"pinned commit {u['pinned_commit']}")
        extra = "".join(f", contract {name} {version}" for name, version in (u.get("contracts") or {}).items())
        extra += f", PR {u['pr']}" if u.get("pr") else ""
        return f"{u['task']} in {u['repository']} at {at} ({u['readiness']}{extra})"
    upstream = "; ".join(describe(u) for u in inputs["upstream"]) or "none"
    needs = ", ".join(f"{c['contract']} {c['version']}" for c in task.get("requires") or [])
    offers = ", ".join(f"{c['contract']} {c['version']}" for c in task.get("provides") or [])
    return (f"Workspace {plan['workspace_id']}, task {task['id']} in repository {repo['repository']} "
            f"at pinned commit {repo['pinned_commit']}.\n"
            f"Task: {task['title']}\n"
            f"Repository-scoped objective: {task.get('objective') or task['title']}\n"
            f"Upstream inputs: {upstream}. "
            + (f"Required contracts: {needs}. " if needs else "")
            + (f"Contracts this task must provide: {offers}. " if offers else "") +
            "Work only inside this repository; other repositories of the workspace are consumed through "
            "their pinned, validated artifacts, never through unverified branch contents and never edited here."
            + (f"\n{authorization}" if authorization else ""))


def _entry(record: dict, task: dict, repo: dict, execution: dict, inputs: dict | None = None) -> dict:
    run = execution.get("run") or {}
    entry = record["tasks"].get(task["id"], {})
    if inputs is not None and "inputs" not in entry:
        entry["inputs"] = inputs  # first dispatch only: inputs are immutable once recorded
    entry.update({"repository": repo["id"], "target_repository": execution["target_repository"],
                  "pinned_commit": repo["pinned_commit"], "execution_id": execution["execution_id"],
                  "objective_sha256": hashlib.sha256(execution["objective"].encode("utf-8")).hexdigest(),
                  "execution_status": execution["status"], "outcome": execution.get("outcome"),
                  "launches": execution.get("launches"), "run_id": execution.get("run_id"),
                  "run_status": run.get("status"), "run_blocker_code": run.get("blocker_code"),
                  "trace_path": run.get("trace_path"), "last_seen": _now()})
    # A run that stopped to ask for mutation approval and provably changed nothing awaits authorization;
    # every other BLOCKED or FAILED run stays a failure.
    entry.pop("stopped_for_authorization", None)
    if run.get("status") == "BLOCKED" and run.get("blocker_code") in workspace_authorization.CODES:
        try:
            trace = json.loads(Path(run["trace_path"]).read_text(encoding="utf-8")) if run.get("trace_path") else None
        except (OSError, ValueError):
            trace = None
        if workspace_authorization.stopped_for_authorization(trace, execution.get("checkout")):
            entry["stopped_for_authorization"] = True
    # Time limit of the current launch (absent keys clear a previous launch's values).
    for key in ("max_duration_seconds", "deadline_at", "deadline_epoch", "stop_reason", "time_limit",
                "max_lead_calls", "max_lead_calls_source", "lead_calls_warn_at",
                "failure_retry_budget", "failure_retry_budget_source", "inputs_digest"):
        if execution.get(key) is not None:
            entry[key] = execution[key]
        else:
            entry.pop(key, None)
    # Lead calls used against the limit, from the run record (absent without a limit).
    if isinstance((execution.get("run") or {}).get("lead_call_limit"), dict):
        entry["lead_call_limit"] = execution["run"]["lead_call_limit"]
    else:
        entry.pop("lead_call_limit", None)
    if execution.get("checkout"):
        cleanup = execution["checkout"]
        entry["checkout"] = {"restored": cleanup["restored"], "branch": cleanup["branch_after"],
                             "removed_worktrees": len(cleanup["removed_worktrees"]),
                             "kept_worktrees": len(cleanup["kept_worktrees"]), "left_reason": cleanup["left_reason"],
                             "dirty_files": cleanup.get("dirty_files"), "dirty_count": cleanup.get("dirty_count"),
                             "new_untracked": cleanup.get("new_untracked"),
                             "new_untracked_count": cleanup.get("new_untracked_count")}
    record["tasks"][task["id"]] = entry
    return entry


def time_limit_view(entry: dict | None, now: float | None = None) -> dict | None:
    """The limit of a task's current launch as plain numbers: None when it has none.

    `remaining_seconds` is set only while the child is running; a child stopped by the limit carries
    `stop_reason` and the elapsed time recorded when it was stopped.
    """
    entry = entry or {}
    limit = entry.get("max_duration_seconds")
    if type(limit) is not int or limit < 1:
        return None
    view = {"max_duration_seconds": limit, "deadline_at": entry.get("deadline_at"), "remaining_seconds": None,
            "stop_reason": None, "elapsed_seconds": None}
    if entry.get("execution_status") == "launched":
        remaining = headless_run.time_limit_remaining(entry, now)
        view["remaining_seconds"] = None if remaining is None else int(remaining)
    stopped = entry.get("time_limit")
    if entry.get("stop_reason") == headless_run.TIME_LIMIT_CODE and isinstance(stopped, dict):
        view["stop_reason"] = headless_run.TIME_LIMIT_CODE
        view["elapsed_seconds"] = stopped.get("elapsed_seconds")
    return view


def inflight_usage(plan: dict, task_ids, cache: dict | None = None) -> tuple[list[str], list[str]]:
    """Run ids of children that have not exited, and the reasons any could not be resolved.

    The workspace record links a child's run only after it exits, so a running
    child's spend would otherwise be invisible to the budget gate. An unreadable
    or missing execution record is reported as a problem, never skipped: the
    caller must treat the spend as incomplete. `cache` maps task id to a run id
    once found, so session files are not re-read on every poll.
    """
    cache = cache if cache is not None else {}
    repos = {r["id"]: r for r in plan["repositories"]}
    by_task = {t["id"]: t for t in plan["tasks"]}
    entries = _record_entries(plan["workspace_id"])
    headless = headless_run
    found: list[str] = []
    problems: list[str] = []
    for tid in sorted(task_ids):
        if tid in cache:
            found.append(cache[tid])
            continue
        repo = repos.get(by_task.get(tid, {}).get("repository"))
        if repo is None or not repo.get("git_common_dir"):
            problems.append(f"{tid}: repository not resolved")
            continue
        try:
            execution = headless.load_record(headless.execution_path(
                Path(repo["git_common_dir"]), current_execution_id(plan["workspace_id"], tid, entries.get(tid))))
        except (headless.HeadlessError, OSError, ValueError) as exc:
            problems.append(f"{tid}: execution record unreadable ({getattr(exc, 'code', type(exc).__name__)})")
            continue
        if not execution:
            problems.append(f"{tid}: no execution record")
            continue
        if not execution.get("session_dir") or not execution.get("session_id"):
            problems.append(f"{tid}: execution record has no session")
            continue
        run_id = execution.get("run_id") or headless.discover_run_id(
            Path(execution["session_dir"]), execution["session_id"])
        if run_id:  # none yet means the child has not started its run, so it has spent nothing
            cache[tid] = run_id
            found.append(run_id)
    return found, problems


def _finished(entry) -> bool:
    """A workspace entry that already links a finished run: that child is not running, whatever its record says."""
    if not isinstance(entry, dict):
        return False
    return entry.get("execution_status") == "cancelled" or (
        entry.get("execution_status") == "exited" and entry.get("run_status") not in (None, "IN_PROGRESS"))


def running_tasks(plan: dict, exclude=(), workspace: dict | None = None) -> tuple[set[str], list[str]]:
    """Tasks (other than `exclude`) whose child process is alive, from their execution records.

    Tasks whose workspace entry already links a finished run are skipped, so a stale or corrupt
    record of a long-finished task never blocks anything. An unreadable record of a task that could
    be running is reported with its path.
    """
    repos = {r["id"]: r for r in plan["repositories"]}
    if workspace is None:
        workspace = load_workspace(workspace_record_path(plan["workspace_id"])) or {}
    entries = workspace.get("tasks") if isinstance(workspace.get("tasks"), dict) else {}
    running: set[str] = set()
    problems: list[str] = []
    for task in plan["tasks"]:
        tid = task["id"]
        repo = repos.get(task["repository"])
        if (tid in exclude or task.get("kind") == "qualification" or not repo or not repo.get("git_common_dir")
                or _finished(entries.get(tid))):
            continue
        path = headless_run.execution_path(Path(repo["git_common_dir"]),
                                           current_execution_id(plan["workspace_id"], tid, entries.get(tid)))
        try:
            record = headless_run.load_record(path)
        except (headless_run.HeadlessError, OSError, ValueError) as exc:
            problems.append(f"{tid}: execution record {path} is unreadable ({getattr(exc, 'code', type(exc).__name__)})")
            continue
        if record and record.get("status") in {"launching", "launched"} and headless_run.execution_alive(record):
            running.add(tid)
    return running, problems


def budget_state(plan: dict, record: dict | None = None, runs_dir: Path | None = None,
                 running: tuple | list | set = (), run_cache: dict | None = None,
                 extra_problems: list | None = None) -> dict | None:
    """Spend against the manifest's budget, from the workspace's recorded child runs.

    `running` names tasks whose children have not exited; inflight_usage resolves
    their runs so in-flight spend counts toward the gate. When that cannot be
    done the state is `incomplete` and callers must not launch anything new.
    """
    budget = plan.get("budget")
    if not budget:
        return None
    if record is None:
        record = load_workspace(workspace_record_path(plan["workspace_id"])) or {}
    inflight, problems = inflight_usage(plan, running, run_cache) if running else ([], [])
    problems = [*problems, *(extra_problems or [])]
    used = usage.workspace_usage(runs_dir or headless_run.state_root() / "runs", record, inflight)
    cost = used.get("estimated_cost_usd") or 0.0
    reasons = []
    if budget.get("max_tokens") is not None and used["spent_tokens"] >= budget["max_tokens"]:
        reasons.append("tokens")
    if budget.get("max_cost_usd") is not None and cost >= budget["max_cost_usd"]:
        reasons.append("cost")
    return {"max_tokens": budget.get("max_tokens"), "spent_tokens": used["spent_tokens"],
            "max_cost_usd": budget.get("max_cost_usd"), "spent_cost_usd": round(cost, 6),
            "runs": used["runs"], "exhausted": bool(reasons), "reasons": reasons,
            "cost_complete": used["cost_complete"], "unpriced": used["unpriced"],
            "incomplete": bool(problems), "incomplete_reasons": problems}


def launch(*, manifest: Path, task_id: str, allow_mutation: bool = False, resume: bool = False, wait: bool = True,
           access_check=headless_run.workspace_plan.gh_access, pi_executable: str = "pi",
           ignore_budget: bool = False, budget_checked: bool = False, takeover_stale_lease: bool = False) -> dict:
    """ignore_budget is the explicit user override (recorded); budget_checked is for a caller that already
    gated the budget itself (the scheduler) and skips the check without recording anything.
    takeover_stale_lease, with resume, explicitly recovers a stale run lease (recorded in provenance)."""
    plan = _plan(manifest, access_check=access_check)
    task, repo = _task(plan, task_id)
    if task["kind"] == "qualification":
        raise WorkspaceRunError("WORKSPACE_TASK_NOT_LAUNCHABLE",
                                "qualification tasks run checks, not a child run: use pico workspace validate")
    repo_blockers = [b for b in plan["blockers"] if b["scope"] == "repository" and b["id"] == repo["id"]]
    if repo_blockers:
        raise WorkspaceRunError(repo_blockers[0]["code"], repo_blockers[0]["detail"])
    task_blockers = [b for b in plan["blockers"] if b["scope"] == "task" and b["id"] == task_id]
    if task_blockers:
        raise WorkspaceRunError(task_blockers[0]["code"], task_blockers[0]["detail"])
    if task["blocked_by"]:
        raise WorkspaceRunError("WORKSPACE_TASK_BLOCKED", "waiting for " + ", ".join(task["blocked_by"]))
    checkout = Path(repo["resolved_path"])
    head = _head(checkout)
    if head != repo["pinned_commit"]:
        raise WorkspaceRunError("WORKSPACE_PIN_MISMATCH",
                                f"checkout HEAD {head or 'unknown'} is not the pinned commit; check it out first")
    budget_override: dict | None = None
    # A task's own limit overrides the manifest default; neither set means no limit.
    max_duration = task.get("max_duration_seconds") or plan.get("max_duration_seconds")
    # Same precedence for the lead-call limit; the repository policy is the extension's fallback.
    if task.get("max_lead_calls"):
        max_lead_calls, lead_source = task["max_lead_calls"], "task"
    elif plan.get("max_lead_calls"):
        max_lead_calls, lead_source = plan["max_lead_calls"], "manifest"
    else:
        max_lead_calls, lead_source = None, "cli"
    lead_warn_at = task.get("lead_calls_warn_at") or plan.get("lead_calls_warn_at")
    # Same precedence for the failure retry budget (0 is a real value: no retry), then the repository policy.
    if task.get("failure_retry_budget") is not None:
        failure_budget, failure_source = task["failure_retry_budget"], "task"
    elif plan.get("failure_retry_budget") is not None:
        failure_budget, failure_source = plan["failure_retry_budget"], "manifest"
    else:
        failure_budget, failure_source = None, "cli"

    def budget_gate() -> None:
        # Runs only when a child would really start or resume, never for a repeat launch that is a no-op.
        nonlocal budget_override
        if budget_checked:
            return
        # Children the scheduler or another launch started are still running: count their spend too,
        # and fail closed when it cannot be resolved.
        others, other_problems = running_tasks(plan, exclude={task_id})
        spend = budget_state(plan, running=others, extra_problems=other_problems)
        if spend is None or not (spend["exhausted"] or spend["incomplete"]):
            return
        if not spend["exhausted"]:
            if not ignore_budget:
                raise WorkspaceRunError(
                    "WORKSPACE_BUDGET_UNKNOWN",
                    "in-flight spend of running children cannot be resolved (" + "; ".join(spend["incomplete_reasons"])
                    + "). If that task is not running, repair or delete the execution record named above; "
                    "otherwise pass --ignore-budget (ignore_budget=True) to override explicitly")
            budget_override = {"at": _now(), "reasons": ["unknown_in_flight_spend"], **{k: spend[k] for k in (
                "spent_tokens", "max_tokens", "spent_cost_usd", "max_cost_usd")}}
            return
        if not ignore_budget:
            raise WorkspaceRunError(
                "WORKSPACE_BUDGET_EXHAUSTED",
                f"workspace budget exhausted ({', '.join(spend['reasons'])}): spent {spend['spent_tokens']} tokens"
                f" of {spend['max_tokens']}, ${spend['spent_cost_usd']} of {spend['max_cost_usd']}; raise the manifest"
                " budget, or pass --ignore-budget (ignore_budget=True) to override explicitly")
        budget_override = {"at": _now(), **{k: spend[k] for k in (
            "reasons", "spent_tokens", "max_tokens", "spent_cost_usd", "max_cost_usd")}}

    def checkout_gate() -> None:
        # Runs only when a child would really start or resume, like the budget gate, and is never skipped:
        # a task must not build on, commit or validate against changes no run recorded (#68).
        dirty = headless_run.tracked_changes(checkout)
        if dirty is None:
            raise WorkspaceRunError("WORKSPACE_CHECKOUT_DIRTY",
                                    f"cannot read the status of the {repo['id']} checkout; refusing to launch")
        if not dirty:
            return
        shown = ", ".join(dirty[:10]) + (f" and {len(dirty) - 10} more" if len(dirty) > 10 else "")
        record = load_workspace(workspace_record_path(plan["workspace_id"])) or {}
        culprits = [f"{tid} ({entry.get('run_id') or 'no run id'})" for tid, entry in sorted((record.get("tasks") or {}).items())
                    if entry.get("repository") == repo["id"] and (entry.get("checkout") or {}).get("dirty_count")]
        left_by = f"; left by {', '.join(culprits)}" if culprits else ""
        raise WorkspaceRunError("WORKSPACE_CHECKOUT_DIRTY",
                                f"the {repo['id']} checkout has uncommitted tracked changes: {shown}{left_by}. "
                                "Inspect them, then commit, move or discard them yourself; PICO never cleans a checkout")

    verdict = workspace_authorization.check(task, repo, allow_mutation=allow_mutation)

    def authorization_gate() -> None:
        # Before any token is spent: a task that needs mutation runs only under its committed policy and the
        # operator's explicit --allow-mutation. Awaiting authorization is not a failure (see the scheduler).
        if verdict["status"] == "awaiting":
            raise WorkspaceRunError(verdict["code"], authorization_detail(verdict))
        # headless_run enforces the checkout's own policy file, so with mutation allowed it must be the committed
        # one: an untracked or edited policy (also under a `mutation: none` task) would otherwise take effect.
        if allow_mutation and workspace_authorization.uncommitted_policy(checkout, repo["pinned_commit"]):
            raise WorkspaceRunError(workspace_authorization.UNCOMMITTED_CODE,
                                    f".pi/autonomy.yaml in repository {repo['id']} differs from "
                                    "the one committed at its pinned commit; commit or remove the change, re-pin, "
                                    "and launch again")

    def before_spawn() -> None:
        checkout_gate()
        authorization_gate()
        budget_gate()

    with locked_workspace(plan["workspace_id"]):
        prior = load_workspace(workspace_record_path(plan["workspace_id"]))
        prior_entry = ((prior or {}).get("tasks") or {}).get(task_id) or {}
    attempt = prior_entry.get("attempt") if type(prior_entry.get("attempt")) is int else 1
    exec_id = current_execution_id(plan["workspace_id"], task_id, prior_entry)
    new_attempt = not resume and retry_due(prior_entry, verdict)
    if new_attempt:
        # The previous attempt stopped for mutation approval and changed nothing; authorization has changed
        # since. A new execution id leaves that attempt's execution and run records untouched.
        attempt += 1
        exec_id = execution_id(plan["workspace_id"], task_id, attempt)
    saved_inputs = None if new_attempt else prior_entry.get("inputs")
    inputs = saved_inputs if saved_inputs is not None else dispatch_inputs(plan, task, repo)
    authorized = workspace_authorization.objective_line(verdict) if verdict["status"] == "authorized" else None
    objective = objective_for(plan, task, repo, inputs, authorized)
    old = headless_run.load_record(headless_run.execution_path(Path(repo["git_common_dir"]), exec_id))
    if old is not None and old.get("objective") != objective:
        # An execution keeps the objective it was created with: one launched before the authorization
        # sentence existed (or while unauthorized), and pre-extension ones that used the workspace goal.
        plain = objective_for(plan, task, repo, inputs)
        candidates = [plain]
        if task.get("objective") is None:
            candidates += [text.replace(f"Repository-scoped objective: {task['title']}\n",
                                        f"Workspace goal: {plan['goal']}\n", 1) for text in (objective, plain)]
        objective = next((text for text in candidates if old.get("objective") == text), objective)
    if new_attempt:
        before_spawn()  # refuse before the record moves to the new attempt; they run again right before spawning
        with locked_workspace(plan["workspace_id"]):
            path, record = _workspace(plan, manifest)
            _begin_attempt(record, task_id, attempt, exec_id, inputs, verdict)
            save_workspace(path, record)
    try:
        execution = headless_run.start(target=checkout, explicit_repo=repo["repository"],
                                       execution_id=exec_id,
                                       objective=objective, allow_mutation=allow_mutation,
                                       resume=resume, wait=wait, pi_executable=pi_executable,
                                       before_spawn=before_spawn, takeover=takeover_stale_lease,
                                       max_duration_seconds=max_duration, max_lead_calls=max_lead_calls,
                                       max_lead_calls_source=lead_source, lead_calls_warn_at=lead_warn_at,
                                       failure_retry_budget=failure_budget, failure_retry_budget_source=failure_source,
                                       inputs_digest=_inputs_digest(plan, task, repo))
    except headless_run.HeadlessError as exc:
        raise WorkspaceRunError(exc.code, exc.detail) from exc
    with locked_workspace(plan["workspace_id"]):
        path, record = _workspace(plan, manifest)
        entry = _entry(record, task, repo, execution, inputs)
        if budget_override is not None and execution.get("outcome") in {"launched", "resumed"}:
            entry["budget_override"] = budget_override
        if execution.get("outcome") in {"launched", "resumed"}:
            entry["authorization"] = authorization_view(verdict)
            entry["authorization_fingerprint"] = workspace_authorization.fingerprint(verdict)
        save_workspace(path, record)
    return {"workspace_id": plan["workspace_id"], "task": entry, "workspace_record": str(path)}


ATTEMPT_FIELDS = ("execution_id", "execution_status", "outcome", "launches", "run_id", "run_status", "run_blocker_code",
                  "trace_path", "objective_sha256", "pinned_commit", "inputs", "inputs_digest", "authorization",
                  "authorization_fingerprint", "stopped_for_authorization", "checkout", "budget_override")


def authorization_view(verdict: dict) -> dict:
    """The recorded part of a verdict: no paths, only the decision and what it rested on."""
    return {k: verdict.get(k) for k in ("status", "code", "required", "missing", "policy_source", "policy_blob",
                                        "uncommitted_policy", "allow_mutation", "pinned_commit")}


def authorization_detail(verdict: dict) -> str:
    if verdict["code"] == workspace_authorization.FLAG_CODE:
        return (f"the policy committed in {verdict['repository']} grants " + " and ".join(verdict["required"])
                + "; pass --allow-mutation to launch it")
    if verdict.get("missing") and verdict["code"] in workspace_authorization.CODES:
        source = "no committed .pi/autonomy.yaml (default policy)" if verdict.get("policy_source") == "default" \
            else "the .pi/autonomy.yaml committed at the pinned commit"
        return (f"{verdict['repository']} needs {', '.join(verdict['missing'])}, which {source} does not grant; "
                "nothing was launched. See pico workspace preflight for the approval steps")
    return f"{verdict['repository']}: {verdict.get('detail') or 'the committed autonomy policy cannot be used'}"


def retry_due(entry: dict | None, verdict: dict) -> bool:
    """A new attempt is due: the current one stopped for authorization, and the task is authorized now under
    a different authorization state than the one it was launched with (so a repeated stop is never looped)."""
    entry = entry or {}
    return (entry.get("stopped_for_authorization") is True and entry.get("execution_status") == "exited"
            and verdict["status"] == "authorized"
            and workspace_authorization.fingerprint(verdict) != entry.get("authorization_fingerprint"))


def _begin_attempt(record: dict, task_id: str, attempt: int, exec_id: str, inputs: dict, verdict: dict) -> None:
    """Move a task's current attempt into its `attempts` history and point the entry at the new one.

    The history keeps each attempt's execution and run ids, outcome, inputs and authorization, so
    nothing recorded about it is rewritten; observed contract state belongs to the task and stays.
    """
    entry = record["tasks"].get(task_id) or {}
    previous = {key: entry[key] for key in ATTEMPT_FIELDS if key in entry}
    previous.update({"attempt": entry.get("attempt", 1), "superseded_at": _now(), "superseded_by": exec_id})
    kept = {key: entry[key] for key in ("repository", "target_repository", "observed") if key in entry}
    record["tasks"][task_id] = {**kept, "attempts": [*(entry.get("attempts") or []), previous], "attempt": attempt,
                                "execution_id": exec_id, "inputs": inputs,
                                "authorization": authorization_view(verdict),
                                "authorization_fingerprint": workspace_authorization.fingerprint(verdict)}


def authorization_verdicts(plan: dict, *, allow_mutation: bool) -> dict[str, dict]:
    """Each task's mutation authorization verdict, before anything is launched (read-only)."""
    repos = {r["id"]: r for r in plan["repositories"]}
    return {task["id"]: workspace_authorization.check(task, repos.get(task["repository"]) or {"id": task["repository"]},
                                                      allow_mutation=allow_mutation)
            for task in plan["tasks"]}


def approval_requests(verdicts: dict[str, dict], task_ids) -> list[dict]:
    """Awaiting verdicts of `task_ids`, grouped into one request per repository and code."""
    groups: dict[tuple, dict] = {}
    for tid in sorted(task_ids):
        verdict = verdicts.get(tid)
        if not verdict or verdict["status"] != "awaiting":
            continue
        key = (verdict["repository"], verdict["code"])
        if key not in groups:
            groups[key] = {**{k: verdict.get(k) for k in (
                "repository", "github", "code", "required", "missing", "policy_source", "uncommitted_policy",
                "base_branch", "pinned_commit", "detail")},
                "never_granted": list(workspace_authorization.NEVER_GRANTED), "tasks": []}
        groups[key]["tasks"].append(tid)
    return [groups[key] for key in sorted(groups)]


def request_text(requests: list[dict], manifest: Path | str) -> list[str]:
    lines = []
    for request in requests:
        lines += workspace_authorization.request_lines(request, request["tasks"], manifest)
    return lines


def preflight(*, manifest: Path, allow_mutation: bool = False, access_check=None) -> dict:
    """Mutation authorization of every implementation task, without launching anything or spending tokens."""
    plan = _plan(manifest, access_check=access_check)
    verdicts = authorization_verdicts(plan, allow_mutation=allow_mutation)
    tasks = [{"id": t["id"], "repository": t["repository"], "status": verdicts[t["id"]]["status"],
              "code": verdicts[t["id"]]["code"], "missing": verdicts[t["id"]]["missing"]} for t in plan["tasks"]]
    requests = approval_requests(verdicts, verdicts)
    return {"workspace_id": plan["workspace_id"], "allow_mutation": allow_mutation, "tasks": tasks,
            "approval_requests": requests, "status": "AWAITING_AUTHORIZATION" if requests else "AUTHORIZED"}


def status(*, manifest: Path, task_id: str | None = None, access_check=None) -> dict:
    plan = _plan(manifest, access_check=access_check)
    with locked_workspace(plan["workspace_id"]):
        return _status_locked(plan, manifest, task_id)


def _status_locked(plan: dict, manifest: Path, task_id: str | None) -> dict:
    path, record = _workspace(plan, manifest)
    tasks = [t for t in plan["tasks"] if task_id is None or t["id"] == task_id]
    if task_id is not None and not tasks:
        raise WorkspaceRunError("WORKSPACE_TASK_UNKNOWN", f"task {task_id} is not in the manifest")
    for task in tasks:
        if any(b["scope"] == "task" and b["id"] == task["id"] and b["code"] == EVIDENCE_CHANGED
               for b in plan["blockers"]):
            continue
        _, repo = _task(plan, task["id"])
        if repo["resolved_path"] is None or repo["blocker_code"] in {"TARGET_REPOSITORY_UNRESOLVED", "TARGET_REPOSITORY_CONFLICT",
                                                                    "TARGET_REPOSITORY_AMBIGUOUS", "WORKSPACE_REPOSITORY_PATH_MISSING"}:
            continue
        try:
            execution = headless_run.status(target=Path(repo["resolved_path"]), explicit_repo=repo["repository"],
                                            execution_id=current_execution_id(plan["workspace_id"], task["id"],
                                                                              record["tasks"].get(task["id"])))
        except headless_run.HeadlessError as exc:
            if exc.code == "EXECUTION_UNKNOWN":
                continue
            raise WorkspaceRunError(exc.code, exc.detail) from exc
        _entry(record, task, repo, execution)
    save_workspace(path, record)
    return {"workspace_id": plan["workspace_id"], "tasks": compatible_entries(record, manifest), "workspace_record": str(path),
            "plan_status": plan["status"], "plan_blockers": plan["blockers"]}


def cancel(*, manifest: Path, task_id: str, access_check=None) -> dict:
    plan = _plan(manifest, access_check=access_check)
    task, repo = _task(plan, task_id)
    if repo["resolved_path"] is None:
        raise WorkspaceRunError(repo["blocker_code"] or "TARGET_REPOSITORY_UNRESOLVED", "repository cannot be resolved")
    entry = _record_entries(plan["workspace_id"]).get(task_id)
    try:
        execution = headless_run.cancel(target=Path(repo["resolved_path"]), explicit_repo=repo["repository"],
                                        execution_id=current_execution_id(plan["workspace_id"], task_id, entry))
    except headless_run.HeadlessError as exc:
        raise WorkspaceRunError(exc.code, exc.detail) from exc
    with locked_workspace(plan["workspace_id"]):
        path, record = _workspace(plan, manifest)
        entry = _entry(record, task, repo, execution)
        save_workspace(path, record)
    return {"workspace_id": plan["workspace_id"], "task": entry, "workspace_record": str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch, observe or cancel one workspace task as a repository-scoped child run.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("launch", "status", "cancel", "preflight"):
        p = sub.add_parser(name)
        p.add_argument("manifest", type=Path)
        if name != "preflight":
            p.add_argument("--task", required=name != "status")
        else:
            p.add_argument("--allow-mutation", action="store_true",
                           help="check as a scheduler run with --allow-mutation would")
            p.add_argument("--json", action="store_true")
        p.add_argument("--no-access-check", action="store_true")
        if name == "launch":
            p.add_argument("--allow-mutation", action="store_true")
            p.add_argument("--resume", action="store_true")
            p.add_argument("--takeover-stale-lease", action="store_true",
                           help="with --resume: explicitly recover a stale run lease (recorded in provenance)")
            p.add_argument("--no-wait", action="store_true")
            p.add_argument("--ignore-budget", action="store_true",
                           help="launch although the workspace budget is exhausted; recorded as budget_override")
            p.add_argument("--pi", default=os.environ.get("PI_EXECUTABLE", "pi"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    access = None if args.no_access_check else headless_run.workspace_plan.gh_access
    try:
        if args.command == "launch":
            result = launch(manifest=args.manifest, task_id=args.task, allow_mutation=args.allow_mutation,
                            resume=args.resume, wait=not args.no_wait, access_check=access, pi_executable=args.pi,
                            ignore_budget=args.ignore_budget, takeover_stale_lease=args.takeover_stale_lease)
            ok = result["task"]["execution_status"] == "launched" or result["task"]["run_status"] in {"COMPLETED", "PARTIAL"}
        elif args.command == "status":
            result = status(manifest=args.manifest, task_id=args.task, access_check=access)
            ok = result["plan_status"] != "BLOCKED"
        elif args.command == "preflight":
            result = preflight(manifest=args.manifest, allow_mutation=args.allow_mutation, access_check=access)
            ok = result["status"] == "AUTHORIZED"
            if not args.json:
                print(f"Workspace {result['workspace_id']}: {result['status']}")
                for row in result["tasks"]:
                    print(f"  {row['id']} [{row['repository']}] {row['status']}" + (f" {row['code']}" if row["code"] else ""))
                lines = request_text(result["approval_requests"], args.manifest)
                if lines:
                    print("Awaiting human authorization (nothing was launched):")
                    print("\n".join(lines))
                return 0 if ok else 1
        else:
            result = cancel(manifest=args.manifest, task_id=args.task, access_check=access)
            ok = result["task"]["execution_status"] == "cancelled"
    except WorkspaceRunError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}, sort_keys=True))
        return 1
    print(json.dumps({"ok": ok, **result}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
