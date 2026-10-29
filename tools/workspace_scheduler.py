#!/usr/bin/env python3
"""Cross-repository scheduler for a workspace execution plan.

Dispatches every dispatchable task of a workspace manifest as a
repository-scoped child run (tools/workspace_run.py), concurrently, bounded by
the manifest's max_parallel (optionally lowered with --max-parallel) and by at
most one active child per repository, so two runs never share a checkout.
It is a local process over persistent state: no broker, queue or service.

Readiness frontier. A task is dispatchable only when every dependency is
declared ready_for_downstream or merged. A child run that completes makes its
task *implemented*, which by design does not unblock dependents: a PR or a
finished run is not evidence that an interface is stable. One invocation
therefore dispatches the current frontier; after review or contract
validation declares upstream tasks ready_for_downstream in the manifest, the
next invocation dispatches the tasks they unblock.

Failure isolation. A task whose child failed, was cancelled, was refused at
dispatch, or ended in an uncertain state blocks all of its transitive
dependents (blocked_by_failure); independent tasks keep running.

Mutation authorization. Before dispatching, every implementation task is
checked against the .pi/autonomy.yaml committed at its repository's pinned
commit (tools/workspace_authorization.py): it needs create_worktrees and
commit, and the operator's --allow-mutation. A task that lacks either is not
launched and spends no tokens; it is awaiting_authorization, which is not a
failure, and its dependents are waiting_for_authorization, not
blocked_by_failure. A child that exited BLOCKED asking for mutation approval
counts as awaiting_authorization only if its run provably changed nothing.
Once authorization changes (policy committed and re-pinned, or the flag
passed), the next invocation starts a new attempt with its own execution id,
linked to the blocked one; a run that asks again under the same authorization
is not retried.

Restart. Every launch goes through the idempotent execution records, so a
restart never launches a second child for a recorded task. Children survive a
scheduler crash (each runs in its own session) and are observed again, not
relaunched. An interrupted child is an uncertain outcome: it is reported as
needs_reconciliation and only relaunched, into the same parent run, when
--resume-interrupted is passed explicitly. That flag is also the explicit
takeover of the interrupted child's stale run lease (recorded in the run's
provenance); a child whose lease is still live is refused with RUN_LEASE_HELD.

Exit codes: 0 every dispatched task ended COMPLETED or PARTIAL and nothing
failed; 1 anything failed, was refused, needs reconciliation or is blocked by
a failure, or another scheduler holds the workspace; 2 invalid manifest;
3 nothing failed but tasks await human mutation authorization.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workspace_authorization  # noqa: E402
import workspace_plan  # noqa: E402
import workspace_run  # noqa: E402

FAILURE_STATES = frozenset({"failed", "cancelled", "refused", "needs_reconciliation"})
AWAITING = "awaiting_authorization"
MAX_SESSIONS = 20


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SchedulerError(workspace_run.WorkspaceRunError):
    pass


@contextlib.contextmanager
def scheduler_lock(workspace_id: str):
    path = workspace_run.workspace_record_path(workspace_id).with_suffix(".scheduler.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SchedulerError("WORKSPACE_SCHEDULER_ACTIVE", "another scheduler is running this workspace") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def classify(entry: dict | None) -> str:
    """State of one task from its workspace-record entry."""
    if not entry or not entry.get("execution_status"):
        return "not_launched"
    status, run = entry["execution_status"], entry.get("run_status")
    if status == "launched":
        return "running"
    if status == "cancelled":
        return "cancelled"
    if status == "exited":
        if run == "BLOCKED" and entry.get("stopped_for_authorization") is True:
            return AWAITING
        return {"COMPLETED": "completed", "PARTIAL": "partial"}.get(run, "failed")
    if status == "created":
        return "not_launched"
    return "needs_reconciliation"  # interrupted, launching, or anything unexpected


OBSERVED_RELEASE = ("validated", "ready_for_downstream", "merged")


def state_of(task: dict, entry: dict | None, refused: dict) -> str:
    """Scheduler state of one task, combining its execution and its observed contract state."""
    if task["id"] in refused:
        return AWAITING if refused[task["id"]].get("code") in workspace_authorization.AWAITING_CODES else "refused"
    observed = (entry or {}).get("observed") or {}
    if task.get("kind") == "qualification":
        if observed.get("readiness") in OBSERVED_RELEASE:
            return "validated"
        if (observed.get("validation") or {}).get("status") == "failed":
            return "failed"
        return "not_launched"
    base = classify(entry)
    if base in {"completed", "partial"} and observed.get("readiness") in OBSERVED_RELEASE:
        return observed["readiness"]
    return base


def dependents_closure(tasks: list[dict], roots: set[str]) -> set[str]:
    by_id = {t["id"]: t for t in tasks}
    blocked: set[str] = set()
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        for dependent in by_id.get(current, {}).get("blocks", []):
            if dependent not in blocked:
                blocked.add(dependent)
                frontier.append(dependent)
    return blocked - roots


def budget_state(plan: dict, record: dict | None = None, runs_dir: Path | None = None, **kwargs) -> dict | None:
    """Spend against the manifest's budget (computation lives in workspace_run.budget_state)."""
    return workspace_run.budget_state(plan, record, runs_dir, **kwargs)


