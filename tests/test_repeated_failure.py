"""Repeated ineffective actions: structured failure signatures, a retry budget and prerequisite blockers (#48)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
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
import yaml  # noqa: E402
from test_headless_run import FAKE_PI, git  # noqa: E402
from test_run_lease import StateTestCase  # noqa: E402
from test_workspace_run import WorkspaceRunFixture, wr  # noqa: E402

HEAD_A, HEAD_B = "a" * 40, "b" * 40
INPUTS_1, INPUTS_2 = "1" * 64, "2" * 64
REPEATED = "REPEATED_INEFFECTIVE_ACTION"


class SignatureTest(StateTestCase):
    def setUp(self):
        super().setUp()
        self.owner = "owner-a"
        self.run_id = self.start_run(self.owner)
        self.schema = json.loads((ROOT / "schemas/run-record.schema.json").read_text())
        self.event({"kind": "phase", "phase": "t1", "status": "in_progress", "route": "CLAUDE_CODEX"})
        self.event({"kind": "phase", "phase": "t2", "status": "in_progress", "route": "CLAUDE_CODEX"})

    def event(self, payload: dict, owner: str | None = None) -> dict:
        return po.record_event(self.run_id, payload, owner_id=owner or self.owner, repo_root=self.records)

    def start_worker(self, phase="t1", step="claude_worker"):
        self.event({"kind": "worker", "phase": phase, "step_id": step, "status": "in_progress", "only_if_open": True})

    def fail(self, code="WORKER_FAILED", phase="t1", step="claude_worker", head=HEAD_A, inputs=INPUTS_1, **extra):
        """One failed attempt as the extension records it: the failure signature, then the worker's terminal status."""
        self.start_worker(phase, step)
        payload = {"kind": "failure", "phase": phase, "step_id": step, "code": code, **extra}
        if head:
            payload["head"] = head
        if inputs:
            payload["inputs_digest"] = inputs
        self.event(payload)
        self.event({"kind": "worker", "phase": phase, "step_id": step, "status": "failed", "blocker_code": code,
                    "only_if_open": True})

    def gate(self, phase="t1", step="claude_worker", head=HEAD_A, inputs=INPUTS_1, budget=2, source="default") -> dict:
        payload = {"kind": "retry_gate", "phase": phase, "step_id": step, "retry_budget": budget, "source": source}
        if head:
            payload["head"] = head
        if inputs:
            payload["inputs_digest"] = inputs
        return self.event(payload)["retry_gate"]

    def worker(self, phase="t1", step="claude_worker") -> dict:
        phases = self.parent(self.run_id)["phases"]
        return next(w for p in phases if p["id"] == phase for w in p["workers"] if w["step_id"] == step)

    def signatures(self) -> list[dict]:
        return self.parent(self.run_id).get("failure_signatures") or []

    def assert_valid(self):
        record = provenance.get_run(self.records, self.run_id)
        self.assertEqual(provenance.validate_run_record(record, self.schema), [])

    # --- the loop -------------------------------------------------------------------------------------------

    def test_the_same_signature_at_the_same_head_reaches_the_budget_and_the_next_start_is_refused(self):
        # Budget 2 = the first failure plus two retries: three failures are allowed to happen.
        for attempt in (1, 2):
            self.fail()
            self.assertFalse(self.gate()["refused"], f"after failure {attempt} a retry is still allowed")
        self.fail()
        refused = self.gate()
        self.assertTrue(refused["refused"])
        self.assertEqual((refused["code"], refused["failure_code"], refused["count"], refused["retry_budget"]),
                         (REPEATED, "WORKER_FAILED", 3, 2))
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", REPEATED))
        [signature] = self.signatures()
        self.assertEqual((signature["count"], signature["blocked"], signature["head"], signature["inputs_digest"],
                          signature["task_id"], signature["step_id"], signature["route_epoch"], signature["code"],
                          signature["kind"]),
                         (3, True, HEAD_A, INPUTS_1, "t1", "claude_worker", 0, "WORKER_FAILED", "failure"))
        self.assertEqual(signature["digest"], refused["signature"])
        self.assertEqual(self.parent(self.run_id)["failure_retry_budget"], {"retry_budget": 2, "source": "default"})
        override = self.parent(self.run_id)["worker_overrides"][-1]
        self.assertEqual((override["from_status"], override["to_status"], override["blocker_code"], override["by"],
                          override["reason"]), ("failed", "blocked", REPEATED, "extension", "repeated_ineffective_action"))
        self.assertEqual(self.parent(self.run_id)["final_status"], "IN_PROGRESS")
        self.assert_valid()

    def test_a_zero_budget_refuses_the_first_repeat_and_a_larger_budget_allows_more(self):
        self.fail()
        self.assertTrue(self.gate(budget=0)["refused"])
        self.fail(phase="t2")
        self.fail(phase="t2")
        self.fail(phase="t2")
        self.assertFalse(self.gate(phase="t2", budget=3)["refused"])
        self.fail(phase="t2")
        self.assertTrue(self.gate(phase="t2", budget=3)["refused"])

    def test_a_new_commit_resets_the_count(self):
        for _ in range(3):
            self.fail()
        self.assertTrue(self.gate()["refused"])
        # The lead (or worker) committed: the checkout moved, so the old signature no longer applies.
        reopened = self.gate(head=HEAD_B)
        self.assertFalse(reopened["refused"])
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("failed", "WORKER_FAILED"))
        self.assertEqual(self.parent(self.run_id)["worker_overrides"][-1]["reason"], "state_changed")
        self.assertFalse(self.signatures()[0]["blocked"])
        # The failure at the new commit is a fresh signature with its own count.
        self.fail(head=HEAD_B)
        self.assertEqual(sorted((s["head"], s["count"]) for s in self.signatures()), [(HEAD_A, 3), (HEAD_B, 1)])
        self.assertFalse(self.gate(head=HEAD_B)["refused"])
        self.assert_valid()

    def test_changed_inputs_reset_the_count(self):
        for _ in range(3):
            self.fail()
        self.assertFalse(self.gate(inputs=INPUTS_2)["refused"])
        self.assertTrue(self.gate(inputs=INPUTS_1)["refused"], "returning to the old state meets the old signature")
        self.assertFalse(self.gate(inputs=None)["refused"], "an unknown input state is not the known one")

    def test_a_different_route_epoch_resets_the_count(self):
        for _ in range(3):
            self.fail()
        self.assertTrue(self.gate()["refused"])
        self.event({"kind": "escalation", "phase": "t1", "from_route": "CLAUDE_CODEX", "to_route": "CODEX_CLAUDE_CODEX",
                    "reason": "FAILED_ATTEMPTS"})
        self.event({"kind": "phase", "phase": "t1", "status": "in_progress", "route": "CODEX_CLAUDE_CODEX"})
        self.assertFalse(self.gate()["refused"])
        self.fail()
        self.assertEqual(sorted((s["route_epoch"], s["route"], s["count"]) for s in self.signatures()),
                         [(0, "CLAUDE_CODEX", 3), (1, "CODEX_CLAUDE_CODEX", 1)])
        self.assert_valid()

    def test_different_failure_codes_are_different_signatures(self):
        for code in ("WORKER_FAILED", "TESTS_FAILED", "WORKER_FAILED", "TESTS_FAILED", "LINT_FAILED"):
            self.fail(code=code)
        self.assertEqual(sorted((s["code"], s["count"]) for s in self.signatures()),
                         [("LINT_FAILED", 1), ("TESTS_FAILED", 2), ("WORKER_FAILED", 2)])
        self.assertFalse(self.gate()["refused"], "five failures, but no single signature went past the budget")
        self.fail(code="TESTS_FAILED")
        self.assertTrue(self.gate()["refused"])

    def test_tasks_and_steps_have_separate_signatures(self):
        for _ in range(3):
            self.fail()
        self.assertTrue(self.gate()["refused"])
        self.assertFalse(self.gate(phase="t2")["refused"])
        self.assertFalse(self.gate(step="codex_reviewer")["refused"])

    # --- prerequisite blockers ------------------------------------------------------------------------------

    UNCHANGED = "PREREQUISITE_UNCHANGED"

    def prerequisite(self, code="APPROVED_PILOT_INPUTS_UNAVAILABLE", head=HEAD_A, inputs=INPUTS_1, **extra):
        self.start_worker()
        payload = {"kind": "failure", "phase": "t1", "step_id": "claude_worker", "code": code, **extra}
        if head:
            payload["head"] = head
        if inputs:
            payload["inputs_digest"] = inputs
        self.event(payload)
        self.event({"kind": "worker", "phase": "t1", "step_id": "claude_worker", "status": "failed",
                    "blocker_code": code, "only_if_open": True})

    def test_a_prerequisite_blocked_step_is_not_restarted_until_head_or_inputs_change(self):
        code = "APPROVED_PILOT_INPUTS_UNAVAILABLE"
        self.prerequisite(code)
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", code))
        for _ in range(5):  # however often the lead asks, even with a budget of 0, it is not a loop
            refused = self.gate(budget=0)
            self.assertEqual((refused["refused"], refused["code"], refused["failure_code"]), (True, self.UNCHANGED, code))
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", code), "the step keeps its own code")
        [signature] = self.signatures()
        self.assertEqual((signature["kind"], signature["count"], signature["blocked"]), ("prerequisite", 1, True))
        self.assertNotIn(REPEATED, str(self.parent(self.run_id)["phases"]))
        self.assertEqual(self.parent(self.run_id)["phases"][0]["status"], "in_progress", "the phase is not blocked")
        self.assertEqual(self.parent(self.run_id)["final_status"], "IN_PROGRESS")
        self.assert_valid()

    def test_a_changed_head_reopens_a_prerequisite_blocked_step(self):
        code = "APPROVED_PILOT_INPUTS_UNAVAILABLE"
        self.prerequisite(code)
        self.assertEqual(self.gate()["code"], self.UNCHANGED)
        self.assertFalse(self.gate(head=HEAD_B)["refused"])
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("failed", code))
        self.assertEqual(self.parent(self.run_id)["worker_overrides"][-1]["reason"], "state_changed")
        # Still unresolved at the new state: blocked again with its own code, and not restartable again.
        self.prerequisite(code, head=HEAD_B)
        self.assertEqual(self.gate(head=HEAD_B)["code"], self.UNCHANGED)
        self.assertEqual(self.worker()["blocker_code"], code)
        self.assert_valid()

    def test_changed_inputs_reopen_a_prerequisite_blocked_step(self):
        code = "APPROVED_PILOT_INPUTS_UNAVAILABLE"
        self.prerequisite(code)
        self.assertEqual(self.gate(inputs=INPUTS_1)["code"], self.UNCHANGED)
        self.assertFalse(self.gate(inputs=INPUTS_2)["refused"])
        self.assertEqual(self.worker()["status"], "failed")
        self.assert_valid()

    def test_a_worker_chosen_code_ending_unavailable_is_not_a_prerequisite_and_does_not_escape_the_loop(self):
        # The reviewer's probe: FOO_UNAVAILABLE restarted at the same HEAD must not be allowed every time.
        for code in ("FOO_UNAVAILABLE", "BAR_MISSING_INPUT"):
            self.assertFalse(po.is_prerequisite(code), code)
        for _ in range(3):
            self.fail(code="FOO_UNAVAILABLE")
        refused = self.gate()
        self.assertEqual((refused["refused"], refused["code"], refused["count"]), (True, REPEATED, 3))
        self.assertEqual(self.signatures()[0]["kind"], "failure")

    def test_a_flapping_backend_code_cannot_restart_at_the_same_state(self):
        self.prerequisite("MODEL_BACKEND_UNAVAILABLE")
        for _ in range(8):
            self.assertEqual(self.gate()["code"], self.UNCHANGED)
        self.assertEqual(self.signatures()[0]["count"], 1, "refused starts are not new failures")

    def test_prerequisite_classification_is_a_closed_set_plus_the_workers_own_declaration(self):
        for code in ("APPROVED_PILOT_INPUTS_UNAVAILABLE", "MODEL_BACKEND_UNAVAILABLE", "MODEL_AUTH_UNAVAILABLE",
                     "PREREQUISITE_UNMET"):
            self.assertTrue(po.is_prerequisite(code), code)
        for code in ("WORKER_FAILED", "TESTS_FAILED", "ANYTHING_UNAVAILABLE", "CRT_MISSING_INPUT", "INPUT_MISSING", REPEATED):
            self.assertFalse(po.is_prerequisite(code), code)
        self.assertTrue(po.is_prerequisite("WORKER_FAILED", "prerequisite"), "the worker may declare it")

    def test_a_worker_declared_prerequisite_blocks_the_step_even_with_an_ordinary_code(self):
        self.prerequisite("NEEDS_REVIEWER_SIGNOFF", blocker_kind="prerequisite")
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", "NEEDS_REVIEWER_SIGNOFF"))
        self.assertEqual(self.gate(budget=0)["code"], self.UNCHANGED)

    def test_a_failed_step_that_later_hits_a_prerequisite_becomes_blocked_by_an_extension_override(self):
        self.fail()
        self.start_worker()
        self.event({"kind": "failure", "phase": "t1", "step_id": "claude_worker", "code": "PREREQUISITE_UNMET"})
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", "PREREQUISITE_UNMET"))
        override = self.parent(self.run_id)["worker_overrides"][-1]
        self.assertEqual((override["from_status"], override["by"], override["reason"]),
                         ("failed", "extension", "prerequisite_blocker"))

    def test_a_refusal_of_a_prerequisite_blocked_step_that_failed_differently_is_on_the_record(self):
        code = "APPROVED_PILOT_INPUTS_UNAVAILABLE"
        self.prerequisite(code)
        # The checkout moved on; the step failed differently there, over and over, while its record still
        # shows the old prerequisite (the automatic status never overrides a recorded one).
        for _ in range(4):
            self.event({"kind": "failure", "phase": "t1", "step_id": "claude_worker", "code": "TESTS_FAILED",
                        "head": HEAD_B, "inputs_digest": INPUTS_1})
        self.assertEqual((self.worker()["status"], self.worker()["blocker_code"]), ("blocked", code))
        # Gate at HEAD_B: the prerequisite entry is at HEAD_A (state changed) so the loop signature decides.
        refused = self.gate(head=HEAD_B)
        self.assertEqual((refused["refused"], refused["code"]), (True, REPEATED))
        worker = self.worker()
        self.assertEqual((worker["status"], worker["blocker_code"]), ("blocked", REPEATED))
        override = self.parent(self.run_id)["worker_overrides"][-1]
        self.assertEqual((override["from_status"], override["from_blocker_code"], override["to_status"],
                          override["blocker_code"], override["by"], override["reason"]),
                         ("blocked", code, "blocked", REPEATED, "extension", "retry_gate"))
        self.assert_valid()

    # --- an unknown HEAD is "no state change" ----------------------------------------------------------------

    def test_an_unknown_head_neither_reopens_a_blocked_step_nor_resets_the_count(self):
        for _ in range(3):
            self.fail()
        self.assertTrue(self.gate()["refused"])
        again = self.gate(head=None)
        self.assertEqual((again["refused"], again["code"]), (True, REPEATED))
        self.assertEqual(self.worker()["blocker_code"], REPEATED, "not reopened")
        self.assertEqual(self.signatures()[0]["count"], 3)
        # With no signature at all an unknown HEAD simply lets the start through.
        self.assertFalse(self.gate(phase="t2", head=None)["refused"])

    def test_an_unknown_head_with_changed_inputs_still_reopens_the_step(self):
        self.prerequisite("PREREQUISITE_UNMET")
        self.assertEqual(self.gate(head=None)["code"], self.UNCHANGED)
        self.assertFalse(self.gate(head=None, inputs=INPUTS_2)["refused"])
        self.assertEqual((self.worker()["status"], self.worker()["blocker_code"]), ("failed", "PREREQUISITE_UNMET"))
        for _ in range(3):
            self.fail(code="STUCK")
        self.assertTrue(self.gate()["refused"])
        self.assertFalse(self.gate(head=None, inputs=INPUTS_2)["refused"])
        self.assertEqual(self.worker()["status"], "failed")

    def test_an_unknown_head_does_not_reopen_a_prerequisite_blocked_step(self):
        self.prerequisite("PREREQUISITE_UNMET")
        for _ in range(2):
            self.assertEqual(self.gate(head=None)["code"], self.UNCHANGED)
        self.assertEqual((self.worker()["status"], self.worker()["blocker_code"]), ("blocked", "PREREQUISITE_UNMET"))

    # --- run, other tasks, reconciliation, lease -----------------------------------------------------------

    def test_the_refusal_blocks_neither_other_tasks_nor_the_run_and_the_lead_can_finish_partial(self):
        self.start_worker("t2")
        for _ in range(3):
            self.fail()
        self.assertTrue(self.gate()["refused"])
        parent = self.parent(self.run_id)
        phases = {p["id"]: p for p in parent["phases"]}
        self.assertEqual(phases["t1"]["status"], "in_progress", "the task's phase is not blocked by the step")
        self.assertEqual(phases["t2"]["workers"][0]["status"], "in_progress")
        self.assertEqual(parent["final_status"], "IN_PROGRESS")
        # The other task goes on and completes; events for it are still accepted.
        self.event({"kind": "worker", "phase": "t2", "step_id": "claude_worker", "status": "completed"})
        self.event({"kind": "phase", "phase": "t2", "status": "completed"})
        # COMPLETED is refused while the blocked task stands as in progress; PARTIAL reconciles (#42).
        self.event({"kind": "phase", "phase": "t1", "status": "blocked", "blocker_code": REPEATED})
        done = po.finish(self.run_id, "PARTIAL", owner_id=self.owner, repo_root=self.records)
        self.assertEqual(done["outcome"], "partial")
        final = self.parent(self.run_id)
        self.assertEqual([w["status"] for p in final["phases"] for w in p["workers"]], ["blocked", "completed"])
        self.assert_valid()

    def test_a_run_finished_partial_with_the_step_open_is_reconciled(self):
        self.start_worker()
        self.fail()
        self.start_worker("t2")
        po.finish(self.run_id, "PARTIAL", owner_id=self.owner, repo_root=self.records)
        final = self.parent(self.run_id)
        self.assertEqual([(e["phase_id"], e["from_status"]) for e in final["reconciled"] if e["kind"] == "worker"],
                         [("t2", "in_progress")])
        self.assertEqual(len(final["failure_signatures"]), 1, "the signatures stay as the record of what happened")

    def test_a_successful_retry_clears_the_signature(self):
        self.event({"kind": "activity", "status": "started", "tool": "ClaudeWorker", "task_id": "t1",
                    "step_id": "claude_worker", "route": "CLAUDE_CODEX"})
        self.event({"kind": "activity", "status": "failed", "tool": "ClaudeWorker", "task_id": "t1",
                    "step_id": "claude_worker"})
        self.fail()
        self.fail()
        self.assertEqual(self.signatures()[0]["count"], 2)
        self.event({"kind": "activity", "status": "started", "tool": "ClaudeWorker", "task_id": "t1",
                    "step_id": "claude_worker", "route": "CLAUDE_CODEX"})
        self.event({"kind": "activity", "status": "finished", "tool": "ClaudeWorker", "task_id": "t1",
                    "step_id": "claude_worker"})
        self.event({"kind": "worker", "phase": "t1", "step_id": "claude_worker", "status": "completed",
                    "only_if_open": True})
        self.assertEqual(self.worker()["status"], "completed")
        self.assertEqual(self.signatures(), [])
        self.assertFalse(self.gate()["refused"])

    def test_only_the_lease_owner_can_record_failures_or_run_the_gate(self):
        for payload in ({"kind": "failure", "phase": "t1", "step_id": "claude_worker", "code": "WORKER_FAILED"},
                        {"kind": "retry_gate", "phase": "t1", "step_id": "claude_worker", "retry_budget": 0,
                         "source": "cli"}):
            with self.assertRaises(po.ParentRunError) as other:
                self.event(payload, owner="intruder")
            self.assertTrue(other.exception.error_code.startswith("RUN_LEASE_"))
        self.assertEqual(self.signatures(), [])

    def test_a_resumed_owner_keeps_the_signatures_and_the_refusal(self):
        for _ in range(3):
            self.fail()
        self.assertTrue(self.gate()["refused"])
        run_lease.release(self.run_id, self.owner, repo_root=self.records)
        po.resume(self.run_id, worktree=self.target, owner_id="owner-b", pid=os.getpid(),
                  explicit_repo=po.github_integration.resolve_target(self.target, "Examplecorp/examplelab"),
                  repo_root=self.records)
        gate = self.event({"kind": "retry_gate", "phase": "t1", "step_id": "claude_worker", "retry_budget": 2,
                           "source": "cli", "head": HEAD_A, "inputs_digest": INPUTS_1}, owner="owner-b")["retry_gate"]
        self.assertTrue(gate["refused"])
        self.assertEqual(self.signatures()[0]["count"], 3)

    # --- bounds and validation ------------------------------------------------------------------------------

    def test_the_signature_list_is_bounded_and_drops_the_oldest_unblocked_entry_first(self):
        for _ in range(3):
            self.fail(code="STUCK")
        self.assertTrue(self.gate()["refused"])
        for index in range(po.MAX_FAILURE_SIGNATURES + 20):
            self.event({"kind": "failure", "phase": "t2", "step_id": "claude_worker", "code": "WORKER_FAILED",
                        "head": f"{index:040x}"})
        signatures = self.signatures()
        self.assertEqual(len(signatures), po.MAX_FAILURE_SIGNATURES)
        self.assertEqual(signatures[0]["code"], "STUCK", "a blocked signature outlives the unblocked ones")
        self.assertEqual(signatures[-1]["head"], f"{po.MAX_FAILURE_SIGNATURES + 19:040x}")
        self.assert_valid()

    def test_invalid_budgets_and_fields_are_refused(self):
        for budget, source in ((-1, "cli"), (1001, "cli"), (True, "cli"), ("2", "cli"), (1.5, "cli"), (2, "nowhere")):
            with self.assertRaises(po.ParentRunError) as refused:
                self.event({"kind": "retry_gate", "phase": "t1", "step_id": "claude_worker", "retry_budget": budget,
                            "source": source})
            self.assertEqual(refused.exception.error_code, "FAILURE_RETRY_BUDGET_INVALID")
        for bad in ({"code": "lower_case"}, {"code": "WORKER_FAILED", "head": "not-a-sha"},
                    {"code": "WORKER_FAILED", "inputs_digest": "short"},
                    {"code": "WORKER_FAILED", "blocker_kind": "other"}, {"code": "WORKER_FAILED", "extra": 1}):
            with self.assertRaises(po.ParentRunError):
                self.event({"kind": "failure", "phase": "t1", "step_id": "claude_worker", **bad})
        self.assertEqual(self.signatures(), [])

    def test_the_gate_outcome_is_never_counted_as_a_failure(self):
        self.event({"kind": "failure", "phase": "t1", "step_id": "claude_worker", "code": REPEATED})
        self.assertEqual(self.signatures(), [])

    def test_the_signature_digest_is_a_deterministic_function_of_its_fields(self):
        fields = ("t1", "claude_worker", 0, "CLAUDE_CODEX", "WORKER_FAILED", HEAD_A, INPUTS_1)
        digest = po.failure_signature_digest(*fields)
        self.assertEqual(digest, po.failure_signature_digest(*fields))
        for index, other in enumerate(("t2", "x", 1, "CODEX", "TESTS_FAILED", HEAD_B, INPUTS_2)):
            changed = list(fields)
            changed[index] = other
            self.assertNotEqual(digest, po.failure_signature_digest(*changed), index)


