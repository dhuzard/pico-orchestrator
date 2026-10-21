#!/usr/bin/env python3
"""Single-owner lease on one orchestration run (audit A3).

A run may be continued, resumed or written to by exactly one owner. The lease
names it: an owner id chosen by the caller, its pid and the pid's start time,
the host (hostname plus a machine key), when it was taken, the last heartbeat
and the expiry. It lives in orchestrator state next to the run records
(``<state>/leases/<run_id>.json``, or ``provenance/leases/`` for a
repository-local installation), never in the target checkout.

Every decision is a read-modify-write under an exclusive ``flock`` on
``<run_id>.lock`` with a bounded wait, and every write is an atomic replace
(``tools/atomic_io.py``), so two processes can never both observe "free" and
both take the lease. A crash releases the flock with the process.

Rules:

* ``acquire`` takes a free lease, or renews the caller's own (same owner id
  and pid). A different pid of the same owner is refused
  (``RUN_LEASE_PID_MISMATCH``) unless it is an explicit hand-off
  (``handoff=True``) from the launcher that holds the lease to a process it
  started. Another owner's live lease fails closed with ``RUN_LEASE_HELD``, a
  stale one with ``RUN_LEASE_STALE``. Acquire never waits and never takes over.
* Stale means: the heartbeat expired (``HEARTBEAT_EXPIRED``), the pid is dead
  or was reused on this machine (``OWNER_PROCESS_DEAD``; the recorded pid start
  time detects reuse), or the file is unreadable or implausible
  (``LEASE_UNREADABLE``: bad JSON, an expiry beyond the maximum TTL, a
  heartbeat in the future beyond clock skew). "This machine" is the hostname
  plus the kernel boot id, machine id and pid namespace when available, so
  containers that share a hostname never judge each other's pids.
* ``takeover`` is the only way to recover a stale lease. It refuses a live
  lease (``RUN_LEASE_HELD``) and, on this machine, an expired lease whose
  holder process is still alive (``RUN_LEASE_HOLDER_ALIVE``) unless a separate
  ``force_reason`` is given. It records the previous holder, the reasons and
  the new owner in the run's provenance before replacing the lease
  (``RUN_LEASE_PROVENANCE_UNAVAILABLE`` when it cannot).
* ``heartbeat`` and ``release`` act only on the caller's own lease
  (``RUN_LEASE_LOST`` when another owner holds it, ``RUN_LEASE_NOT_HELD`` when
  there is none). ``guard`` holds the lease lock around a run write and
  refuses unless the caller holds the lease, or the lease is free and the
  caller is the run's recorded owner (``tools/parent_orchestration.py``).

CLI (JSON on stdout; exit 0 ok, 1 refused, 2 invalid invocation)::

  run_lease.py acquire   RUN_ID --owner ID [--pid N] [--ttl SECONDS] [--handoff]
  run_lease.py heartbeat RUN_ID --owner ID [--pid N] [--ttl SECONDS]
  run_lease.py release   RUN_ID --owner ID
  run_lease.py status    RUN_ID
  run_lease.py takeover  RUN_ID --owner ID --reason CODE [--force-reason CODE] [--pid N] [--ttl SECONDS]

``--pid`` defaults to the calling process (the parent of this CLI process).
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import os
import re
import socket
import subprocess
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
CLOCK_SKEW_SECONDS = 120
LOCK_WAIT_SECONDS = 5
MAX_TAKEOVERS = 50
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
HOST_RE = re.compile(r"[^A-Za-z0-9_.-]")

# Stable error codes.
HELD = "RUN_LEASE_HELD"
STALE = "RUN_LEASE_STALE"
HOLDER_ALIVE = "RUN_LEASE_HOLDER_ALIVE"
NOT_HELD = "RUN_LEASE_NOT_HELD"
LOST = "RUN_LEASE_LOST"
PID_MISMATCH = "RUN_LEASE_PID_MISMATCH"
INVALID = "RUN_LEASE_INVALID"
LOCK_TIMEOUT = "RUN_LEASE_LOCK_TIMEOUT"
PROVENANCE_UNAVAILABLE = "RUN_LEASE_PROVENANCE_UNAVAILABLE"
# Stale reasons.
HEARTBEAT_EXPIRED = "HEARTBEAT_EXPIRED"
OWNER_PROCESS_DEAD = "OWNER_PROCESS_DEAD"
LEASE_UNREADABLE = "LEASE_UNREADABLE"


class LeaseError(Exception):
    def __init__(self, code: str, detail: str, *, lease: dict | None = None, state: str | None = None,
                 stale_reason: str | None = None, holder_alive: bool | None = None):
        self.code = code
        self.detail = detail
        self.lease = lease
        self.state = state
        self.stale_reason = stale_reason
        self.holder_alive = holder_alive
        super().__init__(f"{code}: {detail}")

    def payload(self) -> dict:
        return {"ok": False, "error_code": self.code, "error": self.detail, "state": self.state,
                "stale_reason": self.stale_reason, "holder_alive": self.holder_alive, "lease": self.lease}


def host_name() -> str:
    return HOST_RE.sub("-", socket.gethostname() or "unknown")[:64] or "unknown"


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


@functools.lru_cache(maxsize=1)
def host_id() -> str | None:
    """Key of this machine and pid namespace: boot id, machine id and pid namespace, when available.

    Two containers can share a hostname; they never share all three. Without
    any of them (non-Linux) only the hostname identifies the host.
    """
    try:
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        namespace = None
    parts = [_read_text("/proc/sys/kernel/random/boot_id"), _read_text("/etc/machine-id"), namespace]
    if not any(parts):
        return None
    return hashlib.sha256("\0".join(part or "" for part in parts).encode()).hexdigest()[:16]


def _proc_stat(pid: int) -> list[str] | None:
    """Fields 3.. of /proc/<pid>/stat (after the parenthesised command name); None off Linux."""
    text = _read_text(f"/proc/{pid}/stat")
    if not text or ")" not in text:
        return None
    return text.rsplit(")", 1)[1].split()


def pid_start(pid: Any) -> str | None:
    """Start time of a process in clock ticks since boot (stat field 22); None when unavailable."""
    if type(pid) is not int or pid <= 0:
        return None
    fields = _proc_stat(pid)
    return fields[19] if fields and len(fields) > 19 else None


def parent_pid(pid: int) -> int | None:
    fields = _proc_stat(pid)
    if fields and len(fields) > 1 and fields[1].isdigit():
        return int(fields[1])
    try:  # non-Linux fallback
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


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


def process_matches(pid: Any, recorded_start: Any) -> bool:
    """The process that took the lease is still running: alive, and not a reused pid."""
    if not pid_alive(pid):
        return False
    current = pid_start(pid)
    return recorded_start is None or current is None or current == recorded_start


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


def _code(value: Any, field: str) -> str:
    if not isinstance(value, str) or not CODE_RE.fullmatch(value):
        raise LeaseError(INVALID, f"{field} must be an upper-case code such as OPERATOR_RECOVERY")
    return value


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


def _same_machine(lease: dict) -> bool:
    return lease.get("host") == host_name() and lease.get("host_id") == host_id()


def assess(lease: dict | None | object, *, now: float | None = None) -> tuple[str, str | None, bool | None]:
    """(state, stale_reason, holder_alive): state is "free", "live" or "stale".

    holder_alive is True or False when the holder is on this machine, None when
    it cannot be probed (another machine, or an unreadable lease).
    """
    if lease is None:
        return "free", None, None
    if lease is _UNREADABLE or not isinstance(lease, dict):
        return "stale", LEASE_UNREADABLE, None
    current = time.time() if now is None else now
    beat, expires = _epoch(lease.get("heartbeat_at")), _epoch(lease.get("expires_at"))
    if (beat is None or expires is None or expires < beat or expires > beat + MAX_TTL_SECONDS
            or beat > current + CLOCK_SKEW_SECONDS):
        return "stale", LEASE_UNREADABLE, None
    alive = process_matches(lease.get("pid"), lease.get("pid_start")) if _same_machine(lease) else None
    if alive is False:
        return "stale", OWNER_PROCESS_DEAD, False
    if current >= expires:
        return "stale", HEARTBEAT_EXPIRED, alive
    return "live", None, alive


def _public(lease: dict | None | object) -> dict | None:
    return lease if isinstance(lease, dict) else None


def _identity(pid: int) -> dict:
    return {"pid": pid, "pid_start": pid_start(pid), "host": host_name(), "host_id": host_id()}


def _new_lease(run_id: str, owner_id: str, pid: int, ttl: int, now: float, takeovers: list | None = None) -> dict:
    return {"lease_version": LEASE_VERSION, "run_id": run_id, "owner_id": owner_id, **_identity(pid),
            "acquired_at": _iso(now), "heartbeat_at": _iso(now), "expires_at": _iso(now + ttl),
            "ttl_seconds": ttl, "takeovers": list(takeovers or [])}


def _renew(lease: dict, ttl: int, now: float) -> dict:
    lease.update({"heartbeat_at": _iso(now), "expires_at": _iso(now + ttl), "ttl_seconds": ttl})
    return lease


def status(run_id: str, *, repo_root: Path = ROOT, now: float | None = None, directory: Path | None = None) -> dict:
    """Read-only view: who holds the run, and whether that lease is live or stale."""
    path = lease_path(run_id, repo_root, directory=directory)
    lease = _read(path)
    state, reason, alive = assess(lease, now=now)
    return {"ok": True, "run_id": run_id, "state": state, "stale_reason": reason, "holder_alive": alive,
            "lease": _public(lease)}


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
    return {"state": view["state"], "stale_reason": view["stale_reason"], "holder_alive": view["holder_alive"],
            **{key: lease.get(key) for key in ("owner_id", "pid", "host", "acquired_at", "heartbeat_at", "expires_at")}}


class Held:
    """A lease decision taken under the lease lock, which stays held while the context is open."""

    def __init__(self, path: Path, lease: dict | None, result: dict | None = None):
        self.path = path
        self.lease = lease
        self.result = result

    def release(self) -> None:
        """Remove the lease. Only valid inside the context that authorised the caller."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.lease = None


