"""Garden delivery feedback: previewed, bound to the observed head, posted only as a comment or change request."""
import json
import sqlite3
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
import delivery_feedback  # noqa: E402
import delivery_store  # noqa: E402
import github_sync  # noqa: E402
import watch  # noqa: E402

REPO = "acme/app"
HEAD = "a" * 40
OTHER = "b" * 40


def delivery(issue=7, *, repository=REPO, state="needs_human_review", publication="observed", pr=12, head=HEAD):
    return {"issue": issue, "execution_id": f"issue-{issue}", "run_id": "run-abc", "execution": "completed",
            "repository": repository, "publication": publication, "pr_number": pr if publication == "observed" else None,
            "pr_url": f"https://github.com/{repository}/pull/{pr}" if publication == "observed" else None,
            "head_sha": head if publication == "observed" else None, "checks": "passed", "review": "not_requested",
            "delivery": state, "reason": "checks passed", "next_action": "Review the exact change in GitHub.",
            "actor": "observer", "observed_at": "2026-11-17T12:00:00Z", "first_seen": "2026-11-17T11:00:00Z",
            "last_seen": "2026-11-17T12:00:00Z", "evidence": {}}


class FakeGitHub:
    """Records writes; reads return the live PR the test sets."""
    GhAuthError, GhUnavailableError = github_sync.GhAuthError, github_sync.GhUnavailableError

    def __init__(self):
        self.login = "reviewer"
        self.live = {"number": 12, "state": "OPEN", "head": HEAD, "cross_repository": False, "author": "pico-bot",
                     "url": f"https://github.com/{REPO}/pull/12"}
        self.writes = []
        self.fail_write = None

    def current_login(self):
        return self.login

    def pr_review_target(self, number, *, repo):
        return dict(self.live, number=number)

    def post_pr_comment(self, number, body, *, repo):
        if self.fail_write:
            raise self.fail_write
        self.writes.append(("comment", repo, number, body, None))
        return f"https://github.com/{repo}/pull/{number}#issuecomment-1"

    def request_changes(self, number, body, *, repo, commit):
        if self.fail_write:
            raise self.fail_write
        self.writes.append(("request_changes", repo, number, body, commit))
        return f"https://github.com/{repo}/pull/{number}#pullrequestreview-2"


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "issue-dispatcher.sqlite3"
        self.rows = [delivery()]
        self.write_rows()
        self.gh = FakeGitHub()
        self.audit = self.root / "garden-feedback.jsonl"
        self.feedback = delivery_feedback.DeliveryFeedback(8765, self.db_path, audit_path=self.audit, adapter=self.gh)

    def write_rows(self):
        with sqlite3.connect(self.db_path) as db:
            delivery_store.initialize(db)
            db.execute("DELETE FROM deliveries")
            for row in self.rows:
                delivery_store.save(db, row)

    def preview(self, kind="comment", body="Please rename the helper.", issue=7):
        return self.feedback.act({"issue": issue, "kind": kind, "body": body})

    def send(self, kind="comment", body="Please rename the helper.", issue=7):
        shown = self.preview(kind, body, issue)
        self.assertEqual(shown["stage"], "preview", shown)
        return shown, self.feedback.act({"issue": issue, "kind": kind, "body": body, "confirm": shown["confirm"]})

    def test_preview_names_author_target_and_text_without_writing(self):
        shown = self.preview()
        self.assertEqual((shown["ok"], shown["stage"]), (True, "preview"))
        self.assertEqual({k: shown["preview"][k] for k in ("repository", "pr_number", "head_sha", "kind", "body", "author")},
                         {"repository": REPO, "pr_number": 12, "head_sha": HEAD, "kind": "comment",
                          "body": "Please rename the helper.", "author": "reviewer"})
        self.assertEqual(self.gh.writes, [])
        self.assertFalse(self.audit.exists(), "a preview is not a send")

    def test_confirmed_comment_is_posted_once_and_audited_without_text(self):
        _, done = self.send()
        self.assertEqual((done["ok"], done["stage"], done["status"]), (True, "done", "COMMENT_POSTED"))
        self.assertEqual(self.gh.writes, [("comment", REPO, 12, "Please rename the helper.", None)])
        self.assertTrue(done["url"].startswith(f"https://github.com/{REPO}/pull/12"))
        [line] = self.audit.read_text().splitlines()
        entry = json.loads(line)
        self.assertEqual((entry["stage"], entry["ok"], entry["pr"], entry["head"], entry["author"]), ("done", True, 12, HEAD, "reviewer"))
        self.assertNotIn("rename", line)

    def test_request_changes_is_pinned_to_the_observed_head(self):
        _, done = self.send("request_changes", "The migration drops data.")
        self.assertEqual(done["status"], "CHANGES_REQUESTED")
        self.assertEqual(self.gh.writes, [("request_changes", REPO, 12, "The migration drops data.", HEAD)])

    def test_confirmation_is_single_use(self):
        shown, _ = self.send()
        again = self.feedback.act({"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"]})
        self.assertEqual((again["ok"], again["error_code"]), (False, "FEEDBACK_ALREADY_SENT"))
        self.assertEqual(len(self.gh.writes), 1)

    def test_changed_text_or_kind_invalidates_the_preview(self):
        shown = self.preview()
        for change in ({"body": "Something else."}, {"kind": "request_changes"}):
            data = {"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"], **change}
            refused = self.feedback.act(data)
            self.assertEqual(refused["error_code"], "CONTROL_PREVIEW_STALE")
        self.assertEqual(self.gh.writes, [])

    def test_moved_head_is_refused_at_preview_and_at_send(self):
        shown = self.preview()
        self.gh.live["head"] = OTHER
        refused = self.feedback.act({"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"]})
        self.assertEqual(refused["error_code"], "DELIVERY_HEAD_CHANGED")
        self.assertEqual(self.preview()["error_code"], "DELIVERY_HEAD_CHANGED")
        # A newer observation of the same delivery also invalidates an older preview.
        self.gh.live["head"] = OTHER
        self.rows = [delivery(head=OTHER)]
        self.write_rows()
        stale = self.feedback.act({"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"]})
        self.assertEqual(stale["error_code"], "CONTROL_PREVIEW_STALE")
        self.assertEqual(self.gh.writes, [])

    def test_closed_cross_repository_and_unverified_deliveries_are_refused(self):
        cases = [
            (lambda: self.gh.live.update(state="MERGED"), "DELIVERY_PR_NOT_OPEN"),
            (lambda: self.gh.live.update(state="OPEN", cross_repository=True), "DELIVERY_PR_CROSS_REPOSITORY"),
        ]
        for mutate, code in cases:
            mutate()
            self.assertEqual(self.preview()["error_code"], code)
        self.gh.live.update(cross_repository=False)
        for row, code in ((delivery(state="merged"), "DELIVERY_CLOSED"),
                          (delivery(publication="none"), "DELIVERY_PR_UNVERIFIED")):
            self.rows = [row]
            self.write_rows()
            self.assertEqual(self.preview()["error_code"], code)
        self.assertEqual(self.preview(issue=99)["error_code"], "DELIVERY_UNKNOWN")
        self.assertEqual(self.gh.writes, [])

    def test_cannot_request_changes_on_own_pull_request(self):
        self.gh.login = "Pico-Bot"
        self.assertEqual(self.preview("request_changes")["error_code"], "FEEDBACK_OWN_PR")
        self.assertEqual(self.preview("comment")["stage"], "preview")

    def test_repository_scope_hides_other_deliveries(self):
        self.rows = [delivery(repository="private/other")]
        self.write_rows()
        scoped = delivery_feedback.DeliveryFeedback(8765, self.db_path, repository=REPO, adapter=self.gh)
        self.assertEqual(scoped.act({"issue": 7, "kind": "comment", "body": "x"})["error_code"], "DELIVERY_UNKNOWN")

    def test_invalid_input_is_rejected_before_any_read(self):
        with patch.object(self.gh, "pr_review_target", side_effect=AssertionError("read")):
            for data, code in (({"issue": "7", "kind": "comment", "body": "x"}, "FEEDBACK_INVALID"),
                               ({"issue": 7, "kind": "approve", "body": "x"}, "FEEDBACK_INVALID"),
                               ({"issue": 7, "kind": "comment", "body": "   "}, "FEEDBACK_EMPTY"),
                               ({"issue": 7, "kind": "comment", "body": "x" * 4001}, "FEEDBACK_TOO_LONG"),
                               ({"issue": 7, "kind": "comment", "body": "bell\x07"}, "FEEDBACK_INVALID")):
                with self.assertRaises(delivery_feedback.ControlError) as refused:
                    self.feedback.act(data)
                self.assertEqual(refused.exception.code, code)

    def test_github_failures_are_explicit_and_not_marked_sent(self):
        shown = self.preview()
        self.gh.fail_write = github_sync.GhTimeoutError("slow")
        unknown = self.feedback.act({"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"]})
        self.assertEqual(unknown["error_code"], "FEEDBACK_OUTCOME_UNKNOWN")
        self.gh.fail_write = github_sync.GhCommandError(["gh"], 1, "HTTP 422")
        refused = self.feedback.act({"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"]})
        self.assertEqual(refused["error_code"], "FEEDBACK_GITHUB_FAILED")
        self.gh.fail_write = None
        done = self.feedback.act({"issue": 7, "kind": "comment", "body": "Please rename the helper.", "confirm": shown["confirm"]})
        self.assertEqual(done["stage"], "done")

    def test_unreadable_delivery_state_is_unavailable(self):
        self.db_path.write_bytes(b"corrupt")
        self.assertEqual(self.preview()["error_code"], "DELIVERY_STATE_UNAVAILABLE")


