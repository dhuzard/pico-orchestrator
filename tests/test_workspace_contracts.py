#!/usr/bin/env python3
"""Contract validation and coordinated draft-PR lifecycle on real Git repositories.

Pi is the fake from test_headless_run; GitHub is a fake adapter; `git ls-remote`
and `git push` to `origin` are redirected to a local bare repository, so the real
non-force publication path runs without network access.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import github_integration as gi  # noqa: E402
import github_sync  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402
import workspace_contracts as wc  # noqa: E402
import workspace_run as wr  # noqa: E402
import workspace_scheduler as ws  # noqa: E402
from test_headless_run import FAKE_PI, git  # noqa: E402

COMMIT = ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m"]
PY = sys.executable


class FakeGithub:
    def __init__(self):
        self.created = []
        self.states = {}  # PR number -> "CLOSED" or "MERGED"; open otherwise
        self.checks = {}
        self.heads = {}  # (repo, branch) -> commit, as the GitHub API reports it
        self.policies = {}  # (repo, commit) -> contents entry for .pi/autonomy.yaml

    def default_branch(self, *, repo):
        return "main"

    def branch_head(self, branch, *, repo):
        return self.heads.get((repo, branch))

    def policy_file_at(self, commit, *, repo):
        return self.policies.get((repo, commit))

    def merge_policy(self, repo, text, branch="main"):
        """GitHub's base branch moves to a new commit carrying `text` as .pi/autonomy.yaml."""
        commit = hashlib.sha1(f"{repo}:{branch}:{len(self.heads) + len(self.policies)}".encode()).hexdigest()
        self.heads[(repo, branch)] = commit
        self.policies[(repo, commit)] = {"type": "file", "path": ".pi/autonomy.yaml",
                                         "sha": hashlib.sha1(text.encode()).hexdigest(), "text": text}
        return commit

    def list_prs(self, *, base, head, repo):
        return [{"number": c["number"], "state": self.states.get(c["number"], "OPEN")}
                for c in self.created if c["head"] == head and c["repo"] == repo]

    def create_pr(self, title, body, *, base, head, repo, draft):
        number = 100 + len(self.created) + 1
        self.created.append({"number": number, "title": title, "body": body, "base": base, "head": head,
                             "repo": repo, "draft": draft})
        return {"number": number, "url": f"https://github.com/{repo}/pull/{number}"}

    def pr_checks(self, number, *, repo):
        state, head, rollup = self.checks[number]
        return {"number": number, "state": state, "url": "u", "headRefOid": head, "isDraft": True,
                "statusCheckRollup": rollup}