def _acquire_in(path: Path, run_id: str, owner: str, pid: int, ttl: int, now: float, *,
                handoff: bool, caller_pid: int) -> dict:
    lease = _read(path)
    if isinstance(lease, dict) and lease["owner_id"] == owner:
        if lease.get("pid") != pid:
            # Same owner id, another process: only the launcher -> child hand-off may move it.
            same = _same_machine(lease)
            launcher = lease.get("pid")
            if not (handoff and same and (caller_pid == launcher or parent_pid(pid) == launcher)):
                raise LeaseError(PID_MISMATCH, f"run {run_id} is held by {owner} as pid {launcher}; "
                                 "a different process may take it only through an explicit hand-off from that pid",
                                 lease=_public(lease), state=assess(lease, now=now)[0])
            lease.update(_identity(pid))
        lease = _renew(lease, ttl, now)
        _write(path, lease)
        return {"ok": True, "run_id": run_id, "state": "live", "acquired": "renewed", "lease": lease}
    state, reason, alive = assess(lease, now=now)
    if state == "live":
        raise LeaseError(HELD, f"run {run_id} is held by {lease['owner_id']} (pid {lease['pid']} on {lease['host']})",
                         lease=_public(lease), state=state, holder_alive=alive)
    if state == "stale":
        raise LeaseError(STALE, f"run {run_id} has a stale lease ({reason}); recover it only with an explicit takeover",
                         lease=_public(lease), state=state, stale_reason=reason, holder_alive=alive)
    lease = _new_lease(run_id, owner, pid, ttl, now)
    _write(path, lease)
    return {"ok": True, "run_id": run_id, "state": "live", "acquired": "new", "lease": lease}