inflight_usage = workspace_run.inflight_usage


def report(plan: dict, entries: dict, refused: dict, held: set[str] | None = None,
           verdicts: dict | None = None, awaiting: set[str] | None = None) -> dict:
    """`verdicts` are the preflight's authorization verdicts; `awaiting` the tasks it kept from launching."""
    tasks = plan["tasks"]
    states = {t["id"]: state_of(t, entries.get(t["id"]), refused) for t in tasks}
    for tid in awaiting or ():
        if states.get(tid) == "not_launched":
            states[tid] = AWAITING
    failed = {t for t, s in states.items() if s in FAILURE_STATES}
    by_failure = dependents_closure(tasks, failed)
    by_authorization = dependents_closure(tasks, {t for t, s in states.items() if s == AWAITING}) - by_failure
    rows = []
    for task in tasks:
        state = states[task["id"]]
        if state == "not_launched":
            if held and task["id"] in held:
                state = "held_by_budget"
            elif task["id"] in by_failure:
                state = "blocked_by_failure"
            elif task["id"] in by_authorization:
                state = "waiting_for_authorization"
            elif task["readiness"] != "planned":
                state = f"declared_{task['readiness']}"
            elif task["wave"] is None or any(b["scope"] == "task" and b["id"] == task["id"] for b in plan["blockers"]):
                state = "not_dispatchable"
            elif task["blocked_by"]:
                state = "waiting_for_readiness"
        entry = entries.get(task["id"]) or {}
        rows.append({"id": task["id"], "repository": task["repository"], "wave": task["wave"], "state": state,
                     "blocked_by": task["blocked_by"], "run_id": entry.get("run_id"),
                     "run_status": entry.get("run_status"), "run_blocker_code": entry.get("run_blocker_code"),
                     "launches": entry.get("launches"), "refusal": refused.get(task["id"]),
                     "time_limit": workspace_run.time_limit_view(entry),
                     "lead_call_limit": entry.get("lead_call_limit"),
                     "attempt": entry.get("attempt") or (1 if entry.get("execution_id") else None),
                     "previous_attempts": [{k: a.get(k) for k in ("attempt", "execution_id", "run_id", "run_status",
                                                                  "run_blocker_code")}
                                           for a in entry.get("attempts") or [] if isinstance(a, dict)],
                     "authorization": _verdict_view((verdicts or {}).get(task["id"]))})
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    bad = {"failed", "cancelled", "refused", "needs_reconciliation", "blocked_by_failure", "held_by_budget"}
    waiting = {r["id"] for r in rows if r["state"] == AWAITING}
    if any(r["state"] in bad for r in rows):
        status = "ATTENTION"
    else:
        status = "AWAITING_AUTHORIZATION" if waiting else "OK"
    # A task can await authorization while its verdict says authorized: its run asked for approval again
    # under the same authorization it was launched with. That is reported, never retried in a loop.
    repeated = sorted(t for t in waiting if ((verdicts or {}).get(t) or {}).get("status") == "authorized")
    # Tasks not reached yet whose repository will need approval too, so it can be given in one pass.
    later = {r["id"] for r in rows if r["state"] not in {AWAITING, "completed", "partial", "running", "validated",
                                                          "ready_for_downstream", "merged"}}
    return {"tasks": rows, "counts": dict(sorted(counts.items())), "status": status,
            "approval_requests": workspace_run.approval_requests(verdicts or {}, waiting),
            "later_approval_requests": workspace_run.approval_requests(verdicts or {}, later),
            "repeated_authorization_stops": repeated}


