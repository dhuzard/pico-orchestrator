"""External target integration and trust-boundary regression tests; no live GitHub."""
import hashlib
import inspect
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import github_integration as gi
import github_sync as gh
import provenance
import workspace_contracts


class FakeGithub:
    def __init__(self):
        self.issues = {n: {"number": n, "title": f"Issue {n}", "body": "old", "state": "OPEN",
                           "labels": [], "comments": []}
                       for n in (401, 464, 462, 457)}
        self.prs = {
            24: {"number": 24, "title": "Old PR", "body": "old", "state": "OPEN",
                 "url": "https://github.com/Examplecorp/examplelab/pull/24",
                 "headRefName": "feature", "baseRefName": "main", "isDraft": False, "comments": []}
        }
        self.labels = []
        self.calls = []
        self.change_before_second_read = False
        self.reads = 0
        # The default branch's .pi/autonomy.yaml on GitHub grants every write unless a test withholds one.
        self.withheld: set[str] = set()
        self.policy_present = True

    def default_branch(self, *, repo):
        return "main"

    def branch_head(self, branch, *, repo):
        return "d" * 40

    def policy_file_at(self, commit, *, repo):
        if not self.policy_present:
            return None
        text = (ROOT / "templates/autonomy.yaml").read_text()
        for name in ("create_github_issues", "comment_on_issue", "comment_on_pr", "create_pr"):
            text = re.sub(rf"(?m)^(  {name}): (true|false)$", rf"\1: {str(name not in self.withheld).lower()}", text)
        return {"type": "file", "path": ".pi/autonomy.yaml", "sha": hashlib.sha1(text.encode()).hexdigest(),
                "text": text}

    def view_issue(self, number, *, repo):
        self.calls.append(("view", repo, number))
        self.reads += 1
        if self.change_before_second_read and self.reads == 2:
            self.issues[number]["title"] = "conflicting title"
        return json.loads(json.dumps(self.issues[number]))

    def list_issues(self, *, state, limit, repo):
        self.calls.append(("list", repo))
        return list(self.issues.values())

    def view_pr(self, number, *, repo):
        self.calls.append(("view_pr", repo, number))
        return json.loads(json.dumps(self.prs[number]))

    def list_labels(self, *, repo):
        self.calls.append(("labels", repo))
        return self.labels

    def edit_issue(self, number, *, title=None, body=None, add_labels=None, remove_labels=None, repo):
        self.calls.append(("edit", repo, number))
        issue = self.issues[number]
        if title is not None:
            issue["title"] = title
        if body is not None:
            issue["body"] = body
        for label in add_labels or []:
            issue["labels"].append({"name": label})
        issue["labels"] = [x for x in issue["labels"] if x["name"] not in (remove_labels or [])]

    def comment_issue(self, number, body, *, repo):
        self.calls.append(("comment_issue", repo, number))
        self.issues[number]["comments"].append({"body": body})

    def edit_pr(self, number, *, title=None, body=None, repo):
        self.calls.append(("edit_pr", repo, number))
        pr = self.prs[number]
        if title is not None:
            pr["title"] = title
        if body is not None:
            pr["body"] = body

    def comment_pr(self, number, body, *, repo):
        self.calls.append(("comment_pr", repo, number))
        self.prs[number]["comments"].append({"body": body})

    def close_issue(self, number, *, repo):
        self.calls.append(("close", repo, number))
        self.issues[number]["state"] = "CLOSED"

    def reopen_issue(self, number, *, repo):
        self.calls.append(("reopen", repo, number))
        self.issues[number]["state"] = "OPEN"

    def create_label(self, name, *, color, description, repo):
        self.calls.append(("create_label", repo))
        self.labels.append({"name": name, "color": color, "description": description})

    def create_issue(self, title, body, *, repo):
        self.calls.append(("create_issue", repo))
        return {"number": 500}


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.worktree = base / "external"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.proposals = base / "proposals.json"
        self.fake = FakeGithub()

    def tearDown(self):
        self.tmp.cleanup()

    def remote(self, url, name="origin"):
        subprocess.run(["git", "-C", str(self.worktree), "remote", "add", name, url], check=True)

    def write(self, proposals):
        self.proposals.write_text(json.dumps(proposals))

    def run_batch(self, **kwargs):
        if kwargs.get("apply") and "expected_plan_hash" not in kwargs:
            preview = gi.execute(worktree=self.worktree, proposal_file=self.proposals, adapter=self.fake,
                                 provenance_root=self.records, explicit_repo=kwargs.get("explicit_repo"))
            kwargs["expected_plan_hash"] = preview["plan_hash"]
        return gi.execute(worktree=self.worktree, proposal_file=self.proposals, adapter=self.fake,
                          provenance_root=self.records, **kwargs)

    def test_publication_requires_policy_and_uses_reviewed_draft_path(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        body = Path(self.tmp.name) / "pr-body.md"
        body.write_text("Summary of reviewed work\n\nRefs #401\n")
        self.fake = FakeGithubApi()
        denied = gi.publish(worktree=self.worktree, base="main", head="pico/batch-1",
                            title="Reviewed batch", body_file=body, adapter=self.fake,
                            provenance_root=self.records)
        self.assertEqual(denied["blocker_code"], "PUBLICATION_NOT_AUTHORIZED")
        policy = (ROOT / "templates/autonomy.yaml").read_text().replace("push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        # The policy is on GitHub's base branch; the checkout has no copy.
        base_commit = self.fake.merge("Examplecorp/examplelab", policy)
        state = {"remote": "origin", "commit": "a" * 40, "remote_commit": None, "base_commit": base_commit,
                 "existing_pr": None}
        real_run = subprocess.run
        def git_run(argv, **kwargs):
            if len(argv) > 3 and argv[3] == "push":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_run(argv, **kwargs)
        with patch.object(gi, "_publication_state", return_value=state), \
             patch.object(gi, "_branch", side_effect=lambda _worktree, name, **_kwargs: name):
            preview = gi.publish(worktree=self.worktree, base="main", head="pico/batch-1",
                                 title="Reviewed batch", body_file=body, adapter=self.fake,
                                 provenance_root=self.records)
            self.assertEqual(preview["status"], "DRY_RUN")
            self.assertEqual(preview["plan"]["draft"], True)
            self.assertEqual(preview["plan"]["publication_permissions"], {"push_branch": True, "create_pr": True})
            self.assertEqual(preview["mutations_applied"], 0)
            with patch.object(gi.subprocess, "run", side_effect=git_run), \
                 patch.object(self.fake, "create_pr", return_value={"number": 25, "url": "https://github.com/Examplecorp/examplelab/pull/25"}, create=True) as create:
                applied = gi.publish(worktree=self.worktree, base="main", head="pico/batch-1",
                                     title="Reviewed batch", body_file=body, adapter=self.fake,
                                     provenance_root=self.records, apply=True,
                                     expected_plan_hash=preview["plan_hash"])
        self.assertEqual(applied["status"], "COMPLETED")
        self.assertEqual(applied["pr"], 25)
        self.assertEqual(applied["mutations_applied"], 2)
        self.assertEqual(create.call_args.kwargs["draft"], True)
        body.write_text("Changed after review\n\nRefs #401\n")
        with patch.object(gi, "_publication_state", return_value=state), \
             patch.object(gi, "_branch", side_effect=lambda _worktree, name, **_kwargs: name), \
             patch.object(self.fake, "create_pr", create=True) as must_not_create:
            stale = gi.publish(worktree=self.worktree, base="main", head="pico/batch-1",
                               title="Reviewed batch", body_file=body, adapter=self.fake,
                               provenance_root=self.records, apply=True,
                               expected_plan_hash=preview["plan_hash"])
        self.assertEqual(stale["blocker_code"], "GITHUB_STATE_CONFLICT")
        self.assertEqual(stale["mutations_applied"], 0)
        must_not_create.assert_not_called()

    def test_https_ssh_and_explicit_resolution(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        self.assertEqual(gi.resolve_target(self.worktree), "Examplecorp/examplelab")
        self.assertEqual(gi.resolve_target(self.worktree, "Examplecorp/examplelab"), "Examplecorp/examplelab")
        subprocess.run(["git", "-C", str(self.worktree), "remote", "set-url", "origin", "git@github.com:Examplecorp/examplelab.git"], check=True)
        self.assertEqual(gi.resolve_target(self.worktree), "Examplecorp/examplelab")

    def test_missing_ambiguous_and_conflicting_remotes_fail_closed(self):
        with self.assertRaises(gi.IntegrationError) as error:
            gi.resolve_target(self.worktree)
        self.assertEqual(error.exception.code, "TARGET_REPOSITORY_UNRESOLVED")
        with self.assertRaises(gi.IntegrationError) as error:
            gi.resolve_target(self.worktree, "Examplecorp/examplelab")
        self.assertEqual(error.exception.code, "TARGET_REPOSITORY_CONFLICT")
        self.remote("https://github.com/Examplecorp/examplelab.git")
        with self.assertRaises(gi.IntegrationError) as error:
            gi.resolve_target(self.worktree, "dhuzard/pi-coding-orchestrator")
        self.assertEqual(error.exception.code, "TARGET_REPOSITORY_CONFLICT")
        self.remote("git@github.com:another/repo.git", "upstream")
        with self.assertRaises(gi.IntegrationError) as error:
            gi.resolve_target(self.worktree)
        self.assertEqual(error.exception.code, "TARGET_REPOSITORY_AMBIGUOUS")

    def test_same_identity_across_https_and_ssh_remotes_resolves_without_explicit(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        self.remote("git@github.com:examplecorp/ExamPleLab.git", "mirror")
        audit = {}
        self.assertEqual(gi.resolve_target(self.worktree, audit=audit), "Examplecorp/examplelab")
        self.assertEqual(audit["discovered_repositories"], ["Examplecorp/examplelab"])
        self.assertEqual(audit["repository_selection"], {
            "method": "inferred", "requested": None, "resolved": "Examplecorp/examplelab"})

    def test_multiple_distinct_remotes_explicitly_selects_either_matching_repo(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        self.remote("git@github.com:Examplecorp/examplelab-oss.git", "oss")
        with self.assertRaises(gi.IntegrationError) as error:
            gi.resolve_target(self.worktree)
        self.assertEqual(error.exception.code, "TARGET_REPOSITORY_AMBIGUOUS")
        for requested in ("Examplecorp/examplelab", "Examplecorp/examplelab-oss"):
            audit = {}
            self.assertEqual(gi.resolve_target(self.worktree, requested, audit=audit), requested)
            self.assertEqual(audit["discovered_repositories"],
                             ["Examplecorp/examplelab", "Examplecorp/examplelab-oss"])
            self.assertEqual(audit["repository_selection"], {
                "method": "explicit", "requested": requested, "resolved": requested})
        with self.assertRaises(gi.IntegrationError) as error:
            gi.resolve_target(self.worktree, "another/sensitive-repo")
        self.assertEqual(error.exception.code, "TARGET_REPOSITORY_CONFLICT")

    def test_malformed_explicit_repository_is_rejected(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        for malformed in ("examplelab", "other/repo/extra", "--repo=other/repo", "other/repo;false"):
            with self.assertRaises(gi.IntegrationError) as error:
                gi.resolve_target(self.worktree, malformed)
            self.assertEqual(error.exception.code, "TARGET_REPOSITORY_UNRESOLVED")

    def test_real_examplelab_origin_and_oss_fixture_dry_run(self):
        fixture = json.loads((ROOT / "tests/fixtures/examplelab-repository-resolution.json").read_text())
        for name, url in fixture["remotes"].items():
            self.remote(url, name)
        self.write([{"operation": "add_label", "repository": fixture["resolved_repository"],
                     "issue": 401, "label": "semantic-contract", "rationale": "organize backlog"}])
        result = self.run_batch(explicit_repo=fixture["explicit"])
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["target_repository"], fixture["resolved_repository"])
        self.assertEqual([step["repository"] for step in result["plan"]], [fixture["resolved_repository"]])
        record = provenance.get_run(self.records, result["run_id"])["github_integration"]
        self.assertEqual(record["discovered_repositories"],
                         ["Examplecorp/examplelab", "Examplecorp/examplelab-oss"])
        self.assertEqual(record["repository_selection"], {
            "method": "explicit", "requested": fixture["explicit"],
            "resolved": fixture["resolved_repository"]})
        self.assertIsNone(record["blocker_code"])
        self.assertFalse(any(call[0] == "edit" for call in self.fake.calls))

    def test_worker_proposal_cannot_select_other_discovered_remote(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        self.remote("git@github.com:Examplecorp/examplelab-oss.git", "oss")
        self.write([{"operation": "add_label", "repository": "Examplecorp/examplelab-oss",
                     "issue": 401, "label": "wrong", "rationale": "redirect"}])
        result = self.run_batch(explicit_repo="Examplecorp/examplelab", apply=True)
        self.assertEqual(result["blocker_code"], "TARGET_REPOSITORY_CONFLICT")
        self.assertEqual(result["mutations_applied"], 0)
        self.assertFalse(any(call[0] == "edit" for call in self.fake.calls))

    def test_explicit_target_absent_from_all_remotes_blocks_and_records_discovery(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        self.remote("git@github.com:Examplecorp/examplelab-oss.git", "oss")
        self.write([{"operation": "add_label", "repository": "another/sensitive-repo",
                     "issue": 401, "label": "wrong", "rationale": "redirect"}])
        result = self.run_batch(explicit_repo="another/sensitive-repo", apply=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["blocker_code"], "TARGET_REPOSITORY_CONFLICT")
        record = provenance.get_run(self.records, result["run_id"])["github_integration"]
        self.assertEqual(record["discovered_repositories"],
                         ["Examplecorp/examplelab", "Examplecorp/examplelab-oss"])
        self.assertEqual(record["repository_selection"], {
            "method": "explicit", "requested": "another/sensitive-repo", "resolved": None})
        self.assertFalse(any(call[0] == "edit" for call in self.fake.calls))

    def test_unresolved_target_still_records_blocked_run(self):
        self.write([{"operation": "close_issue", "repository": "Examplecorp/examplelab", "issue": 401, "rationale": "done"}])
        result = self.run_batch(apply=True)
        self.assertEqual(result["blocker_code"], "TARGET_REPOSITORY_UNRESOLVED")
        record = provenance.get_run(self.records, result["run_id"])
        self.assertEqual(record["outcome"], "blocked")
        self.assertEqual(record["github_integration"]["mutations_applied"], 0)

    def test_external_backlog_dry_run_apply_and_provenance(self):
        self.remote("git@github.com:Examplecorp/examplelab.git")
        self.write([
            {"operation": "create_label", "repository": "Examplecorp/examplelab", "label": "semantic-contract", "rationale": "organize backlog"},
            {"operation": "add_label", "repository": "Examplecorp/examplelab", "issue": 401, "label": "semantic-contract", "rationale": "group work"},
            {"operation": "update_issue", "repository": "Examplecorp/examplelab", "issue": 464, "title": "Revised", "rationale": "clarify"},
        ])
        dry = self.run_batch(phase="Phase A")
        self.assertEqual(dry["status"], "DRY_RUN")
        self.assertEqual(dry["target_repository"], "Examplecorp/examplelab")
        self.assertEqual(len(dry["plan"]), 3)
        self.assertEqual(provenance.get_run(self.records, dry["run_id"])["outcome"], "dry_run")
        applied = self.run_batch(apply=True)
        self.assertEqual(applied["status"], "COMPLETED")
        self.assertEqual(applied["mutations_applied"], 3)
        self.assertEqual(self.fake.issues[464]["title"], "Revised")
        self.assertTrue(all("dhuzard/pi-coding-orchestrator" not in str(x) for x in self.fake.calls))
        again = self.run_batch(apply=True)
        self.assertEqual(again["mutations_applied"], 0)
        self.assertFalse((self.worktree / "tools/backlog.py").exists())
        self.assertFalse((self.worktree / "backlog/backlog.yaml").exists())

    def test_issue_and_pr_communication_is_dry_run_guarded_and_idempotent(self):
        self.remote("git@github.com:Examplecorp/examplelab.git")
        self.write([
            {"operation": "comment_issue", "repository": "Examplecorp/examplelab", "issue": 401,
             "body": "Validation complete; CI is green.", "rationale": "report validated status"},
            {"operation": "update_pr", "repository": "Examplecorp/examplelab", "pr": 24,
             "title": "Professional PR title", "body": "## Summary\nValidated change.", "rationale": "improve PR metadata"},
            {"operation": "comment_pr", "repository": "Examplecorp/examplelab", "pr": 24,
             "body": "Reviewed: no blocking findings.", "rationale": "record review outcome"},
        ])
        dry = self.run_batch()
        self.assertEqual(dry["status"], "DRY_RUN")
        self.assertEqual(dry["mutations_applied"], 0)
        self.assertEqual([step["operation"] for step in dry["plan"]],
                         ["comment_issue", "update_pr", "comment_pr"])

        applied = self.run_batch(apply=True)
        self.assertEqual(applied["status"], "COMPLETED")
        self.assertEqual(applied["mutations_applied"], 3)
        self.assertEqual(self.fake.prs[24]["title"], "Professional PR title")
        self.assertEqual(self.fake.issues[401]["comments"][0]["body"], "Validation complete; CI is green.")
        self.assertEqual(self.fake.prs[24]["comments"][0]["body"], "Reviewed: no blocking findings.")

        again = self.run_batch(apply=True)
        self.assertEqual(again["status"], "COMPLETED")
        self.assertEqual(again["mutations_applied"], 0)
        self.assertEqual(len(self.fake.issues[401]["comments"]), 1)
        self.assertEqual(len(self.fake.prs[24]["comments"]), 1)

        record = provenance.get_run(self.records, applied["run_id"])["github_integration"]
        self.assertEqual(record["audit"][1]["pr"], 24)
        self.assertEqual(record["audit"][2]["pr"], 24)

    def test_comment_and_pr_validation_fails_closed(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        invalid = [
            {"operation": "comment_issue", "repository": "Examplecorp/examplelab", "issue": 401,
             "body": "", "rationale": "empty is not useful"},
            {"operation": "comment_pr", "repository": "Examplecorp/examplelab", "pr": True,
             "body": "review", "rationale": "invalid id"},
            {"operation": "update_pr", "repository": "Examplecorp/examplelab", "pr": 24,
             "rationale": "missing update"},
        ]
        for proposal in invalid:
            self.write([proposal])
            result = self.run_batch(apply=True)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["blocker_code"], "GITHUB_MUTATION_VALIDATION_FAILED")

    def test_read_backlog_is_target_scoped(self):
        self.remote("git@github.com:Examplecorp/examplelab.git")
        result = gi.read_backlog(worktree=self.worktree, adapter=self.fake)
        self.assertEqual({x["number"] for x in result["issues"]}, {401, 464, 462, 457})
        self.assertEqual(result["repository"], "Examplecorp/examplelab")
        self.assertEqual(gi.read_backlog(worktree=self.worktree, issue=401, adapter=self.fake)["issue"]["number"], 401)
        traced = gi.read_with_provenance(worktree=self.worktree, adapter=self.fake, provenance_root=self.records)
        self.assertEqual(traced["status"], "COMPLETED")
        self.assertEqual(provenance.get_run(self.records, traced["run_id"])["outcome"], "completed")

    def test_blocked_read_is_traced(self):
        traced = gi.read_with_provenance(worktree=self.worktree, adapter=self.fake, provenance_root=self.records)
        self.assertEqual(traced["blocker_code"], "TARGET_REPOSITORY_UNRESOLVED")
        self.assertEqual(provenance.get_run(self.records, traced["run_id"])["outcome"], "blocked")

    def test_worker_repository_escape_and_malformed_operations(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        base = {"operation": "add_label", "repository": "Examplecorp/examplelab", "issue": 401,
                "label": "safe", "rationale": "group"}
        for proposal, expected in [({**base, "repository": "attacker/repo"}, "TARGET_REPOSITORY_CONFLICT"),
                                   ({**base, "operation": "delete_repo"}, "GITHUB_MUTATION_VALIDATION_FAILED"),
                                   ({**base, "command": "gh issue close 401"}, "GITHUB_MUTATION_VALIDATION_FAILED"),
                                   ({**base, "issue": True}, "GITHUB_MUTATION_VALIDATION_FAILED")]:
            self.write([proposal])
            result = self.run_batch(apply=True)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["blocker_code"], expected)
            self.assertEqual(provenance.get_run(self.records, result["run_id"])["outcome"], "blocked")
        self.assertFalse(any(c[0] == "edit" for c in self.fake.calls))

    def test_destructive_requires_parent_flag(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "close_issue", "repository": "Examplecorp/examplelab", "issue": 401, "rationale": "complete"}])
        blocked = self.run_batch(apply=True)
        self.assertEqual(blocked["blocker_code"], "GITHUB_MUTATION_NOT_AUTHORIZED")
        self.assertEqual(self.fake.issues[401]["state"], "OPEN")
        self.assertEqual(self.run_batch(apply=True, allow_destructive=True)["status"], "COMPLETED")

    MUTATING_CALLS = {"edit", "comment_issue", "edit_pr", "comment_pr", "close", "reopen", "create_label",
                      "create_issue"}

    def mutated(self):
        return [call for call in self.fake.calls if call[0] in self.MUTATING_CALLS]

    def test_each_write_needs_its_permission_in_the_default_branch_policy(self):
        # #58: the permission is read from .pi/autonomy.yaml on the default branch on GitHub.
        self.remote("https://github.com/Examplecorp/examplelab")
        repo = "Examplecorp/examplelab"
        cases = [
            ("create_github_issues", {"operation": "create_issue", "title": "New", "body": "b"}),
            ("create_github_issues", {"operation": "update_issue", "issue": 401, "title": "Renamed"}),
            ("create_github_issues", {"operation": "close_issue", "issue": 401}),
            ("create_github_issues", {"operation": "create_label", "label": "triage"}),
            ("create_github_issues", {"operation": "add_label", "issue": 401, "label": "triage"}),
            ("comment_on_issue", {"operation": "comment_issue", "issue": 401, "body": "Done."}),
            ("comment_on_pr", {"operation": "comment_pr", "pr": 24, "body": "Reviewed."}),
            ("create_pr", {"operation": "update_pr", "pr": 24, "title": "Better title"}),
        ]
        for permission, proposal in cases:
            with self.subTest(operation=proposal["operation"]):
                self.fake = FakeGithub()
                self.fake.labels = [{"name": "triage"}] if proposal["operation"] == "add_label" else []
                self.write([{**proposal, "repository": repo, "rationale": "test"}])
                self.fake.withheld = {permission}
                dry = self.run_batch()
                self.assertEqual((dry["status"], dry["blocker_code"]), ("BLOCKED", "GITHUB_WRITE_NOT_AUTHORIZED"))
                reason = provenance.get_run(self.records, dry["run_id"])["github_integration"]["reason"]
                self.assertTrue(reason.startswith(f"{permission} must be granted"), reason)
                applied = self.run_batch(apply=True, allow_destructive=True, expected_plan_hash="0" * 64)
                self.assertEqual(applied["blocker_code"], "GITHUB_WRITE_NOT_AUTHORIZED")
                self.assertEqual(self.mutated(), [])
                # Withholding every other permission changes nothing: only the one the write needs counts.
                self.fake.withheld = {"create_github_issues", "comment_on_issue", "comment_on_pr", "create_pr"} - {permission}
                done = self.run_batch(apply=True, allow_destructive=True)
                self.assertEqual((done["status"], done["mutations_applied"]), ("COMPLETED", 1))
                self.assertEqual(provenance.get_run(self.records, done["run_id"])["github_integration"]["authorization"],
                                 {"source": "policy", "permissions": [permission], "policy_commit": "d" * 40})

    def test_missing_default_branch_policy_refuses_writes(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "comment_issue", "repository": "Examplecorp/examplelab", "issue": 401,
                     "body": "Done.", "rationale": "report"}])
        self.fake.policy_present = False
        self.assertEqual(self.run_batch()["blocker_code"], "GITHUB_WRITE_NOT_AUTHORIZED")
        self.assertEqual(self.mutated(), [])

    def test_checkout_policy_never_authorizes_writes(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        (self.worktree / ".pi").mkdir()
        (self.worktree / ".pi/autonomy.yaml").write_text((ROOT / "templates/autonomy.yaml").read_text())  # grants comments
        self.write([{"operation": "comment_issue", "repository": "Examplecorp/examplelab", "issue": 401,
                     "body": "Done.", "rationale": "report"}])
        self.fake.withheld = {"comment_on_issue"}
        self.assertEqual(self.run_batch()["blocker_code"], "GITHUB_WRITE_NOT_AUTHORIZED")

    def test_noop_batch_needs_no_permission(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "update_issue", "repository": "Examplecorp/examplelab", "issue": 401,
                     "title": "Issue 401", "rationale": "already so"}])
        self.fake.withheld = {"create_github_issues", "comment_on_issue", "comment_on_pr", "create_pr"}
        result = self.run_batch(apply=True)
        self.assertEqual((result["status"], result["mutations_applied"]), ("COMPLETED", 0))
        self.assertNotIn("authorization", provenance.get_run(self.records, result["run_id"])["github_integration"])

    def test_partial_mutation_failure_is_audited(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "close_issue", "repository": "Examplecorp/examplelab", "issue": 401, "rationale": "done"},
                    {"operation": "close_issue", "repository": "Examplecorp/examplelab", "issue": 464, "rationale": "done"}])
        original = self.fake.close_issue
        def fail_second(number, *, repo):
            if number == 464:
                raise gh.GhCommandError(["gh", "issue", "close"], 1, "failure")
            original(number, repo=repo)
        self.fake.close_issue = fail_second
        result = self.run_batch(apply=True, allow_destructive=True)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["blocker_code"], "GITHUB_MUTATION_FAILED")
        record = provenance.get_run(self.records, result["run_id"])
        self.assertEqual(record["github_integration"]["mutations_applied"], 1)
        self.assertEqual(len(record["github_integration"]["audit"]), 1)

    def test_adapter_rejects_unallowlisted_commands(self):
        with self.assertRaises(gh.GithubSyncError):
            gh._validate_allowlisted(["gh", "api", "repos/attacker/repo"])

    def test_stale_state_blocks_with_provenance(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "update_issue", "repository": "Examplecorp/examplelab", "issue": 401, "title": "new", "rationale": "clarify"}])
        self.fake.change_before_second_read = True
        result = self.run_batch(apply=True)
        self.assertEqual(result["blocker_code"], "GITHUB_STATE_CONFLICT")
        self.assertEqual(result["mutations_applied"], 0)
        self.assertEqual(provenance.get_run(self.records, result["run_id"])["github_integration"]["mutations_applied"], 0)

    def test_changed_plan_hash_blocks_before_mutation(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "update_issue", "repository": "Examplecorp/examplelab", "issue": 401,
                     "title": "new", "rationale": "clarify"}])
        preview = self.run_batch()
        self.fake.issues[401]["title"] = "changed elsewhere"
        result = self.run_batch(apply=True, expected_plan_hash=preview["plan_hash"])
        self.assertEqual(result["blocker_code"], "GITHUB_STATE_CONFLICT")
        self.assertEqual(result["mutations_applied"], 0)

    def test_read_failure_is_structured_and_traced(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        self.write([{"operation": "update_issue", "repository": "Examplecorp/examplelab", "issue": 401,
                     "title": "new", "rationale": "clarify"}])
        self.fake.view_issue = lambda number, *, repo: (_ for _ in ()).throw(gh.GhCommandError(["gh", "issue", "view"], 1, "failure"))
        result = self.run_batch(apply=True)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["blocker_code"], "GITHUB_READ_FAILED")
        self.assertEqual(provenance.get_run(self.records, result["run_id"])["outcome"], "failed")

    def test_shell_arguments_and_secret_provenance(self):
        self.remote("https://github.com/Examplecorp/examplelab")
        payload = "$(touch /tmp/pi-should-not-exist); `false`"
        self.write([{"operation": "update_issue", "repository": "Examplecorp/examplelab", "issue": 401,
                     "body": payload, "rationale": "clarify"}])
        result = self.run_batch(apply=True)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.fake.issues[401]["body"], payload)
        self.assertNotIn(payload, json.dumps(provenance.get_run(self.records, result["run_id"])))
        with patch.object(gh.shutil, "which", return_value="/usr/bin/gh"), patch.object(gh.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, "", "")
            gh.edit_issue(401, body=payload, repo="Examplecorp/examplelab")
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertIn(payload, run.call_args.args[0])
            self.assertEqual(run.call_args.args[0][-2:], ["--repo", "Examplecorp/examplelab"])
        token = "ghp_" + "x" * 30
        self.write([{"operation": "add_label", "repository": "Examplecorp/examplelab", "issue": 462,
                     "label": token, "rationale": "group"}])
        result = self.run_batch(apply=True)
        self.assertNotIn(token, json.dumps(provenance.get_run(self.records, result["run_id"])))


