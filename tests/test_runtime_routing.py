#!/usr/bin/env python3
"""Runtime routing tests: exercise routing_engine.py directly and through the
tools/route_task.py stdio adapter, using tests/routing-cases.yaml as the sole
source of fixtures (no hand-duplicated task data).

Run with: python3 -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
ROUTE_TASK_SCRIPT = TOOLS_DIR / "route_task.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "routing-cases.yaml"
V04_INVARIANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "v0.4-routing-invariants.json"

sys.path.insert(0, str(TOOLS_DIR))

import routing_engine as engine  # noqa: E402


LOW_RISK_DEBUG_TASK = {
    "id": "low-risk-codex-debugging",
    "goal": "Fix a trivial, well-understood off-by-one in a log formatter.",
    "task_type": "debugging",
    "failure_cost": "low",
    "semantic_correctness": "normal",
    "ambiguity": 2,
    "implementation_scope": "small",
    "implementation_required": True,
    "cross_layer": False,
    "behavior_preservation": "not_required",
    "diagnosis_required": False,
    "assumptions_must_be_explicit": False,
    "many_routine_decisions": False,
    "production_feature": False,
    "security_sensitive": False,
    "destructive_operation": False,
    "dependencies": [],
    "acceptance_criteria": ["Log lines no longer drop the final character."],
    "files_or_areas": ["src/logging/formatter"],
}

GIT_PUSH_TASK = dict(
    LOW_RISK_DEBUG_TASK,
    id="git-push",
    goal="Push the already-committed branch to origin.",
    task_type="vcs_operation",
    acceptance_criteria=["origin has the branch head."],
    files_or_areas=["."],
)

BOUNDED_CRITICAL_FEATURE_TASK = dict(
    LOW_RISK_DEBUG_TASK,
    id="bounded-feature",
    goal="Add a bounded export option whose output format is contractually fixed.",
    task_type="end_to_end_feature",
    failure_cost="medium",
    semantic_correctness="critical",
    ambiguity=5,
    implementation_scope="medium",
    cross_layer=True,
    production_feature=True,
    escalation_evidence={"semantic_correctness": "Downstream consumers parse the exact format."},
)

CRITICAL_AMBIGUOUS_TASK = dict(
    BOUNDED_CRITICAL_FEATURE_TASK,
    id="critical-ambiguous",
    failure_cost="high",
    ambiguity=8,
    escalation_evidence={
        "failure_cost": "A wrong result corrupts persisted user data.",
        "semantic_correctness": "Downstream consumers parse the exact format.",
        "ambiguity": "The required semantics are disputed between two specs.",
    },
)


def load_fixtures() -> dict:
    return engine.load_yaml(FIXTURES_PATH)


def load_assets() -> engine.PolicyAssets:
    return engine.load_policy_assets(REPO_ROOT)


def expected_plan_for(assets: engine.PolicyAssets, task: dict, route: str) -> dict:
    primary_intent = engine.derive_primary_intent(task)
    return assets.policy["execution"][route]["primary_intents"][primary_intent]


def run_cli(
    classified_task: dict,
    project_routing_path: Path | None = None,
    unavailable_routes: list[str] | None = None,
) -> subprocess.CompletedProcess:
    args = [sys.executable, str(ROUTE_TASK_SCRIPT)]
    if project_routing_path is not None:
        args += ["--project-routing", str(project_routing_path)]
    payload = {"classified_task": classified_task}
    if unavailable_routes is not None:
        payload["unavailable_routes"] = unavailable_routes
    return subprocess.run(
        args,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


class EngineCasesTest(unittest.TestCase):
    """tools/routing_engine.py exercised directly (no subprocess)."""

    @classmethod
    def setUpClass(cls):
        cls.assets = load_assets()
        cls.fixtures = load_fixtures()

    def test_cases_reproduced_by_engine(self):
        for case in self.fixtures["cases"]:
            with self.subTest(case=case["id"]):
                matched, selected = engine.route_task(case["task"], self.assets.policy)
                self.assertEqual(matched, set(case["expected_matches"]))
                self.assertEqual(selected, case["expected_route"])

    def test_override_cases_reproduced_by_engine(self):
        for case in self.fixtures["override_cases"]:
            with self.subTest(case=case["id"]):
                resolution = engine.resolve_route(
                    case["task"], self.assets.policy, case["project_routing"], self.assets.task_schema
                )
                self.assertEqual(resolution.base_matches, set(case["expected_matches"]))
                self.assertEqual(resolution.base_route, case["expected_route"])
                union = resolution.base_matches | resolution.effective_matches
                self.assertEqual(union, set(case["expected_matches_with_overrides"]))
                self.assertEqual(resolution.selected_route, case["expected_route_with_overrides"])

    def test_override_cases_never_downgrade(self):
        precedence = self.assets.policy["precedence"]
        for case in self.fixtures["override_cases"]:
            with self.subTest(case=case["id"]):
                resolution = engine.resolve_route(
                    case["task"], self.assets.policy, case["project_routing"], self.assets.task_schema
                )
                base_rank = precedence.index(resolution.base_route)
                override_rank = precedence.index(resolution.selected_route)
                self.assertLessEqual(
                    override_rank,
                    base_rank,
                    f"{case['id']}: overrides downgraded {resolution.base_route!r} -> {resolution.selected_route!r}",
                )

    def test_fallback_case_reproduced_by_engine(self):
        case = next(c for c in self.fixtures["override_cases"] if c["id"] == "override-c-fallback-not-downgraded")
        resolution = engine.resolve_route(
            case["task"], self.assets.policy, case["project_routing"], self.assets.task_schema
        )
        self.assertEqual(resolution.base_route, self.assets.policy["fallback_route"])
        self.assertEqual(resolution.selected_route, self.assets.policy["fallback_route"])
        self.assertTrue(resolution.fallback_applied)

    def test_unavailable_preferred_route_falls_back_to_next_policy_valid_route(self):
        case = next(c for c in self.fixtures["cases"] if c["expected_route"] == "CODEX_CLAUDE_CODEX")
        resolution = engine.resolve_route(
            case["task"],
            self.assets.policy,
            None,
            self.assets.task_schema,
            unavailable_routes=["CODEX_CLAUDE_CODEX"],
        )
        self.assertIn("CODEX_CLAUDE_CODEX", resolution.base_matches)
        self.assertIn("CODEX", resolution.base_matches)
        self.assertEqual(resolution.base_route, "CODEX_CLAUDE_CODEX")
        self.assertEqual(resolution.selected_route, "CODEX")
        self.assertEqual(resolution.route_availability["excluded_routes"], ["CODEX_CLAUDE_CODEX"])

    def _override(self, case_id):
        return next(c for c in self.fixtures["override_cases"] if c["id"] == case_id)

    def _case(self, case_id):
        return next(c for c in self.fixtures["cases"] if c["id"] == case_id)

    def test_excluding_every_candidate_fails_closed_without_fallback(self):
        case = self._case("case-5-broad-critical-semantics")
        with self.assertRaises(engine.NoAvailableRouteError) as raised:
            engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema,
                                 unavailable_routes=["CODEX_CLAUDE_CODEX", "CODEX"])
        self.assertEqual(raised.exception.unavailable_routes, ["CODEX", "CODEX_CLAUDE_CODEX"])

    def test_fallback_route_is_never_substituted_for_an_excluded_candidate(self):
        # case-1 matches only CODEX; CLAUDE (no Codex verification) must not replace it.
        case = self._case("case-1-diagnosis-required")
        with self.assertRaises(engine.NoAvailableRouteError):
            engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema,
                                 unavailable_routes=["CODEX"])
        fallback = self._override("override-c-fallback-not-downgraded")
        with self.assertRaises(engine.NoAvailableRouteError):
            engine.resolve_route(fallback["task"], self.assets.policy, fallback["project_routing"],
                                 self.assets.task_schema, unavailable_routes=["CODEX"])

    def test_availability_reroute_never_drops_codex_verification(self):
        reviewed = self._case("case-3-cross-layer-feature")  # matches CLAUDE and CLAUDE_CODEX
        with self.assertRaises(engine.NoAvailableRouteError):
            engine.resolve_route(reviewed["task"], self.assets.policy, None, self.assets.task_schema,
                                 unavailable_routes=["CLAUDE_CODEX"])

    def test_availability_reroute_respects_project_risk_floor(self):
        # Base classification matches only CLAUDE; the security floor escalates to
        # CODEX_CLAUDE_CODEX. Losing that route must not fall back to the
        # base-only, unreviewed CLAUDE route.
        case = self._override("override-a-security-sensitive-floor")
        with self.assertRaises(engine.NoAvailableRouteError):
            engine.resolve_route(case["task"], self.assets.policy, case["project_routing"],
                                 self.assets.task_schema, unavailable_routes=["CODEX_CLAUDE_CODEX"])
        floored = self._override("override-b-semantic-correctness-floor")
        resolution = engine.resolve_route(floored["task"], self.assets.policy, floored["project_routing"],
                                          self.assets.task_schema, unavailable_routes=["CODEX_CLAUDE_CODEX"])
        self.assertEqual(resolution.selected_route, "CODEX")
        self.assertIn("CODEX", resolution.effective_matches)

    def test_excluding_an_unselected_route_does_not_change_selection(self):
        case = self._case("case-5-broad-critical-semantics")
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema,
                                          unavailable_routes=["CLAUDE"])
        self.assertEqual(resolution.selected_route, "CODEX_CLAUDE_CODEX")

    def test_unknown_unavailable_route_is_rejected(self):
        case = self._case("case-5-broad-critical-semantics")
        with self.assertRaises(ValueError):
            engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema,
                                 unavailable_routes=["GPT_ONLY"])

    def test_forged_availability_exclusions_fail_validation(self):
        case = self._case("case-5-broad-critical-semantics")
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema,
                                          unavailable_routes=["CODEX_CLAUDE_CODEX"])
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        for forged in ({"excluded_routes": ["CODEX_CLAUDE_CODEX", "CODEX"],
                        "selection_reason": result["route_availability"]["selection_reason"]},
                       {"excluded_routes": ["NOT_A_ROUTE"]}, "excluded"):
            with self.subTest(forged=forged):
                tampered = copy.deepcopy(result)
                tampered["route_availability"] = forged
                self.assertNotEqual(engine.validate_route_task_result(tampered, self.assets), [])
        stripped = copy.deepcopy(result)
        del stripped["route_availability"]
        self.assertNotEqual(engine.validate_route_task_result(stripped, self.assets), [])

    def test_availability_aware_result_validates_against_actual_selected_route(self):
        case = next(c for c in self.fixtures["cases"] if c["expected_route"] == "CODEX_CLAUDE_CODEX")
        resolution = engine.resolve_route(
            case["task"],
            self.assets.policy,
            None,
            self.assets.task_schema,
            unavailable_routes=["CODEX_CLAUDE_CODEX"],
        )
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        self.assertEqual(result["selected_route"], "CODEX")
        self.assertEqual(result["execution_plan"]["steps"][0]["id"], "codex_worker")
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])

    def test_invalid_project_routing_examples_rejected(self):
        for case in self.fixtures["invalid_project_routing_examples"]:
            with self.subTest(case=case["id"]):
                issues = engine.validate_project_routing_instance(case["config"], self.assets.project_schema)
                self.assertTrue(issues, f"expected schema rejection for {case['id']}")

    def test_invalid_task_produces_normalized_errors(self):
        issues = engine.validate_task_instance({"id": "incomplete"}, self.assets.task_schema)
        self.assertTrue(issues)
        for issue in issues:
            self.assertTrue(issue.path.startswith("classified_task"))
            self.assertTrue(issue.message)

    def test_type_sensitive_equality(self):
        self.assertTrue(engine.json_eq(True, True))
        self.assertFalse(engine.json_eq(True, 1))
        self.assertFalse(engine.json_eq(1, 1.0))
        self.assertTrue(engine.json_eq("x", "x"))

    def test_evaluator_is_deterministic_across_runs(self):
        case = self.fixtures["cases"][0]
        first = engine.route_task(case["task"], self.assets.policy)
        second = engine.route_task(case["task"], self.assets.policy)
        self.assertEqual(first, second)

    # -- Execution-plan materialization: primary-intent derivation ----------

    def test_derive_primary_intent_implementation_required_wins(self):
        self.assertEqual(
            engine.derive_primary_intent({"implementation_required": True, "task_type": "review"}),
            "implementation",
        )

    def test_derive_primary_intent_review_task_type(self):
        for task_type in ("review", "security_review"):
            with self.subTest(task_type=task_type):
                self.assertEqual(
                    engine.derive_primary_intent(
                        {"implementation_required": False, "task_type": task_type}
                    ),
                    "review",
                )

    def test_derive_primary_intent_defaults_to_analysis(self):
        self.assertEqual(
            engine.derive_primary_intent({"implementation_required": False, "task_type": "exploration"}),
            "analysis",
        )

    def test_materialize_execution_plan_for_every_route_and_intent(self):
        for route in engine.ROUTES:
            for intent in engine.PRIMARY_INTENTS:
                with self.subTest(route=route, intent=intent):
                    plan = engine.materialize_execution_plan(
                        self.assets.policy, self.assets.execution_plan_schema, route, intent
                    )
                    issues = engine.validate_execution_plan(
                        plan, self.assets.execution_plan_schema, label="test"
                    )
                    self.assertEqual(issues, [])
                    self.assertEqual(
                        plan, self.assets.policy["execution"][route]["primary_intents"][intent]
                    )

    def test_worker_mutation_permission_matches_intent(self):
        """worker steps allow mutation iff intent is implementation, for every
        route/primary-intent combination."""
        for route in engine.ROUTES:
            for intent in engine.PRIMARY_INTENTS:
                plan = self.assets.policy["execution"][route]["primary_intents"][intent]
                worker_steps = [s for s in plan["steps"] if s["role"] == "worker"]
                self.assertEqual(len(worker_steps), 1, f"{route}/{intent} must have exactly one worker step")
                worker = worker_steps[0]
                expected_mutation = "allowed" if intent == "implementation" else "forbidden"
                self.assertEqual(
                    worker["mutation_permission"],
                    expected_mutation,
                    f"{route}/{intent}: worker mutation_permission",
                )
                self.assertEqual(worker["intent"], intent)

    def test_claude_codex_always_fresh_forbidden_independent_review(self):
        for intent in engine.PRIMARY_INTENTS:
            plan = self.assets.policy["execution"]["CLAUDE_CODEX"]["primary_intents"][intent]
            self.assertTrue(plan["independent_review_required"])
            reviewers = [s for s in plan["steps"] if s["independent_review"]]
            self.assertEqual(len(reviewers), 1)
            reviewer = reviewers[0]
            self.assertEqual(reviewer["role"], "reviewer")
            self.assertEqual(reviewer["family"], "codex")
            self.assertTrue(reviewer["fresh_context"])
            self.assertEqual(reviewer["mutation_permission"], "forbidden")
            self.assertEqual(reviewer["intent"], "review")

    def test_codex_claude_codex_always_two_distinct_fresh_codex_steps(self):
        for intent in engine.PRIMARY_INTENTS:
            plan = self.assets.policy["execution"]["CODEX_CLAUDE_CODEX"]["primary_intents"][intent]
            self.assertTrue(plan["independent_review_required"])
            codex_steps = [s for s in plan["steps"] if s["family"] == "codex"]
            self.assertEqual(len(codex_steps), 2)
            ids = {s["id"] for s in codex_steps}
            self.assertEqual(len(ids), 2, "the two Codex steps must be distinct agent identities")
            for step in codex_steps:
                self.assertTrue(step["fresh_context"])
            reviewers = [s for s in codex_steps if s["independent_review"]]
            self.assertEqual(len(reviewers), 1)
            self.assertEqual(reviewers[0]["role"], "reviewer")
            self.assertEqual(reviewers[0]["mutation_permission"], "forbidden")

    def test_codex_and_claude_solo_routes_never_require_independent_review(self):
        for route in ("CODEX", "CLAUDE"):
            for intent in engine.PRIMARY_INTENTS:
                plan = self.assets.policy["execution"][route]["primary_intents"][intent]
                self.assertFalse(plan["independent_review_required"])
                self.assertFalse(any(s["independent_review"] for s in plan["steps"]))

    def test_every_step_in_policy_requires_fresh_context(self):
        for route in engine.ROUTES:
            for intent in engine.PRIMARY_INTENTS:
                plan = self.assets.policy["execution"][route]["primary_intents"][intent]
                for step in plan["steps"]:
                    self.assertTrue(step["fresh_context"], f"{route}/{intent}/{step['id']}")

    def test_v04_route_and_safety_invariants_are_frozen(self):
        frozen = json.loads(V04_INVARIANTS_PATH.read_text(encoding="utf-8"))
        policy = self.assets.policy
        def legacy_ambiguity_predicates(value):
            if isinstance(value, list):
                return [legacy_ambiguity_predicates(item) for item in value]
            if isinstance(value, dict):
                if value.get("field") == "ambiguity":
                    mapped = {("<=", 3): ("==", "low"),
                              ("<=", 6): ("!=", "high"),
                              (">=", 7): ("==", "high")}
                    old_operator, old_value = mapped[(value["operator"], value["value"])]
                    return {"field": "ambiguity", "operator": old_operator, "value": old_value}
                return {key: legacy_ambiguity_predicates(item) for key, item in value.items()}
            return value
        self.assertEqual(legacy_ambiguity_predicates(policy["routes"]), frozen["routes"])
        self.assertEqual(policy["precedence"], frozen["precedence"])
        self.assertEqual(policy["fallback_route"], frozen["fallback_route"])
        # Explanatory classification text may evolve; the schema contract stays frozen.
        actual_schema = copy.deepcopy(self.assets.task_schema)
        frozen_schema = copy.deepcopy(frozen["task_schema"])
        actual_schema["properties"].pop("ambiguity")
        frozen_schema["properties"].pop("ambiguity")
        actual_schema["properties"]["escalation_evidence"]["description"] = frozen_schema["properties"]["escalation_evidence"]["description"]
        actual_schema["properties"].pop("ambiguity_assessment")
        actual_schema["properties"].pop("work_profile")
        actual_schema.pop("$defs")
        self.assertEqual(actual_schema, frozen_schema)
        self.assertEqual(list(engine.ORDERED_OVERRIDE_FIELDS), frozen["risk_override_fields"]["ordered"])
        self.assertEqual(list(engine.SAFETY_OVERRIDE_FIELDS), frozen["risk_override_fields"]["safety"])
        keep = (
            "id", "order", "role", "family", "intent", "capacity_tier", "fresh_context",
            "mutation_permission", "depends_on", "independent_review",
            "reasoning_effort", "context_strategy",
        )
        actual = {}
        for route, entry in policy["execution"].items():
            actual[route] = {}
            for intent, plan in entry["primary_intents"].items():
                actual[route][intent] = {
                    "independent_review_required": plan["independent_review_required"],
                    "steps": [{key: step[key] for key in keep} for step in plan["steps"]],
                }
        self.assertEqual(actual, frozen["plans_without_schema_version"])

    def test_policy_v3_execution_metadata_is_supported_and_cross_intent_stable(self):
        self.assertEqual(self.assets.policy["version"], 7)
        for route in engine.ROUTES:
            plans = self.assets.policy["execution"][route]["primary_intents"]
            for _intent, plan in plans.items():
                self.assertEqual(plan["schema_version"], 3)
                for step in plan["steps"]:
                    profile = self.assets.policy["model_profiles"][step["family"]]
                    self.assertIn(step["capacity_tier"], profile["capacity_models"])
                    self.assertIn(step["reasoning_effort"], profile["reasoning_efforts"])
                    self.assertIn(step["context_strategy"], profile["context_strategies"])
            by_intent = list(plans.values())
            for index in range(len(by_intent[0]["steps"])):
                values = {
                    tuple(plan["steps"][index][field] for field in engine.EXECUTION_METADATA_FIELDS)
                    for plan in by_intent
                }
                self.assertEqual(len(values), 1, f"{route} step {index} metadata drifted across intents")

    def test_five_tier_profiles_use_cost_first_default_models(self):
        profiles = self.assets.policy["model_profiles"]
        self.assertEqual(engine.CAPACITY_TIER_ORDER, ("light", "economy", "standard", "high", "max"))
        expected = {
            "codex": {
                "light": "openai-codex/gpt-6-luna",
                "economy": "openai-codex/gpt-6-luna",
                "standard": "openai-codex/gpt-6-luna",
                "high": "openai-codex/gpt-6.1-sol",
                "max": "openai-codex/gpt-6-astra",
            },
            "claude": {
                "light": "haiku",
                "economy": "haiku",
                "standard": "sonnet",
                "high": "anthropic/claude-opus-5-5",
                "max": "anthropic/claude-opus-5-5",
            },
        }
        for family, tiers in expected.items():
            with self.subTest(family=family):
                models = profiles[family]["capacity_models"]
                self.assertEqual(set(models), engine.CAPACITY_TIERS)
                self.assertEqual({tier: entry["model"] for tier, entry in models.items()}, tiers)
        # The policy authors only standard/high; light/economy/max are assigned post-route.
        for route in engine.ROUTES:
            for plan in self.assets.policy["execution"][route]["primary_intents"].values():
                for step in plan["steps"]:
                    self.assertIn(step["capacity_tier"], {"standard", "high"})

    def _assert_economy_assignment(self, assignment, *, family, rule_name):
        profile_tier = self.assets.policy["model_profiles"][family]["capacity_models"]["economy"]
        self.assertEqual(assignment["assigned_capacity_tier"], "economy")
        self.assertEqual(assignment["assigned_reasoning_effort"], "low")
        self.assertEqual(assignment["rule"], rule_name)
        # Blocking evidence may only come from earlier, higher-priority rules.
        self.assertTrue(all(p["rule"] != rule_name for p in assignment["blocking_predicates"]))
        self.assertTrue([p for p in assignment["guard_predicates_passed"] if p["rule"] == rule_name])
        self.assertEqual(assignment["selected_model_profile"], {
            "family": family,
            "capacity_tier": "economy",
            "model": profile_tier["model"],
            "context_window_tier": profile_tier["context_window_tier"],
            "ambiguity_capacity": profile_tier["ambiguity_capacity"],
        })

    def test_low_risk_claude_worker_gets_post_route_economy_assignment(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        self.assertEqual(result["selected_route"], "CLAUDE")
        self.assertEqual(result["execution_plan"]["steps"][0]["capacity_tier"], "standard")
        assignments = result["capacity_assignments"]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["step_id"], "claude_worker")
        self.assertEqual(assignments[0]["authored_capacity_tier"], "standard")
        self._assert_economy_assignment(assignments[0], family="claude", rule_name="low-risk-worker")
        self.assertEqual(set(result["backend_bindings"]["claude"]["capacity_models"]), {"economy"})

    def test_low_risk_codex_worker_gets_post_route_economy_assignment(self):
        low_risk_codex_task = {
            "id": "low-risk-codex-debugging",
            "goal": "Fix a trivial, well-understood off-by-one in a log formatter.",
            "task_type": "debugging",
            "failure_cost": "low",
            "semantic_correctness": "normal",
            "ambiguity": 2,
            "implementation_scope": "small",
            "implementation_required": True,
            "cross_layer": False,
            "behavior_preservation": "not_required",
            "diagnosis_required": False,
            "assumptions_must_be_explicit": False,
            "many_routine_decisions": False,
            "production_feature": False,
            "security_sensitive": False,
            "destructive_operation": False,
            "dependencies": [],
            "acceptance_criteria": ["Log lines no longer drop the final character."],
            "files_or_areas": ["src/logging/formatter"],
        }
        resolution = engine.resolve_route(low_risk_codex_task, self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        self.assertEqual(result["selected_route"], "CODEX")
        assignments = result["capacity_assignments"]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["step_id"], "codex_worker")
        self.assertEqual(assignments[0]["authored_capacity_tier"], "standard")
        self._assert_economy_assignment(assignments[0], family="codex", rule_name="low-risk-worker")
        self.assertEqual(set(result["backend_bindings"]["codex"]["capacity_models"]), {"economy"})

    def test_single_unsafe_signal_blocks_codex_economy_assignment(self):
        base_task = {
            "id": "low-risk-codex-debugging",
            "goal": "Fix a trivial, well-understood off-by-one in a log formatter.",
            "task_type": "debugging",
            "failure_cost": "low",
            "semantic_correctness": "normal",
            "ambiguity": 2,
            "implementation_scope": "small",
            "implementation_required": True,
            "cross_layer": False,
            "behavior_preservation": "not_required",
            "diagnosis_required": False,
            "assumptions_must_be_explicit": False,
            "many_routine_decisions": False,
            "production_feature": False,
            "security_sensitive": False,
            "destructive_operation": False,
            "dependencies": [],
            "acceptance_criteria": ["Log lines no longer drop the final character."],
            "files_or_areas": ["src/logging/formatter"],
        }
        # failure_cost, semantic_correctness, security_sensitive, and
        # destructive_operation are deliberately excluded: raising any of them
        # independently matches CODEX_CLAUDE_CODEX (implementation_required is
        # already true), which would change the selected route and plan
        # topology rather than merely blocking economy within the same CODEX
        # route and its single codex_worker step.
        unsafe_overrides = {
            "ambiguity": 5,
            "diagnosis_required": True,
            "assumptions_must_be_explicit": True,
            "behavior_preservation": "required",
            "cross_layer": True,
            "production_feature": True,
            "implementation_scope": "broad",
        }
        for field, unsafe_value in unsafe_overrides.items():
            with self.subTest(field=field):
                task = dict(base_task, **{field: unsafe_value})
                resolution = engine.resolve_route(task, self.assets.policy, None, self.assets.task_schema)
                result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
                assignment = next(a for a in result["capacity_assignments"] if a["step_id"] == "codex_worker")
                self.assertEqual(assignment["assigned_capacity_tier"], "standard")
                self.assertIsNone(assignment["rule"])
                blocked_fields = {p["field"] for p in assignment["blocking_predicates"]}
                self.assertIn(field, blocked_fields)

    def test_security_and_destructive_signals_escalate_route_away_from_economy(self):
        """security_sensitive and destructive_operation are consequential enough
        that the routing policy itself escalates to CODEX_CLAUDE_CODEX (an
        authored `high`-tier plan with no economy-eligible worker step) rather
        than merely blocking a same-route economy downgrade -- a stronger
        fail-closed guarantee than a blocked guard predicate."""
        base_task = {
            "id": "low-risk-codex-debugging",
            "goal": "Fix a trivial, well-understood off-by-one in a log formatter.",
            "task_type": "debugging",
            "failure_cost": "low",
            "semantic_correctness": "normal",
            "ambiguity": 2,
            "implementation_scope": "small",
            "implementation_required": True,
            "cross_layer": False,
            "behavior_preservation": "not_required",
            "diagnosis_required": False,
            "assumptions_must_be_explicit": False,
            "many_routine_decisions": False,
            "production_feature": False,
            "security_sensitive": False,
            "destructive_operation": False,
            "dependencies": [],
            "acceptance_criteria": ["Log lines no longer drop the final character."],
            "files_or_areas": ["src/logging/formatter"],
        }
        for field in ("security_sensitive", "destructive_operation"):
            with self.subTest(field=field):
                task = dict(base_task, **{field: True})
                resolution = engine.resolve_route(task, self.assets.policy, None, self.assets.task_schema)
                result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
                self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
                for assignment in result["capacity_assignments"]:
                    self.assertNotEqual(assignment["assigned_capacity_tier"], "economy")

    def test_effective_risk_floor_blocks_economy_assignment(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        project = {"risk_overrides": {"ambiguity": 5}}
        resolution = engine.resolve_route(case["task"], self.assets.policy, project, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, project, self.assets.execution_plan_schema)
        self.assertEqual(result["selected_route"], "CLAUDE")
        self.assertEqual(result["classification"]["effective"]["ambiguity"], 5)
        self.assertNotIn("economy", [a["assigned_capacity_tier"] for a in result["capacity_assignments"]])
        self.assertEqual(set(result["backend_bindings"]["claude"]["capacity_models"]), {"standard"})

    def test_review_intent_worker_is_never_economy(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        review_plan = engine.materialize_execution_plan(
            self.assets.policy, self.assets.execution_plan_schema, "CLAUDE", "review"
        )
        assignments = engine.assign_capacities(self.assets.policy, review_plan, case["task"])
        self.assertEqual(assignments[0]["assigned_capacity_tier"], "standard")
        self.assertIsNone(assignments[0]["rule"])

    def test_bounded_low_risk_codex_claude_codex_lowers_specifier_and_worker_but_never_reviewer(self):
        """Against a maximally low-risk classification the CODEX_CLAUDE_CODEX
        specifier and worker drop to `standard` through the bounded rules,
        while the independent reviewer always retains its authored `high`."""
        plan = engine.materialize_execution_plan(
            self.assets.policy, self.assets.execution_plan_schema, "CODEX_CLAUDE_CODEX", "implementation"
        )
        assignments = {a["step_id"]: a for a in engine.assign_capacities(self.assets.policy, plan, LOW_RISK_DEBUG_TASK)}
        self.assertEqual(assignments["codex_specifier"]["assigned_capacity_tier"], "standard")
        self.assertEqual(assignments["codex_specifier"]["rule"], "bounded-specifier")
        self.assertEqual(assignments["codex_specifier"]["assigned_reasoning_effort"], "medium")
        self.assertEqual(assignments["claude_worker"]["assigned_capacity_tier"], "standard")
        self.assertEqual(assignments["claude_worker"]["rule"], "bounded-implementation-worker")
        self.assertEqual(assignments["codex_reviewer"]["assigned_capacity_tier"], "high")
        self.assertIsNone(assignments["codex_reviewer"]["rule"])
        self.assertEqual(assignments["codex_reviewer"]["assigned_reasoning_effort"], "high")

    def test_policy_validation_rejects_invalid_v3_profiles_refs_and_metadata(self):
        mutations = []

        wrong_version = copy.deepcopy(self.assets.policy)
        wrong_version["version"] = 1
        mutations.append(("version", wrong_version))

        wrong_family = copy.deepcopy(self.assets.policy)
        wrong_family["model_profiles"]["codex"]["family"] = "claude"
        mutations.append(("profile family", wrong_family))

        missing_ref = copy.deepcopy(self.assets.policy)
        missing_ref["execution_backends"]["codex"]["model_profile_ref"] = "missing"
        mutations.append(("profile ref", missing_ref))

        invalid_window = copy.deepcopy(self.assets.policy)
        invalid_window["model_profiles"]["codex"]["capacity_models"]["standard"]["context_window_tier"] = "unknown"
        mutations.append(("context window", invalid_window))

        unsupported_reasoning = copy.deepcopy(self.assets.policy)
        unsupported_reasoning["model_profiles"]["codex"]["reasoning_efforts"] = ["low"]
        mutations.append(("reasoning capability", unsupported_reasoning))

        unsupported_context = copy.deepcopy(self.assets.policy)
        unsupported_context["model_profiles"]["claude"]["context_strategies"] = ["focused"]
        mutations.append(("context capability", unsupported_context))

        cross_intent_drift = copy.deepcopy(self.assets.policy)
        cross_intent_drift["execution"]["CLAUDE"]["primary_intents"]["analysis"]["steps"][0]["capacity_tier"] = "high"
        mutations.append(("cross-intent metadata", cross_intent_drift))

        unhashable_reasoning = copy.deepcopy(self.assets.policy)
        unhashable_reasoning["model_profiles"]["codex"]["reasoning_efforts"] = [{"bad": "value"}]
        mutations.append(("unhashable reasoning value", unhashable_reasoning))

        unhashable_context = copy.deepcopy(self.assets.policy)
        unhashable_context["model_profiles"]["codex"]["context_strategies"] = [["full"]]
        mutations.append(("unhashable context value", unhashable_context))

        non_string_profile_key = copy.deepcopy(self.assets.policy)
        non_string_profile_key["model_profiles"][True] = non_string_profile_key["model_profiles"].pop("codex")
        mutations.append(("non-string profile key", non_string_profile_key))

        none_capacity_models = copy.deepcopy(self.assets.policy)
        none_capacity_models["model_profiles"]["codex"]["capacity_models"] = None
        mutations.append(("none capacity models", none_capacity_models))

        none_reasoning_efforts = copy.deepcopy(self.assets.policy)
        none_reasoning_efforts["model_profiles"]["codex"]["reasoning_efforts"] = None
        mutations.append(("none reasoning efforts", none_reasoning_efforts))

        scalar_context_strategies = copy.deepcopy(self.assets.policy)
        scalar_context_strategies["model_profiles"]["codex"]["context_strategies"] = 1
        mutations.append(("scalar context strategies", scalar_context_strategies))

        list_context_window = copy.deepcopy(self.assets.policy)
        list_context_window["model_profiles"]["codex"]["capacity_models"]["standard"]["context_window_tier"] = ["standard"]
        mutations.append(("list context window tier", list_context_window))

        unhashable_rule_field = copy.deepcopy(self.assets.policy)
        unhashable_rule_field["capacity_assignment"]["rules"][0]["match"]["all"][0]["field"] = ["failure_cost"]
        mutations.append(("unhashable capacity rule field", unhashable_rule_field))

        mixed_rule_keys = copy.deepcopy(self.assets.policy)
        mixed_rule_keys["capacity_assignment"]["rules"][0]["match"]["all"][0][True] = "boom"
        mutations.append(("mixed capacity rule expression keys", mixed_rule_keys))

        missing_standard = copy.deepcopy(self.assets.policy)
        del missing_standard["model_profiles"]["claude"]["capacity_models"]["standard"]
        mutations.append(("mandatory standard profile", missing_standard))

        missing_codex_economy_profile = copy.deepcopy(self.assets.policy)
        del missing_codex_economy_profile["model_profiles"]["codex"]["capacity_models"]["economy"]
        mutations.append(("codex economy rule without a profile entry", missing_codex_economy_profile))

        malformed_capacity_rule = copy.deepcopy(self.assets.policy)
        malformed_capacity_rule["capacity_assignment"]["rules"][0]["step_match"]["role"] = "reviewer"
        mutations.append(("capacity rule targets reviewer", malformed_capacity_rule))

        missing_capacity_rule_match = copy.deepcopy(self.assets.policy)
        del missing_capacity_rule_match["capacity_assignment"]["rules"][0]["match"]
        mutations.append(("capacity rule missing match", missing_capacity_rule_match))

        weakened_capacity_guard = copy.deepcopy(self.assets.policy)
        weakened_capacity_guard["capacity_assignment"]["rules"][0]["match"]["all"].pop()
        mutations.append(("capacity rule missing low-risk guard", weakened_capacity_guard))

        duplicate_rule_name = copy.deepcopy(self.assets.policy)
        duplicate_rule_name["capacity_assignment"]["rules"][1]["name"] = "ops-light"
        mutations.append(("capacity rule duplicate name", duplicate_rule_name))

        missing_codex_rule = copy.deepcopy(self.assets.policy)
        missing_codex_rule["capacity_assignment"]["rules"] = [missing_codex_rule["capacity_assignment"]["rules"][0]]
        mutations.append(("capacity rule missing codex rule", missing_codex_rule))

        for label, policy in mutations:
            with self.subTest(label=label):
                issues = []
                engine.validate_policy_structure(policy, self.assets.field_info, self.assets.execution_plan_schema, issues)
                self.assertTrue(issues)

    def test_backend_bindings_resolve_family_to_tool_and_model(self):
        codex_plan = self.assets.policy["execution"]["CODEX"]["primary_intents"]["analysis"]
        bindings = engine.resolve_backend_bindings(self.assets.policy, codex_plan)
        codex_standard = self.assets.policy["model_profiles"]["codex"]["capacity_models"]["standard"]
        self.assertEqual(bindings, {
            "codex": {"tool": "spawn_subagent", "capacity_models": {"standard": codex_standard}},
        })

        claude_codex_plan = self.assets.policy["execution"]["CLAUDE_CODEX"]["primary_intents"]["implementation"]
        bindings = engine.resolve_backend_bindings(self.assets.policy, claude_codex_plan)
        self.assertEqual(bindings["claude"]["mutation_modes"], {"forbidden": "read", "allowed": "full"})
        self.assertEqual(
            bindings["claude"]["capacity_models"]["standard"],
            self.assets.policy["model_profiles"]["claude"]["capacity_models"]["standard"],
        )
        self.assertNotIn("high", bindings["claude"]["capacity_models"])
        self.assertNotIn("mutation_modes", bindings["codex"])

        high_plan = self.assets.policy["execution"]["CODEX_CLAUDE_CODEX"]["primary_intents"]["implementation"]
        high_bindings = engine.resolve_backend_bindings(self.assets.policy, high_plan)
        for binding in high_bindings.values():
            self.assertEqual(set(binding["capacity_models"]), {"high"})

    def test_route_result_validation_rejects_forged_capacity_assignment(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        result["capacity_assignments"][0]["assigned_capacity_tier"] = "standard"
        result["capacity_assignments"][0]["rule"] = None
        result["backend_bindings"] = engine.resolve_backend_bindings(
            self.assets.policy, result["execution_plan"], result["capacity_assignments"]
        )
        issues = engine.validate_route_task_result(result, self.assets)
        self.assertTrue(any(issue.path == "result.capacity_assignments" for issue in issues))

    def test_undowngraded_steps_keep_their_authored_reasoning_effort(self):
        for case in self.fixtures["cases"]:
            with self.subTest(case=case["id"]):
                resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
                result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
                if not result["ok"]:
                    continue
                for step, assignment in zip(result["execution_plan"]["steps"], result["capacity_assignments"], strict=True):
                    self.assertEqual(assignment["authored_reasoning_effort"], step["reasoning_effort"])
                    if assignment["rule"] is None:
                        self.assertEqual(assignment["assigned_reasoning_effort"], step["reasoning_effort"])
                    else:
                        self.assertLessEqual(
                            engine.REASONING_EFFORT_ORDER[assignment["assigned_reasoning_effort"]],
                            engine.REASONING_EFFORT_ORDER[step["reasoning_effort"]],
                        )

    def test_route_result_validation_rejects_forged_effort_escalation(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        result["capacity_assignments"][0]["assigned_reasoning_effort"] = "high"
        issues = engine.validate_route_task_result(result, self.assets)
        self.assertTrue(issues)
        with self.assertRaises(engine.RoutingAssetError):
            engine.resolve_backend_bindings(self.assets.policy, result["execution_plan"], result["capacity_assignments"])

    def test_capacity_rule_reasoning_effort_must_be_supported_by_family(self):
        field_info = engine.build_field_info(self.assets.task_schema)
        for bad in (None, "xhigh"):
            with self.subTest(effort=bad):
                policy = copy.deepcopy(self.assets.policy)
                if bad is None:
                    del policy["capacity_assignment"]["rules"][0]["assigned_reasoning_effort"]
                else:
                    policy["capacity_assignment"]["rules"][0]["assigned_reasoning_effort"] = bad
                issues: list = []
                engine.validate_capacity_assignment(policy, field_info, issues)
                self.assertTrue(issues)

    def test_route_result_validation_rejects_boolean_forged_as_numeric_in_capacity_evidence(self):
        # json_eq alone (via Python's native `==`) treats `False`/`True` as
        # equal to `0`/`1`, including inside nested dict/list equality. A
        # forged assignment that swaps a guard predicate's boolean `actual`
        # value for its numeric twin must still be rejected: only recursive,
        # type-sensitive comparison (json_deep_eq) catches this.
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])

        guard_predicates = result["capacity_assignments"][0]["guard_predicates_passed"]
        guard = next(p for p in guard_predicates if p["field"] == "cross_layer")
        self.assertIs(guard["actual"], False)
        guard["actual"] = 0
        issues = engine.validate_route_task_result(result, self.assets)
        self.assertTrue(any(issue.path == "result.capacity_assignments" for issue in issues))

    def test_every_successful_result_has_one_capacity_assignment_per_step(self):
        for case in self.fixtures["cases"]:
            with self.subTest(case=case["id"]):
                resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
                result = engine.build_result(resolution, self.assets.policy, None, self.assets.execution_plan_schema)
                self.assertEqual(len(result["capacity_assignments"]), len(result["execution_plan"]["steps"]))
                self.assertEqual(
                    [a["step_id"] for a in result["capacity_assignments"]],
                    [s["id"] for s in result["execution_plan"]["steps"]],
                )

    def test_project_routing_cannot_override_capacity_model_or_worktree_policy(self):
        for key, value in (
            ("capacity_assignment", {}), ("capacity_tier", "economy"),
            ("model", "anything"), ("worktree_policy", {}),
            ("mutation_permission", "allowed"),
        ):
            with self.subTest(key=key):
                issues = engine.validate_project_routing_instance({key: value}, self.assets.project_schema)
                self.assertTrue(issues)

    def test_route_result_validation_rejects_policy_version_mismatch(self):
        case = self.fixtures["cases"][0]
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(
            resolution, self.assets.policy, None, self.assets.execution_plan_schema
        )
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        result["policy_version"] = 1
        issues = engine.validate_route_task_result(result, self.assets)
        self.assertTrue(any(issue.path == "result.policy_version" for issue in issues))

    def test_route_result_validation_rejects_telemetry_fields(self):
        case = self.fixtures["cases"][0]
        resolution = engine.resolve_route(case["task"], self.assets.policy, None, self.assets.task_schema)
        result = engine.build_result(
            resolution, self.assets.policy, None, self.assets.execution_plan_schema
        )
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        result["routing_telemetry"] = {"duration_ms": 1}
        issues = engine.validate_route_task_result(result, self.assets)
        self.assertTrue(any(issue.path == "result" and "telemetry" in issue.message for issue in issues))

    def test_invalid_execution_plan_rejected_ids_not_unique(self):
        plan = {
            "schema_version": 3,
            "independent_review_required": False,
            "steps": [
                {
                    "id": "dup",
                    "order": 1,
                    "role": "worker",
                    "family": "codex",
                    "intent": "analysis",
                    "capacity_tier": "standard",
                    "reasoning_effort": "high",
                    "context_strategy": "full",
                    "fresh_context": True,
                    "mutation_permission": "forbidden",
                    "depends_on": [],
                    "independent_review": False,
                },
                {
                    "id": "dup",
                    "order": 2,
                    "role": "reviewer",
                    "family": "codex",
                    "intent": "review",
                    "capacity_tier": "standard",
                    "reasoning_effort": "high",
                    "context_strategy": "full",
                    "fresh_context": True,
                    "mutation_permission": "forbidden",
                    "depends_on": ["dup"],
                    "independent_review": True,
                },
            ],
        }
        issues = engine.validate_execution_plan(plan, self.assets.execution_plan_schema, label="test")
        self.assertTrue(issues)

    def test_invalid_execution_plan_rejected_dependency_not_earlier(self):
        plan = {
            "schema_version": 3,
            "independent_review_required": False,
            "steps": [
                {
                    "id": "a",
                    "order": 1,
                    "role": "worker",
                    "family": "codex",
                    "intent": "analysis",
                    "capacity_tier": "standard",
                    "reasoning_effort": "high",
                    "context_strategy": "full",
                    "fresh_context": True,
                    "mutation_permission": "forbidden",
                    "depends_on": ["b"],
                    "independent_review": False,
                },
                {
                    "id": "b",
                    "order": 2,
                    "role": "reviewer",
                    "family": "codex",
                    "intent": "review",
                    "capacity_tier": "standard",
                    "reasoning_effort": "high",
                    "context_strategy": "full",
                    "fresh_context": True,
                    "mutation_permission": "forbidden",
                    "depends_on": [],
                    "independent_review": False,
                },
            ],
        }
        issues = engine.validate_execution_plan(plan, self.assets.execution_plan_schema, label="test")
        self.assertTrue(issues)

    def test_invalid_execution_plan_rejected_independent_review_flag_mismatch(self):
        plan = {
            "schema_version": 3,
            "independent_review_required": False,
            "steps": [
                {
                    "id": "a",
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
                    "id": "b",
                    "order": 2,
                    "role": "reviewer",
                    "family": "codex",
                    "intent": "review",
                    "capacity_tier": "standard",
                    "reasoning_effort": "high",
                    "context_strategy": "full",
                    "fresh_context": True,
                    "mutation_permission": "forbidden",
                    "depends_on": ["a"],
                    "independent_review": True,
                },
            ],
        }
        issues = engine.validate_execution_plan(plan, self.assets.execution_plan_schema, label="test")
        self.assertTrue(any("independent_review_required" in str(i) for i in issues))

    def test_valid_hand_built_execution_plan_passes(self):
        plan = {
            "schema_version": 3,
            "independent_review_required": True,
            "steps": [
                {
                    "id": "a",
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
                    "id": "b",
                    "order": 2,
                    "role": "reviewer",
                    "family": "codex",
                    "intent": "review",
                    "capacity_tier": "standard",
                    "reasoning_effort": "high",
                    "context_strategy": "full",
                    "fresh_context": True,
                    "mutation_permission": "forbidden",
                    "depends_on": ["a"],
                    "independent_review": True,
                },
            ],
        }
        issues = engine.validate_execution_plan(plan, self.assets.execution_plan_schema, label="test")
        self.assertEqual(issues, [])


class GraduatedCapacityTest(unittest.TestCase):
    """Policy v7: five capacity tiers and ordered, graduated capacity rules."""

    @classmethod
    def setUpClass(cls):
        cls.assets = load_assets()

    def _result(self, task, project=None):
        resolution = engine.resolve_route(task, self.assets.policy, project, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, project, self.assets.execution_plan_schema)
        self.assertTrue(result["ok"], result)
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])
        return result

    def _by_step(self, result):
        return {a["step_id"]: a for a in result["capacity_assignments"]}

    def _issues(self, policy):
        issues: list = []
        engine.validate_capacity_assignment(policy, self.assets.field_info, issues)
        return issues

    def test_policy_rules_are_ordered_and_match_engine_contract(self):
        rules = self.assets.policy["capacity_assignment"]["rules"]
        self.assertEqual([r["name"] for r in rules], list(engine.REQUIRED_CAPACITY_RULES))
        self.assertEqual({r["name"]: r["direction"] for r in rules}, engine.REQUIRED_CAPACITY_RULES)
        self.assertEqual(rules[1]["match"], engine.LOW_RISK_MATCH)
        self.assertEqual(self._issues(self.assets.policy), [])

    def test_git_push_routes_to_light_tier_with_low_effort(self):
        result = self._result(GIT_PUSH_TASK)
        self.assertEqual(result["selected_route"], "CLAUDE")
        assignment = self._by_step(result)["claude_worker"]
        self.assertEqual(assignment["rule"], "ops-light")
        self.assertEqual(assignment["assigned_capacity_tier"], "light")
        self.assertEqual(assignment["assigned_reasoning_effort"], "low")
        self.assertEqual(assignment["selected_model_profile"]["model"], "haiku")
        self.assertEqual(assignment["blocking_predicates"], [])
        self.assertTrue(all(p["rule"] == "ops-light" for p in assignment["guard_predicates_passed"]))
        self.assertEqual(set(result["backend_bindings"]["claude"]["capacity_models"]), {"light"})

    def test_ops_light_applies_to_codex_workers_too(self):
        plan = engine.materialize_execution_plan(
            self.assets.policy, self.assets.execution_plan_schema, "CODEX", "implementation"
        )
        assignment = engine.assign_capacities(self.assets.policy, plan, GIT_PUSH_TASK)[0]
        self.assertEqual(assignment["rule"], "ops-light")
        self.assertEqual(assignment["assigned_capacity_tier"], "light")
        self.assertEqual(assignment["assigned_reasoning_effort"], "low")
        self.assertEqual(assignment["selected_model_profile"]["model"], "openai-codex/gpt-6-luna")

    def test_medium_ambiguity_skips_light_model_and_keeps_standard(self):
        task = dict(GIT_PUSH_TASK, ambiguity=5)
        assignment = self._by_step(self._result(task))["claude_worker"]
        self.assertIsNone(assignment["rule"])
        self.assertEqual(assignment["assigned_capacity_tier"], "standard")
        self.assertEqual(assignment["selected_model_profile"]["ambiguity_capacity"], 6)
        self.assertIn("model ambiguity capacity 3 is below task ambiguity 5", assignment["decision_reason"])

    def test_numeric_ambiguity_boundaries_and_capacity_ceiling(self):
        for score, expected_tier in ((2, "light"), (3, "light"), (4, "standard"), (6, "standard")):
            with self.subTest(score=score):
                task = dict(GIT_PUSH_TASK, ambiguity=score)
                assignment = self._by_step(self._result(task))["claude_worker"]
                self.assertEqual(assignment["assigned_capacity_tier"], expected_tier)
                self.assertGreaterEqual(assignment["selected_model_profile"]["ambiguity_capacity"], score)

        for score in (7, 8, 9):
            with self.subTest(score=score):
                result = self._result(dict(CRITICAL_AMBIGUOUS_TASK, ambiguity=score))
                self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
                assignments = self._by_step(result)
                for assignment in assignments.values():
                    self.assertGreaterEqual(assignment["selected_model_profile"]["ambiguity_capacity"], score)

        with self.assertRaisesRegex(engine.RoutingAssetError, "supports 9 ambiguity, but task requires 10"):
            self._result(dict(CRITICAL_AMBIGUOUS_TASK, ambiguity=10))

    def test_ambiguity_assessment_records_each_dimension_and_derives_label(self):
        assessment = {
            axis: {"level": 2, "unresolved_choices": [], "repository_evidence": ["Relevant tests settle this choice."]}
            for axis in engine.AMBIGUITY_DIMENSIONS
        }
        assessment["contract"] = {
            "level": 5,
            "unresolved_choices": ["Choose the response shape for an optional field."],
            "repository_evidence": ["Existing endpoint has no optional-field example."],
        }
        task = dict(GIT_PUSH_TASK, ambiguity=5, ambiguity_assessment=assessment)
        self.assertEqual(engine.validate_task_instance(task, self.assets.task_schema), [])
        self.assertEqual(self._by_step(self._result(task))["claude_worker"]["assigned_capacity_tier"], "standard")

        understated = dict(task, ambiguity=2)
        self.assertTrue(any("highest ambiguity_assessment dimension" in issue.message
                            for issue in engine.validate_task_instance(understated, self.assets.task_schema)))

        missing_choice = copy.deepcopy(task)
        missing_choice["ambiguity_assessment"]["contract"]["unresolved_choices"] = []
        self.assertTrue(engine.validate_task_instance(missing_choice, self.assets.task_schema))

        owner_choice = copy.deepcopy(task)
        owner_choice["ambiguity"] = 8
        owner_choice["ambiguity_assessment"]["authority"] = {
            "level": 8,
            "unresolved_choices": ["Choose the externally visible default."],
            "repository_evidence": ["No approved default is recorded in the API contract."],
        }
        self.assertTrue(any("AUTHORITY_DECISION_REQUIRED" in issue.message
                            for issue in engine.validate_task_instance(owner_choice, self.assets.task_schema)))
        read_only = dict(owner_choice, implementation_required=False)
        self.assertEqual(engine.validate_task_instance(read_only, self.assets.task_schema), [])

        floored = dict(task, ambiguity=8)
        self.assertEqual(engine.validate_task_instance(floored, self.assets.task_schema,
                                                       allow_ambiguity_floor=True), [])
        project = {"risk_overrides": {"ambiguity": 8}}
        resolution = engine.resolve_route(task, self.assets.policy, project, self.assets.task_schema)
        result = engine.build_result(resolution, self.assets.policy, project,
                                     self.assets.execution_plan_schema)
        self.assertEqual(result["classification"]["effective"]["ambiguity"], 8)
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])

    def test_work_profile_records_mode_demands_without_overriding_route(self):
        profile = {
            "search": 7, "analysis": 4, "writing": 2,
            "implementation": 0, "verification": 2, "coordination": 1,
        }
        task = dict(GIT_PUSH_TASK, work_profile=profile)
        self.assertEqual(engine.validate_task_instance(task, self.assets.task_schema), [])
        self.assertEqual(self._result(task)["selected_route"], self._result(GIT_PUSH_TASK)["selected_route"])

        invalid = dict(task, work_profile=dict(profile, search=11))
        self.assertTrue(engine.validate_task_instance(invalid, self.assets.task_schema))
        missing = dict(task, work_profile={"search": 7})
        self.assertTrue(engine.validate_task_instance(missing, self.assets.task_schema))

    def test_ops_light_falls_through_to_next_rule_when_blocked(self):
        task = dict(GIT_PUSH_TASK, task_type="ui_ux")
        assignment = self._by_step(self._result(task))["claude_worker"]
        self.assertEqual(assignment["rule"], "low-risk-worker")
        self.assertEqual(assignment["assigned_capacity_tier"], "economy")
        self.assertEqual(
            [(p["rule"], p["field"]) for p in assignment["blocking_predicates"]], [("ops-light", "task_type")]
        )
        self.assertIn("earlier candidates: ops-light blocked", assignment["decision_reason"])

    def test_security_sensitive_or_destructive_work_is_never_lowered(self):
        for field in ("security_sensitive", "destructive_operation"):
            for route, intent in (("CLAUDE", "implementation"), ("CODEX", "implementation"),
                                  ("CODEX_CLAUDE_CODEX", "implementation")):
                with self.subTest(field=field, route=route):
                    plan = engine.materialize_execution_plan(
                        self.assets.policy, self.assets.execution_plan_schema, route, intent
                    )
                    task = dict(GIT_PUSH_TASK, **{field: True})
                    for step, assignment in zip(plan["steps"], engine.assign_capacities(self.assets.policy, plan, task), strict=True):
                        self.assertEqual(assignment["assigned_capacity_tier"], step["capacity_tier"])
                        self.assertIsNone(assignment["rule"])

    def test_review_intent_worker_is_never_lowered_even_for_ops_work(self):
        plan = engine.materialize_execution_plan(
            self.assets.policy, self.assets.execution_plan_schema, "CLAUDE", "review"
        )
        assignment = engine.assign_capacities(self.assets.policy, plan, GIT_PUSH_TASK)[0]
        self.assertEqual(assignment["assigned_capacity_tier"], "standard")
        self.assertIsNone(assignment["rule"])

    def test_bounded_codex_claude_codex_worker_runs_on_sonnet_while_reviewer_stays_on_sol(self):
        result = self._result(BOUNDED_CRITICAL_FEATURE_TASK)
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        steps = self._by_step(result)
        self.assertEqual(steps["claude_worker"]["rule"], "bounded-implementation-worker")
        self.assertEqual(steps["claude_worker"]["assigned_capacity_tier"], "standard")
        self.assertEqual(steps["claude_worker"]["assigned_reasoning_effort"], "medium")
        self.assertEqual(steps["claude_worker"]["selected_model_profile"]["model"], "sonnet")
        # medium scope keeps the specifier on its authored high tier.
        self.assertEqual(steps["codex_specifier"]["assigned_capacity_tier"], "high")
        self.assertEqual(steps["codex_reviewer"]["assigned_capacity_tier"], "high")
        self.assertIsNone(steps["codex_reviewer"]["rule"])
        self.assertEqual(steps["codex_reviewer"]["selected_model_profile"]["model"], "openai-codex/gpt-6.1-sol")

    def test_bounded_specifier_requires_small_scope_and_low_ambiguity(self):
        plan = engine.materialize_execution_plan(
            self.assets.policy, self.assets.execution_plan_schema, "CODEX_CLAUDE_CODEX", "implementation"
        )
        small = dict(BOUNDED_CRITICAL_FEATURE_TASK, implementation_scope="small", ambiguity=2)
        specifier = engine.assign_capacities(self.assets.policy, plan, small)[0]
        self.assertEqual(specifier["rule"], "bounded-specifier")
        self.assertEqual(specifier["assigned_capacity_tier"], "standard")
        self.assertEqual(specifier["selected_model_profile"]["model"], "openai-codex/gpt-6-luna")
        for override in ({"ambiguity": 5}, {"implementation_scope": "medium"}, {"failure_cost": "high"}):
            with self.subTest(override=override):
                specifier = engine.assign_capacities(self.assets.policy, plan, dict(small, **override))[0]
                self.assertEqual(specifier["assigned_capacity_tier"], "high")
                self.assertIsNone(specifier["rule"])

    def test_critical_ambiguous_high_cost_escalates_specifier_and_reviewer_to_max(self):
        result = self._result(CRITICAL_AMBIGUOUS_TASK)
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        steps = self._by_step(result)
        for step_id in ("codex_specifier", "codex_reviewer"):
            self.assertEqual(steps[step_id]["rule"], "critical-escalation")
            self.assertEqual(steps[step_id]["assigned_capacity_tier"], "max")
            self.assertEqual(steps[step_id]["assigned_reasoning_effort"], "high")
            self.assertEqual(steps[step_id]["selected_model_profile"]["model"], "openai-codex/gpt-6-astra")
        self.assertEqual(steps["claude_worker"]["assigned_capacity_tier"], "high")
        self.assertIsNone(steps["claude_worker"]["rule"])
        self.assertEqual(set(result["backend_bindings"]["codex"]["capacity_models"]), {"max"})

    def test_escalation_requires_all_three_signals(self):
        plan = engine.materialize_execution_plan(
            self.assets.policy, self.assets.execution_plan_schema, "CODEX_CLAUDE_CODEX", "implementation"
        )
        for field, value in (("failure_cost", "medium"), ("semantic_correctness", "normal"), ("ambiguity", 5)):
            with self.subTest(field=field):
                task = dict(CRITICAL_AMBIGUOUS_TASK, **{field: value})
                for assignment in engine.assign_capacities(self.assets.policy, plan, task):
                    self.assertNotEqual(assignment["assigned_capacity_tier"], "max")

    def test_escalation_with_security_flag_still_escalates_but_never_lowers(self):
        task = dict(CRITICAL_AMBIGUOUS_TASK, security_sensitive=True,
                    escalation_evidence=dict(CRITICAL_AMBIGUOUS_TASK["escalation_evidence"],
                                             security_sensitive="Touches token handling."))
        steps = self._by_step(self._result(task))
        self.assertEqual(steps["codex_reviewer"]["assigned_capacity_tier"], "max")
        self.assertEqual(steps["claude_worker"]["assigned_capacity_tier"], "high")

    def test_forged_up_assignment_is_rejected(self):
        result = self._result(BOUNDED_CRITICAL_FEATURE_TASK)
        reviewer = self._by_step(result)["codex_reviewer"]
        reviewer["assigned_capacity_tier"] = "max"
        reviewer["rule"] = "critical-escalation"
        reviewer["selected_model_profile"] = dict(
            reviewer["selected_model_profile"], capacity_tier="max", model="openai-codex/gpt-6-astra"
        )
        result["backend_bindings"] = engine.resolve_backend_bindings(
            self.assets.policy, result["execution_plan"], result["capacity_assignments"]
        )
        issues = engine.validate_route_task_result(result, self.assets)
        self.assertTrue(any(issue.path == "result.capacity_assignments" for issue in issues))

    def test_bindings_reject_raising_a_worker_or_lowering_a_reviewer(self):
        result = self._result(BOUNDED_CRITICAL_FEATURE_TASK)
        for step_id, tier in (("claude_worker", "max"), ("codex_reviewer", "standard")):
            with self.subTest(step_id=step_id):
                forged = copy.deepcopy(result["capacity_assignments"])
                next(a for a in forged if a["step_id"] == step_id)["assigned_capacity_tier"] = tier
                with self.assertRaises(engine.RoutingAssetError):
                    engine.resolve_backend_bindings(self.assets.policy, result["execution_plan"], forged)

    def test_validator_rejects_unsafe_capacity_rule_mutations(self):
        def rule(policy, name):
            return next(r for r in policy["capacity_assignment"]["rules"] if r["name"] == name)

        def mutate(fn):
            policy = copy.deepcopy(self.assets.policy)
            fn(policy)
            return policy

        def drop_leaf(field):
            def apply(policy):
                leaves = rule(policy, "bounded-implementation-worker")["match"]["all"]
                leaves[:] = [leaf for leaf in leaves if leaf["field"] != field]
            return apply

        mutations = {
            "down rule missing security leaf": drop_leaf("security_sensitive"),
            "down rule missing destructive leaf": drop_leaf("destructive_operation"),
            "down rule assigns its own authored tier": lambda p: rule(p, "low-risk-worker").update(
                assigned_capacity_tier="standard"),
            "down rule raises tier": lambda p: rule(p, "bounded-specifier").update(assigned_capacity_tier="max"),
            "down rule targets reviewer": lambda p: rule(p, "ops-light")["step_match"].update(role="reviewer"),
            "down rule targets independent review": lambda p: rule(p, "ops-light")["step_match"].update(
                independent_review=[False, True]),
            "down rule targets review intent": lambda p: rule(p, "ops-light")["step_match"].update(
                intent=["analysis", "review"]),
            "up rule with wrong leaves": lambda p: rule(p, "critical-escalation")["match"]["all"][2].update(
                value="medium"),
            "up rule with extra leaf": lambda p: rule(p, "critical-escalation")["match"]["all"].append(
                {"field": "cross_layer", "operator": "==", "value": True}),
            "up rule assigns high": lambda p: rule(p, "critical-escalation").update(assigned_capacity_tier="high"),
            "up rule targets worker": lambda p: rule(p, "critical-escalation")["step_match"].update(role="worker"),
            "up rule lowers effort": lambda p: rule(p, "critical-escalation").update(assigned_reasoning_effort="low"),
            "unknown assigned tier": lambda p: rule(p, "ops-light").update(assigned_capacity_tier="ultra"),
            "unknown step_match tier": lambda p: rule(p, "ops-light")["step_match"].update(capacity_tier="huge"),
            "unknown step_match family": lambda p: rule(p, "ops-light")["step_match"].update(family="gemini"),
            "wrong direction": lambda p: rule(p, "ops-light").update(direction="up"),
            "invalid direction": lambda p: rule(p, "ops-light").update(direction="sideways"),
            "unknown rule name": lambda p: rule(p, "ops-light").update(name="cheap-everything"),
            "nested match": lambda p: rule(p, "ops-light").update(match={"any": rule(p, "ops-light")["match"]["all"]}),
            "missing direction": lambda p: rule(p, "ops-light").pop("direction"),
            "tier missing from family profile": lambda p: p["model_profiles"]["codex"]["capacity_models"].pop("light"),
        }
        for label, fn in mutations.items():
            with self.subTest(label=label):
                policy = mutate(fn)
                issues = []
                engine.validate_policy_structure(policy, self.assets.field_info, self.assets.execution_plan_schema, issues)
                self.assertTrue(issues, label)

    def test_validator_accepts_family_scoped_rule(self):
        policy = copy.deepcopy(self.assets.policy)
        next(r for r in policy["capacity_assignment"]["rules"] if r["name"] == "bounded-specifier")["step_match"]["family"] = ["codex"]
        self.assertEqual(self._issues(policy), [])


class CLICasesTest(unittest.TestCase):
    """tools/route_task.py stdio adapter exercised as a real subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.assets = load_assets()
        cls.fixtures = load_fixtures()

    def test_regular_fixtures_through_cli(self):
        for case in self.fixtures["cases"]:
            with self.subTest(case=case["id"]):
                proc = run_cli(case["task"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertTrue(result["ok"])
                self.assertEqual(result["policy_version"], 7)
                # The fixtures' expected_route is the global policy alone; a task declaring a path under a built-in
                # path floor (.pi/, .claude/, .mcp.json, .github/workflows/, #76) is security-sensitive at runtime.
                builtin = engine.match_all_path_floors(case["task"]["files_or_areas"], None, declared=True)
                route = "CODEX_CLAUDE_CODEX" if builtin else case["expected_route"]
                self.assertEqual(result["selected_route"], route)
                self.assertEqual(set(result["matches"]["base"]), set(case["expected_matches"]))
                self.assertEqual(
                    result["execution_plan"],
                    expected_plan_for(self.assets, case["task"], route),
                )

    def test_override_fixtures_through_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            for case in self.fixtures["override_cases"]:
                with self.subTest(case=case["id"]):
                    project_path = Path(tmp) / f"{case['id']}.yaml"
                    project_path.write_text(yaml.safe_dump(case["project_routing"]), encoding="utf-8")
                    proc = run_cli(case["task"], project_path)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    result = json.loads(proc.stdout)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["selected_route"], case["expected_route_with_overrides"])
                    self.assertEqual(
                        result["execution_plan"],
                        expected_plan_for(self.assets, case["task"], case["expected_route_with_overrides"]),
                    )

    def test_fallback_not_downgraded_through_cli(self):
        case = next(c for c in self.fixtures["override_cases"] if c["id"] == "override-c-fallback-not-downgraded")
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text(yaml.safe_dump(case["project_routing"]), encoding="utf-8")
            proc = run_cli(case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["selected_route"], "CODEX")
            self.assertTrue(result["fallback_applied"])

    def test_unavailable_preferred_route_through_cli_returns_actual_selected_route(self):
        case = next(c for c in self.fixtures["cases"] if c["expected_route"] == "CODEX_CLAUDE_CODEX")
        proc = run_cli(case["task"], unavailable_routes=["CODEX_CLAUDE_CODEX"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["selected_route"], "CODEX")
        self.assertEqual(result["route_availability"]["excluded_routes"], ["CODEX_CLAUDE_CODEX"])
        self.assertEqual(result["execution_plan"]["steps"][0]["id"], "codex_worker")
        self.assertEqual(engine.validate_route_task_result(result, self.assets), [])

    def test_all_candidates_unavailable_through_cli_returns_no_available_route(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-5-broad-critical-semantics")
        proc = run_cli(case["task"], unavailable_routes=["CODEX_CLAUDE_CODEX", "CODEX"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocker_code"], "NO_AVAILABLE_ROUTE")
        self.assertNotIn("selected_route", result)
        self.assertNotIn("execution_plan", result)
        self.assertEqual(result["route_availability"]["excluded_routes"], ["CODEX", "CODEX_CLAUDE_CODEX"])

    def test_unknown_unavailable_route_through_cli_is_operational_failure(self):
        proc = run_cli(self.fixtures["cases"][0]["task"], unavailable_routes=["GPT_ONLY"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("unavailable_routes", proc.stderr)

    def test_invalid_task_through_cli_returns_ok_false_no_route(self):
        proc = run_cli({"id": "incomplete"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["ok"])
        self.assertIn("errors", result)
        self.assertTrue(len(result["errors"]) > 0)
        self.assertNotIn("selected_route", result)

    def test_escalating_values_without_evidence_are_rejected_through_cli(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-5-broad-critical-semantics")
        task = copy.deepcopy(case["task"])
        del task["escalation_evidence"]
        task.update({"security_sensitive": True, "destructive_operation": True, "ambiguity": 8})
        proc = run_cli(task)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocker_code"], "CLASSIFICATION_EVIDENCE_MISSING")
        self.assertEqual(result["missing_evidence_fields"], list(engine.ESCALATING_CLASSIFICATION_VALUES))
        self.assertEqual(
            [e["path"] for e in result["errors"]],
            [f"classified_task.escalation_evidence.{f}" for f in engine.ESCALATING_CLASSIFICATION_VALUES],
        )
        self.assertNotIn("selected_route", result)

    def test_partial_or_blank_evidence_lists_only_unjustified_fields(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-1-diagnosis-required")
        task = copy.deepcopy(case["task"])
        task["escalation_evidence"] = {"failure_cost": task["escalation_evidence"]["failure_cost"],
                                       "behavior_preservation": "   "}
        proc = run_cli(task)
        result = json.loads(proc.stdout)
        self.assertFalse(result["ok"])
        # Whitespace-only evidence is a schema error, not a silent pass.
        self.assertTrue(any("escalation_evidence" in (e["path"] or "") for e in result["errors"]))
        del task["escalation_evidence"]["behavior_preservation"]
        result = json.loads(run_cli(task).stdout)
        self.assertEqual(result["blocker_code"], "CLASSIFICATION_EVIDENCE_MISSING")
        self.assertEqual(result["missing_evidence_fields"], ["behavior_preservation", "assumptions_must_be_explicit"])

    def test_non_escalating_evidence_entries_are_allowed_and_unknown_keys_rejected(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        task = copy.deepcopy(case["task"])
        task["escalation_evidence"] = {"cross_layer": "Only presentation code changes."}
        self.assertTrue(json.loads(run_cli(task).stdout)["ok"])
        task["escalation_evidence"] = {"repo_is_scientific": "AGENTS.md says so."}
        result = json.loads(run_cli(task).stdout)
        self.assertFalse(result["ok"])
        self.assertNotIn("blocker_code", result)

    def test_values_raised_only_by_project_risk_floors_need_no_evidence(self):
        case = next(c for c in self.fixtures["cases"] if c["id"] == "case-4-ui-polish")
        floors = {"risk_overrides": {
            "failure_cost": "high", "semantic_correctness": "critical", "ambiguity": 8,
            "behavior_preservation": "required", "assumptions_must_be_explicit": True,
            "security_sensitive": True, "destructive_operation": True,
        }}
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text(yaml.safe_dump(floors), encoding="utf-8")
            proc = run_cli(case["task"], project_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["classification"]["effective"]["security_sensitive"], True)
        self.assertEqual(result["selected_route"], "CODEX_CLAUDE_CODEX")
        self.assertEqual(engine.missing_escalation_evidence(case["task"]), [])
        self.assertEqual(len(engine.missing_escalation_evidence(result["classification"]["effective"])), 7)

    def test_operational_task_types_route_through_cli(self):
        expected = {
            "case-15-vcs-operation": "CLAUDE",
            "case-16-issue-management": "CLAUDE",
            # Edits .pi/autonomy.yaml: the built-in .pi/** path floor makes it security-sensitive (#76).
            "case-17-config-edit": "CODEX_CLAUDE_CODEX",
            "case-18-ci-fix-known-cause": "CLAUDE",
            "case-19-ci-fix-unknown-cause": "CODEX",
        }
        cases = {c["id"]: c for c in self.fixtures["cases"]}
        for case_id, route in expected.items():
            with self.subTest(case=case_id):
                result = json.loads(run_cli(cases[case_id]["task"]).stdout)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["selected_route"], route)
                self.assertFalse(result["fallback_applied"])
                self.assertEqual(bool(result["path_floor_matches"]), case_id == "case-17-config-edit")
                self.assertEqual(engine.validate_route_task_result(result, self.assets), [])

    def test_invalid_project_config_through_cli_returns_ok_false_no_route(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            for case in self.fixtures["invalid_project_routing_examples"]:
                with self.subTest(case=case["id"]):
                    project_path = Path(tmp) / f"{case['id']}.yaml"
                    project_path.write_text(yaml.safe_dump(case["config"]), encoding="utf-8")
                    proc = run_cli(base_case["task"], project_path)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    result = json.loads(proc.stdout)
                    self.assertFalse(result["ok"])
                    self.assertNotIn("selected_route", result)

    def test_non_object_project_yaml_returns_ok_false_not_operational_failure(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text("- not\n- an\n- object\n", encoding="utf-8")
            proc = run_cli(base_case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertNotIn("selected_route", result)

    def test_non_string_project_yaml_keys_return_ok_false_not_crash(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text("terminology:\n  1: numeric\n", encoding="utf-8")
            proc = run_cli(base_case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertNotIn("selected_route", result)

    def test_colliding_project_yaml_keys_return_ok_false_not_crash(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text("terminology:\n  1: numeric\n  '1': string\n", encoding="utf-8")
            proc = run_cli(base_case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertNotIn("selected_route", result)

    def test_composite_project_yaml_key_returns_ok_false_not_crash(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text("terminology:\n  ? [a, b]\n  : x\n", encoding="utf-8")
            proc = run_cli(base_case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertNotIn("selected_route", result)

    def test_heterogeneous_mapping_project_yaml_key_returns_ok_false_not_crash(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text("terminology:\n  ? {a: 1, 2: x}\n  : y\n", encoding="utf-8")
            proc = run_cli(base_case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertNotIn("selected_route", result)

    def test_distinct_sequence_project_yaml_keys_return_ok_false_not_duplicate_crash(self):
        base_case = self.fixtures["cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text("terminology:\n  ? [a]\n  : x\n  ? [b]\n  : y\n", encoding="utf-8")
            proc = run_cli(base_case["task"], project_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertFalse(result["ok"])
            self.assertNotIn("selected_route", result)
            self.assertNotIn("duplicate key", proc.stderr)

    def test_missing_project_file_is_valid_not_an_error(self):
        base_case = self.fixtures["cases"][0]
        proc = run_cli(base_case["task"], Path("/nonexistent/dir/routing.yaml"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ok"])

    def test_existing_unreadable_project_file_is_operational_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A directory where a file is expected cannot be read as YAML.
            unreadable = Path(tmp) / "routing.yaml"
            unreadable.mkdir()
            base_case = self.fixtures["cases"][0]
            proc = run_cli(base_case["task"], unreadable)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

    def test_malformed_stdin_is_operational_failure(self):
        proc = subprocess.run(
            [sys.executable, str(ROUTE_TASK_SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_stdin_missing_classified_task_key_is_operational_failure(self):
        proc = subprocess.run(
            [sys.executable, str(ROUTE_TASK_SCRIPT)],
            input=json.dumps({}),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_corrupt_policy_execution_plan_fails_bootstrap_no_route_no_output(self):
        """A structurally/semantically invalid execution plan in
        routing/default-policy.yaml must fail bootstrap (nonzero exit, no
        stdout), not silently produce a route."""
        valid_task_json = json.dumps({"classified_task": self.fixtures["cases"][0]["task"]})
        with tempfile.TemporaryDirectory() as tmp:
            copied_root = Path(tmp) / "repo"
            shutil.copytree(
                REPO_ROOT,
                copied_root,
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
            )
            policy_path = copied_root / "routing" / "default-policy.yaml"
            text = policy_path.read_text(encoding="utf-8")
            # Break the CODEX 'implementation' worker step's mutation_permission
            # so it no longer satisfies "worker allows mutation iff intent is
            # implementation".
            broken = text.replace(
                "          mutation_permission: allowed\n",
                "          mutation_permission: forbidden\n",
                1,
            )
            self.assertNotEqual(broken, text, "test fixture did not locate the text to corrupt")
            policy_path.write_text(broken, encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(copied_root / "tools" / "route_task.py")],
                input=valid_task_json,
                capture_output=True,
                text=True,
                cwd=copied_root,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn("bootstrap failure", proc.stderr)

    def test_stable_ordering_and_repeatability(self):
        case = self.fixtures["cases"][0]
        outputs = {run_cli(case["task"]).stdout for _ in range(3)}
        self.assertEqual(len(outputs), 1, "identical input must produce byte-identical output every time")

    def test_exact_execution_plan_equality_for_every_case(self):
        for case in self.fixtures["cases"]:
            with self.subTest(case=case["id"]):
                proc = run_cli(case["task"])
                result = json.loads(proc.stdout)
                self.assertEqual(
                    result["execution_plan"],
                    expected_plan_for(self.assets, case["task"], result["selected_route"]),
                )

    def test_result_includes_base_and_effective_classification(self):
        case = self.fixtures["override_cases"][0]
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "routing.yaml"
            project_path.write_text(yaml.safe_dump(case["project_routing"]), encoding="utf-8")
            proc = run_cli(case["task"], project_path)
            result = json.loads(proc.stdout)
            self.assertEqual(result["classification"]["base"], case["task"])
            self.assertNotEqual(result["classification"]["base"], result["classification"]["effective"])
            self.assertEqual(result["project_constraints"], case["project_routing"])


if __name__ == "__main__":
    unittest.main()
