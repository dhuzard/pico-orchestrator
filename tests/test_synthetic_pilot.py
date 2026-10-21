#!/usr/bin/env python3
"""The synthetic pilot as a deterministic regression suite (issue #49, part 1).

The workspace of benchmarks/synthetic-workspace/ runs end to end with a scripted fake lead instead of
a model: the real scheduler, launcher, run lease, recorder, contract gates and metrics, a fake GitHub,
no network and no tokens. Each test is one scenario; the provenance a scenario leaves behind is compared
with the golden facts in tests/fixtures/golden/ (regenerate deliberately with PICO_UPDATE_GOLDEN=1,
never in CI). Real-agent benchmarks are a separate, explicitly started part:
benchmarks/synthetic-workspace/run_real.py.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario_support import (  # noqa: E402  (also puts tools/ on sys.path)
    GOLDEN, GREEN, PENDING, SyntheticPilot, bm, ev, git, implement, open_items, start_work, stop_group)
import headless_run as hr  # noqa: E402
import workspace_contracts as wc  # noqa: E402
import workspace_run as wr  # noqa: E402
import workspace_scheduler as ws  # noqa: E402
from test_headless_run import FakeClock  # noqa: E402


def partial_task(code: str = "APPROVED_INPUTS_UNAVAILABLE") -> list[dict]:
    """A task whose required input does not exist: the worker declares a prerequisite blocker and the lead ends PARTIAL."""
    return [ev(kind="phase", phase="$TASK", status="in_progress", route="CLAUDE_CODEX"),
            ev(kind="worker", phase="$TASK", step_id="claude_worker", status="in_progress", model="claude-sonnet-5-5",
               backend="claude-code", backend_model="claude-sonnet-5-5"),
            ev(kind="failure", phase="$TASK", step_id="claude_worker", code=code, head="$HEAD", inputs_digest="$INPUTS",
               blocker_kind="prerequisite"),
            ev(kind="phase", phase="$TASK", status="blocked", blocker_code=code),
            {"op": "finish", "status": "PARTIAL", "blocker": code}]


class HappyMultiRepoTest(SyntheticPilot):
    def test_happy_multi_repo(self):
        """Concurrent wave across both repositories; app-post-url waits for the published, CI-green upstream."""
        # Both second-wave children wait for each other at a barrier: the test only passes if they really overlap.
        barrier = {"op": "barrier", "name": "wave2", "parties": 2}
        self.script({"lib-slugify": [implement(tokens=1200, cost=0.05)],
                     "app-post-url": [[barrier, *implement(tokens=900, cost=0.04)]],
                     "lib-stopwords": [[barrier, *partial_task()]]})

        first = self.schedule()
        self.assertEqual(first["session"]["dispatched"], ["lib-slugify"])
        self.assertEqual(self.states(first), {"lib-slugify": "completed", "lib-stopwords": "waiting_for_readiness",
                                              "app-post-url": "waiting_for_readiness", "qualify-integration": "waiting_for_readiness"})
        # Completed is not ready: the downstream tasks stay held until the upstream is validated, published and CI-green.
        self.assertEqual(self.plan_task("app-post-url")["blocked_by"], ["lib-slugify"])
        self.release("lib-slugify")
        self.assertTrue(self.plan_task("app-post-url")["dispatchable"])

        second = self.schedule()
        self.assertEqual(sorted(second["session"]["dispatched"]), ["app-post-url", "lib-stopwords"])
        self.assertEqual((self.states(second)["app-post-url"], self.states(second)["lib-stopwords"]), ("completed", "partial"))
        self.assertEqual(self.peak_overlap(), (2, {"synth-lib": 1, "synth-app": 1}))
        upstream = self.workspace()["tasks"]["app-post-url"]["inputs"]["upstream"][0]
        self.assertEqual((upstream["task"], upstream["readiness"], upstream["pr"], upstream["contracts"]),
                         ("lib-slugify", "ready_for_downstream", "pico-bench-sandbox/synth-lib#101", {"slugify": "1"}))

        self.release("app-post-url")
        third = self.schedule()
        self.assertEqual((third["session"]["qualified"], third["session"]["dispatched"]), (["qualify-integration"], []))
        self.assertEqual(self.states(third), {"lib-slugify": "ready_for_downstream", "lib-stopwords": "partial",
                                              "app-post-url": "ready_for_downstream", "qualify-integration": "validated"})
        qualification = self.workspace()["tasks"]["qualify-integration"]["observed"]["validation"]
        self.assertEqual((qualification["status"], sorted(qualification["commits"])), ("passed", ["synth-app"]))
        # Draft PRs only; nothing merges.
        self.assertEqual([(pr["repo"], pr["draft"]) for pr in self.fake.created],
                         [("pico-bench-sandbox/synth-lib", True), ("pico-bench-sandbox/synth-app", True)])

        report = bm.workspace_report(self.state, "pico-bench")
        tasks = report["totals"]["tasks"]
        self.assertEqual((tasks["completed"], tasks["verified"], tasks["qualification"]),
                         (2, 2, [{"task": "qualify-integration", "validation": "passed"}]))
        self.assertEqual(report["headline"]["value"], 1050.0)  # (1200 + 900) tokens over the two verified tasks
        self.assert_golden(self.snapshot("happy-multi-repo", ["lib-slugify", "app-post-url", "lib-stopwords"]))


class DependencyGateTest(SyntheticPilot):
    def test_dependency_gate(self):
        """A downstream task stays held until its upstream is validated, published, CI-green and promoted."""
        self.script({"lib-slugify": [implement()]})

        def assert_held():
            with self.assertRaises(wr.WorkspaceRunError) as refused:
                self.launch("app-post-url")
            self.assertEqual(refused.exception.code, "WORKSPACE_TASK_BLOCKED")
            self.assertIn("lib-slugify", refused.exception.detail)
            self.assertIsNone(self.execution("app-post-url"), "a refused launch leaves no execution behind")

        assert_held()  # the upstream has not run at all
        result = self.schedule()
        self.assertEqual(result["session"]["dispatched"], ["lib-slugify"])
        self.assertEqual(self.states(result), {"lib-slugify": "completed", "lib-stopwords": "waiting_for_readiness",
                                               "app-post-url": "waiting_for_readiness", "qualify-integration": "waiting_for_readiness"})
        assert_held()  # implemented is not ready
        commit = self.validate("lib-slugify")["observed"]["validated_commit"]
        self.assertEqual(self.plan_task("app-post-url")["blocked_by"], ["lib-slugify"])
        assert_held()  # validated is not ready either
        number = self.publish("lib-slugify")
        assert_held()  # nor is a published draft
        for rollup, head, label in ((PENDING, commit, "CI pending"), ([], commit, "no CI observed"), (GREEN, "f" * 40, "CI green on another commit")):
            self.fake.checks[number] = ("OPEN", head, rollup)
            with self.assertRaises(wr.WorkspaceRunError, msg=label) as refused:
                wc.promote(manifest=self.manifest, task_id="lib-slugify", adapter=self.fake)
            self.assertEqual(refused.exception.code, "WORKSPACE_CI_NOT_GREEN", label)
            assert_held()
        self.fake.checks[number] = ("OPEN", commit, GREEN)
        wc.promote(manifest=self.manifest, task_id="lib-slugify", adapter=self.fake)
        task = self.plan_task("app-post-url")
        self.assertEqual((task["blocked_by"], task["dispatchable"]), ([], True))
        # The qualification task still waits for app-post-url, and nothing merged.
        self.assertEqual(self.plan_task("qualify-integration")["blocked_by"], ["app-post-url"])
        self.assertTrue(all(pr["draft"] for pr in self.fake.created))
        self.assert_golden(self.snapshot("dependency-gate", ["lib-slugify"]))


class PartialAndReconcileTest(SyntheticPilot):
    def test_partial_and_reconcile(self):
        """lib-stopwords ends PARTIAL on a missing input: nothing stays open, history is kept, the blocker is not looped on."""
        gate = ev(kind="retry_gate", phase="$TASK", step_id="claude_worker", head="$HEAD", inputs_digest="$INPUTS",
                  retry_budget="$BUDGET", source="default")
        declare = partial_task()
        stopwords = [*declare[:3],
                     ev(kind="worker", phase="$TASK", step_id="codex_reviewer", status="in_progress", model="gpt-6.1-sol",
                        backend="codex", backend_model="gpt-6.1-sol"),  # still running when the lead gives up
                     ev(kind="wave", wave_id="w-$TASK", task_ids=["$TASK"], status="declared"),  # never integrated
                     ev(kind="phase", phase="follow-up", status="not_started"),
                     gate, gate, gate,  # the lead keeps asking to start the step again
                     *declare[3:]]
        self.script({"lib-slugify": [implement()], "lib-stopwords": [stopwords]})
        self.complete_lib_slugify()

        result = self.launch("lib-stopwords")["task"]
        self.assertEqual((result["execution_status"], result["run_status"], result["run_blocker_code"]),
                         ("exited", "PARTIAL", "APPROVED_INPUTS_UNAVAILABLE"))
        parent = self.parent_record("lib-stopwords")
        self.assertEqual(open_items(parent), [], "a PARTIAL run leaves no phase, worker or wave open (#42)")
        reconciled = {(e["kind"], e.get("phase_id") or e.get("wave_id"), e.get("step_id")): (e["from_status"], e["to"])
                      for e in parent["reconciled"]}
        self.assertEqual(reconciled, {("worker", "lib-stopwords", "codex_reviewer"): ("in_progress", "abandoned"),
                                      ("wave", "w-lib-stopwords", None): ("declared", "abandoned"),
                                      ("phase", "follow-up", None): ("not_started", "abandoned")})
        # History is not rewritten: the blocker stays on the step and on the phase that recorded it.
        phase = next(p for p in parent["phases"] if p["id"] == "lib-stopwords")
        worker = next(w for w in phase["workers"] if w["step_id"] == "claude_worker")
        self.assertEqual((phase["status"], phase["blocker_code"], worker["status"], worker["blocker_code"]),
                         ("blocked", "APPROVED_INPUTS_UNAVAILABLE", "blocked", "APPROVED_INPUTS_UNAVAILABLE"))
        # The prerequisite is recorded once and every restart is refused with its own code (#48), not counted as a loop.
        gates = [a["answer"] for a in self.answers("lib-stopwords") if "refused" in a["answer"]]
        self.assertEqual([(g["refused"], g["code"], g["failure_code"]) for g in gates],
                         [(True, "PREREQUISITE_UNCHANGED", "APPROVED_INPUTS_UNAVAILABLE")] * 3)
        [signature] = parent["failure_signatures"]
        self.assertEqual((signature["kind"], signature["count"], signature["blocked"]), ("prerequisite", 1, True))
        self.assertNotIn("REPEATED_INEFFECTIVE_ACTION", str(parent))
        self.assert_golden(self.snapshot("partial-and-reconcile", ["lib-slugify", "lib-stopwords"]))


class BudgetTest(SyntheticPilot):
    def test_budget(self):
        """In-flight spend counts: the next task is held while a child still runs, and a direct launch is refused."""
        self.edit_manifest(budget={"max_tokens": 2000})
        running = implement(tokens=1500)
        running.insert(-1, {"op": "wait_file", "name": "go"})  # reports its spend, then keeps running until released
        self.script({"lib-slugify": [implement(tokens=1200)], "app-post-url": [running], "lib-stopwords": [partial_task()]})
        self.complete_lib_slugify()  # 1200 of 2000 tokens: still room

        self.launch_background("app-post-url")

        def spend():
            plan = wr._plan(self.manifest, access_check=None)
            tasks, problems = wr.running_tasks(plan)
            state = wr.budget_state(plan, running=tasks, extra_problems=problems)
            return (state, tasks) if state["spent_tokens"] >= 2700 else None

        state, tasks = self.wait_for(spend, "the running child's spend to be counted")
        self.assertEqual(tasks, {"app-post-url"}, "the child is still running")
        self.assertEqual((state["spent_tokens"], state["exhausted"], state["reasons"]), (2700, True, ["tokens"]))
        # Reported cost is a lower bound: the worker reported tokens but no price, and the report lists it as unpriced.
        self.assertFalse(state["cost_complete"])
        self.assertEqual(state["unpriced"], ["claude-code"])

        with self.assertRaises(wr.WorkspaceRunError) as refused:
            self.launch("lib-stopwords")
        self.assertEqual(refused.exception.code, "WORKSPACE_BUDGET_EXHAUSTED")
        self.assertIn("2700", refused.exception.detail)
        self.assertIn("--ignore-budget", refused.exception.detail)
        self.assertIsNone(self.execution("lib-stopwords"))

        # The scheduler holds the same task. Its first sleep comes after the hold decision: release the child then.
        signals = self.signals

        class ReleaseOnSleep:
            @staticmethod
            def sleep(seconds):
                (signals / "go").touch()
                time.sleep(min(seconds, 0.05))

            def __getattr__(self, name):
                return getattr(time, name)

        with unittest.mock.patch.object(ws, "time", ReleaseOnSleep()):
            held = self.schedule()
        self.assertEqual((held["session"]["dispatched"], held["session"]["held_by_budget"]), ([], ["lib-stopwords"]))
        self.assertEqual((self.states(held)["lib-stopwords"], self.states(held)["app-post-url"]), ("held_by_budget", "completed"))
        self.assertEqual(held["status"], "ATTENTION")
        self.assertEqual((held["budget"]["spent_tokens"], held["budget"]["exhausted"]), (2700, True))

        # The override is explicit and recorded; nothing else lifts the gate.
        forced = self.launch("lib-stopwords", ignore_budget=True)["task"]
        self.assertEqual(forced["budget_override"]["reasons"], ["tokens"])
        self.assertEqual((forced["budget_override"]["spent_tokens"], forced["budget_override"]["max_tokens"]), (2700, 2000))
        self.assert_golden(self.snapshot("budget", ["lib-slugify", "app-post-url", "lib-stopwords"]))


class InterruptResumeTest(SyntheticPilot):
    def kill_mid_run(self) -> str:
        """Start lib-slugify, let it get as far as a worker running, then kill the child's whole process group."""
        reached = [*start_work(), {"op": "signal", "name": "reached"}, {"op": "hang"}]
        self.script({"lib-slugify": [reached, implement(tokens=1000)]})
        self.launch_background("lib-slugify")
        self.wait_for(lambda: (self.signals / "reached").exists(), "the child to reach the point where it is killed")
        run_id = self.run_id("lib-slugify")
        pid = self.execution("lib-slugify")["pid"]
        stop_group(pid)
        self.children.remove(pid)
        return run_id

    def test_interrupt_resume(self):
        """A killed child is resumed into the same run under the lease, after its checkpoint was verified."""
        run_id = self.kill_mid_run()
        entry = wr.status(manifest=self.manifest, task_id="lib-slugify")["tasks"]["lib-slugify"]
        self.assertEqual((entry["execution_status"], entry["run_status"]), ("interrupted", "IN_PROGRESS"))
        lease = self.lease("lib-slugify")
        self.assertEqual((lease["state"], lease["stale_reason"]), ("stale", "OWNER_PROCESS_DEAD"))
        # Nothing restarts it implicitly, and a stale lease is never taken over silently.
        self.assertEqual(self.launch("lib-slugify")["task"]["outcome"], "resume_required")
        with self.assertRaises(wr.WorkspaceRunError) as refused:
            self.launch("lib-slugify", resume=True)
        self.assertEqual(refused.exception.code, "RUN_LEASE_STALE")
        waiting = self.schedule()
        self.assertEqual((waiting["session"]["dispatched"], waiting["session"]["resumed"]), ([], []))
        self.assertEqual((self.states(waiting)["lib-slugify"], waiting["status"]), ("needs_reconciliation", "ATTENTION"))

        # The resumed child restores the run through the real recorder (lease, then checkpoint verification) and finishes it.
        resumed = self.schedule(resume_interrupted=True)
        self.assertEqual(resumed["session"]["resumed"], ["lib-slugify"])
        row = next(r for r in resumed["tasks"] if r["id"] == "lib-slugify")
        self.assertEqual((row["state"], row["run_id"], row["launches"]), ("completed", run_id, 2))
        parent = self.parent_record("lib-slugify")
        self.assertEqual(len(parent["lease_takeovers"]), 1, "the recovery of the stale lease is on the record")
        self.assertEqual(open_items(parent), [])
        self.assert_golden(self.snapshot("interrupt-resume", ["lib-slugify"]))

    def test_interrupt_resume_refuses_a_drifted_checkout(self):
        """Resume verifies the checkpoint: a checkout that moved since is refused, and the run stays open and resumable."""
        run_id = self.kill_mid_run()
        repo = self.repos["synth-lib"]
        git("-C", str(repo), "switch", "-q", "-c", "drifted")  # HEAD is still the pin, so only the checkpoint can notice
        refused = self.launch("lib-slugify", resume=True, takeover_stale_lease=True)["task"]
        self.assertEqual((refused["run_id"], refused["run_status"]), (run_id, "IN_PROGRESS"))
        self.assertEqual(self.execution("lib-slugify")["exit_code"], 4)
        self.assertIn("RESUME_CHECKPOINT_DRIFT", Path(self.execution("lib-slugify")["log_path"]).read_text())
        self.assertEqual(git("-C", str(repo), "branch", "--list", "pico/*"), "", "the refused child must not have worked")
        # Put the checkout back and the same run continues.
        git("-C", str(repo), "switch", "-q", "main")
        again = self.launch("lib-slugify", resume=True, takeover_stale_lease=True)["task"]
        self.assertEqual((again["run_id"], again["run_status"]), (run_id, "COMPLETED"))


