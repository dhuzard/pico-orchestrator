#!/usr/bin/env python3
"""Offline contract tests for the opt-in issue dispatcher; no GitHub or models."""
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "issue_dispatcher.py"
spec = importlib.util.spec_from_file_location("issue_dispatcher", SOURCE)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def issue(number=7, body=None):
    return {"number": number, "title": "Implement validation", "state": "OPEN",
            "labels": [{"name": "pico:queued"}],
            "body": body or "## Target repository\nacme/tool\n\n## Objective\nAdd validation tests.\n\n## Acceptance criteria\nTests pass.\n"}


class IssueDispatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"queue": "acme/private-queue", "targets": {"acme/tool": "/tmp/target"},
                    "state": Path(self.tmp.name) / "pico.sqlite3", "pico": "pico", "gh": "gh",
                    "poll": 10, "duration": 600, "calls": 10}

    def test_parse_is_allowlisted_and_content_bound(self):
        task = mod.parse_task(issue(), self.cfg)
        self.assertEqual(task.repo, "acme/tool")
        self.assertTrue(task.execution_id.startswith("issue-"))
        self.assertEqual(task.execution_id, mod.parse_task(issue(), self.cfg).execution_id)
        changed = issue(body=issue()["body"].replace("Tests pass.", "Tests fail."))
        self.assertNotEqual(task.digest, mod.parse_task(changed, self.cfg).digest)
        bad = issue(body=issue()["body"].replace("acme/tool", "other/repo"))
        with self.assertRaises(mod.DispatcherError):
            mod.parse_task(bad, self.cfg)

    def test_rejects_unlabelled_issue_and_extra_sections(self):
        example = issue()
        example["labels"] = []
        with self.assertRaises(mod.DispatcherError):
            mod.parse_task(example, self.cfg)
        example = issue(body=issue()["body"] + "\n## Elevated permissions\nyes")
        with self.assertRaises(mod.DispatcherError):
            mod.parse_task(example, self.cfg)

    def test_no_unapproved_task_is_executed(self):
        with mod.database(self.cfg) as db, patch.object(mod, "pico_action") as action:
            mod.tick(self.cfg, db)
            action.assert_not_called()

    def test_changed_issue_cannot_use_previous_approval(self):
        task = mod.parse_task(issue(), self.cfg)
        with mod.database(self.cfg) as db:
            db.execute("INSERT INTO approvals (number,digest,repo,checkout,execution_id,mode,state) VALUES(?,?,?,?,?,?,?)",
                       (7, task.digest, task.repo, task.checkout, task.execution_id, "read-only", "approved"))
            db.commit()
            changed = issue(body=issue()["body"].replace("Tests pass.", "Delete everything."))
            with patch.object(mod, "issue_view", return_value=changed), patch.object(mod, "pico_action") as action, patch.object(mod, "report"):
                mod.tick(self.cfg, db)
                action.assert_not_called()
            self.assertEqual(db.execute("SELECT state FROM approvals WHERE number=7").fetchone()[0], "stale")

    def test_only_one_active_job_at_a_time_and_eventual_completion(self):
        with mod.database(self.cfg) as db:
            for n in (7, 8):
                task = mod.parse_task(issue(n), self.cfg)
                db.execute("INSERT INTO approvals (number,digest,repo,checkout,execution_id,mode,state) VALUES(?,?,?,?,?,?,?)",
                           (n, task.digest, task.repo, task.checkout, task.execution_id, "read-only", "approved"))
            db.commit()
            with patch.object(mod, "issue_view", side_effect=lambda cfg, n: issue(n)), \
                 patch.object(mod, "pico_action", return_value={"status": "launched"}) as action, \
                 patch.object(mod, "report"):
                mod.tick(self.cfg, db)
                self.assertEqual(action.call_count, 1)
                self.assertEqual([x[0] for x in db.execute("SELECT state FROM approvals ORDER BY number")], ["running", "approved"])
            def fake_action(cfg, row, action, objective=""):
                return {"status": "exited", "run": {"status": "COMPLETED"}} if action == "status" else {"status": "launched"}
            with patch.object(mod, "issue_view", side_effect=lambda cfg, n: issue(n)), \
                 patch.object(mod, "pico_action", side_effect=fake_action), patch.object(mod, "report"):
                mod.tick(self.cfg, db)
            self.assertEqual([x[0] for x in db.execute("SELECT state FROM approvals ORDER BY number")], ["completed", "running"])

    def test_local_interactive_approval_is_explicit(self):
        task = mod.parse_task(issue(), self.cfg)
        with patch.object(mod, "issue_view", return_value=issue()), patch.object(mod.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", return_value="approve " + task.digest[:12]):
            mod.approve(self.cfg, 7, mutate=True)
        with mod.database(self.cfg) as db:
            row = db.execute("SELECT * FROM approvals WHERE number=7").fetchone()
            self.assertEqual(row["mode"], "local-mutation")
            self.assertEqual(row["state"], "approved")

    def test_classification_is_conservative(self):
        self.assertEqual(mod.classify({"status": "exited", "run": {"status": "BLOCKED"}}), "blocked")
        self.assertEqual(mod.classify({"status": "exited", "run": {"status": "PARTIAL"}}), "partial")
        self.assertEqual(mod.classify({"status": "interrupted"}), "interrupted")
        self.assertEqual(mod.classify({"ok": False, "error_code": "MUTATION_AUTHORIZATION_REQUIRED"}), "blocked")


if __name__ == "__main__":
    unittest.main()