class FakeGithubApi:
    """GitHub as the API adapter reports it: branch heads, and the policy file at each commit."""

    def __init__(self):
        self.heads = {}
        self.policies = {}
        self.default = {}  # repo -> default branch name (main when unset)
        self.reads = []
        self._commits = 0

    def list_prs(self, *, base, head, repo):
        return []

    def default_branch(self, *, repo):
        self.reads.append(("default_branch", repo))
        return self.default.get(repo.casefold(), "main")

    def branch_head(self, branch, *, repo):
        self.reads.append(("branch", repo, branch))
        return self.heads.get((repo.casefold(), branch))

    def policy_file_at(self, commit, *, repo):
        self.reads.append(("policy", repo, commit))
        return self.policies.get((repo.casefold(), commit))

    def merge(self, repo, policy_text=None, *, entry=None, branch="main"):
        """Advance `branch` on GitHub to a new commit carrying `policy_text` (or a raw contents entry)."""
        self._commits += 1
        sha = hashlib.sha1(f"{repo}:{branch}:{self._commits}".encode()).hexdigest()
        self.heads[(repo.casefold(), branch)] = sha
        if entry is None and policy_text is not None:
            entry = {"type": "file", "path": ".pi/autonomy.yaml",
                     "sha": hashlib.sha1(policy_text.encode()).hexdigest(), "text": policy_text}
        if entry is not None:
            self.policies[(repo.casefold(), sha)] = entry
        return sha


