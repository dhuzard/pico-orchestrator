#!/usr/bin/env python3
"""Prepare one deterministic, local-only pilot workspace. No inference or network."""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("synthetic_setup", HERE.parent / "synthetic-workspace/setup.py")
synthetic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(synthetic)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create(target, max_tokens, max_lead_calls, duration, workspace_id="pico-efficiency-pilot", task_kind="arithmetic"):
    if task_kind not in {"arithmetic", "config"}:
        raise synthetic.SetupError("task kind must be arithmetic or config")
    target = target.expanduser().resolve()
    if synthetic.inside_git(target) or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise synthetic.SetupError("target must be an empty directory outside any Git repository")
    if any(type(v) is not int or v < 1 for v in (max_tokens, max_lead_calls, duration)):
        raise synthetic.SetupError("limits must be positive integers")
    target.mkdir(parents=True, exist_ok=True)
    repo = target / "pilot"
    repo.mkdir()
    if task_kind == "arithmetic":
        (repo / "arithmetic.py").write_text('"""Small arithmetic helper."""\n\ndef difference(a, b):\n    return a + b\n')
    else:
        (repo / "settings.json").write_text('{\n  "display": {\n    "max_results": 10\n  }\n}\n')
    policy = json.loads(json.dumps(synthetic.AUTONOMY))
    policy["permissions"].update(push_branch=False, create_pr=False)
    (repo / ".pi").mkdir()
    (repo / ".pi/autonomy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    synthetic.git(repo, "init", "-q", "-b", "main")
    synthetic.git(repo, "remote", "add", "origin", "https://github.com/pico-bench-sandbox/pilot.git")
    synthetic.git(repo, "add", "-A")
    synthetic.git(repo, "commit", "-q", "-m", "Pilot benchmark baseline")
    base = synthetic.git(repo, "rev-parse", "HEAD")
    if task_kind == "arithmetic":
        title = "Correct subtraction"
        objective = "In arithmetic.py, correct difference(a, b) so it returns a minus b instead of their sum. Change only the return line; do not alter any other file."
        acceptance = HERE / "acceptance.py"
    else:
        title = "Update display configuration"
        objective = "In settings.json, set display.max_results to 20 instead of 10. Change only this configuration value; do not alter any other file."
        acceptance = HERE / "config_acceptance.py"
    task = {"id": "one-line", "repository": "pilot", "title": title, "objective": objective,
            "max_duration_seconds": duration,
            "checks": [{"id": "acceptance", "repository": "pilot", "argv": ["python3", str(acceptance), base], "timeout_seconds": 30}]}
    manifest = {"schema_version": 1, "id": workspace_id, "goal": title,
                "max_parallel": 1, "max_duration_seconds": duration, "max_lead_calls": max_lead_calls,
                "budget": {"max_tokens": max_tokens},
                "repositories": [{"id": "pilot", "repository": "pico-bench-sandbox/pilot", "path": "pilot",
                                  "pinned_commit": base, "base_branch": "main"}], "tasks": [task]}
    path = target / "workspace.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, width=120))
    return {"manifest": str(path), "workspace_id": manifest["id"], "base_commit": base,
            "task_sha256": hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest(),
            "autonomy_policy_sha256": sha(repo / ".pi/autonomy.yaml"), "acceptance_sha256": sha(acceptance), "task_kind": task_kind}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--max-lead-calls", type=int, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--task-kind", choices=("arithmetic", "config"), default="arithmetic")
    args = parser.parse_args()
    try:
        print(json.dumps(create(args.target, args.max_tokens, args.max_lead_calls, args.duration, task_kind=args.task_kind), indent=2))
    except synthetic.SetupError as exc:
        print(f"pilot setup: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