def _verdict_view(verdict: dict | None) -> dict | None:
    if not verdict:
        return None
    return {"status": verdict["status"], "code": verdict["code"], "missing": verdict["missing"]}


def _record_session(workspace_id: str, manifest: Path, plan: dict, session: dict) -> None:
    with workspace_run.locked_workspace(workspace_id):
        path, record = workspace_run._workspace(plan, manifest)
        sessions = [s for s in record.get("scheduler_sessions", []) if s.get("id") != session["id"]]
        sessions.append(session)
        record["scheduler_sessions"] = sessions[-MAX_SESSIONS:]
        workspace_run.save_workspace(path, record)


def _qualify(manifest: Path, plan: dict, task_id: str) -> dict:
    import workspace_contracts
    workspace_contracts.validate(manifest=manifest, task_id=task_id)
    record = workspace_run.load_workspace(workspace_run.workspace_record_path(plan["workspace_id"])) or {"tasks": {}}
    return {"task": record["tasks"].get(task_id) or {}}


def _entries(plan: dict, manifest: Path) -> dict:
    return workspace_run.status(manifest=manifest, access_check=None)["tasks"]


def run(manifest: Path, *, max_parallel: int | None = None, allow_mutation: bool = False,
        resume_interrupted: bool = False, access_check=workspace_plan.gh_access, pi_executable: str = "pi",
        poll_seconds: float = 2.0) -> dict:
    plan = workspace_run._plan(manifest, access_check=access_check)
    cap = plan["max_parallel"] if max_parallel is None else max(1, min(max_parallel, plan["max_parallel"]))
    repo_of = {t["id"]: t["repository"] for t in plan["tasks"]}
    order = sorted((t for t in plan["tasks"]), key=lambda t: (t["wave"] if t["wave"] is not None else 1 << 30, t["id"]))
    session = {"id": f"{_now()}-{os.getpid()}", "started_at": _now(), "finished_at": None, "cap": cap,
               "dispatched": [], "resumed": [], "observed_running": [], "refused": {},
               "allow_mutation": bool(allow_mutation)}
    with scheduler_lock(plan["workspace_id"]):
        _record_session(plan["workspace_id"], manifest, plan, session)
        entries = _entries(plan, manifest)
        refused: dict[str, dict] = {}
        owned: dict[str, cf.Future] = {}
        attempted: set[str] = set()
        external = {t for t in entries if classify(entries[t]) == "running"}
        session["observed_running"] = sorted(external)
        held: set[str] = set()
        run_cache: dict[str, str] = {}
        # Preflight, before any token is spent: which tasks may mutate their repository.
        verdicts = workspace_run.authorization_verdicts(plan, allow_mutation=allow_mutation)
        awaiting: set[str] = set()
        with cf.ThreadPoolExecutor(max_workers=cap) as pool:
            while True:
                spend = budget_state(plan, running=set(owned) | external, run_cache=run_cache)
                if spend is not None:
                    session["budget"] = spend
                states = {t["id"]: state_of(t, entries.get(t["id"]), refused) for t in plan["tasks"]}
                failed = {t for t, s in states.items() if s in FAILURE_STATES}
                if resume_interrupted:
                    failed -= {t for t, s in states.items()
                               if s == "needs_reconciliation" and t not in attempted and t not in refused}
                by_failure = dependents_closure(plan["tasks"], failed)
                running = set(owned) | {t for t in external if states.get(t) == "running"}
                busy = {repo_of[t] for t in running}
                for task in order:
                    if len(running) >= cap:
                        break
                    tid = task["id"]
                    if tid in running or tid in attempted or tid in by_failure or repo_of[tid] in busy:
                        continue
                    resume = resume_interrupted and states[tid] == "needs_reconciliation"
                    retry = (states[tid] == AWAITING and task["dispatchable"] and tid not in refused
                             and workspace_run.retry_due(entries.get(tid), verdicts[tid]))
                    if not resume and not retry and not (states[tid] == "not_launched" and task["dispatchable"]):
                        continue
                    if not resume and verdicts[tid]["status"] == "awaiting":
                        # Missing or insufficient mutation authorization: launch nothing, spend nothing.
                        awaiting.add(tid)
                        continue
                    if spend is not None and (spend["exhausted"] or spend["incomplete"]) and task.get("kind") != "qualification":
                        # Over budget, or in-flight spend unknown (fail closed): launch nothing new;
                        # checks and running children are unaffected.
                        held.add(tid)
                        continue
                    attempted.add(tid)
                    if task.get("kind") == "qualification":
                        # The qualification wave runs its cross-repository checks; no child run.
                        session.setdefault("qualified", []).append(tid)
                        owned[tid] = pool.submit(_qualify, manifest, plan, tid)
                    else:
                        (session["resumed"] if resume else session["dispatched"]).append(tid)
                        if retry:
                            session.setdefault("new_attempts", []).append(tid)
                        owned[tid] = pool.submit(workspace_run.launch, manifest=manifest, task_id=tid,
                                                 allow_mutation=allow_mutation, resume=resume, wait=True,
                                                 access_check=None, pi_executable=pi_executable,
                                                 budget_checked=True,  # gated above
                                                 takeover_stale_lease=resume)
                    running.add(tid)
                    busy.add(repo_of[tid])
                    _record_session(plan["workspace_id"], manifest, plan, session)
                if not running:
                    break
                if owned:
                    done, _ = cf.wait(owned.values(), timeout=poll_seconds, return_when=cf.FIRST_COMPLETED)
                else:
                    time.sleep(poll_seconds)
                    done = set()
                for tid, future in list(owned.items()):
                    if future not in done:
                        continue
                    del owned[tid]
                    try:
                        entries[tid] = future.result()["task"]
                    except workspace_run.WorkspaceRunError as exc:
                        refused[tid] = {"code": exc.code, "detail": exc.detail}
                        session["refused"][tid] = exc.code
                for tid in [t for t in external if t not in owned]:
                    observed = workspace_run.status(manifest=manifest, task_id=tid, access_check=None)["tasks"].get(tid)
                    if observed is not None:
                        entries[tid] = observed
                    if classify(entries.get(tid)) != "running":
                        external.discard(tid)
        result = report(plan, entries, refused, held, verdicts, awaiting)
        session["held_by_budget"] = sorted(held)
        session["awaiting_authorization"] = sorted(r["id"] for r in result["tasks"] if r["state"] == AWAITING)
        final_spend = budget_state(plan)
        if final_spend is not None:
            session["budget"] = final_spend
        result["budget"] = final_spend
        session["finished_at"] = _now()
        session["status"] = result["status"]
        _record_session(plan["workspace_id"], manifest, plan, session)
    return {"workspace_id": plan["workspace_id"], "cap": cap, "session": session, **result}


