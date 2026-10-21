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

PILOT_RUN = Path.home() / ".local/state/pi-coding-orchestrator/runs/run-a1f934f4cbaa.json"
OPEN = {"not_started", "in_progress"}


def open_items(record: dict) -> list[str]:
    parent = record["parent_orchestration"]
    return ([f"phase {p['id']}" for p in parent["phases"] if p["status"] in OPEN]
            + [f"worker {p['id']}/{w['step_id']}" for p in parent["phases"] for w in p["workers"]
               if w["status"] in OPEN])


class ReconcileOpenTest(unittest.TestCase):
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
        self.assertEqual([(e["kind"], e["phase_id"], e.get("step_id"), e["from_status"]) for e in entries],
                         [("phase", "busy", None, "in_progress"), ("worker", "busy", "claude_worker", "in_progress"),
                          ("phase", "idle", None, "not_started")])
        for entry in entries:
            self.assertEqual((entry["to"], entry["reason"]), ("interrupted", f"run finished {status}"))
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

    @unittest.skipUnless(PILOT_RUN.exists(), "the CRT pilot record is not on this machine")
    def test_real_crt_pilot_record_has_nothing_left_open(self):
        original = json.loads(PILOT_RUN.read_text())
        self.assertEqual(open_items(original), ["phase crt-export-verification-repair",
                                                "worker crt-claim-export/claude_worker"])
        record = copy.deepcopy(original)  # the copy is reopened; the pilot file is never written
        record.update(outcome="in_progress", ended_at=None)
        record["parent_orchestration"]["final_status"] = "IN_PROGRESS"
        target = provenance.run_path(self.records, record["run_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record))
        finished = po._finish_unlocked(record["run_id"], "PARTIAL", repo_root=self.records)
        self.assertEqual(open_items(finished), [])
        self.assertEqual([e["from_status"] for e in finished["parent_orchestration"]["reconciled"]],
                         ["in_progress", "in_progress"])
        self.assertEqual(finished["parent_orchestration"]["activity"], original["parent_orchestration"]["activity"])
        self.assertEqual(open_items(json.loads(PILOT_RUN.read_text())), open_items(original))


if __name__ == "__main__":
    unittest.main()
