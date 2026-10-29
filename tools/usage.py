#!/usr/bin/env python3
"""Observed token and cost usage of child runs, shared by the Garden and the scheduler.

Counts come from each parent run's recorded context: the lead's usage plus
every worker backend. estimated_cost_usd is the sum of the list-price
estimates participants reported (Claude workers, and the lead when Pi reports
a per-message cost). A participant with tokens but no reported cost is listed
in `unpriced` and makes `cost_complete` false: the cost is then a lower bound.
Spend (the budgeted quantity) is input + output + cache-write tokens; cache
reads are reported separately and never counted against a budget.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "total_tokens")
COUNTED = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _cost(value):
    return value if type(value) in (int, float) and value >= 0 else None


def _has_tokens(part: dict) -> bool:
    return any((_count(part.get(field)) or 0) > 0 for field in (*COUNTED, "total_tokens"))


def _priced(part: dict, cost) -> bool:
    """Whether a participant's cost is fully known.

    A participant that did anything (invocations or tokens) with no cost, or with a
    zero cost despite tokens, is unpriced. An explicit per-participant
    cost_complete=false (some event was unpriced) also makes it so; records that
    predate the flag are judged from their aggregates.
    """
    if part.get("cost_complete") is False:
        return False
    active = (_count(part.get("invocations")) or 0) > 0 or _has_tokens(part)
    if not active:
        return True
    return cost is not None and not (cost == 0 and _has_tokens(part))


def cost_coverage(context: dict) -> tuple[float | None, list[str]]:
    """Reported cost of one run's participants and the names of those whose cost is not fully known."""
    lead = context.get("lead_usage") if isinstance(context.get("lead_usage"), dict) else {}
    usage = context.get("worker_usage") if isinstance(context.get("worker_usage"), dict) else {}
    named = [(str(lead.get("model") or "lead"), lead)] + [(str(name), entry) for name, entry in usage.items()
                                                           if isinstance(entry, dict)]
    costs = [_cost(part.get("estimated_cost_usd")) for _, part in named]
    unpriced = sorted({name for (name, part), cost in zip(named, costs, strict=True) if not _priced(part, cost)})
    known = [cost for cost in costs if cost is not None]
    return (round(sum(known), 6) if known else None), unpriced


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
    for field in COUNTED:
        observed = [_count(part.get(field)) for part in (lead, *workers)]
        observed = [value for value in observed if value is not None]
        tokens[field] = sum(observed) if observed else None
    complete = (lead.get("total_tokens_complete") is True and _count(lead.get("total_tokens")) is not None
                and all(entry.get("total_tokens_complete") is True and _count(entry.get("total_tokens")) is not None
                        for entry in workers))
    tokens["total_tokens"] = lead["total_tokens"] + sum(entry["total_tokens"] for entry in workers) if complete else None
    tokens["estimated_cost_usd"], tokens["unpriced"] = cost_coverage(context)
    tokens["cost_complete"] = not tokens["unpriced"]
    return tokens


def sum_tokens(parts: list[dict]) -> dict:
    """Aggregate per-run token blocks; total_complete is true when every counted run reported a total."""
    result: dict = {"runs": len(parts)}
    for field in (*TOKEN_FIELDS, "estimated_cost_usd"):
        observed = [part[field] for part in parts if part.get(field) is not None]
        total = sum(observed) if observed else None
        result[field] = round(total, 6) if field == "estimated_cost_usd" and total is not None else total
    result["total_complete"] = bool(parts) and all(part.get("total_tokens") is not None for part in parts)
    # Blocks from before these fields carry no `unpriced` list and add nothing to it.
    result["unpriced"] = sorted({name for part in parts for name in part.get("unpriced") or []})
    result["cost_complete"] = not result["unpriced"]
    return result



def spent(tokens: dict) -> int:
    """Billed tokens (input, output and cache writes), the quantity a workspace token budget limits.

    Cache reads are excluded: they are reported separately as cached_tokens.
    """
    return sum(tokens.get(field) or 0 for field in ("input_tokens", "output_tokens", "cache_write_tokens"))


def workspace_usage(runs_dir: Path, record: dict, extra_run_ids=()) -> dict:
    """Summed usage of every child run linked from a workspace record.

    extra_run_ids adds runs not yet linked (children still running).
    """
    parts = []
    tasks = record.get("tasks") if isinstance(record.get("tasks"), dict) else {}
    linked = [entry.get("run_id") for entry in tasks.values() if isinstance(entry, dict)]
    # Earlier attempts of a task (superseded after it stopped for authorization) spent tokens too.
    linked += [attempt.get("run_id") for entry in tasks.values() if isinstance(entry, dict)
               for attempt in entry.get("attempts") or [] if isinstance(attempt, dict)]
    seen: set = set()
    for run_id in (*linked, *extra_run_ids):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or run_id in seen:
            continue
        seen.add(run_id)
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
