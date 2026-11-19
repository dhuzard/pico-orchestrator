"""A lead-model quota stop settles with its own blocker code, not ORCHESTRATION_OUTCOME_UNRECORDED (#110)."""
from __future__ import annotations

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
import headless_run  # noqa: E402
import issue_dispatcher as dispatch  # noqa: E402
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402


class LeadQuotaStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.schema = json.loads((ROOT / "schemas/run-record.schema.json").read_text())
        self.target = base / "target"
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        subprocess.run(["git", "-C", str(self.target), "remote", "add", "origin",
                        "git@github.com:Examplecorp/examplelab.git"], check=True)
        self.run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]

    def parent(self, record: dict) -> dict:
        self.assertEqual(provenance.validate_run_record(record, self.schema), [])
        return record["parent_orchestration"]

    def test_quota_stop_settles_with_its_own_code_and_reset_time(self):
        record = po.settle(self.run_id, repo_root=self.records, stop_cause="model_quota",
                           retry_after="2026-11-18T14:46:00Z")
        parent = self.parent(record)
        self.assertEqual((parent["final_status"], parent["blocker_code"]), ("BLOCKED", "MODEL_QUOTA_EXHAUSTED"))
        self.assertEqual(parent["lead_stop"], {"cause": "model_quota", "retry_after": "2026-11-18T14:46:00Z"})

    def test_silent_exit_still_settles_unrecorded(self):
        parent = self.parent(po.settle(self.run_id, repo_root=self.records))
        self.assertEqual(parent["blocker_code"], "ORCHESTRATION_OUTCOME_UNRECORDED")
        self.assertNotIn("lead_stop", parent)

    def test_unknown_cause_and_malformed_reset_time_are_dropped_not_stored(self):
        parent = self.parent(po.settle(self.run_id, repo_root=self.records, stop_cause="model_quota",
                                       retry_after="in 5 hours; raw provider text"))
        self.assertEqual(parent["lead_stop"], {"cause": "model_quota", "retry_after": None})
        other = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        parent = self.parent(po.settle(other, repo_root=self.records, stop_cause="overloaded"))
        self.assertEqual(parent["blocker_code"], "ORCHESTRATION_OUTCOME_UNRECORDED")

    def test_a_blocker_recorded_by_a_phase_still_wins(self):
        po.record_event(self.run_id, {"kind": "phase", "phase": "build", "status": "blocked", "route": "CODEX",
                                      "blocker_code": "WORKER_TIMEOUT"}, repo_root=self.records)
        parent = self.parent(po.settle(self.run_id, repo_root=self.records, stop_cause="model_quota",
                                       retry_after="2026-11-18T14:46:00Z"))
        self.assertEqual(parent["blocker_code"], "WORKER_TIMEOUT")
        self.assertNotIn("lead_stop", parent)

    def test_cli_accepts_the_stop_cause_and_rejects_an_unknown_one(self):
        cli = [sys.executable, str(ROOT / "tools/parent_orchestration.py"), "settle", self.run_id, "--owner", "x"]
        bad = subprocess.run(cli + ["--stop-cause", "anything"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("invalid choice", bad.stderr)

    def test_run_status_and_dispatcher_show_retry_after(self):
        record = po.settle(self.run_id, repo_root=self.records, stop_cause="model_quota",
                           retry_after="2026-11-18T14:46:00Z")
        with patch.object(headless_run, "ROOT", self.records), \
                patch.object(headless_run.run_lease, "summary", return_value=None):
            summary = headless_run.run_status(self.run_id)
        self.assertEqual(summary["blocker_code"], "MODEL_QUOTA_EXHAUSTED")
        self.assertEqual(summary["lead_stop"], {"cause": "model_quota", "retry_after": "2026-11-18T14:46:00Z"})
        self.assertEqual(dispatch.quota_next_action({"run": summary}),
                         "Retry after 2026-11-18T14:46:00Z: open a new queue issue and approve it")
        unknown = dict(summary, lead_stop={"cause": "model_quota", "retry_after": None})
        self.assertIn("reset time not reported", dispatch.quota_next_action({"run": unknown}))
        self.assertEqual(record["parent_orchestration"]["lead_stop"]["retry_after"], "2026-11-18T14:46:00Z")


class DispatcherQuotaHandoffTest(unittest.TestCase):
    def test_blocked_quota_execution_is_reported_as_a_quota_stop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = {'state': Path(tmp.name) / 'state.sqlite', 'queue': 'acme/queue', 'gh': 'gh',
               'target_gh_config_dir': Path(tmp.name)}
        record = {'status': 'exited', 'run': {'status': 'BLOCKED', 'blocker_code': 'MODEL_QUOTA_EXHAUSTED',
                                             'lead_stop': {'cause': 'model_quota',
                                                           'retry_after': '2026-11-18T14:46:00Z'}}}
        with dispatch.database(cfg) as db:
            db.execute('INSERT INTO approvals (number,digest,repo,checkout,execution_id,mode,state) VALUES (?,?,?,?,?,?,?)',
                       (11, 'd', 'acme/tool', '/tmp/x', 'issue-11', 'mutation', 'blocked'))
            db.commit()
            with patch.object(dispatch, 'pico_action', return_value=record), \
                    patch.object(dispatch, 'target_env', return_value={}), \
                    patch.object(dispatch, 'handoff_comment') as comment:
                dispatch.reconcile_delivery(cfg, db, db.execute('SELECT * FROM approvals').fetchone())
        item = comment.call_args.args[2]
        self.assertEqual(item['reason'],
                         'Execution blocked: MODEL_QUOTA_EXHAUSTED (the lead model hit its usage quota or rate limit)')
        self.assertEqual(item['next_action'], 'Retry after 2026-11-18T14:46:00Z: open a new queue issue and approve it')


if __name__ == "__main__":
    unittest.main()
