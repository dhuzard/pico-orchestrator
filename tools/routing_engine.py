"""Shared deterministic routing evaluator.

This module is the single implementation of every routing primitive used by
both the static validator (`tools/validate_routing.py`) and the runtime
stdio adapter (`tools/route_task.py`). It is importable only: it has no CLI
behavior and performs no I/O beyond the explicit load/validate helpers below.

No network access, no model invocation, no `eval`, no arbitrary YAML
constructors.
"""

from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - surfaced by callers
    raise ImportError(
        "Missing dependency: jsonschema. Run: python3 -m pip install -r requirements-dev.txt"
    ) from exc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RoutingAssetError(Exception):
    """Raised when a repository asset (schema/policy/YAML) is structurally
    invalid, unreadable, or internally inconsistent. This is a bootstrap
    failure, distinct from a task or project-config validation failure."""


@dataclass(frozen=True)
class ValidationIssue:
    """A single normalized validation error."""

    message: str
    path: str | None = None

    def as_dict(self) -> dict:
        return {"path": self.path, "message": self.message}

    def __str__(self) -> str:  # keeps prior plain-string formatting intact
        return self.message


# ---------------------------------------------------------------------------
# Constants shared by every route consumer
# ---------------------------------------------------------------------------

ROUTES = frozenset({"CODEX", "CLAUDE", "CLAUDE_CODEX", "CODEX_CLAUDE_CODEX"})
MODEL_RUNTIME_ENV = "PI_ORCHESTRATOR_MODEL_CONFIG"

ORDERED_OVERRIDE_FIELDS = (
    "failure_cost",
    "semantic_correctness",
    "ambiguity",
    "implementation_scope",
    "behavior_preservation",
)

SAFETY_OVERRIDE_FIELDS = (
    "cross_layer",
    "diagnosis_required",
    "assumptions_must_be_explicit",
    "production_feature",
    "security_sensitive",
    "destructive_operation",
)

# Classification values that escalate routing/capacity and therefore must be
# justified by a task-specific `escalation_evidence[<field>]` entry in the raw
# classification. Values raised only by project risk floors are exempt: the
# check runs on the classification as submitted, before any floor is applied.
ESCALATING_CLASSIFICATION_VALUES: dict[str, Any] = {
    "failure_cost": "high",
    "semantic_correctness": "critical",
    "ambiguity": 7,
    "behavior_preservation": "required",
    "assumptions_must_be_explicit": True,
    "security_sensitive": True,
    "destructive_operation": True,
}
CLASSIFICATION_EVIDENCE_MISSING = "CLASSIFICATION_EVIDENCE_MISSING"
# Post-diff check (`check_diff_path_floors`): the worker's real changed paths
# hit `.pi/routing.yaml` `path_floors` that select a higher-precedence route,
# or a higher capacity/effort on the same route, than the one that ran.
CLASSIFICATION_UNDERRATED_BY_DIFF = "CLASSIFICATION_UNDERRATED_BY_DIFF"
# The post-diff check could not be evaluated (invalid route result, unreadable
# worktree): integration fails closed.
CLASSIFICATION_DIFF_UNVERIFIABLE = "CLASSIFICATION_DIFF_UNVERIFIABLE"
# Bound on the paths recorded per matched path floor (the count is exact).
PATH_FLOOR_MATCHED_PATHS_LIMIT = 50

FORBIDDEN_PROJECT_KEYS = frozenset({
    "task_type",
    "implementation_required",
    "many_routine_decisions",
    "routes",
    "precedence",
    "fallback",
    "fallback_route",
    "model",
    "models",
    "model_profiles",
    "backends",
    "execution",
    "capacity",
    "capacity_tier",
    "capacity_models",
    "capacity_assignment",
    "capacity_assignments",
    "mutation_permission",
    "mutation_permissions",
    "worktree",
    "worktrees",
    "worktree_policy",
    "review_findings",
    "completion",
})

REVIEW_CATEGORIES = frozenset({
    "BLOCKER",
    "CORRECTNESS",
    "REGRESSION",
    "TEST_GAP",
    "ARCHITECTURAL_DEBT",
    "STYLE_ONLY",
})

# ---------------------------------------------------------------------------
# Execution-plan vocabulary shared by policy authoring, materialization, and
# the extension boundary check. This is the single interpreter of plan
# semantics; no other module re-implements these rules.
# ---------------------------------------------------------------------------

EXECUTION_ROLES = frozenset({"specifier", "worker", "reviewer"})
EXECUTION_FAMILIES = frozenset({"codex", "claude"})
EXECUTION_INTENTS = frozenset({"analysis", "implementation", "review"})
MUTATION_PERMISSIONS = frozenset({"forbidden", "allowed"})
PRIMARY_INTENTS = frozenset({"analysis", "implementation", "review"})
MUTATION_MODE_KEYS = frozenset({"forbidden", "allowed"})
# Capacity tiers, ordered cheapest -> strongest. This tuple is the single
# source of truth; every schema enum, model-profile key set, `pico models`
# choice list, and documentation table mirrors it.
CAPACITY_TIER_ORDER = ("light", "economy", "standard", "high", "max")
CAPACITY_TIER_RANK = {tier: rank for rank, tier in enumerate(CAPACITY_TIER_ORDER)}
CAPACITY_TIERS = frozenset(CAPACITY_TIER_ORDER)
# Tiers an execution plan may author. `light` and `economy` are reachable only
# through post-route downward assignment; `max` is authorable but the default
# policy authors it nowhere -- it is reached only through the strict upward
# escalation rule.
AUTHORED_CAPACITY_TIERS = frozenset({"standard", "high", "max"})
MANDATORY_PROFILE_CAPACITY_TIERS = frozenset({"standard", "high"})
PROFILE_CAPACITY_TIERS = {
    "codex": CAPACITY_TIERS,
    "claude": CAPACITY_TIERS,
}
# The historical low-risk classification guard (policy v4/v5), still used
# verbatim by the `low-risk-worker` rule.
LOW_RISK_MATCH = {
    "all": [
        {"field": "failure_cost", "operator": "==", "value": "low"},
        {"field": "semantic_correctness", "operator": "!=", "value": "critical"},
        {"field": "ambiguity", "operator": "<=", "value": 3},
        {"field": "implementation_scope", "operator": "==", "value": "small"},
        {"field": "behavior_preservation", "operator": "!=", "value": "required"},
        {"field": "cross_layer", "operator": "==", "value": False},
        {"field": "diagnosis_required", "operator": "==", "value": False},
        {"field": "assumptions_must_be_explicit", "operator": "==", "value": False},
        {"field": "production_feature", "operator": "==", "value": False},
        {"field": "security_sensitive", "operator": "==", "value": False},
        {"field": "destructive_operation", "operator": "==", "value": False},
    ]
}
# Safety leaves every downward capacity rule's `match.all` must contain
# verbatim. Validated centrally, so no policy edit can lower capacity for
# security-sensitive or destructive work.
CAPACITY_SAFETY_LEAVES = (
    {"field": "security_sensitive", "operator": "==", "value": False},
    {"field": "destructive_operation", "operator": "==", "value": False},
)
# The exact leaf set an upward (escalation) rule's `match.all` must equal:
# raising capacity to `max` requires all three high-risk signals at once.
CAPACITY_ESCALATION_LEAVES = (
    {"field": "failure_cost", "operator": "==", "value": "high"},
    {"field": "semantic_correctness", "operator": "==", "value": "critical"},
    {"field": "ambiguity", "operator": ">=", "value": 7},
)
CAPACITY_RULE_DIRECTIONS = frozenset({"down", "up"})
# The exactly-required capacity rules (policy v7) and each rule's direction.
# Rules are evaluated in policy order per step; the first rule whose
# `step_match` and `match` both hold wins. Fail-closed: bootstrap validation
# rejects any rule set that omits, duplicates, renames, or redirects one.
REQUIRED_CAPACITY_RULES = {
    "ops-light": "down",
    "low-risk-worker": "down",
    "bounded-implementation-worker": "down",
    "bounded-specifier": "down",
    "critical-escalation": "up",
}
# The single source of truth for a capacity-assignment result entry's exact
# key set, shared by `_validated_assignment_tiers` and the static
# documentation-consistency check in `tools/validate_routing.py`, so
# `prompts/orchestrate.md`/README cannot silently drift from the real v7
# assignment shape again.
CAPACITY_ASSIGNMENT_KEYS = frozenset({
    "step_id", "authored_capacity_tier", "assigned_capacity_tier",
    "authored_reasoning_effort", "assigned_reasoning_effort", "rule",
    "decision_reason", "guard_predicates_passed", "blocking_predicates", "selected_model_profile",
})
REASONING_EFFORTS = frozenset({"low", "medium", "high"})
# Ordering used to guarantee that capacity assignment can only lower effort.
REASONING_EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}
CONTEXT_STRATEGIES = frozenset({"focused", "full"})
CONTEXT_WINDOW_TIERS = frozenset({"standard", "extended"})
AMBIGUITY_MIN = 0
AMBIGUITY_MAX = 10
TIER_AMBIGUITY_FLOORS = {
    "light": 2, "economy": 3, "standard": 6,
    "high": 8, "max": 8,
}
EXECUTION_METADATA_FIELDS = ("capacity_tier", "reasoning_effort", "context_strategy")

# Canonical, fixed route topology: step id / role / family / dependency-id
# shape that every primary-intent variant of a route's execution plan must
# share. Only the worker step's `intent`/`mutation_permission` may vary
# across `analysis`/`review`/`implementation` variants of the same route.
CANONICAL_ROUTE_TOPOLOGY: dict[str, tuple[dict[str, Any], ...]] = {
    "CODEX": (
        {"id": "codex_worker", "role": "worker", "family": "codex", "depends_on": []},
    ),
    "CLAUDE": (
        {"id": "claude_worker", "role": "worker", "family": "claude", "depends_on": []},
    ),
    "CLAUDE_CODEX": (
        {"id": "claude_worker", "role": "worker", "family": "claude", "depends_on": []},
        {"id": "codex_reviewer", "role": "reviewer", "family": "codex", "depends_on": ["claude_worker"]},
    ),
    "CODEX_CLAUDE_CODEX": (
        {"id": "codex_specifier", "role": "specifier", "family": "codex", "depends_on": []},
        {"id": "claude_worker", "role": "worker", "family": "claude", "depends_on": ["codex_specifier"]},
        {
            "id": "codex_reviewer",
            "role": "reviewer",
            "family": "codex",
            "depends_on": ["codex_specifier", "claude_worker"],
        },
    ),
}

# Routes whose canonical topology includes an independent-review reviewer
# step; every primary-intent variant of these routes must set
# independent_review_required: true, and the others must set it false.
ROUTES_WITH_INDEPENDENT_REVIEW = frozenset({"CLAUDE_CODEX", "CODEX_CLAUDE_CODEX"})

DELEGATION_MODES = frozenset({"DIRECT", "DELEGATE", "PARALLEL", "BLOCKED", "SEQUENTIAL"})
DELEGATION_BENCHMARK_TOPOLOGIES = frozenset({
    "direct",
    "one_worker",
    "two_workers",
    "three_to_four_workers",
    "mixed_model_workers",
    "same_model_workers",
})


# ---------------------------------------------------------------------------
# Safe YAML loading: SafeLoader base (no arbitrary Python object construction)
# plus rejection of duplicate mapping keys.
# ---------------------------------------------------------------------------

class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _make_hashable_yaml_key(key):
    """Convert YAML's possible composite keys into hashable Python values so
    they can reach structured JSON-compatibility validation instead of causing
    parser-level TypeError tracebacks. Scalar non-string keys are preserved."""
    if isinstance(key, list):
        return tuple(_make_hashable_yaml_key(item) for item in key)
    if isinstance(key, dict):
        return tuple((_make_hashable_yaml_key(k), _make_hashable_yaml_key(v)) for k, v in key.items())
    return key


def _construct_mapping_no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        raw_key = loader.construct_object(key_node, deep=True)
        key = _make_hashable_yaml_key(raw_key)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f, Loader=UniqueKeySafeLoader)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_optional_yaml(path: Path) -> Any | None:
    """Load optional project-local YAML. Returns None if `path` does not exist
    (a missing project-local config file is valid: it means "no project
    constraints"). Raises RoutingAssetError only if the file exists but cannot
    be read or parsed as YAML. Schema/type failures are returned later as
    structured validation errors, not operational failures."""
    if not path.exists():
        return None
    try:
        data = load_yaml(path)
    except OSError as exc:
        raise RoutingAssetError(f"{path}: could not be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RoutingAssetError(f"{path}: is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    return data


# Backward-compatible name for callers that conceptually load project routing.
def load_optional_yaml_mapping(path: Path) -> Any | None:
    return load_optional_yaml(path)


# ---------------------------------------------------------------------------
# User-owned concrete model/runtime configuration
# ---------------------------------------------------------------------------

def runtime_model_config_path() -> Path | None:
    """Return the explicit user-owned model config selected by the launcher.

    The path must be absolute so a target repository cannot alter its meaning
    by changing cwd. Target-local routing files remain forbidden from selecting
    models.
    """
    raw = os.environ.get(MODEL_RUNTIME_ENV)
    if raw is None or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RoutingAssetError(f"{MODEL_RUNTIME_ENV} must be an absolute path")
    return path


def load_model_runtime_config(repo_root: Path, path: Path | None = None) -> dict | None:
    """Load and schema-validate an optional concrete model/runtime overlay."""
    selected = runtime_model_config_path() if path is None else Path(path).expanduser()
    if selected is None:
        return None
    if not selected.is_absolute():
        raise RoutingAssetError("model runtime configuration path must be absolute")
    schema_path = repo_root / "schemas" / "model-runtime.schema.json"
    if not schema_path.exists():
        raise RoutingAssetError(f"missing required asset: {schema_path}")
    if not selected.exists():
        raise RoutingAssetError(f"configured model runtime file does not exist: {selected}")
    try:
        schema = load_json(schema_path)
        jsonschema.Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001
        raise RoutingAssetError(f"{schema_path} is not a valid JSON Schema: {exc}") from exc
    try:
        config = load_yaml(selected)
    except (OSError, yaml.YAMLError) as exc:
        raise RoutingAssetError(f"{selected} is not valid readable YAML: {exc}") from exc
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(config), key=str)
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise RoutingAssetError(f"{selected} is not a valid model runtime configuration: {detail}")
    return config


