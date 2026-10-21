"""A run that ends without COMPLETED leaves no phase or worker open (issue #42)."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import parent_orchestration as po
import provenance

OPEN = {"not_started", "in_progress"}


def open_items(record: dict) -> list[str]:
    parent = record["parent_orchestration"]
    return ([f"phase {p['id']}" for p in parent["phases"] if p["status"] in OPEN]
            + [f"worker {p['id']}/{w['step_id']}" for p in parent["phases"] for w in p["workers"]
               if w["status"] in OPEN])


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.schema = json.loads((ROOT / "schemas/run-record.schema.json").read_text())
        self.target = base / "target"
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        subprocess.run(["git", "-C", str(self.target), "remote", "add", "origin",
                        "git@github.com:Examplecorp/examplelab.git"], check=True)

    def tearDown(self):
        self.tmp.cleanup()


class ReconcileOpenTest(_Base):

    def open_run(self) -> str:
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]

        def event(data):
            po.record_event(run_id, data, repo_root=self.records)

        event({"kind": "phase", "phase": "done", "status": "completed", "route": "CODEX"})
        event({"kind": "phase", "phase": "busy", "status": "in_progress", "route": "CLAUDE_CODEX"})
        event({"kind": "worker", "phase": "busy", "step_id": "claude_worker", "status": "in_progress"})
        event({"kind": "worker", "phase": "busy", "step_id": "codex_reviewer", "status": "completed"})
        event({"kind": "phase", "phase": "idle", "status": "not_started"})
        event({"kind": "activity", "status": "started", "tool": "ClaudeWorker", "task_id": "busy",
               "step_id": "claude_worker"})
        return run_id

    def assert_reconciled(self, before: dict, after: dict, status: str):
        self.assertEqual(open_items(after), [])
        self.assertEqual(provenance.validate_run_record(after, self.schema), [])
        parent, old = after["parent_orchestration"], before["parent_orchestration"]
        entries = parent["reconciled"]
        self.assertEqual([(e["kind"], e["phase_id"], e.get("step_id"), e.get("route_epoch"), e["from_status"])
                          for e in entries],
                         [("phase", "busy", None, None, "in_progress"),
                          ("worker", "busy", "claude_worker", 0, "in_progress"),
                          ("phase", "idle", None, None, "not_started")])
        for entry in entries:
            self.assertEqual((entry["to"], entry["reason"]), ("abandoned", f"run finished {status}"))
        # Nothing recorded earlier changes except the statuses named in `reconciled`.
        self.assertEqual(parent["activity"], old["activity"])
        self.assertEqual(parent["phases"][0], old["phases"][0])
        reverted = copy.deepcopy(parent["phases"])
        reverted[1]["status"] = "in_progress"
        reverted[1]["workers"][0]["status"] = "in_progress"
        reverted[2]["status"] = "not_started"
        self.assertEqual(reverted, old["phases"])

    def test_non_completed_finish_reconciles_open_phases_and_workers(self):
        for status in ("PARTIAL", "BLOCKED", "FAILED"):
            with self.subTest(status=status):
                run_id = self.open_run()
                before = provenance.get_run(self.records, run_id)
                after = po.finish(run_id, status, repo_root=self.records)
                self.assert_reconciled(before, after, status)
                self.assertEqual(provenance.get_run(self.records, run_id), after)

    def test_settle_reconciles(self):
        run_id = self.open_run()
        before = provenance.get_run(self.records, run_id)
        after = po.settle(run_id, repo_root=self.records)
        self.assertEqual(after["outcome"], "blocked")
        self.assert_reconciled(before, after, "BLOCKED")

    def test_completed_still_refuses_open_work_and_records_nothing(self):
        run_id = self.open_run()
        before = provenance.run_path(self.records, run_id).read_bytes()
        with self.assertRaises(po.ParentRunError) as raised:
            po.finish(run_id, "COMPLETED", repo_root=self.records)
        self.assertEqual(raised.exception.error_code, "PARENT_COMPLETION_INCOMPLETE")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)

    def test_finishing_twice_does_not_duplicate(self):
        run_id = self.open_run()
        first = po.finish(run_id, "PARTIAL", repo_root=self.records)
        raw = provenance.run_path(self.records, run_id).read_bytes()
        again = po.finish(run_id, "BLOCKED", repo_root=self.records)
        self.assertEqual(again["parent_orchestration"]["reconciled"], first["parent_orchestration"]["reconciled"])
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), raw)
        # The helper is idempotent too: a reconciled item is no longer open.
        parent = copy.deepcopy(first["parent_orchestration"])
        po._reconcile_open(parent, "FAILED")
        self.assertEqual(parent["reconciled"], first["parent_orchestration"]["reconciled"])

    def test_nothing_open_adds_no_field(self):
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        po.record_event(run_id, {"kind": "phase", "phase": "p", "status": "blocked", "route": "CODEX"},
                        repo_root=self.records)
        record = po.finish(run_id, "BLOCKED", repo_root=self.records)
        self.assertNotIn("reconciled", record["parent_orchestration"])
        self.assertEqual(provenance.validate_run_record(record, self.schema), [])

    def test_open_record_without_the_new_field_still_validates(self):
        record = provenance.get_run(self.records, self.open_run())
        self.assertEqual(provenance.validate_run_record(record, self.schema), [])
        self.assertEqual(open_items(record), ["phase busy", "phase idle", "worker busy/claude_worker"])

    def test_sanitized_crt_pilot_record_has_nothing_left_open(self):
        """Phases and workers of the real CRT pilot run (run-a1f934f4cbaa), anonymized, reopened and finished."""
        fixture = json.loads((ROOT / "tests/fixtures/partial-run-open-work.json").read_text())
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        parent = provenance.get_run(self.records, run_id)["parent_orchestration"]
        parent["phases"] = copy.deepcopy(fixture["phases"])
        provenance.update_parent_orchestration(self.records, run_id, parent)
        before = provenance.get_run(self.records, run_id)
        self.assertEqual(open_items(before), ["phase phase-3", "worker phase-2/claude_worker"])
        finished = po.finish(run_id, "PARTIAL", repo_root=self.records)
        self.assertEqual(open_items(finished), [])
        self.assertEqual(provenance.validate_run_record(finished, self.schema), [])
        done = finished["parent_orchestration"]
        self.assertEqual([(e["kind"], e["phase_id"], e.get("step_id"), e["from_status"]) for e in done["reconciled"]],
                         [("worker", "phase-2", "claude_worker", "in_progress"),
                          ("phase", "phase-3", None, "in_progress")])
        self.assertEqual([p["status"] for p in done["phases"]], ["completed", "partial", "abandoned"])

    def test_only_current_route_epoch_workers_are_reconciled(self):
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]

        def event(data):
            po.record_event(run_id, data, repo_root=self.records)

        event({"kind": "phase", "phase": "p", "status": "in_progress", "route": "CLAUDE_CODEX"})
        event({"kind": "worker", "phase": "p", "step_id": "codex_reviewer", "status": "not_started"})
        event({"kind": "escalation", "phase": "p", "from_route": "CLAUDE_CODEX",
               "to_route": "CODEX_CLAUDE_CODEX", "reason": "REVIEW_FINDINGS"})
        event({"kind": "phase", "phase": "p", "status": "in_progress", "route": "CODEX_CLAUDE_CODEX"})
        event({"kind": "worker", "phase": "p", "step_id": "claude_worker", "status": "in_progress"})
        record = po.finish(run_id, "PARTIAL", repo_root=self.records)
        workers = {(w["step_id"], w["route_epoch"]): w["status"] for w in record["parent_orchestration"]["phases"][0]["workers"]}
        self.assertEqual(workers, {("codex_reviewer", 0): "not_started", ("claude_worker", 1): "abandoned"})
        self.assertEqual([(e["kind"], e.get("step_id"), e.get("route_epoch"))
                          for e in record["parent_orchestration"]["reconciled"]],
                         [("phase", None, None), ("worker", "claude_worker", 1)])
        self.assertEqual(provenance.validate_run_record(record, self.schema), [])


class WorkerJudgmentTest(_Base):
    """The lead's judgment wins over the lifecycle's automatic worker record."""

    def setUp(self):
        super().setUp()
        self.run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        self.event({"kind": "phase", "phase": "p", "status": "in_progress", "route": "CLAUDE_CODEX"})
        self.event({"kind": "worker", "phase": "p", "step_id": "claude_worker", "status": "in_progress"})

    def event(self, data):
        return po.record_event(self.run_id, data, repo_root=self.records)

    def worker(self):
        return provenance.get_run(self.records, self.run_id)["parent_orchestration"]["phases"][0]["workers"][0]

    def auto(self, status, **extra):
        return self.event({"kind": "worker", "phase": "p", "step_id": "claude_worker", "status": status,
                           "only_if_open": True, **extra})

    def lead(self, status, **extra):
        return self.event({"kind": "worker", "phase": "p", "step_id": "claude_worker", "status": status, **extra})

    def test_case_a_auto_failed_then_lead_failed_with_its_own_code_merges(self):
        self.auto("failed", blocker_code="WORKER_FAILED")
        self.lead("failed", blocker_code="MODEL_BACKEND_UNAVAILABLE")
        self.assertEqual((self.worker()["status"], self.worker()["blocker_code"]), ("failed", "MODEL_BACKEND_UNAVAILABLE"))
        self.lead("failed")  # restating without a code is safe
        self.assertEqual(self.worker()["blocker_code"], "MODEL_BACKEND_UNAVAILABLE")

    def test_case_b_auto_completed_then_lead_rejects_it(self):
        self.auto("completed")
        self.lead("failed", blocker_code="REVIEW_REJECTED")
        self.assertEqual((self.worker()["status"], self.worker()["blocker_code"]), ("failed", "REVIEW_REJECTED"))

    def test_lead_blocked_after_auto_completed(self):
        self.auto("completed")
        self.lead("blocked", blocker_code="REVIEW_REJECTED")
        self.assertEqual(self.worker()["status"], "blocked")

    def test_downgrade_needs_a_reason_and_nothing_else_reopens(self):
        self.auto("completed")
        for status, extra in (("failed", {}), ("in_progress", {}), ("not_started", {})):
            with self.assertRaises(po.ParentRunError) as raised:
                self.lead(status, **extra)
            self.assertEqual(raised.exception.error_code, "PARENT_WORKER_TRANSITION_INVALID")
        self.lead("failed", blocker_code="REVIEW_REJECTED")
        with self.assertRaises(po.ParentRunError):
            self.lead("completed")

    def test_automatic_record_never_overrides_a_recorded_status(self):
        self.lead("failed", blocker_code="REVIEW_REJECTED")
        before = provenance.run_path(self.records, self.run_id).read_bytes()
        self.auto("completed")
        self.auto("failed", blocker_code="WORKER_FAILED")
        self.assertEqual(provenance.run_path(self.records, self.run_id).read_bytes(), before)

    def test_unrelated_blocker_conflicts_still_refused(self):
        self.lead("in_progress", blocker_code="A_CODE")
        with self.assertRaises(po.ParentRunError) as raised:
            self.lead("in_progress", blocker_code="OTHER_CODE")
        self.assertEqual(raised.exception.error_code, "PARENT_EVENT_CONFLICT")


if __name__ == "__main__":
    unittest.main()
