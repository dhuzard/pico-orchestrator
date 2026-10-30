"""Attention rows only join sanitized task state and an existing action snapshot."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import workspace_attention  # noqa: E402


class AttentionTest(unittest.TestCase):
    def test_action_availability_is_copied_from_snapshot_only(self):
        view = {"workspace_id": "ws", "repositories": [{"id": "api", "repository": "acme/api", "tasks": [
            {"id": "t1", "state": "awaiting_authorization", "run_id": "run-1",
             "authorization": {"code": "MUTATION_AUTHORIZATION_REQUIRED"}, "blocked_by": []},
        ]}]}
        rows = workspace_attention.attention_rows(view)
        self.assertEqual(rows[0]["actions"], [])
        action_view = {"actions": [
            {"target": "ws", "action": "schedule", "available": True},
            {"target": "t1", "action": "create_new_attempt", "available": False,
             "refusal_code": "AUTHORIZATION_UNCHANGED"},
        ]}
        rows = workspace_attention.attention_rows(view, action_view)
        self.assertEqual(rows[0]["reason_code"], "MUTATION_AUTHORIZATION_REQUIRED")
        self.assertEqual(rows[0]["actions"], [{"action": "create_new_attempt", "available": False,
                                                 "refusal_code": "AUTHORIZATION_UNCHANGED"}])


if __name__ == "__main__":
    unittest.main()