def apply_model_runtime_config(policy: dict, config: dict | None) -> dict:
    """Overlay user-declared model bindings without altering routing semantics."""
    effective = copy.deepcopy(policy)
    if not config:
        return effective
    for family, tiers in config.get("models", {}).items():
        for tier, binding in tiers.items():
            entry = effective["model_profiles"][family]["capacity_models"][tier]
            # Legacy strings matching the built-in binding retain its assessed
            # capacity; an unfamiliar model cannot inherit that assessment.
            if isinstance(binding, str):
                entry.update(model=binding, ambiguity_capacity=(
                    entry["ambiguity_capacity"] if binding == entry["model"] else 0
                ))
            else:
                entry.update(model=binding["model"], ambiguity_capacity=binding["ambiguity_capacity"])
    return effective


def load_backend_compatibility(
    repo_root: Path,
    *,
    model_config: dict | None = None,
    model_config_path: Path | None = None,
    effective_policy: dict | None = None,
) -> dict:
    """Load backend compatibility and apply user-owned Claude mappings.

    New Claude policy model identifiers must resolve to an explicit Claude Code
    model id. This is checked here before any backend invocation.
    """
    path = repo_root / "routing" / "backend-compatibility.json"
    try:
        rules = load_json(path)
    except (OSError, ValueError) as exc:
        raise RoutingAssetError(f"{path} is not valid JSON: {exc}") from exc
    if (
        not isinstance(rules, dict)
        or rules.get("schema_version") != 1
        or not isinstance(rules.get("claude_code_minimum_versions"), dict)
        or not isinstance(rules.get("claude_code_model_ids"), dict)
    ):
        raise RoutingAssetError(f"{path} has an invalid backend compatibility shape")

    config = model_config if model_config is not None else load_model_runtime_config(repo_root, model_config_path)
    effective = copy.deepcopy(rules)
    if config:
        claude_code = config.get("claude_code", {})
        model_id_overrides = claude_code.get("model_ids", {})
        version_overrides = claude_code.get("minimum_versions", {})

        # Built-in compatibility facts are safety floors, not user preferences.
        # A user config may add a mapping for a new policy model, but it may not
        # reinterpret an existing policy model as a different Claude Code model.
        for model, backend_id in model_id_overrides.items():
            built_in = rules["claude_code_model_ids"].get(model)
            if built_in is not None and backend_id != built_in:
                raise RoutingAssetError(
                    f"Claude model {model!r} has built-in backend id {built_in!r}; "
                    f"the runtime config may not replace it with {backend_id!r}"
                )

        # A configured minimum may raise a known compatibility floor, never
        # lower it. Lowering a repository-authored floor could make an
        # unsupported backend appear READY.
        def version_tuple(value: str) -> tuple[int, int, int]:
            if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
                raise RoutingAssetError("backend compatibility contains an invalid Claude minimum version")
            return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]

        for model, minimum in version_overrides.items():
            built_in = rules["claude_code_minimum_versions"].get(model)
            if built_in is not None and version_tuple(minimum) < version_tuple(built_in):
                raise RoutingAssetError(
                    f"Claude model {model!r} has built-in minimum version {built_in}; "
                    f"the runtime config may not lower it to {minimum}"
                )

        effective["claude_code_model_ids"].update(model_id_overrides)
        effective["claude_code_minimum_versions"].update(version_overrides)

    for model, backend_id in effective["claude_code_model_ids"].items():
        if (
            not isinstance(model, str)
            or not model
            or not isinstance(backend_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", backend_id)
        ):
            raise RoutingAssetError("backend compatibility contains an invalid Claude model mapping")
    for model, version in effective["claude_code_minimum_versions"].items():
        if not isinstance(model, str) or not model or not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise RoutingAssetError("backend compatibility contains an invalid Claude minimum version")

    if effective_policy is not None:
        claude_profile = effective_policy.get("model_profiles", {}).get("claude", {})
        capacity_models = claude_profile.get("capacity_models", {}) if isinstance(claude_profile, dict) else {}
        for tier, entry in capacity_models.items():
            model = entry.get("model") if isinstance(entry, dict) else None
            if model not in effective["claude_code_model_ids"]:
                raise RoutingAssetError(
                    f"Claude model {model!r} selected for capacity tier {tier!r} has no Claude Code model-id mapping"
                )
    return effective


# ---------------------------------------------------------------------------
# Task schema field metadata
# ---------------------------------------------------------------------------

def build_field_info(task_schema: dict) -> dict:
    info = {}
    for name, prop in task_schema.get("properties", {}).items():
        info[name] = {"type": prop.get("type"), "enum": prop.get("enum"),
                      "minimum": prop.get("minimum"), "maximum": prop.get("maximum")}
    return info


def value_compatible(field_info_entry, value, issues: list[ValidationIssue], context: str) -> None:
    enum = field_info_entry.get("enum")
    ftype = field_info_entry.get("type")
    if enum is not None:
        if value not in enum:
            issues.append(ValidationIssue(path=context, message=f"{context}: value {value!r} is not one of {enum!r}"))
        return
    if ftype == "boolean":
        if not isinstance(value, bool):
            issues.append(ValidationIssue(path=context, message=f"{context}: value {value!r} is not a boolean"))
        return
    if ftype == "string":
        if not isinstance(value, str):
            issues.append(ValidationIssue(path=context, message=f"{context}: value {value!r} is not a string"))
        return
    if ftype == "integer":
        if (isinstance(value, bool) or not isinstance(value, int)
                or (field_info_entry["minimum"] is not None and value < field_info_entry["minimum"])
                or (field_info_entry["maximum"] is not None and value > field_info_entry["maximum"])):
            issues.append(ValidationIssue(path=context, message=f"{context}: value {value!r} is outside the integer field range"))
        return
    issues.append(ValidationIssue(
        path=context,
        message=f"{context}: field type {ftype!r} is not supported in routing predicates",
    ))


# ---------------------------------------------------------------------------
# Policy match-expression AST validation
# ---------------------------------------------------------------------------

def validate_expr(node, field_info: dict, issues: list[ValidationIssue], context: str) -> None:
    if not isinstance(node, dict):
        issues.append(ValidationIssue(
            path=context,
            message=f"{context}: expression node must be a mapping, got {type(node).__name__}",
        ))
        return
    if any(not isinstance(k, str) for k in node.keys()):
        issues.append(ValidationIssue(
            path=context,
            message=f"{context}: expression node keys must be strings, got {_safe_sorted_repr(node.keys())}",
        ))
        return
    keys = set(node.keys())
    if keys == {"all"} or keys == {"any"}:
        group = "all" if "all" in keys else "any"
        children = node[group]
        if not isinstance(children, list) or len(children) == 0:
            issues.append(ValidationIssue(
                path=context, message=f"{context}: '{group}' group must be a non-empty list"
            ))
            return
        for i, child in enumerate(children):
            validate_expr(child, field_info, issues, f"{context}.{group}[{i}]")
        return
    if keys == {"field", "operator", "value"}:
        field = node["field"]
        operator = node["operator"]
        value = node["value"]
        if not isinstance(field, str):
            issues.append(ValidationIssue(
                path=context,
                message=f"{context}: field must be a string, got {field!r}",
            ))
            return
        if field not in field_info:
            issues.append(ValidationIssue(
                path=context,
                message=f"{context}: field {field!r} is not defined in schemas/task.schema.json",
            ))
            return
        if operator not in ("==", "!=", "IN", "<=", ">="):
            issues.append(ValidationIssue(
                path=context,
                message=f"{context}: operator must be one of '==', '!=', 'IN', '<=', '>=', got {operator!r}",
            ))
            return
        entry = field_info[field]
        if operator in ("<=", ">=") and entry["type"] != "integer":
            issues.append(ValidationIssue(path=context, message=f"{context}: {operator} requires an integer field"))
            return
        if operator == "IN":
            if not isinstance(value, list) or len(value) == 0:
                issues.append(ValidationIssue(
                    path=context, message=f"{context}: IN operator requires a non-empty array value"
                ))
                return
            for i, v in enumerate(value):
                value_compatible(entry, v, issues, f"{context}.value[{i}] (field={field})")
        else:
            value_compatible(entry, value, issues, f"{context}.value (field={field})")
        return
    issues.append(ValidationIssue(
        path=context,
        message=(
            f"{context}: expression node must be exactly one of "
            "{{all: [...]}}, {{any: [...]}}, or {{field, operator, value}}; got keys "
            f"{_safe_sorted_repr(keys)}"
        ),
    ))


# ---------------------------------------------------------------------------
# Execution-plan schema + semantic validation (the single plan interpreter).
# Consumed by policy bootstrap validation, runtime materialization, and
# tools/validate_routing.py -- never reimplemented elsewhere.
# ---------------------------------------------------------------------------

def load_execution_plan_schema(repo_root: Path) -> dict:
    path = repo_root / "schemas" / "execution-plan.schema.json"
    schema = load_json(path)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def validate_execution_plan_semantics(plan: dict, issues: list[ValidationIssue], context: str) -> None:
    """Semantic rules on top of schema-shape validity. Assumes `plan` already
    conforms to schemas/execution-plan.schema.json (caller's responsibility)."""
    steps = plan["steps"]

    ids = [step["id"] for step in steps]
    if len(set(ids)) != len(ids):
        issues.append(ValidationIssue(message=f"{context}: step ids must be unique, got {ids}"))

    for i, step in enumerate(steps):
        expected_order = i + 1
        if step["order"] != expected_order:
            issues.append(ValidationIssue(
                message=(
                    f"{context}.steps[{i}] (id={step['id']!r}): order must be {expected_order} "
                    f"(array position and 'order' must be contiguous 1..N), got {step['order']!r}"
                )
            ))

    id_to_order: dict[str, int] = {}
    for step in steps:
        id_to_order.setdefault(step["id"], step["order"])

    for i, step in enumerate(steps):
        sid = step["id"]
        order = step["order"]
        role = step["role"]
        intent = step["intent"]
        mutation = step["mutation_permission"]
        fresh_context = step["fresh_context"]
        independent_review = step["independent_review"]
        depends_on = step["depends_on"]

        for dep in depends_on:
            if dep not in id_to_order:
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): depends_on references unknown step id {dep!r}"
                ))
                continue
            if id_to_order[dep] >= order:
                issues.append(ValidationIssue(
                    message=(
                        f"{context}.steps[{i}] (id={sid!r}): depends_on {dep!r} must reference an "
                        "earlier step (strictly smaller order)"
                    )
                ))

        if fresh_context is not True:
            issues.append(ValidationIssue(
                message=f"{context}.steps[{i}] (id={sid!r}): fresh_context must be true"
            ))

        if role == "specifier":
            if intent != "analysis":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): role 'specifier' requires intent 'analysis', got {intent!r}"
                ))
            if mutation != "forbidden":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): role 'specifier' requires mutation_permission 'forbidden', got {mutation!r}"
                ))
        elif role == "reviewer":
            if intent != "review":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): role 'reviewer' requires intent 'review', got {intent!r}"
                ))
            if mutation != "forbidden":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): role 'reviewer' requires mutation_permission 'forbidden', got {mutation!r}"
                ))
        elif role == "worker":
            expected_mutation = "allowed" if intent == "implementation" else "forbidden"
            if mutation != expected_mutation:
                issues.append(ValidationIssue(
                    message=(
                        f"{context}.steps[{i}] (id={sid!r}): role 'worker' allows mutation iff intent is "
                        f"'implementation'; intent={intent!r} requires mutation_permission "
                        f"{expected_mutation!r}, got {mutation!r}"
                    )
                ))

        if independent_review:
            if role != "reviewer":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): independent_review requires role 'reviewer', got {role!r}"
                ))
            if intent != "review":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): independent_review requires intent 'review', got {intent!r}"
                ))
            if fresh_context is not True:
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): independent_review requires fresh_context true"
                ))
            if mutation != "forbidden":
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): independent_review requires mutation_permission 'forbidden'"
                ))
            if not depends_on:
                issues.append(ValidationIssue(
                    message=f"{context}.steps[{i}] (id={sid!r}): independent_review requires a dependency on prior work"
                ))

    has_independent_review_step = any(step["independent_review"] for step in steps)
    if plan["independent_review_required"] != has_independent_review_step:
        issues.append(ValidationIssue(
            message=(
                f"{context}: independent_review_required ({plan['independent_review_required']!r}) must equal "
                f"whether any step has independent_review true ({has_independent_review_step!r})"
            )
        ))


def validate_execution_plan(plan: Any, execution_plan_schema: dict, *, label: str) -> list[ValidationIssue]:
    """Full schema + semantic validation of a single execution plan. Semantic
    checks only run once the plan is schema-valid (they assume that shape)."""
    issues = validate_instance(plan, execution_plan_schema, label=label)
    if issues:
        return issues
    semantic_issues: list[ValidationIssue] = []
    validate_execution_plan_semantics(plan, semantic_issues, label)
    return semantic_issues


def _safe_sorted_repr(values: Any) -> list[str]:
    """Stable diagnostic rendering for untrusted YAML keys/values.

    Validation must fail closed with structured issues even when malformed YAML
    contains non-string keys or unhashable list members. Do not call set() or
    sorted() directly on untrusted collections before checking their shape.
    """
    try:
        return sorted(repr(v) for v in values)
    except TypeError:
        return [repr(values)]


def _validate_unique_string_subset(values: Any, allowed: frozenset[str], label: str, issues: list[ValidationIssue]) -> bool:
    if not isinstance(values, list) or not values:
        issues.append(ValidationIssue(message=f"{label} must be a non-empty unique subset of {sorted(allowed)}"))
        return False
    seen: set[str] = set()
    ok = True
    for idx, value in enumerate(values):
        if not isinstance(value, str) or value not in allowed:
            issues.append(ValidationIssue(
                message=f"{label}[{idx}] must be one of {sorted(allowed)}, got {value!r}"
            ))
            ok = False
            continue
        if value in seen:
            issues.append(ValidationIssue(message=f"{label} contains duplicate value {value!r}"))
            ok = False
        seen.add(value)
    return ok