class TimeLimitTest(SyntheticPilot):
    def test_time_limit(self):
        """A hanging child is stopped at its deadline with EXECUTION_TIME_LIMIT; nothing is left open or running."""
        clock = FakeClock()
        for name, replacement in (("clock", clock), ("boot_elapsed", lambda record: None)):
            patch = unittest.mock.patch.object(hr, name, replacement)
            patch.start()
            self.addCleanup(patch.stop)
        self.script({"lib-slugify": [[*start_work(), {"op": "signal", "name": "reached"}, {"op": "hang"}]]})
        launched = self.launch_background("lib-slugify")["task"]
        self.assertEqual(launched["max_duration_seconds"], 900)  # the task's own limit from the manifest
        self.wait_for(lambda: (self.signals / "reached").exists(), "the child to be under way")
        pid = self.execution("lib-slugify")["pid"]

        clock.now += 899
        self.assertEqual(wr.status(manifest=self.manifest, task_id="lib-slugify")["tasks"]["lib-slugify"]["execution_status"], "launched")
        clock.now += 1
        result = self.schedule()
        row = next(r for r in result["tasks"] if r["id"] == "lib-slugify")
        self.assertEqual((row["state"], row["run_status"], row["run_blocker_code"]), ("cancelled", "BLOCKED", "EXECUTION_TIME_LIMIT"))
        self.assertEqual(row["time_limit"]["stop_reason"], "EXECUTION_TIME_LIMIT")
        self.assertIn("stopped after 900.0s of a 900s limit", ws.render(result))
        self.assertEqual((result["status"], self.states(result)["app-post-url"]), ("ATTENTION", "blocked_by_failure"))
        stop_group(pid)  # reaps it and proves the whole process group is gone
        self.children.remove(pid)
        self.assertEqual(open_items(self.parent_record("lib-slugify")), [])
        # The stop is final: neither a relaunch nor a resume starts the child again.
        for resume in (False, True):
            again = self.launch("lib-slugify", resume=resume, takeover_stale_lease=resume)["task"]
            self.assertEqual((again["outcome"], again["launches"]), ("already_finished", 1))
        self.assert_golden(self.snapshot("time-limit", ["lib-slugify"]))


