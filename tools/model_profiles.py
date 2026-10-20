#!/usr/bin/env python3
"""Manage user-owned model/runtime bindings and ambiguity assessments.

This tool may change concrete model identifiers and declared ambiguity capacity
bound to existing family/capacity tiers. It never changes routes, risk floors, capacity-assignment
rules, execution topology, mutation permissions, or review requirements.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import atomic_io  # noqa: E402
import routing_engine as engine  # noqa: E402

FAMILIES = ("codex", "claude")
TIERS = engine.CAPACITY_TIER_ORDER


def default_config_path() -> Path:
    explicit = os.environ.get(engine.MODEL_RUNTIME_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise engine.RoutingAssetError(f"{engine.MODEL_RUNTIME_ENV} must be an absolute path")
        return path
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return (config_home / "pi-coding-orchestrator" / "models.yaml").resolve()


def _base_policy() -> dict:
    return engine.load_yaml(ROOT / "routing" / "default-policy.yaml")


def _load_raw(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1}
    config = engine.load_model_runtime_config(ROOT, path)
    assert config is not None
    return copy.deepcopy(config)


def _effective(config: dict | None) -> tuple[dict, dict]:
    policy = engine.apply_model_runtime_config(_base_policy(), config)
    issues: list[engine.ValidationIssue] = []
    execution_schema = engine.load_execution_plan_schema(ROOT)
    task_schema = engine.load_json(ROOT / "schemas" / "task.schema.json")
    engine.validate_policy_structure(policy, engine.build_field_info(task_schema), execution_schema, issues)
    if issues:
        raise engine.RoutingAssetError("; ".join(str(issue) for issue in issues))
    compatibility = engine.load_backend_compatibility(
        ROOT,
        model_config=config,
        effective_policy=policy,
    )
    return policy, compatibility


def _summary(path: Path, config: dict | None) -> dict:
    policy, compatibility = _effective(config)
    models = {
        family: {
            tier: policy["model_profiles"][family]["capacity_models"][tier]["model"]
            for tier in TIERS
        }
        for family in FAMILIES
    }
    ambiguity_capacities = {
        family: {
            tier: policy["model_profiles"][family]["capacity_models"][tier]["ambiguity_capacity"]
            for tier in TIERS
        }
        for family in FAMILIES
    }
    claude_models = set(models["claude"].values())
    return {
        "schema_version": 1,
        "config_path": str(path),
        "configured": path.exists(),
        "models": models,
        "ambiguity_capacities": ambiguity_capacities,
        "claude_code": {
            "model_ids": {
                model: compatibility["claude_code_model_ids"][model]
                for model in sorted(claude_models)
            },
            "minimum_versions": {
                model: compatibility["claude_code_minimum_versions"][model]
                for model in sorted(claude_models)
                if model in compatibility["claude_code_minimum_versions"]
            },
        },
    }


def _cleanup(config: dict) -> dict:
    result = copy.deepcopy(config)
    for key in ("models", "claude_code"):
        value = result.get(key)
        if isinstance(value, dict):
            for nested in list(value):
                if isinstance(value[nested], dict) and not value[nested]:
                    del value[nested]
            if not value:
                result.pop(key, None)
    return result


def _write(path: Path, config: dict) -> None:
    config = _cleanup(config)
    if set(config) == {"schema_version"}:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    def validate(temp: Path) -> None:
        # Validate the exact serialized candidate, then validate its effective
        # policy/backend contract, before the atomic replacement.
        validated = engine.load_model_runtime_config(ROOT, temp)
        assert validated is not None
        _effective(validated)

    atomic_io.atomic_write_text(path, yaml.safe_dump(config, sort_keys=False), validate=validate)


def cmd_show(path: Path) -> int:
    config = _load_raw(path) if path.exists() else None
    print(json.dumps(_summary(path, config), indent=2, sort_keys=True))
    return 0


def cmd_validate(path: Path) -> int:
    config = _load_raw(path) if path.exists() else None
    summary = _summary(path, config)
    print(json.dumps({"ok": True, **summary}, indent=2, sort_keys=True))
    return 0


def cmd_set(path: Path, family: str, tier: str, model: str,
            claude_code_id: str | None, min_version: str | None,
            ambiguity_capacity: int | None = None) -> int:
    config = _load_raw(path)
    if family == "codex" and (claude_code_id is not None or min_version is not None):
        raise ValueError("Claude backend options are only valid for family=claude")

    if ambiguity_capacity is None:
        base_entry = _base_policy()["model_profiles"][family]["capacity_models"][tier]
        if model != base_entry["model"]:
            raise ValueError("new model bindings require --ambiguity-capacity 0..10")
        ambiguity_capacity = base_entry["ambiguity_capacity"]
    if isinstance(ambiguity_capacity, bool) or not isinstance(ambiguity_capacity, int) or not 0 <= ambiguity_capacity <= 10:
        raise ValueError("ambiguity capacity must be an integer from 0 to 10")
    if ambiguity_capacity < engine.TIER_AMBIGUITY_FLOORS[tier]:
        raise ValueError(f"{tier} tier requires at least {engine.TIER_AMBIGUITY_FLOORS[tier]} ambiguity capacity")
    config.setdefault("models", {}).setdefault(family, {})[tier] = {
        "model": model, "ambiguity_capacity": ambiguity_capacity,
    }

    if family == "claude":
        base_rules = engine.load_json(ROOT / "routing" / "backend-compatibility.json")
        existing = dict(base_rules["claude_code_model_ids"])
        existing.update(config.get("claude_code", {}).get("model_ids", {}))
        if claude_code_id is not None:
            config.setdefault("claude_code", {}).setdefault("model_ids", {})[model] = claude_code_id
        elif model not in existing:
            raise ValueError(
                f"Claude model {model!r} needs --claude-code-id because it has no known backend mapping"
            )
        if min_version is not None:
            config.setdefault("claude_code", {}).setdefault("minimum_versions", {})[model] = min_version

    _write(path, config)
    print(json.dumps(_summary(path, _load_raw(path) if path.exists() else None), indent=2, sort_keys=True))
    return 0


def cmd_reset(path: Path, family: str | None, tier: str | None, reset_all: bool) -> int:
    if reset_all:
        if path.exists():
            path.unlink()
        print(json.dumps(_summary(path, None), indent=2, sort_keys=True))
        return 0
    if family is None or tier is None:
        raise ValueError("reset requires FAMILY TIER or --all")
    config = _load_raw(path)
    family_models = config.get("models", {}).get(family, {})
    family_models.pop(tier, None)
    _write(path, config)
    print(json.dumps(_summary(path, _load_raw(path) if path.exists() else None), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Explicit user-owned config path.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("path")
    sub.add_parser("show")
    sub.add_parser("validate")

    set_parser = sub.add_parser("set")
    set_parser.add_argument("family", choices=FAMILIES)
    set_parser.add_argument("tier", choices=TIERS)
    set_parser.add_argument("model")
    set_parser.add_argument("--claude-code-id")
    set_parser.add_argument("--min-version")
    set_parser.add_argument("--ambiguity-capacity", type=int)

    reset_parser = sub.add_parser("reset")
    reset_parser.add_argument("family", nargs="?", choices=FAMILIES)
    reset_parser.add_argument("tier", nargs="?", choices=TIERS)
    reset_parser.add_argument("--all", action="store_true", dest="reset_all")

    args = parser.parse_args(argv)
    path = (args.config.expanduser().resolve() if args.config else default_config_path())
    try:
        if args.command == "path":
            print(path)
            return 0
        if args.command == "show":
            return cmd_show(path)
        if args.command == "validate":
            return cmd_validate(path)
        if args.command == "set":
            return cmd_set(path, args.family, args.tier, args.model, args.claude_code_id, args.min_version,
                           args.ambiguity_capacity)
        if args.command == "reset":
            return cmd_reset(path, args.family, args.tier, args.reset_all)
    except (ValueError, OSError, yaml.YAMLError, engine.RoutingAssetError) as exc:
        print(f"model-profiles: {exc}", file=sys.stderr)
        return 1
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
