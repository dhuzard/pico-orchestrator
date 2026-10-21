"""Routed backend preflight and unified parent-run failure-path regressions."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import backend_preflight as bp
import parent_orchestration as po
import provenance
import routing_engine as engine
import watch


class LifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assets = engine.load_policy_assets(ROOT)
        cases = engine.load_yaml(ROOT / "tests/routing-cases.yaml")["cases"]
        task = next(case["task"] for case in cases if case["expected_route"] == "CODEX_CLAUDE_CODEX")
        resolution = engine.resolve_route(task, assets.policy, None, assets.task_schema)
        cls.route = engine.build_result(resolution, assets.policy, None, assets.execution_plan_schema)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.records = base / "orchestrator"
        (self.records / "schemas").mkdir(parents=True)
        shutil.copy(ROOT / "schemas/run-record.schema.json", self.records / "schemas/run-record.schema.json")
        self.target = base / "target"
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        subprocess.run(["git", "-C", str(self.target), "remote", "add", "origin",
                        "git@github.com:Examplecorp/examplelab.git"], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def runner(version="2.1.251", authenticated=True):
        def run(args):
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, f"{version} (Claude Code)\n", "")
            if args[-3:] == ["auth", "status", "--json"]:
                return subprocess.CompletedProcess(args, 0, json.dumps({"loggedIn": authenticated}), "")
            raise AssertionError(args)
        return run

    def fake_claude(self) -> Path:
        path = Path(self.tmp.name) / "claude"
        if not path.exists():
            path.write_text("#!/bin/sh\nexit 99\n")
            path.chmod(0o755)
        return path

    def pin(self, version="2.1.251") -> str:
        stat = self.fake_claude().stat()
        return json.dumps({"backend": "claude-code", "backend_executable": str(self.fake_claude()),
                           "backend_version_output": f"{version} (Claude Code)",
                           "backend_device": stat.st_dev, "backend_inode": stat.st_ino})

    def check(self, *, version="2.1.251", authenticated=True, tools=None, pinned_identity=None):
        # The pin is always explicit so an ambient launcher pin can never leak in.
        return bp.preflight(self.route, tools or ["spawn_subagent", "ClaudeWorker"],
                            executable=str(self.fake_claude()), run=self.runner(version, authenticated),
                            pinned_identity=self.pin(version) if pinned_identity is None else pinned_identity)

    def test_required_model_old_backend_is_blocked_without_substitution(self):
        result = self.check()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["blocker_code"], "MODEL_BACKEND_VERSION_UNSUPPORTED")
        self.assertEqual(result["model"], "anthropic/claude-opus-5-5")
        self.assertEqual(result["installed_backend_version"], "2.1.251")
        self.assertEqual(result["required_backend_version"], ">=2.1.280")
        self.assertEqual(self.route["backend_bindings"]["claude"]["capacity_models"]["high"]["model"],
                         "anthropic/claude-opus-5-5")

    def test_supported_backend_proceeds_but_model_entitlement_remains_unverified(self):
        result = self.check(version="2.1.280")
        self.assertEqual(result["status"], "READY")
        claude = next(step for step in result["checked_steps"] if step["backend"] == "claude")
        self.assertEqual(claude["model"], "anthropic/claude-opus-5-5")
        self.assertEqual(claude["backend_model"], "claude-opus-5-5")
        self.assertEqual(claude["model_access"], "unverified")

    def test_auth_and_invocation_unavailable_are_blockers(self):
        self.assertEqual(self.check(version="2.1.280", authenticated=False)["blocker_code"],
                         "MODEL_AUTH_UNAVAILABLE")
        self.assertEqual(self.check(tools=["spawn_subagent"])["blocker_code"],
                         "MODEL_BACKEND_UNAVAILABLE")

    def test_unrecognized_configured_model_fails_closed(self):
        result = bp.preflight(self.route, ["spawn_subagent", "ClaudeWorker"],
                              executable=str(self.fake_claude()), run=self.runner("2.1.280"),
                              pinned_identity=self.pin("2.1.280"),
                              compatibility={"schema_version": 1, "claude_code_minimum_versions": {},
                                             "claude_code_model_ids": {}})
        self.assertEqual(result["blocker_code"], "MODEL_IDENTIFIER_UNSUPPORTED")

    def test_missing_or_malformed_pin_is_uninitialized_not_mismatch(self):
        for raw, state in ((None, "missing"), ("", "missing"), ("{not json", "malformed"),
                           (json.dumps({"backend_executable": "/x"}), "malformed")):
            with self.subTest(raw=raw):
                result = bp.preflight(self.route, ["spawn_subagent", "ClaudeWorker"],
                                      executable=str(self.fake_claude()),
                                      run=lambda args: self.fail("backend probe must not run"),
                                      pinned_identity=raw)
                self.assertEqual(result["blocker_code"], "BACKEND_IDENTITY_UNINITIALIZED")
                self.assertEqual(result["identity_state"], state)
                self.assertIn("scripts/use-latest.sh", result["remediation"])
                self.assertIn("scripts/run-local.sh", result["remediation"])

    def test_changed_pinned_identity_remains_backend_mismatch(self):
        drifted = json.loads(self.pin("2.1.280"))
        drifted["backend_inode"] += 1
        result = self.check(version="2.1.280", pinned_identity=json.dumps(drifted))
        self.assertEqual(result["blocker_code"], "MODEL_BACKEND_MISMATCH")
        self.assertNotIn("MODEL_BACKEND_MISMATCH", bp.REROUTABLE_BLOCKERS)
        self.assertNotIn("BACKEND_IDENTITY_UNINITIALIZED", bp.REROUTABLE_BLOCKERS)

    def test_pin_defaults_to_launcher_environment(self):
        with unittest.mock.patch.dict("os.environ", {bp.PIN_ENV: self.pin("2.1.280")}):
            ready = bp.preflight(self.route, ["spawn_subagent", "ClaudeWorker"],
                                 executable=str(self.fake_claude()), run=self.runner("2.1.280"))
        self.assertEqual(ready["status"], "READY")
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            blocked = bp.preflight(self.route, ["spawn_subagent", "ClaudeWorker"],
                                   executable=str(self.fake_claude()), run=self.runner("2.1.280"))
        self.assertEqual(blocked["blocker_code"], "BACKEND_IDENTITY_UNINITIALIZED")

    def test_invalid_or_forged_route_fails_before_backend_probe(self):
        forged = copy.deepcopy(self.route)
        forged["backend_bindings"]["claude"]["capacity_models"]["high"]["model"] = "sonnet"
        result = bp.preflight(forged, ["spawn_subagent", "ClaudeWorker"],
                              run=lambda args: self.fail("backend probe must not run"))
        self.assertEqual(result["blocker_code"], "ROUTING_RESULT_INVALID")

    def test_preflight_blocks_before_implementation_worktree_and_traces(self):
        created = []
        with self.assertRaises(po.ParentRunBlocked):
            with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
                parent.event({"kind": "phase", "phase": "S1", "status": "in_progress",
                              "route": "CODEX_CLAUDE_CODEX", "policy_version": 4})
                result = self.check()
                parent.event({"kind": "worker", "phase": "S1", "step_id": result["step_id"],
                              "model": result["model"], "backend_model": result["backend_model"],
                              "status": "blocked",
                              "blocker_code": result["blocker_code"],
                              "installed_backend_version": result["installed_backend_version"],
                              "required_backend_version": result["required_backend_version"]})
                run_id = parent.run_id
                parent.prepare_implementation(self.route, lambda route: result,
                                              lambda: created.append("worktree"))
        self.assertEqual(created, [])
        record = provenance.get_run(self.records, run_id)
        self.assertEqual(record["outcome"], "blocked")
        self.assertEqual(record["parent_orchestration"]["blocker_code"], "MODEL_BACKEND_VERSION_UNSUPPORTED")
        self.assertEqual(record["parent_orchestration"]["mutations"], {"github": 0, "code": 0})
        self.assertEqual(record["parent_orchestration"]["phases"][0]["workers"][0]["model"],
                         "anthropic/claude-opus-5-5")
        self.assertEqual(record["parent_orchestration"]["phases"][0]["workers"][0]["backend_model"],
                         "claude-opus-5-5")

    def test_supported_preflight_creates_worktree_only_after_check(self):
        order = []
        with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
            value = parent.prepare_implementation(self.route,
                lambda route: (order.append("preflight") or self.check(version="2.1.280")),
                lambda: (order.append("worktree") or "created"))
            self.assertEqual(value, "created")
            run_id = parent.run_id
            parent.event({"kind": "phase", "phase": "preflight", "status": "completed"})
            parent.finish("COMPLETED")
        self.assertEqual(order, ["preflight", "worktree"])
        self.assertEqual(provenance.get_run(self.records, run_id)["outcome"], "completed")

    def test_worker_spawn_failure_finalizes_parent_failed(self):
        with self.assertRaises(RuntimeError):
            with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
                run_id = parent.run_id
                parent.event({"kind": "phase", "phase": "S1", "status": "in_progress",
                              "route": "CODEX_CLAUDE_CODEX", "policy_version": 4})
                raise RuntimeError("spawn failed with ghp_" + "s" * 30)
        record = provenance.get_run(self.records, run_id)
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["parent_orchestration"]["blocker_code"], "ORCHESTRATION_EXECUTION_FAILED")
        self.assertNotIn("ghp_", json.dumps(record))

    def test_routing_failure_finalizes_parent_blocked(self):
        with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
            run_id = parent.run_id
            parent.event({"kind": "phase", "phase": "routing", "status": "blocked",
                          "blocker_code": "ROUTING_FAILED"})
        record = provenance.get_run(self.records, run_id)
        self.assertEqual(record["outcome"], "blocked")
        self.assertEqual(record["parent_orchestration"]["blocker_code"], "ROUTING_FAILED")

    def test_parent_trace_records_availability_rerouted_selected_route(self):
        assets = engine.load_policy_assets(ROOT)
        task = next(
            case["task"] for case in engine.load_yaml(ROOT / "tests/routing-cases.yaml")["cases"]
            if case["expected_route"] == "CODEX_CLAUDE_CODEX"
        )
        resolution = engine.resolve_route(
            task, assets.policy, None, assets.task_schema, unavailable_routes=["CODEX_CLAUDE_CODEX"]
        )
        result = engine.build_result(resolution, assets.policy, None, assets.execution_plan_schema)
        self.assertEqual(result["selected_route"], "CODEX")
        with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
            run_id = parent.run_id
            parent.event({"kind": "phase", "phase": "implementation", "status": "completed",
                          "route": result["selected_route"], "policy_version": result["policy_version"]})
            parent.finish("COMPLETED")
        record = provenance.get_run(self.records, run_id)
        self.assertEqual(record["parent_orchestration"]["phases"][0]["route"], "CODEX")

    def test_child_github_adapter_run_is_linked_without_body(self):
        child = provenance.start_run(self.records, backlog_id=None, github_issue=401,
                                     user_objective="Read GitHub backlog", route_task_result=None,
                                     starting_commit=None)
        provenance.finish_run(self.records, child["run_id"], outcome="completed")
        with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
            run_id = parent.run_id
            parent.event({"kind": "child", "phase": "phase-a", "run_id": child["run_id"]})
            parent.event({"kind": "phase", "phase": "phase-a", "status": "completed"})
            parent.finish("COMPLETED")
        record = provenance.get_run(self.records, run_id)
        self.assertEqual(record["parent_orchestration"]["phases"][0]["child_run_ids"], [child["run_id"]])
        self.assertNotIn("issue body", json.dumps(record))

    def test_real_phase_a_then_s1_backend_blocker_is_one_parent_trace(self):
        child = provenance.start_run(self.records, backlog_id=None, github_issue=401,
                                     user_objective="Read GitHub backlog", route_task_result=None,
                                     starting_commit=None)
        provenance.finish_run(self.records, child["run_id"], outcome="completed")
        with po.ParentRun(worktree=self.target, repo_root=self.records) as parent:
            run_id = parent.run_id
            parent.event({"kind": "phase", "phase": "phase-a", "status": "completed",
                          "route": "CODEX", "policy_version": 4})
            parent.event({"kind": "worker", "phase": "phase-a", "step_id": "codex_worker",
                          "model": "openai-codex/gpt-6-luna", "status": "completed"})
            parent.event({"kind": "child", "phase": "phase-a", "run_id": child["run_id"]})
            parent.event({"kind": "phase", "phase": "S1", "status": "blocked",
                          "route": "CODEX_CLAUDE_CODEX", "blocker_code": "MODEL_BACKEND_VERSION_UNSUPPORTED"})
            parent.event({"kind": "worker", "phase": "S1", "step_id": "codex_specifier",
                          "model": "openai-codex/gpt-6-sol", "status": "completed"})
            parent.event({"kind": "worker", "phase": "S1", "step_id": "claude_worker",
                          "model": "anthropic/claude-opus-5-5", "status": "blocked",
                          "blocker_code": "MODEL_BACKEND_VERSION_UNSUPPORTED",
                          "installed_backend_version": "2.1.251",
                          "required_backend_version": ">=2.1.280"})
            parent.event({"kind": "worker", "phase": "S1", "step_id": "codex_reviewer",
                          "status": "not_started"})
            parent.finish("BLOCKED", "MODEL_BACKEND_VERSION_UNSUPPORTED")
        record = provenance.get_run(self.records, run_id)
        self.assertEqual(record["outcome"], "blocked")
        trace = record["parent_orchestration"]
        self.assertEqual(trace["target_repository"], "Examplecorp/examplelab")
        self.assertEqual(trace["policy_version"], 4)
        self.assertEqual(trace["mutations"], {"github": 0, "code": 0})
        self.assertEqual(trace["phases"][0]["child_run_ids"], [child["run_id"]])
        # A worker never started when the run ended is reconciled, not left open (issue #42).
        self.assertEqual(trace["phases"][1]["workers"][2]["status"], "abandoned")
        self.assertEqual([(e["kind"], e["step_id"], e["route_epoch"], e["from_status"]) for e in trace["reconciled"]],
                         [("worker", "codex_reviewer", 0, "not_started")])
        report = provenance.build_trace_report(self.records, record)
        self.assertEqual(report["parent_orchestration"], trace)
        rendered = provenance.render_trace_text(report)
        self.assertIn("Target repository: Examplecorp/examplelab", rendered)
        self.assertIn("MODEL_BACKEND_VERSION_UNSUPPORTED", rendered)
        self.assertIn(f"Child run: {child['run_id']}", rendered)
        self.assertIn("Mutations: GitHub 0; code 0", rendered)

    def test_all_parent_terminal_outcomes_validate(self):
        schema = provenance.load_schema(self.records)
        for status in ("COMPLETED", "BLOCKED", "FAILED", "PARTIAL"):
            with self.subTest(status=status):
                parent = po.ParentRun(worktree=self.target, repo_root=self.records)
                if status == "COMPLETED":
                    parent.event({"kind": "phase", "phase": "verification", "status": "completed"})
                record = parent.finish(status, "ROUTING_FAILED" if status == "BLOCKED" else None)
                self.assertEqual(provenance.validate_run_record(record, schema), [])
                self.assertEqual(record["parent_orchestration"]["final_status"], status)

    def test_completed_cannot_hide_unfinished_phase_or_worker(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        parent.event({"kind": "phase", "phase": "implementation", "status": "in_progress"})
        parent.event({"kind": "worker", "phase": "implementation", "step_id": "worker",
                      "status": "in_progress"})
        with self.assertRaises(po.ParentRunError) as incomplete:
            parent.finish("COMPLETED")
        self.assertEqual(incomplete.exception.error_code, "PARENT_COMPLETION_INCOMPLETE")
        # The refusal names exactly what to record, so the lead can fix it in one step.
        self.assertIn("phase implementation is in_progress", str(incomplete.exception))
        self.assertIn("worker implementation/worker is in_progress", str(incomplete.exception))
        self.assertEqual(po.status(parent.run_id, repo_root=self.records)["status"], "IN_PROGRESS")
        parent.event({"kind": "phase", "phase": "implementation", "status": "completed"})
        with self.assertRaises(po.ParentRunError):
            parent.finish("COMPLETED")
        parent.event({"kind": "worker", "phase": "implementation", "step_id": "worker",
                      "status": "completed"})
        self.assertEqual(parent.finish("COMPLETED")["outcome"], "completed")

    def test_completed_requires_mandatory_independent_review(self):
        for route in ("CLAUDE_CODEX", "CODEX_CLAUDE_CODEX"):
            with self.subTest(route=route):
                parent = po.ParentRun(worktree=self.target, repo_root=self.records)
                parent.event({"kind": "phase", "phase": "implementation", "status": "completed",
                              "route": route})
                parent.event({"kind": "worker", "phase": "implementation", "step_id": "claude_worker",
                              "status": "completed"})
                with self.assertRaises(po.ParentRunError) as missing:
                    parent.finish("COMPLETED")
                self.assertEqual(missing.exception.error_code, "PARENT_REVIEW_MISSING")
                self.assertEqual(po.status(parent.run_id, repo_root=self.records)["status"], "IN_PROGRESS")
                parent.event({"kind": "worker", "phase": "implementation", "step_id": "codex_reviewer",
                              "status": "failed"})
                with self.assertRaises(po.ParentRunError):
                    parent.finish("COMPLETED")
                # The run can still end honestly without the review.
                self.assertEqual(parent.finish("PARTIAL")["outcome"], "partial")

        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        parent.event({"kind": "phase", "phase": "implementation", "status": "completed",
                      "route": "CLAUDE_CODEX"})
        for step in ("claude_worker", "codex_reviewer"):
            parent.event({"kind": "worker", "phase": "implementation", "step_id": step,
                          "status": "completed"})
        # A superseded blocked attempt claims no result and needs no review.
        parent.event({"kind": "phase", "phase": "first-attempt", "status": "blocked",
                      "route": "CODEX_CLAUDE_CODEX", "blocker_code": "REVIEW_FINDINGS"})
        self.assertEqual(parent.finish("COMPLETED")["outcome"], "completed")

    def test_required_review_steps_follow_route_topology(self):
        self.assertEqual(po.required_review_steps("CLAUDE_CODEX"), {"codex_reviewer"})
        self.assertEqual(po.required_review_steps("CODEX_CLAUDE_CODEX"), {"codex_reviewer"})
        self.assertEqual(po.required_review_steps("CODEX"), set())
        self.assertEqual(po.required_review_steps("CLAUDE"), set())
        with self.assertRaises(po.ParentRunError) as unavailable:
            po.required_review_steps("CLAUDE_CODEX", policy_path=self.records / "missing.yaml")
        self.assertEqual(unavailable.exception.error_code, "PARENT_POLICY_UNAVAILABLE")

    def test_simultaneous_parent_events_do_not_lose_phase_updates(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        original = provenance.update_parent_orchestration

        def delayed_update(*args, **kwargs):
            time.sleep(0.02)
            return original(*args, **kwargs)

        with unittest.mock.patch.object(provenance, "update_parent_orchestration", delayed_update):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda index: parent.event({"kind": "phase",
                    "phase": f"phase-{index}", "status": "completed"}), range(8)))
        phases = provenance.get_run(self.records, parent.run_id)["parent_orchestration"]["phases"]
        self.assertEqual({phase["id"] for phase in phases}, {f"phase-{index}" for index in range(8)})
        parent.finish("COMPLETED")

    def test_simultaneous_parent_processes_preserve_all_events(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        env = {**os.environ, "PI_ORCHESTRATOR_STATE_DIR": str(self.records / "provenance")}
        children = [subprocess.Popen([sys.executable, str(ROOT / "tools/parent_orchestration.py"),
                   "event", parent.run_id, "--owner", parent.owner_id], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True, env=env)
                   for _ in range(4)]
        for index, child in enumerate(children):
            output, error = child.communicate(json.dumps({"kind": "phase",
                "phase": f"parallel-{index}", "status": "completed"}), timeout=10)
            self.assertEqual(child.returncode, 0, error or output)
        phases = provenance.get_run(self.records, parent.run_id)["parent_orchestration"]["phases"]
        self.assertEqual({phase["id"] for phase in phases}, {f"parallel-{index}" for index in range(4)})

    def test_busy_parent_lock_fails_with_bounded_recoverable_error(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        with unittest.mock.patch.object(po, "LOCK_WAIT_SECONDS", 0.05):
            with po._run_lock(self.records, parent.run_id):
                with self.assertRaises(po.ParentRunError) as busy:
                    parent.event({"kind": "phase", "phase": "inspection", "status": "in_progress"})
        self.assertEqual(busy.exception.error_code, "PARENT_LOCK_TIMEOUT")
        self.assertEqual(po.status(parent.run_id, repo_root=self.records)["status"], "IN_PROGRESS")

    def test_review_rejection_can_be_recorded_then_repaired_and_verified(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        for phase, status in (("implementation", "completed"), ("review-1", "blocked"),
                              ("repair", "completed"), ("review-2", "completed"),
                              ("verification", "completed")):
            parent.event({"kind": "phase", "phase": phase, "status": status,
                          **({"blocker_code": "REVIEW_FINDINGS"} if status == "blocked" else {})})
        record = parent.finish("COMPLETED")
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual([phase["id"] for phase in record["parent_orchestration"]["phases"]],
                         ["implementation", "review-1", "repair", "review-2", "verification"])

    def test_secret_like_event_rejected_without_persisting(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        token = "ghp_" + "x" * 30
        with self.assertRaises(po.ParentRunError):
            parent.event({"kind": "worker", "phase": "S1", "step_id": "worker",
                          "model": token, "status": "blocked"})
        record = parent.finish("BLOCKED", "MODEL_UNAVAILABLE")
        self.assertNotIn(token, json.dumps(record))

    def test_live_activity_records_only_safe_worker_metadata(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        parent.event({"kind": "activity", "status": "started", "tool": "spawn_subagent",
                      "task_id": "task-1", "step_id": "implementation"})
        parent.event({"kind": "activity", "status": "spawned", "tool": "spawn_subagent",
                      "task_id": "task-1", "step_id": "implementation", "subagent_id": "agent-42"})
        activity = provenance.get_run(self.records, parent.run_id)["parent_orchestration"]["activity"]
        self.assertEqual([item["status"] for item in activity], ["started", "spawned"])
        self.assertEqual(activity[1]["subagent_id"], "agent-42")
        self.assertTrue(activity[1]["at"])
        with self.assertRaises(po.ParentRunError):
            parent.event({"kind": "activity", "status": "started", "tool": "spawn_subagent",
                          "task_id": "ghp_" + "x" * 30})

    def test_numbered_interactions_link_directives_feedback_and_decisions(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        first = parent.event({"kind": "interaction", "type": "directive",
                              "task_id": "task-1", "step_id": "worker",
                              "summary": "Inspect the save path and report the race."})
        self.assertEqual(first["interactions"][-1]["sequence"], 1)
        second = parent.event({"kind": "interaction", "type": "feedback",
                               "task_id": "task-1", "step_id": "worker",
                               "subagent_id": "agent-42", "reply_to": 1,
                               "summary": "Found two writes that use stale metadata."})
        self.assertEqual(second["interactions"][-1]["reply_to"], 1)
        third = parent.event({"kind": "interaction", "type": "decision",
                              "task_id": "task-1", "step_id": "worker", "reply_to": 2,
                              "summary": "Add a regression test for the reported race."})
        self.assertEqual([item["sequence"] for item in third["interactions"]], [1, 2, 3])
        for bad in ("ghp_" + "x" * 30, "line one\nline two", "x" * 181):
            with self.assertRaises(po.ParentRunError):
                parent.event({"kind": "interaction", "type": "feedback",
                              "task_id": "task-1", "step_id": "worker", "summary": bad})
        with self.assertRaises(po.ParentRunError):
            parent.event({"kind": "interaction", "type": "decision",
                          "task_id": "task-1", "step_id": "worker", "reply_to": 9,
                          "summary": "Continue with the next step."})
        saved = provenance.get_run(self.records, parent.run_id)["parent_orchestration"]["interactions"]
        self.assertEqual(len(saved), 3)

    def test_real_first_inspection_event_and_retry_are_atomic(self):
        started = po.start(worktree=self.target, repo_root=self.records)
        run_id = started["run_id"]
        self.assertEqual(started["status"], "IN_PROGRESS")
        self.assertEqual(po.status(run_id, repo_root=self.records)["target_repository"],
                         "Examplecorp/examplelab")
        event = {"kind": "phase", "phase": "inspection", "status": "in_progress"}
        first = po.record_event(run_id, event, repo_root=self.records)
        self.assertIsNone(first["phases"][0]["route"])
        before = provenance.run_path(self.records, run_id).read_bytes()
        self.assertEqual(po.record_event(run_id, event, repo_root=self.records), first)
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        with self.assertRaises(po.ParentRunError) as raised:
            po.record_event(run_id, {"kind": "phase", "phase": "inspection",
                                     "status": "started"}, repo_root=self.records)
        self.assertEqual(raised.exception.error_code, "PARENT_PHASE_STATUS_INVALID")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        self.assertEqual(provenance.validate_run_record(provenance.get_run(self.records, run_id),
                         provenance.load_schema(self.records)), [])
        po.record_event(run_id, {"kind": "phase", "phase": "inspection",
                                 "status": "completed"}, repo_root=self.records)
        po.finish(run_id, "COMPLETED", repo_root=self.records)

    def test_explicit_route_escalation_preserves_history_and_review(self):
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        def event(data):
            return po.record_event(run_id, data, repo_root=self.records)
        event({"kind": "phase", "phase": "repair", "status": "in_progress", "route": "CODEX"})
        event({"kind": "worker", "phase": "repair", "step_id": "codex_reviewer", "status": "in_progress"})
        event({"kind": "worker", "phase": "repair", "step_id": "codex_reviewer", "status": "completed"})
        self.assertEqual(po.status(run_id, repo_root=self.records)["reroute_phases"], ["repair"])
        before = provenance.run_path(self.records, run_id).read_bytes()
        with self.assertRaises(po.ParentRunError) as conflict:
            event({"kind": "phase", "phase": "repair", "status": "in_progress", "route": "CLAUDE_CODEX"})
        self.assertEqual(conflict.exception.error_code, "PARENT_EVENT_CONFLICT")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        escalation = {"kind": "escalation", "phase": "repair", "from_route": "CODEX",
                      "to_route": "CLAUDE_CODEX", "reason": "REVIEW_FINDINGS"}
        event(escalation)
        with self.assertRaises(po.ParentRunError) as pending:
            po.finish(run_id, "COMPLETED", repo_root=self.records)
        self.assertEqual(pending.exception.error_code, "PARENT_ROUTE_RECHECK_REQUIRED")
        event({"kind": "phase", "phase": "repair", "status": "completed", "route": "CLAUDE_CODEX"})
        self.assertEqual(po.status(run_id, repo_root=self.records)["reroute_phases"], [])
        phase = provenance.get_run(self.records, run_id)["parent_orchestration"]["phases"][0]
        self.assertEqual(phase["route"], "CODEX")
        self.assertEqual(phase["escalations"][0]["to_route"], "CLAUDE_CODEX")
        with self.assertRaises(po.ParentRunError) as review:
            po.finish(run_id, "COMPLETED", repo_root=self.records)
        self.assertEqual(review.exception.error_code, "PARENT_REVIEW_MISSING")
        with self.assertRaises(po.ParentRunError) as missing_start:
            event({"kind": "worker", "phase": "repair", "step_id": "codex_reviewer", "status": "completed"})
        self.assertEqual(missing_start.exception.error_code, "PARENT_WORKER_TRANSITION_INVALID")
        event({"kind": "worker", "phase": "repair", "step_id": "codex_reviewer", "status": "in_progress"})
        event({"kind": "worker", "phase": "repair", "step_id": "codex_reviewer", "status": "completed"})
        reviewers = [w for w in provenance.get_run(self.records, run_id)["parent_orchestration"]["phases"][0]["workers"]
                     if w["step_id"] == "codex_reviewer"]
        self.assertEqual([w["route_epoch"] for w in reviewers], [0, 1])
        po.finish(run_id, "COMPLETED", repo_root=self.records)

    def test_invalid_escalation_does_not_change_route(self):
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        po.record_event(run_id, {"kind": "phase", "phase": "repair", "status": "in_progress",
                                 "route": "CODEX"}, repo_root=self.records)
        before = provenance.run_path(self.records, run_id).read_bytes()
        for to_route, reason in (("CLAUDE", "AMBIGUITY"), ("CLAUDE_CODEX", "INVENTED")):
            with self.assertRaises(po.ParentRunError) as error:
                po.record_event(run_id, {"kind": "escalation", "phase": "repair", "from_route": "CODEX",
                                         "to_route": to_route, "reason": reason}, repo_root=self.records)
            self.assertEqual(error.exception.error_code, "PARENT_ESCALATION_INVALID")
            self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)

    def test_escalation_refuses_in_flight_worker_without_changing_provenance(self):
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        po.record_event(run_id, {"kind": "phase", "phase": "repair", "status": "in_progress",
                                 "route": "CLAUDE_CODEX"}, repo_root=self.records)
        po.record_event(run_id, {"kind": "worker", "phase": "repair", "step_id": "codex_reviewer",
                                 "status": "in_progress"}, repo_root=self.records)
        before = provenance.run_path(self.records, run_id).read_bytes()
        with self.assertRaises(po.ParentRunError) as blocked:
            po.record_event(run_id, {"kind": "escalation", "phase": "repair",
                                     "from_route": "CLAUDE_CODEX", "to_route": "CODEX_CLAUDE_CODEX",
                                     "reason": "REVIEW_FINDINGS"}, repo_root=self.records)
        self.assertEqual(blocked.exception.error_code, "PARENT_ESCALATION_WORKERS_IN_FLIGHT")
        self.assertEqual(provenance.run_path(self.records, run_id).read_bytes(), before)
        po.record_event(run_id, {"kind": "worker", "phase": "repair", "step_id": "codex_reviewer",
                                 "status": "completed"}, repo_root=self.records)
        po.record_event(run_id, {"kind": "escalation", "phase": "repair",
                                 "from_route": "CLAUDE_CODEX", "to_route": "CODEX_CLAUDE_CODEX",
                                 "reason": "REVIEW_FINDINGS"}, repo_root=self.records)

    def test_routing_scores_and_worker_capacity_reach_viewer_without_task_prose(self):
        run_id = po.start(worktree=self.target, repo_root=self.records)["run_id"]
        effective = self.route["classification"]["effective"]
        by_step = {step["id"]: step for step in self.route["execution_plan"]["steps"]}
        routing = {
            "task_type": effective["task_type"], "ambiguity": effective["ambiguity"],
            "ambiguity_dimensions": {axis: effective["ambiguity"] if axis == "contract" else 2
                                     for axis in engine.AMBIGUITY_DIMENSIONS},
            "work_profile": {mode: 5 for mode in ("search", "analysis", "writing", "implementation",
                                                    "verification", "coordination")},
            "failure_cost": effective["failure_cost"],
            "semantic_correctness": effective["semantic_correctness"],
            "diagnosis_required": effective["diagnosis_required"],
            "implementation_scope": effective["implementation_scope"],
            "signals": ["cross_layer"] if effective["cross_layer"] else [],
            "matches": self.route["matches"]["effective"],
            "fallback_applied": self.route["fallback_applied"],
            "assignments": [{"step_id": item["step_id"], "role": by_step[item["step_id"]]["role"],
                             "family": by_step[item["step_id"]]["family"],
                             "capacity_tier": item["assigned_capacity_tier"],
                             "ambiguity_capacity": item["selected_model_profile"]["ambiguity_capacity"],
                             "rule": item["rule"]} for item in self.route["capacity_assignments"]],
        }
        po.record_event(run_id, {"kind": "phase", "phase": "proof", "status": "in_progress",
                                 "route": self.route["selected_route"], "routing": routing}, repo_root=self.records)
        record = provenance.get_run(self.records, run_id)
        visible = watch.public_run(record)["phases"][0]["routing"]
        self.assertEqual(visible["ambiguity"], effective["ambiguity"])
        self.assertEqual(visible["assignments"][1]["ambiguity_capacity"], 9)
        self.assertNotIn("goal", visible)
        self.assertNotIn("repository_evidence", visible)
        with self.assertRaises(po.ParentRunError):
            po.record_event(run_id, {"kind": "phase", "phase": "proof", "status": "in_progress",
                                     "route": self.route["selected_route"],
                                     "routing": {**routing, "goal": "private task text"}}, repo_root=self.records)

    def test_resume_status_rechecks_repository_and_never_reopens_terminal_run(self):
        started = po.start(worktree=self.target, repo_root=self.records)
        run_id = started["run_id"]
        self.assertEqual(po.status(run_id, repo_root=self.records, worktree=self.target)["status"],
                         "IN_PROGRESS")
        subprocess.run(["git", "-C", str(self.target), "remote", "set-url", "origin",
                        "git@github.com:Examplecorp/other.git"], check=True)
        with self.assertRaises(po.ParentRunError) as raised:
            po.status(run_id, repo_root=self.records, worktree=self.target)
        self.assertEqual(raised.exception.error_code, "SESSION_REPOSITORY_MISMATCH")
        self.assertEqual(po.finish(run_id, "BLOCKED", repo_root=self.records)["outcome"], "blocked")
        self.assertEqual(po.status(run_id, repo_root=self.records, worktree=self.target)["status"],
                         "BLOCKED")

    def test_waves_record_membership_concurrency_and_parallel_speedup(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        for task in ("A", "B"):
            parent.event({"kind": "phase", "phase": task, "status": "in_progress", "route": "CODEX"})
        parent.event({"kind": "wave", "wave_id": "wave-1", "task_ids": ["A", "B"], "status": "declared"})
        with self.assertRaises(po.ParentRunError) as conflict:
            parent.event({"kind": "wave", "wave_id": "wave-1", "task_ids": ["A"], "status": "declared"})
        self.assertEqual(conflict.exception.error_code, "PARENT_EVENT_CONFLICT")
        with self.assertRaises(po.ParentRunError) as unknown:
            parent.event({"kind": "wave", "wave_id": "wave-9", "task_ids": ["A"], "status": "integrated"})
        self.assertEqual(unknown.exception.error_code, "PARENT_WAVE_TRANSITION_INVALID")
        # Two 4 s workers overlapped completely: 4 s with at least one in flight.
        for _ in range(2):
            parent.event({"kind": "usage", "backend": "pi-subagent", "duration_ms": 4000})
        parent.event({"kind": "concurrency", "peak_workers": 2, "active_ms": 4000})
        for status in ("integrating", "integrated"):
            parent.event({"kind": "wave", "wave_id": "wave-1", "task_ids": ["A", "B"], "status": status})
        with self.assertRaises(po.ParentRunError) as backwards:
            parent.event({"kind": "wave", "wave_id": "wave-1", "task_ids": ["A", "B"], "status": "declared"})
        self.assertEqual(backwards.exception.error_code, "PARENT_WAVE_TRANSITION_INVALID")
        for task in ("A", "B"):
            parent.event({"kind": "phase", "phase": task, "status": "completed"})
        record = provenance.get_run(self.records, parent.run_id)
        orchestration = record["parent_orchestration"]
        self.assertEqual(orchestration["waves"], [{"id": "wave-1", "task_ids": ["A", "B"], "status": "integrated"}])
        self.assertEqual({phase["id"]: phase.get("wave_id") for phase in orchestration["phases"]},
                         {"A": "wave-1", "B": "wave-1"})
        self.assertEqual(orchestration["context"]["peak_concurrent_workers"], 2)
        self.assertEqual(orchestration["context"]["worker_active_ms"], 4000)
        metrics = record["orchestration_metrics"]
        self.assertEqual(metrics["delegation_topology"], "two_workers")
        self.assertEqual(metrics["worker_execution_ms"], 8000)
        started = po.datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
        later = started + timedelta(seconds=10)
        final = po.delegation_metrics(record, orchestration, now=later)
        self.assertEqual(final["wall_clock_ms"], 10000)
        self.assertEqual(final["estimated_serial_time_ms"], 14000)
        finished = parent.finish("COMPLETED")
        self.assertGreater(finished["orchestration_metrics"]["parallel_speedup"], 1)

    def test_sequential_workers_have_unit_speedup_and_direct_runs_no_worker_time(self):
        record = {"started_at": "2026-10-14T00:00:00Z"}
        later = po.datetime(2026, 10, 14, 0, 0, 10, tzinfo=po.timezone.utc)
        sequential = {"context": {"peak_concurrent_workers": 1, "worker_active_ms": 6000,
                                  "worker_usage": {"claude-code": {"duration_ms": 6000}}}}
        metrics = po.delegation_metrics(record, sequential, now=later)
        self.assertEqual((metrics["delegation_topology"], metrics["estimated_serial_time_ms"]), ("one_worker", 10000))
        self.assertEqual(po.delegation_metrics(record, {"context": po.empty_context()}, now=later),
                         {"delegation_topology": "direct", "wall_clock_ms": 10000,
                          "worker_model_mix": "none", "spawned_agents": 0,
                          "lead_model": None, "total_tokens": None})

    def test_observed_agents_models_and_tokens_are_aggregated_at_finish(self):
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        parent.event({"kind": "phase", "phase": "task", "status": "in_progress"})
        parent.event({"kind": "worker", "phase": "task", "step_id": "build",
                      "status": "completed", "model": "openai-codex/gpt-6.1-sol"})
        parent.event({"kind": "spawn"})
        parent.event({"kind": "lead_usage", "model": "openai-codex/gpt-6.1-sol",
                      "phase": "task", "task_type": "implementation",
                      "input_tokens": 100, "output_tokens": 40, "cached_tokens": 25,
                      "total_tokens": 165})
        parent.event({"kind": "usage", "backend": "pi-subagent", "duration_ms": 2000,
                      "input_tokens": 70, "output_tokens": 20, "cached_tokens": 10,
                      "total_tokens": 100})
        parent.event({"kind": "phase", "phase": "task", "status": "completed"})
        metrics = parent.finish("COMPLETED")["orchestration_metrics"]
        self.assertEqual(metrics["spawned_agents"], 1)
        self.assertEqual(metrics["lead_model"], "openai-codex/gpt-6.1-sol")
        self.assertEqual(metrics["worker_model_mix"], "same_model")
        self.assertEqual(metrics["total_tokens"], 265)
        self.assertEqual(metrics["worker_cached_tokens"], 10)
        persisted = provenance.get_run(self.records, parent.run_id)
        self.assertEqual(persisted["parent_orchestration"]["context"]["lead_calls"], [{
            "sequence": 1, "phase": "task", "task_type": "implementation",
            "model": "openai-codex/gpt-6.1-sol", "turn_index": None, "duration_ms": None,
            "input_tokens": 100,
            "output_tokens": 40, "cached_tokens": 25, "total_tokens": 165,
        }])
        response = po.terminal_response(persisted, repo_root=self.records)
        self.assertEqual(response["trace_path"], str(provenance.run_path(self.records, parent.run_id)))
        self.assertNotIn("trace_path", persisted)

    def test_lead_usage_validates_with_and_without_estimated_cost(self):
        schema = provenance.load_schema(self.records)
        for cost in (None, 0.5):
            with self.subTest(cost=cost):
                parent = po.ParentRun(worktree=self.target, repo_root=self.records)
                event = {"kind": "lead_usage", "model": "openai-codex/gpt-6.1-sol", "input_tokens": 10,
                         "output_tokens": 5, "cached_tokens": 3, "total_tokens": 18}
                if cost is not None:
                    event["estimated_cost_usd"] = cost
                parent.event(event)
                parent.event({"kind": "usage", "backend": "claude-code", "input_tokens": 4, "output_tokens": 2,
                              "cache_write_tokens": 7, "cached_tokens": 9})
                record = parent.finish("PARTIAL")
                self.assertEqual(provenance.validate_run_record(record, schema), [])
                context = record["parent_orchestration"]["context"]
                self.assertEqual(context["lead_usage"].get("estimated_cost_usd"), cost)
                self.assertEqual(context["worker_usage"]["claude-code"]["cache_write_tokens"], 7)
        with self.assertRaises(po.ParentRunError):
            po.ParentRun(worktree=self.target, repo_root=self.records).event(
                {"kind": "lead_usage", "model": "m/x", "estimated_cost_usd": -1})

    def test_cost_complete_flags_track_unpriced_zero_cost_and_cache_writes(self):
        schema = provenance.load_schema(self.records)
        parent = po.ParentRun(worktree=self.target, repo_root=self.records)
        parent.event({"kind": "usage", "backend": "claude-code", "duration_ms": 5})  # AskClaude: no cost
        parent.event({"kind": "usage", "backend": "pi-subagent", "input_tokens": 5, "cache_write_tokens": 3,
                      "estimated_cost_usd": 0})
        parent.event({"kind": "usage", "backend": "pi-subagent", "input_tokens": 5, "estimated_cost_usd": 0.1})
        parent.event({"kind": "lead_usage", "model": "m/lead", "input_tokens": 4, "cache_write_tokens": 2,
                      "estimated_cost_usd": 0.2})
        record = parent.finish("PARTIAL")
        self.assertEqual(provenance.validate_run_record(record, schema), [])
        context = record["parent_orchestration"]["context"]
        self.assertFalse(context["worker_usage"]["claude-code"]["cost_complete"])
        self.assertFalse(context["worker_usage"]["pi-subagent"]["cost_complete"])  # one zero-cost event with tokens
        self.assertEqual(context["lead_usage"]["cache_write_tokens"], 2)
        self.assertTrue(context["lead_usage"]["cost_complete"])

    def test_partial_worker_usage_does_not_claim_complete_token_total(self):
        record = {"started_at": "2026-10-14T00:00:00Z"}
        parent = {"context": {"lead_usage": {"input_tokens": 10, "output_tokens": 5},
                              "worker_usage": {"claude-code": {"duration_ms": 1000}}}}
        metrics = po.delegation_metrics(record, parent,
                                        now=po.datetime(2026, 10, 14, 0, 0, 2, tzinfo=po.timezone.utc))
        self.assertIsNone(metrics["total_tokens"])


if __name__ == "__main__":
    unittest.main()