@contextlib.contextmanager
def claim(run_id: str, owner_id: str, *, pid: int | None = None, ttl_seconds: int | None = None,
          handoff: bool = False, caller_pid: int | None = None, repo_root: Path = ROOT, now: float | None = None):
    """Acquire and keep the lease lock held, so the caller can verify more before committing to the run."""
    owner, pid, ttl = _owner(owner_id), _pid(pid), _ttl(ttl_seconds)
    with _locked(run_id, repo_root) as path:
        result = _acquire_in(path, run_id, owner, pid, ttl, now if now is not None else time.time(),
                             handoff=handoff, caller_pid=os.getpid() if caller_pid is None else caller_pid)
        yield Held(path, result["lease"], result)


def acquire(run_id: str, owner_id: str, *, pid: int | None = None, ttl_seconds: int | None = None,
            handoff: bool = False, caller_pid: int | None = None, repo_root: Path = ROOT,
            now: float | None = None) -> dict:
    with claim(run_id, owner_id, pid=pid, ttl_seconds=ttl_seconds, handoff=handoff, caller_pid=caller_pid,
               repo_root=repo_root, now=now) as held:
        return held.result


@contextlib.contextmanager
def guard(run_id: str, owner_id: str | None, *, recorded_owner: str | None, repo_root: Path = ROOT):
    """Hold the lease lock around a write to the run and refuse a caller that does not own the run.

    Allowed: the caller holds the lease (live, or stale but not taken over),
    or the lease is free and the caller is the run's recorded owner. A run
    with no recorded owner and no lease (created before leases, or in-process
    without one) accepts any caller.
    """
    owner = None if owner_id is None else _owner(owner_id)
    with _locked(run_id, repo_root) as path:
        lease = _read(path)
        if isinstance(lease, dict) or lease is _UNREADABLE:
            if not isinstance(lease, dict) or owner is None or lease["owner_id"] != owner:
                state, reason, alive = assess(lease)
                raise LeaseError(LOST, f"run {run_id} is held by another owner; this caller may not write to it",
                                 lease=_public(lease), state=state, stale_reason=reason, holder_alive=alive)
        elif recorded_owner is not None and recorded_owner != owner:
            raise LeaseError(NOT_HELD, f"run {run_id} belongs to {recorded_owner}; take its lease before writing",
                             state="free")
        yield Held(path, _public(lease))


