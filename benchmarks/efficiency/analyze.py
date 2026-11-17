#!/usr/bin/env python3
"""Analyze explicitly paired, independently executed benchmark reports. No model calls."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# These fields describe fixed inputs, not assertions inferred from outcomes.
FIXED = ("revision", "task_sha256", "base_commit", "routing_policy_sha256",
         "autonomy_policy_sha256", "model_config_sha256", "lead_model", "lead_effort",
         "worker_model_config_sha256", "provider_config_sha256", "runtime_fingerprint", "acceptance_sha256", "task_id", "task_kind", "provider_caps", "lead_call_limit", "duration_limit_seconds", "workspace_token_gate", "ledger_path")
VARIANTS = {"baseline", "candidate"}


class Invalid(ValueError):
    pass


def read(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Invalid(f"cannot read {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise Invalid(f"{path} must contain an object")
    return result


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0



def provider_contract_cost_ceiling(ledger):
    """Sum settled request charges from this execution's ledger, never launch-only records.

    This is a conservative rate-card ceiling for measured tokens, not a provider invoice. Empty, malformed, or
    unsettled request observations do not establish complete coverage.
    """
    if not isinstance(ledger, dict) or not isinstance(ledger.get("entries"), list):
        return {"ceiling_usd": None, "coverage": "unobserved", "request_entries": 0, "unsettled_requests": 0}
    requests, unsettled, invalid = 0, 0, False
    total = Decimal(0)
    for entry in ledger["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("reserved"), dict):
            invalid = True
            continue
        count = entry["reserved"].get("requests")
        if type(count) is not int or count < 0:
            invalid = True
            continue
        if count == 0:
            continue
        requests += 1
        if entry.get("state") != "settled":
            unsettled += 1
            continue
        actual = entry.get("actual")
        try:
            if not isinstance(actual, dict) or type(actual.get("requests")) is not int or actual["requests"] != count:
                raise ValueError("invalid request count")
            amount = Decimal(str(actual["cost_usd"]))
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid cost")
            total += amount
        except (KeyError, ValueError, InvalidOperation):
            invalid = True
    complete = requests > 0 and unsettled == 0 and not invalid
    return {"ceiling_usd": float(total) if complete else None,
            "coverage": "complete_observed_requests" if complete else ("unobserved" if not requests and not invalid else "incomplete"),
            "request_entries": requests, "unsettled_requests": unsettled}


def effort_observation(report, expected):
    """Only an explicit gateway observation counts; a configured flag is not evidence."""
    ledger = report.get("provider_ledger") or {}
    evidence = ledger.get("lead_effort_observations") if isinstance(ledger, dict) else None
    if not isinstance(evidence, list) or not evidence:
        return {"status": "unobserved", "values": []}
    if any(not isinstance(item, dict) or not isinstance(item.get("value"), str)
           or not isinstance(item.get("source"), str) or not item["source"] for item in evidence):
        return {"status": "unobserved", "values": []}
    values = sorted({item["value"] for item in evidence})
    return {"status": "matches_configured" if values == [expected] else "differs_from_configured", "values": values}


def metric(report, task_id, lead_effort):
    """Keep incomplete quantities unknown, not zero or apparently exact."""
    try:
        totals = report["metrics"]["totals"]
        tasks = totals["tasks"]
        cost = totals["cost"]
        launch = report["launch"]
        checks = report["validation"]
        runs = report["metrics"]["runs"]
        valid = (launch.get("exit_code") == 0 and tasks["runs"] == 1 and
                 tasks["completed"] == 1 and tasks["verified"] == 1 and
                 len(checks) == 1 and checks[0].get("task") == task_id and
                 checks[0].get("validation") == "VALIDATED" and checks[0].get("exit_code") == 0)
        tokens = totals.get("spend_tokens") if totals.get("spend_complete") else None
        usd = cost.get("estimated_usd") if cost.get("cost_complete") and not cost.get("unpriced") else None
        calls = totals["calls"].get("lead")
        elapsed = report.get("elapsed_seconds")
        return {"verified": valid, "tokens": tokens if number(tokens) else None,
                "model_reported_estimated_cost_usd": usd if number(usd) else None,
                "model_reported_cost_coverage": {"complete": cost.get("cost_complete") is True and not cost.get("unpriced"),
                                                 "unpriced": cost.get("unpriced")},
                "provider_contract_cost_ceiling": provider_contract_cost_ceiling(report.get("provider_ledger")),
                "lead_effort_observation": effort_observation(report, lead_effort),
                "lead_calls": calls if number(calls) else None,
                "worker_calls": totals["calls"].get("workers"),
                "tokens_by_participant": totals.get("tokens"),
                "elapsed_seconds": elapsed if number(elapsed) else None,
                "selector_observed": report.get("selector_observed") is True,
                "config_snapshots_unchanged": report.get("config_snapshots_unchanged") is True,
                "observed_lead_models": sorted({r["tokens"]["lead"].get("model") for r in runs
                                               if r["tokens"]["lead"].get("model")}),
                "unpriced": cost.get("unpriced"), "runs": runs}
    except (KeyError, TypeError, AttributeError) as exc:
        raise Invalid(f"malformed run report: {exc}") from exc


def analyze(manifest, directory):
    if manifest.get("version") != 1 or not isinstance(manifest.get("samples"), list) or not manifest["samples"]:
        raise Invalid("version 1 and a nonempty samples list are required")
    paired, seen = [], set()
    for sample in manifest["samples"]:
        if not isinstance(sample, dict) or not isinstance(sample.get("id"), str) or not sample["id"] or sample["id"] in seen:
            raise Invalid("each sample needs a unique nonempty id")
        seen.add(sample["id"])
        arms = {}
        for variant in sorted(VARIANTS):
            arm = sample.get(variant)
            if not isinstance(arm, dict) or not isinstance(arm.get("report"), str):
                raise Invalid(f"{sample['id']}: {variant} requires a report")
            report = read(directory / arm["report"])
            p = report.get("provenance")
            if not isinstance(p, dict):
                raise Invalid(f"{sample['id']}: {variant} report lacks provenance")
            missing = [k for k in FIXED if p.get(k) is None or p.get(k) == "" or p.get(k) == {}]
            if missing:
                raise Invalid(f"{sample['id']} {variant}: missing provenance: {', '.join(missing)}")
            if p.get("variant") != variant or p.get("pair") != sample["id"]:
                raise Invalid(f"{sample['id']}: variant provenance mismatch")
            arms[variant] = {"provenance": p, "report": str(arm["report"]),
                             "metrics": metric(report, p["task_id"], p["lead_effort"])}
        differences = [k for k in FIXED if arms["baseline"]["provenance"][k] != arms["candidate"]["provenance"][k]]
        if differences:
            raise Invalid(f"{sample['id']}: unmatched fixed inputs: {', '.join(differences)}")
        models = {v: arms[v]["metrics"]["observed_lead_models"] for v in VARIANTS}
        expected = arms["baseline"]["provenance"]["lead_model"]
        model_observed = all(models[v] == [expected] for v in VARIANTS)
        valid = model_observed and all(arms[v]["metrics"]["verified"] and arms[v]["metrics"]["selector_observed"] and arms[v]["metrics"]["config_snapshots_unchanged"] for v in VARIANTS)
        deltas = {}
        for name in ("tokens", "model_reported_estimated_cost_usd", "provider_contract_cost_ceiling_usd", "lead_calls", "elapsed_seconds"):
            if name == "provider_contract_cost_ceiling_usd":
                b, c = (arms[v]["metrics"]["provider_contract_cost_ceiling"]["ceiling_usd"] for v in ("baseline", "candidate"))
            else:
                b, c = (arms[v]["metrics"][name] for v in ("baseline", "candidate"))
            deltas[name] = c - b if valid and number(b) and number(c) else None
        paired.append({"id": sample["id"], "arms": arms, "model_pin_observed": model_observed,
                       "both_verified": valid, "candidate_minus_baseline": deltas})
    summary = {}
    for name in ("tokens", "model_reported_estimated_cost_usd", "provider_contract_cost_ceiling_usd", "lead_calls", "elapsed_seconds"):
        values = [p["candidate_minus_baseline"][name] for p in paired if p["candidate_minus_baseline"][name] is not None]
        summary[name] = {"paired_observations": len(values), "mean_delta": statistics.mean(values) if values else None,
                         "sample_stddev": statistics.stdev(values) if len(values) > 1 else None,
                         "min_delta": min(values) if values else None, "max_delta": max(values) if values else None}
    return {"version": 1, "samples": paired, "summary": summary,
            "interpretation": "Descriptive paired deltas only; a single sample cannot quantify variability. Missing, failed or unverified observations are excluded, not zero. Configured pins do not prove worker model or effort was observed. Provider contract cost ceiling uses conservative rates for settled observed requests, not an invoice or proof that every backend was intercepted."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = analyze(read(args.manifest), args.manifest.parent)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (Invalid, OSError) as exc:
        print(f"efficiency analysis: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
