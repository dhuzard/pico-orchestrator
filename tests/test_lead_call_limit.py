#!/usr/bin/env python3
"""Deterministic tests for the lead-call limit (issue #47).

Recorder level: the lifecycle extension's `lead_limit`, `lead_call_warning` and `lead_call_limit_reached`
events, the resumable stop (run stays IN_PROGRESS, fresh checkpoint, lease released by its owner) and the
resume that keeps the count. Launcher level: configuration precedence, validation, the interrupted
execution with `stop_reason: LEAD_CALL_LIMIT`, and the refusal of a resume with an exhausted limit.
The extension's own enforcement is covered by tests/orchestration-lifecycle.test.ts.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import autonomy_policy  # noqa: E402
import headless_run as hr  # noqa: E402
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import run_lease  # noqa: E402
import session_scope  # noqa: E402
import workspace_scheduler  # noqa: E402
from test_headless_run import FAKE_PI, FakeClock, HeadlessFixture, git  # noqa: E402
from test_run_lease import StateTestCase  # noqa: E402
from test_workspace_run import WorkspaceRunFixture, wr  # noqa: E402
import yaml  # noqa: E402

MODEL = "openai-codex/gpt-6.1-sol"


def lead_calls(parent: dict) -> int:
    return len(parent["context"]["lead_calls"])


class RecorderTest(StateTestCase):
    def setUp(self):
        super().setUp()
        self.owner = "owner-a"
        self.run_id = self.start_run(self.owner)

    def event(self, payload: dict, owner: str | None = None) -> dict:
        return po.record_event(self.run_id, payload, owner_id=owner or self.owner, repo_root=self.records)

    def calls(self, n: int) -> None:
        for _ in range(n):
            self.event({"kind": "lead_usage", "model": MODEL})

    def limit(self, maximum=4, source="task", warn_at=3) -> dict:
        payload = {"kind": "lead_limit", "state": "set", "max_lead_calls": maximum, "source": source}
        if warn_at is not None:
            payload["warn_at"] = warn_at
        self.event(payload)
        return self.parent(self.run_id)["context"]["lead_call_limit"]

    def test_no_limit_records_nothing_extra(self):
        self.calls(3)
        self.assertNotIn("lead_call_limit", self.parent(self.run_id)["context"])

    def test_limit_source_and_warning_are_recorded_and_validated(self):
        recorded = self.limit(10, "manifest", 8)
        self.assertEqual((recorded["max_lead_calls"], recorded["source"], recorded["warn_at"]), (10, "manifest", 8))
        self.assertEqual((recorded["stopped"], recorded["stopped_reason"], recorded["calls_at_limit"]), (False, None, None))
        for bad in ({"max_lead_calls": 0, "source": "cli"}, {"max_lead_calls": True, "source": "cli"},
                    {"max_lead_calls": 5, "source": "bogus"}):
            with self.assertRaises(po.ParentRunError) as refused:
                self.event({"kind": "lead_limit", "state": "set", **bad})
            self.assertEqual(refused.exception.error_code, "MAX_LEAD_CALLS_INVALID")
        for warn in (0, 5, 6):
            with self.assertRaises(po.ParentRunError) as refused:
                self.event({"kind": "lead_limit", "state": "set", "max_lead_calls": 5, "source": "cli", "warn_at": warn})
            self.assertEqual(refused.exception.error_code, "LEAD_CALLS_WARN_AT_INVALID")

    def test_warning_and_reached_record_the_call_count_once(self):
        self.limit(4, "cli", 3)
        self.calls(3)
        self.event({"kind": "lead_call_warning"})
        self.calls(1)
        self.event({"kind": "lead_limit", "state": "reached"})
        self.calls(1)
        self.event({"kind": "lead_call_warning"})
        self.event({"kind": "lead_limit", "state": "reached"})
        recorded = self.parent(self.run_id)["context"]["lead_call_limit"]
        self.assertEqual((recorded["warned_at_call"], recorded["calls_at_limit"]), (3, 4))
        self.assertFalse(recorded["stopped"])

    def test_events_need_a_limit_and_a_lead_cannot_forge_them_without_the_lease(self):
        for payload in ({"kind": "lead_call_warning"}, {"kind": "lead_call_limit_reached"},
                        {"kind": "lead_limit", "state": "reached"}):
            with self.assertRaises(po.ParentRunError):
                self.event(payload)
        self.limit()
        with self.assertRaises(po.ParentRunError) as other:
            self.event({"kind": "lead_call_limit_reached"}, owner="intruder")
        self.assertTrue(other.exception.error_code.startswith("RUN_LEASE_"))
        self.assertFalse(self.parent(self.run_id)["context"]["lead_call_limit"]["stopped"])

    def test_the_stop_leaves_the_run_in_progress_with_a_fresh_checkpoint_and_a_released_lease(self):
        self.limit(2, "env", 1)
        before = self.parent(self.run_id)["checkpoint"]
        self.calls(4)
        self.event({"kind": "lead_call_limit_reached"})
        run = provenance.get_run(self.records, self.run_id)
        parent = run["parent_orchestration"]
        self.assertEqual((run["outcome"], parent["final_status"], parent["blocker_code"]), ("in_progress", "IN_PROGRESS", None))
        limit = parent["context"]["lead_call_limit"]
        self.assertEqual((limit["stopped"], limit["stopped_reason"], limit["calls_at_stop"], limit["calls_at_limit"]),
                         (True, "LEAD_CALL_LIMIT", 4, 2))
        self.assertEqual(parent["checkpoint"]["reason"], "lead_call_limit")
        self.assertEqual(parent["checkpoint"]["sequence"], before["sequence"] + 1)
        released = run_lease.release(self.run_id, self.owner, repo_root=self.records)
        self.assertTrue(released["released"])
        self.assertIsNone(run_lease.status(self.run_id, repo_root=self.records)["lease"])
        # Nobody can write the stopped run without taking the lease: the old owner is the recorded owner only.
        with self.assertRaises(po.ParentRunError):
            self.event({"kind": "lead_usage", "model": MODEL}, owner="intruder")

    def test_resume_verifies_the_checkpoint_keeps_the_count_and_records_the_new_limit(self):
        self.limit(2, "task", 1)
        self.calls(4)
        self.event({"kind": "lead_call_limit_reached"})
        run_lease.release(self.run_id, self.owner, repo_root=self.records)
        resumed = po.resume(self.run_id, worktree=self.target, owner_id="owner-b", pid=os.getpid(),
                            explicit_repo=po.github_integration.resolve_target(self.target, "Examplecorp/examplelab"),
                            repo_root=self.records)
        self.assertEqual(resumed["status"], "IN_PROGRESS")
        self.assertEqual(resumed["checkpoint"]["reason"], "lead_call_limit")
        recorded = self.limit_as("owner-b", 10, "cli")
        self.assertEqual(lead_calls(self.parent(self.run_id)), 4, "the call count carries over")
        self.assertEqual((recorded["max_lead_calls"], recorded["source"]), (10, "cli"))
        self.assertEqual((recorded["stopped"], recorded["stopped_reason"], recorded["calls_at_limit"]), (False, None, None))
        self.assertEqual(recorded["calls_at_stop"], 4, "the earlier stop stays on the record")

    def limit_as(self, owner: str, maximum: int, source: str) -> dict:
        self.event({"kind": "lead_limit", "state": "set", "max_lead_calls": maximum, "source": source}, owner=owner)
        return self.parent(self.run_id)["context"]["lead_call_limit"]

    def test_resume_is_refused_when_the_checkout_moved_after_the_stop(self):
        self.limit(2, "task", 1)
        self.calls(4)
        self.event({"kind": "lead_call_limit_reached"})
        run_lease.release(self.run_id, self.owner, repo_root=self.records)
        self.commit("moved on")
        with self.assertRaises(po.ParentRunError) as refused:
            po.resume(self.run_id, worktree=self.target, owner_id="owner-b", pid=os.getpid(),
                      explicit_repo="Examplecorp/examplelab", repo_root=self.records)
        self.assertEqual(refused.exception.error_code, "RESUME_CHECKPOINT_DRIFT")
        self.assertEqual(self.parent(self.run_id)["final_status"], "IN_PROGRESS")

    def test_a_lead_finish_after_the_warning_is_reconciled_as_usual(self):
        self.limit(5, "cli", 4)
        self.event({"kind": "phase", "phase": "t1", "status": "in_progress"})
        done = po.finish(self.run_id, "PARTIAL", owner_id=self.owner, repo_root=self.records)
        self.assertEqual(done["outcome"], "partial")
        phase = self.parent(self.run_id)["phases"][0]
        self.assertEqual(phase["status"], "abandoned")

    def test_a_finished_run_refuses_the_stop_event(self):
        # The time limit (or an operator) may end the run before the lifecycle's stop lands: the stop must not resurrect it.
        self.limit(2, "task", 1)
        po.finish(self.run_id, "BLOCKED", blocker_code="EXECUTION_TIME_LIMIT", owner_id=self.owner, repo_root=self.records)
        try:
            self.event({"kind": "lead_call_limit_reached"})
        except (po.ParentRunError, provenance.ProvenanceError, OSError):
            pass
        run = provenance.get_run(self.records, self.run_id)
        self.assertEqual((run["outcome"], run["parent_orchestration"]["blocker_code"]), ("blocked", "EXECUTION_TIME_LIMIT"))


class LeadLimitConfigurationTest(unittest.TestCase):
    def test_validation_uses_stable_codes(self):
        for bad in (0, -1, True, 1.5, "3"):
            with self.assertRaises(hr.HeadlessError) as refused:
                hr.validate_max_lead_calls(bad)
            self.assertEqual(refused.exception.code, "MAX_LEAD_CALLS_INVALID")
        with self.assertRaises(hr.HeadlessError) as refused:
            hr.validate_max_lead_calls(5, "policy")
        self.assertEqual(refused.exception.code, "MAX_LEAD_CALLS_INVALID")
        for bad in (0, True, "x"):
            with self.assertRaises(hr.HeadlessError) as refused:
                hr.validate_lead_calls_warn_at(bad)
            self.assertEqual(refused.exception.code, "LEAD_CALLS_WARN_AT_INVALID")
        self.assertEqual(hr.validate_max_lead_calls(None), (None, None))
        self.assertEqual(hr.validate_max_lead_calls(7, "task"), (7, "task"))

    def policy(self, directory: Path, **limits) -> None:
        schema = json.loads((ROOT / "schemas" / "autonomy.schema.json").read_text())
        permissions = {name: False for name in schema["properties"]["permissions"]["required"]}
        permissions["merge"] = {"enabled": False, "max_failure_cost": "low", "require_ci": True,
                                "require_review_if_policy_demands": True}
        (directory / ".pi").mkdir()
        import yaml
        (directory / ".pi" / "autonomy.yaml").write_text(yaml.safe_dump({
            "profile": "manual", "permissions": permissions,
            "limits": {"parallel_tasks": 3, "nested_agents_per_task": 1, "repair_attempts": 1, "replans": 1, **limits}}))

    def test_precedence_is_explicit_then_policy_and_no_limit_means_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp)
            self.assertEqual(hr.resolve_lead_limit(plain, None, None, None), (None, None, None))
            self.assertEqual(hr.resolve_lead_limit(plain, 9, "cli", None), (9, "cli", None))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.policy(directory, max_lead_calls=30, lead_calls_warn_at=20)
            self.assertEqual(autonomy_policy.limits(directory)["max_lead_calls"], 30)
            self.assertEqual(hr.resolve_lead_limit(directory, None, None, None), (30, "policy", 20))
            self.assertEqual(hr.resolve_lead_limit(directory, 25, "manifest", 22), (25, "manifest", 22))
            self.assertEqual(hr.resolve_lead_limit(directory, 25, "cli", None), (25, "cli", 20))
            # A policy threshold at or above a lower explicit limit is not inherited (the 80% default applies).
            self.assertEqual(hr.resolve_lead_limit(directory, 12, "task", None), (12, "task", None))
            with self.assertRaises(hr.HeadlessError) as refused:
                hr.resolve_lead_limit(directory, 15, "cli", 15)
            self.assertEqual(refused.exception.code, "LEAD_CALLS_WARN_AT_INVALID")

    def test_policy_schema_rejects_a_bad_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.policy(directory, max_lead_calls=0)
            with self.assertRaises(autonomy_policy.AutonomyPolicyError):
                autonomy_policy.limits(directory)

    def test_scheduler_text_shows_calls_against_the_limit(self):
        self.assertEqual(workspace_scheduler.lead_calls_text(None), "")
        self.assertEqual(workspace_scheduler.lead_calls_text({"max_lead_calls": 30, "calls": 18}), " (lead calls 18/30)")
        self.assertEqual(workspace_scheduler.lead_calls_text({"max_lead_calls": 30, "calls": 32, "stopped": True}),
                         " (lead calls 32/30, stopped by LEAD_CALL_LIMIT)")


LEAD_FAKE = r'''elif behavior in ("leadlimit", "leadstop", "leadstop_hang"):
    def pev(payload):
        subprocess.run([sys.executable, parent, "event", run_id, "--owner", owner], input=json.dumps(payload),
                       capture_output=True, text=True)
    configured = {"kind": "lead_limit", "state": "set", "max_lead_calls": int(os.environ["PI_ORCHESTRATION_MAX_LEAD_CALLS"]),
                  "source": os.environ.get("PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE", "env")}
    if os.environ.get("PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"):
        configured["warn_at"] = int(os.environ["PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"])
    pev(configured)
    for _ in range(int(os.environ.get("FAKE_PI_LEAD_CALLS", "0"))):
        pev({"kind": "lead_usage", "model": "openai-codex/gpt-6.1-sol"})
    if behavior == "leadlimit":
        pcmd("finish", run_id, "PARTIAL", "--owner", owner)
    else:
        pev({"kind": "lead_call_limit_reached"})
        subprocess.run([sys.executable, os.path.join(tools, "run_lease.py"), "release", run_id, "--owner", owner],
                       capture_output=True, text=True)
        if behavior == "leadstop_hang":
            time.sleep(60)
elif behavior == "crash":'''


class LeadLimitExecutionTest(HeadlessFixture):
    def setUp(self):
        super().setUp()
        self.pi.write_text(FAKE_PI.replace('elif behavior == "crash":', LEAD_FAKE, 1))
        os.environ["FAKE_PI_LEAD_CALLS"] = "6"

    def stopped(self, execution_id="exec-1", maximum=4, **kwargs):
        return self.start(execution_id=execution_id, behavior="leadstop", max_lead_calls=maximum,
                          max_lead_calls_source="task", **kwargs)

    def test_the_limit_reaches_the_child_and_the_run_record(self):
        record = self.start(behavior="leadlimit", max_lead_calls=20, max_lead_calls_source="manifest", lead_calls_warn_at=15)
        self.assertEqual((record["max_lead_calls"], record["max_lead_calls_source"], record["lead_calls_warn_at"]),
                         (20, "manifest", 15))
        limit = record["run"]["lead_call_limit"]
        self.assertEqual((limit["max_lead_calls"], limit["source"], limit["warn_at"], limit["calls"], limit["stopped"]),
                         (20, "manifest", 15, 6, False))

    def test_no_limit_leaves_the_environment_and_the_record_alone(self):
        os.environ["PI_ORCHESTRATION_MAX_LEAD_CALLS"] = "3"  # an inherited value must not leak into a limit-free launch
        record = self.start(behavior="finish")
        self.assertNotIn("max_lead_calls", record)
        self.assertNotIn("lead_call_limit", record["run"])
        self.assertEqual(record["run"]["status"], "PARTIAL")

    def test_the_stop_ends_the_execution_interrupted_with_the_reason_and_leaves_the_run_resumable(self):
        record = self.stopped()
        self.assertEqual((record["status"], record["stop_reason"]), ("interrupted", "LEAD_CALL_LIMIT"))
        self.assertEqual(record["run"]["status"], "IN_PROGRESS")
        limit = record["run"]["lead_call_limit"]
        self.assertEqual((limit["stopped"], limit["calls"], limit["max_lead_calls"]), (True, 6, 4))
        parent = provenance.get_run(hr.ROOT, record["run_id"])["parent_orchestration"]
        self.assertEqual(parent["context"]["lead_call_limit"]["stopped_reason"], "LEAD_CALL_LIMIT")
        self.assertEqual(parent["checkpoint"]["reason"], "lead_call_limit")
        self.assertIsNone(run_lease.status(record["run_id"])["lease"], "the lease is released at the stop")
        self.assertEqual(self.status()["stop_reason"], "LEAD_CALL_LIMIT")

    def test_resume_with_the_same_or_a_lower_limit_is_refused_without_launching(self):
        first = self.stopped()
        for attempt in (4, 6):
            with self.assertRaises(hr.HeadlessError) as refused:
                self.start(behavior="leadlimit", resume=True, max_lead_calls=attempt)
            self.assertEqual(refused.exception.code, "LEAD_CALL_LIMIT_EXHAUSTED")
        self.assertEqual(self.status()["launches"], 1)
        self.assertEqual(self.status()["run"]["status"], "IN_PROGRESS")
        self.assertEqual(self.status()["run_id"], first["run_id"])

    def test_resume_with_a_raised_limit_continues_the_same_run_and_keeps_the_count(self):
        first = self.stopped()
        os.environ["FAKE_PI_LEAD_CALLS"] = "1"
        resumed = self.start(behavior="leadlimit", resume=True, max_lead_calls=12)
        self.assertEqual(resumed["run_id"], first["run_id"])
        self.assertEqual(resumed["launches"], 2)
        self.assertEqual(resumed["run"]["status"], "PARTIAL")
        self.assertEqual((resumed["max_lead_calls"], resumed["max_lead_calls_source"]), (12, "cli"))
        parent = provenance.get_run(hr.ROOT, first["run_id"])["parent_orchestration"]
        self.assertEqual(lead_calls(parent), 7, "6 calls before the stop plus 1 after: the count never resets")
        limit = parent["context"]["lead_call_limit"]
        self.assertEqual((limit["max_lead_calls"], limit["source"], limit["stopped"]), (12, "cli", False))
        self.assertNotIn("stop_reason", resumed)

    def test_resume_verifies_the_checkpoint(self):
        self.stopped()
        git("-C", str(self.target), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
            "--allow-empty", "-m", "moved")
        os.environ["FAKE_PI_LEAD_CALLS"] = "1"
        refused = self.start(behavior="leadlimit", resume=True, max_lead_calls=12)
        self.assertEqual(refused["exit_code"], 4)
        self.assertEqual(refused["run"]["status"], "IN_PROGRESS")
        self.assertIn("RESUME_CHECKPOINT_DRIFT", Path(refused["log_path"]).read_text())

    def test_a_lower_policy_limit_cannot_resume_either(self):
        self.stopped()
        with self.assertRaises(hr.HeadlessError) as refused:
            self.start(behavior="leadlimit", resume=True, max_lead_calls=5)
        self.assertEqual(refused.exception.code, "LEAD_CALL_LIMIT_EXHAUSTED")

    def test_invalid_limits_launch_nothing(self):
        for kwargs, code in (({"max_lead_calls": 0}, "MAX_LEAD_CALLS_INVALID"),
                             ({"max_lead_calls": 5, "lead_calls_warn_at": 5}, "LEAD_CALLS_WARN_AT_INVALID"),
                             ({"max_lead_calls": 5, "lead_calls_warn_at": 0}, "LEAD_CALLS_WARN_AT_INVALID")):
            with self.assertRaises(hr.HeadlessError) as refused:
                self.start(behavior="leadlimit", **kwargs)
            self.assertEqual(refused.exception.code, code)
        self.assertEqual(list((self.state / "executions").rglob("*.json")), [])


class LeadLimitAndTimeLimitTest(HeadlessFixture):
    """Both limits can trigger; the later, terminal one wins deterministically and nothing is double-finished."""

    def setUp(self):
        super().setUp()
        self.pi.write_text(FAKE_PI.replace('elif behavior == "crash":', LEAD_FAKE, 1))
        os.environ["FAKE_PI_LEAD_CALLS"] = "6"
        self.clock = FakeClock()
        for target, value in ((hr, "clock"), (hr, "boot_elapsed")):
            patch = unittest.mock.patch.object(target, value, self.clock if value == "clock" else (lambda record: None))
            patch.start()
            self.addCleanup(patch.stop)

    def reap(self, pid):
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def launch(self, **kwargs):
        launched = self.start(behavior="leadstop_hang", wait=False, **kwargs)
        self.addCleanup(self.reap, launched["pid"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = (self.status().get("run") or {}).get("lead_call_limit") or {}
            if run.get("stopped"):
                break
            time.sleep(0.05)
        return launched

    def test_the_lead_stop_comes_first_then_the_deadline_ends_the_run_blocked_by_the_time_limit(self):
        self.launch(max_lead_calls=4, max_duration_seconds=100)
        record = self.status()
        self.assertEqual(record["run"]["status"], "IN_PROGRESS")
        self.assertEqual(record["status"], "launched")  # still alive, but the lead stopped and released the lease
        self.clock.now += 100
        record = self.status()
        self.assertEqual((record["status"], record["stop_reason"]), ("cancelled", "EXECUTION_TIME_LIMIT"))
        self.assertEqual((record["run"]["status"], record["run"]["blocker_code"]), ("BLOCKED", "EXECUTION_TIME_LIMIT"))

    def test_a_run_already_ended_by_the_time_limit_is_not_reopened_by_the_lead_stop(self):
        launched = self.start(behavior="hang", wait=False, max_lead_calls=4, max_duration_seconds=50)
        self.addCleanup(self.reap, launched["pid"])
        deadline = time.monotonic() + 10
        while hr.discover_run_id(Path(launched["session_dir"]), launched["session_id"]) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.clock.now += 50
        record = self.status()
        self.assertEqual(record["stop_reason"], "EXECUTION_TIME_LIMIT")
        run = provenance.get_run(hr.ROOT, record["run_id"])
        self.assertEqual(run["parent_orchestration"]["blocker_code"], "EXECUTION_TIME_LIMIT")
        # Resume cannot revive it, whatever the lead-call limit says.
        again = self.start(behavior="leadlimit", resume=True, max_lead_calls=100)
        self.assertEqual(again["outcome"], "already_finished")
        self.assertEqual(again["stop_reason"], "EXECUTION_TIME_LIMIT")


class MetricsTest(unittest.TestCase):
    def test_the_report_carries_the_limit_only_when_the_run_has_one(self):
        import benchmark_metrics
        limit = {"max_lead_calls": 30, "source": "task", "warn_at": 24, "warned_at_call": 24, "calls_at_limit": 30,
                 "stopped": True, "stopped_reason": "LEAD_CALL_LIMIT", "calls_at_stop": 32}
        context = {"lead_calls": [{}] * 32, "lead_call_limit": limit}
        with_limit = benchmark_metrics.run_report({"run_id": "run-x", "parent_orchestration": {"context": context}})
        self.assertEqual(with_limit["calls"]["lead"], 32)
        self.assertEqual(with_limit["calls"]["lead_limit"]["max_lead_calls"], 30)
        self.assertTrue(with_limit["calls"]["lead_limit"]["stopped"])
        without = benchmark_metrics.run_report({"run_id": "run-y", "parent_orchestration": {"context": {"lead_calls": [{}]}}})
        self.assertNotIn("lead_limit", without["calls"])


class WorkspaceLeadLimitTest(WorkspaceRunFixture):
    """Task, manifest and invalid configuration through `workspace_run.launch` (the repository policy is tested above)."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_PI_BEHAVIOR"] = "hang"

    def manifest_with(self, default=None, warn=None, overrides=None):
        path = self.manifest()
        data = yaml.safe_load(path.read_text())
        if default is not None:
            data["max_lead_calls"] = default
        if warn is not None:
            data["lead_calls_warn_at"] = warn
        for task in data["tasks"]:
            task.update((overrides or {}).get(task["id"], {}))
        path.write_text(yaml.safe_dump(data))
        return path

    def launched(self, manifest, task):
        result = self.launch(manifest, task, wait=False)
        record = hr.load_record(hr.execution_path(
            Path(session_scope.common_dir(self.repos[result["task"]["repository"]][0])), result["task"]["execution_id"]))
        self.addCleanup(self.kill, record["pid"])
        return result["task"]

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

    def test_no_limit_in_the_manifest_means_none(self):
        entry = self.launched(self.manifest_with(), "a1")
        self.assertNotIn("max_lead_calls", entry)

    def test_manifest_default_applies_and_a_task_overrides_it_with_its_source(self):
        manifest = self.manifest_with(default=40, warn=30, overrides={"b1": {"max_lead_calls": 12, "lead_calls_warn_at": 9}})
        a1 = self.launched(manifest, "a1")
        b1 = self.launched(manifest, "b1")
        self.assertEqual((a1["max_lead_calls"], a1["max_lead_calls_source"], a1["lead_calls_warn_at"]), (40, "manifest", 30))
        self.assertEqual((b1["max_lead_calls"], b1["max_lead_calls_source"], b1["lead_calls_warn_at"]), (12, "task", 9))

    def test_a_task_limit_applies_without_a_manifest_default(self):
        entry = self.launched(self.manifest_with(overrides={"g1": {"max_lead_calls": 7}}), "g1")
        self.assertEqual((entry["max_lead_calls"], entry["max_lead_calls_source"]), (7, "task"))

    def test_invalid_values_violate_the_manifest_schema(self):
        for bad in (0, "10", 1.5, -3):
            for manifest in (self.manifest_with(default=bad), self.manifest_with(overrides={"a1": {"max_lead_calls": bad}}),
                             self.manifest_with(warn=bad)):
                with self.assertRaises(wr.WorkspaceRunError):
                    self.launch(manifest, "a1", wait=False)

    def test_a_warning_at_or_above_the_limit_is_refused_before_launch(self):
        with self.assertRaises(wr.WorkspaceRunError) as refused:
            self.launch(self.manifest_with(default=10, warn=10), "a1", wait=False)
        self.assertEqual(refused.exception.code, "LEAD_CALLS_WARN_AT_INVALID")


if __name__ == "__main__":
    unittest.main()
