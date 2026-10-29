"""Garden action snapshots preserve scheduler states and authority boundaries."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import workspace_action_view as view  # noqa: E402


class ActionViewTest(unittest.TestCase):
    def test_native_states_are_distinct(self):
        states = ("awaiting_authorization", "waiting_for_authorization", "waiting_for_readiness",
                  "held_by_budget", "needs_reconciliation", "cancelled", "failed")
        labels = [view.display_state(state)["label"] for state in states]
        self.assertEqual(len(set(labels)), len(states))
        self.assertEqual(view.display_state("needs_reconciliation")["category"], "interrupted")

    def test_preflight_refuses_dispatch_and_never_relabels_interruption(self):
        plan = {"workspace_id": "ws", "orchestrator_revision": "a" * 40, "max_parallel": 2,
                "repositories": [{"id": "repo", "pinned_commit": "b" * 40}],
                "tasks": [{"id": "first", "repository": "repo", "dispatchable": True},
                          {"id": "second", "repository": "repo", "dispatchable": False}]}
        rows = [{"id": "first", "state": "awaiting_authorization"},
                {"id": "second", "state": "waiting_for_authorization"}]
        verdicts = {name: {"status": "awaiting", "code": "MUTATION_AUTHORIZATION_REQUIRED",
                           "required": ["create_worktrees", "commit"], "policy_blob": None}
                    for name in ("first", "second")}
        with patch.object(view.workspace_run, "authorization_verdicts", return_value=verdicts), \
             patch.object(view.workspace_scheduler, "report", return_value={"tasks": rows, "approval_requests": []}):
            result = view.snapshot(plan, {})
        self.assertFalse(result["actions"][0]["available"])
        self.assertEqual(result["actions"][0]["refusal_code"], "WORKSPACE_NO_ELIGIBLE_TASKS")
        self.assertEqual(result["states"][1]["display"]["native"], "waiting_for_authorization")
        attempt = next(action for action in result["actions"] if action["action"] == "create_new_attempt")
        self.assertFalse(attempt["available"])
        self.assertEqual(attempt["required_permissions"], ["create_worktrees", "commit"])


if __name__ == "__main__":
    unittest.main()
