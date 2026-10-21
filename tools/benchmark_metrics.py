#!/usr/bin/env python3
"""Benchmark metrics for PICO runs: tokens, calls, duration, cost, outcome and provenance completeness.

Read-only. It reads run records (`<state>/runs/<run-id>.json`), the workspace
record (`<state>/workspaces/<id>.json`), execution records and, when present, the
lead's Pi session JSONL. It never runs a model and never estimates a value that was
not recorded: anything unobserved is reported as null (shown as "n/a" in tables).

    benchmark_metrics.py --state-dir STATE --workspace ID [--json]
    benchmark_metrics.py --state-dir STATE RUN_ID [RUN_ID ...]
    benchmark_metrics.py --compare BEFORE AFTER [--json]

A compare side is either a JSON report written earlier by this tool, or
`STATE_DIR@WORKSPACE_ID`, or `STATE_DIR@RUN_ID[,RUN_ID...]`.

Headline: tokens per successfully completed and verified task = (input + output +
cache-write tokens of the lead and every worker backend) / (number of tasks whose
child run ended COMPLETED and whose checks all passed). With no such task it is
"n/a", never 0 or infinity. Qualification tasks launch no run and are reported
separately; they are not in the denominator.

Exit codes: 0 ok, 1 provenance incomplete in --compare (facts of BEFORE missing from
AFTER), 2 invalid invocation or unreadable records.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage  # noqa: E402

REPORT_VERSION = 1
NA = "n/a"
SESSION_ENTRY = "pi-orchestration-parent-v1"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
WAVE_STATUSES = ("declared", "integrating", "integrated")
SPEND_FIELDS = ("input_tokens", "output_tokens", "cache_write_tokens")
# Report names for the usage counters; cache_read is the record's `cached_tokens`.
TOKEN_NAMES = {"input_tokens": "input", "output_tokens": "output", "cached_tokens": "cache_read",
               "cache_write_tokens": "cache_write"}


class MetricsError(Exception):
    pass


# --- reading records -------------------------------------------------------

def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MetricsError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MetricsError(f"{path} is not a JSON object")
    return data


def load_run(state: Path, run_id: str) -> dict:
    if not ID_RE.fullmatch(run_id):
        raise MetricsError(f"invalid run id {run_id!r}")
    return _load(state / "runs" / f"{run_id}.json")


def load_workspace_record(state: Path, workspace_id: str) -> dict:
    if not ID_RE.fullmatch(workspace_id):
        raise MetricsError(f"invalid workspace id {workspace_id!r}")
    return _load(state / "workspaces" / f"{workspace_id}.json")


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _num(value: Any) -> float | None:
    return value if type(value) in (int, float) and value >= 0 else None


# --- tokens ----------------------------------------------------------------

def _token_block(part: dict) -> dict:
    block = {name: _count(part.get(field)) for field, name in TOKEN_NAMES.items()}
    block["cost_usd"] = _num(part.get("estimated_cost_usd"))
    return block


def run_usage(parent: dict) -> dict:
    """Lead and per-backend tokens of one parent run, kept separate and null when unobserved."""
    context = parent.get("context") if isinstance(parent.get("context"), dict) else {}
    lead = context.get("lead_usage") if isinstance(context.get("lead_usage"), dict) else {}
    workers = context.get("worker_usage") if isinstance(context.get("worker_usage"), dict) else {}
    lead_block = _token_block(lead)
    lead_block["model"] = lead.get("model") if isinstance(lead.get("model"), str) else None
    by_backend = {}
    for backend in sorted(workers):
        entry = workers[backend]
        if isinstance(entry, dict):
            block = _token_block(entry)
            block["invocations"] = _count(entry.get("invocations"))
            by_backend[backend] = block
    return {"lead": lead_block, "workers": by_backend}


def spend(block: dict) -> tuple[int, bool]:
    """Input + output + cache-write of one participant, and whether all three were observed."""
    values = [block.get(name) for name in ("input", "output", "cache_write")]
    return sum(v for v in values if v is not None), all(v is not None for v in values)


def _sum_known(values: list) -> int | float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


# --- session JSONL ---------------------------------------------------------

def _tool_calls(node: Any, found: dict, run_id: str | None = None, run_filter: str | None = None):
    """Collect orchestration_record tool calls from one decoded session entry, keyed to dedupe repeats.

    Calls are attributed to `run_id`, the run whose lifecycle entry most recently preceded them; with
    `run_filter` set, calls of other runs are ignored.
    """
    if isinstance(node, dict):
        if node.get("name") == "orchestration_record" and node.get("type") in {"toolCall", "tool_use", "tool_call"}:
            if run_filter is not None and run_id != run_filter:
                return
            args = next((node[k] for k in ("arguments", "input", "args", "params") if isinstance(node.get(k), dict)), {})
            key = node.get("id") or f"anon-{len(found)}"
            found.setdefault(key, args)
            return
        for value in node.values():
            _tool_calls(value, found, run_id, run_filter)
    elif isinstance(node, list):
        for value in node:
            _tool_calls(value, found, run_id, run_filter)


def session_files(state: Path, run_id: str, extra_dirs: list[Path]) -> list[Path]:
    """Session JSONL files whose custom lifecycle entry names this run."""
    dirs = [*sorted((state / "executions").glob("*/sessions")), *extra_dirs]
    matches = []
    for directory in dirs:
        for file in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
            try:
                for line in file.read_text(encoding="utf-8").splitlines():
                    if run_id in line and SESSION_ENTRY in line:
                        entry = json.loads(line)
                        if (entry.get("type") == "custom" and entry.get("customType") == SESSION_ENTRY
                                and (entry.get("data") or {}).get("run_id") == run_id):
                            matches.append(file)
                            break
            except (OSError, ValueError):
                continue
    return matches


def session_calls(files: list[Path], run_id: str) -> list[dict] | None:
    """Arguments of the `orchestration_record` calls the lead made for `run_id`; None without a session.

    A session may host several runs: only calls after that run's lifecycle entry (and before another
    run's entry) are counted. Calls before any lifecycle entry belong to no run and are ignored.
    """
    if not files:
        return None
    found: dict = {}
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        current = None
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "custom" and entry.get("customType") == SESSION_ENTRY:
                current = (entry.get("data") or {}).get("run_id", current)
                continue
            _tool_calls(entry, found, current, run_id)
    return list(found.values())


def orchestration_record_calls(calls: list[dict] | None) -> dict | None:
    """Calls by action (events also by kind); None when there is no session."""
    if calls is None:
        return None
    by_action: dict[str, int] = {}
    event_kinds: dict[str, int] = {}
    for args in calls:
        action = args.get("action") if isinstance(args.get("action"), str) else "unknown"
        by_action[action] = by_action.get(action, 0) + 1
        if action == "event" and isinstance(args.get("event"), dict):
            kind = str(args["event"].get("kind"))
            event_kinds[kind] = event_kinds.get(kind, 0) + 1
    return {"total": sum(by_action.values()), "by_action": dict(sorted(by_action.items())),
            "event_kinds": dict(sorted(event_kinds.items()))}


def session_binds(calls: list[dict] | None, phase_ids: list[str]) -> dict | None:
    """Binds of the `bind_workspace` calls: `resolved` (task, step) pairs and `unresolved` ones.

    A missing task_id means the sole routed task. With several phases it cannot be attributed: the bind is
    listed as unresolved (its step and its order among the bind calls) and is never part of the fact diff.
    """
    if calls is None:
        return None
    resolved, unresolved = set(), []
    order = 0
    for args in calls:
        if args.get("action") == "bind_workspace" and isinstance(args.get("step_id"), str):
            order += 1
            task = args.get("task_id")
            if task:
                resolved.add((str(task), args["step_id"]))
            elif len(phase_ids) == 1:
                resolved.add((phase_ids[0], args["step_id"]))
            else:
                unresolved.append({"step": args["step_id"], "order": order})
    return {"resolved": sorted(resolved), "unresolved": unresolved}


# --- provenance facts ------------------------------------------------------

def _fact(category: str, key: list, value: dict) -> dict:
    return {"category": category, "key": [str(k) for k in key], "value": value}


def provenance_facts(record: dict, interaction_types: tuple[str, ...] = (),
                     session_bind_keys: list[tuple[str, str]] | None = None) -> list[dict]:
    """Phase, worker, wave, bind, integrate and finish facts of a run record, each with a semantic key.

    Keys are identities (phase id, phase/step/epoch, a wave's task set and route epoch), never positions or
    ids that a lead or the extension chose, so order and naming do not matter. Waves with the same task set and
    route epoch are one wave fact (the furthest status any of them reached), with `integrate` begin/end facts
    keyed by that task set; the raw wave ids are kept as `ids` metadata, outside the compared value.
    Bind: the run record has no standalone bind event. With a lead session, binds are the
    `bind_workspace` calls in it (`session_bind_keys`, from `session_binds`); without one they are
    an optional record `binds` list plus ClaudeWorker `started` activity. Every bind is keyed by
    (task, step) only; `evidence` is metadata outside the compared value, so a bind seen through
    different evidence on the two sides is not reported missing.
    Interactions are excluded unless their type is listed in `interaction_types`.
    """
    parent = record.get("parent_orchestration") if isinstance(record.get("parent_orchestration"), dict) else {}
    facts: list[dict] = []
    for phase in parent.get("phases") or []:
        facts.append(_fact("phase", [phase.get("id")], {
            "status": phase.get("status"), "route": phase.get("route"), "blocker_code": phase.get("blocker_code"),
            "in_wave": bool(phase.get("wave_id")), "escalations": len(phase.get("escalations") or [])}))
        for worker in phase.get("workers") or []:
            facts.append(_fact("worker", [phase.get("id"), worker.get("step_id"), worker.get("route_epoch", 0)], {
                "status": worker.get("status"), "backend": worker.get("backend"), "model": worker.get("model"),
                "backend_model": worker.get("backend_model"), "blocker_code": worker.get("blocker_code")}))
    for override in parent.get("worker_overrides") or []:
        facts.append(_fact("worker", ["override", override.get("phase_id"), override.get("step_id"),
                                      override.get("route_epoch"), override.get("from_status"), override.get("to_status")],
                           {"blocker_code": override.get("blocker_code")}))
    escalations = {phase.get("id"): len(phase.get("escalations") or []) for phase in parent.get("phases") or []}
    waves: dict[tuple, dict] = {}
    for wave in parent.get("waves") or []:
        tasks = tuple(sorted(map(str, wave.get("task_ids") or [])))
        epoch = max((escalations.get(task, 0) for task in tasks), default=0)
        entry = waves.setdefault((tasks, epoch), {"ids": [], "reached": -1})
        entry["ids"].append(wave.get("id"))
        if wave.get("status") in WAVE_STATUSES:
            entry["reached"] = max(entry["reached"], WAVE_STATUSES.index(wave["status"]))
    for (tasks, epoch), entry in sorted(waves.items()):
        key = [",".join(tasks), epoch]
        facts.append({**_fact("wave", key, {"task_ids": list(tasks)}), "ids": entry["ids"]})
        if entry["reached"] >= 1:
            facts.append({**_fact("integrate", [*key, "begin"], {}), "ids": entry["ids"]})
        if entry["reached"] >= 2:
            facts.append({**_fact("integrate", [*key, "end"], {}), "ids": entry["ids"]})
    binds: dict[tuple[str, str], str] = {}
    if session_bind_keys is not None:
        binds = {key: "session" for key in session_bind_keys}
    else:
        for item in parent.get("binds") or []:
            if isinstance(item, dict):
                binds[(str(item.get("task_id")), str(item.get("step_id")))] = "record_binds"
        for entry in parent.get("activity") or []:
            if isinstance(entry, dict) and entry.get("tool") == "ClaudeWorker" and entry.get("status") == "started":
                binds[(str(entry.get("task_id")), str(entry.get("step_id")))] = "worker_started"
    for (task, step), evidence in sorted(binds.items()):
        facts.append({**_fact("bind", [task, step], {}), "evidence": evidence})
    for item in parent.get("reconciled") or []:
        facts.append(_fact("finish", ["reconciled", item.get("kind"), item.get("phase_id"), item.get("step_id"),
                                      item.get("route_epoch")], {"from_status": item.get("from_status"), "to": item.get("to")}))
    facts.append(_fact("finish", ["run"], {"final_status": parent.get("final_status"),
                                           "blocker_code": parent.get("blocker_code"), "outcome": record.get("outcome")}))
    for item in parent.get("interactions") or []:
        if isinstance(item, dict) and item.get("type") in interaction_types:
            facts.append(_fact("interaction", [item.get("type"), item.get("task_id"), item.get("step_id"), item.get("summary")], {}))
    return facts


def bind_source(parent: dict, session: dict | None) -> str:
    """Where a run's bind facts come from: `session`, `record` (a `binds` list) or `activity` (ClaudeWorker starts)."""
    if session is not None:
        return "session"
    return "record" if parent.get("binds") else "activity"


def diff_facts(before: list[dict], after: list[dict]) -> list[dict]:
    """Facts of `before` whose counterpart is missing from `after`, by semantic identity and multiplicity.

    `absent`: no fact with that category and key in `after`. `differs`: the key exists but none of its
    values equals this fact's value. `count`: the value exists in `after` but fewer times than in `before`
    (a duplicated fact on the other side, or one lost out of several with the same key and value; the entry
    carries `expected` and `found`). Order never matters; facts present only in `after` are not reported.
    """
    def ident(fact: dict) -> tuple:
        return (fact["category"], tuple(fact["key"]))

    def value_id(fact: dict) -> str:
        return json.dumps(fact["value"], sort_keys=True, default=str)

    values: dict[tuple, list] = {}
    counts: dict[tuple, int] = {}
    for fact in after:
        values.setdefault(ident(fact), []).append(fact["value"])
        counts[(ident(fact), value_id(fact))] = counts.get((ident(fact), value_id(fact)), 0) + 1
    expected: dict[tuple, int] = {}
    for fact in before:
        expected[(ident(fact), value_id(fact))] = expected.get((ident(fact), value_id(fact)), 0) + 1
    missing, counted = [], set()
    for fact in before:
        found = counts.get((ident(fact), value_id(fact)), 0)
        if ident(fact) not in values:
            missing.append({**fact, "reason": "absent"})
        elif found == 0:
            missing.append({**fact, "reason": "differs", "after": values[ident(fact)][0]})
        elif found < expected[(ident(fact), value_id(fact))] and (ident(fact), value_id(fact)) not in counted:
            counted.add((ident(fact), value_id(fact)))
            missing.append({**fact, "reason": "count", "expected": expected[(ident(fact), value_id(fact))], "found": found})
    return sorted(missing, key=lambda f: (f["category"], f["key"], f["reason"]))


# Phases a lead recorded only as bookkeeping. Anything else, such as `routing`, can carry a blocker or an outcome.
OPTIONAL_PHASE_IDS = frozenset({"inspection"})


def split_dropped_optional(before_facts: list[dict], missing: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate phases that carried no route, no worker and no wave (like `inspection`) from the missing facts.

    Only phases on the allow-list `OPTIONAL_PHASE_IDS` count, and only when they were `completed` with no
    blocker code, route, worker, wave or escalation. They stay listed as dropped optional phases rather than
    lost evidence; every other phase (a blocked `routing` phase, say) stays missing.
    """
    with_workers = {f["key"][0] for f in before_facts if f["category"] == "worker" and f["key"][0] != "override"}
    dropped, lost = [], []
    for fact in missing:
        value = fact["value"]
        optional = (fact["category"] == "phase" and fact["reason"] == "absent" and fact["key"][0] in OPTIONAL_PHASE_IDS
                    and value.get("status") == "completed" and not value.get("blocker_code")
                    and not value.get("route") and not value.get("in_wave") and not value.get("escalations")
                    and fact["key"][0] not in with_workers)
        (dropped if optional else lost).append(fact)
    return dropped, lost


def provenance_diff(before: dict, after: dict, interaction_types: tuple[str, ...] = ()) -> list[dict]:
    """Every phase, worker, wave, bind, integrate and finish fact in the BEFORE run record missing from AFTER."""
    return diff_facts(provenance_facts(before, interaction_types), provenance_facts(after, interaction_types))


# --- per-run and aggregate reports -----------------------------------------

def _instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(runs: list[dict]) -> float | None:
    """Earliest start to latest end across finished runs: the elapsed wall clock of the whole benchmark."""
    starts = [t for t in (_instant(r.get("started_at")) for r in runs) if t]
    ends = [t for t in (_instant(r.get("ended_at")) for r in runs) if t]
    return round((max(ends) - min(starts)).total_seconds(), 3) if starts and ends and len(ends) == len(runs) else None


def _duration(record: dict) -> float | None:
    try:
        start = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(record["ended_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def run_report(record: dict, *, state: Path | None = None, session_dirs: list[Path] | None = None,
               task: str | None = None, entry: dict | None = None) -> dict:
    parent = record.get("parent_orchestration") if isinstance(record.get("parent_orchestration"), dict) else {}
    context = parent.get("context") if isinstance(parent.get("context"), dict) else {}
    used = run_usage(parent)
    participants = [used["lead"], *used["workers"].values()]
    spent = [spend(p) for p in participants]
    cost, unpriced = usage.cost_coverage(context)
    lead_calls = context.get("lead_calls")
    files = session_files(state, record.get("run_id", ""), session_dirs or []) if state is not None else []
    calls = session_calls(files, record.get("run_id", ""))
    phase_ids = [str(ph.get("id")) for ph in parent.get("phases") or [] if isinstance(ph, dict)]
    binds = session_binds(calls, phase_ids)
    observed = (entry or {}).get("observed") if isinstance((entry or {}).get("observed"), dict) else {}
    checks = observed.get("checks") if isinstance(observed.get("checks"), list) else None
    validation = observed.get("validation") if isinstance(observed.get("validation"), dict) else {}
    checks_passed = None
    if checks:
        checks_passed = all(isinstance(c, dict) and c.get("status") == "passed" for c in checks)
        if validation.get("status") not in (None, "passed"):
            checks_passed = False
    status = parent.get("final_status")
    return {
        "run_id": record.get("run_id"), "task": task,
        "tokens": used,
        "spend_tokens": sum(s for s, _ in spent), "spend_complete": all(c for _, c in spent),
        "calls": {"lead": len(lead_calls) if isinstance(lead_calls, list) else None,
                  **({"lead_limit": {key: context["lead_call_limit"].get(key) for key in (
                      "max_lead_calls", "source", "warn_at", "warned_at_call", "calls_at_limit", "stopped", "stopped_reason")}}
                     if isinstance(context.get("lead_call_limit"), dict) else {}),
                  "orchestration_record": orchestration_record_calls(calls),
                  "workers": {b: v["invocations"] for b, v in used["workers"].items()}},
        "wall_clock_seconds": _duration(record),
        "started_at": record.get("started_at"), "ended_at": record.get("ended_at"),
        "cost": {"estimated_usd": cost, "cost_complete": not unpriced, "unpriced": unpriced},
        "outcome": {"final_status": status, "blocker_code": parent.get("blocker_code"),
                    "checks_passed": checks_passed,
                    "checks": [{"id": c.get("id"), "status": c.get("status")} for c in checks or [] if isinstance(c, dict)],
                    "verified": status == "COMPLETED" and checks_passed is True},
        "bind_source": bind_source(parent, binds),
        "unresolved_binds": binds["unresolved"] if binds else [],
        "provenance_facts": provenance_facts(record, session_bind_keys=binds["resolved"] if binds else None),
    }


def aggregate(label: str, runs: list[dict], *, workspace_id: str | None = None, qualification: list | None = None) -> dict:
    token_total: dict[str, Any] = {"lead": {}, "workers": {}}
    for name in (*TOKEN_NAMES.values(), "cost_usd"):
        token_total["lead"][name] = _sum_known([r["tokens"]["lead"][name] for r in runs])
    backends = sorted({b for r in runs for b in r["tokens"]["workers"]})
    for backend in backends:
        parts = [r["tokens"]["workers"][backend] for r in runs if backend in r["tokens"]["workers"]]
        token_total["workers"][backend] = {name: _sum_known([p[name] for p in parts])
                                           for name in (*TOKEN_NAMES.values(), "cost_usd", "invocations")}
    orch = [r["calls"]["orchestration_record"] for r in runs]
    orch_total = None
    if orch and all(o is not None for o in orch):
        by_action: dict[str, int] = {}
        for o in orch:
            for action, n in o["by_action"].items():
                by_action[action] = by_action.get(action, 0) + n
        orch_total = {"total": sum(o["total"] for o in orch), "by_action": dict(sorted(by_action.items()))}
    worker_calls = {b: _sum_known([r["calls"]["workers"].get(b) for r in runs]) for b in backends}
    unpriced = sorted({n for r in runs for n in r["cost"]["unpriced"]})
    costs = [r["cost"]["estimated_usd"] for r in runs]
    durations = [r["wall_clock_seconds"] for r in runs]
    verified = [r for r in runs if r["outcome"]["verified"]]
    spend_tokens = sum(r["spend_tokens"] for r in runs)
    return {
        "report_version": REPORT_VERSION, "label": label, "workspace_id": workspace_id,
        "runs": runs,
        "totals": {
            "tokens": token_total, "spend_tokens": spend_tokens,
            "spend_complete": bool(runs) and all(r["spend_complete"] for r in runs),
            "calls": {"lead": _sum_known([r["calls"]["lead"] for r in runs]), "orchestration_record": orch_total,
                      "workers": worker_calls},
            "run_seconds_sum": round(sum(d for d in durations if d is not None), 3) if any(d is not None for d in durations) else None,
            "wall_clock_span_seconds": _span(runs),
            "cost": {"estimated_usd": round(sum(c for c in costs if c is not None), 6) if any(c is not None for c in costs) else None,
                     "cost_complete": not unpriced, "unpriced": unpriced},
            "tasks": {"runs": len(runs), "completed": sum(r["outcome"]["final_status"] == "COMPLETED" for r in runs),
                      "verified": len(verified), "qualification": qualification or []},
        },
        "headline": {
            "metric": "tokens per verified task (input + output + cache_write)",
            "verified_tasks": len(verified),
            "value": round(spend_tokens / len(verified), 3) if verified else NA,
            "spend_complete": bool(runs) and all(r["spend_complete"] for r in runs),
        },
    }


def _qualification(record: dict) -> list:
    out = []
    for task_id, entry in sorted((record.get("tasks") or {}).items()):
        observed = (entry or {}).get("observed") or {}
        if (entry or {}).get("run_id") is None and (observed.get("checks") or observed.get("validation")):
            out.append({"task": task_id, "validation": (observed.get("validation") or {}).get("status")})
    return out


def workspace_report(state: Path, workspace_id: str, session_dirs: list[Path] | None = None, label: str | None = None) -> dict:
    record = load_workspace_record(state, workspace_id)
    runs = []
    for task_id, entry in sorted((record.get("tasks") or {}).items()):
        run_id = (entry or {}).get("run_id")
        if isinstance(run_id, str):
            runs.append(run_report(load_run(state, run_id), state=state, session_dirs=session_dirs, task=task_id, entry=entry))
    return aggregate(label or workspace_id, runs, workspace_id=workspace_id, qualification=_qualification(record))


def runs_report(state: Path, run_ids: list[str], session_dirs: list[Path] | None = None, label: str | None = None) -> dict:
    runs = [run_report(load_run(state, rid), state=state, session_dirs=session_dirs) for rid in run_ids]
    return aggregate(label or ",".join(run_ids), runs)


# --- comparison ------------------------------------------------------------

def _delta(before: Any, after: Any) -> dict | None:
    if type(before) in (int, float) and type(after) in (int, float):
        change = round(after - before, 6)
        return {"before": before, "after": after, "delta": change,
                "percent": round(100.0 * change / before, 2) if before else None}
    return {"before": before, "after": after, "delta": None, "percent": None}


def provenance_completeness(before: dict, after: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(missing, bind-source mismatches, dropped optional phases) across the runs of two reports, paired by task
    (or by position when runs carry no task). Dropped optional phases are listed but do not make the run incomplete."""
    def keyed(report):
        return {(r.get("task") or f"#{i}"): r for i, r in enumerate(report["runs"])}
    b, a = keyed(before), keyed(after)
    missing = []
    mismatched = []
    dropped = []
    for task, run in sorted(b.items()):
        counterpart = a.get(task)
        if counterpart is None:
            missing.append({"task": task, "category": "run", "key": [task], "value": {}, "reason": "absent"})
            continue
        before_facts, after_facts = run["provenance_facts"], counterpart["provenance_facts"]
        sources = (run.get("bind_source"), counterpart.get("bind_source"))
        if sources[0] != sources[1]:
            # Binds observed through different sources cannot be compared: flag it, never report false "missing".
            mismatched.append({"task": task, "before": sources[0], "after": sources[1]})
            before_facts = [f for f in before_facts if f["category"] != "bind"]
            after_facts = [f for f in after_facts if f["category"] != "bind"]
        gone, lost = split_dropped_optional(before_facts, diff_facts(before_facts, after_facts))
        missing.extend({"task": task, **fact} for fact in lost)
        dropped.extend({"task": task, **fact} for fact in gone)
    return missing, mismatched, dropped


def unresolved_binds(report: dict) -> list[dict]:
    return [{"task": r.get("task") or f"#{i}", **item} for i, r in enumerate(report["runs"])
            for item in r.get("unresolved_binds") or []]


def compare(before: dict, after: dict) -> dict:
    bt, at = before["totals"], after["totals"]
    deltas: dict[str, Any] = {
        "headline_tokens_per_verified_task": _delta(before["headline"]["value"], after["headline"]["value"]),
        "verified_tasks": _delta(bt["tasks"]["verified"], at["tasks"]["verified"]),
        "spend_tokens": _delta(bt["spend_tokens"], at["spend_tokens"]),
        "lead_calls": _delta(bt["calls"]["lead"], at["calls"]["lead"]),
        "orchestration_record_calls": _delta((bt["calls"]["orchestration_record"] or {}).get("total"),
                                             (at["calls"]["orchestration_record"] or {}).get("total")),
        "run_seconds_sum": _delta(bt["run_seconds_sum"], at["run_seconds_sum"]),
        "wall_clock_span_seconds": _delta(bt["wall_clock_span_seconds"], at["wall_clock_span_seconds"]),
        "estimated_cost_usd": _delta(bt["cost"]["estimated_usd"], at["cost"]["estimated_usd"]),
        "lead_tokens": {n: _delta(bt["tokens"]["lead"].get(n), at["tokens"]["lead"].get(n)) for n in TOKEN_NAMES.values()},
        "worker_tokens": {b: {n: _delta(bt["tokens"]["workers"].get(b, {}).get(n), at["tokens"]["workers"].get(b, {}).get(n))
                              for n in TOKEN_NAMES.values()}
                          for b in sorted(set(bt["tokens"]["workers"]) | set(at["tokens"]["workers"]))},
    }
    missing, mismatched, dropped = provenance_completeness(before, after)
    return {"before": before, "after": after, "deltas": deltas,
            "provenance": {"complete": not missing and not mismatched, "missing": missing,
                           "dropped_optional_phases": dropped,
                           "bind_source_mismatch": bool(mismatched), "bind_sources": mismatched,
                           "unresolved_binds": {"before": unresolved_binds(before), "after": unresolved_binds(after)}}}


# --- rendering -------------------------------------------------------------

def _cell(value: Any) -> str:
    if value is None:
        return NA
    return f"{value:,}" if type(value) is int else (f"{value:,.2f}" if type(value) is float else str(value))


def render_report(report: dict) -> str:
    totals = report["totals"]
    lines = [f"Benchmark {report['label']}" + (f" (workspace {report['workspace_id']})" if report["workspace_id"] else ""),
             f"{'run':<26}{'task':<16}{'status':<11}{'checks':<8}{'lead calls':>11}{'orch calls':>11}{'spend tok':>12}{'secs':>9}"]
    for r in report["runs"]:
        orch = r["calls"]["orchestration_record"]
        checks = r["outcome"]["checks_passed"]
        lines.append(f"{str(r['run_id']):<26}{str(r['task'] or '-'):<16}{str(r['outcome']['final_status']):<11}"
                     f"{NA if checks is None else ('pass' if checks else 'FAIL'):<8}{_cell(r['calls']['lead']):>11}"
                     f"{_cell(orch and orch['total']):>11}{_cell(r['spend_tokens']):>12}{_cell(r['wall_clock_seconds']):>9}")
    lead = totals["tokens"]["lead"]
    lines.append("Tokens (input / output / cache_read / cache_write)")
    for name, block in [("lead", lead), *sorted(totals["tokens"]["workers"].items())]:
        lines.append(f"  {name:<14}" + " / ".join(_cell(block.get(n)) for n in TOKEN_NAMES.values()))
    orch = totals["calls"]["orchestration_record"]
    lines.append(f"Calls: lead={_cell(totals['calls']['lead'])} orchestration_record={_cell(orch and orch['total'])}"
                 + (" " + json.dumps(orch["by_action"]) if orch else "") + f" workers={json.dumps(totals['calls']['workers'])}")
    cost = totals["cost"]
    lines.append(f"Duration: run_seconds_sum={_cell(totals['run_seconds_sum'])} s, "
                 f"wall_clock_span_seconds={_cell(totals['wall_clock_span_seconds'])} s (first start to last end)")
    lines.append(f"Cost: {_cell(cost['estimated_usd'])} USD, cost_complete={cost['cost_complete']}, unpriced={cost['unpriced'] or 'none'}")
    tasks = totals["tasks"]
    lines.append(f"Tasks: {tasks['completed']}/{tasks['runs']} completed, {tasks['verified']} verified")
    head = report["headline"]
    lines.append(f"HEADLINE tokens per verified task: {_cell(head['value'])}"
                 + ("" if head["spend_complete"] else " (some token counts unobserved: lower bound)"))
    return "\n".join(lines)


def render_compare(result: dict) -> str:
    d = result["deltas"]
    lines = [render_report(result["before"]), "", render_report(result["after"]), "", "Deltas (after - before)"]
    for name in ("headline_tokens_per_verified_task", "verified_tasks", "spend_tokens", "lead_calls",
                 "orchestration_record_calls", "run_seconds_sum", "wall_clock_span_seconds", "estimated_cost_usd"):
        item = d[name]
        pct = "" if item["percent"] is None else f" ({item['percent']:+.1f}%)"
        change = NA if item["delta"] is None else f"{item['delta']:+,}"
        lines.append(f"  {name:<36}{_cell(item['before']):>12} -> {_cell(item['after']):>12}  {change}{pct}")
    prov = result["provenance"]
    lines.append(f"Provenance completeness: {'COMPLETE' if prov['complete'] else 'INCOMPLETE'} "
                 f"({len(prov['missing'])} BEFORE fact(s) missing from AFTER)")
    for m in prov["missing"]:
        lines.append(f"  {m.get('task')}: {m['category']} {'/'.join(m['key'])} ({m['reason']})")
    if prov["dropped_optional_phases"]:
        lines.append("Dropped optional phases (no route, workers or wave in BEFORE; listed, not counted as missing)")
        for m in prov["dropped_optional_phases"]:
            lines.append(f"  {m.get('task')}: phase {m['key'][0]} ({m['value'].get('status')})")
    if prov["bind_source_mismatch"]:
        lines.append("  bind_source_mismatch: binds were observed through different sources, so they are not compared:")
        for item in prov["bind_sources"]:
            lines.append(f"    {item['task']}: before={item['before']} after={item['after']}")
    unresolved = prov["unresolved_binds"]
    if unresolved["before"] or unresolved["after"]:
        lines.append("Unresolved binds (bind_workspace without task_id in a run with several phases; not diffed)")
        for side in ("before", "after"):
            for item in unresolved[side]:
                lines.append(f"  {side} {item['task']}: step {item['step']} (bind call #{item['order']})")
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------

def load_side(spec: str, default_state: Path | None, session_dirs: list[Path]) -> dict:
    path = Path(spec)
    if path.is_file():
        report = _load(path)
        if report.get("report_version") != REPORT_VERSION or "totals" not in report:
            raise MetricsError(f"{spec} is not a benchmark_metrics report")
        return report
    state_text, sep, ident = spec.rpartition("@")
    state = Path(state_text) if sep else default_state
    if state is None or not ident:
        raise MetricsError(f"cannot read {spec!r}: use a report file or STATE_DIR@WORKSPACE_ID|RUN_ID[,RUN_ID]")
    state = state.expanduser()
    if (state / "workspaces" / f"{ident}.json").is_file():
        return workspace_report(state, ident, session_dirs, label=spec)
    return runs_report(state, ident.split(","), session_dirs, label=spec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Token, call, duration, cost, outcome and provenance metrics for PICO runs.")
    parser.add_argument("run_ids", nargs="*", help="run ids (or use --workspace)")
    parser.add_argument("--state-dir", type=Path, default=os.environ.get("PI_ORCHESTRATOR_STATE_DIR"),
                        help="orchestrator state directory (default: $PI_ORCHESTRATOR_STATE_DIR)")
    parser.add_argument("--workspace", help="workspace id: all child runs linked from its record")
    parser.add_argument("--session-dir", type=Path, action="append", default=[],
                        help="extra directory of lead session JSONL files (executions' own session dirs are found automatically)")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="report file or STATE_DIR@ID for each side; prints both, deltas and the provenance diff")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    parser.add_argument("--output", type=Path, help="also write the JSON to this file (reusable as a --compare side)")
    args = parser.parse_args(argv)
    state = Path(args.state_dir) if args.state_dir else None
    try:
        if args.compare:
            result = compare(load_side(args.compare[0], state, args.session_dir), load_side(args.compare[1], state, args.session_dir))
            text = render_compare(result)
            exit_code = 0 if result["provenance"]["complete"] else 1
        else:
            if state is None:
                raise MetricsError("--state-dir (or PI_ORCHESTRATOR_STATE_DIR) is required")
            if bool(args.workspace) == bool(args.run_ids):
                raise MetricsError("give either --workspace ID or one or more run ids")
            result = (workspace_report(state, args.workspace, args.session_dir) if args.workspace
                      else runs_report(state, args.run_ids, args.session_dir))
            text = render_report(result)
            exit_code = 0
    except MetricsError as exc:
        print(f"benchmark_metrics: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print(text)
        print()
        print(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
