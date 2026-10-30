#!/usr/bin/env python3
"""Deterministic tests for the cross-repository scheduler on real Git repositories."""
from __future__ import annotations

import io
import json
import os
import subprocess
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

import provenance  # noqa: E402
import session_scope  # noqa: E402
import workspace_run as wr  # noqa: E402
import workspace_scheduler as ws  # noqa: E402
from test_headless_run import FAKE_PI, git, stage_mutation_policy  # noqa: E402

NAMES = ("alpha", "beta", "gamma", "delta")


def ok_access(repository: str) -> dict:
    return {"status": "ok", "blocker_code": None, "detail": None, "private": True}


class SchedulerFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repos = {}
        for name in NAMES:
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
        self.log = self.base / "fake.log"
        self._env = dict(os.environ)
        os.environ[provenance.STATE_ENV] = str(self.state)
        os.environ["FAKE_PI_TOOLS"] = str(REPO_ROOT / "tools")
        os.environ["PI_CODING_AGENT_DIR"] = str(self.base / "agent")
        os.environ["FAKE_PI_BEHAVIOR"] = "slow"
        os.environ["FAKE_PI_LOG"] = str(self.log)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def manifest(self, tasks, max_parallel=4) -> Path:
        data = {"schema_version": 1, "id": "ws", "goal": "Cross-repo goal", "max_parallel": max_parallel,
                "repositories": [{"id": n, "repository": f"acme/{n}", "path": n, "pinned_commit": sha}
                                 for n, (_, sha) in self.repos.items()],
                "tasks": tasks}
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        return path

    def schedule(self, manifest, **kwargs):
        kwargs.setdefault("allow_mutation", True)
        return ws.run(manifest, access_check=ok_access, pi_executable=str(self.pi), poll_seconds=0.05, **kwargs)

    def overlap(self) -> tuple[int, dict]:
        """Peak concurrent children overall, and per repository."""
        events = sorted(line.split() for line in self.log.read_text().splitlines())
        active, peak, per_repo, repo_peak = 0, 0, {}, {}
        for _, event, repo in events:
            delta = 1 if event == "start" else -1
            active += delta
            per_repo[repo] = per_repo.get(repo, 0) + delta
            peak = max(peak, active)
            repo_peak[repo] = max(repo_peak.get(repo, 0), per_repo[repo])
        return peak, repo_peak

    def states(self, result) -> dict:
        return {row["id"]: row["state"] for row in result["tasks"]}

    def one_per_repo(self):
        return [{"id": f"{n[0]}1", "repository": n, "title": f"{n} task"} for n in NAMES]


