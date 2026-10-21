#!/usr/bin/env python3
"""Read a target repository's `.pi/autonomy.yaml` execution-authority limits.

The parent orchestrator reads four values from it:

- `limits.parallel_tasks`: the most routed tasks one wave may run at once
  (`orchestration_record` `declare_wave` rejects larger waves with
  `WAVE_LIMIT_EXCEEDED`);
- `limits.open_prs`: the most open batch PRs before new batches stack on the
  newest batch branch instead of opening another PR;
- `pr_granularity`: `batch` (one push and one PR per integrated wave batch) or
  `task` (one PR per task);
- `issue_reference`: `refs` (PR bodies say `Refs #N`, which never closes the
  issue) or `closes` (`Closes #N`, so the issue closes when the PR merges).

A repository without the file gets the defaults below. A file that exists but
does not validate against `schemas/autonomy.schema.json` fails closed with
`AUTONOMY_POLICY_INVALID`; it is never partially applied.

CLI: `autonomy_policy.py limits --worktree <target>` prints a closed JSON verdict.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "autonomy.schema.json"
RELATIVE_PATH = Path(".pi") / "autonomy.yaml"
DEFAULTS = {"parallel_tasks": 3, "open_prs": 2, "pr_granularity": "batch", "issue_reference": "refs"}


class AutonomyPolicyError(Exception):
    pass


def policy_path(worktree: Path) -> Path:
    """`.pi/autonomy.yaml` at the worktree's Git top level, or in the directory itself outside Git."""
    try:
        result = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
                                shell=False, capture_output=True, text=True, timeout=10)
        top = Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else worktree
    except (OSError, subprocess.TimeoutExpired):
        top = worktree
    return top / RELATIVE_PATH


def validate(data: object) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
            for error in sorted(validator.iter_errors(data), key=str)]


def limits(worktree: Path) -> dict:
    path = policy_path(worktree)
    if not path.is_file():
        return {"ok": True, "source": "default", **DEFAULTS}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AutonomyPolicyError("autonomy policy is unreadable") from exc
    issues = validate(data)
    if issues:
        raise AutonomyPolicyError(issues[0])
    configured = data.get("limits", {})
    return {"ok": True, "source": str(RELATIVE_PATH),
            "parallel_tasks": configured.get("parallel_tasks", DEFAULTS["parallel_tasks"]),
            "open_prs": configured.get("open_prs", DEFAULTS["open_prs"]),
            "pr_granularity": data.get("pr_granularity", DEFAULTS["pr_granularity"]),
            "issue_reference": data.get("issue_reference", DEFAULTS["issue_reference"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_limits = sub.add_parser("limits")
    p_limits.add_argument("--worktree", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = limits(args.worktree)
    except AutonomyPolicyError as exc:
        result = {"ok": False, "blocker_code": "AUTONOMY_POLICY_INVALID", "error": str(exc)[:300]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