def validate_model_profiles(policy: dict, issues: list[ValidationIssue]) -> None:
    """Validate the centralized family/capacity/model capability matrix."""
    profiles = policy.get("model_profiles")
    if not isinstance(profiles, dict):
        issues.append(ValidationIssue(message="policy.model_profiles must be a mapping"))
        return
    if set(k for k in profiles.keys() if isinstance(k, str)) != EXECUTION_FAMILIES or any(not isinstance(k, str) for k in profiles.keys()):
        issues.append(ValidationIssue(
            message=f"policy.model_profiles keys must be exactly {sorted(EXECUTION_FAMILIES)}, got {_safe_sorted_repr(profiles.keys())}"
        ))
    expected_keys = {"family", "reasoning_efforts", "context_strategies", "capacity_models"}
    for name, profile in profiles.items():
        label = f"policy.model_profiles.{name!r}"
        if not isinstance(name, str):
            issues.append(ValidationIssue(message=f"policy.model_profiles key {name!r} must be a string family name"))
            continue
        label = f"policy.model_profiles.{name}"
        if not isinstance(profile, dict):
            issues.append(ValidationIssue(message=f"{label} must be a mapping"))
            continue
        profile_keys = set(k for k in profile.keys() if isinstance(k, str))
        if profile_keys != expected_keys or any(not isinstance(k, str) for k in profile.keys()):
            issues.append(ValidationIssue(
                message=f"{label} keys must be exactly {sorted(expected_keys)}, got {_safe_sorted_repr(profile.keys())}"
            ))
        if profile.get("family") != name or profile.get("family") not in EXECUTION_FAMILIES:
            issues.append(ValidationIssue(message=f"{label}.family must equal profile name {name!r}"))
        _validate_unique_string_subset(profile.get("reasoning_efforts"), REASONING_EFFORTS, f"{label}.reasoning_efforts", issues)
        _validate_unique_string_subset(profile.get("context_strategies"), CONTEXT_STRATEGIES, f"{label}.context_strategies", issues)
        capacity_models = profile.get("capacity_models")
        expected_capacity_tiers = PROFILE_CAPACITY_TIERS.get(name, MANDATORY_PROFILE_CAPACITY_TIERS)
        if not isinstance(capacity_models, dict):
            issues.append(ValidationIssue(
                message=f"{label}.capacity_models keys must be exactly {sorted(expected_capacity_tiers)}"
            ))
            continue
        capacity_keys = set(k for k in capacity_models.keys() if isinstance(k, str))
        if capacity_keys != expected_capacity_tiers or any(not isinstance(k, str) for k in capacity_models.keys()):
            issues.append(ValidationIssue(
                message=(f"{label}.capacity_models keys must be exactly {sorted(expected_capacity_tiers)} "
                         f"(standard and high are mandatory), got {_safe_sorted_repr(capacity_models.keys())}")
            ))
            continue
        for tier, model_entry in capacity_models.items():
            model_label = f"{label}.capacity_models.{tier}"
            if not isinstance(model_entry, dict):
                issues.append(ValidationIssue(message=f"{model_label} must contain model, context_window_tier and ambiguity_capacity"))
                continue
            model_keys = set(k for k in model_entry.keys() if isinstance(k, str))
            if model_keys != {"model", "context_window_tier", "ambiguity_capacity"} or any(not isinstance(k, str) for k in model_entry.keys()):
                issues.append(ValidationIssue(message=f"{model_label} must contain model, context_window_tier and ambiguity_capacity"))
                continue
            if not isinstance(model_entry.get("model"), str) or not model_entry["model"]:
                issues.append(ValidationIssue(message=f"{model_label}.model must be a non-empty string"))
            context_window_tier = model_entry.get("context_window_tier")
            if not isinstance(context_window_tier, str) or context_window_tier not in CONTEXT_WINDOW_TIERS:
                issues.append(ValidationIssue(
                    message=f"{model_label}.context_window_tier must be one of {sorted(CONTEXT_WINDOW_TIERS)}"
                ))
            ambiguity_capacity = model_entry.get("ambiguity_capacity")
            if isinstance(ambiguity_capacity, bool) or not isinstance(ambiguity_capacity, int) or not 0 <= ambiguity_capacity <= 10:
                issues.append(ValidationIssue(message=f"{model_label}.ambiguity_capacity must be an integer from 0 to 10"))
            elif ambiguity_capacity < TIER_AMBIGUITY_FLOORS[tier]:
                issues.append(ValidationIssue(
                    message=f"{model_label}.ambiguity_capacity must be at least {TIER_AMBIGUITY_FLOORS[tier]} for tier {tier}"
                ))


_STEP_MATCH_UNIVERSES = {
    "family": EXECUTION_FAMILIES,
    "role": EXECUTION_ROLES,
    "intent": EXECUTION_INTENTS,
    "capacity_tier": AUTHORED_CAPACITY_TIERS,
}
_STEP_MATCH_REQUIRED_KEYS = frozenset({"role", "intent", "capacity_tier", "independent_review"})
_STEP_MATCH_ALLOWED_KEYS = _STEP_MATCH_REQUIRED_KEYS | {"family"}


def _step_match_values(step_match: dict, key: str) -> list | None:
    """Normalize one step_match selector to a non-empty, duplicate-free list,
    or return None when it is malformed. An omitted `family` means every
    execution family."""
    if key == "family" and "family" not in step_match:
        return sorted(EXECUTION_FAMILIES)
    value = step_match.get(key)
    if key == "independent_review":
        values = value if isinstance(value, list) else [value]
        if not values or any(not isinstance(v, bool) for v in values) or len(set(values)) != len(values):
            return None
        return values
    values = value if isinstance(value, list) else [value]
    universe = _STEP_MATCH_UNIVERSES[key]
    if (not values or any(not isinstance(v, str) or v not in universe for v in values)
            or len(set(values)) != len(values)):
        return None
    return values


def _leaf_key(leaf: Any) -> tuple | None:
    if (not isinstance(leaf, dict) or set(leaf.keys()) != {"field", "operator", "value"}
            or not isinstance(leaf.get("field"), str) or not isinstance(leaf.get("operator"), str)):
        return None
    value = leaf["value"]
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            return None
        value = ("list", tuple((type(v).__name__, v) for v in value))
    elif isinstance(value, dict):
        return None
    else:
        value = (type(value).__name__, value)
    return (leaf["field"], leaf["operator"], value)


