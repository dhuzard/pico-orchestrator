#!/usr/bin/env python3
"""Offline contract tests for the opt-in issue dispatcher; no GitHub or models."""
import importlib.util
import json
import os
from types import SimpleNamespace
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

    def configure_credentials(self):
        queue = Path(self.tmp.name) / "queue-token"
        queue.write_text("queue-secret\n")
        target = Path(self.tmp.name) / "target-gh"
        target.mkdir(mode=0o700)
        queue.chmod(0o600)
        (target / "hosts.yml").write_text("github.com:\n  oauth_token: target-secret\n")
        (target / "hosts.yml").chmod(0o600)
        self.cfg.update(queue_token_file=queue, target_gh_config_dir=target)
        return queue, target

    def test_credentials_are_scoped_to_queue_and_target_calls(self):
        _, target = self.configure_credentials()
        row = {"checkout": "/tmp/target", "repo": "acme/tool", "execution_id": "id", "mode": "read-only"}
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs["env"]))
            if argv[1:3] == ["auth", "status"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[1:3] == ["repo", "view"]:
                return SimpleNamespace(returncode=0, stdout='{"nameWithOwner":"acme/tool"}', stderr="")
            if argv[1:3] == ["exec", "start"]:
                return SimpleNamespace(returncode=0, stdout='{"status":"launched"}', stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(issue()), stderr="")
        with patch.dict(os.environ, {"GH_TOKEN": "ambient-secret", "GITHUB_TOKEN": "ambient-secret", "GH_CONFIG_DIR": "/ambient"}), \
             patch.object(mod.subprocess, "run", side_effect=run):
            mod.issue_view(self.cfg, 7)
            mod.pico_action(self.cfg, row, "start", "objective")
        queue_env, auth_env, repo_env, pico_env = [entry[1] for entry in calls]
        self.assertEqual(queue_env["GH_TOKEN"], "queue-secret")
        self.assertNotIn("GH_CONFIG_DIR", queue_env)
        for env in (auth_env, repo_env, pico_env):
            self.assertEqual(env["GH_CONFIG_DIR"], str(target))
            self.assertEqual(env["PI_DISPATCHER_QUEUE_CREDENTIAL_PATH"], str(self.cfg["queue_token_file"]))
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("queue-secret", env.values())
            self.assertNotIn("target-secret", env.values())

    def test_queue_list_and_comments_use_queue_token(self):
        self.configure_credentials()
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs["env"]))
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        with mod.database(self.cfg) as db, patch.object(mod.subprocess, "run", side_effect=run):
            mod.queued_issues(self.cfg)
            db.execute("INSERT INTO approvals (number,digest,repo,checkout,execution_id,mode,state) VALUES(?,?,?,?,?,?,?)",
                       (7, "digest", "acme/tool", "/tmp/target", "id", "read-only", "completed"))
            mod.report(self.cfg, db, db.execute("SELECT * FROM approvals").fetchone())
        self.assertEqual([call[0][2] for call in calls], ["list", "comment"])
        for _, env in calls:
            self.assertEqual(env["GH_TOKEN"], "queue-secret")
            self.assertNotIn("GH_CONFIG_DIR", env)

    def test_missing_credentials_block_without_invoking_subprocess(self):
        queue, target = self.configure_credentials()
        queue.unlink()
        with patch.object(mod.subprocess, "run") as run, self.assertRaises(mod.DispatcherError):
            mod.issue_view(self.cfg, 7)
        run.assert_not_called()
        (target / "hosts.yml").unlink()
        row = {"checkout": "/tmp/target", "repo": "acme/tool", "execution_id": "id", "mode": "read-only"}
        with patch.object(mod.subprocess, "run") as run, self.assertRaises(mod.DispatcherError):
            mod.pico_action(self.cfg, row, "start", "objective")
        run.assert_not_called()

    def test_status_continues_after_target_credential_is_removed(self):
        _, target = self.configure_credentials()
        (target / "hosts.yml").unlink()
        row = {"checkout": "/tmp/target", "repo": "acme/tool", "execution_id": "id", "mode": "read-only"}
        response = SimpleNamespace(returncode=0, stdout='{"status":"exited"}', stderr="")
        with patch.dict(os.environ, {"GH_TOKEN": "ambient-secret"}), \
             patch.object(mod.subprocess, "run", return_value=response) as run:
            self.assertEqual(mod.pico_action(self.cfg, row, "status")["status"], "exited")
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("GH_CONFIG_DIR", run.call_args.kwargs["env"])

    def test_target_auth_failure_blocks_start(self):
        self.configure_credentials()
        row = {"checkout": "/tmp/target", "repo": "acme/tool", "execution_id": "id", "mode": "read-only"}
        with patch.object(mod.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run, \
             self.assertRaises(mod.DispatcherError):
            mod.pico_action(self.cfg, row, "start", "objective")
        self.assertEqual(run.call_count, 1)

    def test_target_repository_access_failure_blocks_start(self):
        self.configure_credentials()
        row = {"checkout": "/tmp/target", "repo": "acme/tool", "execution_id": "id", "mode": "read-only"}
        responses = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=1, stdout="{}")]
        with patch.object(mod.subprocess, "run", side_effect=responses) as run, \
             self.assertRaises(mod.DispatcherError):
            mod.pico_action(self.cfg, row, "start", "objective")
        self.assertEqual(run.call_count, 2)

    def test_credential_permissions_fail_closed(self):
        queue, target = self.configure_credentials()
        queue.chmod(0o644)
        with self.assertRaises(mod.DispatcherError):
            mod.queue_env(self.cfg)
        (target / "hosts.yml").chmod(0o644)
        with self.assertRaises(mod.DispatcherError):
            mod.target_env(self.cfg)
        (target / "hosts.yml").chmod(0o600)
        target.chmod(0o755)
        with self.assertRaises(mod.DispatcherError):
            mod.target_env(self.cfg)

    def test_incomplete_or_overlapping_configuration_fails(self):
        config = Path(self.tmp.name) / "config.json"
        data = {"queue_repository": "acme/queue", "targets": {"acme/tool": "/tmp/target"},
                "queue_token_file": str(Path(self.tmp.name) / "gh" / "token")}
        config.write_text(json.dumps(data))
        with self.assertRaises(mod.DispatcherError):
            mod.load_config(config)
        data["target_gh_config_dir"] = str(Path(self.tmp.name) / "gh")
        config.write_text(json.dumps(data))
        with self.assertRaises(mod.DispatcherError):
            mod.load_config(config)

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