class LeadCallLimitTest(SyntheticPilot):
    def test_lead_call_limit(self):
        """The recorder, launcher and scheduler side of the lead-call limit (#47).

        SCRIPTED, not asserted as behavior: the fake lead itself sends the warning at call 3, "reached" at
        call 4 and the stop, so those numbers are inputs. The extension's own enforcement (counting calls,
        warning, restricting tools) is covered by tests/orchestration-lifecycle.test.ts. PRODUCTION code
        asserted here: the launcher hands the limit and its source to the child, the recorder stores the
        stop and leaves the run IN_PROGRESS with a fresh checkpoint, the launcher marks the execution
        interrupted with stop_reason LEAD_CALL_LIMIT, a resume with the same limit is refused
        (LEAD_CALL_LIMIT_EXHAUSTED, by launcher and scheduler), and a raised limit continues the same run
        with the recorder keeping the earlier calls.
        """
        self.edit_manifest(tasks={"lib-slugify": {"max_lead_calls": 4, "lead_calls_warn_at": 3}})
        self.script({"lib-slugify": [[{"op": "lead_limit"}, {"op": "lead_calls", "n": 4}, {"op": "lead_stop"}],
                                     [{"op": "lead_limit"}, {"op": "lead_calls", "n": 2}, *implement()]]})
        stopped = self.launch("lib-slugify")["task"]
        run_id = stopped["run_id"]
        self.assertEqual((stopped["execution_status"], stopped["stop_reason"], stopped["run_status"]),
                         ("interrupted", "LEAD_CALL_LIMIT", "IN_PROGRESS"))
        parent = self.parent_record("lib-slugify")
        limit = parent["context"]["lead_call_limit"]
        # Recorder storage of what the launcher configured and of the stop event.
        self.assertEqual((limit["max_lead_calls"], limit["source"], limit["warn_at"], limit["stopped"], limit["stopped_reason"]),
                         (4, "task", 3, True, "LEAD_CALL_LIMIT"))
        self.assertIsNone(self.lease("lib-slugify")["lease"], "the lease is released at the stop")
        self.assertEqual(parent["checkpoint"]["reason"], "lead_call_limit")

        # Resuming with the limit that was just exhausted is refused, by the launcher and by the scheduler.
        with self.assertRaises(wr.WorkspaceRunError) as refused:
            self.launch("lib-slugify", resume=True)
        self.assertEqual(refused.exception.code, "LEAD_CALL_LIMIT_EXHAUSTED")
        scheduled = self.schedule(resume_interrupted=True)
        self.assertEqual(scheduled["session"]["refused"], {"lib-slugify": "LEAD_CALL_LIMIT_EXHAUSTED"})
        self.assertEqual(self.execution("lib-slugify")["launches"], 1)

        self.edit_manifest(tasks={"lib-slugify": {"max_lead_calls": 10, "lead_calls_warn_at": 8}})
        resumed = self.launch("lib-slugify", resume=True)["task"]
        self.assertEqual((resumed["run_id"], resumed["run_status"], resumed["launches"]), (run_id, "COMPLETED", 2))
        parent = self.parent_record("lib-slugify")
        # The calls of the first launch (scripted: 4) are still in the recorder after the resume (scripted: 2 more).
        self.assertEqual(len(parent["context"]["lead_calls"]), 6)
        recorded = parent["context"]["lead_call_limit"]
        self.assertEqual((recorded["max_lead_calls"], recorded["stopped"], recorded["calls_at_stop"]), (10, False, 4))
        self.assert_golden(self.snapshot("lead-call-limit", ["lib-slugify"]))