GREEN = [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
PENDING = [{"__typename": "CheckRun", "name": "ci", "status": "IN_PROGRESS", "conclusion": ""}]


class ContractLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repos, self.bare = {}, {}
        for name in ("alpha", "beta"):
            path = self.base / name
            path.mkdir()
            git("-C", str(path), "init", "-q", "-b", "main")
            git("-C", str(path), "remote", "add", "origin", f"git@github.com:acme/{name}.git")
            (path / "README.md").write_text(f"# {name}\n")
            git("-C", str(path), "add", "README.md")
            git("-C", str(path), *COMMIT, "init")
            self.repos[name] = (path, git("-C", str(path), "rev-parse", "HEAD"))
            self.bare[name] = self.base / f"{name}.git"
            git("init", "-q", "--bare", str(self.bare[name]))
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.pi = self.bin / "pi"
        self.pi.write_text(FAKE_PI)
        self.pi.chmod(0o755)
        self.state = self.base / "state"
        self._env = dict(os.environ)
        os.environ.update({provenance.STATE_ENV: str(self.state), "FAKE_PI_TOOLS": str(REPO_ROOT / "tools"),
                           "PI_CODING_AGENT_DIR": str(self.base / "agent"), "FAKE_PI_BEHAVIOR": "finish"})
        self.fake = FakeGithub()
        real_run = subprocess.run

        def redirect(argv, *args, **kwargs):
            if isinstance(argv, list) and argv[:1] == ["git"] and len(argv) > 3 and argv[3] in {"ls-remote", "push"}:
                # Compare resolved paths: on macOS the temp dir /var/... resolves to /private/var/...
                target = Path(argv[2]).resolve()
                repo = next(n for n, (p, _) in self.repos.items() if p.resolve() == target)
                argv = [str(self.bare[repo]) if a == "origin" else a for a in argv]
            return real_run(argv, *args, **kwargs)
        patcher = mock.patch.object(gi.subprocess, "run", side_effect=redirect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def manifest(self, **task_overrides) -> Path:
        claims = [{"contract": "claims", "version": "1.0"}]
        tasks = {
            "a1": {"id": "a1", "repository": "alpha", "title": "Claim adapter", "issue": 7, "provides": claims,
                   "checks": [{"id": "schema", "repository": "alpha",
                               "argv": [PY, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('claims.json').exists() else 1)"]}]},
            "b1": {"id": "b1", "repository": "beta", "title": "Claim consumer", "issue": 9, "depends_on": ["a1"],
                   "requires": claims, "checks": [{"id": "unit", "repository": "beta", "argv": [PY, "-c", "pass"]}]},
            "q1": {"id": "q1", "repository": "beta", "title": "Cross-repository fixtures", "kind": "qualification",
                   "depends_on": ["a1", "b1"],
                   "checks": [{"id": "fixtures-alpha", "repository": "alpha",
                               "argv": [PY, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('claims.json').exists() else 1)"]},
                              {"id": "fixtures-beta", "repository": "beta",
                               "argv": [PY, "-c", "import pathlib,sys; sys.exit(0 if pathlib.Path('consumer.py').exists() else 1)"]}]},
        }
        for task_id, override in task_overrides.items():
            tasks[task_id].update(override)
        data = {"schema_version": 1, "id": "ws", "goal": "Interoperable claims", "max_parallel": 2,
                "repositories": [{"id": n, "repository": f"acme/{n}", "path": n, "pinned_commit": sha}
                                 for n, (_, sha) in self.repos.items()],
                "tasks": list(tasks.values())}
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def work(self, repo: str, branch: str, filename: str) -> str:
        """Simulate a child run's committed work on a pico/ branch, leaving HEAD at the pin."""
        path, _ = self.repos[repo]
        git("-C", str(path), "checkout", "-q", "-b", branch)
        (path / filename).write_text("{}\n")
        git("-C", str(path), "add", filename)
        git("-C", str(path), *COMMIT, f"work on {branch}")
        sha = git("-C", str(path), "rev-parse", "HEAD")
        git("-C", str(path), "checkout", "-q", "main")
        return sha

    def allow_publication(self, repo: str):
        path, _ = self.repos[repo]
        (path / ".pi").mkdir(exist_ok=True)
        policy = (REPO_ROOT / "templates/autonomy.yaml").read_text()
        granting = policy.replace("push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        (path / ".pi/autonomy.yaml").write_text(granting)
        git("-C", str(path), "add", ".pi/autonomy.yaml")
        git("-C", str(path), *COMMIT, "grant publication")
        # Publication reads the policy GitHub has on the base branch, never the checkout's copy.
        self.fake.merge_policy(f"acme/{repo}", granting)

    def schedule(self, manifest):
        return ws.run(manifest, access_check=None, pi_executable=str(self.pi), poll_seconds=0.05)

    def plan_task(self, manifest, task_id):
        return next(t for t in wr._plan(manifest, access_check=None)["tasks"] if t["id"] == task_id)

    def publish(self, manifest, task_id):
        preview = wc.publish(manifest=manifest, task_id=task_id, adapter=self.fake)
        self.assertEqual(preview["status"], "DRY_RUN", preview)
        return wc.publish(manifest=manifest, task_id=task_id, apply=True, plan_hash=preview["plan_hash"], adapter=self.fake)

    # -- tests -------------------------------------------------------------
    def test_full_lifecycle_across_two_repositories(self):
        manifest = self.manifest()
        first = self.schedule(manifest)
        self.assertEqual({r["id"]: r["state"] for r in first["tasks"]},
                         {"a1": "partial", "b1": "waiting_for_readiness", "q1": "waiting_for_readiness"})
        a1_commit = self.work("alpha", "pico/a1", "claims.json")
        snapshots = {n: session_scope.snapshot(str(p))["digest"] for n, (p, _) in self.repos.items()}

        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.validate(manifest=manifest, task_id="a1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_HEAD_INVALID")
        validated = wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        self.assertEqual(validated["status"], "VALIDATED", validated)
        self.assertEqual(validated["observed"]["validated_commit"], a1_commit)
        self.assertEqual(validated["observed"]["validated_contracts"], {"claims": "1.0"})
        self.assertEqual({n: session_scope.snapshot(str(p))["digest"] for n, (p, _) in self.repos.items()}, snapshots)
        self.assertEqual(len(git("-C", str(self.repos["alpha"][0]), "worktree", "list").splitlines()), 1)

        # Validated is not enough for a downstream implementation task.
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], ["a1"])

        denied = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
        self.assertEqual((denied["ok"], denied["blocker_code"]), (False, "PUBLICATION_NOT_AUTHORIZED"))
        self.allow_publication("alpha")
        preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "for-each-ref"), "")
        stale = wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash="0" * 64, adapter=self.fake)
        self.assertEqual(stale["blocker_code"], "GITHUB_STATE_CONFLICT")
        applied = wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"], adapter=self.fake)
        self.assertEqual((applied["status"], applied["pr"]), ("COMPLETED", 101))
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1"), a1_commit)
        pr = self.fake.created[0]
        self.assertTrue(pr["draft"])
        self.assertIn("Refs #7", pr["body"])
        self.assertIn("`claims` 1.0", pr["body"])
        self.assertIn(a1_commit, pr["body"])

        for status, head in ((PENDING, a1_commit), ([], a1_commit), (GREEN, "f" * 40)):
            self.fake.checks[101] = ("OPEN", head, status)
            with self.assertRaises(wr.WorkspaceRunError) as ctx:
                wc.promote(manifest=manifest, task_id="a1", adapter=self.fake)
            self.assertEqual(ctx.exception.code, "WORKSPACE_CI_NOT_GREEN")
        self.fake.checks[101] = ("OPEN", a1_commit, GREEN)
        promoted = wc.promote(manifest=manifest, task_id="a1", adapter=self.fake)
        self.assertEqual(promoted["readiness"], "ready_for_downstream")
        self.assertTrue(self.plan_task(manifest, "b1")["dispatchable"])

        second = self.schedule(manifest)
        self.assertEqual(second["session"]["dispatched"], ["b1"])
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        upstream = record["tasks"]["b1"]["inputs"]["upstream"][0]
        self.assertEqual((upstream["validated_commit"], upstream["contracts"], upstream["pr"]),
                         (a1_commit, {"claims": "1.0"}, "acme/alpha#101"))

        b1_commit = self.work("beta", "pico/b1", "consumer.py")
        self.assertEqual(wc.validate(manifest=manifest, task_id="b1", head="pico/b1")["status"], "VALIDATED")
        self.allow_publication("beta")
        self.assertEqual(self.publish(manifest, "b1")["pr"], 102)
        self.assertIn("Depends on:\n- acme/alpha#101 (task `a1`)", self.fake.created[1]["body"])

        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wr.launch(manifest=manifest, task_id="q1", access_check=None, pi_executable=str(self.pi))
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_NOT_LAUNCHABLE")
        third = self.schedule(manifest)
        self.assertEqual(third["session"]["qualified"], ["q1"])
        self.assertEqual(third["session"]["dispatched"], [])
        states = {r["id"]: r["state"] for r in third["tasks"]}
        self.assertEqual(states, {"a1": "ready_for_downstream", "b1": "validated", "q1": "validated"})
        q1 = wr.load_workspace(wr.workspace_record_path("ws"))["tasks"]["q1"]["observed"]
        self.assertEqual(q1["validation"]["commits"], {"alpha": a1_commit, "beta": b1_commit})
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="q1", adapter=self.fake)
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_NOT_PUBLISHABLE")

        # Merging is observed, never performed.
        self.fake.checks[101] = ("MERGED", a1_commit, GREEN)
        self.assertEqual(wc.ci(manifest=manifest, task_id="a1", adapter=self.fake)["readiness"], "merged")
        self.assertEqual(len(self.fake.created), 2)

    def test_republishing_after_the_pr_was_closed_uses_a_new_branch(self):
        # The #4 pilot: once a task's draft PR was closed, `pico/<task>` could never be published again (#67).
        manifest = self.manifest()
        self.schedule(manifest)
        a1_commit = self.work("alpha", "pico/a1", "claims.json")
        wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        self.allow_publication("alpha")
        self.assertEqual(self.publish(manifest, "a1")["pr"], 101)
        # While the PR is open there is nothing to supersede.
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(ctx.exception.code, "PUBLICATION_NEW_BRANCH_UNNEEDED")
        self.fake.states[101] = "CLOSED"
        refused = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
        self.assertEqual(refused["blocker_code"], "PUBLICATION_PR_CONFLICT")
        preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(preview["status"], "DRY_RUN", preview)
        self.assertEqual((preview["plan"]["head"], preview["plan"]["remote_head"], preview["plan"]["supersedes_prs"]),
                         ("pico/a1", "pico/a1-2", [101]))
        applied = wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"],
                             adapter=self.fake, new_branch=True)
        self.assertEqual((applied["status"], applied["pr"]), ("COMPLETED", 102))
        new = self.fake.created[-1]
        self.assertEqual((new["head"], new["draft"]), ("pico/a1-2", True))
        self.assertIn("Supersedes #101 (closed without merging).", new["body"])
        # The validated commit is on the new branch; the closed PR's branch is left as it was.
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1-2"), a1_commit)
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1"), a1_commit)
        pr = wr.load_workspace(wr.workspace_record_path("ws"))["tasks"]["a1"]["observed"]["pr"]
        self.assertEqual((pr["number"], pr["branch"], pr["supersedes"]), (102, "pico/a1-2", [101]))
        # Repeating the republication reuses its open PR; nothing new is created.
        again = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual((again["plan"]["remote_head"], again["plan"]["existing_pr"]), ("pico/a1-2", 102))
        # The republished PR goes through CI and promotion as usual, and downstream links it.
        self.fake.checks[102] = ("OPEN", a1_commit, GREEN)
        self.assertTrue(wc.promote(manifest=manifest, task_id="a1", adapter=self.fake)["ok"])
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], [])
        # A plan whose republication branch no longer matches is refused at apply.
        (self.base / "body.md").write_text("Refs #7\n")
        stale = gi.publish(worktree=self.repos["alpha"][0], base="main", head="pico/a1", title="t",
                           body_file=self.base / "body.md", explicit_repo="acme/alpha", adapter=self.fake,
                           remote_head="pico/a1-3", recorded_pr=pr, provenance_root=REPO_ROOT)
        self.assertEqual(stale["blocker_code"], "GITHUB_STATE_CONFLICT")
        # A republished PR that is closed too is superseded with the first one, never reused.
        self.fake.states[102] = "CLOSED"
        third = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual((third["plan"]["remote_head"], third["plan"]["supersedes_prs"], third["plan"]["existing_pr"]),
                         ("pico/a1-3", [101, 102], None))
        # A merged PR is never superseded.
        self.fake.states[102] = "MERGED"
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(ctx.exception.code, "PUBLICATION_PR_CONFLICT")

    def _closed_and_published(self, manifest):
        """a1 validated and published as draft PR 101 on pico/a1 (still open)."""
        self.schedule(manifest)
        commit = self.work("alpha", "pico/a1", "claims.json")
        wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        self.allow_publication("alpha")
        self.assertEqual(self.publish(manifest, "a1")["pr"], 101)
        return commit

    def _foreign_pr(self, number, branch, state="OPEN"):
        self.fake.created.append({"number": number, "title": "other", "body": "", "base": "main", "head": branch,
                                  "repo": "acme/alpha", "draft": True})
        if state != "OPEN":
            self.fake.states[number] = state

    def test_republication_skips_a_branch_with_someone_elses_pr(self):
        manifest = self.manifest()
        a1_commit = self._closed_and_published(manifest)
        # An unrelated open PR on pico/a1-2 whose branch is an ancestor of a1's commit: a fast-forward would succeed.
        _, pin = self.repos["alpha"]
        git("-C", str(self.repos["alpha"][0]), "push", "-q", str(self.bare["alpha"]), f"{pin}:refs/heads/pico/a1-2")
        self._foreign_pr(150, "pico/a1-2")
        # And someone else's closed PR on pico/a1-3: not this task's, so neither superseded nor reused.
        self._foreign_pr(151, "pico/a1-3", state="CLOSED")
        self.fake.states[101] = "CLOSED"
        preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual((preview["plan"]["remote_head"], preview["plan"]["supersedes_prs"], preview["plan"]["existing_pr"]),
                         ("pico/a1-4", [101], None))
        applied = wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"],
                             adapter=self.fake, new_branch=True)
        self.assertEqual(applied["status"], "COMPLETED", applied)
        self.assertNotIn(applied["pr"], (101, 150, 151))
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1-2"), pin)
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1-4"), a1_commit)
        pr = wr.load_workspace(wr.workspace_record_path("ws"))["tasks"]["a1"]["observed"]["pr"]
        self.assertEqual((pr["number"], pr["branch"], pr["supersedes"]), (applied["pr"], "pico/a1-4", [101]))

    def test_republication_skips_reserved_and_local_branches(self):
        manifest = self.manifest()
        self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        checkout = self.repos["alpha"][0]
        git("-C", str(checkout), "branch", "pico/a1-2", "main")  # a local branch, for example another task's work
        chosen = gi.republication_branch(checkout, "acme/alpha", "main", "pico/a1", self.fake,
                                         recorded_pr={"number": 101}, reserved=frozenset({"pico/a1-3"}))
        self.assertEqual(chosen, {"remote_head": "pico/a1-4", "supersedes": [101]})

    def test_republication_refuses_its_own_pr_holding_another_commit(self):
        manifest = self.manifest()
        self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"], adapter=self.fake,
                   new_branch=True)
        # Someone pushes an unrelated commit to the republished branch.
        checkout = self.repos["alpha"][0]
        git("-C", str(checkout), "checkout", "-q", "--orphan", "stray")
        git("-C", str(checkout), *COMMIT, "stray", "--allow-empty")
        stray = git("-C", str(checkout), "rev-parse", "HEAD")
        git("-C", str(checkout), "checkout", "-q", "-f", "main")
        git("-C", str(checkout), "push", "-q", "-f", str(self.bare["alpha"]), f"{stray}:refs/heads/pico/a1-2")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(ctx.exception.code, "PUBLICATION_PR_CONFLICT")

    def test_republishing_a_promoted_task_requires_promotion_on_the_new_pr(self):
        manifest = self.manifest()
        a1_commit = self._closed_and_published(manifest)
        self.fake.checks[101] = ("OPEN", a1_commit, GREEN)
        self.assertTrue(wc.promote(manifest=manifest, task_id="a1", adapter=self.fake)["ok"])
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], [])
        self.fake.states[101] = "CLOSED"
        preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(preview["status"], "DRY_RUN", preview)
        # The apply needs --new-branch too: without it, the plan (and its hash) is the plain one, which is refused.
        plain = wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"], adapter=self.fake)
        self.assertEqual(plain["blocker_code"], "PUBLICATION_PR_CONFLICT")
        applied = wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"],
                             adapter=self.fake, new_branch=True)
        self.assertEqual(applied["status"], "COMPLETED", applied)
        observed = wr.load_workspace(wr.workspace_record_path("ws"))["tasks"]["a1"]["observed"]
        self.assertEqual(observed["readiness"], "validated")
        self.assertNotIn("promoted_at", observed)
        self.assertNotIn("ci", observed)
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], ["a1"])
        self.fake.checks[applied["pr"]] = ("OPEN", a1_commit, GREEN)
        self.assertTrue(wc.promote(manifest=manifest, task_id="a1", adapter=self.fake)["ok"])
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], [])

    def test_republication_lookup_failure_is_a_stable_error(self):
        manifest = self.manifest()
        self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        for error in (github_sync.GithubSyncError("auth expired"), ValueError("unexpected gh output")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.fake, "list_prs", side_effect=error):
                    with self.assertRaises(wr.WorkspaceRunError) as ctx:
                        wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
                self.assertEqual(ctx.exception.code, "PUBLICATION_FAILED")

    def _republish(self, manifest, **patches):
        preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(preview["status"], "DRY_RUN", preview)
        with mock.patch.multiple(self.fake, **patches) if patches else contextlib.nullcontext():
            return wc.publish(manifest=manifest, task_id="a1", apply=True, plan_hash=preview["plan_hash"],
                              adapter=self.fake, new_branch=True)

    def test_republication_refuses_an_unrecorded_open_pr_at_the_validated_commit(self):
        # `gh pr create` made the PR but its answer was lost: the run ends PARTIAL and the PR is not recorded.
        manifest = self.manifest()
        self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        create = self.fake.create_pr

        def created_then_failed(*args, **kwargs):
            create(*args, **kwargs)
            raise github_sync.GithubSyncError("timed out after creating")
        first = self._republish(manifest, create_pr=mock.Mock(side_effect=created_then_failed))
        self.assertEqual(first["status"], "PARTIAL", first)
        orphan = self.fake.created[-1]["number"]
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="a1", adapter=self.fake, new_branch=True)
        self.assertEqual(ctx.exception.code, "PUBLICATION_PR_CONFLICT")
        self.assertIn(f"#{orphan}", ctx.exception.detail)
        self.assertEqual([c["head"] for c in self.fake.created].count("pico/a1-3"), 0)

    def test_republication_skips_another_tasks_open_pr_at_the_same_commit(self):
        manifest = self.manifest()
        a1_commit = self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        checkout = self.repos["alpha"][0]
        git("-C", str(checkout), "push", "-q", str(self.bare["alpha"]), f"{a1_commit}:refs/heads/pico/a1-2")
        self._foreign_pr(150, "pico/a1-2")
        chosen = gi.republication_branch(checkout, "acme/alpha", "main", "pico/a1", self.fake,
                                         recorded_pr={"number": 101}, reserved=frozenset({"pico/a1-2"}))
        self.assertEqual(chosen, {"remote_head": "pico/a1-3", "supersedes": [101]})
        # Not reserved, the same PR refuses, and the message says how to recover.
        with self.assertRaises(gi.IntegrationError) as ctx:
            gi.republication_branch(checkout, "acme/alpha", "main", "pico/a1", self.fake, recorded_pr={"number": 101})
        self.assertEqual(ctx.exception.code, "PUBLICATION_PR_CONFLICT")
        self.assertIn("close it", ctx.exception.detail)
        self.assertNotIn("or record it", ctx.exception.detail)

    def test_republication_resumes_a_push_whose_pr_was_not_created(self):
        manifest = self.manifest()
        a1_commit = self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        first = self._republish(manifest, create_pr=mock.Mock(side_effect=github_sync.GithubSyncError("down")))
        self.assertEqual(first["status"], "PARTIAL", first)
        # Even with a local branch of the same name (someone checked it out), the pushed branch is reused.
        git("-C", str(self.repos["alpha"][0]), "branch", "pico/a1-2", "pico/a1")
        again = self._republish(manifest)
        self.assertEqual((again["status"], again["plan"]["remote_head"], again["mutations_applied"]),
                         ("COMPLETED", "pico/a1-2", 1), again)
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "rev-parse", "refs/heads/pico/a1-2"), a1_commit)
        self.assertEqual([c["head"] for c in self.fake.created], ["pico/a1", "pico/a1-2"])

    def test_a_republication_rerun_that_changes_nothing_keeps_the_promotion(self):
        manifest = self.manifest()
        a1_commit = self._closed_and_published(manifest)
        self.fake.states[101] = "CLOSED"
        applied = self._republish(manifest)
        self.fake.checks[applied["pr"]] = ("OPEN", a1_commit, GREEN)
        self.assertTrue(wc.promote(manifest=manifest, task_id="a1", adapter=self.fake)["ok"])
        before = wr.load_workspace(wr.workspace_record_path("ws"))["tasks"]["a1"]["observed"]
        rerun = self._republish(manifest)
        self.assertEqual((rerun["status"], rerun["pr"], rerun["mutations_applied"]), ("COMPLETED", applied["pr"], 0))
        after = wr.load_workspace(wr.workspace_record_path("ws"))["tasks"]["a1"]["observed"]
        self.assertEqual(after["readiness"], "ready_for_downstream")
        self.assertEqual((after["pr"], after["promoted_at"], after["ci"]), (before["pr"], before["promoted_at"], before["ci"]))
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], [])

    def test_cli_passes_new_branch(self):
        with mock.patch.object(wc, "publish", return_value={"ok": True}) as publish:
            self.assertEqual(wc.main(["publish", "workspace.yaml", "--task", "a1", "--new-branch"]), 0)
        self.assertTrue(publish.call_args.kwargs["new_branch"])

    def test_failed_check_does_not_validate_or_publish(self):
        manifest = self.manifest()
        self.schedule(manifest)
        self.work("alpha", "pico/a1", "other.json")
        result = wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        self.assertEqual((result["ok"], result["error_code"]), (False, "WORKSPACE_CHECK_FAILED"))
        self.assertEqual(result["checks"][0]["status"], "failed")
        self.assertTrue(Path(result["checks"][0]["log_path"]).is_file())
        self.assertEqual(self.plan_task(manifest, "a1")["readiness"], "implemented")
        self.allow_publication("alpha")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_NOT_VALIDATED")
        self.assertEqual(self.fake.created, [])

    def test_head_moved_after_validation_blocks_publication(self):
        manifest = self.manifest()
        self.schedule(manifest)
        self.work("alpha", "pico/a1", "claims.json")
        wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        path, _ = self.repos["alpha"]
        git("-C", str(path), "checkout", "-q", "pico/a1")
        (path / "late.txt").write_text("x\n")
        git("-C", str(path), "add", "late.txt")
        git("-C", str(path), *COMMIT, "unvalidated change")
        git("-C", str(path), "checkout", "-q", "main")
        self.allow_publication("alpha")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
        self.assertEqual(ctx.exception.code, "WORKSPACE_HEAD_CHANGED")
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "for-each-ref"), "")

    def test_task_must_be_implemented_and_head_must_descend_from_pin(self):
        manifest = self.manifest()
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_NOT_IMPLEMENTED")
        self.schedule(manifest)
        path, _ = self.repos["alpha"]
        git("-C", str(path), "checkout", "-q", "--orphan", "pico/a1")
        (path / "claims.json").write_text("{}\n")
        git("-C", str(path), "add", "claims.json")
        git("-C", str(path), *COMMIT, "unrelated history")
        git("-C", str(path), "checkout", "-q", "-f", "main")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.validate(manifest=manifest, task_id="a1", head="pico/a1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_HEAD_UNRELATED")

    def test_issue_reference_defaults_to_refs_and_closes_on_opt_in(self):
        def dry_run(manifest):
            preview = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
            self.assertEqual(preview["status"], "DRY_RUN", preview)
            plan = wr._plan(manifest, access_check=None)
            return (wc._scratch(plan, "a1") / "pr-body.md").read_text(encoding="utf-8"), preview

        manifest = self.manifest()
        self.schedule(manifest)
        self.work("alpha", "pico/a1", "claims.json")
        self.assertEqual(wc.validate(manifest=manifest, task_id="a1", head="pico/a1")["status"], "VALIDATED")
        self.allow_publication("alpha")
        path = self.repos["alpha"][0]
        policy = path / ".pi/autonomy.yaml"
        refs_policy = policy.read_text()
        closes_policy = refs_policy.replace("issue_reference: refs", "issue_reference: closes")
        github_main = self.fake.merge_policy("acme/alpha", refs_policy)

        with self.subTest("default writes Refs"):
            body, preview = dry_run(manifest)
            self.assertTrue(body.startswith("Refs #7\n"), body)
            self.assertEqual(preview["plan"]["issue_reference"], "refs")
            self.assertEqual(preview["issue_reference_decision"],
                             {"closes": False, "source": "repository", "policy_branch": "main",
                              "policy_commit": github_main,
                              "policy_blob": self.fake.policies[("acme/alpha", github_main)]["sha"]})

        with self.subTest("closes_issue: true writes Closes, though the repository says refs"):
            body, preview = dry_run(self.manifest(a1={"closes_issue": True}))
            self.assertTrue(body.startswith("Closes #7\n"), body)
            self.assertEqual(preview["plan"]["issue_reference"], "closes")
            self.assertEqual(preview["issue_reference_decision"]["source"], "task")

        with self.subTest("the opt-in may close only the task's own issue"):
            stray = wc.publish(manifest=self.manifest(a1={"closes_issue": True, "title": "Claim adapter, fixes #8"}),
                               task_id="a1", adapter=self.fake)
            self.assertEqual((stray["status"], stray["blocker_code"]), ("BLOCKED", "PUBLICATION_BODY_INVALID"))

        with self.subTest("an uncommitted repository policy changes nothing"):
            policy.write_text(closes_policy)
            self.assertTrue(dry_run(self.manifest())[0].startswith("Refs #7\n"))

        git("-C", str(path), "add", ".pi/autonomy.yaml")
        git("-C", str(path), *COMMIT, "close issues on merge")
        with self.subTest("a policy committed locally but not on GitHub changes nothing"):
            self.assertTrue(dry_run(self.manifest())[0].startswith("Refs #7\n"))

        github_main = self.fake.merge_policy("acme/alpha", closes_policy)
        with self.subTest("the policy at GitHub's base commit decides when the task is silent"):
            body, preview = dry_run(self.manifest())
            self.assertTrue(body.startswith("Closes #7\n"), body)
            self.assertEqual(preview["plan"]["closes_issues"], [7])
            self.assertEqual(preview["plan"]["base_commit"], github_main)
            self.assertEqual(preview["issue_reference_decision"]["closes"], True)

        with self.subTest("closes_issue: false keeps Refs"):
            self.assertTrue(dry_run(self.manifest(a1={"closes_issue": False}))[0].startswith("Refs #7\n"))

        with self.subTest("an applied opt-in publishes Closes"):
            applied = self.publish(self.manifest(a1={"closes_issue": True}), "a1")
            self.assertEqual(applied["status"], "COMPLETED", applied)
            self.assertTrue(self.fake.created[0]["body"].startswith("Closes #7\n"))

    def test_workspace_publication_ignores_permissions_set_only_in_the_checkout(self):
        manifest = self.manifest()
        self.schedule(manifest)
        self.work("alpha", "pico/a1", "claims.json")
        self.assertEqual(wc.validate(manifest=manifest, task_id="a1", head="pico/a1")["status"], "VALIDATED")
        path = self.repos["alpha"][0]
        policy = path / ".pi/autonomy.yaml"
        policy.parent.mkdir(exist_ok=True)
        text = (REPO_ROOT / "templates/autonomy.yaml").read_text()
        granting = text.replace("push_branch: false", "push_branch: true").replace("create_pr: false", "create_pr: true")
        self.fake.merge_policy("acme/alpha", text)  # GitHub's base withholds both
        with self.subTest("uncommitted edit"):
            policy.write_text(granting)
            blocked = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
            self.assertEqual(blocked["blocker_code"], "PUBLICATION_NOT_AUTHORIZED")
        with self.subTest("committed locally, not on GitHub"):
            git("-C", str(path), "add", ".pi/autonomy.yaml")
            git("-C", str(path), *COMMIT, "grant publication")
            blocked = wc.publish(manifest=manifest, task_id="a1", adapter=self.fake)
            self.assertEqual(blocked["blocker_code"], "PUBLICATION_NOT_AUTHORIZED")
        self.assertEqual(self.fake.created, [])
        self.assertEqual(git("--git-dir", str(self.bare["alpha"]), "for-each-ref"), "")

    def test_downstream_publication_requires_upstream_pr(self):
        # The manifest may declare a1 ready by hand; b1's PR must still link a real upstream PR.
        manifest = self.manifest(a1={"readiness": "ready_for_downstream"})
        observed = {"readiness": "validated", "validated_contracts": {"claims": "1.0"}}
        with wr.locked_workspace("ws"):
            plan = wr._plan(manifest, access_check=None)
            path, record = wr._workspace(plan, manifest)
            record["tasks"]["a1"] = {"observed": observed}
            wr.save_workspace(path, record)
        self.schedule(manifest)
        self.work("beta", "pico/b1", "consumer.py")
        self.assertEqual(wc.validate(manifest=manifest, task_id="b1", head="pico/b1")["status"], "VALIDATED")
        self.allow_publication("beta")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            wc.publish(manifest=manifest, task_id="b1", adapter=self.fake)
        self.assertEqual(ctx.exception.code, "WORKSPACE_UPSTREAM_UNPUBLISHED")

    def test_unvalidated_contract_blocks_even_a_declared_ready_dependency(self):
        manifest = self.manifest(a1={"readiness": "ready_for_downstream"})
        self.assertEqual(self.plan_task(manifest, "b1")["blocked_by"], ["a1"])
        merged = self.manifest(a1={"readiness": "merged"})
        self.assertEqual(self.plan_task(merged, "b1")["blocked_by"], [])

    def test_static_contract_errors(self):
        cases = {
            "WORKSPACE_CONTRACT_UNSATISFIED": {"b1": {"depends_on": []}},
            "WORKSPACE_CONTRACT_VERSION_MISMATCH": {"b1": {"requires": [{"contract": "claims", "version": "2.0"}]}},
            "WORKSPACE_CONTRACT_DUPLICATE": {"b1": {"provides": [{"contract": "claims", "version": "1.0"}],
                                                    "requires": []}},
            "WORKSPACE_QUALIFICATION_INVALID": {"q1": {"checks": []}},
            "WORKSPACE_CHECK_SCOPE_INVALID": {"a1": {"checks": [{"id": "x", "repository": "beta", "argv": ["true"]}]}},
            "WORKSPACE_CHECK_REPOSITORY_UNKNOWN": {"q1": {"checks": [{"id": "x", "repository": "gamma", "argv": ["true"]}]}},
        }
        for code, overrides in cases.items():
            with self.subTest(code=code):
                plan = wr._plan(self.manifest(**overrides), access_check=None)
                self.assertIn(code, [b["code"] for b in plan["blockers"]])

    def test_ci_classification(self):
        classify = github_sync.classify_checks
        self.assertEqual(classify([]), "none")
        self.assertEqual(classify(GREEN), "green")
        self.assertEqual(classify(PENDING), "pending")
        self.assertEqual(classify(GREEN + [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]), "red")
        self.assertEqual(classify([{"__typename": "StatusContext", "context": "ci", "state": "SUCCESS"}]), "green")
        self.assertEqual(classify([{"__typename": "StatusContext", "context": "ci", "state": "PENDING"}]), "pending")
        self.assertEqual(classify([{"__typename": "StatusContext", "context": "ci", "state": "ERROR"}]), "red")
        self.assertEqual(classify([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"}]), "green")


if __name__ == "__main__":
    unittest.main()
