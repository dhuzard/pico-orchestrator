"""Repository-scoped worker sessions, context policy, and checkpoint provenance."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import context_budget  # noqa: E402
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, remote: str) -> Path:
    git("init", "-q", str(path))
    git("-C", str(path), "remote", "add", "origin", remote)
    git("-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com",
        "commit", "-q", "--allow-empty", "-m", "init")
    return path


class _ReposFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.examplelab = make_repo(base / "examplelab", "git@github.com:Examplecorp/examplelab.git")
        self.svc = make_repo(base / "sample-service",
                             "git@github.com:Examplecorp/sample-service.git")
        self.worktree = base / "examplelab-feature"
        git("-C", str(self.examplelab), "worktree", "add", "-q", "-b", "feature", str(self.worktree))

    def tearDown(self):
        self.tmp.cleanup()



class SessionScopeTest(_ReposFixture):
    def test_examplelab_svc_examplelab_sequence_never_crosses_repositories(self):
        steps = [(self.examplelab, self.examplelab, True),
                 (self.svc, self.examplelab, False),   # SVC run, worker still in Examplelab
                 (self.svc, self.svc, True),
                 (self.examplelab, self.svc, False),   # back to Examplelab, worker left in SVC
                 (self.examplelab, self.examplelab, True)]
        for target, worker, expected in steps:
            with self.subTest(target=target.name, worker=worker.name):
                result = session_scope.check(str(target), str(worker))
                self.assertEqual(result["ok"], expected)
                if not expected:
                    self.assertEqual(result["blocker_code"], "SESSION_REPOSITORY_MISMATCH")

    def test_linked_worktrees_and_subdirectories_stay_in_scope(self):
        (self.examplelab / "src").mkdir()
        self.assertTrue(session_scope.check(str(self.examplelab), str(self.worktree))["ok"])
        self.assertTrue(session_scope.check(str(self.worktree), str(self.examplelab / "src"))["ok"])

    def test_non_repository_directories_fail_closed(self):
        outside = Path(self.tmp.name) / "plain"
        outside.mkdir()
        self.assertEqual(session_scope.check(str(outside), str(outside))["blocker_code"], "SESSION_TARGET_UNRESOLVED")
        self.assertEqual(session_scope.check(str(self.svc), str(outside))["blocker_code"], "SESSION_REPOSITORY_MISMATCH")

    def test_cli_rejects_malformed_input(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/session_scope.py")], input="{}",
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "blocker_code": "SESSION_SCOPE_INPUT_INVALID"})


class WorkspaceAndSnapshotTest(_ReposFixture):
    """Real-git behaviour behind workspace binding and parent-checkout detection."""

    def test_only_linked_worktrees_of_the_target_can_be_bound(self):
        self.assertEqual(session_scope.worktree(str(self.examplelab), str(self.worktree)),
                         {"ok": True, "blocker_code": None, "path": str(self.worktree.resolve())})
        for path in (self.examplelab, self.examplelab / ".git", self.svc):
            with self.subTest(path=path.name):
                self.assertEqual(session_scope.worktree(str(self.examplelab), str(path))["blocker_code"],
                                 "WORKER_WORKSPACE_INVALID")

    def test_worktree_inside_a_git_directory_is_refused(self):
        # Regression (run-2dc716699308): the lead created `.git/pico-worktrees/s1`; Claude Code
        # protects every `.git` path and denied the headless worker's Write even in acceptEdits.
        for branch, relative in (("s1", ".git/pico-worktrees/s1"), ("s2", "nested/.git/s2")):
            with self.subTest(path=relative):
                path = self.examplelab / relative
                git("-C", str(self.examplelab), "worktree", "add", "-q", "-b", branch, str(path))
                self.assertEqual(session_scope.check(str(self.examplelab), str(path))["ok"], True)
                self.assertEqual(session_scope.worktree(str(self.examplelab), str(path)),
                                 {"ok": False, "blocker_code": "WORKER_WORKSPACE_PROTECTED_PATH"})
        # Other dot directories inside the checkout remain bindable.
        dotted = self.examplelab / ".pi/worktrees/s3"
        git("-C", str(self.examplelab), "worktree", "add", "-q", "-b", "s3", str(dotted))
        self.assertTrue(session_scope.worktree(str(self.examplelab), str(dotted))["ok"])

    def test_snapshot_changes_only_when_the_checkout_changes(self):
        before = session_scope.snapshot(str(self.examplelab))
        (self.worktree / "work.py").write_text("x = 1\n")
        self.assertEqual(session_scope.snapshot(str(self.examplelab))["digest"], before["digest"],
                         "writes inside a linked worktree do not touch the parent checkout")
        (self.examplelab / "leak.py").write_text("x = 1\n")
        leaked = session_scope.snapshot(str(self.examplelab))
        self.assertNotEqual(leaked["digest"], before["digest"])
        self.assertIn("?? leak.py", leaked["status"])
        (self.examplelab / "leak.py").write_text("x = 2\n")
        self.assertNotEqual(session_scope.snapshot(str(self.examplelab))["digest"], leaked["digest"],
                            "edits to an already-untracked file are detected")


class ContextPolicyTest(unittest.TestCase):
    def test_repository_policy_is_valid(self):
        policy = context_budget.load_policy()
        self.assertEqual(policy["checkpoint"]["max_output_tokens"], 4096)
        self.assertNotIn("lead", policy)

    def test_removed_compaction_controls_are_rejected(self):
        policy = yaml.safe_load((ROOT / "routing/context-policy.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            for field, value in (("compact_trigger_working_tokens", 24000),
                                 ("hard_limit_working_tokens", 32000),
                                 ("target_working_tokens", 20000)):
                with self.subTest(field=field):
                    broken = copy.deepcopy(policy)
                    broken["lead"] = {field: value}
                    path = Path(tmp) / "policy.yaml"
                    path.write_text(yaml.safe_dump(broken))
                    with self.assertRaises(context_budget.ContextBudgetError):
                        context_budget.load_policy(path)


STATE = {"objective": "Fix metadata persistence", "current_phase": "implementation",
         "established_facts": ["Writes go through store.py"], "decisions": ["Keep schema v2: compatibility"],
         "tests_run": ["pytest tests/test_store.py: 12 passed"], "tests_failing": [],
         "unresolved_questions": [], "blockers": [], "next_actions": ["Run independent review"],
         "artifacts": [{"path": "docs/spec.md", "purpose": "contract"}]}


class CheckpointProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.target = make_repo(base / "examplelab", "git@github.com:Examplecorp/examplelab.git")
        (self.target / "store.py").write_text("x = 1\n")
        env = {key: value for key, value in os.environ.items() if key != provenance.STATE_ENV}
        patcher = unittest.mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def parent(self) -> dict:
        return provenance.get_run(self.records, self.run_id)["parent_orchestration"]

    def checkpoint(self, **overrides) -> dict:
        payload = {"cwd": str(self.target), "reason": "budget", "tokens_before": 41000,
                   "files_touched": {"read": [str(self.target / "store.py")], "modified": ["store.py"]},
                   "state": STATE, **overrides}
        return context_budget.checkpoint(self.run_id, payload, repo_root=self.records)

    def test_checkpoint_is_namespaced_by_repository_and_run_and_linked_into_provenance(self):
        context_budget.start(self.run_id, self.target, repo_root=self.records)
        first = self.checkpoint()
        self.assertEqual(first["reference"], f"checkpoints/Examplecorp__examplelab/{self.run_id}/0001.json")
        stored = json.loads((self.records / "provenance" / first["reference"]).read_text())
        self.assertEqual(stored["repository"], "Examplecorp/examplelab")
        self.assertEqual(stored["git"]["starting_commit"], git("-C", str(self.target), "rev-parse", "HEAD"))
        self.assertEqual(stored["git"]["dirty_files"], ["store.py"])
        self.assertEqual(stored["files_touched"], {"read": ["store.py"], "modified": ["store.py"]})
        self.assertEqual(stored["state"], STATE)
        second = self.checkpoint(reason="overflow")
        self.assertTrue(second["reference"].endswith("/0002.json"))
        context = self.parent()["context"]
        self.assertEqual((context["compactions"], context["structured_compactions"]), (2, 2))
        self.assertEqual([item["reason"] for item in context["checkpoints"]], ["budget", "overflow"])

    def test_concurrent_checkpoints_get_distinct_sequences_and_artifacts(self):
        original = context_budget.git_state

        def slow_git_state(cwd):
            time.sleep(0.05)
            return original(cwd)

        with unittest.mock.patch.object(context_budget, "git_state", slow_git_state):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.checkpoint(), range(4)))
        references = sorted(result["reference"] for result in results)
        self.assertEqual([ref.rsplit("/", 1)[1] for ref in references],
                         ["0001.json", "0002.json", "0003.json", "0004.json"])
        for result in results:
            stored = json.loads((self.records / "provenance" / result["reference"]).read_text())
            self.assertEqual(stored["sequence"], result["checkpoint"]["sequence"])
        linked = [item["reference"] for item in self.parent()["context"]["checkpoints"]]
        self.assertEqual(linked, references)

    def test_invalid_or_secret_bearing_checkpoints_are_never_written(self):
        cases = [({**STATE, "transcript": "..."}, "CHECKPOINT_INVALID"),
                 ({**STATE, "decisions": ["x" * 501]}, "CHECKPOINT_INVALID"),
                 ({**STATE, "established_facts": ["api key: sk-ant-api03-" + "a" * 40]},
                  "CHECKPOINT_SECRET_LIKE_CONTENT")]
        for state, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(context_budget.ContextBudgetError) as raised:
                    self.checkpoint(state=state)
                self.assertEqual(raised.exception.code, code)
        self.assertFalse((self.records / "provenance" / "checkpoints").exists())
        self.assertEqual(self.parent()["context"]["compactions"], 0)

    def test_context_and_usage_telemetry_accumulate_observed_values_only(self):
        po.record_event(self.run_id, {"kind": "context", "baseline_tokens": 11000, "context_tokens": 11000,
                                      "working_tokens": 0}, repo_root=self.records)
        po.record_event(self.run_id, {"kind": "context", "context_tokens": 30000, "working_tokens": 19000},
                        repo_root=self.records)
        po.record_event(self.run_id, {"kind": "context", "context_tokens": 20000, "working_tokens": 9000},
                        repo_root=self.records)
        po.record_event(self.run_id, {"kind": "usage", "backend": "claude-code", "duration_ms": 4000},
                        repo_root=self.records)
        po.record_event(self.run_id, {"kind": "usage", "backend": "claude-code", "duration_ms": 1000,
                                      "input_tokens": None}, repo_root=self.records)
        po.record_event(self.run_id, {"kind": "usage", "backend": "pi-subagent", "duration_ms": 10,
                                      "input_tokens": 900, "output_tokens": 100, "cached_tokens": 50},
                        repo_root=self.records)
        context = self.parent()["context"]
        self.assertEqual((context["baseline_tokens"], context["peak_context_tokens"], context["peak_working_tokens"]),
                         (11000, 30000, 19000))
        self.assertEqual(context["worker_usage"]["claude-code"],
                         {"invocations": 2, "duration_ms": 5000, "input_tokens": None,
                          "output_tokens": None, "cached_tokens": None, "total_tokens": None,
                          "total_tokens_complete": False, "cost_complete": False})
        self.assertEqual(context["worker_usage"]["pi-subagent"]["input_tokens"], 900)
        for cost in (0.25, 0.125):
            po.record_event(self.run_id, {"kind": "usage", "backend": "claude-code", "input_tokens": 10,
                                          "estimated_cost_usd": cost}, repo_root=self.records)
        claude = self.parent()["context"]["worker_usage"]["claude-code"]
        self.assertEqual((claude["invocations"], claude["input_tokens"], claude["estimated_cost_usd"]), (4, 20, 0.375))
        for cost in (-1, "1", float("nan")):
            with self.assertRaises(po.ParentRunError):
                po.record_event(self.run_id, {"kind": "usage", "backend": "claude-code", "estimated_cost_usd": cost},
                                repo_root=self.records)
        with self.assertRaises(po.ParentRunError):
            po.record_event(self.run_id, {"kind": "context", "baseline_tokens": 12000}, repo_root=self.records)
        with self.assertRaises(po.ParentRunError):
            po.record_event(self.run_id, {"kind": "usage", "backend": "gpt-cli"}, repo_root=self.records)

    def test_escalation_reason_and_reasoning_effort_are_closed_sets(self):
        po.record_event(self.run_id, {"kind": "phase", "phase": "impl", "status": "in_progress",
                                      "route": "CODEX", "escalation_reason": "FAILED_ATTEMPTS"},
                        repo_root=self.records)
        po.record_event(self.run_id, {"kind": "worker", "phase": "impl", "step_id": "codex_worker",
                                      "status": "in_progress", "reasoning_effort": "low"}, repo_root=self.records)
        phase = self.parent()["phases"][0]
        self.assertEqual(phase["escalation_reason"], "FAILED_ATTEMPTS")
        self.assertEqual(phase["workers"][0]["reasoning_effort"], "low")
        for event in ({"kind": "phase", "phase": "impl", "status": "in_progress", "escalation_reason": "WHIM"},
                      {"kind": "worker", "phase": "impl", "step_id": "codex_worker", "status": "in_progress",
                       "reasoning_effort": "high"}):
            with self.subTest(event=event), self.assertRaises(po.ParentRunError):
                po.record_event(self.run_id, event, repo_root=self.records)


if __name__ == "__main__":
    unittest.main()
