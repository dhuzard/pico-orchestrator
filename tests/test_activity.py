"""Activity log, send-only Telegram notices and the Garden's /api/activity endpoint."""
import contextlib
import io
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
import activity  # noqa: E402
import garden_approvals  # noqa: E402
import watch  # noqa: E402

TOKEN = "123456789:" + "A" * 35
HOST = "pico.tail1234.ts.net"
LOGIN = "dhuzard@github"


class Answer:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_):
        return json.dumps(self.payload).encode()


class ActivityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.telegram = self.root / "telegram.env"
        self.telegram.write_text(f"# bot\nPICO_TELEGRAM_BOT_TOKEN={TOKEN}\nPICO_TELEGRAM_CHAT_ID=8625158313\n")
        self.telegram.chmod(0o600)
        self.cfg = {"state": self.root / "issue-dispatcher.sqlite3", "notify": {"telegram_env_file": self.telegram}}
        self.log = self.root / "activity.jsonl"


class LogTest(ActivityBase):
    def test_append_is_private_and_recent_is_newest_first(self):
        for n in range(60):
            activity.emit(self.cfg, "merged", f"Merged #{n}", "summary", "https://github.com/acme/tool/pull/1",
                          pr=n + 1, sender=lambda *_: None)
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600)
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write("not json\n" + json.dumps({"at": "x", "kind": "unknown", "title": "t"}) + "\n")
            handle.write(json.dumps({"at": "2026-11-20T10:00:00Z", "kind": "deploy", "title": "Deployed",
                                     "summary": "ok", "url": "javascript:alert(1)", "extra": "dropped"}) + "\n")
        events = activity.recent(self.log, 50)
        self.assertEqual(len(events), 50)
        self.assertEqual(events[0], {"at": "2026-11-20T10:00:00Z", "kind": "deploy", "title": "Deployed",
                                     "summary": "ok", "url": None})
        self.assertEqual(events[1]["title"], "Merged #59")
        self.assertEqual(events[-1]["title"], "Merged #11")
        self.assertEqual(activity.recent(self.root / "missing.jsonl"), [])

    def test_events_are_validated(self):
        with self.assertRaises(activity.ActivityError):
            activity.event("rm -rf", "x")
        with self.assertRaises(activity.ActivityError):
            activity.event("deploy", "  ")
        entry = activity.event("deploy", "a\x00b" + "t" * 500, "s", "https://user:pw@example.com/x", sha="z" * 40)
        self.assertEqual((len(entry["title"]), entry["url"]), (200, None))
        self.assertNotIn("sha", entry)
        self.assertNotIn("\x00", entry["title"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(activity.emit(self.cfg, "nope", "x", sender=lambda *_: None))
        self.assertFalse(self.log.exists())


class TelegramTest(ActivityBase):
    def test_send_only_message_with_title_summary_and_url(self):
        requests = []

        def opener(request, timeout=None):
            requests.append(request)
            return Answer({"ok": True, "result": {}})
        entry = activity.emit(self.cfg, "audit_failed", "Audit failed: acme/tool#42", "A criterion is not met.",
                              "https://github.com/acme/tool/pull/42", pr=42,
                              sender=lambda path, text: activity.send_telegram(path, text, opener=opener))
        self.assertEqual(entry["kind"], "audit_failed")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/sendMessage"))
        self.assertNotIn("getUpdates", request.full_url)
        body = json.loads(request.data)
        self.assertEqual(body["chat_id"], "8625158313")
        self.assertEqual(body["text"], "⛔ Audit failed: acme/tool#42\nA criterion is not met.\nhttps://github.com/acme/tool/pull/42")
        self.assertNotIn("parse_mode", body)
        source = (Path(activity.__file__)).read_text()
        self.assertNotIn("getUpdates(", source.replace("never calls `getUpdates`", ""))

    def test_a_failed_notice_never_blocks_and_never_logs_the_token(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            entry = activity.emit(self.cfg, "deploy", "Deployed", "ok",
                                  sender=lambda path, text: activity.send_telegram(path, text, opener=opener))
            activity.emit(self.cfg, "deploy", "Deployed again", sender=lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(entry["title"], "Deployed")
        self.assertEqual([e["title"] for e in activity.recent(self.log)], ["Deployed again", "Deployed"])
        self.assertIn("HTTP 401", stderr.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())
        self.assertNotIn("AAAA", stderr.getvalue())

    def test_the_token_file_must_be_private(self):
        self.telegram.chmod(0o644)
        sent = []
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ok = activity.notify(self.cfg, activity.event("deploy", "x"),
                                 sender=lambda path, text: activity.send_telegram(path, text, opener=lambda *a, **k: sent.append(1)))
        self.assertFalse(ok)
        self.assertEqual(sent, [])
        self.assertIn("mode 0600", stderr.getvalue())
        link = self.root / "link.env"
        link.symlink_to(self.telegram)
        self.telegram.chmod(0o600)
        with self.assertRaises(activity.ActivityError):
            activity.read_private_env(link)
        self.assertFalse(activity.notify({"state": self.cfg["state"]}, activity.event("deploy", "x")), "no notify config")

    def test_cli_emit_appends_and_notifies(self):
        config = self.root / "dispatcher.json"
        config.write_text(json.dumps({"queue_repository": "acme/queue", "targets": {"acme/tool": "/tmp/t"},
                                      "state_path": str(self.cfg["state"]),
                                      "notify": {"telegram_env_file": str(self.telegram)}}))
        sent = []
        with patch.object(activity, "send_telegram", side_effect=lambda path, text: sent.append((path, text))), \
                contextlib.redirect_stdout(io.StringIO()):
            code = activity.main(["emit", "--config", str(config), "--kind", "deploy", "--title", "Deployed abc1234",
                                  "--summary", "Pico abc1234 is live.", "--url", "https://github.com/acme/tool/pull/9"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, [(self.telegram, "🚀 Deployed abc1234\nPico abc1234 is live.\nhttps://github.com/acme/tool/pull/9")])
        self.assertEqual(activity.recent(self.log)[0]["title"], "Deployed abc1234")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(activity.main(["emit", "--config", str(config), "--kind", "deploy", "--title", "x",
                                            "--url", "http://insecure.example"]), 1)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            activity.main(["emit", "--config", str(config), "--kind", "other", "--title", "x"])


GARDEN = "https://pico-pilot.tail35e544.ts.net/"


class GardenLinkTest(ActivityBase):
    def test_garden_url_must_be_plain_https(self):
        self.assertEqual(activity.garden_url(GARDEN), GARDEN)
        self.assertEqual(activity.garden_url("https://pico.example:8443/garden"), "https://pico.example:8443/garden")
        for bad in ("http://pico.example/", "https://pico.example/#token=" + "a" * 30, "https://pico.example/#approvals",
                    "https://pico.example/?token=abc", "https://pico.example/?", "https://pico.example/#",
                    "https://user:pw@pico.example/", "https://user@pico.example/", "javascript:alert(1)", "https:///x",
                    "https://pico.example/ x", "https://pico.example:99999/", "", None, 42, "https://" + "a" * 300):
            with self.assertRaises(activity.ActivityError, msg=bad):
                activity.garden_url(bad)

    def notices(self, garden=GARDEN, **kwargs):
        sent = []
        cfg = {**self.cfg, "notify": {**self.cfg["notify"], **({"garden_url": garden} if garden else {})}}
        sender = lambda path, text: sent.append(text)  # noqa: E731
        for kind in activity.KINDS:
            activity.emit(cfg, kind, f"{kind} event", "summary", "https://github.com/acme/tool/pull/4", sender=sender)
        activity.emit(cfg, "run_finished", "Run blocked: queue #7", "s", sender=sender, garden=True)
        activity.emit(cfg, "run_finished", "Run completed: queue #8", "s", sender=sender, garden=False)
        return sent

    def test_the_garden_link_is_added_only_where_there_is_something_to_do(self):
        sent = self.notices()
        linked = [text.splitlines()[0] for text in sent if text.endswith(f"\nGarden: {GARDEN}")]
        self.assertEqual(linked, ["🙋 approval_needed event", "📬 delivery_opened event", "⛔ audit_failed event",
                                  "⚠️ error event", "🏁 Run blocked: queue #7"])
        self.assertIn("🔎 audit_passed event\nsummary\nhttps://github.com/acme/tool/pull/4", sent)
        self.assertTrue(all(text.count("https://github.com/acme/tool/pull/4") == 1 for text in sent[:-2]),
                        "the event's own URL stays")
        self.assertFalse(any("Garden:" in text for text in self.notices(garden=None)), "no link without garden_url")
        self.assertEqual([e["url"] for e in activity.recent(self.log)][-1], "https://github.com/acme/tool/pull/4")

    def test_no_duplicate_line_when_the_event_url_is_the_garden(self):
        sent = []
        cfg = {**self.cfg, "notify": {**self.cfg["notify"], "garden_url": GARDEN}}
        activity.emit(cfg, "approval_needed", "Approval needed: #7 x", "Target acme/tool.", GARDEN,
                      sender=lambda path, text: sent.append(text))
        self.assertEqual(sent, [f"🙋 Approval needed: #7 x\nTarget acme/tool.\n{GARDEN}"])
        long = activity.telegram_message(activity.event("error", "t", "s" * 1200, "https://github.com/a/b"), GARDEN)
        self.assertLessEqual(len(long), 4000)
        self.assertTrue(long.endswith(f"Garden: {GARDEN}"))

    def test_a_failing_notifier_with_a_garden_link_never_blocks(self):
        cfg = {**self.cfg, "notify": {**self.cfg["notify"], "garden_url": GARDEN}}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            entry = activity.emit(cfg, "approval_needed", "Approval needed: #7 x", "s", GARDEN,
                                  sender=lambda *_: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(entry["kind"], "approval_needed")
        self.assertEqual(activity.recent(self.log)[0]["title"], "Approval needed: #7 x")
        self.assertIn("notification not sent", err.getvalue())


class ActivityEndpointTest(ActivityBase):
    def serve(self, **options):
        cfg = {"queue": "acme/queue", "targets": {"acme/tool": "/tmp/t"}, "state": self.cfg["state"], "pico": "pico",
               "gh": "gh", "poll": 10, "duration": 600, "calls": 10}
        garden = garden_approvals.GardenApprovals(0, cfg, **options)
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(
            self.root / "runs", None, approvals=garden, allowed_hosts=tuple(options.get("allowed_hosts", ())),
            activity_path=self.log))
        self.addCleanup(server.server_close)
        garden.port = server.server_port
        return garden, server

    def get(self, server, path="/api/activity", headers=None):
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
        finally:
            thread.join(timeout=5)

    def test_needs_the_session_token(self):
        activity.emit(self.cfg, "approval", "Approved queue #7", sender=lambda *_: None)
        garden, server = self.serve()
        code, body = self.get(server)
        self.assertEqual((code, body["error_code"]), (403, "CONTROL_TOKEN_INVALID"))
        code, body = self.get(server, headers={"X-Pico-Token": garden.token})
        self.assertEqual((code, [e["title"] for e in body["events"]]), (200, ["Approved queue #7"]))

    def test_remote_reads_need_the_allowed_tailscale_login(self):
        activity.emit(self.cfg, "merged", "Merged", sender=lambda *_: None)
        garden, server = self.serve(allowed_hosts=[HOST], required_logins=[LOGIN])
        base = {"Host": HOST, "X-Pico-Token": garden.token}
        for login in (None, "intruder@github"):
            headers = dict(base) if login is None else {**base, "Tailscale-User-Login": login}
            code, body = self.get(server, headers=headers)
            self.assertEqual((code, body["error_code"]), (403, "APPROVAL_IDENTITY_REJECTED"), login)
            code, body = self.get(server, "/activity.js", headers=headers)
            self.assertEqual(code, 403, login)
        code, body = self.get(server, headers={**base, "Tailscale-User-Login": LOGIN})
        self.assertEqual((code, body["events"][0]["title"]), (200, "Merged"))
        code, body = self.get(server, headers={"Host": HOST, "Tailscale-User-Login": LOGIN})
        self.assertEqual((code, body["error_code"]), (403, "CONTROL_TOKEN_INVALID"))

    def test_off_without_approvals(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.root / "runs", None))
        self.addCleanup(server.server_close)
        code, body = self.get(server)
        self.assertEqual((code, body["error_code"]), (404, "ACTIVITY_OFF"))


if __name__ == "__main__":
    unittest.main()
