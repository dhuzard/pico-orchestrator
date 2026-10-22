#!/usr/bin/env python3
"""Run the synthetic workspace with REAL agents on a pinned PICO revision and write a JSON report (issue #49, part 2).

    PICO_REAL_BENCHMARK=1 benchmarks/synthetic-workspace/run-real.sh <40-hex-revision> <label>
        [--output-dir DIR] [--owner OWNER] [--max-tokens N] [--continue] [--pico PATH]
        [--publication --i-own-these-repos --issue TASK=N ...]

This is the only supported entry point of the real-agent benchmark. It spends tokens and time on whatever
backends `pi` is configured with, so it refuses to run unless PICO_REAL_BENCHMARK=1 is set, exactly that
value. Routine CI never sets it and never runs this script; the deterministic regression suite
(tests/test_synthetic_pilot.py) is the CI part and uses no model.

What it does, in this order, on one fresh workspace and one fresh state directory per label:

  1. setup     benchmarks/synthetic-workspace/setup.py: two throwaway repositories and workspace.yaml
  2. launch    `pico workspace schedule` with PI_CODING_ORCHESTRATOR_REF pinned to the revision
  3. validate  `pico workspace validate` for every task whose run COMPLETED (its checks run on the exact commit)
  4. metrics   tools/benchmark_metrics.py: completion, models, tokens, calls, duration, reported cost

and writes DIR/report.json. Nothing in the report is estimated: a value no run recorded is null.

Limits you should know. Without GitHub the planner never unblocks a dependent task (it needs a published
draft PR with green CI), so a default run covers the first wave: lib-slugify. For the full four-task run use
sandbox repositories you own: pass --publication --i-own-these-repos (and --issue TASK=N), which setup.py
needs to commit the sandbox policy. In that mode `pico workspace schedule` keeps its repository access check,
so missing or inaccessible repositories fail closed. Push the baselines, publish and promote by hand as the
README describes, then run this script again with --continue: it skips setup and repeats launch, validate
and metrics on the same state, with the options recorded by the first run.
This script never pushes, never opens a pull request and never merges.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import benchmark_metrics  # noqa: E402

ENABLE_ENV = "PICO_REAL_BENCHMARK"
REPORT_VERSION = 1
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
RUN_FILE = "run.json"


class Refused(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_setup():
    spec = importlib.util.spec_from_file_location("synthetic_workspace_setup", HERE / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_enabled() -> None:
    if os.environ.get(ENABLE_ENV) != "1":
        raise Refused(f"refusing to run: the real-agent benchmark spends tokens and time, so it only starts when "
                      f"{ENABLE_ENV}=1 is set explicitly (it is never set in CI). Nothing was created.")


def check_revision(revision: str) -> None:
    if not REVISION_RE.fullmatch(revision):
        raise Refused("REV must be a full 40-hex PICO commit id (a branch or tag moves; a benchmark needs a pin)")
    probe = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{revision}^{{commit}}"], capture_output=True, check=False)
    if probe.returncode:
        raise Refused(f"revision {revision} is not a commit of this repository (fetch it first)")


def execute(argv: list[str], env: dict, step: str) -> dict:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    parsed = None
    try:
        parsed = json.loads(done.stdout)
    except ValueError:
        pass
    return {"step": step, "argv": argv, "exit_code": done.returncode, "output": parsed,
            "stdout_tail": None if parsed is not None else done.stdout[-2000:], "stderr_tail": done.stderr[-2000:] or None}


def pico_env(revision: str, state: Path) -> dict:
    return {**os.environ, "PI_CODING_ORCHESTRATOR_REF": revision, "PI_CODING_ORCHESTRATOR_STATE_DIR": str(state)}


def candidate_heads(repo: Path, task: str) -> list[str]:
    listing = subprocess.run(["git", "-C", str(repo), "branch", "--list", "pico/*", "--format=%(refname:short)"],
                             capture_output=True, text=True, check=False).stdout.split()
    return [branch for branch in listing if task in branch]


def validate_completed(pico: str, env: dict, manifest: Path, workspace: dict, schedule: dict) -> list[dict]:
    """Run `pico workspace validate` for each COMPLETED task. A task whose branch cannot be identified stays unverified."""
    repositories = {r["id"]: manifest.parent / r["path"] for r in workspace["repositories"]}
    results = []
    for row in (schedule or {}).get("tasks") or []:
        task = next((t for t in workspace["tasks"] if t["id"] == row["id"]), None)
        if task is None or task.get("kind") == "qualification" or row.get("state") != "completed":
            continue
        heads = candidate_heads(repositories[task["repository"]], task["id"])
        if len(heads) != 1:
            results.append({"task": task["id"], "validation": "skipped",
                            "reason": f"cannot identify the task's pico/ branch (candidates: {heads}); validate it by hand with --head"})
            continue
        step = execute([pico, "workspace", "validate", str(manifest), "--task", task["id"], "--head", heads[0]], env, "validate")
        results.append({"task": task["id"], "head": heads[0], "validation": (step["output"] or {}).get("status", "error"),
                        "exit_code": step["exit_code"], "checks": [{"id": c.get("id"), "status": c.get("status")}
                                                                   for c in (step["output"] or {}).get("checks") or []]})
    return results


def parse_issues(items: list[str]) -> dict[str, int]:
    issues = {}
    for item in items:
        task, _, number = item.partition("=")
        if not number.isdigit() or int(number) < 1:
            raise Refused(f"--issue expects TASK=N, got {item!r}")
        issues[task] = int(number)
    return issues


def run(args: argparse.Namespace) -> int:
    check_enabled()
    check_revision(args.revision)
    if not LABEL_RE.fullmatch(args.label):
        raise Refused("LABEL must be 1-40 characters of letters, digits, '_' or '-'")
    pico = shutil.which(args.pico)
    if pico is None:
        raise Refused(f"the pico launcher ({args.pico}) is not on PATH; install it or pass --pico", code=3)
    if shutil.which("pi") is None:
        raise Refused("no real backend: `pi` is not on PATH. Unavailable backends are reported, never faked.", code=3)
    out = Path(args.output_dir).expanduser() if args.output_dir else Path.home() / "pico-bench" / args.label
    work, state, marker = out / "work", out / "state", out / RUN_FILE
    if args.resume:
        recorded = json.loads(marker.read_text()) if marker.is_file() else {}
        if recorded.get("revision") != args.revision or recorded.get("label") != args.label:
            raise Refused("--continue needs the output directory of an earlier run with the same label and revision")
    elif out.exists() and any(out.iterdir()):
        raise Refused(f"{out} is not empty: use a fresh directory per label and revision, or --continue")

    started = now()
    steps = []
    if args.resume:
        publication = bool(recorded.get("publication"))
    else:
        setup = load_setup()
        issues = parse_issues(args.issue)
        publication = args.publication
        if publication and args.owner != setup.DEFAULT_OWNER and not args.confirm_owned:
            raise Refused("--publication with your own owner needs --i-own-these-repos (see setup.py)")
        out.mkdir(parents=True, exist_ok=True)
        state.mkdir()
        try:
            created = setup.create(work, owner=args.owner, publication=publication, issues=issues,
                                   confirm_owned=args.confirm_owned, max_tokens=args.max_tokens)
        except setup.SetupError as exc:
            raise Refused(f"setup failed: {exc}", code=1) from exc
        marker.write_text(json.dumps({"label": args.label, "revision": args.revision, "workspace": created["workspace_id"],
                                      "manifest": created["manifest"], "started_at": started, "owner": created["owner"],
                                      "publication": publication, "max_tokens": created["max_tokens"], "issues": issues},
                                     indent=2) + "\n")
        steps.append({"step": "setup", "pinned_commits": created["pinned_commits"], "owner": created["owner"],
                      "publication": publication, "max_tokens": created["max_tokens"]})
    manifest = work / "workspace.yaml"
    env = pico_env(args.revision, state)
    # Sandbox repositories exist on GitHub: keep the access check so an unresolved identity fails closed.
    access = [] if publication else ["--no-access-check"]
    schedule = execute([pico, "workspace", "schedule", str(manifest), *access, "--json"], env, "launch")
    steps.append(schedule)
    if schedule["output"] is None:
        raise Refused(f"`pico workspace schedule` produced no JSON (exit {schedule['exit_code']}): {schedule['stderr_tail']}", code=1)
    import yaml
    workspace = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    validation = validate_completed(pico, env, manifest, workspace, schedule["output"])
    metrics = benchmark_metrics.workspace_report(state, workspace["id"], label=args.label)
    report = {"report_version": REPORT_VERSION, "kind": "real-agent-benchmark", "label": args.label,
              "pico_revision": args.revision, "workspace_id": workspace["id"], "started_at": started, "finished_at": now(),
              "continued": bool(args.resume), "publication": publication, "access_check": publication,
              "backend": {"pi": shutil.which("pi"), "pico": pico},
              "launch": {"exit_code": schedule["exit_code"], "status": schedule["output"].get("status"),
                         "session": schedule["output"].get("session"), "tasks": schedule["output"].get("tasks")},
              "steps": steps, "validation": validation, "metrics": metrics,
              "note": "Nothing here is estimated: a value no run recorded is null. A default run covers the first wave "
                      "(dependents wait for a published, CI-green upstream); see run_real.py for the sandbox continuation."}
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(out / "report.json"), "launch_status": report["launch"]["status"],
                      "headline": metrics["headline"]}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-agent synthetic benchmark (explicit start only).")
    parser.add_argument("revision", help="full 40-hex PICO commit under test")
    parser.add_argument("label", help="name of this run, e.g. before or after")
    parser.add_argument("--output-dir", help="default: ~/pico-bench/LABEL")
    parser.add_argument("--owner", default="pico-bench-sandbox", help="GitHub owner named in the repositories' remote URLs")
    parser.add_argument("--max-tokens", type=int, default=5_000_000,
                        help="workspace budget.max_tokens written by setup (default %(default)s)")
    parser.add_argument("--publication", action="store_true",
                        help="sandbox mode: setup commits the sandbox publication policy and the access check stays on")
    parser.add_argument("--i-own-these-repos", dest="confirm_owned", action="store_true",
                        help="with --publication and --owner: confirms the repositories are your own sandbox repositories")
    parser.add_argument("--issue", action="append", default=[], metavar="TASK=N",
                        help="issue number in the task's repository that its draft PR references")
    parser.add_argument("--continue", dest="resume", action="store_true",
                        help="skip setup and repeat launch, validate and metrics on an earlier run's workspace and state")
    parser.add_argument("--pico", default=os.environ.get("PICO_BIN", "pico"), help="the pico launcher (default: pico on PATH)")
    try:
        return run(parser.parse_args(argv))
    except Refused as exc:
        print(f"run_real: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
