#!/usr/bin/env python3
"""Evaluate bounded task-classifier predictions against routing consequences.

The benchmark intentionally does not implement routing semantics. It imports
the repository's shared routing_engine and evaluates every gold/predicted task
through the same schema, policy, precedence, execution-plan, and capacity
assignment used by production routing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import routing_engine as engine  # noqa: E402

DEFAULT_CASES = HERE / "cases.yaml"
FIXED_TASK_FIELDS = (
    "id",
    "goal",
    "dependencies",
    "acceptance_criteria",
    "files_or_areas",
)
# Optional, unscored classification key: a field -> justification map that
# route_task requires for every escalating value (see
# routing_engine.ESCALATING_CLASSIFICATION_VALUES).
EVIDENCE_KEY = "escalation_evidence"
REQUIRED_CASE_KEYS = frozenset({"id", "request", "repository_evidence", "task_facts", "gold_classification"})
# `project_routing` is a case's `.pi/routing.yaml` (in practice its
# `path_floors`): production routing applies it and the built-in path floors,
# so `expected_decision` and the `*_with_path_floors` metrics route through
# both; the classic metrics use neither.
OPTIONAL_CASE_KEYS = frozenset({"notes", "expected_decision", "project_routing"})


class BenchmarkError(Exception):
    """Invalid benchmark fixture or prediction input."""


def classifier_fields(task_schema: dict) -> tuple[str, ...]:
    required = task_schema.get("required")
    if not isinstance(required, list):
        raise BenchmarkError("task schema required must be a list")
    fixed = set(FIXED_TASK_FIELDS)
    missing_fixed = [field for field in FIXED_TASK_FIELDS if field not in required]
    if missing_fixed:
        raise BenchmarkError(f"task schema is missing fixed task fields: {missing_fixed}")
    return tuple(field for field in required if field not in fixed)


def load_cases(path: Path) -> list[dict[str, Any]]:
    try:
        payload = engine.load_yaml(path)
    except Exception as exc:
        raise BenchmarkError(f"could not load cases from {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise BenchmarkError("cases file must be a mapping with version: 1")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkError("cases file must contain a non-empty cases list")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or any(not isinstance(case_id, str) or not case_id for case_id in ids):
        raise BenchmarkError("every case must have a non-empty string id")
    if len(set(ids)) != len(ids):
        raise BenchmarkError("case ids must be unique")
    return cases


def load_predictions(path: Path) -> dict[str, dict[str, Any]]:
    predictions: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise BenchmarkError(
                        f"{path}:{line_number}: invalid JSON: {exc.msg}"
                    ) from exc
                if not isinstance(record, dict):
                    raise BenchmarkError(f"{path}:{line_number}: prediction must be an object")
                case_id = record.get("case_id")
                classification = record.get("classification")
                if not isinstance(case_id, str) or not case_id:
                    raise BenchmarkError(f"{path}:{line_number}: case_id must be a non-empty string")
                if not isinstance(classification, dict):
                    raise BenchmarkError(f"{path}:{line_number}: classification must be an object")
                if case_id in predictions:
                    raise BenchmarkError(f"{path}:{line_number}: duplicate prediction for {case_id}")
                predictions[case_id] = record
    except OSError as exc:
        raise BenchmarkError(f"could not read predictions from {path}: {exc}") from exc
    return predictions


def validate_case(
    case: dict[str, Any],
    fields: tuple[str, ...],
    assets: engine.PolicyAssets,
) -> dict[str, Any]:
    keys = set(case.keys())
    if not REQUIRED_CASE_KEYS <= keys or not keys <= REQUIRED_CASE_KEYS | OPTIONAL_CASE_KEYS:
        raise BenchmarkError(
            f"{case.get('id', '<unknown>')}: case keys must be {sorted(REQUIRED_CASE_KEYS)} "
            f"plus optional {sorted(OPTIONAL_CASE_KEYS)}"
        )
    if "notes" in case and (not isinstance(case["notes"], str) or not case["notes"].strip()):
        raise BenchmarkError(f"{case['id']}: notes must be non-empty text when present")
    if not isinstance(case["request"], str) or not case["request"].strip():
        raise BenchmarkError(f"{case['id']}: request must be non-empty text")
    if not isinstance(case["repository_evidence"], list) or not all(
        isinstance(item, str) and item.strip() for item in case["repository_evidence"]
    ):
        raise BenchmarkError(f"{case['id']}: repository_evidence must be a list of non-empty strings")

    task_facts = case["task_facts"]
    gold = case["gold_classification"]
    if not isinstance(task_facts, dict) or set(task_facts.keys()) != set(FIXED_TASK_FIELDS):
        raise BenchmarkError(
            f"{case['id']}: task_facts keys must be exactly {sorted(FIXED_TASK_FIELDS)}"
        )
    if task_facts.get("id") != case["id"]:
        raise BenchmarkError(f"{case['id']}: task_facts.id must equal case id")
    validate_classification(case["id"], gold, fields)

    if "project_routing" in case:
        project_issues = engine.validate_project_routing_instance(case["project_routing"], assets.project_schema)
        if project_issues:
            raise BenchmarkError(
                f"{case['id']}: invalid project_routing: {'; '.join(str(i) for i in project_issues)}"
            )
    task = materialize_task(task_facts, gold)
    issues = engine.validate_instance(task, assets.task_schema, label=f"case[{case['id']}].gold")
    if issues:
        raise BenchmarkError(f"{case['id']}: invalid gold task: {'; '.join(str(i) for i in issues)}")
    missing = engine.missing_escalation_evidence(task)
    if missing:
        raise BenchmarkError(
            f"{case['id']}: gold uses escalating values without escalation_evidence "
            f"({engine.CLASSIFICATION_EVIDENCE_MISSING}): {missing}"
        )
    if "expected_decision" in case:
        expected = case["expected_decision"]
        actual = decision_signature(task, assets, case.get("project_routing"))
        actual_view = {
            "route": actual["route"],
            "primary_intent": actual["primary_intent"],
            "capacities": dict(actual["capacities"]),
        }
        if expected != actual_view:
            raise BenchmarkError(
                f"{case['id']}: expected_decision {expected!r} does not match the routing engine's "
                f"decision for the gold classification {actual_view!r}"
            )
    return task


def validate_classification(
    case_id: str,
    classification: Any,
    fields: tuple[str, ...],
) -> None:
    if not isinstance(classification, dict):
        raise BenchmarkError(f"{case_id}: classification must be an object")
    expected = set(fields)
    actual = set(classification.keys()) - {EVIDENCE_KEY}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BenchmarkError(
            f"{case_id}: classification keys mismatch; missing={missing}, extra={extra}"
        )


def materialize_task(task_facts: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    return {**task_facts, **classification}


def decision_signature(
    task: dict[str, Any],
    assets: engine.PolicyAssets,
    project_routing: dict[str, Any] | None = None,
    *,
    path_floors: bool = True,
) -> dict[str, Any]:
    """The production decision for `task`. `path_floors=False` leaves out the
    built-in and project path floors (the classic, no-path-floor metrics)."""
    resolution = engine.resolve_route(task, assets.policy, project_routing, assets.task_schema,
                                      path_floors=path_floors)
    route = resolution.selected_route
    effective = resolution.effective_classification
    intent = engine.derive_primary_intent(effective)
    plan = engine.materialize_execution_plan(
        assets.policy,
        assets.execution_plan_schema,
        route,
        intent,
    )
    assignments = engine.assign_capacities(assets.policy, plan, effective)
    capacities = tuple(
        (entry["step_id"], entry["assigned_capacity_tier"])
        for entry in assignments
    )
    return {
        "route": route,
        "primary_intent": intent,
        "capacities": capacities,
    }


def evaluate(
    cases: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
    assets: engine.PolicyAssets,
    *,
    require_all: bool = False,
) -> dict[str, Any]:
    fields = classifier_fields(assets.task_schema)
    case_ids = {case["id"] for case in cases}
    unknown_predictions = sorted(set(predictions) - case_ids)
    if unknown_predictions:
        raise BenchmarkError(f"predictions contain unknown case ids: {unknown_predictions}")

    field_correct = defaultdict(int)
    escalated_predictions = defaultdict(int)
    escalated_gold = defaultdict(int)
    scored = 0
    exact_correct = 0
    route_correct = 0
    decision_correct = 0
    under_routed = 0
    over_routed = 0
    # The same comparison with each case's project_routing (path floors)
    # applied to both the candidate and the gold classification.
    floored_route_correct = 0
    floored_under_routed = 0
    floored_over_routed = 0
    path_floor_cases = 0
    invalid_predictions: list[dict[str, str]] = []
    missing_predictions: list[str] = []
    mismatches: list[dict[str, Any]] = []

    precedence = assets.policy["precedence"]
    rank = {route: index for index, route in enumerate(precedence)}

    for case in cases:
        case_id = case["id"]
        gold_task = validate_case(case, fields, assets)
        gold_classification = case["gold_classification"]
        project_routing = case.get("project_routing")
        gold_decision = decision_signature(gold_task, assets, project_routing, path_floors=False)
        gold_floored = decision_signature(gold_task, assets, project_routing)

        if case_id not in predictions:
            missing_predictions.append(case_id)
            continue

        classification = predictions[case_id]["classification"]
        try:
            validate_classification(case_id, classification, fields)
            candidate_task = materialize_task(case["task_facts"], classification)
            issues = engine.validate_instance(
                candidate_task,
                assets.task_schema,
                label=f"prediction[{case_id}]",
            )
            if issues:
                raise BenchmarkError("; ".join(str(issue) for issue in issues))
            missing = engine.missing_escalation_evidence(candidate_task)
            if missing:
                # Mirrors route_task: production rejects such a classification.
                raise BenchmarkError(f"{engine.CLASSIFICATION_EVIDENCE_MISSING}: {missing}")
        except BenchmarkError as exc:
            invalid_predictions.append({"case_id": case_id, "error": str(exc)})
            continue

        scored += 1
        for field, value in engine.ESCALATING_CLASSIFICATION_VALUES.items():
            if engine.json_eq(classification[field], value):
                escalated_predictions[field] += 1
            if engine.json_eq(gold_classification[field], value):
                escalated_gold[field] += 1
        wrong_fields = []
        for field in fields:
            if classification[field] == gold_classification[field]:
                field_correct[field] += 1
            else:
                wrong_fields.append(field)

        if not wrong_fields:
            exact_correct += 1

        candidate_decision = decision_signature(candidate_task, assets, project_routing, path_floors=False)
        if candidate_decision["route"] == gold_decision["route"]:
            route_correct += 1
        else:
            candidate_rank = rank[candidate_decision["route"]]
            gold_rank = rank[gold_decision["route"]]
            if candidate_rank > gold_rank:
                under_routed += 1
            elif candidate_rank < gold_rank:
                over_routed += 1

        if candidate_decision == gold_decision:
            decision_correct += 1

        if project_routing is not None:
            path_floor_cases += 1
        candidate_floored = decision_signature(candidate_task, assets, project_routing)
        floored_rank = rank[candidate_floored["route"]] - rank[gold_floored["route"]]
        if floored_rank == 0:
            floored_route_correct += 1
        elif floored_rank > 0:
            floored_under_routed += 1
        else:
            floored_over_routed += 1

        if wrong_fields or candidate_decision != gold_decision:
            mismatches.append(
                {
                    "case_id": case_id,
                    "wrong_fields": wrong_fields,
                    "gold_route": gold_decision["route"],
                    "predicted_route": candidate_decision["route"],
                    "gold_primary_intent": gold_decision["primary_intent"],
                    "predicted_primary_intent": candidate_decision["primary_intent"],
                    "gold_capacities": [list(item) for item in gold_decision["capacities"]],
                    "predicted_capacities": [list(item) for item in candidate_decision["capacities"]],
                    "gold_route_with_path_floors": gold_floored["route"],
                    "predicted_route_with_path_floors": candidate_floored["route"],
                }
            )

    if require_all and (missing_predictions or invalid_predictions):
        problems = []
        if missing_predictions:
            problems.append(f"missing={missing_predictions}")
        if invalid_predictions:
            problems.append(f"invalid={[item['case_id'] for item in invalid_predictions]}")
        raise BenchmarkError("--require-all failed: " + ", ".join(problems))

    total = len(cases)
    field_accuracy = {
        field: (field_correct[field] / scored if scored else None)
        for field in fields
    }
    valid_field_values = [value for value in field_accuracy.values() if value is not None]
    macro_field_accuracy = (
        sum(valid_field_values) / len(valid_field_values)
        if valid_field_values
        else None
    )

    return {
        "cases_total": total,
        "predictions_supplied": len(predictions),
        "cases_scored": scored,
        "coverage": scored / total if total else 0.0,
        "exact_classification_accuracy": exact_correct / scored if scored else None,
        "macro_field_accuracy": macro_field_accuracy,
        "field_accuracy": field_accuracy,
        "route_accuracy": route_correct / scored if scored else None,
        "policy_under_routing_rate": under_routed / scored if scored else None,
        "policy_over_routing_rate": over_routed / scored if scored else None,
        "execution_decision_accuracy": decision_correct / scored if scored else None,
        # The same routes with each case's project_routing (path floors)
        # applied to candidate and gold: what production routing selects.
        "path_floor_cases": path_floor_cases,
        "route_accuracy_with_path_floors": floored_route_correct / scored if scored else None,
        "policy_under_routing_rate_with_path_floors": floored_under_routed / scored if scored else None,
        "policy_over_routing_rate_with_path_floors": floored_over_routed / scored if scored else None,
        # Fraction of scored cases whose classification uses each escalating
        # value (prediction vs gold); a calibrated classifier keeps these low.
        "escalation_rates": {
            field: {
                "predicted": escalated_predictions[field] / scored if scored else None,
                "gold": escalated_gold[field] / scored if scored else None,
            }
            for field in engine.ESCALATING_CLASSIFICATION_VALUES
        },
        "missing_predictions": missing_predictions,
        "invalid_predictions": invalid_predictions,
        "mismatches": mismatches,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def print_human(result: dict[str, Any]) -> None:
    print(f"cases: {result['cases_scored']}/{result['cases_total']} scored")
    print(f"coverage: {_fmt(result['coverage'])}")
    print(f"exact classification accuracy: {_fmt(result['exact_classification_accuracy'])}")
    print(f"macro field accuracy: {_fmt(result['macro_field_accuracy'])}")
    print(f"route accuracy: {_fmt(result['route_accuracy'])}")
    print(f"policy under-routing rate: {_fmt(result['policy_under_routing_rate'])}")
    print(f"policy over-routing rate: {_fmt(result['policy_over_routing_rate'])}")
    print(f"scored cases with path floors: {result['path_floor_cases']}")
    print(f"route accuracy with path floors: {_fmt(result['route_accuracy_with_path_floors'])}")
    print(f"policy under-routing rate with path floors: {_fmt(result['policy_under_routing_rate_with_path_floors'])}")
    print(f"policy over-routing rate with path floors: {_fmt(result['policy_over_routing_rate_with_path_floors'])}")
    print(f"execution-decision accuracy: {_fmt(result['execution_decision_accuracy'])}")
    print("field accuracy:")
    for field, value in result["field_accuracy"].items():
        print(f"  {field}: {_fmt(value)}")
    print("escalation rates (predicted / gold):")
    for field, rates in result["escalation_rates"].items():
        print(f"  {field}: {_fmt(rates['predicted'])} / {_fmt(rates['gold'])}")
    if result["missing_predictions"]:
        print(f"missing predictions: {', '.join(result['missing_predictions'])}")
    if result["invalid_predictions"]:
        print("invalid predictions:")
        for item in result["invalid_predictions"]:
            print(f"  {item['case_id']}: {item['error']}")
    if result["mismatches"]:
        print("mismatches:")
        for item in result["mismatches"]:
            fields = ",".join(item["wrong_fields"]) or "<route/capacity only>"
            print(
                f"  {item['case_id']}: fields={fields}; "
                f"route={item['predicted_route']} (gold={item['gold_route']}); "
                f"with path floors route={item['predicted_route_with_path_floors']} "
                f"(gold={item['gold_route_with_path_floors']})"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--require-all", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        assets = engine.load_policy_assets(REPO_ROOT)
        cases = load_cases(args.cases)
        predictions = load_predictions(args.predictions)
        result = evaluate(cases, predictions, assets, require_all=args.require_all)
    except (BenchmarkError, engine.RoutingAssetError) as exc:
        print(f"classifier benchmark error: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