class RetryGateTest(SyntheticPilot):
    def test_retry_gate(self):
        """The same failure signature at the same HEAD and inputs exhausts the budget: REPEATED_INEFFECTIVE_ACTION."""
        self.edit_manifest(failure_retry_budget=1)

        def attempt():
            return [ev(kind="worker", phase="$TASK", step_id="claude_worker", status="in_progress", only_if_open=True,
                       model="claude-sonnet-5-5", backend="claude-code", backend_model="claude-sonnet-5-5"),
                    ev(kind="failure", phase="$TASK", step_id="claude_worker", code="TESTS_FAILED", head="$HEAD", inputs_digest="$INPUTS"),
                    ev(kind="worker", phase="$TASK", step_id="claude_worker", status="failed", blocker_code="TESTS_FAILED", only_if_open=True),
                    ev(kind="retry_gate", phase="$TASK", step_id="claude_worker", head="$HEAD", inputs_digest="$INPUTS",
                       retry_budget="$BUDGET", source="manifest")]
        self.script({"lib-slugify": [[ev(kind="phase", phase="$TASK", status="in_progress", route="CLAUDE_CODEX"),
                                      *attempt(), *attempt(),
                                      ev(kind="phase", phase="$TASK", status="blocked", blocker_code="REPEATED_INEFFECTIVE_ACTION"),
                                      {"op": "finish", "status": "PARTIAL", "blocker": "REPEATED_INEFFECTIVE_ACTION"}]]})
        result = self.launch("lib-slugify")["task"]
        self.assertEqual((result["run_status"], result["run_blocker_code"]), ("PARTIAL", "REPEATED_INEFFECTIVE_ACTION"))
        gates = [a["answer"] for a in self.answers("lib-slugify") if "refused" in a["answer"]]
        self.assertEqual([g["refused"] for g in gates], [False, True])
        self.assertEqual((gates[1]["code"], gates[1]["failure_code"], gates[1]["count"], gates[1]["retry_budget"]),
                         ("REPEATED_INEFFECTIVE_ACTION", "TESTS_FAILED", 2, 1))
        parent = self.parent_record("lib-slugify")
        worker = parent["phases"][0]["workers"][0]
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", "REPEATED_INEFFECTIVE_ACTION"))
        [signature] = parent["failure_signatures"]
        self.assertEqual((signature["kind"], signature["count"], signature["blocked"], signature["code"]),
                         ("failure", 2, True, "TESTS_FAILED"))
        self.assertEqual(parent["failure_retry_budget"], {"retry_budget": 1, "source": "manifest"})
        self.assertEqual(parent["worker_overrides"][-1]["reason"], "repeated_ineffective_action")
        self.assertEqual(open_items(parent), [])
        self.assert_golden(self.snapshot("retry-gate", ["lib-slugify"]))


