#!/usr/bin/env python3
"""Run one guarded real pilot arm. See README before use; this can incur API charges."""
import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import budget_ledger


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


setup = module("efficiency_setup", HERE / "setup.py")
real = module("real_benchmark", HERE.parent / "synthetic-workspace/run_real.py")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()



def snapshot_config(directory, name, data):
    """Write pre-read configuration bytes privately; never reread their original path."""
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return path, hashlib.sha256(data).hexdigest()


def runtime_fingerprint(pi, config):
    node = shutil.which("node")
    if not node:
        raise real.Refused("node is unavailable; runtime cannot be fingerprinted")
    extensions = config.get("extensions")
    if not isinstance(extensions, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in extensions):
        raise real.Refused("provider extension paths must be an explicit list of absolute paths")
    def version(executable):
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True, timeout=15)
        value = result.stdout.strip()
        if not value or len(value) > 500:
            raise real.Refused("runtime returned an invalid version")
        return value
    return {"pi_version": version(pi), "node_version": version(node),
            "pi_target_sha256": digest(Path(pi).resolve(strict=True)),
            "node_target_sha256": digest(Path(node).resolve(strict=True)),
            "host_platform": platform.platform(),
            "configured_extensions_sha256": {p: digest(p) for p in sorted(extensions)},
            "provider_package_lock_sha256": digest(ROOT / "package-lock.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    parser.add_argument("pair")
    parser.add_argument("variant", choices=("baseline", "candidate"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-tokens", type=int, required=True, help="workspace launch gate; provider limits remain authoritative")
    parser.add_argument("--max-lead-calls", type=int, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--lead-model", required=True)
    parser.add_argument("--lead-effort", required=True)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--pico", default="pico")
    args = parser.parse_args()
    try:
        real.check_enabled()
        real.check_revision(args.revision)
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        if head != args.revision or dirty:
            raise real.Refused("harness must be a clean checkout of the exact requested runtime revision")
        if not real.LABEL_RE.fullmatch(args.pair):
            raise real.Refused("pair must be 1-40 letters, digits, '_' or '-'")
        if not (ROOT / "tools/provider_budget.py").is_file():
            raise real.Refused("provider cap gateway is unavailable; refusing a paid run")
        config, ledger = (os.environ.get(k, "") for k in ("PICO_PROVIDER_BUDGET_CONFIG", "PICO_AGGREGATE_BUDGET_LEDGER"))
        if not config or not ledger or not Path(config).is_absolute() or not Path(ledger).is_absolute():
            raise real.Refused("absolute provider config and aggregate ledger paths are required")
        provider_bytes = Path(config).read_bytes()
        model_bytes = args.model_config.read_bytes()
        cfg = json.loads(provider_bytes)
        limits = budget_ledger.inspect(ledger)["limits"]
        for scope in ("lifetime", "attempt"):
            if not {"tokens", "cost_usd"}.issubset(limits.get(scope, {})):
                raise real.Refused(f"ledger requires {scope} token and dollar limits")
        if cfg.get("lead_model") != args.lead_model:
            raise real.Refused("lead model does not match provider configuration")
        pico, pi = shutil.which(args.pico), shutil.which("pi")
        if not pico or not pi:
            raise real.Refused("pico and pi must be available", code=3)
        fingerprint = runtime_fingerprint(pi, cfg)
        out = args.output.expanduser().resolve()
        if out.exists() and any(out.iterdir()):
            raise real.Refused("use a fresh output directory for each arm; resume is not supported")
        out.mkdir(parents=True, exist_ok=True)
        snapshots = out / "config"
        snapshots.mkdir(mode=0o700)
        provider_snapshot, provider_hash = snapshot_config(snapshots, "provider.json", provider_bytes)
        model_snapshot, model_hash = snapshot_config(snapshots, "models.yaml", model_bytes)
        state = out / "state"
        state.mkdir()
        fixture = setup.create(out / "work", args.max_tokens, args.max_lead_calls, args.duration,
                               workspace_id=f"eff-{args.pair}-{args.variant}")
        env = real.pico_env(args.revision, state)
        env.update(PICO_BENCHMARK_VARIANT=args.variant, PICO_BENCHMARK_PAIR=args.pair,
                   PICO_BENCHMARK_REVISION=args.revision, PICO_BENCHMARK_LEAD_MODEL=args.lead_model,
                   PICO_BENCHMARK_LEAD_EFFORT=args.lead_effort,
                   PI_ORCHESTRATOR_MODEL_CONFIG=str(model_snapshot),
                   PICO_PROVIDER_BUDGET_CONFIG=str(provider_snapshot))
        started = real.now()
        start = time.monotonic()
        launch = real.execute([pico, "workspace", "schedule", fixture["manifest"], "--no-access-check", "--allow-mutation", "--json"], env, "launch")
        elapsed = time.monotonic() - start
        if launch["output"] is None:
            raise real.Refused(f"schedule produced no JSON; exit {launch['exit_code']}; see execution state in {out}", code=1)
        workspace = __import__("yaml").safe_load(Path(fixture["manifest"]).read_text())
        validation = real.validate_completed(pico, env, Path(fixture["manifest"]), workspace, launch["output"])
        metrics = real.benchmark_metrics.workspace_report(state, fixture["workspace_id"], label=args.variant)
        provenance = {"variant": args.variant, "pair": args.pair, "revision": args.revision,
                      "task_id": "one-line", "task_sha256": fixture["task_sha256"], "base_commit": fixture["base_commit"],
                      "acceptance_sha256": fixture["acceptance_sha256"], "autonomy_policy_sha256": fixture["autonomy_policy_sha256"],
                      "routing_policy_sha256": digest(ROOT / "routing/default-policy.yaml"),
                      "model_config_sha256": model_hash, "worker_model_config_sha256": model_hash,
                      "provider_config_sha256": provider_hash, "runtime_fingerprint": fingerprint, "lead_model": args.lead_model, "lead_effort": args.lead_effort,
                      "provider_caps": limits, "lead_call_limit": args.max_lead_calls, "duration_limit_seconds": args.duration,
                      "workspace_token_gate": args.max_tokens, "ledger_path": str(Path(ledger).resolve()),
                      "pi_path": pi, "pico_path": pico, "access_check": False, "publication": False}
        records = []
        for path in (state / "executions").rglob("*.json"):
            try:
                record = json.loads(path.read_text())
                if isinstance(record, dict) and record.get("execution_id"):
                    records.append(record)
            except (OSError, ValueError):
                pass
        expected_selection = {"variant": args.variant, "pair": args.pair, "revision": args.revision,
                              "lead_model": args.lead_model, "lead_effort": args.lead_effort}
        observed = len(records) == 1 and records[0].get("benchmark") == expected_selection and records[0].get("orchestrator_revision") == args.revision
        # Keep provider reservations distinct from model-reported cost. An unsettled
        # reservation is not an actual charge; it remains explicit and conservative.
        ledger_after = budget_ledger.inspect(ledger)
        execution_ids = {r["execution_id"] for r in records}
        provider_entries = [{"state": item.get("state"), "reserved": item.get("reserved"),
                             "actual": item.get("actual")}
                            for item in ledger_after["reservations"].values()
                            if item.get("attempt_id") in execution_ids]
        snapshots_unchanged = digest(provider_snapshot) == provider_hash and digest(model_snapshot) == model_hash
        report = {"version": 1, "selector_observed": observed, "config_snapshots_unchanged": snapshots_unchanged, "provenance": provenance, "started_at": started, "finished_at": real.now(),
                  "provider_ledger": {"entries": provider_entries,
                                      "all_entries_settled": bool(provider_entries) and all(e["state"] == "settled" for e in provider_entries)},
                  "elapsed_seconds": round(elapsed, 3), "launch": {"exit_code": launch["exit_code"], **launch["output"]},
                  "validation": validation, "metrics": metrics, "fixture": fixture}
        (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"report": str(out / "report.json"), "launch_exit_code": launch["exit_code"]}))
        return 0
    except (real.Refused, setup.synthetic.SetupError, budget_ledger.BudgetError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"efficiency run: {exc}", file=sys.stderr)
        return getattr(exc, "code", 2) if isinstance(getattr(exc, "code", 2), int) else 2


if __name__ == "__main__":
    raise SystemExit(main())