def budget_lines(b: dict) -> list[str]:
    """Budget summary: spent tokens against the limit, cost when known, and what the cost leaves out."""
    limit = f"{b['max_tokens']:,}" if b.get("max_tokens") else "no limit"
    line = f"  Budget: {b['spent_tokens']:,} of {limit} tokens (input + output + cache writes; cache reads excluded)"
    if b.get("max_cost_usd"):
        line += f", ${b['spent_cost_usd']:.2f} of ${b['max_cost_usd']:.2f} reported cost"
    elif b.get("spent_cost_usd"):
        line += f", ${b['spent_cost_usd']:.2f} reported cost"
    unpriced = b.get("unpriced") or []
    if unpriced:
        line += f" (lower bound; cost excludes: {', '.join(unpriced)})"
    if b.get("exhausted"):
        line += " — exhausted, nothing new was launched"
    lines = [line]
    if b.get("incomplete"):
        lines.append("  In-flight spend is unknown, so nothing new was launched: " + "; ".join(b.get("incomplete_reasons") or []))
    lines.append("  The budget is a launch gate: children already running can still overshoot it.")
    return lines


def time_limit_text(view: dict | None) -> str:
    """` (limit 600s, 412s left)` for a running child, ` (stopped after 601.2s of a 600s limit)` for a stopped one."""
    if not view:
        return ""
    if view.get("stop_reason"):
        elapsed = view.get("elapsed_seconds")
        return f" (stopped after {elapsed if elapsed is not None else '?'}s of a {view['max_duration_seconds']}s limit)"
    if view.get("remaining_seconds") is not None:
        return f" (limit {view['max_duration_seconds']}s, {view['remaining_seconds']}s left)"
    return f" (limit {view['max_duration_seconds']}s)"


