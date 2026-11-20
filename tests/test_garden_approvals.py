"""Garden approvals: the same approval core as the terminal, previewed, HMAC-confirmed and identity-bound."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import garden_approvals  # noqa: E402
import watch  # noqa: E402

disp = garden_approvals.issue_dispatcher  # the module object the Garden calls
QUEUE = "acme/private-queue"
HOST = "pico.tail1234.ts.net"
LOGIN = "dhuzard@github"


def issue(number=7, *, target="acme/tool", objective="Add validation tests.", title="Implement validation"):
    return {"number": number, "title": title, "state": "OPEN", "labels": [{"name": "pico:queued"}],
            "body": f"## Target repository\n{target}\n\n## Objective\n{objective}\n\n## Acceptance criteria\nTests pass.\n"}


class FakeQueue:
    """Stands in for the gh reads made with the queue credential."""

    def __init__(self):
        self.issues = {7: issue()}

    def view(self, cfg, number):
        if number not in self.issues:
            raise disp.DispatcherError("GitHub operation failed (exit 1): issue not found")
        return json.loads(json.dumps(self.issues[number]))

    def listing(self, cfg):
        return [json.loads(json.dumps(item)) for item in self.issues.values()]


class ApprovalsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.config("garden.sqlite3")
        self.queue = FakeQueue()
        for name, fake in (("issue_view", self.queue.view), ("queued_issues", self.queue.listing)):
            patcher = patch.object(disp, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.audit = self.root / "garden-approvals.jsonl"

    def config(self, state_name):
        return {"queue": QUEUE, "targets": {"acme/tool": "/tmp/target"}, "state": self.root / state_name,
                "pico": "pico", "gh": "gh", "poll": 10, "duration": 600, "calls": 10}

    def approvals(self, **kwargs):
        options = {"audit_path": self.audit, **kwargs}
        return garden_approvals.GardenApprovals(8765, self.cfg, **options)

    def row(self, cfg=None, number=7):
        with disp.database(cfg or self.cfg) as db:
            found = db.execute("SELECT * FROM approvals WHERE number=?", (number,)).fetchone()
            return dict(found) if found else None

    def set_state(self, state, number=7):
        task = disp.parse_task(self.queue.issues[number], self.cfg)
        with disp.database(self.cfg) as db:
            db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?,?,?,NULL)",
                       (number, task.digest, task.repo, task.checkout, task.execution_id, "read-only", state))
            db.commit()


class ApprovalCoreTest(ApprovalsBase):
    def preview(self, garden, mode="read-only", actor="local-token", number=7):
        return garden.act({"issue": number, "mode": mode}, actor)

    def confirm(self, garden, shown, mode="read-only", actor="local-token", number=7, **extra):
        return garden.act({"issue": number, "mode": mode, "confirm": shown["confirm"],
                           "digest": shown["preview"]["digest"], **extra}, actor)

    def test_cli_still_requires_a_terminal_and_the_typed_digest(self):
        task = disp.parse_task(issue(), self.cfg)
        with patch.object(disp.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(disp.DispatcherError, "interactive local terminal"):
                disp.approve(self.cfg, 7, mutate=False)
        with patch.object(disp.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="approve 000000000000"):
            with self.assertRaisesRegex(disp.DispatcherError, "approval cancelled"):
                disp.approve(self.cfg, 7, mutate=False)
        self.assertIsNone(self.row())
        with patch.object(disp.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="approve " + task.digest[:12]) as typed:
            disp.approve(self.cfg, 7, mutate=False)
        self.assertIn(task.digest[:12], typed.call_args[0][0])
        self.assertEqual(self.row()["state"], "approved")

    def test_garden_preview_then_confirm_writes_the_row_the_cli_writes(self):
        cli_cfg = self.config("cli.sqlite3")
        task = disp.parse_task(issue(), cli_cfg)
        with patch.object(disp.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="approve " + task.digest[:12]):
            disp.approve(cli_cfg, 7, mutate=True)
        garden = self.approvals()
        shown = self.preview(garden, "local-mutation")
        self.assertEqual((shown["ok"], shown["stage"]), (True, "preview"), shown)
        self.assertEqual({k: shown["preview"][k] for k in ("issue", "target", "checkout", "mode", "mode_label", "digest", "actor")},
                         {"issue": 7, "target": "acme/tool", "checkout": "/tmp/target", "mode": "local-mutation",
                          "mode_label": "LOCAL CODE MUTATION", "digest": task.digest, "actor": "local-token"})
        self.assertEqual(shown["preview"]["criteria"], "Tests pass.")
        self.assertIsNone(self.row(), "a preview records nothing")
        self.assertFalse(self.audit.exists(), "a preview is not an approval attempt")
        done = self.confirm(garden, shown, "local-mutation")
        self.assertEqual((done["ok"], done["stage"], done["status"]), (True, "done", "APPROVED"), done)
        self.assertEqual(self.row(), self.row(cli_cfg))

    def test_audit_line_records_time_issue_digest_mode_actor_and_result(self):
        garden = self.approvals()
        shown = self.preview(garden, actor=LOGIN)
        self.confirm(garden, shown, actor=LOGIN)
        [line] = self.audit.read_text().splitlines()
        entry = json.loads(line)
        self.assertEqual({k: entry[k] for k in ("issue", "digest", "mode", "actor", "stage", "ok", "error_code")},
                         {"issue": 7, "digest": shown["preview"]["digest"], "mode": "read-only", "actor": LOGIN,
                          "stage": "done", "ok": True, "error_code": None})
        self.assertRegex(entry["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("validation", line, "the audit never holds issue text")
        self.assertEqual(os.stat(self.audit).st_mode & 0o777, 0o600)

    def test_an_issue_edited_after_the_preview_is_refused_as_stale(self):
        garden = self.approvals()
        shown = self.preview(garden)
        self.queue.issues[7] = issue(objective="Delete the tests instead.")
        refused = self.confirm(garden, shown)
        self.assertEqual((refused["ok"], refused["error_code"]), (False, "CONTROL_PREVIEW_STALE"))
        self.assertIsNone(self.row())
        entry = json.loads(self.audit.read_text().splitlines()[-1])
        self.assertEqual((entry["ok"], entry["error_code"]), (False, "CONTROL_PREVIEW_STALE"))

    def test_another_mode_actor_or_digest_does_not_match_the_preview(self):
        garden = self.approvals()
        shown = self.preview(garden)
        self.assertEqual(self.confirm(garden, shown, mode="local-mutation")["error_code"], "CONTROL_PREVIEW_STALE")
        self.assertEqual(self.confirm(garden, shown, actor="someone@github")["error_code"], "CONTROL_PREVIEW_STALE")
        wrong = garden.act({"issue": 7, "mode": "read-only", "confirm": shown["confirm"], "digest": "0" * 64}, "local-token")
        self.assertEqual(wrong["error_code"], "CONTROL_PREVIEW_STALE", "the core checks the confirmed digest itself")
        other = garden.act({"issue": 7, "mode": "read-only", "confirm": "f" * 64, "digest": shown["preview"]["digest"]}, "local-token")
        self.assertEqual(other["error_code"], "CONTROL_PREVIEW_STALE")
        self.assertIsNone(self.row())

    def test_a_confirmation_is_single_use(self):
        garden = self.approvals()
        shown = self.preview(garden)
        self.assertTrue(self.confirm(garden, shown)["ok"])
        again = self.confirm(garden, shown)
        self.assertEqual((again["ok"], again["error_code"]), (False, "APPROVAL_ALREADY_SENT"))

    def test_a_dispatched_task_is_refused_at_preview_and_at_confirm(self):
        garden = self.approvals()
        shown = self.preview(garden)
        self.set_state("running")
        refused = self.confirm(garden, shown)
        self.assertEqual((refused["ok"], refused["error_code"]), (False, "APPROVAL_ALREADY_DISPATCHED"))
        self.assertEqual(self.preview(garden)["error_code"], "APPROVAL_ALREADY_DISPATCHED")
        self.assertEqual(self.row()["state"], "running", "never overwrites an execution state")

    def test_a_stale_approval_can_be_renewed_like_on_the_terminal(self):
        self.set_state("stale")
        garden = self.approvals()
        self.assertTrue(self.confirm(garden, self.preview(garden))["ok"])
        self.assertEqual(self.row()["state"], "approved")

    def test_a_target_off_the_allowlist_is_refused(self):
        self.queue.issues[7] = issue(target="evil/repo")
        refused = self.preview(self.approvals())
        self.assertEqual((refused["ok"], refused["error_code"]), (False, "APPROVAL_TASK_INVALID"))
        self.assertIn("allowlist", refused["error"])

    def test_repair_of_and_unknown_fields_are_refused_before_any_read(self):
        garden = self.approvals()
        with self.assertRaises(garden_approvals.ControlError) as caught:
            garden.act({"issue": 8, "mode": "local-mutation", "repair_of": 7}, "local-token")
        self.assertEqual(caught.exception.code, "APPROVAL_REPAIR_UNSUPPORTED")
        for bad in ({"issue": 7, "mode": "yolo"}, {"issue": "7", "mode": "read-only"}, {"issue": 7, "mode": "read-only", "x": 1},
                    {"issue": 7, "mode": "read-only", "confirm": "abc"}):
            with self.assertRaises(garden_approvals.ControlError) as caught:
                garden.act(bad, "local-token")
            self.assertEqual(caught.exception.code, "APPROVAL_INVALID", bad)
        disp.issue_view.assert_not_called()

    def test_listing_shows_sections_digest_state_and_parse_errors(self):
        self.queue.issues[8] = issue(8, target="evil/repo")
        self.queue.issues[9] = {**issue(9), "body": "no sections"}
        self.queue.issues[10] = issue(10)
        self.set_state("approved")
        self.set_state("completed", number=10)
        listing = self.approvals().listing("local-token")
        by_number = {item["number"]: item for item in listing["issues"]}
        task = disp.parse_task(issue(), self.cfg)
        self.assertEqual({k: by_number[7][k] for k in ("target", "objective", "criteria", "digest_short", "state",
                                                       "approved_current", "approvable", "error")},
                         {"target": "acme/tool", "objective": "Add validation tests.", "criteria": "Tests pass.",
                          "digest_short": task.digest[:12], "state": "approved", "approved_current": True,
                          "approvable": True, "error": None})
        self.assertEqual((by_number[8]["approvable"], by_number[8]["target"]), (False, "evil/repo"))
        self.assertIn("allowlist", by_number[8]["error"])
        self.assertEqual((by_number[9]["approvable"], by_number[9]["objective"]), (False, None))
        self.assertEqual((by_number[10]["state"], by_number[10]["approvable"]), ("completed", False))
        self.assertEqual(listing["queue"], QUEUE)


class ConfigurationTest(ApprovalsBase):
    def test_remote_host_without_a_required_login_refuses_to_start(self):
        with self.assertRaisesRegex(ValueError, "require-tailscale-login"):
            self.approvals(allowed_hosts=[HOST])
        argv = ["watch.py", "--all", "--approvals", "--dispatcher-config", str(self.root / "d.json"), "--allowed-host", HOST]
        with patch.object(sys, "argv", argv), patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            watch.main()
        self.assertEqual(caught.exception.code, 2)

    def test_flag_combinations_fail_closed(self):
        config = str(self.root / "d.json")
        for argv in (["--approvals"], ["--all", "--dispatcher-config", config],
                     ["--all", "--require-tailscale-login", LOGIN],
                     ["--all", "--token-file", str(self.root / "t")],
                     ["--all", "--approvals", "--dispatcher-config", config, "--require-tailscale-login", LOGIN,
                      "--allowed-host", "not a host"],
                     ["--workspace", "ws", "--approvals", "--dispatcher-config", config]):
            with patch.object(sys, "argv", ["watch.py", *argv]), patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                watch.main()
            self.assertEqual(caught.exception.code, 2, argv)

    def test_token_file_is_created_private_and_reused(self):
        path = self.root / "garden-token"
        first = garden_approvals.load_or_create_token(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(garden_approvals.load_or_create_token(path), first)
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            garden_approvals.load_or_create_token(path)
        os.chmod(path, 0o600)
        path.write_text("short\n")
        with self.assertRaisesRegex(ValueError, "at least 32"):
            garden_approvals.load_or_create_token(path)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            garden_approvals.load_or_create_token(link)
        with self.assertRaisesRegex(ValueError, "absolute"):
            garden_approvals.load_or_create_token(Path("relative-token"))


class ApprovalsEndpointTest(ApprovalsBase):
    def serve(self, garden, allowed_hosts=()):
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(
            self.root / "runs", None, approvals=garden, allowed_hosts=allowed_hosts))
        self.addCleanup(server.server_close)
        if garden is not None:
            garden.port = server.server_port
        return server

    def request(self, server, path="/api/approvals", *, body=None, headers=None):
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=data,
                                         method="POST" if data else "GET",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
        finally:
            thread.join(timeout=5)

    def remote(self):
        garden = self.approvals(allowed_hosts=[HOST], required_logins=[LOGIN])
        return garden, self.serve(garden, allowed_hosts=(HOST,))

    def test_approvals_off_has_no_endpoint(self):
        server = self.serve(None)
        code, body = self.request(server)
        self.assertEqual((code, body["error_code"]), (404, "APPROVALS_OFF"))
        code, body = self.request(server, body={"issue": 7, "mode": "read-only"},
                                  headers={"Origin": f"http://127.0.0.1:{server.server_port}"})
        self.assertEqual((code, body["error_code"]), (404, "APPROVALS_OFF"))

    def test_loopback_token_holder_is_the_local_actor(self):
        garden = self.approvals()
        server = self.serve(garden)
        origin = {"Origin": f"http://127.0.0.1:{server.server_port}"}
        code, body = self.request(server)
        self.assertEqual((code, body["error_code"]), (403, "CONTROL_TOKEN_INVALID"))
        code, body = self.request(server, headers={"X-Pico-Token": garden.token})
        self.assertEqual((code, body["actor"], body["issues"][0]["number"]), (200, "local-token", 7))
        code, body = self.request(server, body={"issue": 7, "mode": "read-only"}, headers=origin)
        self.assertEqual((code, body["error_code"]), (403, "CONTROL_TOKEN_INVALID"))
        code, shown = self.request(server, body={"issue": 7, "mode": "read-only"}, headers={"X-Pico-Token": garden.token, **origin})
        self.assertEqual((code, shown["stage"], shown["preview"]["actor"]), (200, "preview", "local-token"))
        code, done = self.request(server, body={"issue": 7, "mode": "read-only", "confirm": shown["confirm"],
                                                "digest": shown["preview"]["digest"]},
                                  headers={"X-Pico-Token": garden.token, **origin})
        self.assertEqual((code, done["stage"]), (200, "done"))
        self.assertEqual(self.row()["mode"], "read-only")

    def test_remote_requests_need_the_allowed_tailscale_login(self):
        garden, server = self.remote()
        remote = {"Host": HOST, "Origin": f"https://{HOST}", "X-Pico-Token": garden.token}
        for login in (None, "intruder@github", ""):
            headers = dict(remote) if login is None else {**remote, "Tailscale-User-Login": login}
            code, body = self.request(server, headers=headers)
            self.assertEqual((code, body["error_code"]), (403, "APPROVAL_IDENTITY_REJECTED"), login)
            code, body = self.request(server, body={"issue": 7, "mode": "read-only"}, headers=headers)
            self.assertEqual((code, body["error_code"]), (403, "APPROVAL_IDENTITY_REJECTED"), login)
        signed = {**remote, "Tailscale-User-Login": LOGIN}
        code, body = self.request(server, headers=signed)
        self.assertEqual((code, body["actor"]), (200, LOGIN))
        code, shown = self.request(server, body={"issue": 7, "mode": "local-mutation"}, headers=signed)
        self.assertEqual((code, shown["preview"]["actor"]), (200, LOGIN))
        code, done = self.request(server, body={"issue": 7, "mode": "local-mutation", "confirm": shown["confirm"],
                                                "digest": shown["preview"]["digest"]}, headers=signed)
        self.assertEqual((code, done["stage"], done["actor"]), (200, "done", LOGIN))
        self.assertEqual(json.loads(self.audit.read_text())["actor"], LOGIN)
        self.assertEqual(self.row()["mode"], "local-mutation")

    def test_a_loopback_request_with_a_required_login_still_needs_the_header(self):
        garden, server = self.remote()
        code, body = self.request(server, headers={"X-Pico-Token": garden.token})
        self.assertEqual((code, body["error_code"]), (403, "APPROVAL_IDENTITY_REJECTED"))

    def test_wrong_host_or_origin_is_refused(self):
        garden, server = self.remote()
        signed = {"X-Pico-Token": garden.token, "Tailscale-User-Login": LOGIN}
        for host in ("evil.example", f"{HOST}.evil.example", "pico.other.ts.net"):
            code, body = self.request(server, headers={**signed, "Host": host})
            self.assertEqual((code, body["error_code"]), (421, "HOST_REJECTED"), host)
        code, _ = self.request(server, headers={**signed, "Host": f"{HOST}:443"})
        self.assertEqual(code, 200)
        for origin in (None, "https://evil.example", f"http://{HOST}", f"https://{HOST}.evil.example"):
            headers = {**signed, "Host": HOST}
            if origin:
                headers["Origin"] = origin
            code, body = self.request(server, body={"issue": 7, "mode": "read-only"}, headers=headers)
            self.assertEqual((code, body["error_code"]), (403, "CONTROL_ORIGIN_REJECTED"), origin)
        self.assertIsNone(self.row())

    def test_allowed_host_does_not_widen_other_controls(self):
        # The existing workspace and feedback controls keep their loopback-only Origin check.
        import garden_controls
        self.assertFalse(garden_controls.origin_allowed(f"https://{HOST}", 8765))
        with self.assertRaises(garden_controls.ControlError):
            garden_controls.authorize_json({"Origin": f"https://{HOST}"}, b"{}", port=8765, token="t")


if __name__ == "__main__":
    unittest.main()