def validate_capacity_assignment(policy: dict, field_info: dict, issues: list[ValidationIssue]) -> None:
    """Validate the ordered, graduated post-route capacity rules (policy v7).

    Hard invariants, all checked structurally so a policy typo fails closed:

    * rule names are unique and equal `REQUIRED_CAPACITY_RULES`, each with
      its declared `direction`;
    * every rule's `match` is a flat `{"all": [leaves]}` expression;
    * a `down` rule's `match.all` contains every `CAPACITY_SAFETY_LEAVES`
      leaf verbatim, targets only non-independent-review worker/specifier
      steps of `analysis`/`implementation` intent, and assigns a tier
      strictly lower than every authored tier it selects;
    * an `up` rule's `match.all` equals `CAPACITY_ESCALATION_LEAVES`
      exactly, targets only specifier/reviewer steps, assigns only `max`,
      and keeps effort unchanged (`high`, the strongest effort);
    * every assigned tier and effort exists in each targeted family profile.

    The engine additionally only ever keeps or lowers reasoning effort.
    """
    assignment = policy.get("capacity_assignment")
    if not isinstance(assignment, dict) or set(assignment.keys()) != {"rules"}:
        issues.append(ValidationIssue(
            message="policy.capacity_assignment must be a mapping with exactly one key: 'rules'"
        ))
        return
    rules = assignment.get("rules")
    if not isinstance(rules, list) or not rules:
        issues.append(ValidationIssue(message="policy.capacity_assignment.rules must be a non-empty list"))
        return
    if len(rules) != len(REQUIRED_CAPACITY_RULES):
        issues.append(ValidationIssue(
            message=f"policy.capacity_assignment.rules must contain exactly {list(REQUIRED_CAPACITY_RULES)}"
        ))

    names: set[str] = set()
    profiles = policy.get("model_profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    safety_keys = {_leaf_key(leaf) for leaf in CAPACITY_SAFETY_LEAVES}
    escalation_keys = sorted(_leaf_key(leaf) for leaf in CAPACITY_ESCALATION_LEAVES)
    expected_keys = {"name", "direction", "match", "step_match", "assigned_capacity_tier", "assigned_reasoning_effort"}
    for index, rule in enumerate(rules):
        label = f"policy.capacity_assignment.rules[{index}]"
        if not isinstance(rule, dict):
            issues.append(ValidationIssue(message=f"{label} must be a mapping"))
            continue
        if set(k for k in rule.keys() if isinstance(k, str)) != expected_keys or any(not isinstance(k, str) for k in rule.keys()):
            issues.append(ValidationIssue(
                message=f"{label} keys must be exactly {sorted(expected_keys)}, got {_safe_sorted_repr(rule.keys())}"
            ))
            continue

        name = rule.get("name")
        if not isinstance(name, str) or not name:
            issues.append(ValidationIssue(message=f"{label}.name must be a non-empty string"))
            name = None
        elif name in names:
            issues.append(ValidationIssue(message=f"{label}.name duplicates {name!r}"))
        else:
            names.add(name)
        if name is not None and name not in REQUIRED_CAPACITY_RULES:
            issues.append(ValidationIssue(
                message=f"{label}.name {name!r} is not a known capacity rule; only {list(REQUIRED_CAPACITY_RULES)} are defined"
            ))

        direction = rule.get("direction")
        if not isinstance(direction, str) or direction not in CAPACITY_RULE_DIRECTIONS:
            issues.append(ValidationIssue(message=f"{label}.direction must be one of {sorted(CAPACITY_RULE_DIRECTIONS)}"))
            continue
        if name in REQUIRED_CAPACITY_RULES and REQUIRED_CAPACITY_RULES[name] != direction:
            issues.append(ValidationIssue(
                message=f"{label}.direction must be {REQUIRED_CAPACITY_RULES[name]!r} for rule {name!r}"
            ))

        match = rule.get("match")
        validate_expr(match, field_info, issues, f"{label}.match")
        leaves = match.get("all") if isinstance(match, dict) and set(match.keys()) == {"all"} else None
        leaf_keys = [_leaf_key(leaf) for leaf in leaves] if isinstance(leaves, list) else None
        if leaf_keys is None or not leaf_keys or any(key is None for key in leaf_keys):
            issues.append(ValidationIssue(
                message=f"{label}.match must be a flat 'all' list of field/operator/value predicates"
            ))
            leaf_keys = None
        elif len(set(leaf_keys)) != len(leaf_keys):
            issues.append(ValidationIssue(message=f"{label}.match contains duplicate predicates"))

        if leaf_keys is not None:
            if direction == "down" and not safety_keys.issubset(leaf_keys):
                issues.append(ValidationIssue(
                    message=(f"{label}.match must include the centralized safety predicates "
                             "security_sensitive == false and destructive_operation == false; "
                             "capacity can never be lowered for security-sensitive or destructive work")
                ))
            if direction == "up" and sorted(leaf_keys) != escalation_keys:
                issues.append(ValidationIssue(
                    message=(f"{label}.match must equal exactly the escalation predicates failure_cost == high, "
                             "semantic_correctness == critical, ambiguity >= 7")
                ))

        step_match = rule.get("step_match")
        if (not isinstance(step_match, dict)
                or any(not isinstance(k, str) for k in step_match.keys())
                or not _STEP_MATCH_REQUIRED_KEYS.issubset(step_match.keys())
                or not set(step_match.keys()).issubset(_STEP_MATCH_ALLOWED_KEYS)):
            got = _safe_sorted_repr(step_match.keys()) if isinstance(step_match, dict) else repr(step_match)
            issues.append(ValidationIssue(
                message=(f"{label}.step_match keys must be {sorted(_STEP_MATCH_REQUIRED_KEYS)} "
                         f"plus optional 'family', got {got}")
            ))
            continue
        selectors = {key: _step_match_values(step_match, key) for key in _STEP_MATCH_ALLOWED_KEYS}
        bad = sorted(key for key, values in selectors.items() if values is None)
        if bad:
            issues.append(ValidationIssue(message=f"{label}.step_match has invalid selector(s) {bad}"))
            continue
        families = selectors["family"]
        authored_tiers = selectors["capacity_tier"]

        assigned_tier = rule.get("assigned_capacity_tier")
        if not isinstance(assigned_tier, str) or assigned_tier not in CAPACITY_TIERS:
            issues.append(ValidationIssue(
                message=f"{label}.assigned_capacity_tier must be one of {list(CAPACITY_TIER_ORDER)}"
            ))
            assigned_tier = None

        if direction == "down":
            if not set(selectors["role"]).issubset({"worker", "specifier"}):
                issues.append(ValidationIssue(
                    message=f"{label}.step_match.role: a down rule may target only worker or specifier steps"
                ))
            if selectors["independent_review"] != [False]:
                issues.append(ValidationIssue(
                    message=f"{label}.step_match.independent_review must be false: independent review is never lowered"
                ))
            if not set(selectors["intent"]).issubset({"analysis", "implementation"}):
                issues.append(ValidationIssue(
                    message=f"{label}.step_match.intent: a down rule may target only analysis/implementation steps"
                ))
            if assigned_tier is not None and any(
                CAPACITY_TIER_RANK[assigned_tier] >= CAPACITY_TIER_RANK[tier] for tier in authored_tiers
            ):
                issues.append(ValidationIssue(
                    message=(f"{label}.assigned_capacity_tier {assigned_tier!r} must be strictly lower than every "
                             f"selected authored tier {authored_tiers}; a down rule may only lower capacity")
                ))
        else:
            if not set(selectors["role"]).issubset({"specifier", "reviewer"}):
                issues.append(ValidationIssue(
                    message=f"{label}.step_match.role: an up rule may target only specifier or reviewer steps"
                ))
            if assigned_tier is not None and assigned_tier != "max":
                issues.append(ValidationIssue(message=f"{label}.assigned_capacity_tier must be 'max' for an up rule"))
            if any(CAPACITY_TIER_RANK[tier] >= CAPACITY_TIER_RANK["max"] for tier in authored_tiers):
                issues.append(ValidationIssue(
                    message=f"{label}.step_match.capacity_tier must select tiers strictly below 'max'"
                ))
            if rule.get("assigned_reasoning_effort") != "high":
                issues.append(ValidationIssue(
                    message=f"{label}.assigned_reasoning_effort must be 'high' so escalation keeps the authored effort"
                ))

        assigned_effort = rule.get("assigned_reasoning_effort")
        if not isinstance(assigned_effort, str) or assigned_effort not in REASONING_EFFORTS:
            issues.append(ValidationIssue(
                message=f"{label}.assigned_reasoning_effort must be one of {sorted(REASONING_EFFORTS)}"
            ))
            assigned_effort = None

        for family in families:
            profile = profiles.get(family)
            profile = profile if isinstance(profile, dict) else {}
            capacity_models = profile.get("capacity_models")
            if assigned_tier is not None and (not isinstance(capacity_models, dict) or assigned_tier not in capacity_models):
                issues.append(ValidationIssue(
                    message=(f"{label}.assigned_capacity_tier {assigned_tier!r} requires "
                             f"policy.model_profiles.{family}.capacity_models.{assigned_tier}")
                ))
            efforts = profile.get("reasoning_efforts")
            if assigned_effort is not None and (not isinstance(efforts, list) or assigned_effort not in efforts):
                issues.append(ValidationIssue(
                    message=(f"{label}.assigned_reasoning_effort {assigned_effort!r} is not supported by "
                             f"policy.model_profiles.{family}.reasoning_efforts")
                ))

    missing = [name for name in REQUIRED_CAPACITY_RULES if name not in names]
    if missing:
        issues.append(ValidationIssue(
            message=f"policy.capacity_assignment.rules must define {missing}"
        ))


def validate_execution_backends(policy: dict, execution_plan_schema: dict, issues: list[ValidationIssue]) -> None:
    backends = policy.get("execution_backends")
    if not isinstance(backends, dict):
        issues.append(ValidationIssue(message="policy.execution_backends must be a mapping"))
        return
    backend_keys = set(k for k in backends.keys() if isinstance(k, str))
    if backend_keys != EXECUTION_FAMILIES or any(not isinstance(k, str) for k in backends.keys()):
        issues.append(ValidationIssue(
            message=f"policy.execution_backends keys must be exactly {sorted(EXECUTION_FAMILIES)}, got {_safe_sorted_repr(backends.keys())}"
        ))

    profiles = policy.get("model_profiles")
    profile_keys = set(k for k in profiles.keys() if isinstance(k, str)) if isinstance(profiles, dict) else set()
    for family, entry in backends.items():
        if family not in EXECUTION_FAMILIES:
            continue
        if not isinstance(entry, dict):
            issues.append(ValidationIssue(message=f"policy.execution_backends.{family} must be a mapping"))
            continue
        tool = entry.get("tool")
        if not isinstance(tool, str) or not tool:
            issues.append(ValidationIssue(message=f"policy.execution_backends.{family}.tool must be a non-empty string"))
        profile_ref = entry.get("model_profile_ref")
        if not isinstance(profile_ref, str) or profile_ref not in profile_keys:
            issues.append(ValidationIssue(
                message=f"policy.execution_backends.{family}.model_profile_ref {profile_ref!r} does not resolve through policy.model_profiles"
            ))
        elif not isinstance(profiles[profile_ref], dict) or profiles[profile_ref].get("family") != family:
            issues.append(ValidationIssue(
                message=f"policy.execution_backends.{family}.model_profile_ref must resolve to family {family!r}"
            ))

        if family == "claude":
            expected_keys = {"tool", "model_profile_ref", "mutation_modes"}
            mutation_modes = entry.get("mutation_modes")
            if not isinstance(mutation_modes, dict) or set(mutation_modes.keys()) != MUTATION_MODE_KEYS:
                issues.append(ValidationIssue(
                    message=f"policy.execution_backends.claude.mutation_modes must have exactly {sorted(MUTATION_MODE_KEYS)}"
                ))
            elif mutation_modes != {"forbidden": "read", "allowed": "full"}:
                issues.append(ValidationIssue(message="policy.execution_backends.claude.mutation_modes must map forbidden->read and allowed->full"))
        else:
            expected_keys = {"tool", "model_profile_ref"}
        entry_keys = set(k for k in entry.keys() if isinstance(k, str))
        if entry_keys != expected_keys or any(not isinstance(k, str) for k in entry.keys()):
            issues.append(ValidationIssue(
                message=f"policy.execution_backends.{family} keys must be exactly {sorted(expected_keys)}, got {_safe_sorted_repr(entry.keys())}"
            ))

    validate_policy_execution_plans(policy, execution_plan_schema, issues)


def validate_policy_execution_plans(policy: dict, execution_plan_schema: dict, issues: list[ValidationIssue]) -> None:
    """Validate the `execution.<ROUTE>.primary_intents.<intent>` plans embedded
    in `policy` against the canonical route topology: every route/primary-intent
    combination must be schema/semantic valid and must equal the fixed
    topology for that route, varying only the worker step's `intent` and
    `mutation_permission` across primary-intent variants."""
    schema = execution_plan_schema
    execution = policy.get("execution")
    if not isinstance(execution, dict):
        issues.append(ValidationIssue(message="policy.execution must be a mapping"))
        return
    if set(execution.keys()) != ROUTES:
        issues.append(ValidationIssue(
            message=f"policy.execution keys must be exactly {sorted(ROUTES)}, got {sorted(execution.keys())}"
        ))

    for route, topology in CANONICAL_ROUTE_TOPOLOGY.items():
        route_entry = execution.get(route)
        if not isinstance(route_entry, dict) or set(route_entry.keys()) != {"primary_intents"}:
            issues.append(ValidationIssue(
                message=f"policy.execution.{route} must be a mapping with exactly one key: 'primary_intents'"
            ))
            continue

        primary_intents = route_entry["primary_intents"]
        if not isinstance(primary_intents, dict) or set(primary_intents.keys()) != PRIMARY_INTENTS:
            issues.append(ValidationIssue(
                message=(
                    f"policy.execution.{route}.primary_intents keys must be exactly {sorted(PRIMARY_INTENTS)}, "
                    f"got {sorted(primary_intents.keys()) if isinstance(primary_intents, dict) else primary_intents!r}"
                )
            ))
            continue

        worker_step_id = next(step["id"] for step in topology if step["role"] in ("worker",))
        expected_independent_review_required = route in ROUTES_WITH_INDEPENDENT_REVIEW

        for intent_name, plan in primary_intents.items():
            label = f"policy.execution.{route}.primary_intents.{intent_name}"
            plan_issues = validate_execution_plan(plan, schema, label=label)
            if plan_issues:
                issues.extend(plan_issues)
                continue

            steps = plan["steps"]
            if len(steps) != len(topology):
                issues.append(ValidationIssue(
                    message=f"{label}: must have exactly {len(topology)} step(s) for route {route}, got {len(steps)}"
                ))
                continue

            if plan["independent_review_required"] != expected_independent_review_required:
                issues.append(ValidationIssue(
                    message=(
                        f"{label}: independent_review_required must be {expected_independent_review_required!r} "
                        f"for route {route}"
                    )
                ))

            for step, canonical in zip(steps, topology, strict=True):
                if step["id"] != canonical["id"] or step["role"] != canonical["role"] or step["family"] != canonical["family"]:
                    issues.append(ValidationIssue(
                        message=(
                            f"{label}: step {step['id']!r} must match canonical topology "
                            f"{canonical!r} for route {route}"
                        )
                    ))
                if sorted(step["depends_on"]) != sorted(canonical["depends_on"]):
                    issues.append(ValidationIssue(
                        message=(
                            f"{label}: step {step['id']!r}.depends_on must equal canonical "
                            f"{canonical['depends_on']!r}, got {step['depends_on']!r}"
                        )
                    ))

                if step["id"] == worker_step_id:
                    expected_mutation = "allowed" if intent_name == "implementation" else "forbidden"
                    if step["intent"] != intent_name:
                        issues.append(ValidationIssue(
                            message=f"{label}: worker step intent must equal the primary intent {intent_name!r}, got {step['intent']!r}"
                        ))
                    if step["mutation_permission"] != expected_mutation:
                        issues.append(ValidationIssue(
                            message=f"{label}: worker step mutation_permission must be {expected_mutation!r}, got {step['mutation_permission']!r}"
                        ))
                elif canonical["role"] == "specifier" and step["intent"] != "analysis":
                    issues.append(ValidationIssue(
                        message=f"{label}: specifier step intent must always be 'analysis', got {step['intent']!r}"
                    ))
                elif canonical["role"] == "reviewer" and step["intent"] != "review":
                    issues.append(ValidationIssue(
                        message=f"{label}: reviewer step intent must always be 'review', got {step['intent']!r}"
                    ))

                if step["capacity_tier"] not in AUTHORED_CAPACITY_TIERS:
                    issues.append(ValidationIssue(
                        message=(f"{label}: step {step['id']!r}.capacity_tier must be authored as one of "
                                 f"{sorted(AUTHORED_CAPACITY_TIERS)}; light and economy are post-route assignment only")
                    ))

                profiles = policy.get("model_profiles", {})
                profile = profiles.get(step["family"], {}) if isinstance(profiles, dict) else {}
                if not isinstance(profile, dict):
                    profile = {}
                capacity_models = profile.get("capacity_models")
                if not isinstance(capacity_models, dict) or step["capacity_tier"] not in capacity_models:
                    issues.append(ValidationIssue(
                        message=f"{label}: step {step['id']!r}.capacity_tier is not supported by its family profile"
                    ))
                reasoning_efforts = profile.get("reasoning_efforts")
                if not isinstance(reasoning_efforts, list) or step["reasoning_effort"] not in reasoning_efforts:
                    issues.append(ValidationIssue(
                        message=f"{label}: step {step['id']!r}.reasoning_effort is not supported by its family profile"
                    ))
                context_strategies = profile.get("context_strategies")
                if not isinstance(context_strategies, list) or step["context_strategy"] not in context_strategies:
                    issues.append(ValidationIssue(
                        message=f"{label}: step {step['id']!r}.context_strategy is not supported by its family profile"
                    ))

        # Route topology and execution metadata are invariant across primary
        # intents. Only the worker's intent and mutation permission may vary.
        valid_plans = [primary_intents.get(name) for name in sorted(PRIMARY_INTENTS)]
        if all(isinstance(plan, dict) and isinstance(plan.get("steps"), list) for plan in valid_plans):
            baseline = valid_plans[0]
            for other in valid_plans[1:]:
                if len(other["steps"]) != len(baseline["steps"]):
                    continue
                for baseline_step, other_step in zip(baseline["steps"], other["steps"], strict=True):
                    for field in EXECUTION_METADATA_FIELDS:
                        if baseline_step.get(field) != other_step.get(field):
                            issues.append(ValidationIssue(
                                message=(f"policy.execution.{route}: step {baseline_step.get('id')!r}.{field} "
                                         "must be identical across all primary-intent variants")
                            ))


def derive_primary_intent(effective_classification: dict) -> str:
    """Central, single-source derivation of a task's primary execution intent
    from its effective classification. This is execution materialization, not
    route selection: it never influences which route matched or was chosen."""
    if effective_classification.get("implementation_required") is True:
        return "implementation"
    if effective_classification.get("task_type") in ("review", "security_review"):
        return "review"
    return "analysis"


def materialize_execution_plan(
    policy: dict, execution_plan_schema: dict, selected_route: str, primary_intent: str
) -> dict:
    """Select the policy's execution plan for `selected_route`/`primary_intent`,
    deep-copy it, and revalidate the copy before returning it. Raises
    RoutingAssetError (a fail-closed bootstrap/materialization failure) if the
    plan is missing or invalid -- this must never silently return a corrupt
    plan."""
    try:
        plan = policy["execution"][selected_route]["primary_intents"][primary_intent]
    except (KeyError, TypeError) as exc:
        raise RoutingAssetError(
            f"no execution plan for route={selected_route!r} primary_intent={primary_intent!r}: {exc}"
        ) from exc

    plan_copy = copy.deepcopy(plan)
    issues = validate_execution_plan(
        plan_copy, execution_plan_schema, label=f"execution_plan[{selected_route}][{primary_intent}]"
    )
    if issues:
        joined = "; ".join(str(i) for i in issues)
        raise RoutingAssetError(f"materialized execution plan is invalid: {joined}")
    return plan_copy


def _step_matches_capacity_rule(step: dict, step_match: dict) -> bool:
    """Exact/member matching for the small, validated step_match vocabulary."""
    for field, expected in step_match.items():
        actual = step.get(field)
        if isinstance(expected, list):
            if not any(json_eq(actual, candidate) for candidate in expected):
                return False
        elif not json_eq(actual, expected):
            return False
    return True


def _evaluate_rule_leaves(rule: dict, effective_classification: dict) -> tuple[list[dict], list[dict]]:
    """Evaluate each flat leaf of a capacity rule's `{"all": [...]}` guard
    against the effective classification, in policy-authored order.

    Returns `(passed, blocking)`: the predicates that were satisfied and the
    predicates that were not, each tagged with the rule name and carrying the
    actual classification value observed."""
    passed: list[dict] = []
    blocking: list[dict] = []
    for leaf in rule["match"]["all"]:
        entry = {
            "rule": rule["name"],
            "field": leaf["field"],
            "operator": leaf["operator"],
            "value": leaf["value"],
            "actual": effective_classification.get(leaf["field"]),
        }
        (passed if evaluate_expr(leaf, effective_classification) else blocking).append(entry)
    return passed, blocking


def assign_capacities(policy: dict, execution_plan: dict, effective_classification: dict) -> list[dict]:
    """Assign one capacity tier per authored plan step, after route resolution.

    The execution plan is never mutated. For each step, rules are considered
    in policy order; a rule is a *candidate* when its `step_match` selects the
    step, and the first candidate whose classification guard fully holds
    wins (first match wins). Down rules lower the tier; the single up rule
    raises a specifier/reviewer to `max`. Reasoning effort is only ever kept
    or lowered (`min` of authored and assigned).

    Evidence is deterministic: `guard_predicates_passed` and
    `blocking_predicates` accumulate, in order and tagged by rule name, the
    predicates of every candidate rule evaluated up to and including the
    winner, so `validate_route_task_result`'s exact recomputation check can
    never be satisfied by a forged or hand-edited assignment.
    """
    rules = policy["capacity_assignment"]["rules"]
    profiles = policy["model_profiles"]
    assignments: list[dict] = []
    for step in execution_plan["steps"]:
        authored_tier = step["capacity_tier"]
        assigned_tier = authored_tier
        authored_effort = step["reasoning_effort"]
        assigned_effort = authored_effort
        matched_rule_name = None
        guard_passed: list[dict] = []
        blocking: list[dict] = []
        blocked_summary: list[str] = []
        decision_reason = "no capacity rule targets this step's family, role, intent, or authored capacity tier"

        for rule in rules:
            if not _step_matches_capacity_rule(step, rule["step_match"]):
                continue
            passed, unmet = _evaluate_rule_leaves(rule, effective_classification)
            guard_passed.extend(passed)
            blocking.extend(unmet)
            if unmet:
                blocked_summary.append(f"{rule['name']} blocked by {len(unmet)} unmet guard predicate(s)")
                continue
            candidate = profiles[step["family"]]["capacity_models"][rule["assigned_capacity_tier"]]
            required = effective_classification["ambiguity"]
            if candidate["ambiguity_capacity"] < required:
                blocked_summary.append(
                    f"{rule['name']} blocked: model ambiguity capacity "
                    f"{candidate['ambiguity_capacity']} is below task ambiguity {required}"
                )
                continue
            assigned_tier = rule["assigned_capacity_tier"]
            assigned_effort = min(authored_effort, rule["assigned_reasoning_effort"],
                                  key=REASONING_EFFORT_ORDER.__getitem__)
            matched_rule_name = rule["name"]
            decision_reason = (
                f"{rule['name']} ({rule['direction']}): effective classification satisfies every guard predicate"
            )
            if blocked_summary:
                decision_reason += f"; earlier candidates: {'; '.join(blocked_summary)}"
            break
        else:
            if blocked_summary:
                decision_reason = f"authored tier kept: {'; '.join(blocked_summary)}"

        model_entry = profiles[step["family"]]["capacity_models"][assigned_tier]
        required_ambiguity = effective_classification["ambiguity"]
        if model_entry["ambiguity_capacity"] < required_ambiguity:
            raise RoutingAssetError(
                f"{step['family']} {assigned_tier} model {model_entry['model']!r} supports "
                f"{model_entry['ambiguity_capacity']} ambiguity, but task requires {required_ambiguity}"
            )
        assignments.append({
            "step_id": step["id"],
            "authored_capacity_tier": authored_tier,
            "assigned_capacity_tier": assigned_tier,
            "authored_reasoning_effort": authored_effort,
            "assigned_reasoning_effort": assigned_effort,
            "rule": matched_rule_name,
            "decision_reason": decision_reason,
            "guard_predicates_passed": guard_passed,
            "blocking_predicates": blocking,
            "selected_model_profile": {
                "family": step["family"],
                "capacity_tier": assigned_tier,
                "model": model_entry["model"],
                "context_window_tier": model_entry["context_window_tier"],
                "ambiguity_capacity": model_entry["ambiguity_capacity"],
            },
        })
    return assignments


def _validated_assignment_tiers(execution_plan: dict, capacity_assignments: list[dict] | None) -> dict[str, str]:
    if capacity_assignments is None:
        return {step["id"]: step["capacity_tier"] for step in execution_plan["steps"]}
    if not isinstance(capacity_assignments, list) or len(capacity_assignments) != len(execution_plan["steps"]):
        raise RoutingAssetError("capacity_assignments must contain exactly one entry per execution-plan step")
    tiers: dict[str, str] = {}
    for step, assignment in zip(execution_plan["steps"], capacity_assignments, strict=True):
        if not isinstance(assignment, dict) or set(assignment.keys()) != CAPACITY_ASSIGNMENT_KEYS:
            raise RoutingAssetError("capacity assignment has an invalid shape")
        if (assignment["step_id"] != step["id"]
                or assignment["authored_capacity_tier"] != step["capacity_tier"]
                or assignment["authored_reasoning_effort"] != step["reasoning_effort"]):
            raise RoutingAssetError("capacity assignment does not correspond to its authored execution-plan step")
        effort = assignment["assigned_reasoning_effort"]
        if (not isinstance(effort, str) or effort not in REASONING_EFFORTS
                or REASONING_EFFORT_ORDER[effort] > REASONING_EFFORT_ORDER[step["reasoning_effort"]]):
            raise RoutingAssetError("capacity assignment may only keep or lower the authored reasoning effort")
        assigned = assignment["assigned_capacity_tier"]
        if not isinstance(assigned, str) or assigned not in CAPACITY_TIERS:
            raise RoutingAssetError(f"unknown assigned capacity tier {assigned!r}")
        if (CAPACITY_TIER_RANK[assigned] > CAPACITY_TIER_RANK[step["capacity_tier"]]
                and (assigned != "max" or step.get("role") not in ("specifier", "reviewer"))):
            raise RoutingAssetError("capacity assignment may raise only a specifier or reviewer step, and only to max")
        if (CAPACITY_TIER_RANK[assigned] < CAPACITY_TIER_RANK[step["capacity_tier"]]
                and step.get("independent_review") is True):
            raise RoutingAssetError("capacity assignment may never lower an independent-review step")
        tiers[step["id"]] = assigned
    return tiers


def resolve_backend_bindings(
    policy: dict, execution_plan: dict, capacity_assignments: list[dict] | None = None
) -> dict:
    """Resolve only family/capacity combinations selected for this result.

    Concrete models remain separate from authored plans. When post-route
    assignments are supplied, only assigned (not merely authored) tiers are
    exposed by the returned backend bindings.
    """
    backends = policy["execution_backends"]
    profiles = policy["model_profiles"]
    assigned_tiers = _validated_assignment_tiers(execution_plan, capacity_assignments)
    used: dict[str, set[str]] = {}
    for step in execution_plan["steps"]:
        used.setdefault(step["family"], set()).add(assigned_tiers[step["id"]])

    bindings: dict[str, dict] = {}
    for family in sorted(used):
        backend_entry = backends[family]
        profile = profiles[backend_entry["model_profile_ref"]]
        try:
            selected_models = {
                tier: copy.deepcopy(profile["capacity_models"][tier])
                for tier in sorted(used[family])
            }
        except KeyError as exc:
            raise RoutingAssetError(
                f"assigned capacity tier {exc.args[0]!r} is unsupported by family {family!r}"
            ) from exc
        binding = {"tool": backend_entry["tool"], "capacity_models": selected_models}
        if "mutation_modes" in backend_entry:
            binding["mutation_modes"] = dict(backend_entry["mutation_modes"])
        bindings[family] = binding
    return bindings


def validate_delegation_policy(
    delegation_policy: Any,
    delegation_schema: dict,
    routing_policy: dict,
    issues: list[ValidationIssue],
) -> None:
    """Validate the nested lead-agent delegation policy and cross-file model-tier references.

    Delegation is deliberately separate from deterministic top-level routing:
    this policy may guide a lead worker's DIRECT/DELEGATE/PARALLEL choice, but
    it cannot alter routes, precedence, risk floors, execution-plan topology,
    mutation permissions, or required independent review.
    """
    if not isinstance(delegation_policy, dict):
        issues.append(ValidationIssue(message="routing/delegation-policy.yaml must be a mapping"))
        return

    validator = jsonschema.Draft202012Validator(delegation_schema)
    issue_count_before_schema = len(issues)
    for error in sorted(validator.iter_errors(delegation_policy), key=str):
        issues.append(ValidationIssue(
            path="delegation_policy" + _format_json_path(error.absolute_path),
            message=error.message,
        ))
    if len(issues) > issue_count_before_schema:
        return

    if set(delegation_policy["allowed_modes"]) != DELEGATION_MODES:
        issues.append(ValidationIssue(
            message=(
                "delegation_policy.allowed_modes must contain exactly "
                f"{sorted(DELEGATION_MODES)}"
            )
        ))

    shared = delegation_policy["shared"]
    shared_max = shared["max_parallel_workers"]
    for family in ("codex", "claude"):
        family_max = delegation_policy["families"][family]["max_parallel_workers"]
        if family_max > shared_max:
            issues.append(ValidationIssue(
                message=(
                    f"delegation_policy.families.{family}.max_parallel_workers "
                    "cannot exceed shared.max_parallel_workers"
                )
            ))

    codex = delegation_policy["families"]["codex"]
    if codex["planning_assignment_min"] > codex["planning_assignment_max"]:
        issues.append(ValidationIssue(
            message="delegation_policy.families.codex planning_assignment_min cannot exceed planning_assignment_max"
        ))

    claude_profile = routing_policy.get("model_profiles", {}).get("claude", {})
    supported_tiers = set(claude_profile.get("capacity_models", {}).keys())
    for role, tier in delegation_policy["families"]["claude"]["model_tiers"].items():
        if tier not in supported_tiers:
            issues.append(ValidationIssue(
                message=(
                    f"delegation_policy.families.claude.model_tiers.{role}={tier!r} "
                    "must resolve through routing/default-policy.yaml model_profiles.claude.capacity_models"
                )
            ))

    benchmark_topologies = set(delegation_policy["learning"]["required_benchmark_topologies"])
    if benchmark_topologies != DELEGATION_BENCHMARK_TOPOLOGIES:
        issues.append(ValidationIssue(
            message=(
                "delegation_policy.learning.required_benchmark_topologies must contain exactly "
                f"{sorted(DELEGATION_BENCHMARK_TOPOLOGIES)}"
            )
        ))


def validate_policy_structure(
    policy: dict, field_info: dict, execution_plan_schema: dict, issues: list[ValidationIssue]
) -> None:
    if not isinstance(policy, dict):
        issues.append(ValidationIssue(message="policy must be a mapping"))
        return
    if policy.get("version") != 7:
        issues.append(ValidationIssue(message="policy.version must be 7"))

    routes = policy.get("routes")
    if not isinstance(routes, dict):
        issues.append(ValidationIssue(message="policy.routes must be a mapping"))
        return
    route_names = set(routes.keys())
    if route_names != ROUTES:
        issues.append(ValidationIssue(
            message=f"policy.routes keys must be exactly {sorted(ROUTES)}, got {sorted(route_names)}"
        ))

    for name, route in routes.items():
        if name not in ROUTES:
            continue
        if not isinstance(route, dict) or "match" not in route:
            issues.append(ValidationIssue(message=f"policy.routes.{name} must define a 'match' expression"))
            continue
        validate_expr(route["match"], field_info, issues, f"policy.routes.{name}.match")

    precedence = policy.get("precedence")
    if not isinstance(precedence, list):
        issues.append(ValidationIssue(message="policy.precedence must be a list"))
    else:
        if sorted(precedence) != sorted(ROUTES) or len(precedence) != len(ROUTES):
            issues.append(ValidationIssue(
                message=f"policy.precedence must contain every route in {sorted(ROUTES)} exactly once, got {precedence}"
            ))

    fallback_route = policy.get("fallback_route")
    if fallback_route not in ROUTES:
        issues.append(ValidationIssue(
            message=f"policy.fallback_route must be one of {sorted(ROUTES)}, got {fallback_route!r}"
        ))

    validate_model_profiles(policy, issues)
    validate_capacity_assignment(policy, field_info, issues)
    validate_execution_backends(policy, execution_plan_schema, issues)

    review_findings = policy.get("review_findings")
    if not isinstance(review_findings, dict):
        issues.append(ValidationIssue(message="policy.review_findings must be a mapping"))
    else:
        categories = set()
        for bucket in ("blocking", "repair_required", "advisory"):
            values = review_findings.get(bucket)
            if not isinstance(values, list) or not values:
                issues.append(ValidationIssue(
                    message=f"policy.review_findings.{bucket} must be a non-empty list"
                ))
                continue
            categories.update(values)
        if categories != REVIEW_CATEGORIES:
            issues.append(ValidationIssue(
                message=(
                    f"policy.review_findings categories must be exactly {sorted(REVIEW_CATEGORIES)}, "
                    f"got {sorted(categories)}"
                )
            ))

    completion = policy.get("completion", {})
    require = completion.get("require") if isinstance(completion, dict) else None
    if not isinstance(require, list):
        issues.append(ValidationIssue(message="policy.completion.require must be a list"))
    else:
        required_phrases = [
            "no unresolved BLOCKER",
            "no unresolved CORRECTNESS",
            "no unresolved REGRESSION",
            "no unresolved TEST_GAP",
        ]
        for phrase in required_phrases:
            if phrase not in require:
                issues.append(ValidationIssue(
                    message=f"policy.completion.require is missing required entry: {phrase!r}"
                ))


# ---------------------------------------------------------------------------
# Deterministic evaluator (no eval, no LLM)
# ---------------------------------------------------------------------------

def json_eq(a, b) -> bool:
    return type(a) is type(b) and a == b


def json_deep_eq(a, b) -> bool:
    """Recursive, JSON-type-sensitive equality for nested structures.

    Python's native `==` treats `True`/`False` as equal to `1`/`0` even
    inside nested dicts/lists, so plain `!=` on a structure like
    `capacity_assignments` (which embeds classification-derived booleans and
    numbers as evidence) can be satisfied by forged output that swaps a
    boolean for its numeric twin. This walks dicts/lists structurally and
    falls back to `json_eq` only at scalar leaves.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_deep_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_deep_eq(x, y) for x, y in zip(a, b, strict=True))
    return json_eq(a, b)


def evaluate_expr(node: dict, task: dict) -> bool:
    if "all" in node:
        return all(evaluate_expr(c, task) for c in node["all"])
    if "any" in node:
        return any(evaluate_expr(c, task) for c in node["any"])
    field = node["field"]
    operator = node["operator"]
    value = node["value"]
    actual = task.get(field)
    if operator == "==":
        return json_eq(actual, value)
    if operator == "!=":
        return not json_eq(actual, value)
    if operator == "IN":
        return any(json_eq(actual, v) for v in value)
    if operator == "<=":
        return isinstance(actual, int) and not isinstance(actual, bool) and actual <= value
    if operator == ">=":
        return isinstance(actual, int) and not isinstance(actual, bool) and actual >= value
    raise ValueError(f"unsupported operator {operator!r}")


def match_routes(task: dict, policy: dict) -> frozenset[str]:
    """Evaluate every route's match expression independently against `task`."""
    return frozenset(r for r in ROUTES if evaluate_expr(policy["routes"][r]["match"], task))


def _route_has_family(route: str, family: str) -> bool:
    return any(step["family"] == family for step in CANONICAL_ROUTE_TOPOLOGY[route])


class NoAvailableRouteError(ValueError):
    """Every policy-valid candidate route was excluded by availability preflight."""

    def __init__(self, candidates: Iterable[str], unavailable_routes: Iterable[str]):
        self.candidates = sorted(candidates)
        self.unavailable_routes = list(unavailable_routes)
        super().__init__(
            f"no available policy-valid route: candidates {self.candidates} all excluded "
            f"by availability preflight {self.unavailable_routes}"
        )


def select_route(matched: Iterable[str], policy: dict, unavailable_routes: Iterable[str] | None = None) -> str:
    """Pick the earliest route in policy['precedence'] present in `matched`.

    Without exclusions, policy['fallback_route'] is selected when none of
    `matched` is in precedence (historical contract). With availability
    exclusions, selection is restricted to `matched`: the fallback route is
    never substituted for an excluded candidate, and exhausting every candidate
    raises NoAvailableRouteError so callers fail closed. This helper never
    substitutes a model inside a selected route.
    """
    matched_set = set(matched)
    unavailable = set(unavailable_routes or ())
    for r in policy["precedence"]:
        if r in matched_set and r not in unavailable:
            return r
    if not unavailable:
        return policy["fallback_route"]
    raise NoAvailableRouteError(matched_set, sorted(unavailable))


def route_task(task: dict, policy: dict) -> tuple[frozenset[str], str]:
    """Convenience wrapper: match every route, then select one."""
    matched = match_routes(task, policy)
    return matched, select_route(matched, policy)


# ---------------------------------------------------------------------------
# Risk-floor overrides (no-downgrade construction lives in resolve_route below)
# ---------------------------------------------------------------------------

def apply_risk_overrides(task: dict, risk_overrides: dict, task_schema: dict) -> dict:
    constrained = dict(task)
    for field in ORDERED_OVERRIDE_FIELDS:
        if field not in risk_overrides:
            continue
        if field == "ambiguity":
            constrained[field] = max(task[field], risk_overrides[field])
            continue
        enum = task_schema["properties"][field]["enum"]
        base_idx = enum.index(task[field])
        floor_idx = enum.index(risk_overrides[field])
        constrained[field] = enum[max(base_idx, floor_idx)]
    for field in SAFETY_OVERRIDE_FIELDS:
        if field not in risk_overrides:
            continue
        constrained[field] = bool(task[field]) or bool(risk_overrides[field])
    return constrained


def merge_risk_floors(floor_sets: Iterable[dict], task_schema: dict) -> dict:
    """Combine several risk-floor maps (project `risk_overrides`, matched
    `path_floors`) into one map of the same shape: each ordered field takes
    the strongest floor in its schema enum order (numeric max for ambiguity),
    and each safety boolean is true if any set forces it. Merging can only
    raise, so the result never weakens any input floor."""
    merged: dict = {}
    for floors in floor_sets:
        for field in ORDERED_OVERRIDE_FIELDS:
            if field not in floors:
                continue
            if field not in merged:
                merged[field] = floors[field]
            elif field == "ambiguity":
                merged[field] = max(merged[field], floors[field])
            else:
                enum = task_schema["properties"][field]["enum"]
                merged[field] = enum[max(enum.index(merged[field]), enum.index(floors[field]))]
        for field in SAFETY_OVERRIDE_FIELDS:
            if floors.get(field) is True:
                merged[field] = True
    return {field: merged[field] for field in (*ORDERED_OVERRIDE_FIELDS, *SAFETY_OVERRIDE_FIELDS) if field in merged}


# ---------------------------------------------------------------------------
# Path-triggered risk floors (`path_floors` in .pi/routing.yaml)
# ---------------------------------------------------------------------------
#
# Globs are repository-relative POSIX patterns, anchored at the repository
# root: `*` matches any characters inside one path segment (a leading dot
# included), `?` one character inside a segment, and a whole `**` segment
# zero or more segments (`migrations/**` matches `migrations` itself and
# everything below it; `**/*secret*` matches at any depth). The schema rejects
# every other construct (character classes, braces, backslashes, a leading
# `!` or `/`, empty, `.` or `..` segments, `**` inside a segment), so no
# pattern can mean a negation or a relaxation.
#
# Matching is case-sensitive, like Git's index on a case-sensitive file
# system. Both sides are compared in Unicode NFC, so a precomposed and a
# decomposed spelling of the same name match the same globs.

# Built-in floors, applied in every repository before the project's own
# `path_floors` and by the same matcher: agent and harness configuration
# (`.pi/` holds routing and autonomy policy, `.claude/` and `.mcp.json` hooks and
# MCP servers) and CI workflows (they run with repository secrets). A task or a
# diff touching them is security-sensitive. Recorded with `source: builtin`.
BUILTIN_PATH_FLOORS: tuple[dict, ...] = (
    {"paths": [".pi/**", ".claude/**", ".mcp.json", ".github/workflows/**"],
     "floors": {"security_sensitive": True}},
)
# Recorded paths longer than this are cut (with the number of characters cut),
# so a record stays schema-valid; matching always uses the full path.
RECORDED_PATH_MAX = 4096


def normalize_repo_path(value: Any) -> str | None:
    """A declared area or changed path as a repository-relative POSIX path in
    Unicode NFC, or None when it cannot name one (control characters,
    absolute, escaping the root, or using a backslash). Leading `./` and
    trailing `/` are dropped and repeated `/` collapsed."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value.strip())
    while text.startswith("./"):
        text = text[2:]
    segments = [segment for segment in text.split("/") if segment not in ("", ".")]
    if not segments or text.startswith("/") or ".." in segments:
        return None
    if any(re.search(r"[\x00-\x1f\x7f\\]", segment) for segment in segments):
        return None
    return "/".join(segments)


_GLOB_CACHE: dict[str, re.Pattern] = {}


def _glob_regex(pattern: str) -> re.Pattern:
    compiled = _GLOB_CACHE.get(pattern)
    if compiled is not None:
        return compiled
    segments = unicodedata.normalize("NFC", pattern).split("/")
    regex = ""
    need_separator = False
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            if last:
                regex += "(?:/.*)?" if need_separator else ".*"
            else:
                regex += "(?:/[^/]+)*" if need_separator else "(?:[^/]+/)*"
            continue
        body = "".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in segment)
        regex += ("/" if need_separator else "") + body
        need_separator = True
    compiled = re.compile(regex, re.DOTALL)
    _GLOB_CACHE[pattern] = compiled
    return compiled


def path_glob_matches(pattern: str, path: str) -> bool:
    """True when the schema-valid glob `pattern` matches the normalized `path`."""
    return _glob_regex(pattern).fullmatch(unicodedata.normalize("NFC", path)) is not None


def _fixed_prefix(pattern: str) -> list[str]:
    """The leading wildcard-free segments of a glob (`src/db/*.sql` -> `src`, `db`)."""
    prefix = []
    for segment in unicodedata.normalize("NFC", pattern).split("/"):
        if "*" in segment or "?" in segment:
            break
        prefix.append(segment)
    return prefix


def declared_area_matches(pattern: str, area: str) -> bool:
    """A declared `files_or_areas` entry touches a glob when the glob matches it,
    or when it is a directory that is an ancestor of (or equal to) the glob's
    wildcard-free prefix: declaring `src` touches `src/db/migrations/**`. A glob
    with no fixed prefix (`**/*secret*`) is only matched as a path, and the root
    never names an area (normalize_repo_path drops `.` and `/`)."""
    if path_glob_matches(pattern, area):
        return True
    prefix = _fixed_prefix(pattern)
    parts = area.split("/")
    return bool(prefix) and len(parts) <= len(prefix) and prefix[:len(parts)] == parts


def _recorded_path(path: str) -> str:
    if len(path) <= RECORDED_PATH_MAX:
        return path
    keep = RECORDED_PATH_MAX - 32
    return f"{path[:keep]}...(+{len(path) - keep} chars)"


def match_path_floors(paths: Iterable[Any], path_floors: Iterable[dict] | None, *,
                      source: str = "project", declared: bool = False) -> list[dict]:
    """Every rule of `path_floors` (in declared order) that at least one of
    `paths` matches: its `source` (`builtin` or `project`) and index, the
    patterns that matched, its floors, and the matched normalized paths
    (sorted, bounded, long ones cut) with their exact count. `declared` paths
    are `files_or_areas` entries (see `declared_area_matches`); otherwise they
    are real changed paths, matched as paths only. Values that are not
    repository-relative paths never match: the post-diff check refuses them
    before matching instead of dropping them."""
    normalized = sorted({path for path in (normalize_repo_path(value) for value in paths) if path is not None})
    matcher = declared_area_matches if declared else path_glob_matches
    matches = []
    for index, rule in enumerate(path_floors or ()):
        hit_patterns = []
        hit_paths: set[str] = set()
        for pattern in rule["paths"]:
            hits = [path for path in normalized if matcher(pattern, path)]
            if hits:
                hit_patterns.append(pattern)
                hit_paths.update(hits)
        if hit_paths:
            ordered = sorted(hit_paths)
            matches.append({
                "source": source,
                "rule": index,
                "patterns": hit_patterns,
                "floors": dict(rule["floors"]),
                "matched_paths": [_recorded_path(path) for path in ordered[:PATH_FLOOR_MATCHED_PATHS_LIMIT]],
                "matched_path_count": len(ordered),
            })
    return matches


def match_all_path_floors(paths: Iterable[Any], project_routing: dict | None, *, declared: bool = False) -> list[dict]:
    """Built-in floors, then the project's `path_floors`, matched against `paths`."""
    paths = list(paths)
    return [*match_path_floors(paths, BUILTIN_PATH_FLOORS, source="builtin", declared=declared),
            *match_path_floors(paths, (project_routing or {}).get("path_floors"), declared=declared)]


def project_risk_floors(task: dict, project_routing: dict | None, task_schema: dict, *,
                        diff_paths: Iterable[Any] = (), path_floors: bool = True) -> tuple[dict, list[dict]]:
    """The floors `route_task` applies to `task`: project-wide `risk_overrides`
    merged with every built-in and project path floor its `files_or_areas`
    touch, plus those matches (recorded in the route result). `diff_paths` are
    real changed paths, matched as paths and merged in as well (the post-diff
    check). `path_floors=False` leaves path floors out entirely (the
    classification benchmark's no-path-floor baseline)."""
    config = project_routing or {}
    matches: list[dict] = []
    extra: list[dict] = []
    if path_floors:
        matches = match_all_path_floors(task.get("files_or_areas") or [], config, declared=True)
        diff_paths = list(diff_paths)
        if diff_paths:
            extra = match_all_path_floors(diff_paths, config)
    floors = merge_risk_floors([config.get("risk_overrides") or {},
                                *(match["floors"] for match in [*matches, *extra])], task_schema)
    return floors, matches


# ---------------------------------------------------------------------------
# project-routing.schema.json <-> task.schema.json structural consistency
# ---------------------------------------------------------------------------

def validate_project_routing_schema(project_schema: dict, task_schema: dict, issues: list[ValidationIssue]) -> None:
    props = project_schema.get("properties", {})
    if project_schema.get("additionalProperties") is not False:
        issues.append(ValidationIssue(
            message="schemas/project-routing.schema.json must set additionalProperties: false"
        ))

    forbidden_present = FORBIDDEN_PROJECT_KEYS & set(props.keys())
    if forbidden_present:
        issues.append(ValidationIssue(
            message=(
                "schemas/project-routing.schema.json must not expose global-policy keys: "
                f"{sorted(forbidden_present)}"
            )
        ))

    risk_overrides = props.get("risk_overrides", {})
    if risk_overrides.get("additionalProperties") is not False:
        issues.append(ValidationIssue(
            message="schemas/project-routing.schema.json risk_overrides must set additionalProperties: false"
        ))

    ro_props = risk_overrides.get("properties", {})
    expected_fields = set(ORDERED_OVERRIDE_FIELDS) | set(SAFETY_OVERRIDE_FIELDS)
    if set(ro_props.keys()) != expected_fields:
        issues.append(ValidationIssue(
            message=(
                "schemas/project-routing.schema.json risk_overrides fields must be exactly "
                f"{sorted(expected_fields)}, got {sorted(ro_props.keys())}"
            )
        ))

    for field in ORDERED_OVERRIDE_FIELDS:
        if field not in ro_props:
            continue
        if field == "ambiguity":
            actual = ro_props[field]
            expected = {key: task_schema["properties"][field][key] for key in ("type", "minimum", "maximum")}
            if {key: actual.get(key) for key in expected} != expected:
                issues.append(ValidationIssue(message="schemas/project-routing.schema.json risk_overrides.ambiguity must match task score range 0..10"))
            continue
        expected_enum = task_schema["properties"][field]["enum"]
        actual_enum = ro_props[field].get("enum")
        if actual_enum != expected_enum:
            issues.append(ValidationIssue(
                message=(
                    f"schemas/project-routing.schema.json risk_overrides.{field} enum {actual_enum!r} "
                    f"must match schemas/task.schema.json properties.{field} enum {expected_enum!r}"
                )
            ))

    for field in SAFETY_OVERRIDE_FIELDS:
        if field not in ro_props:
            continue
        if ro_props[field].get("enum") != [True]:
            issues.append(ValidationIssue(
                message=(
                    f"schemas/project-routing.schema.json risk_overrides.{field} must be restricted to "
                    "enum: [true] (safety booleans may only be forced true)"
                )
            ))

    # path_floors reuse the risk_overrides definition verbatim, so a path rule
    # can never accept a value (or a key) a project-wide floor would reject.
    path_floors = props.get("path_floors", {})
    rule = path_floors.get("items", {}) if isinstance(path_floors, dict) else {}
    floors = rule.get("properties", {}).get("floors", {}) if isinstance(rule, dict) else {}
    if (path_floors.get("type") != "array" or rule.get("additionalProperties") is not False
            or set(rule.get("required", [])) != {"paths", "floors"}
            or set(rule.get("properties", {})) != {"paths", "floors"}
            or floors.get("$ref") != "#/properties/risk_overrides"):
        issues.append(ValidationIssue(
            message=(
                "schemas/project-routing.schema.json path_floors must be an array of closed "
                "{paths, floors} rules whose floors $ref #/properties/risk_overrides"
            )
        ))


# ---------------------------------------------------------------------------
# JSON Schema instance validation (normalized issues, for tasks/configs)
# ---------------------------------------------------------------------------

def _format_json_path(absolute_path) -> str:
    parts = []
    for p in absolute_path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            parts.append(f".{p}")
    return "".join(parts)


def validate_instance(instance: Any, schema: dict, *, label: str) -> list[ValidationIssue]:
    validator = jsonschema.Draft202012Validator(schema)
    issues = []
    for e in sorted(validator.iter_errors(instance), key=str):
        issues.append(ValidationIssue(path=f"{label}{_format_json_path(e.absolute_path)}", message=e.message))
    return issues


AMBIGUITY_DIMENSIONS = ("outcome", "contract", "design", "evidence", "authority")


def validate_task_instance(task: Any, task_schema: dict, *, allow_ambiguity_floor: bool = False) -> list[ValidationIssue]:
    issues = validate_instance(task, task_schema, label="classified_task")
    if issues or not isinstance(task, dict) or "ambiguity_assessment" not in task:
        return issues
    assessment = task["ambiguity_assessment"]
    derived = max(assessment[axis]["level"] for axis in AMBIGUITY_DIMENSIONS)
    observed = task["ambiguity"]
    consistent = observed >= derived if allow_ambiguity_floor else observed == derived
    if not consistent:
        relation = "at least" if allow_ambiguity_floor else "equal"
        issues.append(ValidationIssue(
            path="classified_task.ambiguity",
            message=f"ambiguity must {relation} highest ambiguity_assessment dimension {derived!r}; got {observed!r}",
        ))
    if assessment["authority"]["level"] >= 7 and task["implementation_required"]:
        issues.append(ValidationIssue(
            path="classified_task.ambiguity_assessment.authority",
            message=("AUTHORITY_DECISION_REQUIRED: an owner-only choice remains; "
                     "route a read-only investigation or obtain the owner decision before mutating work"),
        ))
    return issues


def missing_escalation_evidence(task: dict) -> list[str]:
    """Return, in ESCALATING_CLASSIFICATION_VALUES order, every field whose raw
    classified value escalates risk but has no non-empty
    `escalation_evidence[<field>]` justification. Call this on the classification
    as submitted -- never on the floored effective classification -- so values
    raised only by project risk floors stay exempt."""
    evidence = task.get("escalation_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    missing = []
    for field, escalating_value in ESCALATING_CLASSIFICATION_VALUES.items():
        escalates = (isinstance(task.get(field), int) and not isinstance(task.get(field), bool)
                     and task[field] >= escalating_value) if field == "ambiguity" else json_eq(task.get(field), escalating_value)
        if not escalates:
            continue
        entry = evidence.get(field)
        if not isinstance(entry, str) or not entry.strip():
            missing.append(field)
    return missing


def classification_evidence_issues(task: dict) -> list[ValidationIssue]:
    """Normalized CLASSIFICATION_EVIDENCE_MISSING issues for `task`."""
    return [
        ValidationIssue(
            path=f"classified_task.escalation_evidence.{field}",
            message=(
                f"{CLASSIFICATION_EVIDENCE_MISSING}: {field}{'>=' if field == 'ambiguity' else '='}{json.dumps(ESCALATING_CLASSIFICATION_VALUES[field])} "
                "escalates routing but has no escalation_evidence entry; add a one-line task-specific "
                "justification or lower the field to its low-risk value"
            ),
        )
        for field in missing_escalation_evidence(task)
    ]


def _json_pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _non_string_key_issues(value: Any, label: str) -> list[ValidationIssue]:
    """YAML can produce Python mappings that are not valid JSON objects, such
    as integer keys. JSON Schema validation assumes JSON data, so reject these
    explicitly before schema validation/JSON serialization."""
    issues: list[ValidationIssue] = []
    if isinstance(value, dict):
        seen_stringified: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                issues.append(ValidationIssue(
                    path=label,
                    message=f"{label}: mapping key {key!r} is not a string; YAML project routing must be JSON-compatible",
                ))
            string_key = str(key)
            if string_key in seen_stringified and seen_stringified[string_key] != key:
                issues.append(ValidationIssue(
                    path=label,
                    message=(
                        f"{label}: mapping keys {seen_stringified[string_key]!r} and {key!r} collide "
                        "when represented as JSON object keys"
                    ),
                ))
            else:
                seen_stringified[string_key] = key
            next_label = f"{label}/{_json_pointer_escape(string_key)}"
            issues.extend(_non_string_key_issues(child, next_label))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            issues.extend(_non_string_key_issues(child, f"{label}/{idx}"))
    return issues


def validate_project_routing_instance(config: Any, project_schema: dict) -> list[ValidationIssue]:
    issues = _non_string_key_issues(config, "project_routing")
    issues.extend(validate_instance(config, project_schema, label="project_routing"))
    return issues


# ---------------------------------------------------------------------------
# Repository policy assets: loaded once, structurally validated
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyAssets:
    repo_root: Path
    task_schema: dict
    project_schema: dict
    execution_plan_schema: dict
    delegation_schema: dict
    policy: dict
    delegation_policy: dict
    field_info: dict


def load_policy_assets(repo_root: Path, model_config_path: Path | None = None) -> PolicyAssets:
    """Load and structurally validate the task/project/delegation schemas,
    top-level routing policy, nested delegation policy, and every embedded
    execution plan. Raises RoutingAssetError (a bootstrap failure,
    not a task-validation failure) if any of these repository assets is
    missing, malformed, or internally inconsistent."""
    task_schema_path = repo_root / "schemas" / "task.schema.json"
    project_schema_path = repo_root / "schemas" / "project-routing.schema.json"
    execution_plan_schema_path = repo_root / "schemas" / "execution-plan.schema.json"
    delegation_schema_path = repo_root / "schemas" / "delegation-policy.schema.json"
    policy_path = repo_root / "routing" / "default-policy.yaml"
    delegation_policy_path = repo_root / "routing" / "delegation-policy.yaml"
    model_runtime_schema_path = repo_root / "schemas" / "model-runtime.schema.json"

    for path in (
        task_schema_path,
        project_schema_path,
        execution_plan_schema_path,
        delegation_schema_path,
        policy_path,
        delegation_policy_path,
        model_runtime_schema_path,
    ):
        if not path.exists():
            raise RoutingAssetError(f"missing required asset: {path}")

    try:
        task_schema = load_json(task_schema_path)
        jsonschema.Draft202012Validator.check_schema(task_schema)
    except Exception as exc:  # noqa: BLE001
        raise RoutingAssetError(f"{task_schema_path} is not a valid JSON Schema: {exc}") from exc

    try:
        project_schema = load_json(project_schema_path)
        jsonschema.Draft202012Validator.check_schema(project_schema)
    except Exception as exc:  # noqa: BLE001
        raise RoutingAssetError(f"{project_schema_path} is not a valid JSON Schema: {exc}") from exc

    try:
        execution_plan_schema = load_execution_plan_schema(repo_root)
    except Exception as exc:  # noqa: BLE001
        raise RoutingAssetError(f"{execution_plan_schema_path} is not a valid JSON Schema: {exc}") from exc

    try:
        delegation_schema = load_json(delegation_schema_path)
        jsonschema.Draft202012Validator.check_schema(delegation_schema)
    except Exception as exc:  # noqa: BLE001
        raise RoutingAssetError(f"{delegation_schema_path} is not a valid JSON Schema: {exc}") from exc

    try:
        policy = load_yaml(policy_path)
    except yaml.YAMLError as exc:
        raise RoutingAssetError(f"{policy_path} is not valid YAML: {exc}") from exc

    model_config = load_model_runtime_config(repo_root, model_config_path)
    policy = apply_model_runtime_config(policy, model_config)

    try:
        delegation_policy = load_yaml(delegation_policy_path)
    except yaml.YAMLError as exc:
        raise RoutingAssetError(f"{delegation_policy_path} is not valid YAML: {exc}") from exc

    field_info = build_field_info(task_schema)

    issues: list[ValidationIssue] = []
    validate_policy_structure(policy, field_info, execution_plan_schema, issues)
    if not issues:
        # Fail closed before routing if a configured Claude model cannot be
        # translated to the backend model identifier expected by Claude Code.
        load_backend_compatibility(repo_root, model_config=model_config, effective_policy=policy)
    validate_delegation_policy(delegation_policy, delegation_schema, policy, issues)
    validate_project_routing_schema(project_schema, task_schema, issues)
    if issues:
        joined = "; ".join(str(i) for i in issues)
        raise RoutingAssetError(f"routing/delegation policy assets are structurally invalid: {joined}")

    return PolicyAssets(
        repo_root=repo_root,
        task_schema=task_schema,
        project_schema=project_schema,
        execution_plan_schema=execution_plan_schema,
        delegation_schema=delegation_schema,
        policy=policy,
        delegation_policy=delegation_policy,
        field_info=field_info,
    )


# ---------------------------------------------------------------------------
# No-downgrade route resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteResolution:
    base_classification: dict
    effective_classification: dict
    base_matches: frozenset[str]
    effective_matches: frozenset[str]
    base_route: str
    selected_route: str
    fallback_applied: bool
    unavailable_routes: tuple[str, ...] = ()
    route_availability: dict | None = None
    path_floor_matches: tuple[dict, ...] = ()


def resolve_route(
    task: dict,
    policy: dict,
    project_routing: dict | None,
    task_schema: dict,
    unavailable_routes: Iterable[str] | None = None,
    *,
    diff_paths: Iterable[Any] = (),
    path_floors: bool = True,
) -> RouteResolution:
    """Route `task` under the global policy, then apply project risk floors.

    Project risk floors are the project-wide `risk_overrides` merged with every
    built-in and project path floor the task's `files_or_areas` touch (see
    `project_risk_floors`); the matches are kept on the resolution so the
    result records them and `validate_route_task_result` recomputes them.
    `diff_paths` (the post-diff check) adds the floors a worker's real changed
    paths match; `path_floors=False` is the benchmark's no-path-floor baseline.

    Availability-aware callers may provide routes that were observed to be
    unavailable during candidate preflight. Those route identities are excluded
    before the final route is committed, so execution receives a coherent policy
    route/plan/bindings tuple rather than a silently substituted model inside a
    selected route. Raises NoAvailableRouteError when no policy-valid candidate
    remains; unknown route names raise ValueError.
    """
    observed_unavailable: tuple[str, ...] = ()
    for route in unavailable_routes or ():
        if route not in ROUTES:
            raise ValueError(f"unknown unavailable route {route!r}")
        if route not in observed_unavailable:
            observed_unavailable += (route,)
    base_matched = match_routes(task, policy)
    base_route = select_route(base_matched, policy)

    risk_floors, path_floor_matches = project_risk_floors(task, project_routing, task_schema,
                                                          diff_paths=diff_paths, path_floors=path_floors)
    effective_task = apply_risk_overrides(task, risk_floors, task_schema) if risk_floors else dict(task)
    effective_matched = match_routes(effective_task, policy) if risk_floors else base_matched

    union_matched = base_matched | effective_matched
    candidates = union_matched | {base_route}
    selected_route = select_route(candidates, policy)
    if observed_unavailable:
        # Availability can only re-select among routes the floored (effective)
        # classification itself matches, plus the unconstrained selection. A
        # base-only match below a project risk floor, or a policy fallback the
        # task never matched, is never substituted for an unavailable route.
        # It also never drops Codex verification: if the unconstrained selection
        # includes a codex step, a replacement must include one too.
        availability_candidates = {
            route for route in set(effective_matched) | {selected_route}
            if _route_has_family(route, "codex") or not _route_has_family(selected_route, "codex")
        }
        selected_route = select_route(availability_candidates, policy, observed_unavailable)
    fallback_applied = selected_route not in union_matched
    availability = None
    if observed_unavailable:
        availability = {
            "excluded_routes": list(observed_unavailable),
            "selection_reason": "candidate route(s) excluded by pre-selection backend availability preflight",
        }

    return RouteResolution(
        base_classification=task,
        effective_classification=effective_task,
        base_matches=base_matched,
        effective_matches=effective_matched,
        base_route=base_route,
        selected_route=selected_route,
        fallback_applied=fallback_applied,
        unavailable_routes=observed_unavailable,
        route_availability=availability,
        path_floor_matches=tuple(path_floor_matches),
    )


def validate_route_task_result(result: Any, assets: PolicyAssets) -> list[ValidationIssue]:
    """Validate the runtime result envelope at adapter boundaries. This is the
    shared semantic check used by the TypeScript extension via
    tools/validate_route_result.py, so plan semantics are not reimplemented in
    TypeScript."""
    issues: list[ValidationIssue] = []
    if not isinstance(result, dict):
        return [ValidationIssue(path="result", message="route_task result must be an object")]
    if result.get("ok") is not True:
        return []

    forbidden_telemetry_keys = sorted({"routing_telemetry", "telemetry"} & set(result))
    if forbidden_telemetry_keys:
        issues.append(ValidationIssue(
            path="result",
            message=("route_task results must not contain telemetry keys; observational telemetry "
                     "belongs only in provenance run records"),
        ))

    if result.get("policy_version") != assets.policy.get("version"):
        issues.append(ValidationIssue(
            path="result.policy_version",
            message="ok:true result policy_version must equal routing/default-policy.yaml version",
        ))

    selected_route = result.get("selected_route")
    if selected_route not in ROUTES:
        issues.append(ValidationIssue(path="result.selected_route", message="ok:true result must include a known selected_route"))

    base_classification = None
    effective_classification = None
    expected_resolution = None
    classification = result.get("classification")
    if not isinstance(classification, dict) or not isinstance(classification.get("base"), dict):
        issues.append(ValidationIssue(
            path="result.classification.base",
            message="ok:true result must include base classification for deterministic route validation",
        ))
    else:
        base_classification = classification["base"]
        issues.extend(validate_task_instance(base_classification, assets.task_schema))

    if not isinstance(classification, dict) or not isinstance(classification.get("effective"), dict):
        issues.append(ValidationIssue(
            path="result.classification.effective",
            message="ok:true result must include effective classification for execution-plan parity validation",
        ))
    else:
        effective_classification = classification["effective"]
        issues.extend(validate_task_instance(effective_classification, assets.task_schema, allow_ambiguity_floor=True))

    project_constraints = result.get("project_constraints", {})
    project_issues = validate_project_routing_instance(project_constraints, assets.project_schema)
    issues.extend(project_issues)

    route_availability = result.get("route_availability")
    unavailable_routes: list[str] = []
    if route_availability is not None:
        if not isinstance(route_availability, dict):
            issues.append(ValidationIssue(path="result.route_availability", message="route_availability must be an object"))
        else:
            excluded = route_availability.get("excluded_routes")
            if not isinstance(excluded, list) or not all(isinstance(item, str) and item in ROUTES for item in excluded):
                issues.append(ValidationIssue(
                    path="result.route_availability.excluded_routes",
                    message="excluded_routes must be a list of known route names",
                ))
            else:
                unavailable_routes = excluded

    if base_classification is not None and not project_issues:
        try:
            expected_resolution = resolve_route(
                base_classification,
                assets.policy,
                project_constraints,
                assets.task_schema,
                unavailable_routes,
            )
            if result.get("selected_route") != expected_resolution.selected_route:
                issues.append(ValidationIssue(
                    path="result.selected_route",
                    message="selected_route must equal deterministic routing of classification.base plus project_constraints and any pre-selection availability exclusions",
                ))
            if not json_deep_eq(effective_classification, expected_resolution.effective_classification):
                issues.append(ValidationIssue(
                    path="result.classification.effective",
                    message="effective classification must equal classification.base after project risk floors",
                ))
            if not json_deep_eq(result.get("path_floor_matches"), list(expected_resolution.path_floor_matches)):
                issues.append(ValidationIssue(
                    path="result.path_floor_matches",
                    message="path_floor_matches must equal the project path_floors matched by classification.base files_or_areas",
                ))
            expected_matches = {
                "base": sorted(expected_resolution.base_matches),
                "effective": sorted(expected_resolution.effective_matches),
            }
            if not json_deep_eq(result.get("matches"), expected_matches):
                issues.append(ValidationIssue(
                    path="result.matches",
                    message="matches must equal deterministic route matches for base and effective classifications",
                ))
            if not json_deep_eq(result.get("fallback_applied"), expected_resolution.fallback_applied):
                issues.append(ValidationIssue(
                    path="result.fallback_applied",
                    message="fallback_applied must equal deterministic route resolution",
                ))
            if not json_deep_eq(route_availability, expected_resolution.route_availability):
                issues.append(ValidationIssue(
                    path="result.route_availability",
                    message="route_availability must equal deterministic pre-selection availability exclusions",
                ))
        except Exception as exc:  # noqa: BLE001
            issues.append(ValidationIssue(path="result.selected_route", message=f"could not validate deterministic route result: {exc}"))

    plan = result.get("execution_plan")
    expected_capacity_assignments = None
    if plan is None:
        issues.append(ValidationIssue(path="result.execution_plan", message="ok:true result must include execution_plan"))
    else:
        issues.extend(validate_execution_plan(plan, assets.execution_plan_schema, label="result.execution_plan"))
        if selected_route in ROUTES and effective_classification is not None:
            try:
                route_for_plan = expected_resolution.selected_route if expected_resolution is not None else selected_route
                intent_source = expected_resolution.effective_classification if expected_resolution is not None else effective_classification
                primary_intent = derive_primary_intent(intent_source)
                expected_plan = materialize_execution_plan(
                    assets.policy, assets.execution_plan_schema, route_for_plan, primary_intent
                )
                if not json_deep_eq(plan, expected_plan):
                    issues.append(ValidationIssue(
                        path="result.execution_plan",
                        message="execution_plan must equal the canonical authored policy plan for the deterministic selected route and derived primary intent",
                    ))
                expected_capacity_assignments = assign_capacities(assets.policy, expected_plan, intent_source)
            except Exception as exc:  # noqa: BLE001
                issues.append(ValidationIssue(path="result.execution_plan", message=f"could not validate route/intent plan parity: {exc}"))

    capacity_assignments = result.get("capacity_assignments")
    if expected_capacity_assignments is None:
        if not isinstance(capacity_assignments, list):
            issues.append(ValidationIssue(
                path="result.capacity_assignments",
                message="ok:true result must include capacity_assignments",
            ))
    elif not json_deep_eq(capacity_assignments, expected_capacity_assignments):
        issues.append(ValidationIssue(
            path="result.capacity_assignments",
            message=("capacity_assignments must equal post-route policy assignment for every canonical "
                     "execution-plan step using classification.effective"),
        ))

    bindings = result.get("backend_bindings")
    if not isinstance(bindings, dict):
        issues.append(ValidationIssue(path="result.backend_bindings", message="ok:true result must include backend_bindings"))
    elif isinstance(plan, dict) and expected_capacity_assignments is not None:
        try:
            expected = resolve_backend_bindings(assets.policy, plan, expected_capacity_assignments)
            if not json_deep_eq(bindings, expected):
                issues.append(ValidationIssue(
                    path="result.backend_bindings",
                    message="backend_bindings must equal policy backends resolved for only the assigned capacity tiers",
                ))
        except Exception as exc:  # noqa: BLE001
            issues.append(ValidationIssue(path="result.backend_bindings", message=f"could not validate backend_bindings: {exc}"))

    return issues


def build_result(
    resolution: RouteResolution,
    policy: dict,
    project_routing: dict | None,
    execution_plan_schema: dict,
) -> dict:
    """Construct the deterministic runtime result. Key order and list
    ordering (routes sorted alphabetically) are fixed for reproducibility.
    The primary execution intent is derived centrally from the effective
    classification (execution materialization, not route selection); the
    resulting plan is deep-copied and revalidated before being returned.
    Capacity is then assigned from the effective classification without
    mutating the plan, and backend/tool/model bindings are resolved only for
    those assigned tiers."""
    selected = resolution.selected_route
    primary_intent = derive_primary_intent(resolution.effective_classification)
    execution_plan = materialize_execution_plan(policy, execution_plan_schema, selected, primary_intent)
    capacity_assignments = assign_capacities(policy, execution_plan, resolution.effective_classification)
    backend_bindings = resolve_backend_bindings(policy, execution_plan, capacity_assignments)
    result = {
        "ok": True,
        "policy_version": policy["version"],
        "classification": {
            "base": resolution.base_classification,
            "effective": resolution.effective_classification,
        },
        "matches": {
            "base": sorted(resolution.base_matches),
            "effective": sorted(resolution.effective_matches),
        },
        "fallback_applied": resolution.fallback_applied,
        "path_floor_matches": [copy.deepcopy(match) for match in resolution.path_floor_matches],
        "selected_route": selected,
        "execution_plan": execution_plan,
        "capacity_assignments": capacity_assignments,
        "backend_bindings": backend_bindings,
        "project_constraints": project_routing or {},
    }
    if resolution.route_availability is not None:
        result["route_availability"] = resolution.route_availability
    return result


# ---------------------------------------------------------------------------
# Post-diff classification check (path floors against a worker's real diff)
# ---------------------------------------------------------------------------

def _decision_view(route: str, capacity_assignments: list[dict]) -> dict:
    return {
        "route": route,
        "capacities": {entry["step_id"]: entry["assigned_capacity_tier"] for entry in capacity_assignments},
        "reasoning_efforts": {entry["step_id"]: entry.get("assigned_reasoning_effort") for entry in capacity_assignments},
    }


def check_diff_path_floors(route_result: Any, changed_paths: Iterable[Any], assets: PolicyAssets) -> dict:
    """Re-route the task that ran with its real changed paths' floors added,
    under the same project constraints and availability exclusions recorded in
    `route_result`, and compare.

    `route_result` must be a valid `route_task` result
    (`validate_route_task_result`); its `project_constraints` are the floors
    that governed the run, so a worker that edits `.pi/routing.yaml` in its
    worktree cannot remove a floor. Built-in floors always apply. The result
    is `blocked` with CLASSIFICATION_UNDERRATED_BY_DIFF when the diff-floored
    decision selects a route earlier in the policy `precedence` (a tie-break
    order, not a risk ranking) than the one that ran, or the same route with a
    strictly higher capacity tier or reasoning effort for any step. A diff
    that matches no floor reproduces the routed inputs exactly and can never
    block. Anything that cannot be evaluated returns `ok: false` with
    CLASSIFICATION_DIFF_UNVERIFIABLE (fail closed), including a changed path
    that is not a repository-relative path (control characters, a backslash):
    such a path is never dropped."""
    def unverifiable(message: str, path: str = "route_task_result") -> dict:
        return {"ok": False, "blocked": True, "blocker_code": CLASSIFICATION_DIFF_UNVERIFIABLE,
                "errors": [{"path": path, "message": message}]}

    if not isinstance(route_result, dict) or route_result.get("ok") is not True:
        return unverifiable("the route result is not an ok:true route_task result")
    raw = list(changed_paths)
    unusable = [value for value in raw if normalize_repo_path(value) is None]
    if unusable:
        return unverifiable(f"{len(unusable)} changed path(s) are not repository-relative paths "
                            "(control characters, a backslash, absolute or escaping the root)", "changed_paths")
    issues = validate_route_task_result(route_result, assets)
    if issues:
        return unverifiable("; ".join(str(issue) for issue in issues[:5]))
    base = route_result["classification"]["base"]
    project_constraints = route_result.get("project_constraints") or {}
    excluded = (route_result.get("route_availability") or {}).get("excluded_routes") or []
    changed = sorted({normalize_repo_path(value) for value in raw})
    diff_matches = match_all_path_floors(changed, project_constraints)
    ran = _decision_view(route_result["selected_route"], route_result["capacity_assignments"])
    result = {
        "ok": True,
        "blocked": False,
        "blocker_code": None,
        "task_id": base["id"],
        "changed_path_count": len(changed),
        "diff_path_floor_matches": diff_matches,
        "ran": ran,
        "required": ran,
        "route_raised": False,
        "capacity_raised": False,
    }
    if not diff_matches:
        return result
    try:
        resolution = resolve_route(base, assets.policy, project_constraints, assets.task_schema, excluded,
                                   diff_paths=changed)
    except NoAvailableRouteError as exc:
        # Every route the diff's floors allow is unavailable: still underrated, never integrated as is.
        result.update(blocked=True, blocker_code=CLASSIFICATION_UNDERRATED_BY_DIFF, required=None,
                      route_raised=True, errors=[{"path": "route_availability", "message": str(exc)}])
        return result
    intent = derive_primary_intent(resolution.effective_classification)
    plan = materialize_execution_plan(assets.policy, assets.execution_plan_schema, resolution.selected_route, intent)
    required = _decision_view(resolution.selected_route,
                              assign_capacities(assets.policy, plan, resolution.effective_classification))
    precedence = assets.policy["precedence"]
    route_raised = precedence.index(required["route"]) < precedence.index(ran["route"])
    capacity_raised = required["route"] == ran["route"] and any(
        CAPACITY_TIER_RANK[required["capacities"][step]] > CAPACITY_TIER_RANK[tier]
        or REASONING_EFFORT_ORDER.get(required["reasoning_efforts"][step], -1)
        > REASONING_EFFORT_ORDER.get(ran["reasoning_efforts"][step], -1)
        for step, tier in ran["capacities"].items() if step in required["capacities"]
    )
    result.update(required=required, route_raised=route_raised, capacity_raised=capacity_raised)
    if route_raised or capacity_raised:
        result.update(blocked=True, blocker_code=CLASSIFICATION_UNDERRATED_BY_DIFF)
    return result


# Routing-summary signals (orchestration-lifecycle routingSummary) per floored boolean field.
_SUMMARY_SIGNALS = {
    "cross_layer": "cross_layer",
    "assumptions_must_be_explicit": "assumptions_must_be_explicit",
    "production_feature": "production_feature",
    "security_sensitive": "security_sensitive",
    "destructive_operation": "destructive_operation",
}


def summary_satisfies_floors(summary: Any, floors: dict, task_schema: dict) -> bool:
    """Whether a recorded phase routing summary (the run record's
    `phases[].routing`: the effective classification's risk fields and
    signals) is already at or above every floor in `floors`. When it is,
    applying the floors again changes no field, so routing and capacity are
    unchanged. A field the summary does not carry exactly
    (`behavior_preservation` below `required`) counts as unsatisfied."""
    if not isinstance(summary, dict):
        return False
    signals = summary.get("signals") if isinstance(summary.get("signals"), list) else []
    for field, floor in floors.items():
        if field == "ambiguity":
            value = summary.get("ambiguity")
            if not isinstance(value, int) or isinstance(value, bool) or value < floor:
                return False
        elif field == "behavior_preservation":
            if floor != "not_required" and "behavior_preservation_required" not in signals:
                return False
        elif field in ORDERED_OVERRIDE_FIELDS:
            enum = task_schema["properties"][field]["enum"]
            if summary.get(field) not in enum or enum.index(summary[field]) < enum.index(floor):
                return False
        elif field == "diagnosis_required":
            if summary.get("diagnosis_required") is not True:
                return False
        elif _SUMMARY_SIGNALS.get(field) not in signals:
            return False
    return True


def check_recorded_path_floors(changed_paths: Iterable[Any], project_routing: dict | None,
                               summaries: Iterable[Any], task_schema: dict) -> dict:
    """The post-diff check where only the run record is reachable (workspace
    validation and publication): a commit's changed paths against the built-in
    floors and `project_routing` (read from the pinned base, never the work),
    compared with the routing summaries the child run recorded. No floor
    matched: passes. Some recorded summary already satisfies every matched
    floor: passes (re-routing would change nothing). Otherwise
    CLASSIFICATION_UNDERRATED_BY_DIFF; with no usable summary, or a changed
    path that is not a repository-relative path,
    CLASSIFICATION_DIFF_UNVERIFIABLE."""
    raw = list(changed_paths)
    if any(normalize_repo_path(value) is None for value in raw):
        return {"ok": False, "blocked": True, "blocker_code": CLASSIFICATION_DIFF_UNVERIFIABLE,
                "diff_path_floor_matches": [], "reason": "a changed path is not a repository-relative path"}
    matches = match_all_path_floors(raw, project_routing)
    result = {"ok": True, "blocked": False, "blocker_code": None, "diff_path_floor_matches": matches}
    if not matches:
        return result
    floors = merge_risk_floors([match["floors"] for match in matches], task_schema)
    usable = [summary for summary in summaries if isinstance(summary, dict)]
    if not usable:
        return {**result, "ok": False, "blocked": True, "blocker_code": CLASSIFICATION_DIFF_UNVERIFIABLE,
                "reason": "the child run recorded no routing decision to compare the floors with"}
    if any(summary_satisfies_floors(summary, floors, task_schema) for summary in usable):
        return result
    return {**result, "blocked": True, "blocker_code": CLASSIFICATION_UNDERRATED_BY_DIFF,
            "reason": "no recorded routing decision meets the floors the commit's paths require"}
