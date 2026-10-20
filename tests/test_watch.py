"""The local viewer exposes only the metadata its UI needs."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import watch


class WatchTest(unittest.TestCase):
    def test_filters_by_repository_and_omits_private_trace_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            record = {"run_id": "run-123", "started_at": "2026-10-14T12:00:00Z",
                      "outcome": "in_progress", "user_objective": "private text",
                      "parent_orchestration": {"target_repository": "owner/one",
                                               "final_status": "IN_PROGRESS", "phases": []}}
            (runs / "run-123.json").write_text(json.dumps(record))
            self.assertEqual(watch.load_runs(runs, "owner/two"), [])
            visible = watch.load_runs(runs, "OWNER/ONE")
            self.assertEqual(len(visible), 1)
            self.assertNotIn("user_objective", visible[0])
            self.assertEqual(visible[0]["id"], "run-123")
            self.assertEqual(visible[0]["interactions"], [])

    def test_exposes_only_recorded_interaction_summaries(self):
        record = {"run_id": "run-123", "parent_orchestration": {"target_repository": "owner/one",
                  "phases": [], "interactions": [{"sequence": 1, "type": "directive", "summary": "Review the save path.",
                  "task_id": "task-1", "step_id": "worker", "subagent_id": None, "reply_to": None,
                  "private_prompt": "Do not expose this"}],
                  "private_prompt": "Do not expose this"}}
        visible = watch.public_run(record)
        self.assertEqual(visible["interactions"][0]["summary"], "Review the save path.")
        self.assertNotIn("private_prompt", json.dumps(visible))

    def test_parent_run_exposes_only_links_from_matching_publication_children(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            parent = {"run_id": "run-123", "started_at": "2026-10-14T12:00:00Z",
                      "outcome": "partial", "parent_orchestration": {
                          "target_repository": "owner/repo", "final_status": "PARTIAL",
                          "phases": [{"id": "publication", "status": "completed",
                                      "child_run_ids": ["run-child", "run-other"]}]}}
            child = {"github_integration": {"target_repository": "owner/repo",
                     "audit": [{"operation": "create_draft_pr", "status": "applied", "pr": 42}]}}
            other = {"github_integration": {"target_repository": "different/repo",
                     "audit": [{"operation": "create_draft_pr", "status": "applied", "pr": 43}]}}
            for name, record in (("run-123", parent), ("run-child", child), ("run-other", other)):
                (runs / f"{name}.json").write_text(json.dumps(record))
            visible = watch.load_runs(runs, "owner/repo")[0]
            self.assertEqual(visible["status"], "PARTIAL")
            self.assertEqual(visible["links"], [{"kind": "pr", "number": 42,
                                                 "url": "https://github.com/owner/repo/pull/42"}])


if __name__ == "__main__":
    unittest.main()
