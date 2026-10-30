#!/usr/bin/env python3
"""Deterministic tests for the read-only workspace planner on real Git repositories."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import session_scope  # noqa: E402
import workspace_plan as wp  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(base: Path, name: str, remotes: dict[str, str]) -> tuple[Path, str]:
    path = base / name
    path.mkdir()
    git("-C", str(path), "init", "-q")
    for remote, url in remotes.items():
        git("-C", str(path), "remote", "add", remote, url)
    (path / "README.md").write_text(f"# {name}\n")
    git("-C", str(path), "add", "README.md")
    git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    return path, git("-C", str(path), "rev-parse", "HEAD")


def ok_access(repository: str) -> dict:
    return {"status": "ok", "blocker_code": None, "detail": None, "private": True}


class WorkspacePlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.alpha, self.alpha_sha = make_repo(self.base, "alpha", {"origin": "git@github.com:acme/alpha.git"})
        self.beta, self.beta_sha = make_repo(self.base, "beta", {"origin": "https://github.com/acme/beta.git"})
        self.gamma, self.gamma_sha = make_repo(self.base, "gamma", {"origin": "https://github.com/acme/gamma"})

    def tearDown(self):
        self._tmp.cleanup()

    def manifest(self, **overrides) -> Path:
        data = {
            "schema_version": 1, "id": "ws-test", "goal": "Cross-repo test", "max_parallel": 2,
            "repositories": [
                {"id": "alpha", "repository": "acme/alpha", "path": "alpha", "pinned_commit": self.alpha_sha},
                {"id": "beta", "repository": "acme/beta", "path": "beta", "pinned_commit": self.beta_sha},
                {"id": "gamma", "repository": "acme/gamma", "path": "gamma", "pinned_commit": self.gamma_sha},
            ],
            "tasks": [
                {"id": "a1", "repository": "alpha", "title": "Alpha adapter"},
                {"id": "b1", "repository": "beta", "title": "Beta extraction"},
                {"id": "g1", "repository": "gamma", "title": "Gamma consumer", "depends_on": ["a1", "b1"]},
                {"id": "g2", "repository": "gamma", "title": "Gamma qualification", "depends_on": ["g1"]},
            ],
        }
        data.update(overrides)
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def snapshots(self) -> list[str]:
        return [session_scope.snapshot(str(p))["digest"] for p in (self.alpha, self.beta, self.gamma)]

    def test_valid_manifest_plans_waves_without_mutation(self):
        before = self.snapshots()
        result = wp.plan(self.manifest(), access_check=ok_access)
        self.assertEqual(result["status"], "PLANNED", result["blockers"])
        self.assertEqual(result["waves"], [["a1", "b1"], ["g1"], ["g2"]])
        self.assertEqual(result["mutations"], {"git": 0, "github": 0, "workers": 0})
        self.assertEqual(self.snapshots(), before)
        by_id = {r["id"]: r for r in result["repositories"]}
        self.assertEqual(by_id["alpha"]["repository"], "acme/alpha")
        self.assertEqual(by_id["alpha"]["pinned_commit"], self.alpha_sha)
        self.assertEqual(by_id["alpha"]["access"]["status"], "ok")
        self.assertTrue(by_id["alpha"]["git_common_dir"].endswith(".git"))
        g1 = next(t for t in result["tasks"] if t["id"] == "g1")
        self.assertEqual(g1["depends_on"], ["a1", "b1"])
        self.assertEqual(g1["blocks"], ["g2"])
        self.assertEqual(g1["wave"], 1)
        self.assertIsNotNone(result["orchestrator_revision"])
        for wave_index, wave in enumerate(result["waves"]):
            for task_id in wave:
                task = next(t for t in result["tasks"] if t["id"] == task_id)
                for dep in task["depends_on"]:
                    dep_task = next(t for t in result["tasks"] if t["id"] == dep)
                    self.assertLess(dep_task["wave"], wave_index)

    def test_plan_is_deterministic(self):
        first = wp.plan(self.manifest(), access_check=ok_access)
        second = wp.plan(self.manifest(), access_check=ok_access)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_manifest_objectives_are_strictly_validated(self):
        task = {"id": "a1", "repository": "alpha", "title": "Adapter", "objective": "Implement alpha only."}
        self.assertEqual(wp.plan(self.manifest(tasks=[task]), access_check=ok_access)["tasks"][0]["objective"],
                         "Implement alpha only.")
        for value in ("", "one\ntwo", "x" * 4001, "ghp_" + "x" * 40):
            task["objective"] = value
            with self.subTest(value=value[:20]):
                self.assertTrue(wp.validate_manifest(wp.load_manifest(self.manifest(tasks=[task]))))

    def test_readiness_defaults_to_planned_and_roots_are_dispatchable(self):
        result = wp.plan(self.manifest(), access_check=ok_access)
        by_id = {t["id"]: t for t in result["tasks"]}
        self.assertTrue(all(t["readiness"] == "planned" for t in result["tasks"]))
        self.assertEqual({t: by_id[t]["blocked_by"] for t in by_id},
                         {"a1": [], "b1": [], "g1": ["a1", "b1"], "g2": ["g1"]})
        self.assertEqual({t: by_id[t]["dispatchable"] for t in by_id},
                         {"a1": True, "b1": True, "g1": False, "g2": False})
        self.assertEqual(by_id["g1"]["blocks"], ["g2"])
        self.assertEqual(result["summary"], {
            "tasks": 4, "dispatchable": ["a1", "b1"],
            "readiness": {"planned": 4, "implemented": 0, "validated": 0, "ready_for_downstream": 0, "merged": 0}})

    def test_implemented_or_validated_dependency_still_blocks(self):
        tasks = [
            {"id": "a1", "repository": "alpha", "title": "x", "readiness": "implemented"},
            {"id": "b1", "repository": "beta", "title": "x", "readiness": "validated"},
            {"id": "c1", "repository": "gamma", "title": "x", "readiness": "ready_for_downstream"},
            {"id": "d1", "repository": "alpha", "title": "x", "readiness": "merged"},
            {"id": "g1", "repository": "gamma", "title": "x", "depends_on": ["d1", "c1", "b1", "a1"]},
            {"id": "g2", "repository": "gamma", "title": "x", "depends_on": ["d1", "c1"]},
            {"id": "g3", "repository": "gamma", "title": "x", "depends_on": ["c1"], "readiness": "implemented"},
        ]
        result = wp.plan(self.manifest(tasks=tasks), access_check=ok_access)
        self.assertEqual(result["status"], "PLANNED", result["blockers"])
        by_id = {t["id"]: t for t in result["tasks"]}
        # A PR existing (implemented) or passing tests (validated) is not sufficient for downstream.
        self.assertEqual(by_id["g1"]["blocked_by"], ["a1", "b1"])
        self.assertFalse(by_id["g1"]["dispatchable"])
        # ready_for_downstream and merged dependencies do not block.
        self.assertEqual(by_id["g2"]["blocked_by"], [])
        self.assertTrue(by_id["g2"]["dispatchable"])
        # An unblocked task that is no longer planned is not dispatchable again.
        self.assertEqual(by_id["g3"]["blocked_by"], [])
        self.assertFalse(by_id["g3"]["dispatchable"])
        # Non-planned roots are never dispatchable.
        self.assertFalse(any(by_id[t]["dispatchable"] for t in ("a1", "b1", "c1", "d1")))
        self.assertEqual(result["summary"]["dispatchable"], ["g2"])
        self.assertEqual(result["summary"]["readiness"],
                         {"planned": 2, "implemented": 2, "validated": 1, "ready_for_downstream": 1, "merged": 1})
        # Readiness does not alter waves: wave order is structural.
        self.assertEqual(result["waves"], [["a1", "b1", "c1", "d1"], ["g1", "g2", "g3"]])

    def test_tasks_on_a_cycle_are_never_dispatchable(self):
        tasks = [
            {"id": "a1", "repository": "alpha", "title": "x", "depends_on": ["b1"]},
            {"id": "b1", "repository": "beta", "title": "x", "depends_on": ["a1"], "readiness": "merged"},
        ]
        result = wp.plan(self.manifest(tasks=tasks), access_check=ok_access)
        by_id = {t["id"]: t for t in result["tasks"]}
        self.assertEqual(by_id["a1"]["blocked_by"], [])
        self.assertFalse(by_id["a1"]["dispatchable"])
        self.assertEqual(result["summary"]["dispatchable"], [])

    def test_invalid_readiness_is_rejected(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "x", "readiness": "done"}]
        with self.assertRaises(wp.WorkspaceError) as ctx:
            wp.plan(self.manifest(tasks=tasks), access_check=ok_access)
        self.assertIn("readiness", str(ctx.exception))

    def test_readiness_plan_is_deterministic_regardless_of_manifest_order(self):
        tasks = [
            {"id": "g1", "repository": "gamma", "title": "x", "depends_on": ["b1", "a1"]},
            {"id": "b1", "repository": "beta", "title": "x", "readiness": "implemented"},
            {"id": "a1", "repository": "alpha", "title": "x", "readiness": "merged"},
        ]
        first = wp.plan(self.manifest(tasks=tasks), access_check=ok_access)
        second = wp.plan(self.manifest(tasks=list(reversed(tasks))), access_check=ok_access)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        g1 = next(t for t in first["tasks"] if t["id"] == "g1")
        self.assertEqual(g1["blocked_by"], ["b1"])
        self.assertEqual(first["summary"]["dispatchable"], [])
        self.assertEqual([t["id"] for t in first["tasks"]], ["a1", "b1", "g1"])

    def test_unknown_fields_and_branch_pins_are_rejected(self):
        with self.assertRaises(wp.WorkspaceError) as ctx:
            wp.plan(self.manifest(extra="field"), access_check=ok_access)
        self.assertIn("extra", str(ctx.exception))
        repos = [{"id": "alpha", "repository": "acme/alpha", "path": "alpha", "pinned_commit": "main"}]
        tasks = [{"id": "a1", "repository": "alpha", "title": "x"}]
        with self.assertRaises(wp.WorkspaceError) as ctx:
            wp.plan(self.manifest(repositories=repos, tasks=tasks), access_check=ok_access)
        self.assertIn("pinned_commit", str(ctx.exception))

    def test_identity_absent_from_remotes_fails_closed(self):
        repos = [{"id": "alpha", "repository": "acme/other", "path": "alpha", "pinned_commit": self.alpha_sha}]
        tasks = [{"id": "a1", "repository": "alpha", "title": "x"}]
        result = wp.plan(self.manifest(repositories=repos, tasks=tasks), access_check=ok_access)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["blockers"][0]["code"], "TARGET_REPOSITORY_CONFLICT")

    def test_ambiguous_checkout_fails_closed(self):
        git("-C", str(self.alpha), "remote", "add", "upstream", "git@github.com:other/alpha.git")
        repos = [{"id": "alpha", "repository": "acme/alpha", "path": "alpha", "pinned_commit": self.alpha_sha}]
        tasks = [{"id": "a1", "repository": "alpha", "title": "x"}]
        # Explicit identity present among ambiguous remotes resolves; absent identity conflicts.
        result = wp.plan(self.manifest(repositories=repos, tasks=tasks), access_check=ok_access)
        self.assertEqual(result["status"], "PLANNED")
        repos[0]["repository"] = "third/alpha"
        result = wp.plan(self.manifest(repositories=repos, tasks=tasks), access_check=ok_access)
        self.assertEqual(result["blockers"][0]["code"], "TARGET_REPOSITORY_CONFLICT")

    def test_duplicate_repository_fails_closed(self):
        linked = self.base / "alpha-linked"
        git("-C", str(self.alpha), "worktree", "add", "-q", str(linked), "-b", "linked")
        repos = [
            {"id": "alpha", "repository": "acme/alpha", "path": "alpha", "pinned_commit": self.alpha_sha},
            {"id": "alpha2", "repository": "acme/alpha", "path": "alpha-linked", "pinned_commit": self.alpha_sha},
        ]
        tasks = [{"id": "a1", "repository": "alpha", "title": "x"}]
        result = wp.plan(self.manifest(repositories=repos, tasks=tasks), access_check=ok_access)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual([b["code"] for b in result["blockers"]], ["WORKSPACE_REPOSITORY_DUPLICATE"])
        self.assertEqual(result["blockers"][0]["id"], "alpha2")

    def test_missing_path_and_missing_commit_fail_closed(self):
        repos = [
            {"id": "alpha", "repository": "acme/alpha", "path": "nowhere", "pinned_commit": self.alpha_sha},
            {"id": "beta", "repository": "acme/beta", "path": "beta", "pinned_commit": "f" * 40},
        ]
        tasks = [{"id": "a1", "repository": "alpha", "title": "x"}]
        result = wp.plan(self.manifest(repositories=repos, tasks=tasks), access_check=ok_access)
        self.assertEqual([b["code"] for b in result["blockers"]],
                         ["WORKSPACE_REPOSITORY_PATH_MISSING", "WORKSPACE_PIN_MISSING"])

    def test_access_denied_blocks_but_other_repositories_still_resolve(self):
        def denied(repository: str) -> dict:
            if repository == "acme/beta":
                return {"status": "denied", "blocker_code": "WORKSPACE_REPOSITORY_ACCESS_DENIED", "detail": "404"}
            return ok_access(repository)
        result = wp.plan(self.manifest(), access_check=denied)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual([b["id"] for b in result["blockers"]], ["beta"])
        self.assertEqual(result["waves"], [["a1", "b1"], ["g1"], ["g2"]])

    def test_skipped_access_check_is_reported(self):
        result = wp.plan(self.manifest(), access_check=None)
        self.assertEqual(result["access_check"], "skipped")
        self.assertTrue(all(r["access"]["status"] == "skipped" for r in result["repositories"]))

    def test_dependency_graph_errors(self):
        tasks = [
            {"id": "a1", "repository": "alpha", "title": "x", "depends_on": ["b1"]},
            {"id": "b1", "repository": "beta", "title": "x", "depends_on": ["a1"]},
            {"id": "c1", "repository": "nope", "title": "x", "depends_on": ["c1", "ghost"]},
            {"id": "d1", "repository": "gamma", "title": "x"},
        ]
        result = wp.plan(self.manifest(tasks=tasks), access_check=ok_access)
        codes = sorted(b["code"] for b in result["blockers"])
        self.assertEqual(codes, ["WORKSPACE_DEPENDENCY_CYCLE", "WORKSPACE_DEPENDENCY_SELF",
                                 "WORKSPACE_DEPENDENCY_UNKNOWN", "WORKSPACE_TASK_REPOSITORY_UNKNOWN"])
        cycle = next(b for b in result["blockers"] if b["code"] == "WORKSPACE_DEPENDENCY_CYCLE")
        self.assertIn("a1, b1", cycle["detail"])
        # Tasks outside the cycle are still planned.
        self.assertEqual(result["waves"], [["c1", "d1"]])

    def test_cli_exit_codes_and_json(self):
        def run(args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = wp.main(args)
            return rc, out.getvalue(), err.getvalue()
        path = self.manifest()
        rc, out, _ = run([str(path), "--no-access-check", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "PLANNED")
        rc, out, _ = run([str(path), "--no-access-check"])
        self.assertEqual(rc, 0)
        self.assertIn("wave 1: a1 [alpha] planned dispatchable, b1 [beta] planned dispatchable", out)
        self.assertIn("wave 2: g1 [gamma] planned blocked_by=a1+b1", out)
        self.assertIn("Readiness: planned=4, implemented=0, validated=0, ready_for_downstream=0, merged=0", out)
        self.assertIn("Dispatchable: a1, b1", out)
        self.assertIn("No worker, worktree, Git or GitHub mutation", out)
        bad = self.manifest(tasks=[{"id": "x", "repository": "zzz", "title": "x"}])
        rc, _, _ = run([str(bad), "--no-access-check"])
        self.assertEqual(rc, 1)
        invalid = self.base / "invalid.yaml"
        invalid.write_text("schema_version: 2\n")
        rc, out, _ = run([str(invalid), "--no-access-check", "--json"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["blocker_code"], "WORKSPACE_MANIFEST_INVALID")
        rc, _, err = run([str(self.base / "absent.yaml"), "--no-access-check"])
        self.assertEqual(rc, 2)
        self.assertIn("WORKSPACE_MANIFEST_INVALID", err)

    def test_cli_overlays_observed_state_like_the_scheduler(self):
        import workspace_run
        state = Path(self._tmp.name) / "state"
        with unittest.mock.patch.dict(os.environ, {"PI_ORCHESTRATOR_STATE_DIR": str(state)}):
            path = self.manifest()
            record_path = workspace_run.workspace_record_path("ws-test")
            record_path.parent.mkdir(parents=True)
            record_path.write_text(json.dumps({"record_version": 1, "workspace_id": "ws-test",
                "evidence_bindings": workspace_run.evidence_bindings(path),
                "evidence_pins": workspace_run.evidence_pins(path), "tasks": {
                "a1": {"observed": {"readiness": "ready_for_downstream"}},
                "b1": {"observed": {"readiness": "merged"}}}}))
            def run(args):
                out = io.StringIO()
                with redirect_stdout(out):
                    wp.main(args)
                return json.loads(out.getvalue())
            overlaid = {t["id"]: t for t in run([str(path), "--no-access-check", "--json"])["tasks"]}
            self.assertEqual(overlaid["a1"]["readiness"], "ready_for_downstream")
            self.assertTrue(overlaid["g1"]["dispatchable"])
            declared = {t["id"]: t for t in run([str(path), "--no-access-check", "--json", "--declared-only"])["tasks"]}
            self.assertEqual(declared["a1"]["readiness"], "planned")
            self.assertEqual(declared["g1"]["blocked_by"], ["a1", "b1"])

    def test_gh_access_uses_explicit_repository(self):
        calls = []
        with unittest.mock.patch.object(wp.gh, "gh_available", return_value=True), \
             unittest.mock.patch.object(wp.gh, "require_authenticated"), \
             unittest.mock.patch.object(wp.gh, "repo_view",
                                        side_effect=lambda **kw: (calls.append(kw), {"nameWithOwner": "acme/alpha", "isPrivate": True})[1]):
            self.assertEqual(wp.gh_access("acme/alpha")["status"], "ok")
            self.assertEqual(calls, [{"repo": "acme/alpha"}])
            self.assertEqual(wp.gh_access("acme/beta")["blocker_code"], "WORKSPACE_REPOSITORY_ACCESS_DENIED")
        with unittest.mock.patch.object(wp.gh, "gh_available", return_value=False):
            self.assertEqual(wp.gh_access("acme/alpha")["blocker_code"], "GITHUB_ACCESS_UNVERIFIED")
        with unittest.mock.patch.object(wp.gh, "gh_available", return_value=True), \
             unittest.mock.patch.object(wp.gh, "require_authenticated", side_effect=wp.gh.GhAuthError("no")):
            self.assertEqual(wp.gh_access("acme/alpha")["blocker_code"], "GITHUB_ACCESS_UNVERIFIED")



if __name__ == "__main__":
    unittest.main()
