#!/usr/bin/env python3
"""Observed token and cost usage of child runs, shared by the Garden and the scheduler.

Counts come from each parent run's recorded context: the lead's usage plus
every worker backend. estimated_cost_usd is the backend-reported list-price
estimate, which today only Claude workers report, so it is a lower bound on
real spend; token counts cover every participant that reported them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _count(value):
    return value if type(value) is int and value >= 0 else None


def run_tokens(parent: dict) -> dict:
    """Observed tokens of one parent run, from the context counters the run view's metrics derive from.

    Input, output and cached counts add the lead and every worker backend.
    total_tokens is known only when the lead and every backend reported
    explicit, complete totals, as in parent_orchestration.delegation_metrics.
    """
    context = parent.get("context") if isinstance(parent.get("context"), dict) else {}
    lead = context.get("lead_usage") if isinstance(context.get("lead_usage"), dict) else {}
    usage = context.get("worker_usage") if isinstance(context.get("worker_usage"), dict) else {}
    workers = [entry for entry in usage.values() if isinstance(entry, dict)]
    tokens: dict = {}
    for field in ("input_tokens", "output_tokens", "cached_tokens"):
        observed = [_count(part.get(field)) for part in (lead, *workers)]
        observed = [value for value in observed if value is not None]
        tokens[field] = sum(observed) if observed else None
    complete = (lead.get("total_tokens_complete") is True and _count(lead.get("total_tokens")) is not None
                and all(entry.get("total_tokens_complete") is True and _count(entry.get("total_tokens")) is not None
                        for entry in workers))
    tokens["total_tokens"] = lead["total_tokens"] + sum(entry["total_tokens"] for entry in workers) if complete else None
    costs = [entry["estimated_cost_usd"] for entry in workers
             if type(entry.get("estimated_cost_usd")) in (int, float) and entry["estimated_cost_usd"] >= 0]
    tokens["estimated_cost_usd"] = round(sum(costs), 6) if costs else None
    return tokens


def sum_tokens(parts: list[dict]) -> dict:
    """Aggregate per-run token blocks; total_complete is true when every counted run reported a total."""
    result: dict = {"runs": len(parts)}
    for field in (*TOKEN_FIELDS, "estimated_cost_usd"):
        observed = [part[field] for part in parts if part.get(field) is not None]
        total = sum(observed) if observed else None
        result[field] = round(total, 6) if field == "estimated_cost_usd" and total is not None else total
    result["total_complete"] = bool(parts) and all(part.get("total_tokens") is not None for part in parts)
    return result



def spent(tokens: dict) -> int:
    """Input plus output tokens, the quantity a workspace token budget limits."""
    return (tokens.get("input_tokens") or 0) + (tokens.get("output_tokens") or 0)


def workspace_usage(runs_dir: Path, record: dict) -> dict:
    """Summed usage of every child run linked from a workspace record."""
    parts = []
    tasks = record.get("tasks") if isinstance(record.get("tasks"), dict) else {}
    for entry in tasks.values():
        run_id = entry.get("run_id") if isinstance(entry, dict) else None
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            continue
        try:
            run = json.loads((runs_dir / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        parent = run.get("parent_orchestration") if isinstance(run, dict) else None
        if isinstance(parent, dict):
            parts.append(run_tokens(parent))
    total = sum_tokens(parts)
    total["spent_tokens"] = spent(total)
    return total
