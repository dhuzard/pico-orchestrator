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
limits belong to the scheduler.

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
import workspace_plan  # noqa: E402

RECORD_VERSION = 1


class WorkspaceRunError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execution_id(workspace_id: str, task_id: str) -> str:
    return f"{workspace_id}--{task_id}"


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


def observed_state(workspace_id: str) -> dict[str, dict]:
    """Per-task observed state from the workspace record, as the planner overlay.

    A child run that ended COMPLETED or PARTIAL makes its task implemented;
    validated, ready_for_downstream and merged come only from the recorded
    `observed` block written by the contract tools.
    """
    if not WORKSPACE_ID_RE.fullmatch(workspace_id or ""):
        return {}
    return observed_from_record(load_workspace(workspace_record_path(workspace_id)) or {"tasks": {}})


def observed_from_record(record: dict) -> dict[str, dict]:
    observed: dict[str, dict] = {}
    tasks = record.get("tasks") if isinstance(record.get("tasks"), dict) else {}
    for task_id, entry in tasks.items():
        if not isinstance(entry, dict):
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
        return workspace_plan.plan(manifest, access_check=access_check, observed=observed_state(workspace_id))
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
    record["manifest_digest"] = digest
    return path, record


def dispatch_inputs(plan: dict, task: dict, repo: dict) -> dict:
    """Immutable inputs of one dispatch: this repository's pin and each upstream task's pin and readiness."""
    tasks = {t["id"]: t for t in plan["tasks"]}
    repos = {r["id"]: r for r in plan["repositories"]}
    observed = observed_state(plan["workspace_id"])
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


def objective_for(plan: dict, task: dict, repo: dict, inputs: dict | None = None) -> str:
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
            "their pinned, validated artifacts, never through unverified branch contents and never edited here.")


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
    if execution.get("checkout"):
        cleanup = execution["checkout"]
        entry["checkout"] = {"restored": cleanup["restored"], "branch": cleanup["branch_after"],
                             "removed_worktrees": len(cleanup["removed_worktrees"]),
                             "kept_worktrees": len(cleanup["kept_worktrees"]), "left_reason": cleanup["left_reason"]}
    record["tasks"][task["id"]] = entry
    return entry


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
                Path(repo["git_common_dir"]), execution_id(plan["workspace_id"], tid)))
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
        path = headless_run.execution_path(Path(repo["git_common_dir"]), execution_id(plan["workspace_id"], tid))
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

    with locked_workspace(plan["workspace_id"]):
        prior = load_workspace(workspace_record_path(plan["workspace_id"]))
        saved_inputs = ((prior or {}).get("tasks") or {}).get(task_id, {}).get("inputs")
    inputs = saved_inputs if saved_inputs is not None else dispatch_inputs(plan, task, repo)
    objective = objective_for(plan, task, repo, inputs)
    if task.get("objective") is None:
        # Pre-extension executions used the workspace goal. Resume only when
        # the stored text exactly matches that historical deterministic form.
        old = headless_run.load_record(headless_run.execution_path(
            Path(repo["git_common_dir"]), execution_id(plan["workspace_id"], task_id)))
        legacy = objective.replace(f"Repository-scoped objective: {task['title']}\n",
                                   f"Workspace goal: {plan['goal']}\n", 1)
        if old is not None and old.get("objective") == legacy:
            objective = legacy
    try:
        execution = headless_run.start(target=checkout, explicit_repo=repo["repository"],
                                       execution_id=execution_id(plan["workspace_id"], task_id),
                                       objective=objective, allow_mutation=allow_mutation,
                                       resume=resume, wait=wait, pi_executable=pi_executable,
                                       before_spawn=budget_gate, takeover=takeover_stale_lease)
    except headless_run.HeadlessError as exc:
        raise WorkspaceRunError(exc.code, exc.detail) from exc
    with locked_workspace(plan["workspace_id"]):
        path, record = _workspace(plan, manifest)
        entry = _entry(record, task, repo, execution, inputs)
        if budget_override is not None and execution.get("outcome") in {"launched", "resumed"}:
            entry["budget_override"] = budget_override
        save_workspace(path, record)
    return {"workspace_id": plan["workspace_id"], "task": entry, "workspace_record": str(path)}


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
        _, repo = _task(plan, task["id"])
        if repo["resolved_path"] is None or repo["blocker_code"] in {"TARGET_REPOSITORY_UNRESOLVED", "TARGET_REPOSITORY_CONFLICT",
                                                                    "TARGET_REPOSITORY_AMBIGUOUS", "WORKSPACE_REPOSITORY_PATH_MISSING"}:
            continue
        try:
            execution = headless_run.status(target=Path(repo["resolved_path"]), explicit_repo=repo["repository"],
                                            execution_id=execution_id(plan["workspace_id"], task["id"]))
        except headless_run.HeadlessError as exc:
            if exc.code == "EXECUTION_UNKNOWN":
                continue
            raise WorkspaceRunError(exc.code, exc.detail) from exc
        _entry(record, task, repo, execution)
    save_workspace(path, record)
    return {"workspace_id": plan["workspace_id"], "tasks": record["tasks"], "workspace_record": str(path),
            "plan_status": plan["status"]}


def cancel(*, manifest: Path, task_id: str, access_check=None) -> dict:
    plan = _plan(manifest, access_check=access_check)
    task, repo = _task(plan, task_id)
    if repo["resolved_path"] is None:
        raise WorkspaceRunError(repo["blocker_code"] or "TARGET_REPOSITORY_UNRESOLVED", "repository cannot be resolved")
    try:
        execution = headless_run.cancel(target=Path(repo["resolved_path"]), explicit_repo=repo["repository"],
                                        execution_id=execution_id(plan["workspace_id"], task_id))
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
    for name in ("launch", "status", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("manifest", type=Path)
        p.add_argument("--task", required=name != "status")
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
            ok = True
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
