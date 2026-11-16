#!/usr/bin/env python3
"""Durable aggregate admission accounting; no provider interception is implied.

Limits are pinned in the ledger at creation, not supplied by each caller. All
mutations share a process-safe lock and atomic fsynced replacement. Uncertain
reservations never expire. The workspace adapter enforces launch counts only;
provider request/token/money limits require an adapter at every provider request.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
from pathlib import Path
import uuid

import atomic_io

ENV = "PICO_AGGREGATE_BUDGET_LEDGER"
SCOPES = {"task", "attempt", "day", "lifetime"}
DIMENSIONS = {"launches", "requests", "tokens", "cost_usd"}


class BudgetError(Exception):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _fail(detail, code="AGGREGATE_BUDGET_INVALID"):
    raise BudgetError(code, detail)


def _amounts(values):
    if not isinstance(values, dict) or not values or set(values) - DIMENSIONS:
        _fail("amounts must name known nonempty dimensions")
    result = {}
    for key, value in values.items():
        if key == "cost_usd":
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                _fail("cost_usd must be a finite nonnegative decimal")
            if not number.is_finite() or number < 0:
                _fail("cost_usd must be a finite nonnegative decimal")
            result[key] = str(number)
        elif type(value) is not int or value < 0:
            _fail(f"{key} must be a nonnegative integer")
        else:
            result[key] = value
    return result


def _limits(values):
    if not isinstance(values, dict) or not values or set(values) - SCOPES:
        _fail("limits must name task, attempt, day and/or lifetime")
    return {scope: _amounts(limits) for scope, limits in values.items()}


def _day():
    return datetime.now(timezone.utc).date().isoformat()


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _fail("operation, task and attempt ids must be nonempty strings up to 512 characters")
    return value


@contextlib.contextmanager
def _locked(path):
    path = Path(path).absolute()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.check_no_symlinks(path, path.parent)
        lock_path = path.with_suffix(path.suffix + ".lock")
        atomic_io.check_no_symlinks(lock_path, path.parent)
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError as exc:
        raise BudgetError("AGGREGATE_BUDGET_UNREADABLE", f"cannot access ledger {path}: {exc}") from exc


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            _fail("unsupported ledger")
        validated_limits = _limits(data["limits"])
        required = set().union(*(set(limits) for limits in validated_limits.values()))
        if not isinstance(data["reservations"], dict):
            _fail("invalid reservations")
        for operation, entry in data["reservations"].items():
            _identity(operation)
            _identity(entry["task_id"])
            _identity(entry["attempt_id"])
            if entry["state"] not in {"reserved", "uncertain", "settled"}:
                _fail("invalid reservation state")
            for day in (entry["day"], entry.get("settled_day", entry["day"])):
                if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
                    _fail("invalid accounting day")
            _amounts(entry["reserved"])
            if required - set(entry["reserved"]):
                _fail("reservation is missing configured dimensions")
            if entry["state"] == "settled":
                _amounts(entry["actual"])
                if set(entry["actual"]) != set(entry["reserved"]):
                    _fail("incomplete settled amounts")
        return data
    except (OSError, ValueError, TypeError, KeyError, BudgetError) as exc:
        raise BudgetError("AGGREGATE_BUDGET_UNREADABLE", f"cannot trust ledger {path}: {exc}") from exc


def _save(path, data):
    atomic_io.atomic_write_json(path, data, indent=2, sort_keys=True, allow_nan=False)


def initialize(path, limits):
    """Explicit creation only: existing ledgers cannot be reset by this API."""
    limits = _limits(limits)
    with _locked(path) as path:
        if path.exists():
            _fail("ledger already exists; refusing to reset allowance")
        data = {"version": 1, "limits": limits, "reservations": {}}
        _save(path, data)
        return data


def inspect(path):
    with _locked(path) as path:
        return _load(path)


def _matches(scope, entry, task_id, attempt_id, day):
    if scope == "task":
        return entry["task_id"] == task_id
    if scope == "attempt":
        return entry["task_id"] == task_id and entry["attempt_id"] == attempt_id
    if scope == "day":
        # A request still in flight may spend today even if admitted yesterday.
        # Cross-day settlements count on both days, conservatively.
        return entry["state"] != "settled" or day in {entry["day"], entry.get("settled_day")}
    return True


def reserve(path, *, operation_id, task_id, attempt_id, maximum, supported=None):
    """Reserve an adapter-enforced upper bound before an external operation.

    Returns (reservation, created). Only created=True authorizes execution.
    Repeated ids reconcile without authorizing a second request. `supported`
    lists dimensions the adapter can actually bound, not merely estimate.
    """
    operation_id, task_id, attempt_id = map(_identity, (operation_id, task_id, attempt_id))
    maximum = _amounts(maximum)
    supported = set(supported or ())
    with _locked(path) as path:
        data = _load(path)
        required = set().union(*(set(limits) for limits in data["limits"].values()))
        if required - supported or required - set(maximum):
            _fail("adapter cannot enforce configured dimensions: " + ", ".join(sorted(
                (required - supported) | (required - set(maximum)))), "AGGREGATE_BUDGET_UNSUPPORTED")
        prior = data["reservations"].get(operation_id)
        if prior:
            if (prior["task_id"], prior["attempt_id"], prior["reserved"]) != (task_id, attempt_id, maximum):
                _fail("operation id already has different reservation", "AGGREGATE_BUDGET_CONFLICT")
            return prior, False
        day = _day()
        for scope, limits in data["limits"].items():
            entries = [entry for entry in data["reservations"].values()
                       if _matches(scope, entry, task_id, attempt_id, day)]
            for dimension, limit in limits.items():
                used = sum((Decimal(str((e["actual"] if e["state"] == "settled" else e["reserved"])[dimension]))
                            for e in entries), Decimal(0))
                if used + Decimal(str(maximum[dimension])) > Decimal(str(limit)):
                    _fail(f"{scope} {dimension}: {used} + {maximum[dimension]} exceeds {limit}",
                          "AGGREGATE_BUDGET_EXHAUSTED")
        entry = {"task_id": task_id, "attempt_id": attempt_id, "day": day,
                 "reserved": maximum, "state": "reserved"}
        data["reservations"][operation_id] = entry
        _save(path, data)
        return entry, True


def settle(path, operation_id, *, actual=None, complete=False):
    """Settle complete measured quantities, or retain the entire uncertain bound.

    Incomplete provider telemetry must use complete=False; partial values never
    release capacity. A measured overrun is recorded truthfully and blocks later
    admission through the totals; it is not silently clamped to the reservation.
    """
    with _locked(path) as path:
        data = _load(path)
        entry = data["reservations"].get(operation_id)
        if entry is None:
            _fail("unknown reservation")
        measured = _amounts(actual) if complete else None
        if complete and set(measured) != set(entry["reserved"]):
            _fail("settlement must cover every reserved dimension")
        if entry["state"] == "settled":
            if not complete or measured != entry["actual"]:
                _fail("settled reservation cannot be rewritten", "AGGREGATE_BUDGET_CONFLICT")
            return entry
        if complete:
            entry.update(state="settled", actual=measured, settled_day=_day(),
                         overrun=any(Decimal(str(value)) > Decimal(str(entry["reserved"][key]))
                                     for key, value in measured.items()))
        else:
            entry["state"] = "uncertain"
        _save(path, data)
        return entry


def reserve_launch(path, *, task_id, attempt_id, operation_id=None):
    """Charge one attempted launch. Provider ceilings are refused at this boundary.

    Every resume/new attempt charges again. A repeated/no-op headless start must
    not call this function. An uncertain/crashed launch retains its charge.
    """
    operation_id = operation_id or str(uuid.uuid4())
    _, created = reserve(path, operation_id=operation_id, task_id=task_id, attempt_id=attempt_id,
                         maximum={"launches": 1}, supported={"launches"})
    if not created:
        _fail("launch admission was already consumed", "AGGREGATE_BUDGET_REPLAY")
    return operation_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("init", help="create a new ledger; never resets an existing ledger")
    create.add_argument("path", type=Path)
    create.add_argument("--limits", required=True, help='JSON, e.g. {"day":{"launches":4},"lifetime":{"launches":20}}')
    show = sub.add_parser("status")
    show.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = initialize(args.path, json.loads(args.limits)) if args.command == "init" else inspect(args.path)
    except (BudgetError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error_code": getattr(exc, "code", "AGGREGATE_BUDGET_INVALID"), "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
