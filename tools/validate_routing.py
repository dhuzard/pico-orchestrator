#!/usr/bin/env python3
"""Deterministic, read-only validator and fixture runner for the routing policy.

Validates:
  - schemas/task.schema.json, schemas/project-routing.schema.json, and
    schemas/delegation-policy.schema.json are well-formed JSON Schemas.
  - routing/delegation-policy.yaml is schema-valid and cross-consistent with
    routing/default-policy.yaml's model capacity tiers.
  - routing/default-policy.yaml has a structurally valid match-expression
    AST (fields exist in the task schema, operators/values are compatible,
    routes/precedence/execution/fallback are exactly the four known routes).
  - templates/project-routing.yaml validates against
    schemas/project-routing.schema.json.
  - prompts/orchestrate.md mirrors the task schema's required field list,
    the four route names, and the fallback route, and contains no
    environment-specific absolute paths.
  - tests/routing-cases.yaml fixtures: every task is schema-valid, every
    route match/precedence/fallback outcome is reproduced deterministically,
    every override case resolves per the documented floor + union +
    precedence algorithm, and every "invalid" project-routing example is
    correctly rejected by schemas/project-routing.schema.json.

All evaluator, YAML-safety, schema-validation, override, and route-selection
primitives are imported from tools/routing_engine.py -- the single shared
implementation also used by tools/route_task.py at runtime. This module adds
only the static repo checks (prompt/template/README consistency) and fixture
orchestration on top of that.

No network access, no model invocation, no eval, no arbitrary YAML
constructors. Exits 0 with a stable OK summary on success; otherwise prints
every failure found and exits 1.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML. Run: python3 -m pip install -r requirements-dev.txt")
    sys.exit(1)

try:
    import jsonschema
except ImportError:
    print("Missing dependency: jsonschema. Run: python3 -m pip install -r requirements-dev.txt")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_engine as engine  # noqa: E402
import context_budget  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

ROUTES = engine.ROUTES


class Errors:
    def __init__(self):
        self.items = []

    def add(self, message):
        self.items.append(message)

    def extend_issues(self, issues):
        for issue in issues:
            self.items.append(str(issue))

    def ok(self):
        return not self.items


load_yaml = engine.load_yaml
load_json = engine.load_json
build_field_info = engine.build_field_info
validate_policy_structure = engine.validate_policy_structure
validate_project_routing_schema = engine.validate_project_routing_schema
route_task = engine.route_task
apply_risk_overrides = engine.apply_risk_overrides


# ---------------------------------------------------------------------------
# prompts/orchestrate.md <-> schema/policy consistency
# ---------------------------------------------------------------------------

ABS_PATH_RE = re.compile(r"(?<![:\w~])/(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]*")


def validate_escalation_evidence_schema(task_schema, errors):
    """Every escalating value enforced by route_task must be a real enum/boolean
    value of its field, and justifiable through escalation_evidence."""
    props = task_schema.get("properties", {})
    evidence = props.get("escalation_evidence")
    if not isinstance(evidence, dict):
        errors.add("schemas/task.schema.json must define optional properties.escalation_evidence")
        return
    if "escalation_evidence" in task_schema.get("required", []):
        errors.add("schemas/task.schema.json escalation_evidence must stay optional")
    allowed = set((evidence.get("propertyNames") or {}).get("enum") or [])
    for field, value in engine.ESCALATING_CLASSIFICATION_VALUES.items():
        prop = props.get(field)
        if not isinstance(prop, dict):
            errors.add(f"escalating field {field!r} is missing from schemas/task.schema.json")
            continue
        enum = prop.get("enum")
        if enum is not None and value not in enum:
            errors.add(f"escalating value {field}={value!r} is not in schemas/task.schema.json enum {enum!r}")
        if enum is None and prop.get("type") == "boolean" and not isinstance(value, bool):
            errors.add(f"escalating value {field}={value!r} must be a boolean")
        if field not in allowed:
            errors.add(f"schemas/task.schema.json escalation_evidence must accept key {field!r}")
        if not prop.get("description"):
            errors.add(f"schemas/task.schema.json properties.{field} must carry a rubric description")


def validate_prompt(prompt_text, task_schema, policy, errors):
    for match in ABS_PATH_RE.finditer(prompt_text):
        errors.add(f"prompts/orchestrate.md contains an absolute filesystem path: {match.group(0)!r}")

    m = re.search(r"## Classification\n(.*?)\n## ", prompt_text, re.S)
    if not m:
        errors.add("prompts/orchestrate.md: could not locate '## Classification' field list")
    else:
        block = re.search(r"((?:^- \S+\n?)+)", m.group(1), re.M)
        fields = re.findall(r"^- (\S+)$", block.group(1), re.M) if block else []
        expected = task_schema["required"]
        if fields != expected:
            errors.add(
                "prompts/orchestrate.md classification list must exactly equal "
                f"schemas/task.schema.json required list.\n  prompt:  {fields}\n  schema:  {expected}"
            )

    headings = set(re.findall(r"^### ([A-Z_]+)$", prompt_text, re.M))
    if headings != ROUTES:
        errors.add(f"prompts/orchestrate.md route headings must be exactly {sorted(ROUTES)}, got {sorted(headings)}")

    if f"(CODEX)" not in prompt_text or "fallback_route" not in prompt_text:
        errors.add("prompts/orchestrate.md must document fallback_route (CODEX)")

    for literal in ("escalation_evidence", engine.CLASSIFICATION_EVIDENCE_MISSING):
        if literal not in prompt_text:
            errors.add(f"prompts/orchestrate.md classification rubric must mention {literal!r}")

    if "route_task" not in prompt_text:
        errors.add("prompts/orchestrate.md must instruct the orchestrator to call the route_task tool")
    if "routing/delegation-policy.yaml" not in prompt_text:
        errors.add("prompts/orchestrate.md must document routing/delegation-policy.yaml")
    for mode in engine.DELEGATION_MODES:
        if mode not in prompt_text:
            errors.add(f"prompts/orchestrate.md must mention delegation mode {mode!r}")

    for key in engine.CAPACITY_ASSIGNMENT_KEYS:
        if key not in prompt_text:
            errors.add(
                f"prompts/orchestrate.md must document capacity_assignments field {key!r} "
                "(see tools/routing_engine.py CAPACITY_ASSIGNMENT_KEYS)"
            )
    if "`economy` is never authored" not in prompt_text or "`light` is never authored" not in prompt_text:
        errors.add(
            "prompts/orchestrate.md must state that authored capacity_tier is never 'light' or 'economy' "
            "(both are assigned only by post-route capacity_assignment rules)"
        )

    for profile in policy.get("model_profiles", {}).values():
        for model_entry in profile.get("capacity_models", {}).values():
            model = model_entry.get("model")
            if isinstance(model, str) and model in prompt_text:
                errors.add(
                    f"prompts/orchestrate.md must not hard-code policy model identifier {model!r}"
                )

    review_block_match = re.search(
        r"For any `review`-intent step.*?(?=\n## |\nCapacity, reasoning)",
        prompt_text,
        re.S,
    )
    review_block = review_block_match.group(0) if review_block_match else ""
    prompt_categories = set(re.findall(r"^\s*-\s+([A-Z_]+)\s*$", review_block, re.M))
    policy_categories = set()
    for bucket in ("blocking", "repair_required", "advisory"):
        policy_categories.update(policy.get("review_findings", {}).get(bucket, []))
    if prompt_categories != policy_categories:
        errors.add(
            "prompts/orchestrate.md review finding categories must match "
            f"routing/default-policy.yaml review_findings categories.\n"
            f"  prompt: {sorted(prompt_categories)}\n  policy: {sorted(policy_categories)}"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_LIST_KEYS = ("cases", "override_cases", "invalid_project_routing_examples")


def validate_fixtures_structure(fixtures, errors):
    if not isinstance(fixtures, dict):
        errors.add("tests/routing-cases.yaml must be a mapping")
        return False
    ok = True
    for key in FIXTURE_LIST_KEYS:
        value = fixtures.get(key)
        if not isinstance(value, list) or len(value) == 0:
            errors.add(f"tests/routing-cases.yaml top-level key {key!r} must be a non-empty list")
            ok = False
    return ok


def validate_fixtures(fixtures, task_schema, project_schema, policy, errors):
    if not validate_fixtures_structure(fixtures, errors):
        return

    task_validator = jsonschema.Draft202012Validator(task_schema)
    project_validator = jsonschema.Draft202012Validator(project_schema)

    for case in fixtures.get("cases", []):
        cid = case.get("id", "<missing id>")
        task = case["task"]
        schema_errors = sorted(task_validator.iter_errors(task), key=str)
        if schema_errors:
            errors.add(f"tests/routing-cases.yaml case {cid}: task fails schema validation: "
                       f"{[e.message for e in schema_errors]}")
            continue
        matched, selected = route_task(task, policy)
        expected_matches = set(case.get("expected_matches", []))
        expected_route = case.get("expected_route")
        if matched != expected_matches:
            errors.add(
                f"tests/routing-cases.yaml case {cid}: expected_matches {sorted(expected_matches)} "
                f"but evaluator computed {sorted(matched)}"
            )
        if selected != expected_route:
            errors.add(
                f"tests/routing-cases.yaml case {cid}: expected_route {expected_route!r} "
                f"but evaluator computed {selected!r}"
            )

    for case in fixtures.get("override_cases", []):
        cid = case.get("id", "<missing id>")
        task = case["task"]
        schema_errors = sorted(task_validator.iter_errors(task), key=str)
        if schema_errors:
            errors.add(f"tests/routing-cases.yaml override_case {cid}: task fails schema validation: "
                       f"{[e.message for e in schema_errors]}")
            continue

        project_routing = case.get("project_routing", {})
        proj_errors = sorted(project_validator.iter_errors(project_routing), key=str)
        if proj_errors:
            errors.add(
                f"tests/routing-cases.yaml override_case {cid}: project_routing fails schema validation: "
                f"{[e.message for e in proj_errors]}"
            )
            continue

        resolution = engine.resolve_route(task, policy, project_routing, task_schema)

        if resolution.base_matches != set(case.get("expected_matches", [])):
            errors.add(
                f"tests/routing-cases.yaml override_case {cid}: base expected_matches mismatch: "
                f"expected {sorted(case.get('expected_matches', []))}, got {sorted(resolution.base_matches)}"
            )
        if resolution.base_route != case.get("expected_route"):
            errors.add(
                f"tests/routing-cases.yaml override_case {cid}: base expected_route mismatch: "
                f"expected {case.get('expected_route')!r}, got {resolution.base_route!r}"
            )

        union_matched = resolution.base_matches | resolution.effective_matches
        expected_union = set(case.get("expected_matches_with_overrides", []))
        expected_route_override = case.get("expected_route_with_overrides")
        if union_matched != expected_union:
            errors.add(
                f"tests/routing-cases.yaml override_case {cid}: expected_matches_with_overrides mismatch: "
                f"expected {sorted(expected_union)}, got {sorted(union_matched)}"
            )
        if resolution.selected_route != expected_route_override:
            errors.add(
                f"tests/routing-cases.yaml override_case {cid}: expected_route_with_overrides mismatch: "
                f"expected {expected_route_override!r}, got {resolution.selected_route!r}"
            )

        base_precedence_rank = policy["precedence"].index(resolution.base_route)
        override_precedence_rank = policy["precedence"].index(resolution.selected_route)
        if override_precedence_rank > base_precedence_rank:
            errors.add(
                f"tests/routing-cases.yaml override_case {cid}: overrides downgraded routing "
                f"from {resolution.base_route!r} to {resolution.selected_route!r}"
            )

    for case in fixtures.get("invalid_project_routing_examples", []):
        cid = case.get("id", "<missing id>")
        config = case["config"]
        proj_errors = list(project_validator.iter_errors(config))
        if not proj_errors:
            errors.add(
                f"tests/routing-cases.yaml invalid_project_routing_examples {cid}: "
                f"expected schema rejection ({case.get('reason')}) but config validated successfully"
            )


def validate_run_record_review_categories(run_schema_path, policy, errors):
    if not run_schema_path.exists():
        return
    try:
        run_schema = load_json(run_schema_path)
    except Exception as exc:  # noqa: BLE001
        errors.add(f"schemas/run-record.schema.json could not be read: {exc}")
        return
    try:
        enum = set(run_schema["$defs"]["step"]["properties"]["review_findings"]["items"]["properties"]["category"]["enum"])
    except KeyError as exc:
        errors.add(f"schemas/run-record.schema.json missing review finding enum path: {exc}")
        return
    policy_categories = set()
    for bucket in ("blocking", "repair_required", "advisory"):
        policy_categories.update(policy.get("review_findings", {}).get(bucket, []))
    if enum != policy_categories:
        errors.add(
            "schemas/run-record.schema.json review finding categories must match "
            f"routing/default-policy.yaml review_findings categories. schema={sorted(enum)} policy={sorted(policy_categories)}"
        )

    step_properties = run_schema.get("$defs", {}).get("step", {}).get("properties", {})
    expected_execution_enums = {
        "capacity_tier": engine.CAPACITY_TIERS,
        "reasoning_setting": engine.REASONING_EFFORTS,
        "context_strategy": engine.CONTEXT_STRATEGIES,
        "context_window_tier": engine.CONTEXT_WINDOW_TIERS,
    }
    for field, expected in expected_execution_enums.items():
        actual = set(step_properties.get(field, {}).get("enum", [])) - {None}
        if actual != expected:
            errors.add(
                f"schemas/run-record.schema.json {field} enum must match policy vocabulary: "
                f"schema={sorted(actual)} policy={sorted(expected)}"
            )


def validate_template(template_path, project_schema, errors):
    template = load_yaml(template_path)
    validator = jsonschema.Draft202012Validator(project_schema)
    template_errors = sorted(validator.iter_errors(template), key=str)
    if template_errors:
        errors.add(
            f"{template_path.relative_to(REPO_ROOT)} fails validation against "
            f"schemas/project-routing.schema.json: {[e.message for e in template_errors]}"
        )


def validate_readme(readme_text, errors):
    for route in ROUTES:
        if route not in readme_text:
            errors.add(f"README.md must mention route {route!r}")
    # Public URLs and the portable /usr/bin editor path in the launcher docs
    # are not environment-specific filesystem paths.
    without_urls = re.sub(r"https?://[^\s)]+", "", readme_text)
    if any(not match.group(0).startswith("/usr/bin") for match in ABS_PATH_RE.finditer(without_urls)):
        errors.add("README.md contains an environment-specific absolute path")

    for rule_name in engine.REQUIRED_CAPACITY_RULES:
        if rule_name not in readme_text:
            errors.add(f"README.md must document capacity assignment rule {rule_name!r}")
    for key in engine.CAPACITY_ASSIGNMENT_KEYS:
        if key not in readme_text:
            errors.add(
                f"README.md must document capacity_assignments field {key!r} "
                "(see tools/routing_engine.py CAPACITY_ASSIGNMENT_KEYS)"
            )
    for nested_key in ("rule", "field", "operator", "value", "actual", "family", "model", "context_window_tier"):
        if nested_key not in readme_text:
            errors.add(
                f"README.md must document nested capacity-assignment evidence/profile field {nested_key!r}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_backend_compatibility(policy, errors):
    """Keep static backend minimums tied to actual routed model IDs."""
    try:
        data = load_json(REPO_ROOT / "routing/backend-compatibility.json")
    except Exception as exc:  # noqa: BLE001
        errors.add(f"routing/backend-compatibility.json is unreadable: {exc}")
        return
    if not isinstance(data, dict) or set(data) != {"schema_version", "claude_code_minimum_versions", "claude_code_model_ids"} or data.get("schema_version") != 1:
        errors.add("routing/backend-compatibility.json has invalid top-level fields")
        return
    minimums = data.get("claude_code_minimum_versions")
    if not isinstance(minimums, dict):
        errors.add("routing/backend-compatibility.json minimums must be a mapping")
        return
    known = {entry["model"] for entry in policy["model_profiles"]["claude"]["capacity_models"].values()}
    backend_ids = data.get("claude_code_model_ids")
    if not isinstance(backend_ids, dict) or set(backend_ids) != known:
        errors.add("Claude Code backend model IDs must cover exactly the configured Claude models")
    else:
        for model, backend_id in backend_ids.items():
            if not isinstance(backend_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", backend_id):
                errors.add(f"invalid Claude Code backend model ID for {model!r}")
    for model, version in minimums.items():
        if model not in known or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            errors.add(f"invalid backend minimum for model {model!r}")


def main():
    errors = Errors()

    task_schema_path = REPO_ROOT / "schemas" / "task.schema.json"
    project_schema_path = REPO_ROOT / "schemas" / "project-routing.schema.json"
    execution_plan_schema_path = REPO_ROOT / "schemas" / "execution-plan.schema.json"
    delegation_schema_path = REPO_ROOT / "schemas" / "delegation-policy.schema.json"
    policy_path = REPO_ROOT / "routing" / "default-policy.yaml"
    delegation_policy_path = REPO_ROOT / "routing" / "delegation-policy.yaml"
    prompt_path = REPO_ROOT / "prompts" / "orchestrate.md"
    template_path = REPO_ROOT / "templates" / "project-routing.yaml"
    fixtures_path = REPO_ROOT / "tests" / "routing-cases.yaml"
    readme_path = REPO_ROOT / "README.md"
    run_schema_path = REPO_ROOT / "schemas" / "run-record.schema.json"

    for path in (
        task_schema_path,
        project_schema_path,
        execution_plan_schema_path,
        delegation_schema_path,
        policy_path,
        delegation_policy_path,
        prompt_path,
        template_path,
        fixtures_path,
        readme_path,
        run_schema_path,
    ):
        if not path.exists():
            errors.add(f"missing required file: {path.relative_to(REPO_ROOT)}")

    if not errors.ok():
        print_errors(errors)
        return 1

    try:
        task_schema = load_json(task_schema_path)
        jsonschema.Draft202012Validator.check_schema(task_schema)
    except Exception as exc:  # noqa: BLE001
        errors.add(f"schemas/task.schema.json is not a valid JSON Schema: {exc}")
        print_errors(errors)
        return 1

    validate_escalation_evidence_schema(task_schema, errors)

    try:
        project_schema = load_json(project_schema_path)
        jsonschema.Draft202012Validator.check_schema(project_schema)
    except Exception as exc:  # noqa: BLE001
        errors.add(f"schemas/project-routing.schema.json is not a valid JSON Schema: {exc}")
        print_errors(errors)
        return 1

    try:
        execution_plan_schema = engine.load_execution_plan_schema(REPO_ROOT)
    except Exception as exc:  # noqa: BLE001
        errors.add(f"schemas/execution-plan.schema.json is not a valid JSON Schema: {exc}")
        print_errors(errors)
        return 1

    try:
        delegation_schema = load_json(delegation_schema_path)
        jsonschema.Draft202012Validator.check_schema(delegation_schema)
    except Exception as exc:  # noqa: BLE001
        errors.add(f"schemas/delegation-policy.schema.json is not a valid JSON Schema: {exc}")
        print_errors(errors)
        return 1

    try:
        policy = load_yaml(policy_path)
    except yaml.YAMLError as exc:
        errors.add(f"routing/default-policy.yaml is not valid YAML: {exc}")
        print_errors(errors)
        return 1

    try:
        delegation_policy = load_yaml(delegation_policy_path)
    except yaml.YAMLError as exc:
        errors.add(f"routing/delegation-policy.yaml is not valid YAML: {exc}")
        print_errors(errors)
        return 1

    try:
        template_check = load_yaml(template_path)  # noqa: F841 (parse check only)
    except yaml.YAMLError as exc:
        errors.add(f"templates/project-routing.yaml is not valid YAML: {exc}")

    try:
        fixtures = load_yaml(fixtures_path)
    except yaml.YAMLError as exc:
        errors.add(f"tests/routing-cases.yaml is not valid YAML: {exc}")
        print_errors(errors)
        return 1

    field_info = build_field_info(task_schema)
    issues = []
    validate_policy_structure(policy, field_info, execution_plan_schema, issues)
    errors.extend_issues(issues)
    if not issues:
        validate_backend_compatibility(policy, errors)
    issues = []
    engine.validate_delegation_policy(delegation_policy, delegation_schema, policy, issues)
    errors.extend_issues(issues)
    issues = []
    validate_project_routing_schema(project_schema, task_schema, issues)
    errors.extend_issues(issues)

    if not errors.ok():
        # Structural problems make evaluation meaningless; report and stop.
        print_errors(errors)
        return 1

    prompt_text = prompt_path.read_text(encoding="utf-8")
    validate_prompt(prompt_text, task_schema, policy, errors)

    validate_template(template_path, project_schema, errors)

    readme_text = readme_path.read_text(encoding="utf-8")
    validate_readme(readme_text, errors)

    try:
        context_budget.load_policy()
    except context_budget.ContextBudgetError as exc:
        errors.add(f"routing/context-policy.yaml: {exc}")
    if "ops-light" not in readme_text or "capacity_assignments" not in readme_text:
        errors.add("README.md must document centralized post-route capacity assignment")
    if "dedicated worktree" not in readme_text.lower() or "pinned commit" not in readme_text.lower():
        errors.add("README.md must document the dedicated-worktree protocol from a pinned commit")
    if "routing/delegation-policy.yaml" not in readme_text:
        errors.add("README.md must document routing/delegation-policy.yaml")
    if "smallest useful fan-out" not in readme_text.lower():
        errors.add("README.md must document the smallest useful fan-out principle")

    validate_run_record_review_categories(run_schema_path, policy, errors)

    validate_fixtures(fixtures, task_schema, project_schema, policy, errors)

    if not errors.ok():
        print_errors(errors)
        return 1

    total_cases = len(fixtures.get("cases", []))
    total_overrides = len(fixtures.get("override_cases", []))
    total_invalid = len(fixtures.get("invalid_project_routing_examples", []))
    print(
        f"OK: routing/delegation policies, schemas, prompt, template and README are consistent "
        f"({total_cases} routing cases, {total_overrides} override cases, "
        f"{total_invalid} invalid-config examples all passed)."
    )
    return 0


def print_errors(errors: Errors):
    print(f"FAIL: {len(errors.items)} routing validation error(s):", file=sys.stderr)
    for item in errors.items:
        print(f"  - {item}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
