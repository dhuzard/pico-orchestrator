"""Read-only Garden action and display contract over the existing workspace services.

This is a snapshot, not an authorization token. Every action still runs its
own preview and rechecks the state at confirmation and dispatch.
"""
from __future__ import annotations

import hashlib
import json

import workspace_run
import workspace_scheduler


DISPLAY = {
    "not_launched": ("planned", "Planned"),
    "running": ("running", "Running"),
    "awaiting_authorization": ("waiting", "Awaiting authorization"),
    "waiting_for_authorization": ("waiting", "Waiting for repository authorization"),
    "waiting_for_readiness": ("waiting", "Waiting for dependency readiness"),
    "held_by_budget": ("waiting", "Held by budget"),
    "needs_reconciliation": ("interrupted", "Interrupted; reconciliation required"),
    "cancelled": ("cancelled", "Cancelled"),
    "failed": ("failed", "Failed"),
    "blocked_by_failure": ("waiting", "Blocked by a failed dependency"),
    "refused": ("failed", "Dispatch refused"),
    "completed": ("completed", "Implementation completed"),
    "partial": ("completed", "Implementation partial"),
    "validated": ("completed", "Validated"),
    "ready_for_downstream": ("completed", "Ready for downstream"),
    "merged": ("completed", "Merged"),
    "not_dispatchable": ("waiting", "Prerequisites missing"),
}


def display_state(native: str) -> dict:
    category, label = DISPLAY.get(native, ("waiting", native.replace("_", " ").capitalize()))
    return {"native": native, "category": category, "label": label}


def _action(name: str, target: str, available: bool, code: str | None, explanation: str,
            required: list[str], state_id: str, configuration: dict) -> dict:
    invocation = ({"action": "schedule", "resume_interrupted": True} if name == "resume_interrupted" else
                  {"action": "schedule", "allow_mutation": True} if name == "create_new_attempt" else
                  {"action": name})
    return {"action": name, "target": target, "available": available,
            "refusal_code": None if available else code, "explanation": explanation,
            "required_permissions": required, "state_id": state_id,
            "effective_configuration": configuration, "requires_preview": True,
            "invocation": invocation}


def snapshot(plan: dict, entries: dict, *, allow_mutation: bool = False,
             budget: dict | None = None, scheduler_running: bool = False) -> dict:
    """Explain next actions from the scheduler's native states and authorization verdicts."""
    verdicts = workspace_run.authorization_verdicts(plan, allow_mutation=allow_mutation)
    report = workspace_scheduler.report(plan, entries, {}, verdicts=verdicts)
    state_basis = {"workspace": plan["workspace_id"], "pin": plan.get("orchestrator_revision"),
                   "goal": plan.get("goal"), "plan_tasks": plan["tasks"], "blockers": plan.get("blockers"),
                   "repos": [(r["id"], r.get("pinned_commit")) for r in plan["repositories"]],
                   "tasks": [(r["id"], r["state"], r.get("run_id"), r.get("attempt")) for r in report["tasks"]],
                   "authorization": [(key, value.get("policy_blob"), value.get("status"))
                                     for key, value in sorted(verdicts.items())],
                   "budget": budget, "running": scheduler_running}
    state_id = hashlib.sha256(json.dumps(state_basis, sort_keys=True).encode()).hexdigest()
    config = {"max_parallel": plan["max_parallel"], "allow_mutation": allow_mutation,
              "orchestrator_revision": plan.get("orchestrator_revision"),
              "repository_pins": {r["id"]: r.get("pinned_commit") for r in plan["repositories"]}}
    rows = []
    actions = []
    runnable = False
    schedule_permissions: set[str] = set()
    for row in report["tasks"]:
        task = next(t for t in plan["tasks"] if t["id"] == row["id"])
        repo = next(r for r in plan["repositories"] if r["id"] == task["repository"])
        repo_ready = bool(repo.get("resolved_path")) and not repo.get("blocker_code")
        native = row["state"]
        verdict = verdicts[task["id"]]
        rows.append({"id": task["id"], "display": display_state(native)})
        if native == "needs_reconciliation":
            actions.append(_action("resume_interrupted", task["id"], True, None,
                                   "Resume the interrupted attempt after its lease is reconciled.",
                                   verdict["required"], state_id, config))
        elif native == "awaiting_authorization":
            retry = repo_ready and task["dispatchable"] and workspace_run.retry_due(entries.get(task["id"]), verdict)
            runnable = runnable or retry
            if retry:
                schedule_permissions.update(verdict["required"])
            actions.append(_action("create_new_attempt", task["id"], retry,
                                   verdict.get("code") or "AUTHORIZATION_UNCHANGED",
                                   "A terminal authorization stop starts a new attempt after authorization changes.",
                                   verdict["required"], state_id, config))
        elif native == "not_launched" and task["dispatchable"]:
            if repo_ready and verdict["status"] in {"authorized", "not_required"}:
                runnable = True
                schedule_permissions.update(verdict["required"])
            else:
                actions.append(_action("dispatch", task["id"], False, repo.get("blocker_code") or verdict["code"],
                                       "Resolve the repository and its authorization before dispatch.",
                                       verdict["required"], state_id, config))
        if native == "running":
            actions.append(_action("cancel", task["id"], True, None,
                                   "Cancel the active child run after preview.", [], state_id, config))
    budget_held = bool(budget and (budget.get("exhausted") or budget.get("incomplete")))
    code = ("WORKSPACE_SCHEDULER_ACTIVE" if scheduler_running else
            "WORKSPACE_BUDGET_HELD" if budget_held else "WORKSPACE_NO_ELIGIBLE_TASKS")
    actions.insert(0, _action("schedule", plan["workspace_id"], runnable and not budget_held and not scheduler_running,
                              code, "Review the scheduler preview before launching eligible work." if runnable else
                              "No task is currently eligible for dispatch.", sorted(schedule_permissions), state_id, config))
    return {"schema_version": 1, "state_id": state_id, "actions": actions, "states": rows,
            "approval_requests": report["approval_requests"]}