class IssueClosingGateTest(unittest.TestCase):
    """The publication gate's issue-closing rules; each bypass is its own test or subTest.

    The base branch and its policy come from a fake GitHub API; the local
    checkout (refs, config, working tree) is what a worker could tamper with.
    """

    REPO = "Examplecorp/examplelab"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.worktree = self.base / "external"
        self.bare = self.base / "remote.git"
        self.records = self.base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        real_run = subprocess.run
        self.git_calls = []

        def redirect(argv, *args, **kwargs):
            # The pushed head is inspected in a local bare repository standing in for the remote.
            if isinstance(argv, list) and argv[:1] == ["git"]:
                self.git_calls.append(list(argv))
                if len(argv) > 3 and argv[3] in {"ls-remote", "push"}:
                    argv = [str(self.bare) if a == "origin" else a for a in argv]
            return real_run(argv, *args, **kwargs)
        patcher = patch.object(gi.subprocess, "run", side_effect=redirect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.template = (ROOT / "templates/autonomy.yaml").read_text().replace(
            "push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        self.closes_policy = self.template.replace("issue_reference: refs", "issue_reference: closes")
        self.git("init", "-q", "--bare", str(self.bare), cwd=False)
        self.git("init", "-q", "-b", "main", str(self.worktree), cwd=False)
        self.git("remote", "add", "origin", f"https://github.com/{self.REPO}.git")
        (self.worktree / ".pi").mkdir()
        self.policy = self.worktree / ".pi/autonomy.yaml"
        self.commit(self.template, "refs policy")
        self.git("branch", "pico/batch-1")
        self.api = FakeGithubApi()
        self.api.merge(self.REPO, self.template)
        self.body = self.base / "pr-body.md"

    def git(self, *args, cwd=True):
        prefix = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        if cwd:
            prefix += ["-C", str(self.worktree)]
        return subprocess.run(prefix + list(args), check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, text, message):
        self.policy.write_text(text)
        self.git("add", ".pi/autonomy.yaml")
        self.git("commit", "-q", "-m", message)

    def merge_closes_policy(self):
        """A maintainer merges issue_reference: closes on GitHub and the checkout pulls it."""
        self.commit(self.closes_policy, "close issues on merge")
        return self.api.merge(self.REPO, self.closes_policy)

    def dry_run(self, text, **kwargs):
        self.body.write_text(text, encoding="utf-8")
        return gi.publish(worktree=self.worktree, base=kwargs.pop("base", "main"), head="pico/batch-1",
                          title="Reviewed batch", body_file=self.body, adapter=self.api,
                          provenance_root=self.records, **kwargs)

    def assertRefused(self, text, code="PUBLICATION_BODY_INVALID", **kwargs):
        result = self.dry_run(text, **kwargs)
        self.assertEqual((result["status"], result["blocker_code"]), ("BLOCKED", code), text)
        return result

    def test_default_policy_accepts_refs_and_records_the_github_base(self):
        refs = self.dry_run("Summary\n\nRefs #401\n")
        self.assertEqual((refs["status"], refs["plan"]["issue_reference"]), ("DRY_RUN", "refs"))
        self.assertEqual(refs["plan"]["base_commit"], self.api.heads[(self.REPO.casefold(), "main")])
        self.assertEqual(refs["plan"]["policy_blob"],
                         self.api.policies[(self.REPO.casefold(), refs["plan"]["base_commit"])]["sha"])

    def test_default_policy_refuses_every_closing_form(self):
        for text in ("Summary\n\nCloses #401\n", "Refs #401\nAlso fixes: Examplecorp/examplelab#464\n",
                     "Resolved #401\n", "Refs #401\nCloses https://github.com/o/r/issues/2\n",
                     "Refs #401\nFixes:#2\n", "Refs #401\nfixes #2\n", "Refs #401\n`Fixes #2`\n",
                     "Refs #401\nCLOSE #2\n"):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_non_closing_text_is_not_a_false_positive(self):
        self.assertEqual(self.dry_run("Refs #401\nDisclosed #464, prefix#3, see #5\n")["status"], "DRY_RUN")

    def test_caller_opt_in_closes_only_its_own_issue(self):
        opted = self.dry_run("Summary\n\nCloses #401\n", closes_issue=401)
        self.assertEqual((opted["status"], opted["plan"]["issue_reference"], opted["plan"]["closes_issues"]),
                         ("DRY_RUN", "closes", [401]))
        for text in ("Summary only\n", "Closes #402\n", "Closes #401\nFixes #402\n",
                     "Closes #401\nFixes other/repo#401\n", "Closes #401\nCloses https://github.com/o/r/issues/401\n"):
            with self.subTest(text=text):
                self.assertRefused(text, closes_issue=401)

    def test_github_closes_policy_accepts_declared_closes_lines(self):
        refs = self.dry_run("Refs #401\n")
        head = self.merge_closes_policy()
        closes = self.dry_run("Summary\n\nCloses #401\n")
        self.assertEqual((closes["status"], closes["plan"]["issue_reference"]), ("DRY_RUN", "closes"))
        self.assertEqual(closes["plan"]["base_commit"], head)
        self.assertEqual(closes["plan"]["policy_blob"], self.api.policies[(self.REPO.casefold(), head)]["sha"])
        self.assertNotEqual(closes["plan_hash"], refs["plan_hash"])
        batch = self.dry_run("Closes #401\nCloses #402\n")
        self.assertEqual((batch["status"], batch["plan"]["closes_issues"]), ("DRY_RUN", [401, 402]))
        same = self.dry_run("Closes #401\nFixes https://github.com/Examplecorp/examplelab/issues/401\n")
        self.assertEqual(same["status"], "DRY_RUN")
        self.assertEqual(self.dry_run("Summary\n\nRefs #401\n")["plan"]["issue_reference"], "refs")

    def test_github_closes_policy_refuses_undeclared_and_cross_repository_targets(self):
        self.merge_closes_policy()
        for text in ("Closes #401\nFixes other/repo#99\nResolves #500\n", "Closes #401\nResolves #500\n",
                     "Closes #401\nFixes other/repo#401\n", "Closes #401\nCloses https://github.com/o/r/issues/401\n"):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_uncommitted_edit_cannot_grant_closing(self):
        self.policy.write_text(self.closes_policy)
        self.assertRefused("Closes #401\n")

    def test_local_commit_not_on_github_cannot_grant_closing(self):
        for branch in ("pico/batch-1", "main"):
            with self.subTest(branch=branch):
                self.git("checkout", "-q", branch)
                self.commit(self.closes_policy, f"grant closing on {branch}")
                self.assertRefused("Closes #401\n")

    def test_forged_local_base_ref_cannot_grant_closing(self):
        # From its pico/ worktree, a worker builds a commit with issue_reference: closes and points local
        # refs at it; its checkout stays clean.
        self.git("checkout", "-q", "pico/batch-1")
        self.policy.write_text(self.closes_policy)
        blob = self.git("hash-object", "-w", ".pi/autonomy.yaml")
        self.git("checkout", "-q", "--", ".pi/autonomy.yaml")
        pi_tree = subprocess.run(["git", "-C", str(self.worktree), "mktree"], input=f"100644 blob {blob}\tautonomy.yaml\n",
                                 check=True, capture_output=True, text=True).stdout.strip()
        root = subprocess.run(["git", "-C", str(self.worktree), "mktree"], input=f"040000 tree {pi_tree}\t.pi\n",
                              check=True, capture_output=True, text=True).stdout.strip()
        forged = self.git("commit-tree", root, "-p", "main", "-m", "forged")
        for ref in ("refs/heads/main", "refs/remotes/origin/main"):
            with self.subTest(ref=ref):
                self.git("update-ref", ref, forged)
                self.assertEqual(self.git("status", "--porcelain", "--", ".pi/autonomy.yaml"), "")
                self.assertRefused("Closes #1\nCloses #77\n")

    def test_hostile_git_config_cannot_change_the_policy_read(self):
        # A worker points the shared git config at a fake "remote" whose main carries a closes policy.
        marker = self.base / "hostile-ran"
        hostile = self.base / "hostile.sh"
        hostile.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        hostile.chmod(0o755)
        included = self.base / "included.gitconfig"
        included.write_text(f"[core]\n\tsshCommand = {hostile}\n")
        self.git("remote", "set-url", "origin", f"git@github.com:{self.REPO}.git")
        for key, value in (("core.sshCommand", str(hostile)), ("remote.origin.uploadpack", str(hostile)),
                           ("include.path", str(included))):
            self.git("config", key, value)
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "main"))
        self.policy.write_text(self.closes_policy)
        self.git("commit", "-q", "-am", "closes, locally only")

        refused = self.assertRefused("Closes #1\nCloses #77\n")
        self.assertIsNone(refused["plan"])
        github_main = self.api.heads[(self.REPO.casefold(), "main")]
        self.assertIn(("branch", self.REPO, "main"), self.api.reads)
        self.assertIn(("policy", self.REPO, github_main), self.api.reads)
        # No local git invocation reads the base branch or a tree: the policy comes from the GitHub API.
        for argv in self.git_calls:
            self.assertFalse({"ls-tree", "cat-file", "show"} & set(argv), argv)
            self.assertFalse(any("refs/heads/main" in a for a in argv), argv)
        # Once GitHub's own main says closes, the same hostile config cannot add more issues.
        self.api.merge(self.REPO, self.closes_policy)
        self.git("checkout", "-q", "--", ".pi/autonomy.yaml")
        self.assertEqual(self.dry_run("Closes #1\n")["plan"]["closes_issues"], [1])
        self.assertRefused("Closes #1\nFixes #77\n")
        self.assertFalse(marker.exists())

    def test_dirty_policy_checkout_is_ignored_not_refused(self):
        # Authority is remote, so a Garden toggle or init_project's uncommitted policy is harmless.
        self.merge_closes_policy()
        self.policy.write_text(self.closes_policy.replace("create_pr: true", "create_pr: false"))
        result = self.dry_run("Closes #401\n")
        self.assertEqual((result["status"], result["policy_drift"]), ("DRY_RUN", ["create_pr"]))
        self.assertTrue(result["plan"]["publication_permissions"]["create_pr"])

    def test_symlinked_pi_is_unsafe(self):
        self.merge_closes_policy()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "autonomy.yaml").write_text(self.closes_policy)
        shutil.rmtree(self.worktree / ".pi")
        (self.worktree / ".pi").symlink_to(outside, target_is_directory=True)
        self.assertRefused("Closes #401\n", "AUTONOMY_POLICY_PATH_UNSAFE")

    def test_symlinked_policy_on_github_is_unsafe(self):
        self.commit(self.closes_policy, "close issues on merge")
        sha = "b" * 40
        for entry in ({"type": "symlink", "path": ".pi/autonomy.yaml", "sha": sha, "text": None},
                      {"type": "file", "path": "elsewhere.yaml", "sha": sha, "text": self.closes_policy}):
            with self.subTest(entry=entry["type"] + ":" + entry["path"]):
                self.api.merge(self.REPO, entry=entry)
                self.assertRefused("Closes #401\n", "AUTONOMY_POLICY_PATH_UNSAFE")

    def test_unreadable_or_invalid_policy_on_github_fails_closed(self):
        sha = "c" * 40
        for name, entry in (
                ("invalid value", {"type": "file", "path": ".pi/autonomy.yaml", "sha": sha,
                                   "text": self.template.replace("issue_reference: refs", "issue_reference: fixes")}),
                ("directory", {"type": "dir", "path": ".pi/autonomy.yaml", "sha": None, "text": None}),
                ("not decodable", {"type": "file", "path": ".pi/autonomy.yaml", "sha": sha, "text": None})):
            with self.subTest(name):
                self.api.merge(self.REPO, entry=entry)
                self.assertRefused("Closes #401\n", "AUTONOMY_POLICY_INVALID")

    def test_github_without_the_base_branch_or_policy_defaults_to_refs(self):
        # Publication itself is refused then (test_missing_base_policy_or_branch_is_not_authorized).
        with self.subTest("no policy at the base commit"):
            commit = self.api.merge(self.REPO)
            self.assertEqual(gi.remote_issue_reference(self.REPO, commit, self.api), ("refs", None))
        with self.subTest("no base branch on GitHub"):
            self.assertEqual(gi.remote_issue_reference(self.REPO, None, self.api), ("refs", None))

    def test_base_branch_name_outside_the_api_charset_is_refused(self):
        self.git("branch", "rel@1")
        self.assertRefused("Refs #401\n", "PUBLICATION_REF_INVALID", base="rel@1")

    def test_base_moving_after_the_dry_run_conflicts_even_for_refs(self):
        preview = self.dry_run("Refs #401\n")
        self.assertEqual(preview["status"], "DRY_RUN")
        self.api.merge(self.REPO, self.template)
        stale = self.dry_run("Refs #401\n", apply=True, expected_plan_hash=preview["plan_hash"])
        self.assertEqual((stale["blocker_code"], stale["mutations_applied"]), ("GITHUB_STATE_CONFLICT", 0))

    # --- publication permissions (push_branch, create_pr) come from the base policy on GitHub only ---

    NOT_AUTHORIZED = "PUBLICATION_NOT_AUTHORIZED"

    def withhold_on_github(self):
        """The base branch on GitHub withholds push_branch and create_pr (the template default)."""
        self.withheld = (ROOT / "templates/autonomy.yaml").read_text()
        self.assertIn("push_branch: false", self.withheld)
        return self.api.merge(self.REPO, self.withheld)

    def test_base_policy_granting_authorizes_and_binds_blob_commit_and_permissions(self):
        plan = self.dry_run("Refs #401\n")["plan"]
        head = self.api.heads[(self.REPO.casefold(), "main")]
        self.assertEqual((plan["authorization"], plan["base_commit"], plan["publication_permissions"]),
                         ("policy", head, {"push_branch": True, "create_pr": True}))
        self.assertEqual(plan["policy_blob"], self.api.policies[(self.REPO.casefold(), head)]["sha"])
        self.assertEqual(self.api.reads.count(("policy", self.REPO, head)), 1)  # one read serves every decision
        self.assertEqual(self.api.reads.count(("branch", self.REPO, "main")), 1)
        self.assertEqual((plan["policy_branch"], plan["policy_commit"]), ("main", head))

    def test_uncommitted_working_tree_edit_cannot_grant_publication(self):
        self.withhold_on_github()
        self.commit(self.withheld, "withheld, as on GitHub")
        self.policy.write_text(self.template)  # a worker edits its tracked copy
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)
        self.policy.write_text(self.withheld)
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)
        self.git("rm", "-q", "-f", ".pi/autonomy.yaml")  # a deleted copy changes nothing either
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)

    def test_untracked_working_tree_policy_cannot_grant_publication(self):
        self.withhold_on_github()
        self.git("rm", "-q", "-f", ".pi/autonomy.yaml")
        self.git("commit", "-q", "-m", "drop the policy")
        (self.worktree / ".pi").mkdir(exist_ok=True)
        self.policy.write_text(self.template)
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)

    def test_policy_committed_locally_but_not_on_the_remote_base_cannot_grant_publication(self):
        self.withhold_on_github()
        for branch in ("pico/batch-1", "main"):  # a worker branch, and the local integrated branch
            with self.subTest(branch=branch):
                self.git("checkout", "-q", branch)
                self.commit(self.template + f"# granted on {branch}\n", f"grant publication on {branch}")
                self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)

    def test_symlinked_pi_cannot_grant_publication(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "autonomy.yaml").write_text(self.template)
        for name, base_policy in (("withheld", True), ("granted", False)):
            with self.subTest(base=name):
                if base_policy:
                    self.withhold_on_github()
                self.git("checkout", "-q", "main")
                shutil.rmtree(self.worktree / ".pi", ignore_errors=True)
                (self.worktree / ".pi").unlink(missing_ok=True)
                (self.worktree / ".pi").symlink_to(outside, target_is_directory=True)
                self.assertRefused("Refs #401\n", "AUTONOMY_POLICY_PATH_UNSAFE")
                (self.worktree / ".pi").unlink()
                self.git("checkout", "-q", "--", ".pi")

    def test_missing_base_policy_or_branch_is_not_authorized(self):
        self.api.merge(self.REPO)  # a base commit without .pi/autonomy.yaml
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)
        self.api.heads.clear()  # and no base branch on GitHub at all
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)

    def test_invalid_or_symlinked_base_policy_fails_closed(self):
        sha = "c" * 40
        for code, entry in (("AUTONOMY_POLICY_INVALID", {"type": "file", "path": ".pi/autonomy.yaml", "sha": sha,
                                                         "text": "profile: yolo\n"}),
                            ("AUTONOMY_POLICY_PATH_UNSAFE", {"type": "symlink", "path": ".pi/autonomy.yaml",
                                                             "sha": sha, "text": None}),
                            ("AUTONOMY_POLICY_PATH_UNSAFE", {"type": "file", "path": "other.yaml", "sha": sha,
                                                             "text": self.template})):
            with self.subTest(code=code, path=entry["path"]):
                self.api.merge(self.REPO, entry=entry)
                self.assertRefused("Refs #401\n", code)

    def test_caller_base_cannot_supply_authority(self):
        # The reviewer's probe: main withholds, but a branch on GitHub (reachable through one push) grants.
        self.withhold_on_github()
        self.api.merge(self.REPO, self.template + "# evil\n", branch="pico/evil-base")
        self.git("branch", "pico/evil-base")
        for text in ("Refs #401\n", "Closes #401\n"):
            with self.subTest(text=text):
                refused = self.assertRefused(text, self.NOT_AUTHORIZED, base="pico/evil-base")
                self.assertIsNone(refused["plan"])
        self.assertIn(("default_branch", self.REPO), self.api.reads)

    def test_default_branch_policy_authorizes_a_different_pr_base(self):
        self.api.merge(self.REPO, self.template + "# other\n", branch="release")  # release has no say
        self.git("branch", "release")
        result = self.dry_run("Refs #401\n", base="release")
        self.assertEqual((result["status"], result["plan"]["authorization"]), ("DRY_RUN", "policy"))
        main_commit = self.api.heads[(self.REPO.casefold(), "main")]
        self.assertEqual((result["plan"]["policy_branch"], result["plan"]["policy_commit"], result["plan"]["base"]),
                         ("main", main_commit, "release"))
        self.assertEqual(result["plan"]["policy_blob"], self.api.policies[(self.REPO.casefold(), main_commit)]["sha"])

    def test_renamed_default_branch_is_followed(self):
        self.api.default[self.REPO.casefold()] = "trunk"
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)  # no trunk on GitHub yet
        trunk = self.api.merge(self.REPO, self.template, branch="trunk")
        result = self.dry_run("Refs #401\n", base="trunk")
        self.assertEqual((result["plan"]["policy_branch"], result["plan"]["policy_commit"]), ("trunk", trunk))

    def test_default_branch_moving_after_the_dry_run_conflicts(self):
        preview = self.dry_run("Refs #401\n", base="main")
        self.api.merge(self.REPO, self.template + "# moved\n", branch="main")
        stale = self.dry_run("Refs #401\n", base="main", apply=True, expected_plan_hash=preview["plan_hash"])
        self.assertEqual(stale["blocker_code"], "GITHUB_STATE_CONFLICT")

    def test_base_policy_changing_between_dry_run_and_apply_conflicts(self):
        preview = self.dry_run("Refs #401\n")
        self.assertEqual(preview["status"], "DRY_RUN")
        self.api.merge(self.REPO, self.template + "# comment, same permissions\n")  # new blob, new base commit
        stale = self.dry_run("Refs #401\n", apply=True, expected_plan_hash=preview["plan_hash"])
        self.assertEqual((stale["blocker_code"], stale["mutations_applied"]), ("GITHUB_STATE_CONFLICT", 0))
        # A change that withdraws the permissions is refused outright at apply time.
        fresh = self.dry_run("Refs #401\n")
        self.withhold_on_github()
        revoked = self.dry_run("Refs #401\n", apply=True, expected_plan_hash=fresh["plan_hash"])
        self.assertEqual((revoked["blocker_code"], revoked["mutations_applied"]), (self.NOT_AUTHORIZED, 0))

    def test_publication_has_no_override_for_a_withholding_base_policy(self):
        # The one-shot grant was removed (#59): anyone holding the Garden token, an agent included, could confirm it.
        self.assertNotIn("grant", inspect.signature(gi.publish).parameters)
        self.assertNotIn("grant", inspect.signature(workspace_contracts.publish).parameters)
        self.withhold_on_github()
        refused = self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)
        self.assertNotIn("authorization", provenance.get_run(self.records, refused["run_id"])["github_integration"])
        # Withholding one permission is enough to refuse, and the reason names it.
        self.api.merge(self.REPO, self.template.replace("create_pr: true", "create_pr: false"))
        refused = self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)
        reason = provenance.get_run(self.records, refused["run_id"])["github_integration"]["reason"]
        self.assertTrue(reason.startswith("create_pr must be granted"), reason)

    def test_authorized_publication_records_policy_permissions_and_commit(self):
        result = self.dry_run("Refs #401\n")
        self.assertEqual(provenance.get_run(self.records, result["run_id"])["github_integration"]["authorization"],
                         {"source": "policy", "permissions": ["push_branch", "create_pr"],
                          "policy_commit": self.api.heads[(self.REPO.casefold(), "main")]})

    def test_policy_drift_is_reported_but_never_grants(self):
        self.commit(self.template.replace("create_pr: true", "create_pr: false"), "local copy withholds more")
        result = self.dry_run("Refs #401\n")
        self.assertEqual((result["status"], result["policy_drift"]), ("DRY_RUN", ["create_pr"]))
        self.assertTrue(result["plan"]["publication_permissions"]["create_pr"])  # the base policy decides
        self.assertEqual(self.dry_run("Refs #401\n")["plan_hash"], result["plan_hash"])  # drift is not in the plan
        self.withhold_on_github()
        self.commit(self.template, "local copy grants more")
        self.assertRefused("Refs #401\n", self.NOT_AUTHORIZED)  # a local copy that grants more never authorizes


if __name__ == "__main__":
    unittest.main()
