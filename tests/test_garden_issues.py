#!/usr/bin/env python3
"""Garden issues panel: GET /api/issues with a fake gh adapter and real Git checkouts.

Never invokes the real `gh`. Covers the response shape, the cap, gh failure
states, the cache, fail-closed repository identity, references, the session
token on /api/issues, /api/runs and /api/workspace, and that issue text is
served verbatim as JSON data (the page renders it with textContent).
"""
from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import garden_controls as gc  # noqa: E402
import garden_issues as gi  # noqa: E402
import github_sync  # noqa: E402
import watch  # noqa: E402

HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
SENTINEL = "SECRET-STDERR-SENTINEL"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def issue(number: int, **extra) -> dict:
    return {"number": number, "title": f"Issue {number}", "state": "OPEN",
            "labels": [{"id": "x", "name": "enhancement", "color": "a2eeef"}],
            "assignees": [{"login": "octo-cat", "name": "Octo"}], "updatedAt": "2026-10-21T14:15:11Z",
            "url": f"https://github.com/acme/app/issues/{number}", **extra}


class FakeGh:
    def __init__(self, issues=None, prs=None, issue_error=None, pr_error=None):
        self.issues = issues if issues is not None else [issue(7), issue(8), issue(1)]
        self.prs = prs if prs is not None else []
        self.issue_error, self.pr_error = issue_error, pr_error
        self.calls: list[tuple] = []

    def list_issue_summaries(self, limit, *, repo, timeout=None):
        self.calls.append(("issues", limit, repo))
        if self.issue_error:
            raise self.issue_error
        return self.issues[:limit]

    def list_open_prs(self, limit, *, repo, timeout=None):
        self.calls.append(("prs", limit, repo))
        if self.pr_error:
            raise self.pr_error
        return self.prs[:limit]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class IssuesFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "app"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/app.git")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        for branch in ("pico/issue-7", "pico/batch-1", "pico/gh-8-garden", "feature/issue-1"):
            git(self.repo, "branch", branch)
        self.clock = Clock()

    def source(self, fake, *, explicit=None, repository="acme/app", **kwargs):
        return gi.IssuesSource(gi.repository_targets(self.repo, explicit, repository), mode="repository",
                               adapter=fake, clock=self.clock, **kwargs)


