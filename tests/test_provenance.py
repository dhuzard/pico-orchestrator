#!/usr/bin/env python3
"""Deterministic tests for tools/provenance.py (run-record lifecycle).

Run with: python3 -m unittest discover -s tests -p 'test_*.py'

Every test operates against an isolated temporary repo root (a copy of the
real schemas/run-record.schema.json under a fresh provenance/runs/
directory), so no test ever reads or writes this repository's real
provenance/runs/ directory.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import provenance  # noqa: E402


class TokenUsageSummaryTest(unittest.TestCase):
    def test_recorded_task_type_enum_matches_task_schema(self):
        run_schema = json.loads((REPO_ROOT / "schemas" / "run-record.schema.json").read_text())
        task_schema = json.loads((REPO_ROOT / "schemas" / "task.schema.json").read_text())
        recorded = run_schema["properties"]["parent_orchestration"]["properties"]["context"]["properties"]["lead_calls"]["items"]["properties"]["task_type"]["enum"]
        self.assertEqual(set(recorded), {None, *task_schema["properties"]["task_type"]["enum"]})

    def test_groups_calls_and_keeps_missing_usage_explicit(self):
        records = [
            {"outcome": "completed", "orchestration_metrics": {"wall_clock_ms": 1200},
             "parent_orchestration": {"context": {"lead_calls": [
                 {"task_type": "documentation", "input_tokens": 100, "output_tokens": 10,
                  "cached_tokens": 70, "total_tokens": 110},
                 {"task_type": None, "input_tokens": 20, "output_tokens": 2,
                  "cached_tokens": None, "total_tokens": None}]}}},
            {"outcome": "blocked", "parent_orchestration": {"context": {"lead_calls": [
                {"task_type": "documentation", "input_tokens": 30, "output_tokens": 3,
                 "cached_tokens": 15, "total_tokens": 33}]}}},
        ]
        result = provenance.summarize_token_usage(records)
        self.assertEqual(result["wall_clock_ms"], 1200)
        self.assertEqual(result["runs_without_wall_clock"], 1)
        self.assertEqual(result["runs_without_lead_calls"], 0)
        self.assertEqual(result["outcomes"], {"completed": 1, "blocked": 1})
        self.assertEqual(result["lead_by_task_type"]["documentation"]["input_tokens"], 130)
        self.assertEqual(result["lead_by_task_type"]["documentation"]["cached_tokens"], 85)
        self.assertEqual(result["lead_by_task_type"]["unattributed"]["missing_total_tokens_calls"], 1)


def make_repo_root(tmp_root: Path) -> Path:
    repo = tmp_root / "repo"
    (repo / "schemas").mkdir(parents=True)
    (repo / "provenance" / "runs").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "schemas" / "run-record.schema.json",
        repo / "schemas" / "run-record.schema.json",
    )
    return repo


SAMPLE_ROUTE_RESULT = {
    "ok": True,
    "selected_route": "CLAUDE_CODEX",
    "execution_plan": {
        "schema_version": 3,
        "independent_review_required": True,
        "steps": [
            {
                "id": "claude_worker",
                "order": 1,
                "role": "worker",
                "family": "claude",
                "intent": "implementation",
                "capacity_tier": "standard",
                "reasoning_effort": "high",
                "context_strategy": "full",
                "fresh_context": True,
                "mutation_permission": "allowed",
                "depends_on": [],
                "independent_review": False,
            },
            {
                "id": "codex_reviewer",
                "order": 2,
                "role": "reviewer",
                "family": "codex",
                "intent": "review",
                "capacity_tier": "standard",
                "reasoning_effort": "high",
                "context_strategy": "full",
                "fresh_context": True,
                "mutation_permission": "forbidden",
                "depends_on": ["claude_worker"],
                "independent_review": True,
            },
        ],
    },
}


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_repo_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_run_creates_valid_in_progress_record(self):
        record = provenance.start_run(
            self.repo_root,
            run_id="run-test-1",
            backlog_id="ORCH-014",
            github_issue=None,
            user_objective="Fix the metadata persistence bug.",
            route_task_result=SAMPLE_ROUTE_RESULT,
            starting_commit="abc123",
        )
        self.assertEqual(record["outcome"], "in_progress")
        self.assertEqual(record["ended_at"], None)
        self.assertEqual(record["steps"], [])
        fetched = provenance.get_run(self.repo_root, "run-test-1")
        self.assertEqual(fetched, record)

    def test_external_state_dir_keeps_mutable_runs_outside_repo_checkout(self):
        state = Path(self._tmp.name) / "state"
        with patch.dict("os.environ", {provenance.STATE_ENV: str(state)}):
            record = provenance.start_run(
                self.repo_root,
                run_id="run-external-state",
                backlog_id=None,
                github_issue=None,
                user_objective="state separation",
                route_task_result=None,
                starting_commit=None,
            )
            external = state / "runs" / "run-external-state.json"
            self.assertTrue(external.exists())
            self.assertFalse((self.repo_root / "provenance" / "runs" / "run-external-state.json").exists())
            self.assertEqual(provenance.get_run(self.repo_root, record["run_id"])["run_id"], record["run_id"])
            trace = provenance.build_trace_report(self.repo_root, record)
            self.assertEqual(trace["trace_path"], str(external.resolve()))

    def test_relative_external_state_dir_is_rejected(self):
        with patch.dict("os.environ", {provenance.STATE_ENV: "relative/state"}):
            with self.assertRaises(provenance.ProvenanceError):
                provenance.runs_dir(self.repo_root)

    def test_start_run_stores_route_task_result_verbatim(self):
        record = provenance.start_run(
            self.repo_root,
            run_id="run-verbatim",
            backlog_id=None,
            github_issue=None,
            user_objective="obj",
            route_task_result=SAMPLE_ROUTE_RESULT,
            starting_commit=None,
        )
        self.assertEqual(record["route_task_result"], SAMPLE_ROUTE_RESULT)

    def test_start_run_duplicate_run_id_rejected(self):
        provenance.start_run(
            self.repo_root, run_id="dup", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunAlreadyExistsError):
            provenance.start_run(
                self.repo_root, run_id="dup", backlog_id=None, github_issue=None,
                user_objective="obj2", route_task_result=None, starting_commit=None,
            )

    def test_get_run_missing_raises(self):
        with self.assertRaises(provenance.RunNotFoundError):
            provenance.get_run(self.repo_root, "nope")

    # -- steps: worker attribution / explicit-null semantics ------------------

    def test_add_step_defaults_unspecified_fields_to_explicit_null(self):
        provenance.start_run(
            self.repo_root, run_id="run-a", backlog_id="ORCH-001", github_issue=None,
            user_objective="obj", route_task_result=SAMPLE_ROUTE_RESULT, starting_commit=None,
        )
        record = provenance.add_step(self.repo_root, "run-a", task_id="T1")
        step = record["steps"][0]
        self.assertEqual(step["task_id"], "T1")
        self.assertIsNone(step["model"])
        self.assertIsNone(step["backend"])
        self.assertIsNone(step["worker_identifier"])
        self.assertIsNone(step["capacity_tier"])
        self.assertIsNone(step["reasoning_setting"])
        self.assertIsNone(step["context_strategy"])
        self.assertIsNone(step["context_window_tier"])
        self.assertEqual(step["status"], "pending")

    def test_add_step_records_full_worker_attribution(self):
        provenance.start_run(
            self.repo_root, run_id="run-b", backlog_id="ORCH-014", github_issue=7,
            user_objective="obj", route_task_result=SAMPLE_ROUTE_RESULT, starting_commit="c1",
        )
        record = provenance.add_step(
            self.repo_root,
            "run-b",
            task_id="T2 implementation",
            execution_plan_step_id="claude_worker",
            role="worker",
            family="claude",
            backend="AskClaude",
            model="sonnet",
            capacity_tier="high",
            reasoning_setting="high",
            context_strategy="full",
            context_window_tier="extended",
            fresh_context=True,
            mutation_permission="allowed",
            worker_identifier="session-xyz",
            status="completed",
            outcome="repaired once",
            files_changed=["tools/foo.py"],
            validation_performed=["pytest tests/"],
            review_findings=None,
            repair_of=None,
        )
        step = record["steps"][0]
        self.assertEqual(step["model"], "sonnet")
        self.assertEqual(step["family"], "claude")
        self.assertEqual(step["capacity_tier"], "high")
        self.assertEqual(step["reasoning_setting"], "high")
        self.assertEqual(step["context_strategy"], "full")
        self.assertEqual(step["context_window_tier"], "extended")
        self.assertEqual(step["outcome"], "repaired once")
        self.assertEqual(step["files_changed"], ["tools/foo.py"])

    def test_add_step_accepts_economy_capacity_provenance(self):
        provenance.start_run(
            self.repo_root, run_id="run-economy", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=SAMPLE_ROUTE_RESULT, starting_commit="c1",
        )
        record = provenance.add_step(
            self.repo_root, "run-economy", task_id="T-economy",
            family="claude", capacity_tier="economy", status="completed",
        )
        self.assertEqual(record["steps"][0]["capacity_tier"], "economy")

    def test_add_step_unknown_field_rejected(self):
        provenance.start_run(
            self.repo_root, run_id="run-c", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.add_step(self.repo_root, "run-c", task_id="T1", not_a_real_field="x")

    def test_add_step_requires_task_id(self):
        provenance.start_run(
            self.repo_root, run_id="run-d", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.add_step(self.repo_root, "run-d", task_id="")

    def test_add_step_with_review_findings_and_unknown_model(self):
        provenance.start_run(
            self.repo_root, run_id="run-e", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.add_step(
            self.repo_root,
            "run-e",
            task_id="T3 review",
            role="reviewer",
            family="codex",
            model=None,  # explicit unknown/unobservable -- never guessed
            status="completed",
            outcome="2 CORRECTNESS findings, 1 TEST_GAP",
            review_findings=[
                {"category": "CORRECTNESS", "description": "off-by-one in range check"},
                {"category": "CORRECTNESS", "description": "missing null check"},
                {"category": "TEST_GAP", "description": "no regression test for the fix"},
            ],
        )
        step = record["steps"][0]
        self.assertIsNone(step["model"])
        self.assertEqual(len(step["review_findings"]), 3)

    def test_update_step_transitions_pending_to_completed(self):
        provenance.start_run(
            self.repo_root, run_id="run-f", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        provenance.add_step(self.repo_root, "run-f", task_id="T1", status="in_progress")
        record = provenance.update_step(self.repo_root, "run-f", "T1", status="completed", outcome="accepted")
        self.assertEqual(record["steps"][0]["status"], "completed")
        self.assertEqual(record["steps"][0]["outcome"], "accepted")

    def test_update_step_missing_task_id_raises(self):
        provenance.start_run(
            self.repo_root, run_id="run-g", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.update_step(self.repo_root, "run-g", "does-not-exist", status="completed")

    # -- observational telemetry ---------------------------------------------

    def test_start_run_and_steps_default_telemetry_to_explicit_null(self):
        record = provenance.start_run(
            self.repo_root, run_id="run-telemetry-null", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        self.assertIsNone(record["routing_telemetry"])
        self.assertIsNone(record["orchestration_metrics"])
        record = provenance.add_step(self.repo_root, "run-telemetry-null", task_id="T1")
        self.assertIsNone(record["steps"][0]["telemetry"])

    def test_structured_telemetry_is_normalized_without_touching_route_result(self):
        route_result = dict(SAMPLE_ROUTE_RESULT)
        record = provenance.start_run(
            self.repo_root,
            run_id="run-telemetry",
            backlog_id=None,
            github_issue=None,
            user_objective="obj",
            route_task_result=route_result,
            starting_commit=None,
            routing_telemetry={"source": "route_task.py", "duration_ms": 12.5, "input_bytes": 123},
        )
        self.assertEqual(record["route_task_result"], route_result)
        self.assertNotIn("routing_telemetry", record["route_task_result"])
        self.assertEqual(record["routing_telemetry"]["schema_version"], 1)
        self.assertEqual(record["routing_telemetry"]["duration_ms"], 12.5)
        self.assertIsNone(record["routing_telemetry"]["output_tokens"])

        record = provenance.add_step(
            self.repo_root,
            "run-telemetry",
            task_id="T1",
            telemetry={"source": "spawn_subagent", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        self.assertEqual(record["steps"][0]["telemetry"]["total_tokens"], 15)
        self.assertIsNone(record["steps"][0]["telemetry"]["estimated_cost_usd"])

    def test_telemetry_secret_like_and_transcript_keys_rejected(self):
        with self.assertRaises(provenance.ProvenanceError):
            provenance.start_run(
                self.repo_root,
                run_id="run-bad-routing-telemetry",
                backlog_id=None,
                github_issue=None,
                user_objective="obj",
                route_task_result=None,
                starting_commit=None,
                routing_telemetry={"api_key": "nope"},
            )

        provenance.start_run(
            self.repo_root, run_id="run-bad-step-telemetry", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.add_step(
                self.repo_root,
                "run-bad-step-telemetry",
                task_id="T1",
                telemetry={"prompt_transcript": "do not persist"},
            )

    def test_route_task_result_telemetry_keys_rejected_by_run_validation(self):
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.start_run(
                self.repo_root,
                run_id="run-result-telemetry",
                backlog_id=None,
                github_issue=None,
                user_objective="obj",
                route_task_result={"ok": True, "telemetry": {"duration_ms": 1}},
                starting_commit=None,
            )

    # -- delegation/orchestration metrics ------------------------------------

    def test_orchestration_metrics_normalize_and_derive_when_baselines_exist(self):
        provenance.start_run(
            self.repo_root, run_id="run-metrics", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.update_orchestration_metrics(
            self.repo_root,
            "run-metrics",
            lead_model="lead-model",
            delegation_topology="two_workers",
            worker_model_mix="mixed_model",
            spawned_agents=2,
            total_tokens=1200,
            estimated_direct_tokens=1000,
            coordination_ms=250,
            wall_clock_ms=1000,
            estimated_serial_time_ms=1800,
            direct_baseline_quality=0.80,
            delegated_quality=0.90,
        )
        metrics = record["orchestration_metrics"]
        self.assertEqual(metrics["schema_version"], 1)
        self.assertEqual(metrics["coordination_tax_tokens"], 200)
        self.assertEqual(metrics["coordination_tax_time_ms"], 250)
        self.assertAlmostEqual(metrics["quality_gain"], 0.10)
        self.assertAlmostEqual(metrics["parallel_speedup"], 1.8)
        self.assertIsNone(metrics["lead_input_tokens"])

    def test_orchestration_metrics_can_show_negative_token_tax(self):
        provenance.start_run(
            self.repo_root, run_id="run-metrics-saving", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.update_orchestration_metrics(
            self.repo_root,
            "run-metrics-saving",
            total_tokens=800,
            estimated_direct_tokens=1000,
        )
        self.assertEqual(record["orchestration_metrics"]["coordination_tax_tokens"], -200)

    def test_orchestration_metrics_reject_caller_supplied_derived_fields(self):
        provenance.start_run(
            self.repo_root, run_id="run-bad-derived", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.update_orchestration_metrics(
                self.repo_root,
                "run-bad-derived",
                parallel_speedup=99,
            )

    # -- finishing / listing -------------------------------------------------

    def test_finish_run_sets_outcome_and_ending_commit(self):
        provenance.start_run(
            self.repo_root, run_id="run-h", backlog_id="ORCH-001", github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit="c0",
        )
        record = provenance.finish_run(self.repo_root, "run-h", outcome="completed", ending_commit="c1")
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["ending_commit"], "c1")
        self.assertIsNotNone(record["ended_at"])

    def test_finish_run_rejects_unknown_outcome(self):
        provenance.start_run(
            self.repo_root, run_id="run-i", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.finish_run(self.repo_root, "run-i", outcome="not_a_real_outcome")

    def test_runs_for_backlog_filters_correctly(self):
        provenance.start_run(
            self.repo_root, run_id="run-j1", backlog_id="ORCH-001", github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        provenance.start_run(
            self.repo_root, run_id="run-j2", backlog_id="ORCH-002", github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        runs = provenance.runs_for_backlog(self.repo_root, "ORCH-001")
        self.assertEqual([r["run_id"] for r in runs], ["run-j1"])

    def test_list_runs_on_empty_directory_returns_empty(self):
        self.assertEqual(provenance.list_runs(self.repo_root), [])

    # -- schema validation ----------------------------------------------------

    def test_invalid_record_rejected_by_schema(self):
        schema = provenance.load_schema(self.repo_root)
        bad_record = {"run_id": "x"}  # missing every other required field
        issues = provenance.validate_run_record(bad_record, schema)
        self.assertTrue(issues)

    def test_step_with_invalid_v04_execution_metadata_rejected(self):
        provenance.start_run(
            self.repo_root, run_id="run-invalid-metadata", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        for field, value in (
            ("capacity_tier", "unbounded"),
            ("reasoning_setting", "extreme"),
            ("context_strategy", "everything"),
            ("context_window_tier", "infinite"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(provenance.RunSchemaValidationError):
                    provenance.add_step(
                        self.repo_root, "run-invalid-metadata", task_id=field, **{field: value}
                    )

    def test_step_with_invalid_review_category_rejected(self):
        schema = provenance.load_schema(self.repo_root)
        record = {
            "run_id": "x",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
            "route_task_result": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [
                {
                    "task_id": "T1",
                    "parent_task_id": None,
                    "execution_plan_step_id": None,
                    "role": None,
                    "family": None,
                    "backend": None,
                    "model": None,
                    "capacity_tier": None,
                    "reasoning_setting": None,
                    "context_strategy": None,
                    "context_window_tier": None,
                    "fresh_context": None,
                    "mutation_permission": None,
                    "worker_identifier": None,
                    "status": "completed",
                    "outcome": None,
                    "files_changed": None,
                    "validation_performed": None,
                    "review_findings": [{"category": "NOT_A_REAL_CATEGORY", "description": "x"}],
                    "repair_of": None,
                }
            ],
        }
        issues = provenance.validate_run_record(record, schema)
        self.assertTrue(issues)

    def test_run_id_path_traversal_rejected(self):
        with self.assertRaises(provenance.ProvenanceError):
            provenance.start_run(
                self.repo_root, run_id="../escape", backlog_id=None, github_issue=None,
                user_objective="obj", route_task_result=None, starting_commit=None,
            )

    def test_finish_run_rejects_in_progress_and_post_finalization_mutation(self):
        provenance.start_run(
            self.repo_root, run_id="run-final", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.ProvenanceError):
            provenance.finish_run(self.repo_root, "run-final", outcome="in_progress")
        provenance.add_step(self.repo_root, "run-final", task_id="T1")
        provenance.finish_run(self.repo_root, "run-final", outcome="completed")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.add_step(self.repo_root, "run-final", task_id="T2")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.update_step(self.repo_root, "run-final", "T1", status="completed")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.finish_run(self.repo_root, "run-final", outcome="failed")

    def test_private_reasoning_and_transcript_fields_rejected(self):
        provenance.start_run(
            self.repo_root, run_id="run-private", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        for field in ("reasoning", "transcript", "hidden_reasoning"):
            with self.subTest(field=field):
                with self.assertRaises(provenance.ProvenanceError):
                    provenance.add_step(self.repo_root, "run-private", task_id="T1", **{field: "secret"})

    def test_malformed_timestamps_rejected_by_format_checker(self):
        schema = provenance.load_schema(self.repo_root)
        record = {
            "run_id": "x",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "not-a-date",
            "ended_at": None,
            "route_task_result": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [],
        }
        self.assertTrue(provenance.validate_run_record(record, schema))

    # -- step hierarchy (agent/subagent tree) validation ----------------------

    def test_start_run_sets_trace_schema_version(self):
        record = provenance.start_run(
            self.repo_root, run_id="run-tsv", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        self.assertEqual(record["trace_schema_version"], 1)

    def test_legacy_record_without_trace_schema_version_still_validates(self):
        schema = provenance.load_schema(self.repo_root)
        record = {
            "run_id": "legacy",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
            "route_task_result": None,
            "routing_telemetry": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [],
        }
        self.assertEqual(provenance.validate_run_record(record, schema), [])

    def test_step_returned_artifacts_default_to_explicit_null(self):
        provenance.start_run(
            self.repo_root, run_id="run-artifacts-null", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.add_step(self.repo_root, "run-artifacts-null", task_id="T1")
        self.assertIsNone(record["steps"][0]["returned_artifacts"])

    def test_step_accepts_structured_returned_artifacts_and_validation(self):
        provenance.start_run(
            self.repo_root, run_id="run-artifacts", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.add_step(
            self.repo_root, "run-artifacts", task_id="T1",
            returned_artifacts=[
                {"kind": "specification", "summary": "Implementation contract", "reference": None},
            ],
            validation_performed=[
                "python3 -m pytest",
                {"command": "npm test", "result": "pass", "exit_code": 0, "summary": "22 passed"},
            ],
        )
        step = record["steps"][0]
        self.assertEqual(step["returned_artifacts"][0]["kind"], "specification")
        self.assertEqual(step["validation_performed"][0], "python3 -m pytest")
        self.assertEqual(step["validation_performed"][1]["result"], "pass")

    def test_returned_artifact_rejects_unknown_field(self):
        provenance.start_run(
            self.repo_root, run_id="run-bad-artifact", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-bad-artifact", task_id="T1",
                returned_artifacts=[
                    {"kind": "implementation", "summary": "x", "reference": None, "transcript": "leaked"},
                ],
            )

    def test_structured_validation_entry_rejects_unknown_field(self):
        provenance.start_run(
            self.repo_root, run_id="run-bad-validation", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-bad-validation", task_id="T1",
                validation_performed=[
                    {"command": "x", "result": "pass", "exit_code": 0, "summary": "ok", "stdout": "leak"},
                ],
            )

    def test_step_cannot_be_its_own_parent(self):
        provenance.start_run(
            self.repo_root, run_id="run-self-parent", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(self.repo_root, "run-self-parent", task_id="T1", parent_task_id="T1")

    def test_step_cannot_repair_itself(self):
        provenance.start_run(
            self.repo_root, run_id="run-self-repair", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(self.repo_root, "run-self-repair", task_id="T1", repair_of="T1")

    def test_parent_task_id_must_reference_earlier_step(self):
        provenance.start_run(
            self.repo_root, run_id="run-forward-parent", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(self.repo_root, "run-forward-parent", task_id="T1", parent_task_id="not-yet-added")

    def test_repair_of_must_reference_earlier_step(self):
        provenance.start_run(
            self.repo_root, run_id="run-forward-repair", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(self.repo_root, "run-forward-repair", task_id="T1", repair_of="not-yet-added")

    def test_valid_parent_and_repair_chain_accepted(self):
        provenance.start_run(
            self.repo_root, run_id="run-valid-chain", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        provenance.add_step(self.repo_root, "run-valid-chain", task_id="lead")
        provenance.add_step(self.repo_root, "run-valid-chain", task_id="sub", parent_task_id="lead")
        record = provenance.add_step(self.repo_root, "run-valid-chain", task_id="lead-repair", repair_of="lead")
        self.assertEqual(len(record["steps"]), 3)

    def test_duplicate_task_id_rejected(self):
        schema = provenance.load_schema(self.repo_root)
        record = {
            "run_id": "x",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
            "route_task_result": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [
                {**provenance._STEP_DEFAULTS, "task_id": "T1"},
                {**provenance._STEP_DEFAULTS, "task_id": "T1"},
            ],
        }
        issues = provenance.validate_run_record(record, schema)
        self.assertTrue(any("duplicate task_id" in i for i in issues))

    def test_dangling_legacy_parent_reference_passes_validation_unchecked_on_read(self):
        # A directly-constructed record simulating a historical file written
        # before hierarchy validation existed: get_run() (a pure read) must
        # not reject it, even though a fresh write via add_step() would.
        schema = provenance.load_schema(self.repo_root)
        record = {
            "run_id": "legacy-dangling",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
            "route_task_result": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [
                {**provenance._STEP_DEFAULTS, "task_id": "T1", "parent_task_id": "ghost-parent"},
            ],
        }
        import json
        path = provenance.run_path(self.repo_root, "legacy-dangling")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(record, f)
        fetched = provenance.get_run(self.repo_root, "legacy-dangling")
        self.assertEqual(fetched["steps"][0]["parent_task_id"], "ghost-parent")
        # But writing it fresh (schema-validated) is rejected:
        issues = provenance.validate_run_record(record, schema)
        self.assertTrue(any("unresolved" not in i and "parent_task_id" in i for i in issues))

    # -- trace selection/derivation/rendering ---------------------------------

    def _start_codex_claude_codex_run(self, run_id: str) -> None:
        route_result = {
            "ok": True,
            "policy_version": 4,
            "selected_route": "CODEX_CLAUDE_CODEX",
            "fallback_applied": False,
            "matches": {
                "base": ["CODEX", "CODEX_CLAUDE_CODEX"],
                "effective": ["CODEX", "CODEX_CLAUDE_CODEX"],
            },
            "execution_plan": {
                "schema_version": 3,
                "independent_review_required": True,
                "steps": [
                    {
                        "id": "codex_specifier", "order": 1, "role": "specifier", "family": "codex",
                        "intent": "analysis", "capacity_tier": "high", "reasoning_effort": "high",
                        "context_strategy": "full", "fresh_context": True, "mutation_permission": "forbidden",
                        "depends_on": [], "independent_review": False,
                    },
                    {
                        "id": "claude_worker", "order": 2, "role": "worker", "family": "claude",
                        "intent": "implementation", "capacity_tier": "high", "reasoning_effort": "high",
                        "context_strategy": "full", "fresh_context": True, "mutation_permission": "allowed",
                        "depends_on": ["codex_specifier"], "independent_review": False,
                    },
                    {
                        "id": "codex_reviewer", "order": 3, "role": "reviewer", "family": "codex",
                        "intent": "review", "capacity_tier": "high", "reasoning_effort": "high",
                        "context_strategy": "focused", "fresh_context": True, "mutation_permission": "forbidden",
                        "depends_on": ["codex_specifier", "claude_worker"], "independent_review": True,
                    },
                ],
            },
        }
        provenance.start_run(
            self.repo_root, run_id=run_id, backlog_id="ORCH-011", github_issue=11,
            user_objective="Implement end-of-run orchestration trace.",
            route_task_result=route_result, starting_commit="c0",
        )
        provenance.add_step(
            self.repo_root, run_id, task_id="codex_specifier",
            execution_plan_step_id="codex_specifier", role="specifier", family="codex",
            status="completed", outcome="accepted",
            returned_artifacts=[{"kind": "specification", "summary": "Trace contract", "reference": None}],
        )
        provenance.add_step(
            self.repo_root, run_id, task_id="claude_worker",
            execution_plan_step_id="claude_worker", role="worker", family="claude",
            status="completed", outcome="accepted",
            validation_performed=[
                "python3 -m pytest",
                {"command": "npm test", "result": "pass", "exit_code": 0, "summary": "22 passed"},
            ],
        )
        provenance.add_step(
            self.repo_root, run_id, task_id="claude_worker.nested_search",
            parent_task_id="claude_worker", role="worker", family="claude",
            status="completed", outcome="subtask done",
        )
        provenance.add_step(
            self.repo_root, run_id, task_id="codex_reviewer",
            execution_plan_step_id="codex_reviewer", role="reviewer", family="codex",
            status="completed", outcome="no required findings",
        )
        provenance.finish_run(self.repo_root, run_id, outcome="completed", ending_commit="c1")

    def test_build_trace_report_multi_agent_shape(self):
        self._start_codex_claude_codex_run("run-trace-shape")
        record = provenance.get_run(self.repo_root, "run-trace-shape")
        report = provenance.build_trace_report(self.repo_root, record)

        self.assertEqual(report["trace_schema_version"], 1)
        self.assertEqual(report["trace_path"], "provenance/runs/run-trace-shape.json")
        self.assertEqual(report["backlog_id"], "ORCH-011")
        self.assertEqual(report["github_issue"], 11)
        self.assertEqual(report["outcome"], "completed")
        self.assertEqual(report["route"]["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(report["route"]["policy_version"], 4)
        self.assertEqual(len(report["execution_plan"]["steps"]), 3)

        # Agent tree: three top-level roots (specifier, worker, reviewer),
        # with the nested subagent parented under claude_worker.
        root_ids = [n["task_id"] for n in report["agents"]]
        self.assertEqual(root_ids, ["codex_specifier", "claude_worker", "codex_reviewer"])
        worker_node = next(n for n in report["agents"] if n["task_id"] == "claude_worker")
        self.assertEqual([c["task_id"] for c in worker_node["children"]], ["claude_worker.nested_search"])
        self.assertEqual(report["warnings"], [])

        self.assertEqual(len(report["returned_artifacts"]), 1)
        self.assertEqual(report["returned_artifacts"][0]["task_id"], "codex_specifier")

        self.assertEqual(len(report["validation_results"]), 2)
        legacy_entry = next(v for v in report["validation_results"] if v["command"] == "python3 -m pytest")
        self.assertEqual(legacy_entry["result"], "unknown")
        self.assertIsNone(legacy_entry["exit_code"])
        structured_entry = next(v for v in report["validation_results"] if v["command"] == "npm test")
        self.assertEqual(structured_entry["result"], "pass")
        self.assertEqual(structured_entry["exit_code"], 0)

    def test_parent_run_trace_lists_routes_per_phase(self):
        report = {"run_id": "run-x", "trace_path": "t", "outcome": "completed", "objective": "o",
                  "parent_orchestration": {"target_repository": "acme/a", "phases": [
                      {"id": "inspection", "status": "completed", "route": None, "workers": []},
                      {"id": "docs", "status": "completed", "route": "CLAUDE", "workers": []},
                      {"id": "review", "status": "completed", "route": "CODEX", "workers": []}]},
                  "route": None}
        text = provenance.render_trace_text(report)
        self.assertIn("Routed per phase: docs CLAUDE, review CODEX", text)
        self.assertNotIn("no route_task_result recorded", text)

    def test_render_trace_text_is_deterministic_and_covers_required_sections(self):
        self._start_codex_claude_codex_run("run-trace-text")
        record = provenance.get_run(self.repo_root, "run-trace-text")
        report = provenance.build_trace_report(self.repo_root, record)
        text_a = provenance.render_trace_text(report)
        text_b = provenance.render_trace_text(provenance.build_trace_report(self.repo_root, record))
        self.assertEqual(text_a, text_b)
        for expected in (
            "Run: run-trace-text",
            "Trace: provenance/runs/run-trace-text.json",
            "References: backlog ORCH-011; GitHub issue #11",
            "Selected: CODEX_CLAUDE_CODEX",
            "codex_specifier - specifier/codex",
            "claude_worker.nested_search",
            "PASS [exit 0] `npm test`",
            "UNKNOWN [exit unknown] `python3 -m pytest`",
            "Final status: completed",
        ):
            self.assertIn(expected, text_a)

    def test_render_trace_text_reports_unknown_and_none_observed_for_empty_run(self):
        provenance.start_run(
            self.repo_root, run_id="run-empty", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.get_run(self.repo_root, "run-empty")
        report = provenance.build_trace_report(self.repo_root, record)
        text = provenance.render_trace_text(report)
        self.assertIn("(no route_task_result recorded)", text)
        self.assertIn("Execution plan:\n  (none)", text)
        self.assertIn("Agents:\n  (none observed)", text)
        self.assertIn("Artifacts:\n  (none observed)", text)
        self.assertIn("Validation:\n  (none observed)", text)
        self.assertIn("Routing: unknown", text)
        self.assertIn("Run metrics: unknown", text)
        self.assertIn("References: backlog unknown; GitHub issue unknown", text)

    def test_build_trace_report_warns_on_unresolved_legacy_parent(self):
        import json
        record = {
            "run_id": "run-legacy-warn",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
            "route_task_result": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [
                {**provenance._STEP_DEFAULTS, "task_id": "T1", "parent_task_id": "ghost-parent"},
            ],
        }
        path = provenance.run_path(self.repo_root, "run-legacy-warn")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(record, f)
        fetched = provenance.get_run(self.repo_root, "run-legacy-warn")
        report = provenance.build_trace_report(self.repo_root, fetched)
        self.assertEqual([n["task_id"] for n in report["agents"]], ["T1"])
        self.assertTrue(any("ghost-parent" in w for w in report["warnings"]))

    def test_select_trace_runs_run_id_exclusive_with_backlog_id_and_latest(self):
        with self.assertRaises(provenance.ProvenanceError):
            provenance.select_trace_runs(self.repo_root, run_id="x", backlog_id="ORCH-001")
        with self.assertRaises(provenance.ProvenanceError):
            provenance.select_trace_runs(self.repo_root, run_id="x", latest=True)

    def test_select_trace_runs_by_run_id(self):
        provenance.start_run(
            self.repo_root, run_id="run-sel-1", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        runs = provenance.select_trace_runs(self.repo_root, run_id="run-sel-1")
        self.assertEqual([r["run_id"] for r in runs], ["run-sel-1"])

    def test_select_trace_runs_by_backlog_id_and_latest(self):
        provenance.start_run(
            self.repo_root, run_id="run-a1", backlog_id="ORCH-020", github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            started_at="2026-01-01T00:00:00Z",
        )
        provenance.start_run(
            self.repo_root, run_id="run-a2", backlog_id="ORCH-020", github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            started_at="2026-01-02T00:00:00Z",
        )
        provenance.start_run(
            self.repo_root, run_id="run-b1", backlog_id="ORCH-021", github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            started_at="2026-01-03T00:00:00Z",
        )
        all_for_020 = provenance.select_trace_runs(self.repo_root, backlog_id="ORCH-020")
        self.assertEqual([r["run_id"] for r in all_for_020], ["run-a1", "run-a2"])

        latest_for_020 = provenance.select_trace_runs(self.repo_root, backlog_id="ORCH-020", latest=True)
        self.assertEqual([r["run_id"] for r in latest_for_020], ["run-a2"])

        latest_overall = provenance.select_trace_runs(self.repo_root, latest=True)
        self.assertEqual([r["run_id"] for r in latest_overall], ["run-b1"])

    def test_select_trace_runs_no_match_returns_empty_list(self):
        self.assertEqual(provenance.select_trace_runs(self.repo_root, backlog_id="ORCH-999"), [])
        self.assertEqual(provenance.select_trace_runs(self.repo_root, latest=True), [])

    # -- chronological (not lexical) instant sorting --------------------------

    def test_list_runs_sorts_by_utc_instant_not_lexical_string(self):
        # Lexically, '2026-01-01T01:00:00+05:00' > '2025-12-31T23:00:00Z'
        # (the leading '2026' > '2025' decides it before the offset is even
        # considered), but the actual UTC instants disagree:
        # 2026-01-01T01:00:00+05:00 == 2025-12-31T20:00:00Z, which is
        # earlier than 2025-12-31T23:00:00Z. Sorting/`--latest` must follow
        # the real instant, not the raw string.
        provenance.start_run(
            self.repo_root, run_id="run-tz-a", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            started_at="2026-01-01T01:00:00+05:00",
        )
        provenance.start_run(
            self.repo_root, run_id="run-tz-b", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            started_at="2025-12-31T23:00:00Z",
        )
        runs = provenance.list_runs(self.repo_root)
        self.assertEqual([r["run_id"] for r in runs], ["run-tz-a", "run-tz-b"])
        latest = provenance.select_trace_runs(self.repo_root, latest=True)
        self.assertEqual([r["run_id"] for r in latest], ["run-tz-b"])

    def test_list_runs_treats_unparseable_timestamp_as_oldest(self):
        import json
        provenance.start_run(
            self.repo_root, run_id="run-bad-ts", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.get_run(self.repo_root, "run-bad-ts")
        record["started_at"] = "not-a-timestamp"
        path = provenance.run_path(self.repo_root, "run-bad-ts")
        with path.open("w", encoding="utf-8") as f:
            json.dump(record, f)
        provenance.start_run(
            self.repo_root, run_id="run-good-ts", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            started_at="2020-01-01T00:00:00Z",
        )
        runs = provenance.list_runs(self.repo_root)
        self.assertEqual(runs[0]["run_id"], "run-bad-ts")

    # -- agent-tree hardening: cycles/forward-refs/duplicates ------------------

    def test_build_agent_tree_forward_reference_rendered_as_root_with_warning(self):
        steps = [
            {"task_id": "A", "parent_task_id": "B"},
            {"task_id": "B", "parent_task_id": None},
        ]
        agents, warnings = provenance._build_agent_tree(steps)
        self.assertEqual([n["task_id"] for n in agents], ["A", "B"])
        self.assertTrue(any("'A'" in w and "'B'" in w for w in warnings))

    def test_build_agent_tree_cycle_is_broken_deterministically(self):
        # A's parent is B; B's parent is A. A is processed first, so B is
        # not yet established -- A becomes a root with a warning. B is
        # processed second, so A is now established -- B attaches under A.
        # The result is always a tree, never a cycle.
        steps = [
            {"task_id": "A", "parent_task_id": "B"},
            {"task_id": "B", "parent_task_id": "A"},
        ]
        agents, warnings = provenance._build_agent_tree(steps)
        self.assertEqual([n["task_id"] for n in agents], ["A"])
        self.assertEqual([c["task_id"] for c in agents[0]["children"]], ["B"])
        self.assertTrue(any("'B'" in w for w in warnings))

    def test_build_agent_tree_duplicate_task_id_renders_extra_root_with_warning(self):
        steps = [
            {"task_id": "A", "parent_task_id": None},
            {"task_id": "A", "parent_task_id": None},
        ]
        agents, warnings = provenance._build_agent_tree(steps)
        task_ids = [n["task_id"] for n in agents]
        self.assertEqual(task_ids.count("A"), 2)
        self.assertEqual(agents[0]["children"], [])
        self.assertEqual(agents[1]["children"], [])
        self.assertTrue(any("appears more than once" in w for w in warnings))

    def test_build_agent_tree_child_of_duplicated_parent_is_ambiguous_root(self):
        # Once 'A' has been seen twice, which of the two same-id steps a
        # later child actually meant is ambiguous -- it must not silently
        # attach to the first occurrence.
        steps = [
            {"task_id": "A", "parent_task_id": None},
            {"task_id": "A", "parent_task_id": None},
            {"task_id": "C", "parent_task_id": "A"},
        ]
        agents, warnings = provenance._build_agent_tree(steps)
        task_ids = [n["task_id"] for n in agents]
        self.assertEqual(task_ids, ["A", "A", "C"])
        for node in agents:
            self.assertEqual(node["children"], [])
        self.assertTrue(any("ambiguous" in w and "'C'" in w and "'A'" in w for w in warnings))

    def test_build_agent_tree_child_before_duplicate_keeps_original_attachment(self):
        # A child that named the parent before the duplicate appeared keeps
        # its original (unambiguous, at the time) attachment -- append order
        # is never rewritten retroactively.
        steps = [
            {"task_id": "A", "parent_task_id": None},
            {"task_id": "C", "parent_task_id": "A"},
            {"task_id": "A", "parent_task_id": None},
        ]
        agents, warnings = provenance._build_agent_tree(steps)
        canonical = next(n for n in agents if n["task_id"] == "A" and n["children"])
        self.assertEqual([c["task_id"] for c in canonical["children"]], ["C"])
        self.assertTrue(any("appears more than once" in w for w in warnings))

    def test_build_agent_tree_self_parent_rendered_as_root_with_warning(self):
        steps = [{"task_id": "A", "parent_task_id": "A"}]
        agents, warnings = provenance._build_agent_tree(steps)
        self.assertEqual([n["task_id"] for n in agents], ["A"])
        self.assertTrue(any("references itself as parent_task_id" in w for w in warnings))

    # -- route decision fidelity: full safe fields + assigned capacity --------

    def test_build_trace_report_retains_full_route_decision_fields(self):
        route_result = {
            "ok": True,
            "policy_version": 4,
            "selected_route": "CODEX",
            "fallback_applied": False,
            "matches": {"base": ["CODEX"], "effective": ["CODEX"]},
            "classification": {
                "base": {"task_type": "implementation"},
                "effective": {"task_type": "implementation"},
            },
            "project_constraints": {"risk_overrides": {}},
            "execution_plan": {
                "schema_version": 3,
                "independent_review_required": False,
                "steps": [
                    {
                        "id": "codex_worker", "order": 1, "role": "worker", "family": "codex",
                        "intent": "implementation", "capacity_tier": "standard", "reasoning_effort": "high",
                        "context_strategy": "full", "fresh_context": True, "mutation_permission": "allowed",
                        "depends_on": [], "independent_review": False,
                    },
                ],
            },
            "capacity_assignments": [
                {
                    "step_id": "codex_worker", "authored_capacity_tier": "standard",
                    "assigned_capacity_tier": "economy", "rule": "low-risk-worker",
                },
            ],
            "backend_bindings": {
                "codex": {
                    "tool": "spawn_subagent",
                    "capacity_models": {
                        "economy": {"model": "openai-codex/gpt-6-luna", "context_window_tier": "standard"},
                    },
                },
            },
        }
        provenance.start_run(
            self.repo_root, run_id="run-full-route", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=route_result, starting_commit=None,
        )
        record = provenance.get_run(self.repo_root, "run-full-route")
        report = provenance.build_trace_report(self.repo_root, record)
        self.assertEqual(report["route"]["classification"], route_result["classification"])
        self.assertEqual(report["route"]["capacity_assignments"], route_result["capacity_assignments"])
        self.assertEqual(report["route"]["backend_bindings"], route_result["backend_bindings"])
        self.assertEqual(report["route"]["project_constraints"], route_result["project_constraints"])

        text = provenance.render_trace_text(report)
        # The rendered execution plan shows both the authored tier and the
        # actually *assigned* economy tier (plus which rule made that
        # decision), its resolved backend/model -- not just the authored
        # 'standard' tier in isolation.
        self.assertIn("capacity=standard->economy", text)
        self.assertIn("rule=low-risk-worker", text)
        self.assertIn("backend=spawn_subagent", text)
        self.assertIn("model=openai-codex/gpt-6-luna", text)
        # The classification that drove the route/capacity decision is also
        # rendered, not just retained in the JSON report.
        self.assertIn("Classification (base):", text)
        self.assertIn("task_type", text)
        self.assertIn("implementation", text)

    def test_build_trace_report_renders_failed_route_errors(self):
        route_result = {
            "ok": False,
            "errors": [{"path": "classified_task.failure_cost", "message": "'extreme' is not a valid value"}],
        }
        provenance.start_run(
            self.repo_root, run_id="run-failed-route", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=route_result, starting_commit=None,
        )
        record = provenance.get_run(self.repo_root, "run-failed-route")
        report = provenance.build_trace_report(self.repo_root, record)
        self.assertEqual(report["route"]["ok"], False)
        self.assertEqual(report["route"]["errors"], route_result["errors"])
        text = provenance.render_trace_text(report)
        self.assertIn("Route selection failed", text)
        self.assertIn("classified_task.failure_cost", text)

    # -- free-text safety: no transcripts/secrets/multiline in worker-controlled fields --

    def test_validation_summary_rejects_multiline_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-bad-summary", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-bad-summary", task_id="T1",
                validation_performed=[
                    {"command": "pytest", "result": "pass", "exit_code": 0, "summary": "line one\nline two"},
                ],
            )

    def test_validation_command_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-secret-command", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-secret-command", task_id="T1",
                validation_performed=[
                    {
                        "command": "curl -H 'Authorization: Bearer sk-abcdefghij0123456789'",
                        "result": "pass", "exit_code": 0, "summary": None,
                    },
                ],
            )

    def test_returned_artifact_summary_rejects_private_key_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-secret-artifact", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-secret-artifact", task_id="T1",
                returned_artifacts=[
                    {"kind": "other", "summary": "-----BEGIN RSA PRIVATE KEY----- leaked", "reference": None},
                ],
            )

    def test_returned_artifact_reference_rejects_absolute_filesystem_path(self):
        provenance.start_run(
            self.repo_root, run_id="run-abs-ref", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-abs-ref", task_id="T1",
                returned_artifacts=[
                    {"kind": "other", "summary": "ok", "reference": "/etc/passwd"},
                ],
            )

    def test_returned_artifact_reference_accepts_relative_path_and_https_url(self):
        provenance.start_run(
            self.repo_root, run_id="run-good-ref", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.add_step(
            self.repo_root, "run-good-ref", task_id="T1",
            returned_artifacts=[
                {"kind": "other", "summary": "ok", "reference": "provenance/runs/run-good-ref.json"},
                {"kind": "other", "summary": "ok", "reference": "https://github.com/acme/repo/pull/1"},
            ],
        )
        self.assertEqual(len(record["steps"][0]["returned_artifacts"]), 2)

    def test_returned_artifact_reference_rejects_relative_path_traversal(self):
        provenance.start_run(
            self.repo_root, run_id="run-traversal-ref", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-traversal-ref", task_id="T1",
                returned_artifacts=[
                    {"kind": "other", "summary": "ok", "reference": "../../etc/passwd"},
                ],
            )

    def test_returned_artifact_reference_rejects_traversal_disguised_within_path(self):
        provenance.start_run(
            self.repo_root, run_id="run-traversal-ref-2", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-traversal-ref-2", task_id="T1",
                returned_artifacts=[
                    {"kind": "other", "summary": "ok", "reference": "provenance/runs/../../../etc/passwd"},
                ],
            )

    def test_step_outcome_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-outcome-secret", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-outcome-secret", task_id="T1",
                outcome="Bearer sk-abcdefghij0123456789",
            )

    def test_step_outcome_rejects_multiline_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-outcome-multiline", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-outcome-multiline", task_id="T1",
                outcome="accepted\nFinal status: forged",
            )

    def test_review_finding_description_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-finding-secret", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-finding-secret", task_id="T1",
                review_findings=[
                    {"category": "BLOCKER", "description": "leaked AKIA0123456789ABCDEF in config"},
                ],
            )

    def test_user_objective_rejects_multiline_content(self):
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.start_run(
                self.repo_root, run_id="run-objective-multiline", backlog_id=None, github_issue=None,
                user_objective="line one\nline two", route_task_result=None, starting_commit=None,
            )

    def test_step_outcome_compact_text_is_accepted(self):
        provenance.start_run(
            self.repo_root, run_id="run-outcome-ok", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        record = provenance.add_step(
            self.repo_root, "run-outcome-ok", task_id="T1",
            outcome="accepted; repaired once after independent review",
        )
        self.assertEqual(record["steps"][0]["outcome"], "accepted; repaired once after independent review")

    def test_routing_telemetry_source_rejects_secret_like_content(self):
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.start_run(
                self.repo_root, run_id="run-telemetry-source-secret", backlog_id=None, github_issue=None,
                user_objective="obj", route_task_result=None, starting_commit=None,
                routing_telemetry={"source": "Bearer abcdefghijklmnopqrst"},
            )

    def test_step_telemetry_source_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-step-telemetry-source-secret", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-step-telemetry-source-secret", task_id="T1",
                telemetry={"source": "Bearer abcdefghijklmnopqrst"},
            )

    def test_orchestration_metrics_lead_model_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-metrics-lead-model-secret", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.update_orchestration_metrics(
                self.repo_root, "run-metrics-lead-model-secret",
                lead_model="Bearer abcdefghijklmnopqrst",
            )

    def test_files_changed_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-files-changed-secret", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-files-changed-secret", task_id="T1",
                files_changed=["tools/backlog.py", "Bearer abcdefghijklmnopqrst"],
            )

    def test_step_task_id_rejects_secret_like_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-task-id-secret", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-task-id-secret", task_id="Bearer abcdefghijklmnopqrst",
            )

    def test_step_task_id_rejects_multiline_content(self):
        provenance.start_run(
            self.repo_root, run_id="run-task-id-multiline", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        with self.assertRaises(provenance.RunSchemaValidationError):
            provenance.add_step(
                self.repo_root, "run-task-id-multiline", task_id="T1\nFinal status: forged",
            )

    def test_render_trace_text_sanitizes_legacy_multiline_content(self):
        import json
        record = {
            "run_id": "run-legacy-multiline",
            "backlog_id": None,
            "github_issue": None,
            "user_objective": "obj",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": None,
            "route_task_result": None,
            "starting_commit": None,
            "ending_commit": None,
            "outcome": "in_progress",
            "steps": [
                {
                    **provenance._STEP_DEFAULTS,
                    "task_id": "T1",
                    "validation_performed": [
                        {"command": "pytest", "result": "pass", "exit_code": 0, "summary": "line one\nFinal status: completed"},
                    ],
                },
            ],
        }
        path = provenance.run_path(self.repo_root, "run-legacy-multiline")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(record, f)
        fetched = provenance.get_run(self.repo_root, "run-legacy-multiline")
        report = provenance.build_trace_report(self.repo_root, fetched)
        text = provenance.render_trace_text(report)
        # The embedded newline must never let stored content masquerade as
        # a new section header in the fixed-section plain-text layout.
        self.assertNotIn("line one\nFinal status: completed", text)
        self.assertIn("line one\\nFinal status: completed", text)


class TelemetryObservationTest(unittest.TestCase):
    """Tests for the observed timing/token telemetry helpers (ORCH-013):
    `start_observation`/`finish_observation` (monotonic duration + UTC
    timestamps, no guessed duration/cost) and `extract_token_usage` (safe
    structured-usage-only token extraction)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_repo_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    # -- timing -----------------------------------------------------------

    def test_finish_observation_reports_nonnegative_duration_and_utc_timestamps(self):
        handle = provenance.start_observation()
        telemetry = provenance.finish_observation(handle, source="route_task")
        self.assertEqual(telemetry["source"], "route_task")
        self.assertIsInstance(telemetry["started_at"], str)
        self.assertIsInstance(telemetry["ended_at"], str)
        self.assertTrue(telemetry["started_at"].endswith("Z") or "+" in telemetry["started_at"])
        self.assertIsInstance(telemetry["duration_ms"], float)
        self.assertGreaterEqual(telemetry["duration_ms"], 0.0)
        # started_at <= ended_at as real timestamps (never guessed/backdated).
        self.assertLessEqual(telemetry["started_at"], telemetry["ended_at"])

    def test_finish_observation_duration_reflects_elapsed_monotonic_time(self):
        handle = provenance.start_observation()
        time.sleep(0.02)
        telemetry = provenance.finish_observation(handle)
        self.assertGreaterEqual(telemetry["duration_ms"], 15.0)

    def test_finish_observation_requires_a_handle_from_start_observation(self):
        with self.assertRaises(provenance.ProvenanceError):
            provenance.finish_observation({"not": "a handle"})
        with self.assertRaises(provenance.ProvenanceError):
            provenance.finish_observation(None)  # type: ignore[arg-type]

    def test_finish_observation_never_estimates_cost_or_queue_time(self):
        handle = provenance.start_observation()
        telemetry = provenance.finish_observation(handle)
        self.assertNotIn("estimated_cost_usd", telemetry)
        self.assertNotIn("queue_ms", telemetry)

    def test_observation_telemetry_round_trips_through_add_step(self):
        provenance.start_run(
            self.repo_root, run_id="run-observed", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        handle = provenance.start_observation()
        telemetry = provenance.finish_observation(
            handle, source="spawn_subagent", usage={"input_tokens": 100, "output_tokens": 20}
        )
        record = provenance.add_step(self.repo_root, "run-observed", task_id="T1", telemetry=telemetry)
        stored = record["steps"][0]["telemetry"]
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["source"], "spawn_subagent")
        self.assertEqual(stored["input_tokens"], 100)
        self.assertEqual(stored["output_tokens"], 20)
        self.assertIsNone(stored["total_tokens"])
        self.assertIsNone(stored["estimated_cost_usd"])
        self.assertIsNone(stored["queue_ms"])

    # -- token usage extraction --------------------------------------------

    def test_extract_token_usage_direct_object_snake_case(self):
        result = provenance.extract_token_usage(
            {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
        )
        self.assertEqual(result, {"input_tokens": 12, "output_tokens": 8, "cached_tokens": None, "total_tokens": 20})

    def test_extract_token_usage_camel_case(self):
        result = provenance.extract_token_usage(
            {"inputTokens": 12, "outputTokens": 8, "totalTokens": 20}
        )
        self.assertEqual(result, {"input_tokens": 12, "output_tokens": 8, "cached_tokens": None, "total_tokens": 20})

    def test_extract_token_usage_openai_style_prompt_completion(self):
        result = provenance.extract_token_usage(
            {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}
        )
        self.assertEqual(result, {"input_tokens": 30, "output_tokens": 5, "cached_tokens": None, "total_tokens": 35})

    def test_extract_token_usage_gemini_style_token_counts(self):
        result = provenance.extract_token_usage(
            {"promptTokenCount": 40, "candidatesTokenCount": 6, "totalTokenCount": 46}
        )
        self.assertEqual(result, {"input_tokens": 40, "output_tokens": 6, "cached_tokens": None, "total_tokens": 46})

    def test_extract_token_usage_pi_usage_totals_shape(self):
        result = provenance.extract_token_usage({"input": 10, "output": 4, "cacheRead": 7, "cacheWrite": 99, "cost": {"total": 1}})
        self.assertEqual(result, {"input_tokens": 10, "output_tokens": 4, "cached_tokens": 7, "total_tokens": None})

    def test_extract_token_usage_from_usage_wrapper_key(self):
        result = provenance.extract_token_usage(
            {"model": "x", "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}
        )
        self.assertEqual(result, {"input_tokens": 3, "output_tokens": 4, "cached_tokens": None, "total_tokens": 7})

    def test_extract_token_usage_cached_tokens(self):
        result = provenance.extract_token_usage(
            {"input_tokens": 30, "output_tokens": 5, "cached_tokens": 11, "total_tokens": 35}
        )
        self.assertEqual(result, {"input_tokens": 30, "output_tokens": 5, "cached_tokens": 11, "total_tokens": 35})
        result = provenance.extract_token_usage({"cacheReadInputTokens": 9})
        self.assertEqual(result["cached_tokens"], 9)
        result = provenance.extract_token_usage({"cachedContentTokenCount": 7})
        self.assertEqual(result["cached_tokens"], 7)

    def test_extract_token_usage_cached_tokens_from_structured_details(self):
        result = provenance.extract_token_usage({"prompt_tokens_details": {"cached_tokens": 6}})
        self.assertEqual(result["cached_tokens"], 6)
        result = provenance.extract_token_usage({"input_tokens_details": {"cached_tokens": 8}})
        self.assertEqual(result["cached_tokens"], 8)

    def test_extract_token_usage_from_usage_metadata_wrapper_key(self):
        result = provenance.extract_token_usage(
            {"usage_metadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7}}
        )
        self.assertEqual(result, {"input_tokens": 3, "output_tokens": 4, "cached_tokens": None, "total_tokens": 7})

    def test_extract_token_usage_from_token_usage_wrapper_key(self):
        result = provenance.extract_token_usage(
            {"token_usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}
        )
        self.assertEqual(result, {"input_tokens": 3, "output_tokens": 4, "cached_tokens": None, "total_tokens": 7})

    def test_extract_token_usage_missing_usage_returns_all_none(self):
        self.assertEqual(
            provenance.extract_token_usage(None),
            {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "total_tokens": None},
        )
        self.assertEqual(
            provenance.extract_token_usage({}),
            {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "total_tokens": None},
        )
        self.assertEqual(
            provenance.extract_token_usage("not a dict"),
            {"input_tokens": None, "output_tokens": None, "cached_tokens": None, "total_tokens": None},
        )

    def test_extract_token_usage_partial_usage_leaves_unobserved_fields_none(self):
        result = provenance.extract_token_usage({"input_tokens": 12})
        self.assertEqual(result, {"input_tokens": 12, "output_tokens": None, "cached_tokens": None, "total_tokens": None})

    def test_extract_token_usage_conflicting_field_names_fail_closed(self):
        # Two different spellings for the same logical counter disagree:
        # neither is trustworthy, so the field is None, not an arbitrary pick.
        result = provenance.extract_token_usage({"input_tokens": 12, "inputTokens": 99})
        self.assertIsNone(result["input_tokens"])

    def test_extract_token_usage_agreeing_duplicate_field_names_accepted(self):
        result = provenance.extract_token_usage({"input_tokens": 12, "inputTokens": 12})
        self.assertEqual(result["input_tokens"], 12)

    def test_extract_token_usage_rejects_negative_values(self):
        result = provenance.extract_token_usage({"input_tokens": -5})
        self.assertIsNone(result["input_tokens"])

    def test_extract_token_usage_rejects_non_integer_values(self):
        result = provenance.extract_token_usage({"input_tokens": "12"})
        self.assertIsNone(result["input_tokens"])
        result = provenance.extract_token_usage({"input_tokens": 12.5})
        self.assertIsNone(result["input_tokens"])
        result = provenance.extract_token_usage({"input_tokens": True})
        self.assertIsNone(result["input_tokens"])

    def test_extract_token_usage_accepts_integral_float(self):
        result = provenance.extract_token_usage({"input_tokens": 12.0})
        self.assertEqual(result["input_tokens"], 12)

    def test_extract_token_usage_never_persists_raw_usage_object(self):
        usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15, "model": "gpt-6"}
        result = provenance.extract_token_usage(usage)
        self.assertNotIn("model", result)
        self.assertEqual(set(result), {"input_tokens", "output_tokens", "cached_tokens", "total_tokens"})

    def test_finish_observation_never_persists_raw_usage_object(self):
        handle = provenance.start_observation()
        usage = {"input_tokens": 12, "output_tokens": 3, "usage_metadata": {"secret": "nope"}}
        telemetry = provenance.finish_observation(handle, usage=usage)
        self.assertNotIn("usage_metadata", telemetry)
        self.assertNotIn("secret", str(telemetry.keys()))

    def test_observe_call_measures_mocked_worker_call_and_tokens(self):
        def worker(arg):
            return {"ok": True, "usage": {"input_tokens": 5, "output_tokens": 2, "cached_tokens": 1, "total_tokens": 7}, "body": arg}

        result, telemetry = provenance.observe_call(worker, "done", source="spawn_subagent", usage_getter=lambda r: r["usage"])
        self.assertEqual(result["body"], "done")
        self.assertEqual(telemetry["source"], "spawn_subagent")
        self.assertGreaterEqual(telemetry["duration_ms"], 0)
        self.assertEqual(telemetry["input_tokens"], 5)
        self.assertEqual(telemetry["output_tokens"], 2)
        self.assertEqual(telemetry["cached_tokens"], 1)
        self.assertEqual(telemetry["total_tokens"], 7)
        self.assertNotIn("body", telemetry)

    def test_observe_call_preserves_telemetry_on_failure(self):
        def failing_route():
            raise RuntimeError("boom")

        with self.assertRaises(provenance.ObservedCallError) as ctx:
            provenance.observe_call(failing_route, source="route_task")
        self.assertEqual(ctx.exception.telemetry["source"], "route_task")
        self.assertGreaterEqual(ctx.exception.telemetry["duration_ms"], 0)
        self.assertIsInstance(ctx.exception.original_exception, RuntimeError)

    def test_legacy_telemetry_without_cached_tokens_can_be_finalized(self):
        record = provenance.start_run(
            self.repo_root, run_id="run-legacy-no-cached", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            routing_telemetry={"source": "legacy", "input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
        path = provenance.run_path(self.repo_root, "run-legacy-no-cached")
        record = json.loads(path.read_text())
        record["routing_telemetry"].pop("cached_tokens", None)
        path.write_text(json.dumps(record))
        finished = provenance.finish_run(self.repo_root, "run-legacy-no-cached", outcome="completed")
        self.assertNotIn("cached_tokens", finished["routing_telemetry"])

    # -- trace rendering: observed vs unknown telemetry --------------------

    def test_render_trace_text_shows_observed_step_telemetry(self):
        provenance.start_run(
            self.repo_root, run_id="run-telemetry-trace", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        provenance.add_step(
            self.repo_root, "run-telemetry-trace", task_id="T1", status="completed",
            telemetry={
                "source": "spawn_subagent", "duration_ms": 1234.5,
                "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            },
        )
        record = provenance.get_run(self.repo_root, "run-telemetry-trace")
        report = provenance.build_trace_report(self.repo_root, record)
        text = provenance.render_trace_text(report)
        self.assertIn("source=spawn_subagent", text)
        self.assertIn("duration_ms=1234.5", text)
        self.assertIn("tokens(in/out/cached/total)=100/20/unknown/120", text)

    def test_render_trace_text_shows_unknown_for_unobserved_step_telemetry(self):
        provenance.start_run(
            self.repo_root, run_id="run-telemetry-unknown", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
        )
        provenance.add_step(self.repo_root, "run-telemetry-unknown", task_id="T1", status="completed")
        record = provenance.get_run(self.repo_root, "run-telemetry-unknown")
        report = provenance.build_trace_report(self.repo_root, record)
        text = provenance.render_trace_text(report)
        self.assertIn("telemetry: unknown", text)

    def test_render_trace_text_shows_observed_routing_telemetry(self):
        provenance.start_run(
            self.repo_root, run_id="run-routing-telemetry-trace", backlog_id=None, github_issue=None,
            user_objective="obj", route_task_result=None, starting_commit=None,
            routing_telemetry={"source": "route_task.py", "duration_ms": 5.0},
        )
        record = provenance.get_run(self.repo_root, "run-routing-telemetry-trace")
        report = provenance.build_trace_report(self.repo_root, record)
        text = provenance.render_trace_text(report)
        self.assertIn("Routing: source=route_task.py duration_ms=5.0", text)


class ProvenanceRunLockTest(unittest.TestCase):
    """Parallel wave workers record steps and metrics for one run concurrently."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = make_repo_root(Path(self._tmp.name))
        provenance.start_run(self.repo_root, run_id="run-lock", backlog_id=None, github_issue=None,
                             user_objective="obj", route_task_result=None, starting_commit=None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_concurrent_step_writers_preserve_every_step(self):
        from concurrent.futures import ThreadPoolExecutor

        def add(index: int) -> None:
            provenance.add_step(self.repo_root, "run-lock", task_id=f"T{index}", status="in_progress")
            provenance.update_step(self.repo_root, "run-lock", f"T{index}", status="completed")

        # This test checks that no step is lost, not the lock timeout (see
        # test_held_lock_bounds_every_writer); give queued writers room on slow runners.
        with patch.object(provenance, "RUN_LOCK_WAIT_SECONDS", 120), ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(16)))
        steps = provenance.get_run(self.repo_root, "run-lock")["steps"]
        self.assertEqual(sorted(step["task_id"] for step in steps), sorted(f"T{index}" for index in range(16)))
        self.assertTrue(all(step["status"] == "completed" for step in steps))

    def test_held_lock_bounds_every_writer(self):
        with patch.object(provenance, "RUN_LOCK_WAIT_SECONDS", 0.05):
            with provenance.run_lock(self.repo_root, "run-lock"):
                for write in (lambda: provenance.add_step(self.repo_root, "run-lock", task_id="T1"),
                              lambda: provenance.update_step(self.repo_root, "run-lock", "T1", status="completed"),
                              lambda: provenance.update_orchestration_metrics(self.repo_root, "run-lock",
                                                                              wall_clock_ms=10)):
                    with self.assertRaises(provenance.RunLockTimeout):
                        write()
        provenance.add_step(self.repo_root, "run-lock", task_id="T1")
        self.assertEqual(len(provenance.get_run(self.repo_root, "run-lock")["steps"]), 1)


if __name__ == "__main__":
    unittest.main()