class GithubArgvTest(unittest.TestCase):
    def test_review_argv_is_one_fixed_shape(self):
        ok = ["gh", "api", "-X", "POST", "repos/acme/app/pulls/12/reviews", "-f", "event=REQUEST_CHANGES",
              "-f", f"commit_id={HEAD}", "-f", "body=-F @/etc/passwd", "--hostname", "github.com"]
        github_sync._validate_allowlisted(ok)
        bad = [
            ok[:6] + ["event=APPROVE"] + ok[7:],
            ok[:4] + ["repos/acme/app/pulls/12/merge"] + ok[5:],
            ok[:8] + ["commit_id=abc"] + ok[9:],
            ok[:9] + ["-F", "body=x"] + ok[11:],
            ok[:10] + ["body=  "] + ok[11:],
            ok[:11] + ["--hostname", "evil.example"],
            ok[:3] + ["DELETE"] + ok[4:],
        ]
        for argv in bad:
            with self.assertRaises(github_sync.GithubSyncError, msg=argv):
                github_sync._validate_allowlisted(argv)

    def test_user_get_is_allowed_and_pinned(self):
        github_sync._validate_allowlisted(["gh", "api", "-X", "GET", "user", "--hostname", "github.com"])
        with self.assertRaises(github_sync.GithubSyncError):
            github_sync._validate_allowlisted(["gh", "api", "-X", "GET", "users/someone", "--hostname", "github.com"])

    def run_capturing(self, stdout, calls):
        class Proc:
            returncode, stderr = 0, ""
        Proc.stdout = stdout

        def run(argv, **kwargs):
            github_sync._validate_allowlisted(argv)
            calls.append((argv, kwargs.get("env")))
            return Proc()
        return patch.object(github_sync, "_run", side_effect=run)

    def test_every_feedback_call_is_pinned_to_github_com_without_gh_host(self):
        calls = []
        pull = {"number": 12, "state": "open", "merged": False, "head": {"sha": HEAD, "repo": {"full_name": "acme/app"}},
                "base": {"repo": {"full_name": "acme/app"}}, "user": {"login": "pico-bot"}}
        with patch.dict("os.environ", {"GH_HOST": "ghe.example"}):
            with self.run_capturing(json.dumps({"login": "reviewer"}), calls):
                self.assertEqual(github_sync.current_login(), "reviewer")
            with self.run_capturing(json.dumps(pull), calls):
                self.assertEqual(github_sync.pr_review_target(12, repo="acme/app"),
                                 {"number": 12, "state": "OPEN", "head": HEAD, "cross_repository": False, "author": "pico-bot"})
            with self.run_capturing(json.dumps({"html_url": "https://github.com/acme/app/pull/12#issuecomment-1"}), calls):
                github_sync.post_pr_comment(12, "x", repo="acme/app")
            with self.run_capturing("{}", calls):
                github_sync.request_changes(12, "x", repo="acme/app", commit=HEAD)
        self.assertEqual(len(calls), 4)
        for argv, env in calls:
            self.assertEqual(argv[-2:], ["--hostname", "github.com"])
            self.assertIsNotNone(env)
            self.assertNotIn("GH_HOST", env)

    def test_pull_request_shapes(self):
        base = {"number": 12, "state": "open", "merged": False, "head": {"sha": HEAD, "repo": {"full_name": "acme/app"}},
                "base": {"repo": {"full_name": "acme/app"}}, "user": {"login": "pico-bot"}}
        cases = [({**base, "state": "closed", "merged": True}, ("state", "MERGED")),
                 ({**base, "state": "closed"}, ("state", "CLOSED")),
                 ({**base, "head": {"sha": HEAD, "repo": {"full_name": "fork/app"}}}, ("cross_repository", True)),
                 ({**base, "head": {"sha": HEAD, "repo": None}}, ("cross_repository", True))]
        for data, (key, value) in cases:
            with self.run_capturing(json.dumps(data), []):
                self.assertEqual(github_sync.pr_review_target(12, repo="acme/app")[key], value)
        for broken in ({**base, "number": 13}, {**base, "head": {"sha": "short"}}, {**base, "user": None}):
            with self.run_capturing(json.dumps(broken), []), self.assertRaises(github_sync.GhOutputError):
                github_sync.pr_review_target(12, repo="acme/app")

    def test_comment_argv_keeps_hostile_bodies_as_one_raw_field(self):
        calls = []
        for body in ("--repo other/app", "@/etc/passwd", "body=x\nmore", "-f event=APPROVE"):
            with self.run_capturing("{}", calls):
                github_sync.post_pr_comment(12, body, repo="acme/app")
            argv = calls[-1][0]
            self.assertEqual(argv[:6], ["gh", "api", "-X", "POST", "repos/acme/app/issues/12/comments", "-f"])
            self.assertEqual(argv[6:], [f"body={body}", "--hostname", "github.com"])
        for argv in (["gh", "api", "-X", "POST", "repos/acme/app/issues/12/comments", "-F", "body=@x", "--hostname", "github.com"],
                     ["gh", "api", "-X", "POST", "repos/acme/app/issues/12/comments", "-f", "title=x", "--hostname", "github.com"],
                     ["gh", "api", "-X", "POST", "repos/acme/app/pulls/12/merge", "-f", "body=x", "--hostname", "github.com"]):
            with self.assertRaises(github_sync.GithubSyncError):
                github_sync._validate_allowlisted(argv)

    def test_successful_post_with_unreadable_output_is_still_sent(self):
        with self.run_capturing("not json", []):
            self.assertEqual(github_sync.request_changes(12, "x", repo="acme/app", commit=HEAD), "")
            self.assertEqual(github_sync.post_pr_comment(12, "x", repo="acme/app"), "")

    def test_request_changes_builds_the_validated_argv(self):
        calls = []

        class Proc:
            returncode, stderr, stdout = 0, "", json.dumps({"html_url": "https://github.com/acme/app/pull/12#pullrequestreview-9"})

        def run(argv, **kwargs):
            github_sync._validate_allowlisted(argv)
            calls.append(argv)
            return Proc()
        with patch.object(github_sync, "_run", side_effect=run):
            url = github_sync.request_changes(12, "Fix it", repo="acme/app", commit=HEAD)
        self.assertEqual(url, "https://github.com/acme/app/pull/12#pullrequestreview-9")
        self.assertEqual(calls[0][6:11], ["event=REQUEST_CHANGES", "-f", f"commit_id={HEAD}", "-f", "body=Fix it"])
        with self.assertRaises(ValueError):
            github_sync.request_changes(12, "Fix it", repo="acme/app", commit="HEAD")


class FeedbackEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def serve(self, feedback):
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.root / "runs", None, feedback=feedback))
        self.addCleanup(server.server_close)
        if feedback is not None:
            feedback.port = server.server_port
        return server

    def request(self, server, path, *, body=None, headers=None):
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        port = server.server_port
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
        finally:
            thread.join(timeout=5)

    def test_feedback_off_has_no_write_endpoint_and_reports_read_only(self):
        server = self.serve(None)
        port = server.server_port
        code, body = self.request(server, "/api/delivery-feedback", body={"issue": 1},
                                  headers={"Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual((code, body["error_code"]), (404, "FEEDBACK_OFF"))
        code, body = self.request(server, "/api/deliveries")
        self.assertEqual((code, body), (200, {"ok": True, "deliveries": []}))

    def test_feedback_requires_token_and_origin_and_gates_reads(self):
        feedback = delivery_feedback.DeliveryFeedback(0, self.root / "issue-dispatcher.sqlite3", adapter=FakeGitHub())
        server = self.serve(feedback)
        port = server.server_port
        origin = {"Origin": f"http://127.0.0.1:{port}"}
        code, body = self.request(server, "/api/delivery-feedback", body={"issue": 1, "kind": "comment", "body": "x"}, headers=origin)
        self.assertEqual((code, body["error_code"]), (403, "CONTROL_TOKEN_INVALID"))
        code, body = self.request(server, "/api/delivery-feedback", body={"issue": 1, "kind": "comment", "body": "x"},
                                  headers={"X-Pico-Token": feedback.token, "Origin": "http://evil.example"})
        self.assertEqual((code, body["error_code"]), (403, "CONTROL_ORIGIN_REJECTED"))
        code, body = self.request(server, "/api/deliveries")
        self.assertEqual(code, 403, "with feedback on, reading deliveries needs the session token")
        code, body = self.request(server, "/api/deliveries", headers={"X-Pico-Token": feedback.token})
        self.assertEqual((code, body["feedback"]), (200, True))
        code, body = self.request(server, "/api/delivery-feedback", body={"issue": 1, "kind": "comment", "body": "x"},
                                  headers={"X-Pico-Token": feedback.token, **origin})
        self.assertEqual((code, body["stage"], body["error_code"]), (200, "refused", "DELIVERY_UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
