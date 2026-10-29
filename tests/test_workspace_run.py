#!/usr/bin/env python3
"""Deterministic tests for the repository-scoped child launcher on real Git repositories."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import headless_run as hr  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402
import workspace_run as wr  # noqa: E402
from test_headless_run import FAKE_PI, git, stage_mutation_policy  # noqa: E402


def ok_access(repository: str) -> dict:
    return {"status": "ok", "blocker_code": None, "detail": None, "private": True}


class WorkspaceRunFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repos = {}
        for name in ("alpha", "beta", "gamma"):
            path = self.base / name
            path.mkdir()
            git("-C", str(path), "init", "-q")
            git("-C", str(path), "remote", "add", "origin", f"git@github.com:acme/{name}.git")
            (path / "README.md").write_text(f"# {name}\n")
            git("-C", str(path), "add", "README.md")
            stage_mutation_policy(path)
            git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
            self.repos[name] = (path, git("-C", str(path), "rev-parse", "HEAD"))
        self.state = self.base / "state"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.pi = self.bin / "pi"
        self.pi.write_text(FAKE_PI)
        self.pi.chmod(0o755)
        self._env = dict(os.environ)
        os.environ[provenance.STATE_ENV] = str(self.state)
        os.environ["FAKE_PI_TOOLS"] = str(REPO_ROOT / "tools")
        os.environ["PI_CODING_AGENT_DIR"] = str(self.base / "agent")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def manifest(self, tasks=None, pins=None) -> Path:
        pins = pins or {}
        data = {"schema_version": 1, "id": "ws", "goal": "Cross-repo goal", "max_parallel": 3,
                "repositories": [{"id": n, "repository": f"acme/{n}", "path": n,
                                  "pinned_commit": pins.get(n, sha)} for n, (_, sha) in self.repos.items()],
                "tasks": tasks or [{"id": "a1", "repository": "alpha", "title": "Alpha adapter"},
                                   {"id": "b1", "repository": "beta", "title": "Beta extraction"},
                                   {"id": "g1", "repository": "gamma", "title": "Gamma identity"}]}
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def launch(self, manifest, task, **kwargs):
        kwargs.setdefault("allow_mutation", True)
        return wr.launch(manifest=manifest, task_id=task, access_check=ok_access, pi_executable=str(self.pi), **kwargs)

    def snapshots(self):
        return {n: session_scope.snapshot(str(p))["digest"] for n, (p, _) in self.repos.items()}


class WorkspaceRunTest(WorkspaceRunFixture):
    def test_three_children_run_isolated_and_are_linked(self):
        manifest = self.manifest()
        before = self.snapshots()
        results = {t: self.launch(manifest, t) for t in ("a1", "b1", "g1")}
        self.assertEqual(self.snapshots(), before)
        run_ids = set()
        for task, result in results.items():
            entry = result["task"]
            self.assertEqual(entry["run_status"], "PARTIAL", entry)
            self.assertEqual(entry["execution_id"], f"ws--{task}")
            self.assertTrue(entry["run_id"].startswith("run-"))
            run_ids.add(entry["run_id"])
            record = provenance.get_run(REPO_ROOT, entry["run_id"])
            self.assertEqual(record["parent_orchestration"]["target_repository"], entry["target_repository"])
        self.assertEqual(len(run_ids), 3)
        self.assertEqual({r["task"]["target_repository"] for r in results.values()}, {"acme/alpha", "acme/beta", "acme/gamma"})
        workspace = json.loads(Path(results["a1"]["workspace_record"]).read_text())
        self.assertEqual(sorted(workspace["tasks"]), ["a1", "b1", "g1"])
        self.assertEqual(workspace["tasks"]["b1"]["run_id"], results["b1"]["task"]["run_id"])
        self.assertNotIn('"objective":', json.dumps(workspace))
        self.assertNotIn("prompt", json.dumps(workspace))
        # Each execution is bound to its own repository's state key.
        for name, (path, _) in self.repos.items():
            for other in self.repos:
                if other != name:
                    with self.assertRaises(hr.HeadlessError) as ctx:
                        hr.status(target=path, explicit_repo=f"acme/{name}", execution_id=f"ws--{other[0]}1")
                    self.assertEqual(ctx.exception.code, "EXECUTION_UNKNOWN")

    def test_a_dirty_checkout_refuses_the_launch_and_names_the_run_that_left_it(self):
        manifest = self.manifest(tasks=[{"id": "a1", "repository": "alpha", "title": "First"},
                                        {"id": "a2", "repository": "alpha", "title": "Second"}])
        alpha = self.repos["alpha"][0]
        # Untracked files (PICO's worktrees, scratch files) do not count.
        (alpha / "notes.txt").write_text("untracked\n")
        os.environ["FAKE_PI_MESS"] = "parent-apply"
        first = self.launch(manifest, "a1")
        self.assertEqual((first["task"]["checkout"]["dirty_files"], first["task"]["checkout"]["dirty_count"]), (["README.md"], 1))
        os.environ.pop("FAKE_PI_MESS")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(manifest, "a2")
        self.assertEqual(ctx.exception.code, "WORKSPACE_CHECKOUT_DIRTY")
        self.assertIn("README.md", ctx.exception.detail)
        self.assertIn(f"left by a1 ({first['task']['run_id']})", ctx.exception.detail)
        self.assertNotIn("a2", json.loads(Path(first["workspace_record"]).read_text())["tasks"])  # nothing started
        # A repeat of a finished launch still answers (no child would start), and nothing was cleaned.
        self.assertEqual(self.launch(manifest, "a1")["task"]["outcome"], "already_finished")
        self.assertIn("applied in the parent checkout", (alpha / "README.md").read_text())
        # Staged changes count too; once the checkout is clean, the task launches.
        git("-C", str(alpha), "checkout", "--", "README.md")
        (alpha / "README.md").write_text("staged\n")
        git("-C", str(alpha), "add", "README.md")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(manifest, "a2")
        self.assertEqual(ctx.exception.code, "WORKSPACE_CHECKOUT_DIRTY")
        git("-C", str(alpha), "reset", "-q", "--hard")
        self.assertEqual(self.launch(manifest, "a2")["task"]["run_status"], "PARTIAL")

    def test_new_untracked_files_are_reported_on_the_task_and_do_not_block_the_next_launch(self):
        manifest = self.manifest(tasks=[{"id": "a1", "repository": "alpha", "title": "First"},
                                        {"id": "a2", "repository": "alpha", "title": "Second"}])
        os.environ["FAKE_PI_MESS"] = "parent-new-file"
        first = self.launch(manifest, "a1")
        self.assertEqual((first["task"]["checkout"]["new_untracked"], first["task"]["checkout"]["new_untracked_count"]),
                         (["generated/"], 1))
        os.environ.pop("FAKE_PI_MESS")
        self.assertEqual(self.launch(manifest, "a2")["task"]["run_status"], "PARTIAL")

    def test_relaunch_is_idempotent_and_crash_resumes_same_run(self):
        manifest = self.manifest()
        first = self.launch(manifest, "a1")
        again = self.launch(manifest, "a1")
        self.assertEqual(again["task"]["outcome"], "already_finished")
        self.assertEqual(again["task"]["launches"], 1)
        self.assertEqual(again["task"]["run_id"], first["task"]["run_id"])
        os.environ["FAKE_PI_BEHAVIOR"] = "crash"
        crashed = self.launch(manifest, "b1")
        self.assertEqual(crashed["task"]["execution_status"], "interrupted")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        blocked = self.launch(manifest, "b1")
        self.assertEqual(blocked["task"]["outcome"], "resume_required")
        # The crashed child's lease is stale: resume without an explicit takeover is refused.
        with self.assertRaises(wr.WorkspaceRunError) as refused:
            self.launch(manifest, "b1", resume=True)
        self.assertEqual(refused.exception.code, "RUN_LEASE_STALE")
        resumed = self.launch(manifest, "b1", resume=True, takeover_stale_lease=True)
        self.assertEqual(resumed["task"]["run_id"], crashed["task"]["run_id"])
        self.assertEqual(resumed["task"]["run_status"], "PARTIAL")
        self.assertEqual(resumed["task"]["launches"], 2)
        self.assertEqual(len(list((self.state / "runs").glob("run-*.json"))), 2)

    def test_scoped_objective_is_immutable_and_excludes_workspace_goal(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "Alpha adapter",
                  "objective": "Implement the alpha adapter using only this checkout."},
                 {"id": "b1", "repository": "beta", "title": "Private beta title"}]
        manifest = self.manifest(tasks)
        result = self.launch(manifest, "a1")
        execution = hr.status(target=self.repos["alpha"][0], explicit_repo="acme/alpha", execution_id="ws--a1")
        self.assertIn(tasks[0]["objective"], execution["objective"])
        self.assertNotIn("Private beta title", execution["objective"])
        self.assertNotIn("Cross-repo goal", execution["objective"])
        self.assertEqual(result["task"]["objective_sha256"], __import__("hashlib").sha256(execution["objective"].encode()).hexdigest())
        tasks[0]["objective"] = "Change the alpha adapter behavior."
        self.manifest(tasks)
        with self.assertRaises(wr.WorkspaceRunError) as rejected:
            self.launch(manifest, "a1")
        self.assertEqual(rejected.exception.code, "EXECUTION_OBJECTIVE_MISMATCH")
        self.assertEqual(self.launch(self.manifest(tasks=[{"id": "new", "repository": "alpha", "title": "Changed",
                                                   "objective": tasks[0]["objective"]}]), "new")["task"]["execution_id"], "ws--new")

    def test_legacy_execution_resumes_with_its_original_objective(self):
        manifest = self.manifest(tasks=[{"id": "a1", "repository": "alpha", "title": "Alpha adapter"}])
        plan = wr._plan(manifest, access_check=ok_access)
        task, repo = wr._task(plan, "a1")
        inputs = wr.dispatch_inputs(plan, task, repo)
        legacy = wr.objective_for(plan, task, repo, inputs).replace(
            "Repository-scoped objective: Alpha adapter\n", "Workspace goal: Cross-repo goal\n", 1)
        os.environ["FAKE_PI_BEHAVIOR"] = "crash"
        hr.start(target=self.repos["alpha"][0], explicit_repo="acme/alpha", execution_id="ws--a1",
                 objective=legacy, pi_executable=str(self.pi), allow_mutation=True)
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        resumed = self.launch(manifest, "a1", resume=True, takeover_stale_lease=True)
        self.assertEqual(resumed["task"]["launches"], 2)
        saved = hr.status(target=self.repos["alpha"][0], explicit_repo="acme/alpha", execution_id="ws--a1")
        self.assertEqual(saved["objective"], legacy)

    def test_blocked_task_is_refused_until_upstream_is_ready(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "Adapter"},
                 {"id": "b1", "repository": "beta", "title": "Consumer", "depends_on": ["a1"]}]
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(tasks), "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_BLOCKED")
        self.assertIn("a1", ctx.exception.detail)
        self.assertFalse((self.state / "runs").exists())
        tasks[0]["readiness"] = "implemented"
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(tasks), "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_BLOCKED")
        tasks[0]["readiness"] = "ready_for_downstream"
        result = self.launch(self.manifest(tasks), "b1")
        self.assertEqual(result["task"]["run_status"], "PARTIAL")

    def test_pin_mismatch_and_repository_blockers_fail_closed(self):
        path, _ = self.repos["alpha"]
        (path / "extra.txt").write_text("x\n")
        git("-C", str(path), "add", "extra.txt")
        git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "more")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(), "a1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_PIN_MISMATCH")
        self.assertFalse((self.state / "runs").exists())
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(pins={"beta": "f" * 40}), "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_PIN_MISSING")
        git("-C", str(self.repos["gamma"][0]), "remote", "set-url", "origin", "git@github.com:other/gamma.git")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(), "g1")
        self.assertEqual(ctx.exception.code, "TARGET_REPOSITORY_CONFLICT")
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(self.manifest(), "zz")
        self.assertEqual(ctx.exception.code, "WORKSPACE_TASK_UNKNOWN")
        self.assertFalse((self.state / "runs").exists())

    def test_cancel_and_status(self):
        manifest = self.manifest()
        os.environ["FAKE_PI_BEHAVIOR"] = "hang"
        launched = self.launch(manifest, "g1", wait=False)
        self.assertEqual(launched["task"]["execution_status"], "launched")
        deadline = time.monotonic() + 5
        while wr.status(manifest=manifest, task_id="g1")["tasks"]["g1"]["run_id"] is None and time.monotonic() < deadline:
            time.sleep(0.05)
        cancelled = wr.cancel(manifest=manifest, task_id="g1")
        self.assertEqual(cancelled["task"]["execution_status"], "cancelled")
        self.assertEqual(cancelled["task"]["run_blocker_code"], "EXECUTION_CANCELLED")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        self.launch(manifest, "a1")
        overall = wr.status(manifest=manifest)
        self.assertEqual(overall["tasks"]["a1"]["run_status"], "PARTIAL")
        self.assertEqual(overall["tasks"]["g1"]["execution_status"], "cancelled")
        self.assertNotIn("b1", overall["tasks"])

    def budget_manifest(self, budget):
        path = self.manifest()
        data = yaml.safe_load(path.read_text())
        if budget is not None:
            data["budget"] = budget
        path.write_text(yaml.safe_dump(data))
        return path

    def test_direct_launch_is_refused_when_budget_exhausted(self):
        os.environ["FAKE_PI_USAGE"] = "600,500"
        manifest = self.budget_manifest({"max_tokens": 1000})
        self.launch(manifest, "a1")  # the first child spends past the limit
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(manifest, "b1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_BUDGET_EXHAUSTED")
        self.assertIn("--ignore-budget", ctx.exception.detail)
        self.assertIn("1000", ctx.exception.detail)
        self.assertNotIn("b1", wr.status(manifest=manifest, access_check=None)["tasks"])

    def test_direct_launch_counts_the_spend_of_a_running_child(self):
        os.environ["FAKE_PI_USAGE"] = "3000,0"
        os.environ["FAKE_PI_BEHAVIOR"] = "hang"
        manifest = self.budget_manifest({"max_tokens": 2000})
        self.launch(manifest, "a1", wait=False)  # still running: the workspace record has no run id for it
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                plan = wr._plan(manifest, access_check=None)
                running, _ = wr.running_tasks(plan, exclude={"b1"})
                state = wr.budget_state(plan, running=running)
                if state["spent_tokens"] >= 3000:
                    break
                time.sleep(0.1)
            self.assertEqual(running, {"a1"})
            self.assertEqual(state["spent_tokens"], 3000)
            # A record whose pid now belongs to another process (pid reuse) is not a running child.
            record_path = hr.execution_path(Path(next(r for r in plan["repositories"] if r["id"] == "alpha")["git_common_dir"]),
                                           "ws--a1")
            record = json.loads(record_path.read_text())
            if record.get("pid_start") is not None:
                record_path.write_text(json.dumps({**record, "pid_start": "1"}))
                self.assertEqual(wr.running_tasks(plan, exclude={"b1"})[0], set())
                record_path.write_text(json.dumps(record))
            with self.assertRaises(wr.WorkspaceRunError) as ctx:
                self.launch(manifest, "b1")
            self.assertEqual(ctx.exception.code, "WORKSPACE_BUDGET_EXHAUSTED")
        finally:
            wr.cancel(manifest=manifest, task_id="a1")

    def exec_path(self, manifest, task):
        repo = {"a1": "alpha", "b1": "beta", "g1": "gamma"}[task]
        repos = {r["id"]: r for r in wr._plan(manifest, access_check=None)["repositories"]}
        return hr.execution_path(Path(repos[repo]["git_common_dir"]), f"ws--{task}")

    def test_corrupt_record_of_a_finished_task_does_not_block_launch(self):
        manifest = self.budget_manifest({"max_tokens": 1000000})
        self.launch(manifest, "a1")
        self.exec_path(manifest, "a1").write_text("{not json")
        self.assertEqual(self.launch(manifest, "b1")["task"]["run_status"], "PARTIAL")

    def test_corrupt_record_of_a_possibly_running_task_refuses_with_path_and_remedy(self):
        manifest = self.budget_manifest({"max_tokens": 1000000})
        broken = self.exec_path(manifest, "g1")
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json")  # g1 has no finished entry: it might be running
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(manifest, "a1")
        self.assertEqual(ctx.exception.code, "WORKSPACE_BUDGET_UNKNOWN")
        self.assertIn(str(broken), ctx.exception.detail)
        self.assertIn("repair or delete", ctx.exception.detail)
        self.assertEqual(self.launch(manifest, "a1", ignore_budget=True)["task"]["run_status"], "PARTIAL")

    def test_garden_budget_matches_the_direct_launch_gate(self):
        import watch
        manifest = self.budget_manifest({"max_tokens": 1000000})
        broken = self.exec_path(manifest, "g1")
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json")
        plan = wr._plan(manifest, access_check=None)
        state = watch._budget(plan, {"tasks": {}}, self.state / "runs")
        self.assertTrue(state["incomplete"])
        self.assertIn(str(broken), state["incomplete_reasons"][0])

    def test_repeat_launch_is_a_noop_even_when_budget_is_exhausted(self):
        os.environ["FAKE_PI_USAGE"] = "600,500"
        manifest = self.budget_manifest({"max_tokens": 1000})
        first = self.launch(manifest, "a1")
        for kwargs in ({}, {"ignore_budget": True}, {"resume": True}):
            again = self.launch(manifest, "a1", **kwargs)
            self.assertEqual(again["task"]["outcome"], "already_finished")
            self.assertEqual(again["task"]["launches"], 1)
            self.assertEqual(again["task"]["run_id"], first["task"]["run_id"])
            self.assertNotIn("budget_override", again["task"])

    def test_resume_of_interrupted_task_is_gated(self):
        os.environ["FAKE_PI_USAGE"] = "600,500"
        plain = self.manifest()
        self.launch(plain, "a1")
        os.environ["FAKE_PI_BEHAVIOR"] = "crash"
        self.assertEqual(self.launch(plain, "b1")["task"]["execution_status"], "interrupted")
        os.environ["FAKE_PI_BEHAVIOR"] = "finish"
        manifest = self.budget_manifest({"max_tokens": 1000})
        with self.assertRaises(wr.WorkspaceRunError) as ctx:
            self.launch(manifest, "b1", resume=True)
        self.assertEqual(ctx.exception.code, "WORKSPACE_BUDGET_EXHAUSTED")
        # The budget refused first, so the crashed child's lease is still stale: recover it explicitly.
        resumed = self.launch(manifest, "b1", resume=True, ignore_budget=True, takeover_stale_lease=True)
        self.assertEqual(resumed["task"]["outcome"], "resumed")
        self.assertIn("budget_override", resumed["task"])

    def test_budget_checked_skips_the_gate_and_records_no_override(self):
        os.environ["FAKE_PI_USAGE"] = "600,500"
        manifest = self.budget_manifest({"max_tokens": 1000})
        self.launch(manifest, "a1")
        result = self.launch(manifest, "b1", budget_checked=True)
        self.assertEqual(result["task"]["outcome"], "launched")
        self.assertNotIn("budget_override", result["task"])

    def test_ignore_budget_launches_and_records_the_override(self):
        os.environ["FAKE_PI_USAGE"] = "600,500"
        manifest = self.budget_manifest({"max_tokens": 1000})
        first = self.launch(manifest, "a1")
        self.assertNotIn("budget_override", first["task"])
        result = self.launch(manifest, "b1", ignore_budget=True)
        override = result["task"]["budget_override"]
        self.assertEqual(override["reasons"], ["tokens"])
        self.assertEqual(override["max_tokens"], 1000)
        self.assertTrue(override["at"])
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        self.assertIn("budget_override", record["tasks"]["b1"])

    def test_launch_allowed_without_budget_or_under_budget(self):
        plain = self.launch(self.manifest(), "a1")
        self.assertNotIn("budget_override", plain["task"])
        roomy = self.launch(self.budget_manifest({"max_tokens": 1000000}), "b1")
        self.assertNotIn("budget_override", roomy["task"])

    def test_cli_ignore_budget(self):
        os.environ["FAKE_PI_USAGE"] = "600,500"
        manifest = self.budget_manifest({"max_tokens": 1000})
        self.launch(manifest, "a1")
        def run(args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = wr.main(args)
            return rc, json.loads(out.getvalue())
        base = ["launch", str(manifest), "--task", "b1", "--no-access-check", "--allow-mutation", "--pi", str(self.pi)]
        rc, data = run(base)
        self.assertEqual((rc, data["error_code"]), (1, "WORKSPACE_BUDGET_EXHAUSTED"))
        rc, data = run(base + ["--ignore-budget"])
        self.assertEqual(rc, 0, data)
        self.assertIn("budget_override", data["task"])

    def test_cli(self):
        manifest = self.manifest()
        def run(args):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = wr.main(args)
            return rc, json.loads(out.getvalue())
        rc, data = run(["launch", str(manifest), "--task", "a1", "--no-access-check", "--allow-mutation", "--pi", str(self.pi)])
        self.assertEqual(rc, 0, data)
        self.assertEqual(data["task"]["run_status"], "PARTIAL")
        rc, data = run(["status", str(manifest), "--no-access-check"])
        self.assertEqual(rc, 0)
        self.assertIn("a1", data["tasks"])
        rc, data = run(["launch", str(manifest), "--task", "nope", "--no-access-check", "--pi", str(self.pi)])
        self.assertEqual(rc, 1)
        self.assertEqual(data["error_code"], "WORKSPACE_TASK_UNKNOWN")



class WorkspaceTimeLimitTest(WorkspaceRunFixture):
    """Manifest default, per-task override and enforcement through the workspace status path."""

    def setUp(self):
        super().setUp()
        self.clock = [2_000_000.0]
        patch = unittest.mock.patch.object(hr, "clock", lambda: self.clock[0])
        patch.start()
        self.addCleanup(patch.stop)
        boot = unittest.mock.patch.object(hr, "boot_elapsed", lambda record: None)  # the fake wall clock drives these
        boot.start()
        self.addCleanup(boot.stop)
        os.environ["FAKE_PI_BEHAVIOR"] = "hang"

    def limited_manifest(self, default=None, overrides=None):
        path = self.manifest()
        data = yaml.safe_load(path.read_text())
        if default is not None:
            data["max_duration_seconds"] = default
        for task in data["tasks"]:
            if task["id"] in (overrides or {}):
                task["max_duration_seconds"] = overrides[task["id"]]
        path.write_text(yaml.safe_dump(data))
        return path

    def reap_all(self, *results):
        for result in results:
            record = hr.load_record(hr.execution_path(
                Path(session_scope.common_dir(self.repos[result["task"]["repository"]][0])), result["task"]["execution_id"]))
            self.addCleanup(self.kill, record["pid"])
            # Let the child create its parent run, so the stop has a run to settle.
            deadline = time.monotonic() + 5
            while hr.discover_run_id(Path(record["session_dir"]), record["session_id"]) is None and time.monotonic() < deadline:
                time.sleep(0.05)

    @staticmethod
    def kill(pid):
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        # SIGKILL is delivered asynchronously: wait until the whole group is gone,
        # so no grandchild still writes into the state directory during cleanup.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.02)

    def test_no_limit_in_the_manifest_leaves_the_child_unlimited(self):
        manifest = self.limited_manifest()
        a1 = self.launch(manifest, "a1", wait=False)
        self.reap_all(a1)
        self.assertNotIn("max_duration_seconds", a1["task"])
        self.assertNotIn("deadline_epoch", a1["task"])
        self.clock[0] += 10 ** 7
        status = wr.status(manifest=manifest)["tasks"]["a1"]
        self.assertEqual(status["execution_status"], "launched")
        self.assertIsNone(wr.time_limit_view(status))

    def test_manifest_default_applies_and_a_task_overrides_it(self):
        manifest = self.limited_manifest(default=300, overrides={"b1": 45})
        a1 = self.launch(manifest, "a1", wait=False)
        b1 = self.launch(manifest, "b1", wait=False)
        self.reap_all(a1, b1)
        self.assertEqual(a1["task"]["max_duration_seconds"], 300)
        self.assertEqual(b1["task"]["max_duration_seconds"], 45)
        self.assertEqual(b1["task"]["deadline_epoch"], self.clock[0] + 45)
        self.assertEqual(wr.time_limit_view(b1["task"])["remaining_seconds"], 45)

    def test_a_task_limit_applies_without_a_manifest_default(self):
        manifest = self.limited_manifest(overrides={"g1": 20})
        g1 = self.launch(manifest, "g1", wait=False)
        self.reap_all(g1)
        self.assertEqual(g1["task"]["max_duration_seconds"], 20)

    def test_invalid_limits_violate_the_manifest_schema(self):
        for bad in (0, "10", 1.5):
            manifest = self.limited_manifest(default=bad)
            with self.assertRaises(wr.WorkspaceRunError):
                self.launch(manifest, "a1", wait=False)
            manifest = self.limited_manifest(overrides={"a1": bad})
            with self.assertRaises(wr.WorkspaceRunError):
                self.launch(manifest, "a1", wait=False)

    def test_status_stops_the_child_at_its_deadline_and_the_run_is_not_left_open(self):
        manifest = self.limited_manifest(overrides={"a1": 60})
        a1 = self.launch(manifest, "a1", wait=False)
        self.reap_all(a1)
        self.clock[0] += 59
        self.assertEqual(wr.status(manifest=manifest, task_id="a1")["tasks"]["a1"]["execution_status"], "launched")
        self.clock[0] += 1
        entry = wr.status(manifest=manifest, task_id="a1")["tasks"]["a1"]
        self.assertEqual((entry["execution_status"], entry["outcome"]), ("cancelled", "time_limit"))
        self.assertEqual(entry["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertEqual((entry["run_status"], entry["run_blocker_code"]), ("BLOCKED", "EXECUTION_TIME_LIMIT"))
        view = wr.time_limit_view(entry)
        self.assertEqual((view["stop_reason"], view["elapsed_seconds"], view["remaining_seconds"]),
                         ("EXECUTION_TIME_LIMIT", 60.0, None))

    def test_relaunch_after_a_time_limit_stop_does_not_restart_the_child(self):
        manifest = self.limited_manifest(overrides={"a1": 5})
        self.reap_all(self.launch(manifest, "a1", wait=False))
        self.clock[0] += 5
        wr.status(manifest=manifest, task_id="a1")
        for resume in (False, True):
            again = self.launch(manifest, "a1", wait=False, resume=resume, takeover_stale_lease=resume)
            self.assertEqual(again["task"]["outcome"], "already_finished")
            self.assertEqual(again["task"]["stop_reason"], "EXECUTION_TIME_LIMIT")
            self.assertEqual(again["task"]["launches"], 1)


if __name__ == "__main__":
    unittest.main()
