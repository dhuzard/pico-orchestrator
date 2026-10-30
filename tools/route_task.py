#!/usr/bin/env python3
"""Thin JSON-over-stdio adapter around tools/routing_engine.py.

Protocol:
  stdin:  a single JSON object: {"classified_task": {...}}, optionally with
          "unavailable_routes": [<route>, ...] observed by availability
          preflight. If every policy-valid candidate is excluded the result is
          {"ok": false, "blocker_code": "NO_AVAILABLE_ROUTE", ...}.
          A schema-valid classification that uses an escalating value
          (routing_engine.ESCALATING_CLASSIFICATION_VALUES) without a
          non-empty escalation_evidence entry for that field yields
          {"ok": false, "blocker_code": "CLASSIFICATION_EVIDENCE_MISSING",
           "missing_evidence_fields": [...], "errors": [...]}. Values raised
          only by project risk floors are exempt: the project-wide
          `risk_overrides` and every `path_floors` rule matched by the task's
          `files_or_areas` (recorded in the result's `path_floor_matches`).
  stdout: a single JSON object (the routing result), exactly once
  stderr: diagnostics only (never part of the protocol result)

Optional argument:
  --project-routing <absolute-config-path>
      Path to a project-local routing.yaml-style config, validated against
      schemas/project-routing.schema.json. A missing file is valid (it means
      "no project constraints"). An existing-but-unreadable/corrupt file is
      an operational failure.

Exit codes:
  0  the protocol completed, including a validation-failure result
     ({"ok": false, "errors": [...]}) for an invalid classified_task or
     project-routing config -- that is a normal, well-formed outcome.
  nonzero  operational/bootstrap failure: malformed stdin, corrupt or
     missing repository assets, an unreadable existing project-routing
     file, or an internal exception. Diagnostics go to stderr; nothing is
     written to stdout in this case.

This module contains no route predicates, precedence, fallback, or override
logic of its own -- all routing decisions are delegated to
tools/routing_engine.py, the single shared evaluator.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_engine as engine  # noqa: E402


def _fail_operational(message: str) -> NoReturn:
    print(f"route_task: {message}", file=sys.stderr)
    sys.exit(1)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="route_task.py",
        description="Deterministic routing decision over a classified task, via routing_engine.py.",
    )
    parser.add_argument(
        "--project-routing",
        dest="project_routing",
        default=None,
        help="Absolute path to a project-local routing config (schemas/project-routing.schema.json).",
    )
    return parser.parse_args(argv)


def _read_stdin_protocol() -> tuple[dict, list[str]]:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail_operational(f"malformed stdin: not valid JSON: {exc}")
    if not isinstance(payload, dict):
        _fail_operational(f"malformed stdin: top-level JSON value must be an object, got {type(payload).__name__}")
    if "classified_task" not in payload:
        _fail_operational("malformed stdin: missing required key 'classified_task'")
    classified_task = payload["classified_task"]
    if not isinstance(classified_task, dict):
        _fail_operational(
            f"malformed stdin: 'classified_task' must be a JSON object, got {type(classified_task).__name__}"
        )
    unavailable_routes = payload.get("unavailable_routes", [])
    if unavailable_routes is None:
        unavailable_routes = []
    if not isinstance(unavailable_routes, list) or not all(
        isinstance(item, str) and item in engine.ROUTES for item in unavailable_routes
    ):
        _fail_operational("malformed stdin: 'unavailable_routes' must be a list of known route names when provided")
    return classified_task, unavailable_routes


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    try:
        assets = engine.load_policy_assets(REPO_ROOT)
    except engine.RoutingAssetError as exc:
        _fail_operational(f"bootstrap failure: {exc}")
    except Exception as exc:  # noqa: BLE001
        _fail_operational(f"internal exception while loading routing assets: {exc}")

    project_routing = None
    if args.project_routing is not None:
        project_path = Path(args.project_routing)
        try:
            project_routing = engine.load_optional_yaml(project_path)
        except engine.RoutingAssetError as exc:
            _fail_operational(f"project-routing file error: {exc}")

    classified_task, unavailable_routes = _read_stdin_protocol()

    errors: list[engine.ValidationIssue] = []
    task_errors = engine.validate_task_instance(classified_task, assets.task_schema)
    errors.extend(task_errors)
    # Evidence is checked on the raw classification, before project risk
    # floors (risk_overrides and matched path_floors), so values raised only
    # by a floor never need justification.
    missing_evidence = [] if task_errors else engine.missing_escalation_evidence(classified_task)
    if missing_evidence:
        errors.extend(engine.classification_evidence_issues(classified_task))
    if project_routing is not None:
        errors.extend(engine.validate_project_routing_instance(project_routing, assets.project_schema))

    if errors:
        result = {"ok": False, "errors": [e.as_dict() for e in errors]}
        if missing_evidence:
            result["blocker_code"] = engine.CLASSIFICATION_EVIDENCE_MISSING
            result["missing_evidence_fields"] = missing_evidence
        print(json.dumps(result, sort_keys=True))
        return 0

    try:
        resolution = engine.resolve_route(
            classified_task,
            assets.policy,
            project_routing,
            assets.task_schema,
            unavailable_routes,
        )
        result = engine.build_result(resolution, assets.policy, project_routing, assets.execution_plan_schema)
    except engine.NoAvailableRouteError as exc:
        # Fail closed: every policy-valid candidate was excluded by availability
        # preflight. Never fall back to a route the task did not match.
        result = {
            "ok": False,
            "blocker_code": "NO_AVAILABLE_ROUTE",
            "errors": [{"path": "unavailable_routes", "message": str(exc)}],
            "route_availability": {
                "candidate_routes": exc.candidates,
                "excluded_routes": exc.unavailable_routes,
            },
        }
    except Exception as exc:  # noqa: BLE001
        _fail_operational(f"internal exception while resolving route: {exc}")

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
