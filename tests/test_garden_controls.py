#!/usr/bin/env python3
"""Garden controls over real HTTP: security gates, preview/confirm binding, and the
validate -> request PR -> CI -> promote flow on two real repositories."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import yaml
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import garden_controls as gc  # noqa: E402
import watch  # noqa: E402
import workspace_run as wr  # noqa: E402
from test_headless_run import git  # noqa: E402
import test_workspace_contracts as twc  # noqa: E402

COMMIT, GREEN, PENDING = twc.COMMIT, twc.GREEN, twc.PENDING


class GardenControlsTest(unittest.TestCase):
    # Borrow the two-repository fixture without rerunning the contract tests.
    setUp_fixture = twc.ContractLifecycleTest.setUp
    tearDown_fixture = twc.ContractLifecycleTest.tearDown
    manifest = twc.ContractLifecycleTest.manifest
    work = twc.ContractLifecycleTest.work
    allow_publication = twc.ContractLifecycleTest.allow_publication
    schedule = twc.ContractLifecycleTest.schedule

    def setUp(self):
        self.setUp_fixture()
        self.path = self.manifest()
        source = watch.WorkspaceSource(self.state, self.path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.state / "runs", None, source))
        self.port = self.server.server_port
        self.controls = gc.Controls(self.path, self.port, adapter=self.fake)
        self.server.RequestHandlerClass = watch.make_handler(self.state / "runs", None, source, self.controls, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tearDown_fixture()

    def request(self, method, path, body=None, *, host=None, origin="default", token="default", ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Pico-Token"] = self.controls.token if token == "default" else token
        if method == "POST":
            if origin:
                headers["Origin"] = f"http://127.0.0.1:{self.port}" if origin == "default" else origin
            if ctype:
                headers["Content-Type"] = ctype
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def act(self, **body):
        return self.request("POST", "/api/action", body)

    def test_security_gates(self):
        status, data = self.request("GET", "/api/workspace", host="evil.example:80")
        self.assertEqual((status, data["error_code"]), (421, "HOST_REJECTED"))
        status, _ = self.request("GET", "/api/workspace", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)
        body = {"action": "ci", "task": "a1"}
        for kwargs, expected in (({"token": None}, "CONTROL_TOKEN_INVALID"), ({"token": "x" * 43}, "CONTROL_TOKEN_INVALID"),
                                 ({"origin": None}, "CONTROL_ORIGIN_REJECTED"),
                                 ({"origin": "http://evil.example"}, "CONTROL_ORIGIN_REJECTED"),
                                 ({"ctype": "text/plain"}, "CONTROL_BODY_INVALID")):
            status, data = self.request("POST", "/api/action", body, **kwargs)
            self.assertIn(status, {403, 415})
            self.assertEqual(data["error_code"], expected)
        status, data = self.request("POST", "/api/action", body, host="evil.example:80")
        self.assertEqual(status, 421)
        status, data = self.act(action="merge", task="a1")
        self.assertEqual(data["error_code"], "CONTROL_ACTION_UNKNOWN")
        status, data = self.act(action="ci", task="../a1")
        self.assertEqual(data["error_code"], "CONTROL_TASK_INVALID")
        record = wr.load_workspace(wr.workspace_record_path("ws")) or {}
        self.assertEqual(record.get("garden_actions", []), [])

    def test_controls_reads_need_the_session_token(self):
        for path in ("/api/controls", "/api/branches?task=a1"):
            for token in (None, "x" * 43):
                status, data = self.request("GET", path, token=token)
                self.assertEqual((status, data["error_code"]), (403, "CONTROL_TOKEN_INVALID"), path)
                self.assertNotIn("repositories", data)
                self.assertNotIn("branches", data)
        status, data = self.request("GET", "/api/controls")
        self.assertEqual((status, data["enabled"]), (200, True))
        self.assertEqual(data["action_view"]["schema_version"], 1)
        self.assertEqual(len(data["action_view"]["state_id"]), 64)
        self.assertIn("required_permissions", data["action_view"]["actions"][0])
        status, data = self.request("GET", "/api/branches?task=a1")
        self.assertEqual(status, 200)
        # With controls on, the polled views need the token too (audit S4).
        for path in ("/api/workspace", "/api/runs", "/api/issues"):
            status, data = self.request("GET", path, token=None)
            self.assertEqual((status, data["error_code"]), (403, "CONTROL_TOKEN_INVALID"), path)
            self.assertNotIn("runs", data)
            self.assertNotIn("repositories", data)
        status, _ = self.request("GET", "/api/workspace")
        self.assertEqual(status, 200)
        status, data = self.request("GET", "/api/runs")
        self.assertEqual((status, data["workspace"]), (200, "ws"))

    def raw_post(self, content_length):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/action", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.putheader("Origin", f"http://127.0.0.1:{self.port}")
        conn.putheader("X-Pico-Token", self.controls.token)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", content_length)
        conn.endheaders()
        response = conn.getresponse()
        status, data = response.status, json.loads(response.read())
        conn.close()
        return status, data

    def test_malformed_content_length_is_a_json_400(self):
        for value in ("abc", "-5", "1e3", ""):
            status, data = self.raw_post(value)
            if value == "":
                # An empty header still reads as no body, like before.
                self.assertEqual(data["error_code"], "CONTROL_ACTION_UNKNOWN")
                continue
            self.assertEqual((status, data["ok"], data["error_code"]), (400, False, "CONTROL_BODY_INVALID"), value)

    def drop_policy(self, name: str) -> None:
        """Start a repository from the default policy (no committed .pi/autonomy.yaml), re-pinned."""
        path = self.repos[name][0]
        git("-C", str(path), "rm", "-q", "-r", ".pi")
        git("-C", str(path), *COMMIT, "no autonomy policy")
        self.repos[name] = (path, git("-C", str(path), "rev-parse", "HEAD"))
        self.manifest()

    def _victim(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        victim = outside / "autonomy.yaml"
        victim.write_text("victim: untouched\n")
        return outside, victim

    def test_permission_write_ignores_a_planted_temp_symlink(self):
        _, victim = self._victim()
        self.drop_policy("alpha")
        checkout = self.repos["alpha"][0]
        (checkout / ".pi").mkdir()
        (checkout / ".pi" / "autonomy.yaml.garden-tmp").symlink_to(victim)
        _, preview = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True)
        _, done = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True,
                           confirm=preview["confirm"])
        self.assertEqual(done["status"], "POLICY_UPDATED", done)
        self.assertEqual(victim.read_text(), "victim: untouched\n")
        policy = checkout / ".pi" / "autonomy.yaml"
        self.assertFalse(policy.is_symlink())
        self.assertIn("  push_branch: true", policy.read_text())
        self.assertEqual(sorted(p.name for p in (checkout / ".pi").iterdir()), ["autonomy.yaml", "autonomy.yaml.garden-tmp"])

    def test_permission_write_refuses_symlinked_policy_paths(self):
        outside, victim = self._victim()
        self.drop_policy("alpha")
        checkout = self.repos["alpha"][0]
        # A symlinked .pi directory: refused at preview (nothing read from outside) and at confirm.
        (checkout / ".pi").mkdir()
        _, preview = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True)
        (checkout / ".pi").rmdir()
        (checkout / ".pi").symlink_to(outside, target_is_directory=True)
        _, refused = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True,
                              confirm=preview["confirm"])
        self.assertEqual(refused["error_code"], "AUTONOMY_POLICY_PATH_UNSAFE", refused)
        _, refused = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True)
        self.assertEqual((refused["stage"], refused["error_code"]), ("refused", "AUTONOMY_POLICY_PATH_UNSAFE"))
        self.assertNotIn("victim", json.dumps(refused))
        self.assertEqual(victim.read_text(), "victim: untouched\n")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["autonomy.yaml"])
        # A symlinked policy file inside a real .pi directory.
        (checkout / ".pi").unlink()
        (checkout / ".pi").mkdir()
        (checkout / ".pi" / "autonomy.yaml").symlink_to(victim)
        _, refused = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True)
        self.assertEqual(refused["error_code"], "AUTONOMY_POLICY_PATH_UNSAFE")
        self.assertEqual(victim.read_text(), "victim: untouched\n")

    def test_actions_are_off_without_controls(self):
        source = watch.WorkspaceSource(self.state, self.path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.state / "runs", None, source))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            conn.request("POST", "/api/action", body=b"{}", headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())["error_code"]), (404, "CONTROLS_OFF"))
            conn.request("GET", "/api/controls")
            panels = json.loads(conn.getresponse().read())
            self.assertEqual((panels["ok"], panels["enabled"], panels["actions"]), (True, False, []))
        finally:
            server.shutdown()
            server.server_close()

    def test_panels_show_policy_and_models_read_only(self):
        self.drop_policy("alpha")
        self.allow_publication("beta")
        status, data = self.request("GET", "/api/controls")
        self.assertEqual((status, data["enabled"]), (200, True))
        repos = {r["id"]: r for r in data["repositories"]}
        self.assertTrue(repos["alpha"]["source"].startswith("default"))
        self.assertFalse(repos["alpha"]["permissions"]["create_pr"])
        self.assertEqual(repos["beta"]["source"], ".pi/autonomy.yaml")
        self.assertTrue(repos["beta"]["permissions"]["create_pr"] and repos["beta"]["permissions"]["push_branch"])
        self.assertFalse(repos["beta"]["merge_enabled"])
        self.assertEqual((repos["alpha"]["issue_reference"], repos["beta"]["issue_reference"]), ("refs", "refs"))
        self.assertIn("claude", data["models"]["models"])
        self.assertNotIn("Interoperable claims", json.dumps(data))  # the workspace goal is never served

    def test_repin_after_bounded_policy_commit_changes_one_manifest_pin(self):
        self.drop_policy("alpha")
        before = self.path.read_text()
        old = self.repos["alpha"][1]
        _, panels = self.request("GET", "/api/controls")
        alpha = next(row for row in panels["repositories"] if row["id"] == "alpha")
        self.assertEqual(alpha["effective"]["policy_source"], "default")
        self.assertEqual(set(alpha["effective"]["missing_local_permissions"]), {"create_worktrees", "commit"})
        self.assertEqual(alpha["effective"]["publication"]["status"], "not_checked")
        approve_action = next(row for row in panels["action_view"]["actions"]
                              if row["action"] == "approve_mutation" and row["target"] == "alpha")
        self.assertTrue(approve_action["available"], approve_action)
        _, approval = self.act(action="approve_mutation", repository="alpha")
        self.assertEqual(approval["stage"], "preview")
        _, updated = self.act(action="approve_mutation", repository="alpha", confirm=approval["confirm"])
        self.assertEqual(updated["status"], "POLICY_UPDATED")
        _, commit_preview = self.act(action="commit_policy", repository="alpha")
        _, committed = self.act(action="commit_policy", repository="alpha", confirm=commit_preview["confirm"])
        head = committed["commit"]
        self.assertNotEqual(head, old)
        _, panels = self.request("GET", "/api/controls")
        alpha = next(row for row in panels["repositories"] if row["id"] == "alpha")
        self.assertEqual(alpha["effective"]["pinned_commit"], old)
        self.assertEqual(alpha["effective"]["working_head"], head)
        self.assertEqual(alpha["effective"]["policy_source"], "default")
        approve_action = next(row for row in panels["action_view"]["actions"]
                              if row["action"] == "approve_mutation" and row["target"] == "alpha")
        self.assertEqual((approve_action["available"], approve_action["refusal_code"]), (False, "POLICY_UNCHANGED"))
        action = next(row for row in panels["action_view"]["actions"]
                      if row["action"] == "repin_manifest" and row["target"] == "alpha")
        self.assertTrue(action["available"], action)
        _, preview = self.act(action="repin_manifest", repository="alpha")
        self.assertEqual((preview["stage"], preview["preview"]["old_pin"], preview["preview"]["new_pin"]),
                         ("preview", old, head))
        self.assertIn(f"-  pinned_commit: {old}", preview["preview"]["diff"])
        self.assertIn(f"+  pinned_commit: {head}", preview["preview"]["diff"])
        self.path.write_text(before + "# concurrent edit\n")
        _, stale = self.act(action="repin_manifest", repository="alpha", confirm=preview["confirm"])
        self.assertEqual(stale["error_code"], "CONTROL_PREVIEW_STALE")
        self.path.write_text(before)
        _, preview = self.act(action="repin_manifest", repository="alpha")
        _, done = self.act(action="repin_manifest", repository="alpha", confirm=preview["confirm"])
        self.assertEqual((done["status"], done["pinned_commit"]), ("MANIFEST_REPINNED", head))
        old_doc = yaml.safe_load(before)
        new_doc = yaml.safe_load(self.path.read_text())
        self.assertEqual(new_doc["repositories"][0]["pinned_commit"], head)
        self.assertEqual(new_doc["repositories"][1], old_doc["repositories"][1])
        self.assertEqual(new_doc["tasks"], old_doc["tasks"])
        _, repeated = self.act(action="repin_manifest", repository="alpha", confirm=preview["confirm"])
        self.assertEqual(repeated["error_code"], "WORKSPACE_PIN_UNCHANGED")

    def test_repin_refuses_existing_work_and_symlinked_manifest(self):
        self.schedule(self.path)
        checkout = self.repos["alpha"][0]
        policy = checkout / ".pi" / "autonomy.yaml"
        policy.write_text(policy.read_text() + "# policy review\n")
        git("-C", str(checkout), "add", ".pi/autonomy.yaml")
        git("-C", str(checkout), *COMMIT, "policy review")
        before = self.path.read_text()
        _, refused = self.act(action="repin_manifest", repository="alpha")
        self.assertEqual(refused["error_code"], "WORKSPACE_REPIN_TASK_STARTED")
        self.assertEqual(self.path.read_text(), before)
        original = self.path.with_suffix(".original.yaml")
        self.path.rename(original)
        self.path.symlink_to(original)
        _, refused = self.act(action="repin_manifest", repository="alpha")
        self.assertEqual(refused["error_code"], "WORKSPACE_MANIFEST_PATH_UNSAFE")
        self.assertEqual(original.read_text(), before)

    def test_repin_refuses_unrelated_committed_change(self):
        checkout = self.repos["alpha"][0]
        (checkout / "README.md").write_text("new version\n")
        git("-C", str(checkout), "add", "README.md")
        git("-C", str(checkout), *COMMIT, "new version")
        before = self.path.read_text()
        _, refused = self.act(action="repin_manifest", repository="alpha")
        self.assertEqual(refused["error_code"], "WORKSPACE_REPIN_SCOPE_CHANGED")
        self.assertEqual(self.path.read_text(), before)

    def test_repin_refuses_branch_move_during_manifest_save(self):
        self.drop_policy("alpha")
        before = self.path.read_text()
        _, approval = self.act(action="approve_mutation", repository="alpha")
        self.act(action="approve_mutation", repository="alpha", confirm=approval["confirm"])
        _, commit = self.act(action="commit_policy", repository="alpha")
        self.act(action="commit_policy", repository="alpha", confirm=commit["confirm"])
        _, preview = self.act(action="repin_manifest", repository="alpha")
        checkout = self.repos["alpha"][0]
        write = gc.atomic_io.atomic_write_text

        def move_branch(path, content, **kwargs):
            if Path(path) == self.path:
                git("-C", str(checkout), "checkout", "-q", "-b", "moved")
            return write(path, content, **kwargs)

        with patch.object(gc.atomic_io, "atomic_write_text", side_effect=move_branch):
            _, refused = self.act(action="repin_manifest", repository="alpha", confirm=preview["confirm"])
        self.assertEqual(refused["error_code"], "CONTROL_PREVIEW_STALE")
        self.assertEqual(self.path.read_text(), before)

    def test_bounded_approval_refuses_repository_without_mutating_task(self):
        self.drop_policy("alpha")
        manifest = yaml.safe_load(self.path.read_text())
        next(task for task in manifest["tasks"] if task["id"] == "a1")["mutation"] = "none"
        self.path.write_text(yaml.safe_dump(manifest))
        policy = self.repos["alpha"][0] / ".pi" / "autonomy.yaml"
        self.assertFalse(policy.exists())
        _, result = self.act(action="approve_mutation", repository="alpha")
        self.assertEqual((result["stage"], result["error_code"]), ("refused", "MUTATION_APPROVAL_TASKS_NONE"))
        _, panels = self.request("GET", "/api/controls")
        row = next(row for row in panels["action_view"]["actions"]
                   if row["action"] == "approve_mutation" and row["target"] == "alpha")
        self.assertEqual((row["available"], row["refusal_code"]), (False, "MUTATION_APPROVAL_TASKS_NONE"))
        self.assertFalse(policy.exists())

    def test_validate_publish_ci_promote_flow(self):
        self.schedule(self.path)
        commit = self.work("alpha", "pico/a1", "claims.json")
        status, data = self.request("GET", "/api/branches?task=a1")
        self.assertEqual(data["branches"], [{"name": "pico/a1", "commit": commit, "descends_from_pin": True}])

        _, preview = self.act(action="validate", task="a1", head="pico/a1")
        self.assertEqual((preview["stage"], preview["preview"]["commit"]), ("preview", commit))
        _, tampered = self.act(action="validate", task="a1", head="pico/a1", confirm="0" * 64)
        self.assertEqual(tampered["error_code"], "CONTROL_PREVIEW_STALE")
        _, done = self.act(action="validate", task="a1", head="pico/a1", confirm=preview["confirm"])
        self.assertEqual((done["stage"], done["status"]), ("done", "VALIDATED"))

        _, refused = self.act(action="publish", task="a1")
        self.assertEqual((refused["stage"], refused["error_code"]), ("refused", "PUBLICATION_NOT_AUTHORIZED"))
        self.allow_publication("alpha")
        _, preview = self.act(action="publish", task="a1")
        self.assertEqual(preview["stage"], "preview")
        self.assertEqual((preview["preview"]["head"], preview["preview"]["draft"], preview["preview"]["mutations_planned"]),
                         ("pico/a1", True, 2))
        self.assertIn("Refs #7", preview["preview"]["body"])
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "for-each-ref"), "")
        _, published = self.act(action="publish", task="a1", confirm=preview["confirm"])
        self.assertEqual((published["status"], published["pr"], published["url"]),
                         ("COMPLETED", 101, "https://github.com/acme/alpha/pull/101"))
        self.assertTrue(self.fake.created[0]["draft"])

        self.fake.checks[101] = ("OPEN", commit, PENDING)
        _, ci = self.act(action="ci", task="a1")
        self.assertEqual((ci["ok"], ci["ci"]["status"]), (False, "pending"))
        _, preview = self.act(action="promote", task="a1")
        self.assertEqual((preview["stage"], preview["ok"]), ("preview", False))
        self.fake.checks[101] = ("OPEN", commit, GREEN)
        _, stale = self.act(action="promote", task="a1", confirm=preview["confirm"])
        self.assertEqual(stale["error_code"], "CONTROL_PREVIEW_STALE")  # CI changed since the preview
        _, preview = self.act(action="promote", task="a1")
        _, promoted = self.act(action="promote", task="a1", confirm=preview["confirm"])
        self.assertEqual(promoted["readiness"], "ready_for_downstream")

        log = wr.load_workspace(wr.workspace_record_path("ws"))["garden_actions"]
        self.assertEqual([(e["action"], e["stage"], e["ok"]) for e in log],
                         [("validate", "refused", False), ("validate", "done", True), ("publish", "refused", False),
                          ("publish", "done", True), ("ci", "done", False), ("promote", "refused", False),
                          ("promote", "done", True)])

    def test_moved_head_after_preview_is_refused(self):
        self.schedule(self.path)
        self.work("alpha", "pico/a1", "claims.json")
        _, preview = self.act(action="validate", task="a1", head="pico/a1")
        path, _ = self.repos["alpha"]
        git("-C", str(path), "checkout", "-q", "pico/a1")
        (path / "late.txt").write_text("x\n")
        git("-C", str(path), "add", "late.txt")
        git("-C", str(path), *COMMIT, "moved after preview")
        git("-C", str(path), "checkout", "-q", "main")
        _, result = self.act(action="validate", task="a1", head="pico/a1", confirm=preview["confirm"])
        self.assertEqual(result["error_code"], "CONTROL_PREVIEW_STALE")

    def test_one_action_at_a_time(self):
        self.controls._lock.acquire()
        try:
            status, data = self.act(action="ci", task="a1")
        finally:
            self.controls._lock.release()
        self.assertEqual((status, data["error_code"]), (409, "CONTROL_BUSY"))

    # -- slice 2 -------------------------------------------------------------
    def validated_a1(self):
        self.schedule(self.path)
        commit = self.work("alpha", "pico/a1", "claims.json")
        _, preview = self.act(action="validate", task="a1", head="pico/a1")
        self.act(action="validate", task="a1", head="pico/a1", confirm=preview["confirm"])
        return commit

    def test_publication_has_no_one_shot_grant(self):
        # Removed (#59): whoever holds the session token, an agent included, could confirm a grant.
        self.validated_a1()
        policy = (self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text()
        _, refused = self.act(action="publish", task="a1")
        self.assertEqual((refused["stage"], refused["error_code"]), ("refused", "PUBLICATION_NOT_AUTHORIZED"))
        self.assertNotIn("grantable", refused)
        self.assertIn("merge it into the default branch", refused["error"])
        for confirm in (None, "0" * 64):
            with self.subTest(confirm=confirm):
                _, granted = self.act(action="publish", task="a1", grant_once=True, confirm=confirm)
                self.assertEqual((granted["stage"], granted["error_code"]), ("refused", "CONTROL_BODY_INVALID"))
        self.assertEqual(self.fake.created, [])
        self.assertEqual((self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text(), policy)

    def test_policy_publication_records_policy_authorization(self):
        commit = self.validated_a1()
        policy = (REPO_ROOT / "templates/autonomy.yaml").read_text().replace(
            "push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        self.fake.merge_policy("acme/alpha", policy)
        _, preview = self.act(action="publish", task="a1")
        self.assertEqual((preview["stage"], preview["preview"]["authorization"]), ("preview", "policy"))
        _, done = self.act(action="publish", task="a1", confirm=preview["confirm"])
        self.assertEqual((done["status"], done["pr"]), ("COMPLETED", 101))
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1"), commit)
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        run_id = record["tasks"]["a1"]["observed"]["pr"]["publication_run_id"]
        publication = json.loads((self.state / "runs" / f"{run_id}.json").read_text())
        authorization = publication["github_integration"]["authorization"]
        self.assertEqual((authorization["source"], authorization["permissions"]), ("policy", ["push_branch", "create_pr"]))
        self.assertEqual(record["garden_actions"][-1]["authorization"], "policy")

    def test_garden_toggle_does_not_authorize_publication_until_it_reaches_the_base(self):
        self.validated_a1()
        alpha = self.repos["alpha"][0]
        git("-C", str(alpha), "config", "user.name", "t")  # CI runners have no global identity
        git("-C", str(alpha), "config", "user.email", "t@example.com")
        for permission in ("push_branch", "create_pr"):
            _, preview = self.act(action="set_permission", repository="alpha", permission=permission, value=True)
            self.assertIn("no effect on them until it is committed and pushed", preview["preview"]["note"])
            self.act(action="set_permission", repository="alpha", permission=permission, value=True,
                     confirm=preview["confirm"])
        _, refused = self.act(action="publish", task="a1")  # the toggled copy is uncommitted
        self.assertEqual((refused["stage"], refused["error_code"]), ("refused", "PUBLICATION_NOT_AUTHORIZED"))
        _, committed = self.act(action="commit_policy", repository="alpha")
        _, done = self.act(action="commit_policy", repository="alpha", confirm=committed["confirm"])
        self.assertTrue(done["ok"], done)
        # Committed locally, but GitHub's base branch still has no policy that allows it.
        _, refused = self.act(action="publish", task="a1")
        self.assertEqual((refused["stage"], refused["error_code"]), ("refused", "PUBLICATION_NOT_AUTHORIZED"))
        self.assertEqual(self.fake.created, [])
        self.fake.merge_policy("acme/alpha", (self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text())
        _, preview = self.act(action="publish", task="a1")  # pushed to the base: now authorized without a grant
        self.assertEqual((preview["stage"], preview["preview"]["authorization"]), ("preview", "policy"))

    def test_permission_toggle_writes_a_reviewed_policy_diff(self):
        self.drop_policy("alpha")
        _, preview = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True)
        self.assertEqual((preview["stage"], preview["preview"]["creates_file"]), ("preview", True))
        self.assertIn("+  push_branch: true", preview["preview"]["diff"])
        _, done = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True,
                           confirm=preview["confirm"])
        self.assertEqual(done["status"], "POLICY_UPDATED")
        policy = (self.repos["alpha"][0] / ".pi" / "autonomy.yaml").read_text()
        self.assertIn("  push_branch: true", policy)
        self.assertIn("  create_pr: false", policy)
        self.assertIn("# Project-local execution authority policy", policy)  # comments preserved
        _, panels = self.request("GET", "/api/controls")
        alpha = next(r for r in panels["repositories"] if r["id"] == "alpha")
        self.assertTrue(alpha["permissions"]["push_branch"] and alpha["policy_uncommitted"])
        for locked in ("merge", "deploy", "mark_pr_ready"):
            _, data = self.act(action="set_permission", repository="alpha", permission=locked, value=True)
            self.assertEqual(data["error_code"], "CONTROL_PERMISSION_LOCKED")
        _, preview = self.act(action="set_permission", repository="alpha", permission="create_pr", value=True)
        path = self.repos["alpha"][0] / ".pi" / "autonomy.yaml"
        path.write_text(path.read_text().replace("commit: true", "commit: false"))
        _, stale = self.act(action="set_permission", repository="alpha", permission="create_pr", value=True,
                            confirm=preview["confirm"])
        self.assertEqual(stale["error_code"], "CONTROL_PREVIEW_STALE")
        _, unknown = self.act(action="set_permission", repository="gamma", permission="commit", value=True)
        self.assertEqual(unknown["error_code"], "CONTROL_REPOSITORY_INVALID")

    def test_schedule_runs_in_the_background_and_cancel_stops_a_child(self):
        self.controls.pi_executable = str(self.pi)
        _, preview = self.act(action="schedule", allow_mutation=True)
        self.assertEqual((preview["preview"]["dispatch"], preview["preview"]["cap"]), (["a1"], 2))
        _, started = self.act(action="schedule", allow_mutation=True, confirm=preview["confirm"])
        self.assertEqual(started["status"], "SCHEDULER_STARTED")
        _, busy = self.act(action="schedule", allow_mutation=True)
        self.assertIn(busy.get("error_code"), {"CONTROL_BUSY", None})
        self.controls._scheduler.join(timeout=60)
        self.assertEqual(self.controls.last_schedule["status"], "finished")
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        self.assertEqual(record["tasks"]["a1"]["run_status"], "PARTIAL")
        started_entry = next(e for e in record["garden_actions"] if e["stage"] == "started")
        self.assertEqual((started_entry["dispatch"], started_entry["allow_mutation"]), (["a1"], True))
        _, nothing = self.act(action="cancel", task="a1")
        self.assertEqual(nothing["error_code"], "CONTROL_NOTHING_TO_CANCEL")

        os.environ["FAKE_PI_BEHAVIOR"] = "hang"
        self.manifest(a1={"readiness": "ready_for_downstream"})
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        record["tasks"]["a1"]["observed"] = {"readiness": "ready_for_downstream", "validated_contracts": {"claims": "1.0"}}
        with wr.locked_workspace("ws"):
            wr.save_workspace(wr.workspace_record_path("ws"), record)
        wr.launch(manifest=self.path, task_id="b1", access_check=None, pi_executable=str(self.pi), wait=False,
                  allow_mutation=True)
        import time
        deadline = time.monotonic() + 10
        while wr.status(manifest=self.path, task_id="b1", access_check=None)["tasks"]["b1"].get("run_id") is None \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        _, preview = self.act(action="cancel", task="b1")
        self.assertEqual(preview["stage"], "preview")
        _, cancelled = self.act(action="cancel", task="b1", confirm=preview["confirm"])
        self.assertEqual((cancelled["status"], cancelled["error_code"]), ("cancelled", "EXECUTION_CANCELLED"))

    def test_model_set_and_reset_through_the_user_config(self):
        # A fresh user config directory: the default models.yaml does not exist until a model is set.
        os.environ.pop("PI_ORCHESTRATOR_MODEL_CONFIG", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.base / "config")
        config = self.base / "config" / "pi-coding-orchestrator" / "models.yaml"
        _, panels = self.request("GET", "/api/controls")
        models = panels["models"]
        default = models["models"]["codex"]["light"]
        other = next(c for c in models["choices"]["codex"] if c["model"] != default)
        capacity = max(other["capacity"], models["floors"]["light"])
        _, preview = self.act(action="set_model", family="codex", tier="light", model=other["model"], capacity=capacity)
        self.assertEqual((preview["preview"]["current"]["model"], preview["preview"]["new"]["model"]), (default, other["model"]))
        _, done = self.act(action="set_model", family="codex", tier="light", model=other["model"], capacity=capacity,
                           confirm=preview["confirm"])
        self.assertEqual(done["status"], "MODEL_UPDATED")
        self.assertTrue(config.is_file())
        _, panels = self.request("GET", "/api/controls")
        self.assertEqual(panels["models"]["models"]["codex"]["light"], other["model"])
        _, refused = self.act(action="set_model", family="codex", tier="light", model="vendor/unknown-model", capacity=5)
        self.assertEqual(refused["error_code"], "MODEL_CHANGE_INVALID")
        _, preview = self.act(action="reset_model", family="codex", tier="light")
        _, reset = self.act(action="reset_model", family="codex", tier="light", confirm=preview["confirm"])
        self.assertEqual(reset["status"], "MODEL_RESET")
        _, panels = self.request("GET", "/api/controls")
        self.assertEqual(panels["models"]["models"]["codex"]["light"], default)

    # -- slice 3 -------------------------------------------------------------
    def test_commit_policy_commits_only_the_policy_on_the_base_branch(self):
        alpha = self.repos["alpha"][0]
        _, data = self.act(action="commit_policy", repository="alpha")
        self.assertEqual(data["error_code"], "POLICY_UNCHANGED")
        _, preview = self.act(action="set_permission", repository="alpha", permission="push_branch", value=True)
        self.act(action="set_permission", repository="alpha", permission="push_branch", value=True, confirm=preview["confirm"])
        (alpha / "unrelated.txt").write_text("not part of the policy commit\n")
        git("-C", str(alpha), "checkout", "-q", "-b", "pico/work")
        _, wrong = self.act(action="commit_policy", repository="alpha")
        self.assertEqual(wrong["error_code"], "POLICY_COMMIT_BRANCH")
        git("-C", str(alpha), "checkout", "-q", "main")
        _, preview = self.act(action="commit_policy", repository="alpha")
        self.assertEqual((preview["stage"], preview["preview"]["branch"]), ("preview", "main"))
        self.assertIn("push_branch: true", preview["preview"]["diff"])
        git("-C", str(alpha), "config", "user.name", "t")
        git("-C", str(alpha), "config", "user.email", "t@example.com")
        _, done = self.act(action="commit_policy", repository="alpha", confirm=preview["confirm"])
        self.assertEqual(done["status"], "POLICY_COMMITTED", done)
        self.assertEqual(git("-C", str(alpha), "show", "--name-only", "--format=%s", "HEAD").splitlines(),
                         ["Update PICO autonomy policy", "", ".pi/autonomy.yaml"])
        self.assertEqual(git("-C", str(alpha), "status", "--porcelain"), "?? unrelated.txt")
        self.assertEqual(git("-C", str(alpha), "for-each-ref", "refs/remotes"), "")  # nothing pushed
        _, panels = self.request("GET", "/api/controls")
        self.assertFalse(next(r for r in panels["repositories"] if r["id"] == "alpha")["policy_uncommitted"])

    def test_budget_is_reported_and_blocks_schedule_preview(self):
        import yaml
        data = yaml.safe_load(self.path.read_text())
        data["budget"] = {"max_tokens": 1000}
        self.path.write_text(yaml.safe_dump(data))
        os.environ["FAKE_PI_USAGE"] = "700,400"
        self.schedule(self.path)
        _, view = self.request("GET", "/api/workspace")
        self.assertEqual((view["budget"]["spent_tokens"], view["budget"]["max_tokens"], view["budget"]["exhausted"]),
                         (1100, 1000, True))
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        record["tasks"]["a1"]["observed"] = {"readiness": "ready_for_downstream", "validated_contracts": {"claims": "1.0"}}
        with wr.locked_workspace("ws"):
            wr.save_workspace(wr.workspace_record_path("ws"), record)
        _, preview = self.act(action="schedule", allow_mutation=True)
        self.assertEqual(preview["preview"]["dispatch"], ["b1"])
        self.assertTrue(preview["preview"]["budget"]["exhausted"])
        self.assertFalse(preview["ok"])  # nothing may launch while the budget is exhausted


if __name__ == "__main__":
    unittest.main()