def heartbeat(run_id: str, owner_id: str, *, pid: int | None = None, ttl_seconds: int | None = None,
              repo_root: Path = ROOT, now: float | None = None) -> dict:
    owner, ttl = _owner(owner_id), _ttl(ttl_seconds)
    with _locked(run_id, repo_root) as path:
        lease = _read(path)
        if lease is None:
            raise LeaseError(NOT_HELD, f"run {run_id} has no lease", state="free")
        if not isinstance(lease, dict) or lease["owner_id"] != owner:
            state, reason, alive = assess(lease, now=now)
            raise LeaseError(LOST, f"run {run_id} is no longer held by {owner}", lease=_public(lease), state=state,
                             stale_reason=reason, holder_alive=alive)
        if pid is not None and _pid(pid) != lease.get("pid"):
            raise LeaseError(PID_MISMATCH, f"run {run_id} is held by pid {lease.get('pid')}, not {pid}",
                             lease=_public(lease), state=assess(lease, now=now)[0])
        lease = _renew(lease, ttl, now if now is not None else time.time())
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


def _record_takeover(run_id: str, entry: dict, repo_root: Path) -> None:
    """Append the takeover and the new owner to the run's provenance; any failure refuses the takeover."""
    try:
        with provenance.run_lock(repo_root, run_id):
            record = provenance.get_run(repo_root, run_id)
            parent = record.get("parent_orchestration")
            if record.get("outcome") != "in_progress" or not isinstance(parent, dict):
                raise LeaseError(PROVENANCE_UNAVAILABLE, "only an in-progress parent run can be taken over")
            parent.setdefault("lease_takeovers", []).append(entry)
            parent["lease_takeovers"] = parent["lease_takeovers"][-MAX_TAKEOVERS:]
            parent["owner_id"] = entry["owner_id"]
            provenance.update_parent_orchestration(repo_root, run_id, parent)
    except LeaseError:
        raise
    except (provenance.ProvenanceError, OSError, ValueError) as exc:
        raise LeaseError(PROVENANCE_UNAVAILABLE, f"takeover cannot be recorded in provenance: {type(exc).__name__}") from exc


