#!/usr/bin/env python3
"""Deterministic tests for the task-classifier benchmark."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = REPO_ROOT / "benchmarks" / "classification"
TOOLS_DIR = REPO_ROOT / "tools"
for path in (str(BENCHMARK_DIR), str(TOOLS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import evaluate as benchmark  # noqa: E402
import routing_engine as engine  # noqa: E402


class ClassifierBenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = engine.load_policy_assets(REPO_ROOT)
        cls.cases = benchmark.load_cases(BENCHMARK_DIR / "cases.yaml")
        cls.fields = benchmark.classifier_fields(cls.assets.task_schema)

    def test_fixture_gold_is_schema_valid(self):
        for case in self.cases:
            task = benchmark.validate_case(case, self.fields, self.assets)
            self.assertEqual(task["id"], case["id"])

    def test_classifier_field_contract_matches_current_task_schema(self):
        self.assertEqual(
            self.fields,
            (
                "task_type",
                "failure_cost",
                "semantic_correctness",
                "ambiguity",
                "implementation_scope",
                "implementation_required",
                "cross_layer",
                "behavior_preservation",
                "diagnosis_required",
                "assumptions_must_be_explicit",
                "many_routine_decisions",
                "production_feature",
                "security_sensitive",
                "destructive_operation",
            ),
        )

    def test_gold_predictions_score_perfectly(self):
        predictions = {
            case["id"]: {
                "case_id": case["id"],
                "classification": dict(case["gold_classification"]),
            }
            for case in self.cases
        }
        result = benchmark.evaluate(
            self.cases,
            predictions,
            self.assets,
            require_all=True,
        )
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(result["exact_classification_accuracy"], 1.0)
        self.assertEqual(result["macro_field_accuracy"], 1.0)
        self.assertEqual(result["route_accuracy"], 1.0)
        self.assertEqual(result["policy_under_routing_rate"], 0.0)
        self.assertEqual(result["policy_over_routing_rate"], 0.0)
        self.assertEqual(result["execution_decision_accuracy"], 1.0)
        self.assertEqual(result["route_accuracy_with_path_floors"], 1.0)
        self.assertEqual(result["policy_under_routing_rate_with_path_floors"], 0.0)
        self.assertEqual(result["policy_over_routing_rate_with_path_floors"], 0.0)
        self.assertGreaterEqual(result["path_floor_cases"], 3)
        self.assertEqual(result["mismatches"], [])

    def test_lower_risk_prediction_can_be_detected_as_policy_under_routing(self):
        case = next(c for c in self.cases if c["id"] == "classifier-008-architectural-ambiguity")
        prediction = dict(case["gold_classification"])
        prediction.update(
            {
                "failure_cost": "medium",
                "ambiguity": 2,
                "implementation_scope": "small",
                "cross_layer": False,
                "production_feature": False,
            }
        )
        result = benchmark.evaluate(
            [case],
            {
                case["id"]: {
                    "case_id": case["id"],
                    "classification": prediction,
                }
            },
            self.assets,
            require_all=True,
        )
        self.assertEqual(result["route_accuracy"], 0.0)
        self.assertEqual(result["policy_under_routing_rate"], 1.0)
        self.assertEqual(result["policy_over_routing_rate"], 0.0)

    def test_same_route_capacity_change_changes_execution_decision(self):
        case = next(c for c in self.cases if c["id"] == "classifier-004-ui-polish")
        prediction = dict(case["gold_classification"])
        prediction["ambiguity"] = 5
        result = benchmark.evaluate(
            [case],
            {
                case["id"]: {
                    "case_id": case["id"],
                    "classification": prediction,
                }
            },
            self.assets,
            require_all=True,
        )
        self.assertEqual(result["route_accuracy"], 1.0)
        self.assertEqual(result["execution_decision_accuracy"], 0.0)
        mismatch = result["mismatches"][0]
        self.assertNotEqual(mismatch["gold_capacities"], mismatch["predicted_capacities"])

    def test_partial_predictions_report_coverage(self):
        case = self.cases[0]
        result = benchmark.evaluate(
            self.cases,
            {
                case["id"]: {
                    "case_id": case["id"],
                    "classification": dict(case["gold_classification"]),
                }
            },
            self.assets,
        )
        self.assertEqual(result["cases_scored"], 1)
        self.assertEqual(result["coverage"], 1 / len(self.cases))
        self.assertEqual(len(result["missing_predictions"]), len(self.cases) - 1)

    def test_gold_with_unjustified_escalation_is_rejected(self):
        case = copy.deepcopy(next(c for c in self.cases if c["id"] == "classifier-001-debugging-race"))
        del case["gold_classification"]["escalation_evidence"]
        with self.assertRaisesRegex(benchmark.BenchmarkError, "CLASSIFICATION_EVIDENCE_MISSING"):
            benchmark.validate_case(case, self.fields, self.assets)

    def test_prediction_without_evidence_is_invalid_like_route_task(self):
        case = next(c for c in self.cases if c["id"] == "classifier-014-git-push-main")
        prediction = dict(case["gold_classification"])
        prediction.update({"assumptions_must_be_explicit": True, "semantic_correctness": "critical"})
        result = benchmark.evaluate(
            [case], {case["id"]: {"case_id": case["id"], "classification": prediction}}, self.assets,
        )
        self.assertEqual(result["cases_scored"], 0)
        self.assertIn("CLASSIFICATION_EVIDENCE_MISSING", result["invalid_predictions"][0]["error"])
        self.assertIn("semantic_correctness", result["invalid_predictions"][0]["error"])

        prediction["escalation_evidence"] = {
            "assumptions_must_be_explicit": "AGENTS.md says not to invent facts.",
            "semantic_correctness": "The repository is scientific.",
        }
        result = benchmark.evaluate(
            [case], {case["id"]: {"case_id": case["id"], "classification": prediction}}, self.assets,
        )
        self.assertEqual(result["cases_scored"], 1)
        self.assertEqual(result["policy_over_routing_rate"], 1.0)
        self.assertEqual(result["escalation_rates"]["assumptions_must_be_explicit"], {"predicted": 1.0, "gold": 0.0})

    def test_expected_decision_must_match_engine(self):
        case = copy.deepcopy(next(c for c in self.cases if c["id"] == "classifier-020-ci-lint-fix-known-cause"))
        case["expected_decision"]["route"] = "CODEX"
        with self.assertRaisesRegex(benchmark.BenchmarkError, "expected_decision"):
            benchmark.validate_case(case, self.fields, self.assets)

    def test_operational_gold_cases_use_low_risk_flags(self):
        # Path-floor cases pin a decision too, but they are not operational work.
        operational = [c for c in self.cases if "expected_decision" in c and "project_routing" not in c]
        self.assertGreaterEqual(len(operational), 9)
        for case in operational:
            with self.subTest(case=case["id"]):
                gold = case["gold_classification"]
                for field, value in engine.ESCALATING_CLASSIFICATION_VALUES.items():
                    self.assertNotEqual(gold[field], value)
                self.assertFalse(gold["diagnosis_required"])
                if engine.match_all_path_floors(case["task_facts"]["files_or_areas"], None, self.assets.policy, declared=True):
                    # A built-in path floor (.pi/ and friends) is not a rubric judgment; without path floors the
                    # low-risk gold still stays off the strongest route.
                    task = benchmark.materialize_task(case["task_facts"], gold)
                    self.assertNotEqual(benchmark.decision_signature(task, self.assets, path_floors=False)["route"],
                                        "CODEX_CLAUDE_CODEX")
                    continue
                self.assertNotEqual(case["expected_decision"]["route"], "CODEX_CLAUDE_CODEX")

    def _understated(self, case_id: str) -> tuple[dict, dict]:
        """A request-text-only prediction: every risk flag at its low-risk value."""
        case = next(c for c in self.cases if c["id"] == case_id)
        prediction = {field: value for field, value in case["gold_classification"].items() if field != "escalation_evidence"}
        prediction.update({"task_type": "implementation" if prediction["task_type"] == "migration" else prediction["task_type"],
                           "failure_cost": "low", "behavior_preservation": "not_required",
                           "security_sensitive": False})
        return case, {"case_id": case_id, "classification": prediction}

    def test_path_floors_recover_understated_classifications(self):
        floored = ["classifier-023-path-floor-migration", "classifier-024-path-floor-release-workflow"]
        pairs = [self._understated(case_id) for case_id in floored]
        result = benchmark.evaluate([case for case, _ in pairs], {p["case_id"]: p for _, p in pairs},
                                    self.assets, require_all=True)
        self.assertEqual(result["path_floor_cases"], 2)
        self.assertEqual(result["policy_under_routing_rate"], 1.0)
        self.assertEqual(result["policy_under_routing_rate_with_path_floors"], 0.0)
        self.assertEqual(result["route_accuracy_with_path_floors"], 1.0)
        for mismatch in result["mismatches"]:
            self.assertEqual(mismatch["predicted_route_with_path_floors"], mismatch["gold_route_with_path_floors"])

    def test_baseline_preserves_non_path_project_risk_overrides(self):
        case, prediction = self._understated("classifier-023-path-floor-migration")
        case = copy.deepcopy(case)
        case["project_routing"] = {"risk_overrides": {"failure_cost": "high"}}
        case.pop("expected_decision", None)
        result = benchmark.evaluate([case], {case["id"]: prediction}, self.assets, require_all=True)
        self.assertEqual(result["policy_under_routing_rate"], 0.0)
        self.assertEqual(result["policy_under_routing_rate_with_path_floors"], 0.0)
        self.assertEqual(result["route_accuracy"], result["route_accuracy_with_path_floors"])

    def test_unmatched_path_floor_changes_nothing(self):
        case = next(c for c in self.cases if c["id"] == "classifier-025-path-floor-unmatched-docs")
        task = benchmark.materialize_task(case["task_facts"], case["gold_classification"])
        self.assertEqual(benchmark.decision_signature(task, self.assets),
                         benchmark.decision_signature(task, self.assets, case["project_routing"]))

    def test_path_floor_cases_cover_floored_paths(self):
        for case_id in ("classifier-023-path-floor-migration", "classifier-024-path-floor-release-workflow"):
            with self.subTest(case=case_id):
                case = next(c for c in self.cases if c["id"] == case_id)
                matches = engine.match_path_floors(case["task_facts"]["files_or_areas"],
                                                   case["project_routing"]["path_floors"])
                self.assertTrue(matches)

    def test_invalid_case_project_routing_is_rejected(self):
        case = copy.deepcopy(next(c for c in self.cases if c["id"] == "classifier-023-path-floor-migration"))
        case["project_routing"]["path_floors"][0]["floors"] = {"failure_cost": "lowest"}
        with self.assertRaisesRegex(benchmark.BenchmarkError, "invalid project_routing"):
            benchmark.validate_case(case, self.fields, self.assets)

    def test_jsonl_loader_rejects_duplicate_case_ids(self):
        case = self.cases[0]
        record = json.dumps(
            {
                "case_id": case["id"],
                "classification": case["gold_classification"],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "predictions.jsonl"
            path.write_text(record + "\n" + record + "\n", encoding="utf-8")
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.load_predictions(path)


if __name__ == "__main__":
    unittest.main()
