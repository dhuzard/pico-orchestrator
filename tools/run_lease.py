#!/usr/bin/env python3
"""Single-owner lease on one orchestration run (audit A3).

A run may be continued, resumed or dispatched by exactly one live process. The
lease names that process: an owner id chosen by the caller, its pid and host,
when it was taken, the last heartbeat and the expiry. It lives in orchestrator
state next to the run records (``<state>/leases/<run_id>.json``, or
``provenance/leases/`` for a repository-local installation), never in the
target checkout.

Every decision is a read-modify-write under an exclusive ``flock`` on
``<run_id>.lock`` with a bounded wait, and every write is an atomic replace
(``tools/atomic_io.py``), so two processes can never both observe "free" and
both take the lease.

Rules:

* ``acquire`` takes a free lease, or renews one this owner already holds
  (same owner id; the pid may change, which is how a launcher hands its lease
  to the process it starts). A live lease of another owner fails closed with
  ``RUN_LEASE_HELD``; a stale one with ``RUN_LEASE_STALE``. Acquire never
  waits for a holder and never takes a lease over.
* A lease is stale when its heartbeat expired, when its pid is dead on this
  host, or when the lease file is unreadable. Staleness is only reported.
* ``takeover`` is the only way to recover a stale lease. It records the
  previous holder and the reason in the run's provenance
  (``parent_orchestration.lease_takeovers``) before replacing the lease, and
  refuses a live lease (``RUN_LEASE_HELD``) or a run whose record cannot be
  updated (``RUN_LEASE_PROVENANCE_UNAVAILABLE``).
* ``heartbeat`` renews only the caller's own lease (``RUN_LEASE_LOST`` when
  another owner holds it, ``RUN_LEASE_NOT_HELD`` when there is none).
* ``release`` removes only the caller's own lease. ``release_for_outcome``
  removes whatever lease a run has once the run reached an explicit outcome
  (``tools/parent_orchestration.py`` calls it from ``finish``).

CLI (JSON on stdout; exit 0 ok, 1 refused, 2 invalid invocation)::

  run_lease.py acquire   RUN_ID --owner ID [--pid N] [--ttl SECONDS]
  run_lease.py heartbeat RUN_ID --owner ID [--pid N] [--ttl SECONDS]
  run_lease.py release   RUN_ID --owner ID
  run_lease.py status    RUN_ID
  run_lease.py takeover  RUN_ID --owner ID --reason CODE [--pid N] [--ttl SECONDS]

``--pid`` defaults to the calling process (the parent of this CLI process).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402
import provenance  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LEASE_VERSION = 1
DEFAULT_TTL_SECONDS = 300
MIN_TTL_SECONDS = 5
MAX_TTL_SECONDS = 24 * 3600
LOCK_WAIT_SECONDS = 5
MAX_TAKEOVERS = 50
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
HOST_RE = re.compile(r"[^A-Za-z0-9_.-]")

# Stable error codes.
HELD = "RUN_LEASE_HELD"
STALE = "RUN_LEASE_STALE"
NOT_HELD = "RUN_LEASE_NOT_HELD"
LOST = "RUN_LEASE_LOST"
INVALID = "RUN_LEASE_INVALID"
LOCK_TIMEOUT = "RUN_LEASE_LOCK_TIMEOUT"
PROVENANCE_UNAVAILABLE = "RUN_LEASE_PROVENANCE_UNAVAILABLE"
# Stale reasons.
HEARTBEAT_EXPIRED = "HEARTBEAT_EXPIRED"
OWNER_PROCESS_DEAD = "OWNER_PROCESS_DEAD"
LEASE_UNREADABLE = "LEASE_UNREADABLE"


class LeaseError(Exception):
    def __init__(self, code: str, detail: str, *, lease: dict | None = None, state: str | None = None,
                 stale_reason: str | None = None):
        self.code = code
        self.detail = detail
        self.lease = lease
        self.state = state
        self.stale_reason = stale_reason
        super().__init__(f"{code}: {detail}")

    def payload(self) -> dict:
        return {"ok": False, "error_code": self.code, "error": self.detail, "state": self.state,
                "stale_reason": self.stale_reason, "lease": self.lease}


def host_name() -> str:
    return HOST_RE.sub("-", socket.gethostname() or "unknown")[:64] or "unknown"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def lease_dir(repo_root: Path = ROOT) -> Path:
    """Leases sit beside the run records in orchestrator state, never in a target checkout."""
    return provenance.runs_dir(repo_root).parent / "leases"


def lease_path(run_id: str, repo_root: Path = ROOT, *, directory: Path | None = None) -> Path:
    """`directory` names a lease directory directly (a status surface that already knows its runs directory)."""
    try:
        provenance.validate_run_id(run_id)
    except provenance.ProvenanceError as exc:
        raise LeaseError(INVALID, "run id must be filename-safe") from exc
    return (directory if directory is not None else lease_dir(repo_root)) / f"{run_id}.json"


def _owner(owner_id: Any) -> str:
    if not isinstance(owner_id, str) or not OWNER_RE.fullmatch(owner_id):
        raise LeaseError(INVALID, "owner id must match " + OWNER_RE.pattern)
    return owner_id


def _pid(pid: Any) -> int:
    if pid is None:
        return os.getpid()
    if type(pid) is not int or pid <= 0:
        raise LeaseError(INVALID, "pid must be a positive integer")
    return pid


def _ttl(ttl_seconds: Any) -> int:
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    if type(ttl) is not int or not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise LeaseError(INVALID, f"ttl must be an integer between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds")
    return ttl


def pid_alive(pid: Any) -> bool:
    """Signal-0 probe. Never reaps: a lease may name a child of the caller."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _locked(run_id: str, repo_root: Path):
    path = lease_path(run_id, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+b") as handle:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - non-POSIX
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LeaseError(LOCK_TIMEOUT, f"lease of run {run_id!r} is busy") from exc
                time.sleep(0.02)
        try:
            yield path
        finally:
            if os.name == "nt":  # pragma: no cover - non-POSIX
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_UNREADABLE = object()


def _read(path: Path) -> dict | None | object:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return _UNREADABLE
    required = {"lease_version", "run_id", "owner_id", "pid", "host", "acquired_at", "heartbeat_at", "expires_at"}
    if not isinstance(data, dict) or data.get("lease_version") != LEASE_VERSION or not required <= set(data):
        return _UNREADABLE
    return data


def _write(path: Path, lease: dict) -> None:
    atomic_io.atomic_write_json(path, lease, indent=2, sort_keys=True, trailing_newline=True)


def assess(lease: dict | None | object, *, now: float | None = None, host: str | None = None) -> tuple[str, str | None]:
    """(state, stale_reason): state is "free", "live" or "stale"."""
    if lease is None:
        return "free", None
    if lease is _UNREADABLE or not isinstance(lease, dict):
        return "stale", LEASE_UNREADABLE
    expires = _epoch(lease.get("expires_at"))
    if expires is None or (time.time() if now is None else now) >= expires:
        return "stale", HEARTBEAT_EXPIRED
    if lease.get("host") == (host or host_name()) and not pid_alive(lease.get("pid")):
        return "stale", OWNER_PROCESS_DEAD
    return "live", None


def _public(lease: dict | None | object) -> dict | None:
    return lease if isinstance(lease, dict) else None


def _new_lease(run_id: str, owner_id: str, pid: int, ttl: int, now: float, takeovers: list | None = None) -> dict:
    return {"lease_version": LEASE_VERSION, "run_id": run_id, "owner_id": owner_id, "pid": pid, "host": host_name(),
            "acquired_at": _iso(now), "heartbeat_at": _iso(now), "expires_at": _iso(now + ttl),
            "ttl_seconds": ttl, "takeovers": list(takeovers or [])}


def _renew(lease: dict, pid: int, ttl: int, now: float) -> dict:
    lease.update({"pid": pid, "host": host_name(), "heartbeat_at": _iso(now), "expires_at": _iso(now + ttl),
                  "ttl_seconds": ttl})
    return lease


def status(run_id: str, *, repo_root: Path = ROOT, now: float | None = None, directory: Path | None = None) -> dict:
    """Read-only view: who holds the run, and whether that lease is live or stale."""
    path = lease_path(run_id, repo_root, directory=directory)
    lease = _read(path)
    state, reason = assess(lease, now=now)
    return {"ok": True, "run_id": run_id, "state": state, "stale_reason": reason, "lease": _public(lease)}


def summary(run_id: str, *, repo_root: Path = ROOT, now: float | None = None,
            directory: Path | None = None) -> dict | None:
    """Display view for status surfaces (Garden, `pico exec status`); None when there is no lease."""
    try:
        view = status(run_id, repo_root=repo_root, now=now, directory=directory)
    except (LeaseError, OSError, provenance.ProvenanceError):
        return None
    if view["state"] == "free":
        return None
    lease = view["lease"] or {}
    return {"state": view["state"], "stale_reason": view["stale_reason"],
            **{key: lease.get(key) for key in ("owner_id", "pid", "host", "acquired_at", "heartbeat_at", "expires_at")}}


def acquire(run_id: str, owner_id: str, *, pid: int | None = None, ttl_seconds: int | None = None,
            repo_root: Path = ROOT, now: float | None = None) -> dict:
    owner, pid, ttl = _owner(owner_id), _pid(pid), _ttl(ttl_seconds)
    with _locked(run_id, repo_root) as path:
        current = now if now is not None else time.time()
        lease = _read(path)
        if isinstance(lease, dict) and lease["owner_id"] == owner:
            lease = _renew(lease, pid, ttl, current)
            _write(path, lease)
            return {"ok": True, "run_id": run_id, "state": "live", "acquired": "renewed", "lease": lease}
        state, reason = assess(lease, now=current)
        if state == "live":
            raise LeaseError(HELD, f"run {run_id} is held by {lease['owner_id']} (pid {lease['pid']} on {lease['host']})",
                             lease=_public(lease), state=state)
        if state == "stale":
            raise LeaseError(STALE, f"run {run_id} has a stale lease ({reason}); recover it only with an explicit takeover",
                             lease=_public(lease), state=state, stale_reason=reason)
        lease = _new_lease(run_id, owner, pid, ttl, current)
        _write(path, lease)
        return {"ok": True, "run_id": run_id, "state": "live", "acquired": "new", "lease": lease}


def heartbeat(run_id: str, owner_id: str, *, pid: int | None = None, ttl_seconds: int | None = None,
              repo_root: Path = ROOT, now: float | None = None) -> dict:
    owner, ttl = _owner(owner_id), _ttl(ttl_seconds)
    with _locked(run_id, repo_root) as path:
        lease = _read(path)
        if lease is None:
            raise LeaseError(NOT_HELD, f"run {run_id} has no lease", state="free")
        if not isinstance(lease, dict) or lease["owner_id"] != owner:
            state, reason = assess(lease, now=now)
            raise LeaseError(LOST, f"run {run_id} is no longer held by {owner}", lease=_public(lease), state=state,
                             stale_reason=reason)
        lease = _renew(lease, _pid(pid if pid is not None else lease.get("pid")), ttl,
                       now if now is not None else time.time())
        _write(path, lease)
        return {"ok": True, "run_id": run_id, "state": "live", "lease": lease}


def release(run_id: str, owner_id: str, *, repo_root: Path = ROOT) -> dict:
    owner = _owner(owner_id)
    with _locked(run_id, repo_root) as path:
        lease = _read(path)
        if lease is None:
            return {"ok": True, "run_id": run_id, "released": False, "lease": None}
        if not isinstance(lease, dict) or lease["owner_id"] != owner:
            raise LeaseError(LOST, f"run {run_id} is not held by {owner}", lease=_public(lease))
        path.unlink()
        return {"ok": True, "run_id": run_id, "released": True, "lease": lease}


def release_for_outcome(run_id: str, *, repo_root: Path = ROOT) -> dict | None:
    """Remove any lease of a run that reached an explicit outcome; returns the removed lease."""
    with _locked(run_id, repo_root) as path:
        lease = _read(path)
        if lease is None:
            return None
        path.unlink()
        return _public(lease) or {"run_id": run_id, "owner_id": None}


def _record_takeover(run_id: str, entry: dict, repo_root: Path) -> None:
    """Append the takeover to the run's provenance; any failure refuses the takeover."""
    try:
        with provenance.run_lock(repo_root, run_id):
            record = provenance.get_run(repo_root, run_id)
            parent = record.get("parent_orchestration")
            if record.get("outcome") != "in_progress" or not isinstance(parent, dict):
                raise LeaseError(PROVENANCE_UNAVAILABLE, "only an in-progress parent run can be taken over")
            parent.setdefault("lease_takeovers", []).append(entry)
            parent["lease_takeovers"] = parent["lease_takeovers"][-MAX_TAKEOVERS:]
            provenance.update_parent_orchestration(repo_root, run_id, parent)
    except LeaseError:
        raise
    except (provenance.ProvenanceError, OSError, ValueError) as exc:
        raise LeaseError(PROVENANCE_UNAVAILABLE, f"takeover cannot be recorded in provenance: {type(exc).__name__}") from exc


def takeover(run_id: str, owner_id: str, *, reason: str, pid: int | None = None, ttl_seconds: int | None = None,
             repo_root: Path = ROOT, now: float | None = None) -> dict:
    """Recover a stale lease. Recorded in provenance first; a live lease is never taken."""
    owner, pid, ttl = _owner(owner_id), _pid(pid), _ttl(ttl_seconds)
    if not isinstance(reason, str) or not CODE_RE.fullmatch(reason):
        raise LeaseError(INVALID, "takeover reason must be an upper-case code such as OPERATOR_RECOVERY")
    with _locked(run_id, repo_root) as path:
        current = now if now is not None else time.time()
        lease = _read(path)
        state, stale_reason = assess(lease, now=current)
        if state == "live":
            raise LeaseError(HELD, f"run {run_id} is held by a live owner; a live lease is never taken over",
                             lease=_public(lease), state=state)
        if state == "free":
            raise LeaseError(NOT_HELD, f"run {run_id} has no lease to take over; acquire it instead", state=state)
        previous = lease if isinstance(lease, dict) else {}
        entry = {"at": _iso(current), "owner_id": owner, "pid": pid, "host": host_name(), "reason": reason,
                 "stale_reason": stale_reason, "previous_owner_id": previous.get("owner_id"),
                 "previous_pid": previous.get("pid") if type(previous.get("pid")) is int else None,
                 "previous_host": previous.get("host"), "previous_heartbeat_at": previous.get("heartbeat_at")}
        _record_takeover(run_id, entry, repo_root)
        history = (previous.get("takeovers") or []) if isinstance(previous.get("takeovers"), list) else []
        lease = _new_lease(run_id, owner, pid, ttl, current, takeovers=(history + [entry])[-MAX_TAKEOVERS:])
        _write(path, lease)
        return {"ok": True, "run_id": run_id, "state": "live", "acquired": "takeover", "takeover": entry,
                "lease": lease}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Single-owner run lease: acquire, heartbeat, release, status, takeover.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("acquire", "heartbeat", "release", "status", "takeover"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        if name != "status":
            p.add_argument("--owner", required=True)
        if name in {"acquire", "heartbeat", "takeover"}:
            p.add_argument("--pid", type=int, help="owner process (default: the process that runs this command)")
            p.add_argument("--ttl", type=int, help=f"seconds until the lease expires without a heartbeat (default {DEFAULT_TTL_SECONDS})")
        if name == "takeover":
            p.add_argument("--reason", required=True, help="upper-case reason code recorded in provenance")
    args = parser.parse_args(argv)
    pid = getattr(args, "pid", None)
    if pid is None and args.command in {"acquire", "takeover"}:
        pid = os.getppid()
    try:
        if args.command == "acquire":
            result = acquire(args.run_id, args.owner, pid=pid, ttl_seconds=args.ttl)
        elif args.command == "heartbeat":
            result = heartbeat(args.run_id, args.owner, pid=pid, ttl_seconds=args.ttl)
        elif args.command == "release":
            result = release(args.run_id, args.owner)
        elif args.command == "takeover":
            result = takeover(args.run_id, args.owner, reason=args.reason, pid=pid, ttl_seconds=args.ttl)
        else:
            result = status(args.run_id)
    except LeaseError as exc:
        print(json.dumps({**exc.payload(), "run_id": args.run_id, "component": "run_lease",
                          "operation": args.command}, sort_keys=True))
        return 1
    except (OSError, provenance.ProvenanceError) as exc:
        print(json.dumps({"ok": False, "error_code": "RUN_LEASE_STORAGE_UNAVAILABLE", "error": type(exc).__name__,
                          "run_id": args.run_id, "component": "run_lease", "operation": args.command}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