def takeover(run_id: str, owner_id: str, *, reason: str, force_reason: str | None = None, pid: int | None = None,
             ttl_seconds: int | None = None, repo_root: Path = ROOT, now: float | None = None) -> dict:
    """Recover a stale lease. Recorded in provenance first; a live lease is never taken.

    An expired lease whose holder is still running on this machine is refused
    unless `force_reason` names why the running holder may be displaced.
    """
    owner, pid, ttl = _owner(owner_id), _pid(pid), _ttl(ttl_seconds)
    reason = _code(reason, "takeover reason")
    if force_reason is not None:
        force_reason = _code(force_reason, "force reason")
    with _locked(run_id, repo_root) as path:
        current = now if now is not None else time.time()
        lease = _read(path)
        state, stale_reason, alive = assess(lease, now=current)
        if state == "live":
            raise LeaseError(HELD, f"run {run_id} is held by a live owner; a live lease is never taken over",
                             lease=_public(lease), state=state, holder_alive=alive)
        if state == "free":
            raise LeaseError(NOT_HELD, f"run {run_id} has no lease to take over; acquire it instead", state=state)
        if alive is True and force_reason is None:
            raise LeaseError(HOLDER_ALIVE, f"run {run_id}: the lease expired but its holder pid {lease['pid']} is still "
                             "running on this machine; stop it, or pass an explicit force reason",
                             lease=_public(lease), state=state, stale_reason=stale_reason, holder_alive=True)
        previous = lease if isinstance(lease, dict) else {}
        entry = {"at": _iso(current), "owner_id": owner, "pid": pid, "host": host_name(), "reason": reason,
                 "stale_reason": stale_reason, "forced": alive is True, "force_reason": force_reason if alive else None,
                 "previous_owner_id": previous.get("owner_id"),
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
        if name == "acquire":
            p.add_argument("--handoff", action="store_true",
                           help="move this owner's lease from the launcher pid to the process it started")
        if name == "takeover":
            p.add_argument("--reason", required=True, help="upper-case reason code recorded in provenance")
            p.add_argument("--force-reason", help="required to displace a holder that is still running on this machine")
    args = parser.parse_args(argv)
    pid = getattr(args, "pid", None)
    if pid is None and args.command in {"acquire", "takeover"}:
        pid = os.getppid()
    try:
        if args.command == "acquire":
            result = acquire(args.run_id, args.owner, pid=pid, ttl_seconds=args.ttl, handoff=args.handoff,
                             caller_pid=os.getppid())
        elif args.command == "heartbeat":
            result = heartbeat(args.run_id, args.owner, pid=pid, ttl_seconds=args.ttl)
        elif args.command == "release":
            result = release(args.run_id, args.owner)
        elif args.command == "takeover":
            result = takeover(args.run_id, args.owner, reason=args.reason, force_reason=args.force_reason, pid=pid,
                              ttl_seconds=args.ttl)
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