class GoldenGuardTest(unittest.TestCase):
    """The comparison against the golden record is not vacuous, and regenerating it is a deliberate act."""

    def setUp(self):
        # Never let this class rewrite a golden file, even when the suite runs with PICO_UPDATE_GOLDEN=1.
        patcher = unittest.mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PICO_UPDATE_GOLDEN", None)

    def test_a_lost_or_extra_fact_is_detected(self):
        golden = json.loads((GOLDEN / "happy-multi-repo.json").read_text())
        guard = SyntheticPilot("assert_golden")
        for mutate in (lambda runs: runs["lib-slugify"]["facts"].pop(),
                       lambda runs: runs["lib-slugify"]["facts"].append({"category": "bind", "key": ["x", "y"], "value": {}}),
                       lambda runs: runs["lib-slugify"]["facts"][0]["value"].update(status="blocked")):
            changed = json.loads(json.dumps(golden))
            mutate(changed["runs"])
            with self.assertRaises(AssertionError):
                guard.assert_golden(changed)
        guard.assert_golden(golden)

    def test_regenerating_golden_facts_is_refused_in_ci(self):
        guard = SyntheticPilot("assert_golden")
        with unittest.mock.patch.dict(os.environ, {"PICO_UPDATE_GOLDEN": "1", "CI": "true"}):  # deliberately set: must refuse
            with self.assertRaises(AssertionError):
                guard.assert_golden({"scenario": "never-written", "runs": {}, "workspace": {}})
        self.assertFalse((GOLDEN / "never-written.json").exists())


if __name__ == "__main__":
    unittest.main()