def lead_calls_text(view: dict | None) -> str:
    """` (lead calls 18/30)`; ` (lead calls 32/30, stopped by LEAD_CALL_LIMIT)` once the run was ended by it."""
    if not isinstance(view, dict) or type(view.get("max_lead_calls")) is not int:
        return ""
    calls = view.get("calls")
    text = f"lead calls {calls if calls is not None else '?'}/{view['max_lead_calls']}"
    return f" ({text}, stopped by LEAD_CALL_LIMIT)" if view.get("stopped") else f" ({text})"


def render(result: dict, manifest: Path | str | None = None) -> str:
    lines = [f"Workspace {result['workspace_id']}: {result['status']} (cap {result['cap']})"]
    for row in result["tasks"]:
        detail = row["run_blocker_code"] or (row["refusal"] or {}).get("code") or ""
        lines.append(f"  {row['id']} [{row['repository']}] {row['state']}"
                     + (f" run={row['run_id']}" if row["run_id"] else "") + (f" {detail}" if detail else "")
                     + time_limit_text(row.get("time_limit")) + lead_calls_text(row.get("lead_call_limit")))
    lines.append("  " + ", ".join(f"{k}={v}" for k, v in result["counts"].items()))
    if any(r["state"] == "needs_reconciliation" for r in result["tasks"]):
        lines.append("  Inspect interrupted children, then rerun with --resume-interrupted to continue them.")
    if result.get("approval_requests"):
        lines.append("  Awaiting human authorization (not a failure; these tasks launch nothing until it is given):")
        lines.extend(workspace_run.request_text(result["approval_requests"], manifest or "<manifest>"))
    for request in result.get("later_approval_requests") or []:
        lines.append(f"  Later tasks will also need approval in {request['repository']} ({request.get('github')}): "
                     f"{request['code']} for {', '.join(request['tasks'])}"
                     + (f", missing {', '.join(request['missing'])}" if request.get("missing") else ""))
    for tid in result.get("repeated_authorization_stops") or []:
        row = next(r for r in result["tasks"] if r["id"] == tid)
        lines.append(f"  {tid}: run {row['run_id']} stopped with {row['run_blocker_code']} although its committed policy "
                     "authorizes mutation; it is not retried under the same authorization. Inspect its trace.")
    if any(r["state"] == "waiting_for_authorization" for r in result["tasks"]):
        lines.append("  Tasks waiting_for_authorization dispatch once their upstream tasks are authorized, run and declared ready.")
    if result.get("budget"):
        b = result["budget"]
        lines.extend(budget_lines(b))
    if any(r["state"] == "waiting_for_readiness" for r in result["tasks"]):
        lines.append("  Waiting tasks dispatch on a later run once their upstream tasks are declared ready_for_downstream.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch the dispatchable tasks of a workspace concurrently.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--max-parallel", type=int, help="lower the manifest's max_parallel for this run")
    parser.add_argument("--allow-mutation", action="store_true")
    parser.add_argument("--resume-interrupted", action="store_true",
                        help="relaunch interrupted children into their existing parent runs")
    parser.add_argument("--no-access-check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pi", default=os.environ.get("PI_EXECUTABLE", "pi"), help=argparse.SUPPRESS)
    parser.add_argument("--poll-seconds", type=float, default=2.0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run(args.manifest, max_parallel=args.max_parallel, allow_mutation=args.allow_mutation,
                     resume_interrupted=args.resume_interrupted,
                     access_check=None if args.no_access_check else workspace_plan.gh_access,
                     pi_executable=args.pi, poll_seconds=args.poll_seconds)
    except workspace_run.WorkspaceRunError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}, sort_keys=True))
        return 2 if exc.code == "WORKSPACE_MANIFEST_INVALID" else 1
    print(json.dumps({"ok": result["status"] == "OK", **result}, indent=2, sort_keys=True) if args.json
          else render(result, args.manifest))
    return {"OK": 0, "AWAITING_AUTHORIZATION": 3}.get(result["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