class IssuesViewTest(IssuesFixture):
    def test_shape_and_references(self):
        prs = [{"number": 40, "headRefName": "pico/other", "isDraft": True, "body": "Closes #7\n\nRefs #1"},
               {"number": 41, "headRefName": "feature", "isDraft": False, "body": "fixes ACME/app#8"},
               {"number": 42, "headRefName": "pico/issue-1", "isDraft": False, "body": "closes other/repo#7"}]
        fake = FakeGh(prs=prs)
        view = self.source(fake).view()
        self.assertEqual((view["ok"], view["mode"], view["cap"], view["cache_ttl_seconds"]), (True, "repository", 50, 60.0))
        self.assertIn("branches", view["reference_rules"])
        [entry] = view["repositories"]
        self.assertEqual({k: entry[k] for k in ("id", "repository", "available", "error_code", "truncated", "cached")},
                         {"id": None, "repository": "acme/app", "available": True, "error_code": None,
                          "truncated": False, "cached": False})
        self.assertEqual(entry["pull_requests"], {"available": True, "error_code": None, "scanned": 3, "truncated": False})
        self.assertEqual(entry["branches"], {"available": True, "scanned": 3})
        by_number = {item["number"]: item for item in entry["issues"]}
        self.assertEqual(list(by_number), [7, 8, 1])  # gh order is kept
        seven = by_number[7]
        self.assertEqual(set(seven), {"number", "title", "labels", "assignees", "updated_at", "url", "references"})
        self.assertEqual(seven["labels"], ["enhancement"])
        self.assertEqual(seven["assignees"], ["octo-cat"])
        self.assertEqual(seven["url"], "https://github.com/acme/app/issues/7")
        self.assertEqual(seven["references"]["branches"], ["pico/issue-7"])
        self.assertEqual(seven["references"]["pull_requests"],
                         [{"number": 40, "draft": True, "url": "https://github.com/acme/app/pull/40"}])
        self.assertEqual(by_number[8]["references"]["branches"], ["pico/gh-8-garden"])
        self.assertEqual([pr["number"] for pr in by_number[8]["references"]["pull_requests"]], [41])
        # pico/batch-1 is not issue #1, Refs is not a closing keyword, and feature/ branches are not pico branches;
        # PR 42's head branch carries issue-1.
        self.assertEqual(by_number[1]["references"]["branches"], [])
        self.assertEqual([pr["number"] for pr in by_number[1]["references"]["pull_requests"]], [42])
        self.assertEqual(fake.calls, [("issues", 51, "acme/app"), ("prs", 101, "acme/app")])

    def test_cap_reports_truncation(self):
        fake = FakeGh(issues=[issue(n) for n in range(200, 140, -1)])
        [entry] = self.source(fake).view()["repositories"]
        self.assertEqual(len(entry["issues"]), 50)
        self.assertTrue(entry["truncated"])
        self.assertEqual(entry["issues"][0]["number"], 200)
        [entry] = self.source(FakeGh(issues=[issue(n) for n in range(1, 51)])).view()["repositories"]
        self.assertEqual((len(entry["issues"]), entry["truncated"]), (50, False))

    def test_gh_failures_are_explicit_and_never_an_empty_list(self):
        cases = [
            (github_sync.GhUnavailableError("missing"), "GH_UNAVAILABLE"),
            (github_sync.GhAuthError(f"not logged in {SENTINEL}"), "GH_UNAUTHENTICATED"),
            (github_sync.GhAuthError("API rate limit exceeded. Authenticated requests get a higher limit"), "GH_RATE_LIMITED"),
            (github_sync.GhCommandError(["gh"], 1, f"HTTP 403: API rate limit exceeded {SENTINEL}"), "GH_RATE_LIMITED"),
            (github_sync.GhCommandError(["gh"], 1, SENTINEL), "GH_COMMAND_FAILED"),
            (github_sync.GhTimeoutError("slow"), "GH_TIMEOUT"),
            (github_sync.GhOutputError(SENTINEL), "GH_OUTPUT_INVALID"),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                view = self.source(FakeGh(issue_error=error)).view()
                [entry] = view["repositories"]
                self.assertEqual((entry["available"], entry["error_code"], entry["issues"]), (False, code, None))
                self.assertEqual(entry["error"], gi.REASONS[code])
                self.assertNotIn(SENTINEL, json.dumps(view))

    def test_pr_failure_keeps_issues_and_marks_references_unknown(self):
        fake = FakeGh(pr_error=github_sync.GhCommandError(["gh"], 1, "boom"))
        [entry] = self.source(fake).view()["repositories"]
        self.assertTrue(entry["available"])
        self.assertEqual(entry["pull_requests"]["error_code"], "GH_COMMAND_FAILED")
        self.assertFalse(entry["pull_requests"]["available"])
        self.assertTrue(all(item["references"]["pull_requests"] is None for item in entry["issues"]))
        self.assertEqual(entry["issues"][0]["references"]["branches"], ["pico/issue-7"])

    def test_results_and_failures_are_cached(self):
        fake = FakeGh()
        source = self.source(fake)
        source.view()
        self.clock.now += 59
        [entry] = source.view()["repositories"]
        self.assertTrue(entry["cached"])
        self.assertEqual(len(fake.calls), 2)
        self.clock.now += 2
        self.assertFalse(source.view()["repositories"][0]["cached"])
        self.assertEqual(len(fake.calls), 4)
        failing = FakeGh(issue_error=github_sync.GhAuthError("not logged in"))
        source = self.source(failing)
        source.view()
        source.view()
        self.assertEqual(len(failing.calls), 1)

    def test_ambiguous_identity_fails_closed_without_calling_gh(self):
        git(self.repo, "remote", "add", "fork", "https://github.com/someone/app.git")
        fake = FakeGh()
        [entry] = self.source(fake).view()["repositories"]
        self.assertEqual((entry["available"], entry["error_code"], entry["issues"]),
                         (False, "TARGET_REPOSITORY_AMBIGUOUS", None))
        self.assertEqual(fake.calls, [])
        # An explicit --repo still selects one of the remotes, as everywhere else.
        [entry] = self.source(fake, explicit="acme/app").view()["repositories"]
        self.assertTrue(entry["available"])

    def test_identity_conflict_and_missing_checkout(self):
        fake = FakeGh()
        [entry] = self.source(fake, repository="acme/other").view()["repositories"]
        self.assertEqual(entry["error_code"], "TARGET_REPOSITORY_CONFLICT")
        targets = gi.workspace_targets(lambda: {"repositories": [
            {"id": "api", "repository": "acme/app", "resolved_path": str(self.repo), "blocker_code": None},
            {"id": "web", "repository": "acme/web", "resolved_path": None, "blocker_code": "WORKSPACE_REPOSITORY_PATH_MISSING"},
            {"id": "lib", "repository": "acme/lib", "resolved_path": str(self.repo), "blocker_code": None}]})
        view = gi.IssuesSource(targets, mode="workspace", adapter=fake, clock=self.clock).view()
        api, web, lib = view["repositories"]
        self.assertEqual((api["id"], api["available"]), ("api", True))
        self.assertEqual((web["id"], web["error_code"], web["issues"]), ("web", "WORKSPACE_REPOSITORY_PATH_MISSING", None))
        self.assertEqual((lib["error_code"], lib["issues"]), ("TARGET_REPOSITORY_CONFLICT", None))
        self.assertEqual(fake.calls, [("issues", 51, "acme/app"), ("prs", 101, "acme/app")])

    def test_text_is_served_verbatim_and_urls_are_rebuilt(self):
        fake = FakeGh(issues=[issue(5, title=HOSTILE, labels=[{"name": HOSTILE}],
                                    assignees=[{"login": "<b>x</b>"}], url="javascript:alert(1)",
                                    updatedAt="<script>")])
        item = self.source(fake).view()["repositories"][0]["issues"][0]
        self.assertEqual(item["title"], HOSTILE)
        self.assertEqual(item["labels"], [HOSTILE])
        self.assertEqual(item["assignees"], [])
        self.assertEqual(item["url"], "https://github.com/acme/app/issues/5")
        self.assertIsNone(item["updated_at"])


class BlockingGh(FakeGh):
    """A gh that hangs on chosen repositories until released."""

    def __init__(self, hang: set[str], **kwargs):
        super().__init__(**kwargs)
        self.hang, self.release, self.entered = hang, threading.Event(), threading.Event()

    def list_issue_summaries(self, limit, *, repo, timeout=None):
        if repo in self.hang:
            self.entered.set()
            self.release.wait(10)
        return super().list_issue_summaries(limit, repo=repo, timeout=timeout)


class IssuesRobustnessTest(IssuesFixture):
    def second_repo(self, name="web"):
        other = Path(self.tmp.name) / name
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "remote", "add", "origin", f"https://github.com/acme/{name}.git")
        return other

    def workspace(self, fake, repos):
        state = {"repositories": repos}
        return gi.IssuesSource(gi.workspace_targets(lambda: state), mode="workspace", adapter=fake, clock=self.clock), state

    def test_git_timeout_or_missing_git_fails_only_that_repository(self):
        other = self.second_repo()
        real_run = subprocess.run
        for error in (subprocess.TimeoutExpired(["git"], 10), FileNotFoundError("git")):
            def run(argv, *args, error=error, **kwargs):
                if str(self.repo) in argv:
                    raise error
                return real_run(argv, *args, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(gi.github_integration.subprocess, "run", run):
                source, _ = self.workspace(FakeGh(), [
                    {"id": "api", "repository": "acme/app", "resolved_path": str(self.repo)},
                    {"id": "web", "repository": "acme/web", "resolved_path": str(other)}])
                api, web = source.view()["repositories"]
                self.assertEqual((api["available"], api["error_code"], api["issues"]),
                                 (False, "TARGET_REPOSITORY_UNREADABLE", None))
                self.assertTrue(web["available"])

    def test_hung_gh_blocks_neither_other_repositories_nor_other_requests(self):
        other = self.second_repo()
        fake = BlockingGh({"acme/app"})
        source, _ = self.workspace(fake, [
            {"id": "api", "repository": "acme/app", "resolved_path": str(self.repo)},
            {"id": "web", "repository": "acme/web", "resolved_path": str(other)}])
        results = []
        first = threading.Thread(target=lambda: results.append(source.view()))
        first.start()
        self.assertTrue(fake.entered.wait(5))
        try:
            api, web = source.view()["repositories"]  # returns while the first request is still stuck
            self.assertEqual((api["error_code"], api["issues"]), ("ISSUES_FETCH_IN_PROGRESS", None))
            self.assertTrue(web["available"])
        finally:
            fake.release.set()
            first.join(10)
        self.assertTrue(results[0]["repositories"][0]["available"])
        # The second request never started its own gh call for the hung repository.
        self.assertEqual(sum(1 for kind, _, repo in fake.calls if (kind, repo) == ("issues", "acme/app")), 1)

    def test_expired_entry_is_served_stale_while_it_is_refetched(self):
        fake = BlockingGh(set())
        source = self.source(fake)
        source.view()
        self.clock.now += 61
        fake.hang = {"acme/app"}
        first = threading.Thread(target=source.view)
        first.start()
        self.assertTrue(fake.entered.wait(5))
        try:
            [entry] = source.view()["repositories"]
            self.assertEqual((entry["available"], entry["cached"]), (True, True))
        finally:
            fake.release.set()
            first.join(10)

    def test_cache_drops_expired_and_unwatched_repositories(self):
        other = self.second_repo()
        source, state = self.workspace(FakeGh(), [
            {"id": "api", "repository": "acme/app", "resolved_path": str(self.repo)},
            {"id": "web", "repository": "acme/web", "resolved_path": str(other)}])
        source.view()
        self.assertEqual(len(source._cache), 2)
        state["repositories"] = state["repositories"][:1]
        source.view()
        self.assertEqual([key[0] for key in source._cache], ["api"])
        state["repositories"] = []
        source.view()
        self.assertEqual(source._cache, {})

    def test_bool_number_is_invalid_and_label_names_are_capped(self):
        [entry] = self.source(FakeGh(issues=[issue(True)])).view()["repositories"]
        self.assertEqual((entry["error_code"], entry["issues"]), ("GH_OUTPUT_INVALID", None))
        [entry] = self.source(FakeGh(issues=[issue(2, labels=[{"name": "x" * 500}])])).view()["repositories"]
        self.assertEqual(len(entry["issues"][0]["labels"][0]), gi.MAX_LABEL)

    def test_gh_reads_use_the_short_timeout(self):
        seen = []

        class Recording(FakeGh):
            def list_issue_summaries(self, limit, *, repo, timeout=None):
                seen.append(timeout)
                return super().list_issue_summaries(limit, repo=repo)

            def list_open_prs(self, limit, *, repo, timeout=None):
                seen.append(timeout)
                return super().list_open_prs(limit, repo=repo)
        self.source(Recording()).view()
        self.assertEqual(seen, [gi.GH_TIMEOUT, gi.GH_TIMEOUT])
        self.assertLess(gi.GH_TIMEOUT, github_sync.DEFAULT_TIMEOUT_SECONDS)


class GithubSyncWrappersTest(unittest.TestCase):
    def run_with(self, stdout):
        proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")
        return patch.object(github_sync.shutil, "which", return_value="/usr/bin/gh"), \
            patch.object(github_sync.subprocess, "run", return_value=proc)

    def test_fixed_argv_and_shape_checks(self):
        which, run = self.run_with(json.dumps([issue(3)]))
        with which, run as mock_run:
            self.assertEqual(github_sync.list_issue_summaries(51, repo="acme/app")[0]["number"], 3)
        self.assertEqual(mock_run.call_args[0][0], ["gh", "issue", "list", "--state", "open", "--json",
                                                    "number,title,state,labels,assignees,updatedAt,url",
                                                    "--limit", "51", "--repo", "acme/app"])
        self.assertIs(mock_run.call_args[1]["shell"], False)
        self.assertEqual(mock_run.call_args[1]["timeout"], github_sync.DEFAULT_TIMEOUT_SECONDS)
        which, run = self.run_with(json.dumps([issue(3)]))
        with which, run as mock_run:
            github_sync.list_issue_summaries(5, repo="acme/app", timeout=10)
        self.assertEqual(mock_run.call_args[1]["timeout"], 10)
        which, run = self.run_with(json.dumps([issue(True)]))
        with which, run, self.assertRaises(github_sync.GhOutputError):
            github_sync.list_issue_summaries(5, repo="acme/app")
        which, run = self.run_with(json.dumps([{**issue(3), "assignees": "nobody"}]))
        with which, run, self.assertRaises(github_sync.GhOutputError):
            github_sync.list_issue_summaries(5, repo="acme/app")
        which, run = self.run_with(json.dumps([{"number": 4, "headRefName": "pico/x", "body": "", "isDraft": False}]))
        with which, run as mock_run:
            github_sync.list_open_prs(101, repo="acme/app")
        self.assertEqual(mock_run.call_args[0][0][:4], ["gh", "pr", "list", "--state"])
        which, run = self.run_with(json.dumps([{"number": 4, "headRefName": "x", "body": None, "isDraft": False}]))
        with which, run, self.assertRaises(github_sync.GhOutputError):
            github_sync.list_open_prs(5, repo="acme/app")
        for bad in (0, 1001, True):
            with self.assertRaises(ValueError):
                github_sync.list_issue_summaries(bad, repo="acme/app")
        with self.assertRaises(ValueError):
            github_sync.list_open_prs(5, repo="not a repo")


class IssuesHttpTest(IssuesFixture):
    TOKEN = "t" * 43

    def serve(self, *, controls_on: bool, issues=True):
        self.runs = Path(self.tmp.name) / "runs"
        self.runs.mkdir(exist_ok=True)
        source = self.source(FakeGh(issues=[issue(9, title=HOSTILE)])) if issues else None
        server = ThreadingHTTPServer(("127.0.0.1", 0), watch.make_handler(self.runs, "acme/app", issues=source))
        port = server.server_port
        if controls_on:
            controls = gc.Controls(Path(self.tmp.name) / "unused.yaml", port, token=self.TOKEN)
            server.RequestHandlerClass = watch.make_handler(self.runs, "acme/app", None, controls, port, issues=source)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def get(self, port, path, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": f"127.0.0.1:{port}"}
        if token:
            headers["X-Pico-Token"] = token
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type")
        conn.close()
        return response.status, json.loads(raw or b"{}"), content_type

    def test_token_required_on_data_endpoints_when_controls_are_on(self):
        port = self.serve(controls_on=True)
        for path in ("/api/issues", "/api/runs", "/api/workspace", "/api/controls"):
            for token in (None, "x" * 43):
                with self.subTest(path=path, token=token):
                    status, data, _ = self.get(port, path, token)
                    self.assertEqual((status, data["error_code"]), (403, "CONTROL_TOKEN_INVALID"))
                    self.assertNotIn("repositories", data)
                    self.assertNotIn("runs", data)
        status, data, _ = self.get(port, "/api/issues", self.TOKEN)
        self.assertEqual((status, data["repositories"][0]["issues"][0]["number"]), (200, 9))
        status, data, _ = self.get(port, "/api/runs", self.TOKEN)
        self.assertEqual((status, data["runs"]), (200, []))
        status, data, _ = self.get(port, "/api/workspace", self.TOKEN)
        self.assertEqual((status, data["error_code"]), (404, "WORKSPACE_MODE_OFF"))  # past the token gate
        # The static page still loads without the token: it carries the token in the URL fragment.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/issues.js", headers={"Host": f"127.0.0.1:{port}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"innerHTML =", response.read())
        conn.close()

    def test_no_token_needed_when_controls_are_off(self):
        port = self.serve(controls_on=False)
        for path, expected in (("/api/issues", 200), ("/api/runs", 200), ("/api/workspace", 404)):
            status, data, _ = self.get(port, path)
            self.assertEqual(status, expected, path)
            self.assertNotEqual(data.get("error_code"), "CONTROL_TOKEN_INVALID")

    def test_issue_text_is_json_data(self):
        port = self.serve(controls_on=False)
        status, data, content_type = self.get(port, "/api/issues")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("application/json"))
        self.assertEqual(data["repositories"][0]["issues"][0]["title"], HOSTILE)

    def test_no_target_repository(self):
        port = self.serve(controls_on=False, issues=False)
        status, data, _ = self.get(port, "/api/issues")
        self.assertEqual((status, data["ok"], data["error_code"]), (404, False, "ISSUES_TARGET_NONE"))


if __name__ == "__main__":
    unittest.main()
