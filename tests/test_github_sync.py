#!/usr/bin/env python3
"""Deterministic, fully-mocked tests for tools/github_sync.py (the safe `gh`
CLI subprocess boundary) and tools/backlog.py's GitHub composition layer
(github_sync_status/github_sync_task/github_push_task/github_pull_task).

These tests NEVER invoke the real `gh` binary and NEVER touch a real GitHub
repository: `subprocess.run` is mocked throughout via unittest.mock.patch.

Run with: python3 -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import github_sync as gh  # noqa: E402
import backlog_store as store  # noqa: E402
import backlog as backlog_cli  # noqa: E402


def fake_proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# tools/github_sync.py -- the pure gh boundary
# ---------------------------------------------------------------------------

class GithubSyncBoundaryTest(unittest.TestCase):
    def test_gh_unavailable_raises_before_any_subprocess_call(self):
        with patch.object(gh.shutil, "which", return_value=None), \
             patch.object(gh.subprocess, "run") as mock_run:
            with self.assertRaises(gh.GhUnavailableError):
                gh.auth_status()
            mock_run.assert_not_called()

    def test_not_authenticated_is_a_normal_result_not_an_exception(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode=1, stderr="not logged in")):
            status = gh.auth_status()
        self.assertFalse(status["authenticated"])
        self.assertIn("not logged in", status["detail"])

    def test_authenticated_true_on_zero_exit(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode=0, stdout="Logged in to github.com")):
            status = gh.auth_status()
        self.assertTrue(status["authenticated"])

    def test_require_authenticated_raises_gh_auth_error_when_not_authenticated(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode=1, stderr="not logged in")):
            with self.assertRaises(gh.GhAuthError):
                gh.require_authenticated()

    def test_repo_view_private_repo_parses_correctly(self):
        payload = {
            "nameWithOwner": "acme/secret-repo",
            "owner": {"login": "acme"},
            "name": "secret-repo",
            "isPrivate": True,
            "url": "https://github.com/acme/secret-repo",
        }
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout=json.dumps(payload))) as mock_run:
            data = gh.repo_view()
        self.assertTrue(data["isPrivate"])
        args, kwargs = mock_run.call_args
        self.assertEqual(kwargs.get("shell"), False)
        self.assertEqual(args[0][:3], ["gh", "repo", "view"])

    def test_repo_view_malformed_json_raises_gh_output_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout="not json at all")):
            with self.assertRaises(gh.GhOutputError):
                gh.repo_view()

    def test_repo_view_nonzero_exit_raises_gh_command_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode=1, stderr="no repo")):
            with self.assertRaises(gh.GhCommandError):
                gh.repo_view()

    def test_list_issues_rejects_invalid_state(self):
        with self.assertRaises(ValueError):
            gh.list_issues(state="bogus")

    def test_list_issues_non_array_json_raises_output_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout=json.dumps({"not": "a list"}))):
            with self.assertRaises(gh.GhOutputError):
                gh.list_issues()

    def test_view_issue_rejects_non_positive_number(self):
        with self.assertRaises(ValueError):
            gh.view_issue(0)
        with self.assertRaises(ValueError):
            gh.view_issue(-3)

    def test_create_issue_parses_number_from_url(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout="https://github.com/acme/repo/issues/42\n")) as mock_run:
            result = gh.create_issue("Title", "Body", labels=["backlog"])
        self.assertEqual(result["number"], 42)
        args, kwargs = mock_run.call_args
        self.assertEqual(kwargs.get("shell"), False)
        self.assertIn("--label", args[0])
        self.assertIn("backlog", args[0])

    def test_create_issue_unparseable_output_raises(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout="no url here")):
            with self.assertRaises(gh.GhOutputError):
                gh.create_issue("Title")

    def test_close_issue_builds_expected_argv(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc()) as mock_run:
            gh.close_issue(7, comment="done")
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["gh", "issue", "close", "7", "--comment", "done"])
        self.assertEqual(kwargs.get("shell"), False)

    def test_reopen_issue_builds_expected_argv(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc()) as mock_run:
            gh.reopen_issue(7)
        args, _ = mock_run.call_args
        self.assertEqual(args[0], ["gh", "issue", "reopen", "7"])

    def test_edit_issue_requires_at_least_one_field(self):
        with self.assertRaises(ValueError):
            gh.edit_issue(1)

    def test_internal_run_rejects_non_allowlisted_command(self):
        with self.assertRaises(gh.GithubSyncError):
            gh._run(["gh", "api", "repos/x/y"])

    def test_internal_run_rejects_argv_not_starting_with_gh(self):
        with self.assertRaises(gh.GithubSyncError):
            gh._run(["sh", "-c", "echo hi"])

    def test_every_public_mutating_call_uses_shell_false(self):
        """Defense in depth: assert every subprocess.run call captured across
        every public function in this module passes shell=False."""
        calls = []

        def recording_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return fake_proc(stdout=json.dumps({"number": 1, "title": "t", "state": "open", "labels": [], "body": "", "url": "u"}))

        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", side_effect=recording_run):
            gh.view_issue(1)
            gh.edit_issue(1, title="new title")

        self.assertTrue(calls)
        for _, kwargs in calls:
            self.assertEqual(kwargs.get("shell"), False)

    def test_issue_commands_are_repo_pinned(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout=json.dumps({"number": 7, "title": "T", "state": "open", "labels": [], "body": "", "url": "u"}))) as mock_run:
            gh.view_issue(7, repo="acme/repo")
        argv = mock_run.call_args.args[0]
        self.assertIn("--repo", argv)
        self.assertEqual(argv[argv.index("--repo") + 1], "acme/repo")

    def test_repo_view_uses_supplied_cwd(self):
        payload = {"nameWithOwner": "acme/repo", "isPrivate": True}
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout=json.dumps(payload))) as mock_run:
            gh.repo_view(cwd="/tmp/expected")
        self.assertEqual(mock_run.call_args.kwargs.get("cwd"), "/tmp/expected")

    def test_view_issue_missing_required_json_field_raises_output_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout=json.dumps({"number": 7, "title": "T"}))):
            with self.assertRaises(gh.GhOutputError):
                gh.view_issue(7, repo="acme/repo")

    def test_view_pr_builds_expected_argv_and_uses_shell_false(self):
        payload = {
            "number": 8,
            "title": "PR title",
            "state": "OPEN",
            "body": "Refs #5",
            "url": "https://github.com/acme/repo/pull/8",
            "headRefName": "feature",
            "baseRefName": "main",
            "isDraft": False,
        }
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout=json.dumps(payload))) as mock_run:
            result = gh.view_pr(8, repo="acme/repo")
        self.assertEqual(result["number"], 8)
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[:3], ["gh", "pr", "view"])
        self.assertIn("--repo", argv)
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)

    def test_create_pr_requires_explicit_head_and_base_and_never_pushes(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout="https://github.com/acme/repo/pull/9\n")) as mock_run:
            result = gh.create_pr("Title", "Refs #5", base="main", head="feature", repo="acme/repo")
        self.assertEqual(result["number"], 9)
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[:3], ["gh", "pr", "create"])
        self.assertIn("--base", argv)
        self.assertIn("--head", argv)
        self.assertNotIn("push", argv)
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)

    def test_create_draft_pr_uses_explicit_draft_flag(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout="https://github.com/acme/repo/pull/9\n")) as mock_run:
            gh.create_pr("Title", "Refs #5", base="main", head="pico/batch-1",
                         repo="acme/repo", draft=True)
        self.assertIn("--draft", mock_run.call_args.args[0])
        self.assertEqual(mock_run.call_args.kwargs["shell"], False)

    def test_issue_comment_is_repo_pinned_and_shell_safe(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc()) as mock_run:
            gh.comment_issue(7, "Validated.", repo="acme/repo")
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "issue", "comment", "7"])
        self.assertEqual(argv[argv.index("--body") + 1], "Validated.")
        self.assertEqual(argv[argv.index("--repo") + 1], "acme/repo")
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)

    def test_pr_edit_and_comment_are_explicit_allowlisted_operations(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc()) as mock_run:
            gh.edit_pr(8, title="Revised", body="## Summary\nDone", repo="acme/repo")
        self.assertEqual(mock_run.call_args.args[0], [
            "gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8",
            "-f", "title=Revised", "-f", "body=## Summary\nDone",
        ])
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)

        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc()) as mock_run:
            gh.comment_pr(8, "No blocking findings.", repo="acme/repo")
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "pr", "comment", "8"])
        self.assertEqual(argv[argv.index("--body") + 1], "No blocking findings.")
        self.assertEqual(argv[argv.index("--repo") + 1], "acme/repo")
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)

    def test_comment_and_pr_edit_validation_rejects_empty_or_missing_content(self):
        with self.assertRaises(ValueError):
            gh.comment_issue(1, "")
        with self.assertRaises(ValueError):
            gh.comment_pr(1, "   ")
        with self.assertRaises(ValueError):
            gh.edit_pr(1)

    def test_pr_merge_checkout_ready_are_not_allowlisted(self):
        for verb in ("merge", "checkout", "ready"):
            with self.assertRaises(gh.GithubSyncError):
                gh._run(["gh", "pr", verb, "1"])


class EditPrRestPatchTest(unittest.TestCase):
    """edit_pr uses the fixed REST shape `gh api -X PATCH repos/.../pulls/N`
    instead of `gh pr edit` (which fails on gh 2.45 via classic Projects
    GraphQL). The allowlist accepts only that exact argv shape."""

    def run_edit(self, number, **kwargs):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(stdout="{}")) as mock_run:
            result = gh.edit_pr(number, **kwargs)
        self.assertIsNone(result)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)
        return mock_run.call_args.args[0]

    def assert_rejected_before_subprocess(self, argv):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run") as mock_run:
            with self.assertRaises(gh.GithubSyncError, msg=repr(argv)):
                gh._run(argv)
            mock_run.assert_not_called()

    # -- exact argv ---------------------------------------------------------

    def test_explicit_repo_title_and_body(self):
        self.assertEqual(self.run_edit(8, title="Revised", body="## Summary\nDone", repo="acme/repo"), [
            "gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8",
            "-f", "title=Revised", "-f", "body=## Summary\nDone",
        ])

    def test_implicit_repo_uses_literal_gh_placeholders(self):
        self.assertEqual(self.run_edit(8, title="Revised", body="## Summary\nDone"), [
            "gh", "api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/8",
            "-f", "title=Revised", "-f", "body=## Summary\nDone",
        ])

    def test_title_only(self):
        self.assertEqual(self.run_edit(8, title="Revised", repo="acme/repo"), [
            "gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8", "-f", "title=Revised",
        ])

    def test_body_only(self):
        self.assertEqual(self.run_edit(8, body="Done"), [
            "gh", "api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/8", "-f", "body=Done",
        ])

    def test_empty_body_is_allowed(self):
        self.assertEqual(self.run_edit(8, body="", repo="acme/repo"), [
            "gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8", "-f", "body=",
        ])

    def test_values_are_passed_verbatim_as_raw_strings(self):
        for value in ("@/etc/passwd", "true", "false", "null", "123", "-1.5",
                      "$(rm -rf /); `id` | && > x ' \"", "line1\nline2\r\n\tline3",
                      "a=b=c", "=leading", "--repo other/repo", "-F body=x"):
            argv = self.run_edit(3, title=value, body=value, repo="acme/repo")
            self.assertEqual(argv, [
                "gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/3",
                "-f", f"title={value}", "-f", f"body={value}",
            ])
            self.assertNotIn("-F", argv[:5] + argv[5::2])

    # -- argument validation (unchanged semantics) --------------------------

    def test_argument_validation_rejects_before_subprocess(self):
        cases = [
            ((0,), {"title": "t"}),
            ((-1,), {"title": "t"}),
            ((True,), {"title": "t"}),
            (("8",), {"title": "t"}),
            ((8,), {}),
            ((8,), {"title": ""}),
            ((8,), {"title": "  \n\t"}),
            ((8,), {"title": 5}),
            ((8,), {"body": None, "title": None}),
            ((8,), {"body": b"bytes"}),
            ((8,), {"title": "t", "repo": "acme"}),
            ((8,), {"title": "t", "repo": "acme/repo/pulls"}),
            ((8,), {"title": "t", "repo": "../repo"}),
            ((8,), {"title": "t", "repo": "{owner}/{repo}"}),
            ((8,), {"title": "t", "repo": "acme/repo?x=1"}),
        ]
        for args, kwargs in cases:
            with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
                 patch.object(gh.subprocess, "run") as mock_run:
                with self.assertRaises(ValueError, msg=(args, kwargs)):
                    gh.edit_pr(*args, **kwargs)
                mock_run.assert_not_called()

    # -- existing error handling preserved ----------------------------------

    def test_nonzero_exit_raises_command_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode=1, stderr="gh: Not Found (HTTP 404)")):
            with self.assertRaises(gh.GhCommandError) as ctx:
                gh.edit_pr(8, title="t", repo="acme/repo")
        self.assertEqual(ctx.exception.argv[:5], ["gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8"])

    def test_auth_failure_raises_auth_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode=1, stderr="not logged in")):
            with self.assertRaises(gh.GhAuthError):
                gh.edit_pr(8, title="t")

    def test_gh_unavailable_raises_before_subprocess(self):
        with patch.object(gh.shutil, "which", return_value=None), \
             patch.object(gh.subprocess, "run") as mock_run:
            with self.assertRaises(gh.GhUnavailableError):
                gh.edit_pr(8, title="t")
            mock_run.assert_not_called()

    def test_timeout_raises_timeout_error(self):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(gh.subprocess, "run", side_effect=subprocess.TimeoutExpired(["gh"], 30)):
            with self.assertRaises(gh.GhTimeoutError):
                gh.edit_pr(8, title="t")

    # -- allowlist: only the whole fixed PATCH shape -------------------------

    def test_allowlist_accepts_exact_shapes(self):
        for argv in (
            ["gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8", "-f", "title=t"],
            ["gh", "api", "-X", "PATCH", "repos/acme/repo/pulls/8", "-f", "body="],
            ["gh", "api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/12", "-f", "title=t", "-f", "body=b=c"],
            ["gh", "api", "-X", "PATCH", "repos/Acme-1/re.po_x/pulls/1", "-f", "title=@x", "-f", "body=true"],
        ):
            gh._validate_allowlisted(argv)

    def test_old_pr_edit_is_no_longer_allowlisted(self):
        self.assertNotIn(("pr", "edit"), gh.ALLOWED_COMMANDS)
        self.assert_rejected_before_subprocess(["gh", "pr", "edit", "8", "--title", "t"])

    def test_allowlist_rejects_everything_but_the_fixed_shape(self):
        ok_head = ["gh", "api", "-X", "PATCH"]
        ep = "repos/acme/repo/pulls/8"
        rejected = [
            # generic api use
            ["gh", "api", "repos/x/y"],
            ["gh", "api", "user"],
            ["gh", "api", "graphql", "-f", "query=mutation{}"],
            ["gh", "api", ep],
            ["gh", "api", "-X", "PATCH"],
            ["gh", "api", "-X", "PATCH", ep],
            # wrong method
            ["gh", "api", "-X", "POST", ep, "-f", "title=t"],
            ["gh", "api", "-X", "DELETE", ep, "-f", "title=t"],
            ["gh", "api", "-X", "patch", ep, "-f", "title=t"],
            ["gh", "api", "--method", "PATCH", ep, "-f", "title=t"],
            ["gh", "api", "-XPATCH", ep, "-f", "title=t", "-f", "body=b"],
            ["gh", "api", ep, "-X", "PATCH", "-f", "title=t"],
            # wrong resource / number
            ok_head + ["repos/acme/repo/issues/8", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/8/merge", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/8/", "-f", "title=t"],
            ok_head + ["/repos/acme/repo/pulls/8", "-f", "title=t"],
            ok_head + ["https://api.github.com/repos/acme/repo/pulls/8", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/8?state=closed", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/8#x", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/8\n", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/0", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/08", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/-8", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/x", "-f", "title=t"],
            ok_head + ["repos/acme/repo/pulls/", "-f", "title=t"],
            ok_head + ["repos/acme/pulls/8", "-f", "title=t"],
            ok_head + ["repos/a/b/c/pulls/8", "-f", "title=t"],
            ok_head + ["repos/acme/../pulls/8", "-f", "title=t"],
            ok_head + ["repos/acme/re..po/pulls/8", "-f", "title=t"],
            ok_head + ["repos/-acme/repo/pulls/8", "-f", "title=t"],
            ok_head + ["repos/ac me/repo/pulls/8", "-f", "title=t"],
            # partial / malformed placeholders
            ok_head + ["repos/{owner}/repo/pulls/8", "-f", "title=t"],
            ok_head + ["repos/acme/{repo}/pulls/8", "-f", "title=t"],
            ok_head + ["repos/{repo}/{owner}/pulls/8", "-f", "title=t"],
            ok_head + ["repos/{owner}x/{repo}/pulls/8", "-f", "title=t"],
            ok_head + ["repos/{owner/{repo}/pulls/8", "-f", "title=t"],
            ok_head + ["repos/{branch}/{repo}/pulls/8", "-f", "title=t"],
            # fields: duplicates, unknown keys, ordering, typed/unknown flags
            ok_head + [ep, "-f", "title=a", "-f", "title=b"],
            ok_head + [ep, "-f", "body=a", "-f", "body=b"],
            ok_head + [ep, "-f", "body=b", "-f", "title=t"],
            ok_head + [ep, "-f", "state=closed"],
            ok_head + [ep, "-f", "title=t", "-f", "base=main"],
            ok_head + [ep, "-f", "Title=t"],
            ok_head + [ep, "-f", " title=t"],
            ok_head + [ep, "-f", "title"],
            ok_head + [ep, "-f", "=t"],
            ok_head + [ep, "-F", "title=t"],
            ok_head + [ep, "--raw-field", "title=t"],
            ok_head + [ep, "--field", "title=t"],
            ok_head + [ep, "-f", "title=t", "--input", "file.json"],
            ok_head + [ep, "-f", "title=t", "-H", "Accept: x"],
            ok_head + [ep, "-f", "title=t", "--hostname", "evil.example"],
            ok_head + [ep, "-f", "title=t", "-f"],
            ok_head + [ep, "-f", "title=t", "-f", "body=b", "-f", "title=u"],
            ok_head + [ep, "title=t"],
            # title content rules
            ok_head + [ep, "-f", "title="],
            ok_head + [ep, "-f", "title=   "],
            ok_head + [ep, "-f", "title=\n", "-f", "body=b"],
            # non-string elements
            ok_head + [ep, "-f", None],
            ok_head + [ep, "-f", 5],
        ]
        for argv in rejected:
            self.assert_rejected_before_subprocess(argv)


# ---------------------------------------------------------------------------
# tools/backlog.py -- GitHub composition layer (status/sync/push/pull)
# ---------------------------------------------------------------------------

def make_repo_root(tmp_root: Path) -> Path:
    repo = tmp_root / "repo"
    (repo / "schemas").mkdir(parents=True)
    (repo / "backlog").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "schemas" / "backlog-item.schema.json",
        repo / "schemas" / "backlog-item.schema.json",
    )
    return repo


class PublicationGateReadsTest(unittest.TestCase):
    """The two fixed read-only `gh api -X GET` shapes the publication gate uses."""

    SHA = "a" * 40

    def call(self, fn, *args, stdout="", returncode=0, stderr="", **kwargs):
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
             patch.dict(gh.os.environ, {"GH_HOST": "evil.example", "GH_CONFIG_DIR": "/cfg"}), \
             patch.object(gh.subprocess, "run", return_value=fake_proc(returncode, stdout, stderr)) as mock_run:
            result = fn(*args, **kwargs)
        self.assertEqual(mock_run.call_args.kwargs.get("shell"), False)
        # The host is pinned in argv and GH_HOST cannot override it; the rest of the environment is kept.
        env = mock_run.call_args.kwargs.get("env")
        self.assertNotIn("GH_HOST", env)
        self.assertEqual(env.get("GH_CONFIG_DIR"), "/cfg")
        self.assertEqual(mock_run.call_args.args[0][-2:], ["--hostname", "github.com"])
        return result, mock_run.call_args.args[0]

    def test_branch_head_uses_the_fixed_argv_and_validates_the_shape(self):
        ref = {"ref": "refs/heads/release/1.x", "object": {"type": "commit", "sha": self.SHA}}
        sha, argv = self.call(gh.branch_head, "release/1.x", repo="acme/repo", stdout=json.dumps(ref))
        self.assertEqual(sha, self.SHA)
        self.assertEqual(argv, ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/release/1.x",
                                "--hostname", "github.com"])
        self.assertIsNone(self.call(gh.branch_head, "main", repo="acme/repo", returncode=1,
                                    stderr="gh: Not Found (HTTP 404)")[0])
        for bad in ({"ref": "refs/heads/other", "object": {"type": "commit", "sha": self.SHA}},
                    {"ref": "refs/heads/main", "object": {"type": "tag", "sha": self.SHA}},
                    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "xyz"}},
                    [{"ref": "refs/heads/main"}]):
            with self.subTest(bad=bad), self.assertRaises(gh.GhOutputError):
                self.call(gh.branch_head, "main", repo="acme/repo", stdout=json.dumps(bad))
        with self.assertRaises(gh.GhCommandError):
            self.call(gh.branch_head, "main", repo="acme/repo", returncode=1, stderr="HTTP 500")
        for branch in ("../x", "a..b", "a b", "a?b", "-x", "a#b", "", "a/", "/a"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                gh.branch_head(branch, repo="acme/repo")

    def test_default_branch_uses_the_fixed_argv_and_validates_the_shape(self):
        info = {"full_name": "Acme/Repo", "default_branch": "release/1.x"}
        name, argv = self.call(gh.default_branch, repo="acme/repo", stdout=json.dumps(info))
        self.assertEqual(name, "release/1.x")
        self.assertEqual(argv, ["gh", "api", "-X", "GET", "repos/acme/repo", "--hostname", "github.com"])
        for bad in ({"full_name": "acme/other", "default_branch": "main"},  # a rename or transfer redirect
                    {"full_name": "acme/repo", "default_branch": "../x"},
                    {"full_name": "acme/repo", "default_branch": 3},
                    {"default_branch": "main"}, ["acme/repo"]):
            with self.subTest(bad=bad), self.assertRaises(gh.GhOutputError):
                self.call(gh.default_branch, repo="acme/repo", stdout=json.dumps(bad))
        with self.assertRaises(gh.GhOutputError):
            self.call(gh.default_branch, repo="acme/repo", returncode=1, stderr="gh: Not Found (HTTP 404)")
        with self.assertRaises(gh.GhCommandError):
            self.call(gh.default_branch, repo="acme/repo", returncode=1, stderr="HTTP 500")

    def test_get_allowlist_accepts_only_the_repository_branch_and_policy_endpoints(self):
        base = [gh.GH_BINARY, "api", "-X", "GET"]
        pin = ["--hostname", "github.com"]
        for endpoint in ("repos/acme/repo", "repos/acme/repo/git/ref/heads/main",
                         f"repos/acme/repo/contents/.pi/autonomy.yaml?ref={'a' * 40}"):
            gh._validate_allowlisted(base + [endpoint] + pin)
        for endpoint in ("repos/acme/repo/pulls", "repos/acme/repo/contents/README.md?ref=" + "a" * 40,
                         "user", "repos/acme/repo/", "repos/acme"):
            with self.subTest(endpoint=endpoint), self.assertRaises(gh.GithubSyncError):
                gh._validate_allowlisted(base + [endpoint] + pin)
        with self.assertRaises(gh.GithubSyncError):
            gh._validate_allowlisted(base + ["repos/acme/repo", "--hostname", "evil.example"])

    def test_policy_file_at_decodes_and_reports_type_and_path(self):
        import base64
        content = {"type": "file", "path": ".pi/autonomy.yaml", "sha": "b" * 40, "encoding": "base64",
                   "content": base64.b64encode(b"issue_reference: closes\n").decode()}
        entry, argv = self.call(gh.policy_file_at, self.SHA, repo="acme/repo", stdout=json.dumps(content))
        self.assertEqual(argv, ["gh", "api", "-X", "GET", f"repos/acme/repo/contents/.pi/autonomy.yaml?ref={self.SHA}",
                                "--hostname", "github.com"])
        self.assertEqual(entry, {"type": "file", "path": ".pi/autonomy.yaml", "sha": "b" * 40,
                                 "text": "issue_reference: closes\n"})
        symlink = {"type": "symlink", "path": ".pi/autonomy.yaml", "sha": "c" * 40, "target": "/etc/x"}
        self.assertEqual(self.call(gh.policy_file_at, self.SHA, repo="acme/repo", stdout=json.dumps(symlink))[0]["type"],
                         "symlink")
        self.assertEqual(self.call(gh.policy_file_at, self.SHA, repo="acme/repo", stdout="[]")[0]["type"], "dir")
        self.assertIsNone(self.call(gh.policy_file_at, self.SHA, repo="acme/repo", returncode=1,
                                    stderr="gh: Not Found (HTTP 404)")[0])
        with self.assertRaises(gh.GhOutputError):
            self.call(gh.policy_file_at, self.SHA, repo="acme/repo", stdout=json.dumps({"type": "weird"}))
        with self.assertRaises(ValueError):
            gh.policy_file_at("main", repo="acme/repo")

    def test_allowlist_accepts_only_the_two_get_shapes(self):
        host = ["--hostname", "github.com"]
        gh._validate_allowlisted(["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main", *host])
        gh._validate_allowlisted(["gh", "api", "-X", "GET", f"repos/acme/repo/contents/.pi/autonomy.yaml?ref={self.SHA}",
                                  *host])
        for argv in (
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main", "--hostname", "evil.example"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main", "--hostname"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main", "github.com", "--hostname"],
                ["gh", "api", "--hostname", "github.com", "-X", "GET", "repos/acme/repo/git/ref/heads/main"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/refs/heads/main", "--hostname", "github.com"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/a..b"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main?x=1"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/contents/.pi/autonomy.yaml"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/contents/.pi/autonomy.yaml?ref=main"],
                ["gh", "api", "-X", "GET", "repos/acme/repo/contents/secrets.env?ref=" + self.SHA],
                ["gh", "api", "-X", "GET", "repos/acme/repo/git/ref/heads/main", "-f", "x=y"],
                ["gh", "api", "-X", "GET", "user"],
                ["gh", "api", "-X", "GET", "repos/../repo/git/ref/heads/main"],
                ["gh", "api", "-X", "POST", "repos/acme/repo/git/ref/heads/main"]):
            with self.subTest(argv=argv):
                with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), \
                     patch.object(gh.subprocess, "run") as mock_run:
                    with self.assertRaises(gh.GithubSyncError):
                        gh._run(argv)
                    mock_run.assert_not_called()


class BacklogGithubCompositionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_repo_root(Path(self._tmp.name))
        self._repo_patch = patch.object(backlog_cli, "_repo_name", return_value="acme/repo")
        self._repo_patch.start()
        # Issue writes are authorized by the default branch's policy on GitHub (#58); tested separately below.
        self._authority_patch = patch.object(backlog_cli, "_require_issue_writes")
        self.require_issue_writes = self._authority_patch.start()

    def tearDown(self):
        self._authority_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def test_github_sync_status_reports_unavailable(self):
        with patch.object(backlog_cli.gh, "gh_available", return_value=False):
            status = backlog_cli.github_sync_status(self.repo_root)
        self.assertFalse(status["gh_available"])
        self.assertFalse(status["authenticated"])

    def test_github_sync_status_reports_not_authenticated(self):
        with patch.object(backlog_cli.gh, "gh_available", return_value=True), \
             patch.object(backlog_cli.gh, "auth_status", return_value={"authenticated": False, "detail": "no auth"}):
            status = backlog_cli.github_sync_status(self.repo_root)
        self.assertTrue(status["gh_available"])
        self.assertFalse(status["authenticated"])

    def test_github_sync_status_reports_private_repo(self):
        repo_payload = {"nameWithOwner": "acme/secret", "isPrivate": True}
        with patch.object(backlog_cli.gh, "gh_available", return_value=True), \
             patch.object(backlog_cli.gh, "auth_status", return_value={"authenticated": True, "detail": "ok"}), \
             patch.object(backlog_cli.gh, "repo_view", return_value=repo_payload):
            status = backlog_cli.github_sync_status(self.repo_root)
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["repo"]["isPrivate"])

    def test_github_push_task_creates_issue_when_none_exists(self):
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            acceptance_criteria=["bug is fixed"], repo_root=self.repo_root,
        )
        with patch.object(backlog_cli.gh, "create_issue", return_value={"number": 99, "url": "https://x/issues/99"}) as mock_create:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["issue"], 99)
        mock_create.assert_called_once()
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["github_issue"], 99)
        self.assertEqual(item["github_sync"]["remote_state"], "open")

    def test_github_push_task_checks_issue_permission_before_every_write(self):
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            acceptance_criteria=["bug is fixed"], repo_root=self.repo_root,
        )
        refusal = backlog_cli.github_integration.IntegrationError(
            "GITHUB_WRITE_NOT_AUTHORIZED", "create_github_issues must be granted")
        self.require_issue_writes.side_effect = refusal
        with patch.object(backlog_cli.gh, "create_issue") as mock_create:
            with self.assertRaises(backlog_cli.github_integration.IntegrationError):
                backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_create.assert_not_called()
        self.require_issue_writes.assert_called_once_with("acme/repo")
        self.assertIsNone(store.backlog_get("ORCH-001", self.repo_root).get("github_issue"))

        # A linked item whose push would edit or close the issue is refused before either call.
        store.backlog_update("ORCH-001", github_issue=5, repo_root=self.repo_root)
        for target in ("planned", "ready", "in_progress", "review", "completed"):
            store.backlog_update("ORCH-001", status=target, repo_root=self.repo_root)
        remote = {"number": 5, "title": "Old title", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit, \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            with self.assertRaises(backlog_cli.github_integration.IntegrationError):
                backlog_cli.github_push_task("ORCH-001", self.repo_root, establish_baseline=True)
        mock_edit.assert_not_called()
        mock_close.assert_not_called()

    def test_github_push_task_in_sync_needs_no_issue_permission(self):
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        self.require_issue_writes.side_effect = AssertionError("a push that writes nothing must not need permission")
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            self.assertEqual(backlog_cli.github_push_task("ORCH-001", self.repo_root)["action"], ["no_change"])

    def test_require_issue_writes_reads_the_default_branch_policy_on_github(self):
        self._authority_patch.stop()
        try:
            template = (REPO_ROOT / "templates/autonomy.yaml").read_text()
            for granted in (True, False):
                text = template.replace("create_github_issues: true", f"create_github_issues: {str(granted).lower()}")
                entry = {"type": "file", "path": ".pi/autonomy.yaml", "sha": "e" * 40, "text": text}
                with self.subTest(granted=granted), \
                     patch.object(backlog_cli.gh, "default_branch", return_value="main") as branch, \
                     patch.object(backlog_cli.gh, "branch_head", return_value="f" * 40), \
                     patch.object(backlog_cli.gh, "policy_file_at", return_value=entry) as policy:
                    if granted:
                        backlog_cli._require_issue_writes("acme/repo")
                    else:
                        with self.assertRaises(backlog_cli.github_integration.IntegrationError) as caught:
                            backlog_cli._require_issue_writes("acme/repo")
                        self.assertEqual(caught.exception.code, "GITHUB_WRITE_NOT_AUTHORIZED")
                    branch.assert_called_once_with(repo="acme/repo")
                    policy.assert_called_once_with("f" * 40, repo="acme/repo")
        finally:
            self._authority_patch.start()

    def test_github_push_task_close_guard_blocks_close_when_not_completed(self):
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        # Simulate: item's status is 'planned' (not completed), but somehow
        # the desired state computation is exercised directly to prove the
        # guard fires even if reached.
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        # status stayed 'planned' -> desired remote state is 'open' == actual
        # remote state, so nothing to close; assert the guard was never
        # reached with a mismatched state by forcing status to 'review' then
        # 'completed' is required to actually close -- verified below.
        mock_close.assert_not_called()
        self.assertEqual(result["action"], ["no_change"])

    def test_github_push_task_closes_issue_once_completed(self):
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        # Establish a verified sync baseline first (see
        # test_github_push_task_without_baseline_and_divergent_remote_refuses_to_overwrite):
        # remote already exactly matches local here, so this push is a safe,
        # no-mutation no-op that simply records the baseline snapshot.
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            baseline_result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        self.assertEqual(baseline_result["action"], ["no_change"])
        self.assertIsNotNone(store.backlog_get("ORCH-001", self.repo_root).get("github_sync"))

        for target in ("planned", "ready", "in_progress", "review", "completed"):
            store.backlog_update("ORCH-001", status=target, repo_root=self.repo_root)
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_close.assert_called_once_with(5, repo="acme/repo")
        self.assertIn("closed", result["action"])

    def test_github_push_task_never_closes_issue_for_non_completed_item_even_if_attempted_directly(self):
        """Defense in depth: github_sync.close_issue itself has no backlog
        awareness, so this asserts the *composition* layer's guard, not the
        boundary module (which cannot enforce it -- documented in
        tools/github_sync.py's close_issue docstring)."""
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        # Establish a verified baseline first (remote matches local exactly
        # at this point), per the same baseline requirement exercised in
        # test_github_push_task_without_baseline_and_divergent_remote_refuses_to_overwrite.
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            backlog_cli.github_push_task("ORCH-001", self.repo_root)

        store.backlog_update("ORCH-001", status="planned", repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="ready", repo_root=self.repo_root)
        store.backlog_update("ORCH-001", status="in_progress", repo_root=self.repo_root)
        # Local title changed (to force a push action) while status is
        # in_progress (not completed): remote must not be closed even though
        # remote happens to already differ.
        store.backlog_update("ORCH-001", title="Fix bug (renamed)", repo_root=self.repo_root)
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit, \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_edit.assert_called_once()
        mock_close.assert_not_called()

    # -----------------------------------------------------------------
    # Finding 1: conflict detection must not be silently disabled when no
    # prior sync snapshot exists.
    # -----------------------------------------------------------------

    def test_github_push_task_without_baseline_and_divergent_remote_refuses_to_overwrite(self):
        """Linking directly to a pre-existing GitHub issue (github_issue=5
        set at backlog_add time, never synced) whose remote state diverges
        from what a naive push would write must NOT be silently overwritten.
        Without an established baseline there is no proof the divergence is
        expected, so github_push_task must refuse rather than clobber."""
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        # Issue #5 pre-exists independently of this backlog item: different
        # title and labels, and no github_sync snapshot has ever been
        # recorded for it.
        remote = {
            "number": 5, "title": "Some unrelated pre-existing issue", "state": "open",
            "labels": [{"name": "unrelated"}], "body": "", "url": "u",
        }
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit, \
             patch.object(backlog_cli.gh, "close_issue") as mock_close, \
             patch.object(backlog_cli.gh, "reopen_issue") as mock_reopen:
            with self.assertRaises(backlog_cli.SyncConflictError):
                backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_edit.assert_not_called()
        mock_close.assert_not_called()
        mock_reopen.assert_not_called()
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertIsNone(item.get("github_sync"))  # no baseline silently recorded either

    def test_github_push_task_creating_brand_new_issue_needs_no_baseline_ceremony(self):
        """The legitimate initial-creation flow (github_issue is None, this
        call itself creates the issue) is unaffected by the baseline
        requirement -- there is nothing pre-existing to clobber."""
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            repo_root=self.repo_root,
        )
        with patch.object(backlog_cli.gh, "create_issue", return_value={"number": 42, "url": "https://x/issues/42"}) as mock_create:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        self.assertEqual(result["action"], "created")
        mock_create.assert_called_once()
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertIsNotNone(item.get("github_sync"))

    def test_github_push_task_establish_baseline_true_allows_deliberate_initial_link_overwrite(self):
        """The explicit escape hatch: a caller who deliberately wants to
        claim/overwrite a pre-existing issue's fields on first link passes
        establish_baseline=True."""
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Old remote title", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit, \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root, establish_baseline=True)
        mock_edit.assert_called_once_with(5, repo="acme/repo", title="Fix bug")
        mock_close.assert_not_called()
        self.assertIn("title_updated", result["action"])
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["github_sync"]["remote_title"], "Fix bug")

    def test_github_push_task_baseline_via_pull_then_push_succeeds(self):
        """The other sanctioned path: run github_pull_task first to import
        remote state and establish a baseline, then a subsequent push of a
        genuinely local-only change proceeds without needing
        establish_baseline=True."""
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            pull_result = backlog_cli.github_pull_task(5, self.repo_root)
        self.assertFalse(pull_result["has_baseline"])  # this call established the first one

        store.backlog_update("ORCH-001", title="Renamed locally", repo_root=self.repo_root)
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_edit.assert_called_once_with(5, repo="acme/repo", title="Renamed locally")
        self.assertIn("title_updated", result["action"])

    def test_github_sync_task_no_baseline_and_divergent_reports_baseline_required(self):
        store.backlog_add(
            title="Fix bug", description="d", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Different remote title", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            result = backlog_cli.github_sync_task("ORCH-001", self.repo_root)
        self.assertEqual(result["status"], "baseline_required")
        self.assertFalse(result["has_baseline"])

    def test_github_sync_task_no_baseline_but_matching_remote_reports_in_sync(self):
        store.backlog_add(
            title="Fix bug", description="d", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            result = backlog_cli.github_sync_task("ORCH-001", self.repo_root)
        self.assertEqual(result["status"], "in_sync")
        self.assertFalse(result["has_baseline"])

    # -----------------------------------------------------------------
    # Finding 2: the close-guard must be an independently-reachable,
    # independently-enforced invariant, not dead code implied by
    # _desired_remote_state's own construction.
    # -----------------------------------------------------------------

    def test_guard_close_raises_for_non_completed_item(self):
        item = {"id": "ORCH-001", "status": "planned", "github_issue": 5}
        with self.assertRaises(backlog_cli.CloseGuardError):
            backlog_cli._guard_close(item)

    def test_guard_close_allows_completed_item(self):
        item = {"id": "ORCH-001", "status": "completed", "github_issue": 5}
        backlog_cli._guard_close(item)  # must not raise

    def test_close_guard_is_reachable_and_blocking_independent_of_desired_remote_state(self):
        """Prove the guard actually fires through github_push_task's real
        call path -- not merely that _desired_remote_state's own
        construction happens to never trigger it. We force
        _desired_remote_state to report 'closed' while the backlog item's
        status remains 'proposed' (not completed); _guard_close alone must
        be what blocks the close, and gh.close_issue must never be called."""
        store.backlog_add(
            title="Fix bug", description="Fix the bug.", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            backlog_cli.github_push_task("ORCH-001", self.repo_root)  # establish baseline

        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["status"], "proposed")  # not completed
        with patch.object(backlog_cli, "_desired_remote_state", return_value="closed"), \
             patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            with self.assertRaises(backlog_cli.CloseGuardError):
                backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_close.assert_not_called()

    # -----------------------------------------------------------------
    # Finding 3: label reconciliation must actually be implemented, not
    # merely claimed in the docstring.
    # -----------------------------------------------------------------

    def test_github_push_task_reconciles_local_only_label_change(self):
        store.backlog_add(
            title="Fix bug", description="d", version="v1", id="ORCH-001",
            github_issue=5, labels=["bug"], repo_root=self.repo_root,
        )
        store.backlog_update(
            "ORCH-001",
            github_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "issue": 5,
                "remote_title": "Fix bug",
                "remote_state": "open",
                "remote_labels": [],
            },
            repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "Fix bug", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit:
            result = backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_edit.assert_called_once_with(5, repo="acme/repo", add_labels=["bug"])
        self.assertIn("labels_updated", result["action"])
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["github_sync"]["remote_labels"], ["bug"])

    def test_github_pull_task_picks_up_remote_only_label_change(self):
        store.backlog_add(
            title="Fix bug", description="d", version="v1", id="ORCH-001",
            github_issue=5, labels=["bug"], repo_root=self.repo_root,
        )
        store.backlog_update(
            "ORCH-001",
            github_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "issue": 5,
                "remote_title": "Fix bug",
                "remote_state": "open",
                "remote_labels": ["bug"],
            },
            repo_root=self.repo_root,
        )
        remote = {
            "number": 5, "title": "Fix bug", "state": "open",
            "labels": [{"name": "bug"}, {"name": "urgent"}], "body": "", "url": "u",
        }
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            result = backlog_cli.github_pull_task(5, self.repo_root)
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["labels"], ["bug", "urgent"])
        self.assertIn("labels", result["updated_fields"])

    def test_github_push_task_divergent_label_changes_conflict_and_do_not_push(self):
        store.backlog_add(
            title="Fix bug", description="d", version="v1", id="ORCH-001",
            github_issue=5, labels=["bug"], repo_root=self.repo_root,
        )
        store.backlog_update(
            "ORCH-001",
            github_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "issue": 5,
                "remote_title": "Fix bug",
                "remote_state": "open",
                "remote_labels": [],
            },
            repo_root=self.repo_root,
        )
        # Local added "bug" since the baseline (which had no labels); remote
        # independently added a *different* label "urgent" since that same
        # baseline: a genuine divergent-labels conflict.
        remote = {
            "number": 5, "title": "Fix bug", "state": "open",
            "labels": [{"name": "urgent"}], "body": "", "url": "u",
        }
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit:
            with self.assertRaises(backlog_cli.SyncConflictError):
                backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_edit.assert_not_called()

    def test_github_sync_task_no_issue_returns_no_issue_status(self):
        store.backlog_add(title="A", description="d", version="v1", id="ORCH-001", repo_root=self.repo_root)
        result = backlog_cli.github_sync_task("ORCH-001", self.repo_root)
        self.assertEqual(result["status"], "no_issue")

    def test_github_sync_task_detects_conflict_without_mutating(self):
        store.backlog_add(
            title="Original title", description="d", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        # Establish a prior synced snapshot.
        store.backlog_update(
            "ORCH-001",
            github_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "issue": 5,
                "remote_title": "Original title",
                "remote_state": "open",
                "remote_labels": [],
            },
            repo_root=self.repo_root,
        )
        # Local changes the title independently...
        store.backlog_update("ORCH-001", title="Locally renamed", repo_root=self.repo_root)
        # ...and remote also changed independently, to a *different* value.
        remote = {"number": 5, "title": "Remotely renamed", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            result = backlog_cli.github_sync_task("ORCH-001", self.repo_root)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["title_conflict"])
        # No mutation should have occurred: local item unchanged since check.
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["title"], "Locally renamed")

    def test_github_push_task_raises_sync_conflict_error_and_does_not_mutate(self):
        store.backlog_add(
            title="Original title", description="d", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        store.backlog_update(
            "ORCH-001",
            github_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "issue": 5,
                "remote_title": "Original title",
                "remote_state": "open",
                "remote_labels": [],
            },
            repo_root=self.repo_root,
        )
        store.backlog_update("ORCH-001", title="Locally renamed", repo_root=self.repo_root)
        remote = {"number": 5, "title": "Remotely renamed", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote), \
             patch.object(backlog_cli.gh, "edit_issue") as mock_edit, \
             patch.object(backlog_cli.gh, "close_issue") as mock_close:
            with self.assertRaises(backlog_cli.SyncConflictError):
                backlog_cli.github_push_task("ORCH-001", self.repo_root)
        mock_edit.assert_not_called()
        mock_close.assert_not_called()
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["title"], "Locally renamed")  # unmutated

    def test_github_pull_task_unmapped_issue_raises(self):
        with self.assertRaises(backlog_cli.UnmappedIssueError):
            backlog_cli.github_pull_task(123, self.repo_root)

    def test_github_pull_task_updates_local_title_and_never_changes_status(self):
        store.backlog_add(
            title="Old title", description="d", version="v1", id="ORCH-001",
            github_issue=5, status="proposed", repo_root=self.repo_root,
        )
        remote = {"number": 5, "title": "New title from GitHub", "state": "closed", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            result = backlog_cli.github_pull_task(5, self.repo_root)
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["title"], "New title from GitHub")
        self.assertEqual(item["status"], "proposed")  # unchanged: status is never pulled
        self.assertIsNotNone(result["discrepancy"])  # remote closed but local not completed

    def test_github_pull_task_conflict_refuses_to_pick_a_side(self):
        store.backlog_add(
            title="Original title", description="d", version="v1", id="ORCH-001",
            github_issue=5, repo_root=self.repo_root,
        )
        store.backlog_update(
            "ORCH-001",
            github_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "issue": 5,
                "remote_title": "Original title",
                "remote_state": "open",
                "remote_labels": [],
            },
            repo_root=self.repo_root,
        )
        store.backlog_update("ORCH-001", title="Locally renamed", repo_root=self.repo_root)
        remote = {"number": 5, "title": "Remotely renamed", "state": "open", "labels": [], "body": "", "url": "u"}
        with patch.object(backlog_cli.gh, "view_issue", return_value=remote):
            with self.assertRaises(backlog_cli.SyncConflictError):
                backlog_cli.github_pull_task(5, self.repo_root)
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["title"], "Locally renamed")  # unmutated

    def _add_review_item(self, *, github_issue=5):
        store.backlog_add(
            title="Implement feature", description="d", version="v1", id="ORCH-001",
            github_issue=github_issue, repo_root=self.repo_root,
        )
        for target in ("planned", "ready", "in_progress", "review"):
            store.backlog_update("ORCH-001", status=target, repo_root=self.repo_root)

    def test_backlog_cli_cannot_create_a_pr(self):
        # `github-pr-create` called gh pr create for any base and head with no permission check, dry run
        # or plan hash. PRs are opened only by github_integration.publish (default-branch policy gate).
        self._add_review_item(github_issue=5)
        self.assertFalse(hasattr(backlog_cli, "github_pr_create_task"))
        with patch.object(backlog_cli.gh, "create_pr") as mock_create, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            backlog_cli.main(["github-pr-create", "ORCH-001", "--base", "main", "--head", "feature"])
        self.assertNotEqual(ctx.exception.code, 0)
        mock_create.assert_not_called()
        self.assertNotIn("github-pr-create", backlog_cli.build_parser().format_help())

    def test_github_pr_link_records_existing_remote_without_editing_pr(self):
        self._add_review_item(github_issue=5)
        remote = {
            "number": 8,
            "title": "Implement feature",
            "state": "OPEN",
            "body": "Refs #5",
            "url": "https://github.com/acme/repo/pull/8",
            "headRefName": "feature",
            "baseRefName": "main",
            "isDraft": False,
        }
        with patch.object(backlog_cli.gh, "view_pr", return_value=remote):
            result = backlog_cli.github_pr_link_task("ORCH-001", 8, self.repo_root)
        self.assertEqual(result["action"], "linked")
        self.assertTrue(result["issue_reference_present"])
        item = store.backlog_get("ORCH-001", self.repo_root)
        self.assertEqual(item["github_pr"], 8)
        self.assertEqual(item["github_pr_sync"]["remote_state"], "open")

    def test_github_pr_sync_reports_conflict_without_mutating(self):
        self._add_review_item(github_issue=5)
        body = backlog_cli._format_pr_body(store.backlog_get("ORCH-001", self.repo_root))
        store.backlog_update(
            "ORCH-001",
            github_pr=8,
            github_pr_sync={
                "last_synced_at": "2026-01-01T00:00:00Z",
                "repo": "acme/repo",
                "pr": 8,
                "issue": 5,
                "remote_title": "Implement feature",
                "remote_state": "open",
                "remote_body": body,
                "head_ref": "feature",
                "base_ref": "main",
                "is_draft": False,
            },
            repo_root=self.repo_root,
        )
        store.backlog_update("ORCH-001", title="Local title", repo_root=self.repo_root)
        remote = {
            "number": 8,
            "title": "Remote title",
            "state": "OPEN",
            "body": body,
            "url": "https://github.com/acme/repo/pull/8",
            "headRefName": "feature",
            "baseRefName": "main",
            "isDraft": False,
        }
        with patch.object(backlog_cli.gh, "view_pr", return_value=remote):
            result = backlog_cli.github_pr_sync_task("ORCH-001", self.repo_root)
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["title_conflict"])
        self.assertEqual(store.backlog_get("ORCH-001", self.repo_root)["title"], "Local title")


if __name__ == "__main__":
    unittest.main()
