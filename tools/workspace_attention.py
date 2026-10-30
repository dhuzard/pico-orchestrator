"""Small, read-only attention list from Garden's sanitized scheduler view.

The scheduler owns task state and the action snapshot owns availability. This
module only joins their display data; an attention row is never an action
authorization or a substitute for a fresh action preview.
"""
from __future__ import annotations


REASONS = {
    "awaiting_authorization": "This task is waiting for repository mutation authorization.",
    "waiting_for_authorization": "An upstream task is waiting for repository authorization.",
    "needs_reconciliation": "The previous run stopped with an uncertain outcome and needs reconciliation.",
    "failed": "The child run failed.",
    "cancelled": "The child run was cancelled.",
    "refused": "The scheduler refused to dispatch this task.",
    "blocked_by_failure": "An upstream task failed, was cancelled, or needs reconciliation.",
    "held_by_budget": "The workspace budget is holding this task.",
    "not_dispatchable": "A planning or repository prerequisite is missing.",
    "partial": "The implementation ended with follow-up work to review.",
}
PRIORITY = {state: index for index, state in enumerate(REASONS)}


def attention_rows(view: dict, action_view: dict | None = None) -> list[dict]:
    """Project native task states and existing action availability into task rows.

    Input must be ``watch.workspace_view`` (already sanitized). The only text
    sent to the browser is fixed here; objective, title and prompt are absent.
    """
    actions_by_target: dict[str, list[dict]] = {}
    for action in (action_view or {}).get("actions") or []:
        if not isinstance(action, dict) or action.get("target") == view.get("workspace_id"):
            continue
        target = action.get("target")
        if not isinstance(target, str):
            continue
        actions_by_target.setdefault(target, []).append({
            "action": action.get("action"), "available": action.get("available") is True,
            "refusal_code": action.get("refusal_code") if action.get("available") is not True else None,
        })
    rows = []
    for repo in view.get("repositories") or []:
        for task in repo.get("tasks") or []:
            state = task.get("state")
            if state not in REASONS:
                continue
            reason_code = (task.get("run_blocker_code") or task.get("refusal_code") or
                           (task.get("authorization") or {}).get("code"))
            if state in {"not_dispatchable", "refused"}:
                reason_code = reason_code or repo.get("blocker_code")
            row = {
                "task_id": task["id"], "repository": repo["id"], "github": repo.get("repository"),
                "state": state, "reason": REASONS[state],
                "reason_code": reason_code,
                "blocked_by": task.get("blocked_by") or [], "run_id": task.get("run_id"),
                "actions": actions_by_target.get(task["id"], []),
            }
            rows.append(row)
    return sorted(rows, key=lambda row: (PRIORITY[row["state"]], row["repository"], row["task_id"]))