class SchedulerTest(SchedulerFixture):
    def test_concurrency_cap_is_respected_and_all_complete(self):
        before = {n: session_scope.snapshot(str(p))["digest"] for n, (p, _) in self.repos.items()}
        result = self.schedule(self.manifest(self.one_per_repo(), max_parallel=4), max_parallel=2)
        self.assertEqual(result["cap"], 2)
        self.assertEqual(result["status"], "OK", result)
        self.assertEqual(set(self.states(result).values()), {"partial"})
        peak, _ = self.overlap()
        self.assertEqual(peak, 2)
        self.assertEqual(sorted(result["session"]["dispatched"]), ["a1", "b1", "d1", "g1"])
        after = {n: session_scope.snapshot(str(p))["digest"] for n, (p, _) in self.repos.items()}
        self.assertEqual(after, before)

    def test_cli_cap_can_only_lower_the_manifest_limit(self):
        result = self.schedule(self.manifest(self.one_per_repo(), max_parallel=1), max_parallel=8)
        self.assertEqual(result["cap"], 1)
        self.assertEqual(self.overlap()[0], 1)

    def test_tasks_in_one_repository_never_overlap(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "x"}, {"id": "a2", "repository": "alpha", "title": "y"},
                 {"id": "b1", "repository": "beta", "title": "z"}]
        result = self.schedule(self.manifest(tasks, max_parallel=3))
        self.assertEqual(result["status"], "OK")
        peak, repo_peak = self.overlap()
        self.assertEqual(repo_peak["alpha"], 1)
        self.assertEqual(peak, 2)

    def test_failed_dependency_blocks_dependents_but_not_independent_work(self):
        os.environ["FAKE_PI_BEHAVIOR_alpha"] = "settle"  # alpha's child ends BLOCKED
        tasks = [{"id": "a1", "repository": "alpha", "title": "Upstream that fails"},
                 {"id": "b1", "repository": "beta", "title": "Independent"},
                 {"id": "g1", "repository": "gamma", "title": "Downstream of a1", "depends_on": ["a1"]},
                 {"id": "d1", "repository": "delta", "title": "Downstream of g1", "depends_on": ["g1"]},
                 {"id": "d2", "repository": "delta", "title": "Downstream of b1", "depends_on": ["b1"]}]
        result = self.schedule(self.manifest(tasks))
        states = self.states(result)
        self.assertEqual(states, {"a1": "failed", "b1": "partial", "g1": "blocked_by_failure",
                                  "d1": "blocked_by_failure", "d2": "waiting_for_readiness"})
        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual(sorted(result["session"]["dispatched"]), ["a1", "b1"])
        row = next(r for r in result["tasks"] if r["id"] == "a1")
        self.assertEqual(row["run_blocker_code"], "ORCHESTRATION_OUTCOME_UNRECORDED")

    def test_completed_upstream_does_not_unblock_until_declared_ready(self):
        tasks = [{"id": "a1", "repository": "alpha", "title": "Upstream"},
                 {"id": "b1", "repository": "beta", "title": "Downstream", "depends_on": ["a1"]}]
        first = self.schedule(self.manifest(tasks))
        self.assertEqual(self.states(first), {"a1": "partial", "b1": "waiting_for_readiness"})
        tasks[0]["readiness"] = "ready_for_downstream"
        second = self.schedule(self.manifest(tasks))
        self.assertEqual(second["session"]["dispatched"], ["b1"])
        self.assertEqual(self.states(second), {"a1": "partial", "b1": "partial"})
        record = json.loads(wr.workspace_record_path("ws").read_text())
        upstream = record["tasks"]["b1"]["inputs"]["upstream"]
        self.assertEqual(upstream, [{"task": "a1", "repository": "acme/alpha",
                                     "pinned_commit": self.repos["alpha"][1], "readiness": "ready_for_downstream"}])
        self.assertEqual(len(record["scheduler_sessions"]), 2)

    def test_restart_never_relaunches_recorded_tasks(self):
        manifest = self.manifest(self.one_per_repo())
        first = self.schedule(manifest)
        runs_before = sorted(p.name for p in (self.state / "runs").glob("run-*.json"))
        second = self.schedule(manifest)
        self.assertEqual(second["session"]["dispatched"], [])
        self.assertEqual(sorted(p.name for p in (self.state / "runs").glob("run-*.json")), runs_before)
        self.assertEqual([r["run_id"] for r in first["tasks"]], [r["run_id"] for r in second["tasks"]])
        self.assertTrue(all(r["launches"] == 1 for r in second["tasks"]))

    def test_restart_observes_a_child_left_running_by_a_previous_scheduler(self):
        manifest = self.manifest(self.one_per_repo())
        os.environ["FAKE_PI_SLEEP"] = "1.0"
        # A previous scheduler launched a1 and died; the child outlives it.
        subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "workspace_run.py"), "launch", str(manifest),
                        "--task", "a1", "--no-wait", "--no-access-check", "--allow-mutation", "--pi", str(self.pi)],
                       check=True, capture_output=True, env=os.environ.copy())
        os.environ["FAKE_PI_SLEEP"] = "0.2"
        result = self.schedule(manifest)
        self.assertEqual(result["session"]["observed_running"], ["a1"])
        self.assertNotIn("a1", result["session"]["dispatched"])
        row = next(r for r in result["tasks"] if r["id"] == "a1")
        self.assertEqual((row["state"], row["launches"]), ("partial", 1))
        self.assertEqual(result["status"], "OK")

    def test_interrupted_child_needs_explicit_reconciliation(self):
        os.environ["FAKE_PI_BEHAVIOR_gamma"] = "crash"
        tasks = self.one_per_repo() + [{"id": "a2", "repository": "alpha", "title": "after g1", "depends_on": ["g1"]}]
        first = self.schedule(self.manifest(tasks))
        states = self.states(first)
        self.assertEqual(states["g1"], "needs_reconciliation")
        self.assertEqual(states["a2"], "blocked_by_failure")
        self.assertEqual(first["status"], "ATTENTION")
        again = self.schedule(self.manifest(tasks))
        self.assertEqual(again["session"]["dispatched"], [])
        self.assertEqual(again["session"]["resumed"], [])
        os.environ["FAKE_PI_BEHAVIOR_gamma"] = "finish"
        resumed = self.schedule(self.manifest(tasks), resume_interrupted=True)
        self.assertEqual(resumed["session"]["resumed"], ["g1"])
        row = next(r for r in resumed["tasks"] if r["id"] == "g1")
        crashed = next(r for r in first["tasks"] if r["id"] == "g1")
        self.assertEqual((row["state"], row["run_id"], row["launches"]), ("partial", crashed["run_id"], 2))
        self.assertEqual(self.states(resumed)["a2"], "waiting_for_readiness")

    def test_refused_dispatch_blocks_dependents_and_is_retried_on_restart(self):
        alpha, _ = self.repos["alpha"]
        (alpha / "moved.txt").write_text("x\n")
        git("-C", str(alpha), "add", "moved.txt")
        git("-C", str(alpha), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "moved")
        tasks = self.one_per_repo() + [{"id": "b2", "repository": "beta", "title": "after a1", "depends_on": ["a1"]}]
        result = self.schedule(self.manifest(tasks))
        states = self.states(result)
        self.assertEqual(states["a1"], "refused")
        self.assertEqual(states["b2"], "blocked_by_failure")
        self.assertEqual(states["b1"], "partial")
        self.assertEqual(result["session"]["refused"], {"a1": "WORKSPACE_PIN_MISMATCH"})
        git("-C", str(alpha), "checkout", "-q", self.repos["alpha"][1])
        retried = self.schedule(self.manifest(tasks))
        self.assertEqual(retried["session"]["dispatched"], ["a1"])
        self.assertEqual(self.states(retried)["a1"], "partial")

    def test_second_scheduler_on_the_same_workspace_is_refused(self):
        manifest = self.manifest(self.one_per_repo())
        with ws.scheduler_lock("ws"):
            with self.assertRaises(ws.SchedulerError) as ctx:
                self.schedule(manifest)
        self.assertEqual(ctx.exception.code, "WORKSPACE_SCHEDULER_ACTIVE")

    def test_budget_holds_new_launches_once_spent(self):
        os.environ["FAKE_PI_USAGE"] = "600,500,0.25"
        data = yaml.safe_load(self.manifest(self.one_per_repo(), max_parallel=1).read_text())
        data["budget"] = {"max_tokens": 2000}
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        result = self.schedule(path)
        states = self.states(result)
        # Each child spends 1100 tokens: after two the budget is reached and the rest are held.
        self.assertEqual(sorted(result["session"]["dispatched"]), ["a1", "b1"])
        self.assertEqual(sorted(t for t, s in states.items() if s == "held_by_budget"), ["d1", "g1"])
        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual((result["budget"]["spent_tokens"], result["budget"]["exhausted"], result["budget"]["reasons"]),
                         (2200, True, ["tokens"]))
        self.assertEqual(result["session"]["held_by_budget"], ["d1", "g1"])
        again = self.schedule(path)
        self.assertEqual(again["session"]["dispatched"], [])
        data["budget"] = {"max_tokens": 10000, "max_cost_usd": 1.0}
        path.write_text(yaml.safe_dump(data))
        third = self.schedule(path)
        # Raised limits let the held tasks run; the reported cost (0.25 per child) then reaches 1.0.
        self.assertEqual(third["budget"]["reasons"], ["cost"])
        self.assertEqual(sorted(third["session"]["dispatched"]), ["d1", "g1"])

    def test_in_flight_spend_holds_the_next_task_while_a_child_is_still_running(self):
        # alpha's child reports 3000 tokens at once and keeps running; beta's child spends nothing and
        # exits first. The workspace record has no run id for alpha yet, but its spend must gate gamma.
        os.environ["FAKE_PI_USAGE_alpha"] = "3000,0"
        os.environ["FAKE_PI_USAGE_beta"] = ""
        os.environ["FAKE_PI_SLEEP_alpha"] = "4"
        os.environ["FAKE_PI_SLEEP_beta"] = "1.2"
        tasks = [{"id": "a1", "repository": "alpha", "title": "a"}, {"id": "b1", "repository": "beta", "title": "b"},
                 {"id": "g1", "repository": "gamma", "title": "g"}]
        data = yaml.safe_load(self.manifest(tasks, max_parallel=2).read_text())
        data["budget"] = {"max_tokens": 2000}
        path = self.base / "workspace.yaml"
        path.write_text(yaml.safe_dump(data))
        result = self.schedule(path)
        self.assertEqual(sorted(result["session"]["dispatched"]), ["a1", "b1"])
        self.assertEqual(result["session"]["held_by_budget"], ["g1"])
        self.assertEqual(self.states(result)["g1"], "held_by_budget")
        self.assertTrue(result["budget"]["exhausted"])

    def test_second_task_in_a_repository_launches_after_a_child_moved_the_checkout(self):
        # Real leads left the parent checkout on pico/batch-<n> with worktrees behind; the launcher
        # then refused the next task in that repository (WORKSPACE_PIN_MISMATCH).
        os.environ["FAKE_PI_MESS"] = "clean"
        tasks = [{"id": "a1", "repository": "alpha", "title": "first"}, {"id": "a2", "repository": "alpha", "title": "second"}]
        alpha = self.repos["alpha"][0]
        starting_branch = git("-C", str(alpha), "branch", "--show-current")
        result = self.schedule(self.manifest(tasks, max_parallel=1))
        self.assertEqual(self.states(result), {"a1": "partial", "a2": "partial"}, result)
        self.assertEqual(git("-C", str(alpha), "branch", "--show-current"), starting_branch)
        self.assertEqual(git("-C", str(alpha), "rev-parse", "HEAD"), self.repos["alpha"][1])
        # Cleanup preserves a clean worktree whose ownership cannot be proved;
        # it must not prevent the next task from using the restored checkout.
        self.assertEqual(len(git("-C", str(alpha), "worktree", "list").splitlines()), 2)
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        self.assertTrue(record["tasks"]["a1"]["checkout"]["restored"])
        self.assertTrue(record["tasks"]["a1"]["checkout"]["kept_worktrees"])

    def test_second_task_is_refused_when_the_first_left_the_checkout_dirty(self):
        # The #4 pilot: a lead applied its diff to the parent checkout; the next task in that repository
        # launched on the dirty checkout because only HEAD was checked (#68).
        os.environ["FAKE_PI_MESS"] = "parent-apply"
        tasks = [{"id": "a1", "repository": "alpha", "title": "first"}, {"id": "a2", "repository": "alpha", "title": "second"}]
        result = self.schedule(self.manifest(tasks, max_parallel=1))
        self.assertEqual(self.states(result), {"a1": "partial", "a2": "refused"}, result)
        self.assertEqual(result["session"]["refused"], {"a2": "WORKSPACE_CHECKOUT_DIRTY"})
        record = wr.load_workspace(wr.workspace_record_path("ws"))
        self.assertEqual(record["tasks"]["a1"]["checkout"]["dirty_files"], ["README.md"])
        self.assertNotIn("a2", record["tasks"])

    def test_cli(self):
        manifest = self.manifest(self.one_per_repo()[:2])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ws.main([str(manifest), "--no-access-check", "--allow-mutation", "--json", "--pi", str(self.pi), "--poll-seconds", "0.05"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ws.main([str(manifest), "--no-access-check", "--allow-mutation", "--pi", str(self.pi), "--poll-seconds", "0.05"])
        self.assertEqual(rc, 0)
        self.assertIn("a1 [alpha] partial", out.getvalue())
        bad = self.base / "bad.yaml"
        bad.write_text("schema_version: 9\n")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ws.main([str(bad), "--no-access-check"])
        self.assertEqual(rc, 2)



class SchedulerTimeLimitTest(unittest.TestCase):
    """The scheduler render shows the limit and the time left; its loop observes and enforces the deadline."""

    ENTRY = {"execution_status": "launched", "run_status": "IN_PROGRESS", "max_duration_seconds": 600,
             "deadline_epoch": 1000.0 + 600}

    def view(self, entry, now):
        return wr.time_limit_view(entry, now)

    def test_running_child_shows_limit_and_remaining_time(self):
        view = self.view(self.ENTRY, 1000.0 + 188.6)
        self.assertEqual((view["max_duration_seconds"], view["remaining_seconds"]), (600, 411))
        self.assertEqual(ws.time_limit_text(view), " (limit 600s, 411s left)")

    def test_overdue_child_shows_zero_left(self):
        self.assertEqual(ws.time_limit_text(self.view(self.ENTRY, 5000.0)), " (limit 600s, 0s left)")

    def test_stopped_child_shows_the_reason_and_elapsed_time(self):
        entry = {**self.ENTRY, "execution_status": "cancelled", "stop_reason": "EXECUTION_TIME_LIMIT",
                 "time_limit": {"max_duration_seconds": 600, "elapsed_seconds": 601.2}}
        self.assertEqual(ws.time_limit_text(self.view(entry, 5000.0)), " (stopped after 601.2s of a 600s limit)")

    def test_no_limit_renders_nothing(self):
        self.assertIsNone(self.view({"execution_status": "launched"}, 1.0))
        self.assertEqual(ws.time_limit_text(None), "")
        self.assertIsNone(self.view({"execution_status": "launched", "max_duration_seconds": True}, 1.0))

    def test_render_appends_the_limit_to_the_task_line(self):
        plan = {"tasks": [{"id": "t1", "repository": "alpha", "wave": 0, "readiness": "planned", "blocked_by": [],
                           "dispatchable": False, "depends_on": [], "blocks": []}], "blockers": []}
        entry = {**self.ENTRY, "run_id": "run-x", "launches": 1}
        report = ws.report(plan, {"t1": entry}, {})
        text = ws.render({"workspace_id": "w", "status": report["status"], "cap": 1, "counts": report["counts"],
                          "tasks": report["tasks"]})
        self.assertRegex(text, r"t1 \[alpha\] running run=run-x \(limit 600s, \d+s left\)")


class SchedulerEnforcesTimeLimitTest(SchedulerFixture):
    def test_scheduler_observes_a_child_past_its_deadline_and_it_ends_cancelled(self):
        import headless_run
        clock = [3_000_000.0]
        with unittest.mock.patch.object(headless_run, "clock", lambda: clock[0]), \
                unittest.mock.patch.object(headless_run, "boot_elapsed", lambda record: None):
            os.environ["FAKE_PI_BEHAVIOR"] = "hang"
            manifest = self.manifest([{"id": "a1", "repository": "alpha", "title": "Alpha", "max_duration_seconds": 30}])
            launched = wr.launch(manifest=manifest, task_id="a1", access_check=None, pi_executable=str(self.pi), wait=False,
                                 allow_mutation=True)
            record = headless_run.load_record(headless_run.execution_path(
                session_scope.common_dir(self.repos["alpha"][0]), launched["task"]["execution_id"]))
            self.addCleanup(self._kill, record["pid"])
            deadline = time.monotonic() + 5  # let the child create its parent run before the deadline passes
            while headless_run.discover_run_id(Path(record["session_dir"]), record["session_id"]) is None \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            clock[0] += 31
            result = ws.run(manifest, access_check=None, pi_executable=str(self.pi), poll_seconds=0.1, allow_mutation=True)
        row = result["tasks"][0]
        self.assertEqual((row["state"], row["run_blocker_code"]), ("cancelled", "EXECUTION_TIME_LIMIT"))
        self.assertEqual(row["time_limit"]["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertEqual(result["status"], "ATTENTION")
        self.assertIn("stopped after 31.0s of a 30s limit", ws.render(result))

    @staticmethod
    def _kill(pid):
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


if __name__ == "__main__":
    unittest.main()
