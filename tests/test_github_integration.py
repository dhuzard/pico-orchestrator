"""External target integration and trust-boundary regression tests; no live GitHub."""
import json
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
        denied = gi.publish(worktree=self.worktree, base="main", head="pico/batch-1",
                            title="Reviewed batch", body_file=body, adapter=self.fake,
                            provenance_root=self.records)
        self.assertEqual(denied["blocker_code"], "PUBLICATION_NOT_AUTHORIZED")
        policy = (ROOT / "templates/autonomy.yaml").read_text().replace("push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        (self.worktree / ".pi").mkdir()
        (self.worktree / ".pi/autonomy.yaml").write_text(policy)
        state = {"remote": "origin", "commit": "a" * 40, "remote_commit": None, "existing_pr": None}
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

    def test_publication_gate_accepts_closes_only_when_authorized(self):
        self.remote("https://github.com/Examplecorp/examplelab.git")
        template = (ROOT / "templates/autonomy.yaml").read_text().replace(
            "push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        self.assertIn("issue_reference: refs", template)
        (self.worktree / ".pi").mkdir()
        policy = self.worktree / ".pi/autonomy.yaml"
        body = Path(self.tmp.name) / "pr-body.md"
        state = {"remote": "origin", "commit": "a" * 40, "remote_commit": None, "existing_pr": None}

        def dry_run(text, **kwargs):
            body.write_text(text)
            with patch.object(gi, "_publication_state", return_value=state), \
                 patch.object(gi, "_branch", side_effect=lambda _worktree, name, **_kwargs: name):
                return gi.publish(worktree=self.worktree, base="main", head="pico/batch-1", title="Reviewed batch",
                                  body_file=body, adapter=self.fake, provenance_root=self.records, **kwargs)

        # Default policy (refs): Refs passes, any closing keyword is refused, wherever it appears.
        policy.write_text(template)
        refs = dry_run("Summary\n\nRefs #401\n")
        self.assertEqual((refs["status"], refs["plan"]["issue_reference"]), ("DRY_RUN", "refs"))
        for text in ("Summary\n\nCloses #401\n", "Summary\n\nRefs #401\nAlso fixes: Examplecorp/examplelab#464\n",
                     "Resolved #401\n"):
            refused = dry_run(text)
            self.assertEqual((refused["status"], refused["blocker_code"]), ("BLOCKED", "PUBLICATION_BODY_INVALID"), text)
        self.assertEqual(dry_run("Refs #401\nDisclosed #464, prefixed fix#3\n")["status"], "DRY_RUN")
        # An explicit caller opt-in authorizes Closes without changing the repository policy.
        opted = dry_run("Summary\n\nCloses #401\n", allow_closes=True)
        self.assertEqual((opted["status"], opted["plan"]["issue_reference"]), ("DRY_RUN", "closes"))
        self.assertEqual(dry_run("Summary only\n", allow_closes=True)["blocker_code"], "PUBLICATION_BODY_INVALID")
        # issue_reference: closes authorizes both forms; the plan hash covers the reference mode and body.
        policy.write_text(template.replace("issue_reference: refs", "issue_reference: closes"))
        closes = dry_run("Summary\n\nCloses #401\n")
        self.assertEqual((closes["status"], closes["plan"]["issue_reference"]), ("DRY_RUN", "closes"))
        self.assertEqual(dry_run("Summary\n\nRefs #401\n")["status"], "DRY_RUN")
        self.assertNotEqual(closes["plan_hash"], refs["plan_hash"])
        # An invalid value fails closed.
        policy.write_text(template.replace("issue_reference: refs", "issue_reference: fixes"))
        self.assertEqual(dry_run("Summary\n\nRefs #401\n")["blocker_code"], "AUTONOMY_POLICY_INVALID")

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


if __name__ == "__main__":
    unittest.main()