class ConfigurationTest(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(hr.validate_failure_retry_budget(None), (None, None))
        self.assertEqual(hr.validate_failure_retry_budget(0, "task"), (0, "task"))
        self.assertEqual(hr.validate_failure_retry_budget(1000, "manifest"), (1000, "manifest"))
        for bad in (-1, 1001, True, "2", 1.5):
            with self.assertRaises(hr.HeadlessError) as refused:
                hr.validate_failure_retry_budget(bad)
            self.assertEqual(refused.exception.code, "FAILURE_RETRY_BUDGET_INVALID")
        with self.assertRaises(hr.HeadlessError):
            hr.validate_failure_retry_budget(2, "policy")
        self.assertEqual(hr.validate_inputs_digest("f" * 64), "f" * 64)
        for bad in ("short", "F" * 64, 7):
            with self.assertRaises(hr.HeadlessError):
                hr.validate_inputs_digest(bad)

    def test_policy_supplies_the_default_and_the_schema_rejects_a_bad_value(self):
        schema = __import__("json").loads((ROOT / "schemas" / "autonomy.schema.json").read_text())
        permissions = {name: False for name in schema["properties"]["permissions"]["required"]}
        permissions["merge"] = {"enabled": False, "max_failure_cost": "low", "require_ci": True,
                                "require_review_if_policy_demands": True}

        def write(directory: Path, **limits):
            (directory / ".pi").mkdir()
            (directory / ".pi" / "autonomy.yaml").write_text(yaml.safe_dump({
                "profile": "manual", "permissions": permissions,
                "limits": {"parallel_tasks": 3, "nested_agents_per_task": 1, "repair_attempts": 1, "replans": 1,
                           **limits}}))

        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn("failure_retry_budget", autonomy_policy.limits(Path(tmp)))
            write(Path(tmp), failure_retry_budget=0)
            self.assertEqual(autonomy_policy.limits(Path(tmp))["failure_retry_budget"], 0)
        for bad in (-1, 1.5, "2"):
            with tempfile.TemporaryDirectory() as tmp:
                write(Path(tmp), failure_retry_budget=bad)
                with self.assertRaises(autonomy_policy.AutonomyPolicyError):
                    autonomy_policy.limits(Path(tmp))

    def test_session_scope_reports_the_checkout_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(session_scope.head(tmp)["ok"], False)
            git("-C", tmp, "init", "-q")
            self.assertEqual(session_scope.head(tmp), {"ok": True, "head": None}, "no commit yet")
            git("-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
            self.assertEqual(session_scope.head(tmp), {"ok": True, "head": git("-C", tmp, "rev-parse", "HEAD")})


ENV_FAKE = r'''elif behavior == "envdump":
    with open(os.environ["FAKE_PI_ENV_OUT"], "w") as out:
        json.dump({k: v for k, v in os.environ.items() if k.startswith("PI_ORCHESTRATION_")}, out)
    pcmd("finish", run_id, "PARTIAL", "--owner", owner)
elif behavior == "crash":'''


class WorkspaceConfigurationTest(WorkspaceRunFixture):
    """Task, manifest and CLI precedence through `workspace_run.launch` / `headless_run.start`."""

    def setUp(self):
        super().setUp()
        self.pi.write_text(FAKE_PI.replace('elif behavior == "crash":', ENV_FAKE, 1))
        os.environ["FAKE_PI_BEHAVIOR"] = "envdump"
        self.out = self.base / "env.json"
        os.environ["FAKE_PI_ENV_OUT"] = str(self.out)

    def manifest_with(self, default=None, overrides=None):
        path = self.manifest()
        data = yaml.safe_load(path.read_text())
        if default is not None:
            data["failure_retry_budget"] = default
        for task in data["tasks"]:
            task.update((overrides or {}).get(task["id"], {}))
        path.write_text(yaml.safe_dump(data))
        return path

    def launched(self, manifest, task) -> tuple[dict, dict]:
        import json
        self.out.unlink(missing_ok=True)
        entry = self.launch(manifest, task)["task"]
        return entry, json.loads(self.out.read_text())

    def test_no_value_leaves_the_extension_to_the_policy_and_the_default(self):
        entry, env = self.launched(self.manifest_with(), "a1")
        self.assertNotIn("failure_retry_budget", entry)
        self.assertNotIn("PI_ORCHESTRATION_FAILURE_RETRY_BUDGET", env)

    def test_manifest_default_then_task_override_with_zero_being_a_real_value(self):
        manifest = self.manifest_with(default=4, overrides={"b1": {"failure_retry_budget": 0},
                                                            "g1": {"failure_retry_budget": 6}})
        a1, env_a = self.launched(manifest, "a1")
        b1, env_b = self.launched(manifest, "b1")
        g1, _ = self.launched(manifest, "g1")
        self.assertEqual((a1["failure_retry_budget"], a1["failure_retry_budget_source"]), (4, "manifest"))
        self.assertEqual((env_a["PI_ORCHESTRATION_FAILURE_RETRY_BUDGET"],
                          env_a["PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE"]), ("4", "manifest"))
        self.assertEqual((b1["failure_retry_budget"], b1["failure_retry_budget_source"]), (0, "task"))
        self.assertEqual(env_b["PI_ORCHESTRATION_FAILURE_RETRY_BUDGET"], "0")
        self.assertEqual((g1["failure_retry_budget"], g1["failure_retry_budget_source"]), (6, "task"))

    def test_the_declared_inputs_digest_reaches_the_child_and_changes_with_the_inputs(self):
        manifest = self.manifest()
        entry, env = self.launched(manifest, "a1")
        digest = env["PI_ORCHESTRATION_INPUTS_DIGEST"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(entry["inputs_digest"], digest)
        # Computed from the declared inputs as they stand now: a changed pin of the task's repository changes it.
        import copy
        plan = wr.workspace_plan.plan(manifest, access_check=None)
        task = next(t for t in plan["tasks"] if t["id"] == "a1")
        repo = next(r for r in plan["repositories"] if r["id"] == task["repository"])
        self.assertEqual(wr._inputs_digest(plan, task, repo), digest)
        moved = copy.deepcopy(plan)
        next(r for r in moved["repositories"] if r["id"] == task["repository"])["pinned_commit"] = "c" * 40
        self.assertNotEqual(wr._inputs_digest(moved, task, next(
            r for r in moved["repositories"] if r["id"] == task["repository"])), digest)

    def test_invalid_values_violate_the_manifest_schema(self):
        for bad in (-1, "2", 1.5, True):
            for manifest in (self.manifest_with(default=bad), self.manifest_with(overrides={"a1": {"failure_retry_budget": bad}})):
                with self.assertRaises(wr.WorkspaceRunError):
                    self.launch(manifest, "a1")

    def test_the_cli_value_wins_on_a_direct_start_and_an_invalid_one_launches_nothing(self):
        # headless_run.start with an explicit CLI budget: handed to the child with source "cli".
        import json
        self.out.unlink(missing_ok=True)
        target, _ = self.repos["alpha"]
        record = hr.start(target=target, explicit_repo="acme/alpha", execution_id="exec-cli", objective="Inspect",
                          pi_executable=str(self.pi), failure_retry_budget=5)
        self.assertEqual((record["failure_retry_budget"], record["failure_retry_budget_source"]), (5, "cli"))
        env = json.loads(self.out.read_text())
        self.assertEqual((env["PI_ORCHESTRATION_FAILURE_RETRY_BUDGET"], env["PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE"]),
                         ("5", "cli"))
        with self.assertRaises(hr.HeadlessError) as refused:
            hr.start(target=target, explicit_repo="acme/alpha", execution_id="exec-bad", objective="Inspect",
                     pi_executable=str(self.pi), failure_retry_budget=-1)
        self.assertEqual(refused.exception.code, "FAILURE_RETRY_BUDGET_INVALID")
        self.assertFalse(list((self.state / "executions").rglob("exec-bad.json")))


if __name__ == "__main__":
    unittest.main()
