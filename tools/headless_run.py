#!/usr/bin/env python3
"""Headless single-repository execution: start, observe, resume and cancel one
bounded orchestration run without an interactive terminal.

A headless run is one Pi process in print mode (`pi --print --mode json`) with
the orchestrator extensions loaded and the /orchestrate prompt as its only
input. The orchestration-lifecycle extension creates the parent run, keeps all
of its guards (SESSION_REPOSITORY_MISMATCH, WORKER_WORKSPACE_INVALID,
WAVE_UNDECLARED, backend preflight) and settles the run as BLOCKED when the
process exits without an explicit outcome. Pi's session id is chosen here and
stored with the execution, so a crashed process can be resumed into the same
parent run while a normally exited one cannot be resurrected.

Headless is not autonomous. Mutation and publication stay governed by the
target's .pi/autonomy.yaml; a policy that permits any mutation is refused
unless --allow-mutation is passed explicitly.

Execution records are keyed by a caller-chosen execution id in orchestrator
state (never in the target), so repeating a `start` never launches a second
process, run, branch or PR for the same id.

Exit codes: 0 ok, 1 blocked or refused, 2 invalid invocation.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402
import autonomy_policy  # noqa: E402
import github_integration  # noqa: E402
import parent_orchestration  # noqa: E402
import provenance  # noqa: E402
import run_lease  # noqa: E402
import session_scope  # noqa: E402
import workspace_plan  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "orchestrate-core.md"
EXTENSIONS = ["model-router.ts", "workflow-commands.ts", "mission-controller.ts", "orchestration-lifecycle.ts"]
SESSION_ENTRY = "pi-orchestration-parent-v1"
RECORD_VERSION = 1
EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MUTATING_PERMISSIONS = ("create_worktrees", "commit", "push_branch", "create_pr", "mark_pr_ready", "merge", "deploy")
TERMINATE_GRACE_SECONDS = 10
LEASE_OWNER_ENV = "PI_ORCHESTRATION_LEASE_OWNER"
LEASE_TAKEOVER_REASON = "HEADLESS_RESUME_TAKEOVER"
TIME_LIMIT_CODE = "EXECUTION_TIME_LIMIT"
WAIT_POLL_SECONDS = 1.0
# The lead-call limit travels to the child as env vars the lifecycle extension reads at run start.
LEAD_LIMIT_ENV = "PI_ORCHESTRATION_MAX_LEAD_CALLS"
LEAD_LIMIT_SOURCE_ENV = "PI_ORCHESTRATION_MAX_LEAD_CALLS_SOURCE"
LEAD_WARN_ENV = "PI_ORCHESTRATION_LEAD_CALLS_WARN_AT"
LEAD_LIMIT_SOURCES = ("cli", "task", "manifest")  # explicit sources; `policy` is resolved below
LEAD_LIMIT_CODE = "LEAD_CALL_LIMIT"
# The per-signature retry budget (repeated ineffective actions, #48) and the declared-input digest travel the same way.
FAILURE_BUDGET_ENV = "PI_ORCHESTRATION_FAILURE_RETRY_BUDGET"
FAILURE_BUDGET_SOURCE_ENV = "PI_ORCHESTRATION_FAILURE_RETRY_BUDGET_SOURCE"
INPUTS_DIGEST_ENV = "PI_ORCHESTRATION_INPUTS_DIGEST"
FAILURE_BUDGET_SOURCES = ("cli", "task", "manifest")  # `policy` and the default are resolved by the extension
# A resumed session must not carry the run marker: the lifecycle answers the
# marker by settling the restored run and starting a new one. Without it,
# session_start restores the IN_PROGRESS run and the agent continues it.
RESUME_PROMPT = ("Continue the active orchestration run recorded in this session. First call "
                 "orchestration_record with {\"action\":\"status\"}. If its reply says NEXT STEP "
                 "(PARENT_ROUTE_RECHECK_REQUIRED), that is the expected first step of a resume, not a blocker: "
                 "rerun route_task for the named task, complete its backend preflight, and call status again. "
                 "Continue only when its reply says the record is ready with ok: true and active: true; if it "
                 "says finalized or BLOCKED, stop and report the blocker. Then resume from the last recorded "
                 "phase and finish the run with an explicit outcome.")


class HeadlessError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock() -> float:
    """Wall-clock seconds since the epoch. Module-level so tests can substitute a fake clock."""
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_max_duration(value) -> int | None:
    """None means no limit; otherwise a positive integer number of seconds."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HeadlessError("MAX_DURATION_INVALID", "max_duration_seconds must be a positive integer")
    return value


def validate_max_lead_calls(value, source: str = "cli") -> tuple[int | None, str | None]:
    """None means no explicit limit; otherwise a positive integer and where it came from."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HeadlessError("MAX_LEAD_CALLS_INVALID", "max_lead_calls must be a positive integer")
    if source not in LEAD_LIMIT_SOURCES:
        raise HeadlessError("MAX_LEAD_CALLS_INVALID", f"max_lead_calls source must be one of {', '.join(LEAD_LIMIT_SOURCES)}")
    return value, source


def validate_failure_retry_budget(value, source: str = "cli") -> tuple[int | None, str | None]:
    """None means no explicit budget (the extension then reads the repository policy, else the default 2)."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise HeadlessError("FAILURE_RETRY_BUDGET_INVALID", "failure_retry_budget must be an integer from 0 to 1000")
    if source not in FAILURE_BUDGET_SOURCES:
        raise HeadlessError("FAILURE_RETRY_BUDGET_INVALID", f"failure_retry_budget source must be one of {', '.join(FAILURE_BUDGET_SOURCES)}")
    return value, source


def validate_inputs_digest(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise HeadlessError("INPUTS_DIGEST_INVALID", "inputs_digest must be a SHA-256 hex digest")
    return value


def validate_lead_calls_warn_at(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HeadlessError("LEAD_CALLS_WARN_AT_INVALID", "lead_calls_warn_at must be a positive integer")
    return value


def resolve_lead_limit(worktree: Path, explicit: int | None, source: str | None,
                       warn_at: int | None) -> tuple[int | None, str | None, int | None]:
    """Effective (limit, source, warning call): an explicit CLI/task/manifest value wins, then `.pi/autonomy.yaml`."""
    policy: dict = {}
    if explicit is None or warn_at is None:
        try:
            policy = autonomy_policy.limits(worktree)
        except autonomy_policy.AutonomyPolicyError as exc:
            raise HeadlessError("AUTONOMY_POLICY_INVALID", str(exc)[:300]) from exc
    if explicit is None:
        explicit, source = policy.get("max_lead_calls"), "policy"
    if explicit is None:
        return None, None, None
    if warn_at is None:
        # The repository's default threshold only applies below the effective limit; otherwise the 80% default does.
        inherited = policy.get("lead_calls_warn_at")
        warn_at = inherited if inherited is not None and inherited < explicit else None
    elif warn_at >= explicit:
        raise HeadlessError("LEAD_CALLS_WARN_AT_INVALID", f"lead_calls_warn_at ({warn_at}) must be below max_lead_calls ({explicit})")
    return explicit, source, warn_at


def state_root() -> Path:
    explicit = os.environ.get(provenance.STATE_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise HeadlessError("STATE_DIR_INVALID", f"{provenance.STATE_ENV} must be an absolute path")
        return path
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "pi-coding-orchestrator"


def repository_key(common_dir: Path) -> str:
    name = (common_dir.parent if common_dir.name == ".git" else common_dir).name or "root"
    return f"{name}-{hashlib.sha256(str(common_dir).encode()).hexdigest()[:12]}"


def execution_path(common_dir: Path, execution_id: str) -> Path:
    if not EXECUTION_ID_RE.fullmatch(execution_id):
        raise HeadlessError("EXECUTION_ID_INVALID", "execution id must be filename-safe")
    return state_root() / "executions" / repository_key(common_dir) / f"{execution_id}.json"


def load_record(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HeadlessError("EXECUTION_RECORD_UNREADABLE", str(exc)) from exc
    if not isinstance(data, dict) or data.get("record_version") != RECORD_VERSION:
        raise HeadlessError("EXECUTION_RECORD_UNREADABLE", "unsupported execution record")
    return data


def save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = _now()
    atomic_io.atomic_write_json(path, record, indent=2, sort_keys=True)


@contextlib.contextmanager
def locked_record(path: Path):
    """Exclusive read-modify-write of one execution record across threads and processes.

    Not re-entrant (flock is per open file). Never held while waiting for the
    child to exit, so status and cancel stay responsive during a run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def build_prompt(objective: str, prompt_path: Path = PROMPT_PATH) -> str:
    template = re.sub(r"^---[\s\S]*?---\s*", "", prompt_path.read_text(encoding="utf-8"))
    if "$ARGUMENTS" in template:
        return template.replace("$ARGUMENTS", objective)
    return f"{template.rstrip()}\n\n{objective}\n"


def mutation_permitted(worktree: Path) -> list[str]:
    """Names of mutating permissions the target's autonomy policy grants."""
    path = autonomy_policy.policy_path(worktree)
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HeadlessError("AUTONOMY_POLICY_UNREADABLE", "autonomy policy is unreadable") from exc
    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, dict):
        return []
    return [name for name in MUTATING_PERMISSIONS if permissions.get(name) is True]


# Children this process launched and is waiting for, by pid. Their liveness is read through the Popen
# object: reaping them behind its back with waitpid would make Popen.wait() report exit status 0.
_OWN_CHILDREN: dict[int, subprocess.Popen] = {}


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    own = _OWN_CHILDREN.get(pid)
    if own is not None:
        return own.poll() is None
    try:
        # Reap our own exited child so a zombie never reads as alive.
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass  # not our child: fall through to the signal probe
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def execution_alive(record: dict) -> bool:
    """The execution's child is still the process it launched: alive, and not a reused pid.

    Records launched by this version carry the child's `pid_start` (start time
    from /proc); a live pid with a different start time is another process.
    Older records, and hosts without /proc, fall back to the plain pid probe.
    """
    pid = record.get("pid")
    if not pid_alive(pid):
        return False
    recorded, current = record.get("pid_start"), run_lease.pid_start(pid)
    return recorded is None or current is None or recorded == current


def discover_run_id(session_dir: Path, session_id: str) -> str | None:
    """The lifecycle extension stores the parent run id in a custom session entry."""
    run_id = None
    for file in sorted(session_dir.glob(f"*_{session_id}.jsonl")):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (isinstance(entry, dict) and entry.get("type") == "custom" and entry.get("customType") == SESSION_ENTRY
                    and isinstance(entry.get("data"), dict) and isinstance(entry["data"].get("run_id"), str)
                    and provenance.RUN_ID_PATTERN.fullmatch(entry["data"]["run_id"])):
                run_id = entry["data"]["run_id"]
    return run_id


def run_status(run_id: str | None) -> dict | None:
    if run_id is None:
        return None
    try:
        record = provenance.get_run(ROOT, run_id)
        parent = record.get("parent_orchestration") or {}
        summary = {"run_id": run_id, "status": parent.get("final_status"), "blocker_code": parent.get("blocker_code"),
                   "trace_path": str(provenance.run_path(ROOT, run_id)), "lease": run_lease.summary(run_id)}
        context = parent.get("context") or {}
        limit = context.get("lead_call_limit")
        if isinstance(limit, dict):
            calls = context.get("lead_calls")
            summary["lead_call_limit"] = {"max_lead_calls": limit.get("max_lead_calls"), "source": limit.get("source"),
                                          "calls": max(len(calls) if isinstance(calls, list) else 0, limit.get("calls_seen") or 0),
                                          "warn_at": limit.get("warn_at"), "warned": limit.get("warned_at_call") is not None,
                                          "reached": limit.get("calls_at_limit") is not None,
                                          "stopped": bool(limit.get("stopped"))}
        return summary
    except (provenance.ProvenanceError, OSError, KeyError, TypeError):
        return {"run_id": run_id, "status": None, "blocker_code": "PARENT_PROVENANCE_UNAVAILABLE", "trace_path": None}


def _backend_identity(target: Path, python: str) -> str:
    probe = subprocess.run([python, str(ROOT / "tools" / "claude_backend.py"), str(target)], shell=False,
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise HeadlessError("BACKEND_IDENTITY_UNINITIALIZED", probe.stderr.strip() or "claude backend identity unavailable")
    return probe.stdout.strip().splitlines()[-1]


def _target(target: Path, explicit_repo: str) -> tuple[Path, str, Path]:
    try:
        worktree = target.resolve(strict=True)
    except OSError as exc:
        raise HeadlessError("TARGET_REPOSITORY_UNRESOLVED", "target checkout does not exist") from exc
    try:
        identity = github_integration.resolve_target(worktree, explicit_repo)
    except github_integration.IntegrationError as exc:
        raise HeadlessError(exc.code, exc.detail) from exc
    common = session_scope.common_dir(worktree)
    if common is None:
        raise HeadlessError("SESSION_TARGET_UNRESOLVED", "git common directory cannot be resolved")
    return worktree, identity, common


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(checkout), *args], shell=False, capture_output=True, text=True, timeout=60)


def untracked_entries(checkout: Path) -> list[str] | None:
    """Untracked, not ignored entries of a checkout (an untracked directory is one entry), without PICO's own
    `.pi/worktrees/`; None when Git cannot answer."""
    try:
        listing = _git(checkout, "ls-files", "-z", "--others", "--exclude-standard", "--directory",
                       "--no-empty-directory", "--exclude=/.pi/worktrees/")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode:
        return None
    return sorted(p for p in listing.stdout.split("\0") if p)


def checkout_state(checkout: Path) -> dict:
    """Branch (or detached HEAD), linked worktrees and untracked entries of a checkout, to undo or report
    what a run left behind."""
    branch = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or None
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip() or None
    listing = _git(checkout, "worktree", "list", "--porcelain").stdout.splitlines()
    paths = [line[len("worktree "):] for line in listing if line.startswith("worktree ")]
    main = str(checkout.resolve())
    refs = _git(checkout, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    branches = dict(line.split(" ", 1) for line in refs.stdout.splitlines() if " " in line) if refs.returncode == 0 else None
    return {"branch": branch, "head": head, "worktrees": sorted(p for p in paths if str(Path(p).resolve()) != main),
            "untracked": untracked_entries(checkout), "branches": branches}


DIRTY_FILES_SHOWN = 50


def tracked_changes(checkout: Path) -> list[str] | None:
    """Paths with uncommitted tracked changes (staged or not); None when Git cannot answer.

    Untracked files are not counted: PICO's own worktrees and scratch files live there, and they
    cannot change what a commit or a check sees.
    """
    try:
        status = _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status.returncode:
        return None
    entries = status.stdout.split("\0")
    paths, skip = [], False
    for entry in entries:
        if skip:  # the source path of a rename or copy
            skip = False
            continue
        if len(entry) > 3:
            paths.append(entry[3:])
            skip = entry[0] in "RC"
    return sorted(paths)


def restore_checkout(checkout: Path, before: dict) -> dict:
    """After a child exits, return a clean checkout to its starting point.

    New worktrees are reported but kept: their appearance during the run does
    not establish ownership. A checkout with uncommitted tracked changes is left where it is.
    Branches are never deleted, because they hold the work. The tracked changes
    left in the parent checkout are recorded (`dirty_files`, `dirty_count`;
    None when Git cannot answer), and so are untracked entries that appeared
    during the run (`new_untracked`, `new_untracked_count`), as a report only.
    """
    after = checkout_state(checkout)
    result = {"branch_before": before["branch"], "branch_after": after["branch"], "restored": False,
              "removed_worktrees": [], "kept_worktrees": [], "left_reason": None}
    result["kept_worktrees"] = sorted(set(after["worktrees"]) - set(before["worktrees"]))
    moved = (after["branch"] != before["branch"]) if before["branch"] else (after["head"] != before["head"])
    if moved:
        if _git(checkout, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            result["left_reason"] = "uncommitted changes in the checkout"
        else:
            target = ["switch", "--quiet", before["branch"]] if before["branch"] else ["switch", "--quiet", "--detach", before["head"]]
            switched = _git(checkout, *target)
            result["restored"] = switched.returncode == 0
            if not result["restored"]:
                result["left_reason"] = "git switch failed"
        result["branch_after"] = checkout_state(checkout)["branch"]
    # Where HEAD and the local branches ended up, so a commit the run made on the checkout's own branch (or on
    # a branch it never switched to) is visible; None when Git cannot answer.
    final = checkout_state(checkout)
    result["head_before"], result["head_after"] = before.get("head"), final["head"]
    old, new_refs = before.get("branches"), final["branches"]
    result["branches_changed"] = (None if old is None or new_refs is None
                                  else sorted(n for n in set(old) | set(new_refs) if old.get(n) != new_refs.get(n)))
    # What the run left uncommitted in the parent checkout itself, recorded whether or not it moved, so the next
    # launch's refusal (WORKSPACE_CHECKOUT_DIRTY) can name the run that caused it. Nothing is cleaned.
    dirty = tracked_changes(checkout)
    result["dirty_files"] = None if dirty is None else dirty[:DIRTY_FILES_SHOWN]
    result["dirty_count"] = None if dirty is None else len(dirty)
    # Untracked entries that appeared during the run: reported only, never refused or removed. They cannot change
    # a commit or a check at the recorded commits, but they show a write outside the run's worktrees (#63).
    untracked, earlier = untracked_entries(checkout), before.get("untracked")
    new = None if untracked is None or earlier is None else sorted(set(untracked) - set(earlier))
    result["new_untracked"] = None if new is None else new[:DIRTY_FILES_SHOWN]
    result["new_untracked_count"] = None if new is None else len(new)
    return result


def refresh(record: dict) -> dict:
    """Reconcile a record against the live process and the parent provenance."""
    session_dir = Path(record["session_dir"])
    if record.get("run_id") is None:
        record["run_id"] = discover_run_id(session_dir, record["session_id"])
    record["run"] = run_status(record.get("run_id"))
    if record["status"] == "launching":
        # A launch whose process id was never recorded: the outcome is uncertain.
        terminal = (record["run"] or {}).get("status") not in {None, "IN_PROGRESS"}
        record["status"] = "exited" if terminal else "interrupted"
    if record["status"] == "launched" and not execution_alive(record):
        # Gone without this process observing the exit: terminal run means it
        # ended normally, an open run means it was interrupted. A live pid with
        # another start time is a different process that reused the pid: the
        # child is gone, and that pid is never signalled.
        if pid_alive(record.get("pid")):
            record["child_gone_reason"] = "PID_REUSED"
        terminal = (record["run"] or {}).get("status") not in {None, "IN_PROGRESS"}
        record["status"] = "exited" if terminal else "interrupted"
        record["pid"] = None
    # A process that is gone while its parent run is still open never settled:
    # that is a crash, resumable into the same run, not a finished execution.
    if record["status"] == "exited" and (record["run"] or {}).get("status") == "IN_PROGRESS":
        record["status"] = "interrupted"
    # The lifecycle stopped the run at its lead-call limit: interrupted and resumable, with the reason named.
    if (record["status"] == "interrupted" and ((record["run"] or {}).get("lead_call_limit") or {}).get("stopped")
            and not record.get("stop_reason")):
        record["stop_reason"] = LEAD_LIMIT_CODE
    return record


def lease_owner(execution_id: str) -> str:
    """A fresh lease owner per launch: a relaunch never silently inherits a crashed launch's lease."""
    return f"headless-{hashlib.sha256(execution_id.encode()).hexdigest()[:12]}-{uuid.uuid4().hex[:12]}"


def claim_run_lease(run_id: str, owner: str, *, takeover: bool) -> dict:
    """Take the lease of a run this launch resumes. Live: RUN_LEASE_HELD. Stale: RUN_LEASE_STALE unless taken over."""
    try:
        return run_lease.acquire(run_id, owner, pid=os.getpid())
    except run_lease.LeaseError as exc:
        if exc.code != run_lease.STALE or not takeover:
            hint = "; pass --takeover-stale-lease with --resume to recover it" if exc.code == run_lease.STALE else ""
            raise HeadlessError(exc.code, exc.detail + hint) from exc
    try:
        return run_lease.takeover(run_id, owner, reason=LEASE_TAKEOVER_REASON, pid=os.getpid())
    except run_lease.LeaseError as exc:
        raise HeadlessError(exc.code, exc.detail) from exc


def start(*, target: Path, explicit_repo: str, execution_id: str, objective: str, allow_mutation: bool = False,
          resume: bool = False, pi_executable: str = "pi", python: str = sys.executable,
          wait: bool = True, before_spawn=None, takeover: bool = False,
          max_duration_seconds: int | None = None, max_lead_calls: int | None = None,
          max_lead_calls_source: str = "cli", lead_calls_warn_at: int | None = None,
          failure_retry_budget: int | None = None, failure_retry_budget_source: str = "cli",
          inputs_digest: str | None = None) -> dict:
    max_duration_seconds = validate_max_duration(max_duration_seconds)
    failure_retry_budget, failure_retry_budget_source = validate_failure_retry_budget(
        failure_retry_budget, failure_retry_budget_source)
    inputs_digest = validate_inputs_digest(inputs_digest)
    max_lead_calls, max_lead_calls_source = validate_max_lead_calls(max_lead_calls, max_lead_calls_source)
    lead_calls_warn_at = validate_lead_calls_warn_at(lead_calls_warn_at)
    if takeover and not resume:
        raise HeadlessError("TAKEOVER_REQUIRES_RESUME", "--takeover-stale-lease applies only to --resume")
    if not objective.strip() or len(objective) > 20000:
        raise HeadlessError("OBJECTIVE_INVALID", "objective must be a non-empty string under 20000 characters")
    if provenance._contains_secret_like_content(objective):
        raise HeadlessError("OBJECTIVE_INVALID", "objective looks like it contains a secret")
    worktree, identity, common = _target(target, explicit_repo)
    max_lead_calls, max_lead_calls_source, lead_calls_warn_at = resolve_lead_limit(
        worktree, max_lead_calls, max_lead_calls_source, lead_calls_warn_at)
    path = execution_path(common, execution_id)
    # Deciding and launching happen under the record lock, so two starts with
    # the same id cannot both see "not running" and both start Pi.
    with locked_record(path):
        launched = _launch_locked(path, worktree=worktree, identity=identity, common=common,
                                  execution_id=execution_id, objective=objective, allow_mutation=allow_mutation,
                                  resume=resume, pi_executable=pi_executable, python=python,
                                  before_spawn=before_spawn, takeover=takeover,
                                  max_duration_seconds=max_duration_seconds, max_lead_calls=max_lead_calls,
                                  max_lead_calls_source=max_lead_calls_source, lead_calls_warn_at=lead_calls_warn_at,
                                  failure_retry_budget=failure_retry_budget,
                                  failure_retry_budget_source=failure_retry_budget_source, inputs_digest=inputs_digest)
    if isinstance(launched, dict):
        return launched
    record, child, log, before = launched
    if not wait:
        _OWN_CHILDREN.pop(child.pid, None)
        log.close()
        return record
    try:
        exit_code = _wait_for_child(child, path, record)
    finally:
        _OWN_CHILDREN.pop(child.pid, None)
        log.close()
    with locked_record(path):
        current = load_record(path) or {}
        record["exit_code"] = exit_code
        record["status"] = "exited"
        record["pid"] = None
        # Leave the checkout as the run found it, so the next task in this repository can launch.
        record["checkout"] = restore_checkout(worktree, before)
        refresh(record)  # downgrades to "interrupted" when the run is still open
        if current.get("status") == "cancelled":
            # A cancel (or time-limit stop) recorded while the child ran stays the outcome of this execution.
            record["status"] = "cancelled"
            record["outcome"] = current.get("outcome") or "cancelled"
            for key in ("stop_reason", "time_limit"):
                if key in current:
                    record[key] = current[key]
        save_record(path, record)
    return record


def _wait_for_child(child, path: Path, record: dict) -> int:
    """Wait for the child. Without a limit this is a plain wait; with one, the wait is sliced so the
    launcher enforces the deadline itself (the same stop path `status` uses for children nobody waits on)."""
    if record.get("deadline_epoch") is None:
        return child.wait()
    while True:
        remaining = time_limit_remaining(record)
        try:
            return child.wait(timeout=min(WAIT_POLL_SECONDS, max(0.05, remaining or 0)))
        except subprocess.TimeoutExpired:
            pass
        if time_limit_remaining(record) == 0:
            with locked_record(path):
                current = load_record(path)
                if current is not None:
                    enforce_time_limit(current)
                    save_record(path, current)


def _launch_locked(path: Path, *, worktree: Path, identity: str, common: Path, execution_id: str, objective: str,
                   allow_mutation: bool, resume: bool, pi_executable: str, python: str, before_spawn=None,
                   takeover: bool = False, max_duration_seconds: int | None = None,
                   max_lead_calls: int | None = None, max_lead_calls_source: str | None = None,
                   lead_calls_warn_at: int | None = None, failure_retry_budget: int | None = None,
                   failure_retry_budget_source: str | None = None, inputs_digest: str | None = None):
    """Return the settled record, or (record, child, log, checkout_before) for a new launch. Caller holds the lock."""
    record = load_record(path)
    if record is not None:
        if record["target_repository"].casefold() != identity.casefold():
            raise HeadlessError("EXECUTION_TARGET_MISMATCH", "execution id belongs to another repository")
        if record["objective"] != objective:
            raise HeadlessError("EXECUTION_OBJECTIVE_MISMATCH",
                                "execution id has a different recorded objective; use a new task id for changed instructions")
        refresh(record)
        enforce_time_limit(record)  # a repeat start past the deadline stops the child rather than report it running
        if record["status"] == "launched":
            record["outcome"] = "already_running"
            save_record(path, record)
            return record
        if record["status"] in {"exited", "cancelled"}:
            record["outcome"] = "already_finished"
            save_record(path, record)
            return record
        if record["status"] == "interrupted" and not resume:
            record["outcome"] = "resume_required"
            save_record(path, record)
            return record
        run = record.get("run") or {}
        exhausted = dict(run.get("lead_call_limit") or {})
        # The launcher's own record of the previous launch's limit is authoritative: the run record's limit can be
        # rewritten by anything holding the run's owner id, this execution record cannot.
        if type(record.get("max_lead_calls")) is int:
            exhausted["max_lead_calls"] = record["max_lead_calls"]
        if (resume and run.get("status") == "IN_PROGRESS" and type(exhausted.get("calls")) is int
                and type(exhausted.get("max_lead_calls")) is int and exhausted["calls"] >= exhausted["max_lead_calls"]
                and (max_lead_calls is None or max_lead_calls <= exhausted["calls"])):
            # The run stopped (or is parked) at its lead-call limit: only an explicit higher limit continues it.
            raise HeadlessError("LEAD_CALL_LIMIT_EXHAUSTED",
                                f"the run has made {exhausted['calls']} lead calls against a limit of {exhausted['max_lead_calls']}; "
                                f"resume with a limit above {exhausted['calls']} (a resume without a limit is refused too; --max-lead-calls, or a higher task/manifest "
                                "max_lead_calls) or finish the run")
        if resume and run.get("status") not in {None, "IN_PROGRESS"}:
            record["status"] = "exited"
            record["outcome"] = "already_finished"
            save_record(path, record)
            return record
    elif resume:
        raise HeadlessError("EXECUTION_UNKNOWN", "nothing to resume for this execution id")

    if before_spawn is not None:
        before_spawn()  # a child will start or resume: callers may still refuse here

    granted = mutation_permitted(worktree)
    if granted and not allow_mutation:
        raise HeadlessError("MUTATION_NOT_AUTHORIZED",
                            "autonomy policy permits " + ", ".join(granted) + "; pass --allow-mutation to run it headless")

    session_dir = path.parent / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    if record is None:
        record = {"record_version": RECORD_VERSION, "execution_id": execution_id, "target_repository": identity,
                  "target_worktree": str(worktree), "git_common_dir": str(common), "objective": objective,
                  "session_id": str(uuid.uuid4()), "session_dir": str(session_dir),
                  "log_path": str(path.with_suffix(".log")), "mutation_allowed": bool(allow_mutation),
                  "orchestrator_revision": workspace_plan.orchestrator_revision(ROOT), "created_at": _now(),
                  "launches": 0, "run_id": None, "run": None, "pid": None, "exit_code": None, "status": "created"}
    identity_env = _backend_identity(worktree, python)
    env = dict(os.environ)
    # An explicit limit is handed to the child; otherwise the extension falls back to the repository policy.
    for name in (LEAD_LIMIT_ENV, LEAD_LIMIT_SOURCE_ENV, LEAD_WARN_ENV, FAILURE_BUDGET_ENV, FAILURE_BUDGET_SOURCE_ENV,
                 INPUTS_DIGEST_ENV):
        env.pop(name, None)
    if failure_retry_budget is not None:
        env.update({FAILURE_BUDGET_ENV: str(failure_retry_budget),
                    FAILURE_BUDGET_SOURCE_ENV: failure_retry_budget_source or "cli"})
    if inputs_digest is not None:
        env[INPUTS_DIGEST_ENV] = inputs_digest
    if max_lead_calls is not None:
        env.update({LEAD_LIMIT_ENV: str(max_lead_calls), LEAD_LIMIT_SOURCE_ENV: max_lead_calls_source or "cli"})
        if lead_calls_warn_at is not None:
            env[LEAD_WARN_ENV] = str(lead_calls_warn_at)
    env.update({"PI_TARGET_REPOSITORY": identity, "PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY": identity_env,
                provenance.STATE_ENV: str(state_root()), "PI_ORCHESTRATION_EXECUTION_ID": execution_id})
    run = run_status(record.get("run_id")) or {}
    continuing = resume and run.get("status") == "IN_PROGRESS"
    # The launched Pi takes the run lease under this owner id: on start for a
    # new run, and on resume through the lifecycle's restore. For a resume the
    # run id is already known, so the lease is claimed here, before anything
    # launches; a live or stale lease of another owner refuses the resume.
    owner = lease_owner(execution_id)
    if continuing:
        claim_run_lease(record["run_id"], owner, takeover=takeover)
    env[LEASE_OWNER_ENV] = owner
    record["lease_owner"] = owner
    # Persist intent before the process exists, so a crash in between is seen
    # as an uncertain launch on restart rather than as never launched.
    record.update({"status": "launching", "pid": None})
    save_record(path, record)
    argv = [pi_executable, "--print", "--mode", "json", "--session-id", record["session_id"],
            "--session-dir", record["session_dir"]]
    for extension in EXTENSIONS:
        argv += ["-e", str(ROOT / "extensions" / extension)]
    argv += ["--", RESUME_PROMPT if continuing else build_prompt(record["objective"])]
    before = checkout_state(worktree)
    log = open(record["log_path"], "ab")
    try:
        child = subprocess.Popen(argv, cwd=str(worktree), env=env, shell=False, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        _OWN_CHILDREN[child.pid] = child
    except OSError as exc:
        log.close()
        record["status"] = "created" if record["launches"] == 0 else "interrupted"
        save_record(path, record)
        if continuing:
            try:
                run_lease.release(record["run_id"], owner)
            except (run_lease.LeaseError, OSError):
                pass
        raise HeadlessError("PI_UNAVAILABLE", f"cannot launch {pi_executable}: {exc}") from exc
    try:
        if continuing:
            # Hand the lease to the launched process, so its liveness is what keeps the lease live.
            try:
                run_lease.acquire(record["run_id"], owner, pid=child.pid, handoff=True)
            except (run_lease.LeaseError, OSError) as exc:
                # Never leave a child running under a lease it cannot hold: stop it and say so.
                code = exc.code if isinstance(exc, run_lease.LeaseError) else "RUN_LEASE_STORAGE_UNAVAILABLE"
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                child.wait()
                log.close()
                record.update({"status": "interrupted", "pid": None, "lease_handoff_error": code,
                               "launches": record["launches"] + 1, "outcome": "lease_handoff_failed"})
                save_record(path, record)
                raise HeadlessError("RUN_LEASE_HANDOFF_FAILED",
                                    f"the run lease could not be handed to the launched process ({code}); it was stopped") from exc
        # The limit belongs to this launch: a resume starts a fresh window from now and does not inherit an
        # earlier launch's limit (the caller passes it again).
        launched_at = clock()
        for stale in ("max_duration_seconds", "deadline_at", "deadline_epoch", "stop_reason", "time_limit",
                      "max_lead_calls", "max_lead_calls_source", "lead_calls_warn_at",
                      "failure_retry_budget", "failure_retry_budget_source", "inputs_digest"):
            record.pop(stale, None)
        if failure_retry_budget is not None:
            record.update({"failure_retry_budget": failure_retry_budget,
                           "failure_retry_budget_source": failure_retry_budget_source})
        if inputs_digest is not None:
            record["inputs_digest"] = inputs_digest
        if max_lead_calls is not None:
            record.update({"max_lead_calls": max_lead_calls, "max_lead_calls_source": max_lead_calls_source})
            if lead_calls_warn_at is not None:
                record["lead_calls_warn_at"] = lead_calls_warn_at
        if max_duration_seconds is not None:
            record.update({"max_duration_seconds": max_duration_seconds, "deadline_epoch": launched_at + max_duration_seconds,
                           "deadline_at": _iso(launched_at + max_duration_seconds)})
        record.update({"status": "launched", "pid": child.pid, "pid_start": run_lease.pid_start(child.pid),
                       "launches": record["launches"] + 1, "started_at": _now(), "exit_code": None,
                       "outcome": "resumed" if resume else "launched"})
        save_record(path, record)
        return record, child, log, before
    except BaseException:
        # Every exit after registration that does not hand the child to start() drops it.
        _OWN_CHILDREN.pop(child.pid, None)
        raise


def status(*, target: Path, explicit_repo: str, execution_id: str) -> dict:
    _, identity, common = _target(target, explicit_repo)
    path = execution_path(common, execution_id)
    if not path.is_file():
        raise HeadlessError("EXECUTION_UNKNOWN", "no execution record for this id")
    with locked_record(path):
        record = load_record(path)
        if record is None:
            raise HeadlessError("EXECUTION_UNKNOWN", "no execution record for this id")
        if record["target_repository"].casefold() != identity.casefold():
            raise HeadlessError("EXECUTION_TARGET_MISMATCH", "execution id belongs to another repository")
        refresh(record)
        enforce_time_limit(record)
        save_record(path, record)
        return record


def time_limit_remaining(record: dict, now: float | None = None) -> float | None:
    """Seconds left of an execution's limit (0 when over), or None when it has no limit."""
    deadline = record.get("deadline_epoch")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return None
    if now is None:
        elapsed = boot_elapsed(record)
        if elapsed is not None:
            return max(0.0, record["max_duration_seconds"] - elapsed)
    return max(0.0, deadline - (clock() if now is None else now))


def boot_elapsed(record: dict) -> float | None:
    """Seconds since the child started, from /proc, or None where that is unavailable.

    The child's start time (`pid_start`, clock ticks since boot) and /proc/uptime share one boot-time
    clock, so a wall-clock jump (NTP step, WSL2 resuming after host sleep) neither stops a child early
    nor lets it run late. Without /proc, enforcement falls back to the wall clock (`deadline_epoch`).
    """
    start = record.get("pid_start")
    if not isinstance(start, str) or not start.isdigit():
        return None
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return uptime - int(start) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def enforce_time_limit(record: dict, grace: float = TERMINATE_GRACE_SECONDS) -> bool:
    """Stop a still-running child whose deadline has passed. Caller holds the record lock.

    Only a child that is still the launched process is stopped (`execution_alive`, so a reused pid is
    never signalled). A child that already ended on its own is left to `refresh`'s normal outcome.
    Returns True when the execution was stopped by the limit.
    """
    if record.get("status") != "launched" or time_limit_remaining(record) != 0:
        return False
    if not execution_alive(record):
        return False
    _stop_locked(record, grace, TIME_LIMIT_CODE)
    return True


def _finish_cancelled(record: dict, code: str = "EXECUTION_CANCELLED") -> None:
    """Close the run as this execution's lease owner. Another owner's run is never closed from here."""
    run_id = record.get("run_id")
    if not run_id:
        return
    try:
        parent_orchestration.finish(run_id, "BLOCKED", blocker_code=code,
                                    owner_id=record.get("lease_owner"))
    except parent_orchestration.ParentRunError as exc:
        if exc.error_code.startswith("RUN_LEASE_"):
            record["cancel_error_code"] = exc.error_code
    except (provenance.ProvenanceError, OSError):
        pass  # already terminal or unreadable; the refreshed run status tells which


def cancel(*, target: Path, explicit_repo: str, execution_id: str, grace: float = TERMINATE_GRACE_SECONDS) -> dict:
    """Stop the process if it is alive and close the parent run explicitly as cancelled."""
    _, identity, common = _target(target, explicit_repo)
    path = execution_path(common, execution_id)
    if not path.is_file():
        raise HeadlessError("EXECUTION_UNKNOWN", "no execution record for this id")
    with locked_record(path):
        record = load_record(path)
        if record is None:
            raise HeadlessError("EXECUTION_UNKNOWN", "no execution record for this id")
        if record["target_repository"].casefold() != identity.casefold():
            raise HeadlessError("EXECUTION_TARGET_MISMATCH", "execution id belongs to another repository")
        refresh(record)
        if record["status"] not in {"exited", "cancelled"}:
            _stop_locked(record, grace, "EXECUTION_CANCELLED")
        save_record(path, record)
        return record


def _stop_locked(record: dict, grace: float, code: str) -> None:
    """Close the parent run BLOCKED with `code`, stop the child if it is still ours, and record the outcome.

    The caller holds the record lock and has refreshed the record. `code` is EXECUTION_CANCELLED for an
    operator cancel and EXECUTION_TIME_LIMIT for the wall-clock limit; both end as status `cancelled`.
    """
    limit_stop = code == TIME_LIMIT_CODE
    elapsed = None
    if limit_stop and record.get("deadline_epoch") is not None:
        elapsed = boot_elapsed(record)
        if elapsed is None:
            elapsed = clock() - (record["deadline_epoch"] - record["max_duration_seconds"])
        elapsed = round(elapsed, 3)
    if record.get("run_id") is None:
        record["run_id"] = discover_run_id(Path(record["session_dir"]), record["session_id"])
    # Close the run before signalling, so a graceful Pi shutdown that settles
    # the parent finds it already terminal with the stop code.
    _finish_cancelled(record, code)
    pid = record.get("pid")
    # Signal only the process this execution launched: the pid and its start time must both match.
    child = {"pid": pid, "pid_start": record.get("pid_start")}
    if record["status"] == "launched" and execution_alive(child):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + grace
        while execution_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        if execution_alive(child):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    record["pid"] = None
    if record.get("run_id") is None:
        # The process may have created its run between discovery and the signal.
        record["run_id"] = discover_run_id(Path(record["session_dir"]), record["session_id"])
        _finish_cancelled(record, code)
    record["status"] = "cancelled"
    limit_stop = limit_stop or record.get("stop_reason") == TIME_LIMIT_CODE  # a later cancel keeps the earlier outcome
    record["outcome"] = "time_limit" if limit_stop else "cancelled"
    if code == TIME_LIMIT_CODE:
        record["stop_reason"] = TIME_LIMIT_CODE
        record["time_limit"] = {"max_duration_seconds": record["max_duration_seconds"], "elapsed_seconds": elapsed,
                                "deadline_at": record.get("deadline_at"), "stopped_at": _now()}
    refresh(record)
    record["status"] = "cancelled"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headless single-repository orchestration run (start, status, resume, cancel).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("--target", type=Path, default=Path("."), help="target checkout (default: current directory)")
        p.add_argument("--repo", required=True, help="explicit owner/repository; must match a GitHub remote of the target")
        p.add_argument("--execution-id", required=True, help="caller-chosen idempotency key, filename-safe")
        if name == "start":
            p.add_argument("--objective", required=True)
            p.add_argument("--allow-mutation", action="store_true",
                           help="required when the target's autonomy policy grants any mutating permission")
            p.add_argument("--resume", action="store_true", help="relaunch an interrupted execution into the same parent run")
            p.add_argument("--takeover-stale-lease", action="store_true",
                           help="with --resume: explicitly recover a stale run lease (recorded in provenance)")
            p.add_argument("--no-wait", action="store_true", help="return after launching instead of waiting for exit")
            p.add_argument("--max-duration", type=int, default=None, metavar="SECONDS",
                           help="stop the child after this many wall-clock seconds (EXECUTION_TIME_LIMIT); default: no limit")
            p.add_argument("--max-lead-calls", type=int, default=None, metavar="N",
                           help="stop the run (left in progress, resumable only with a higher limit, LEAD_CALL_LIMIT) once the "
                                "lead has made this many model calls; default: the repository's .pi/autonomy.yaml limits.max_lead_calls, else no limit")
            p.add_argument("--lead-calls-warn-at", type=int, default=None, metavar="N",
                           help="tell the lead once to converge at this lead call (default: 80%% of the limit, rounded down)")
            p.add_argument("--failure-retry-budget", type=int, default=None, metavar="N",
                           help="retries a worker step gets after the same structured failure (same task, step, route epoch, "
                                "failure code, HEAD and inputs) before it is blocked with REPEATED_INEFFECTIVE_ACTION; "
                                "default: the repository's .pi/autonomy.yaml limits.failure_retry_budget, else 2")
            p.add_argument("--pi", default=os.environ.get("PI_EXECUTABLE", "pi"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            record = start(target=args.target, explicit_repo=args.repo, execution_id=args.execution_id,
                           objective=args.objective, allow_mutation=args.allow_mutation, resume=args.resume,
                           pi_executable=args.pi, wait=not args.no_wait, takeover=args.takeover_stale_lease,
                           max_duration_seconds=args.max_duration, max_lead_calls=args.max_lead_calls,
                           lead_calls_warn_at=args.lead_calls_warn_at, failure_retry_budget=args.failure_retry_budget)
        elif args.command == "status":
            record = status(target=args.target, explicit_repo=args.repo, execution_id=args.execution_id)
        else:
            record = cancel(target=args.target, explicit_repo=args.repo, execution_id=args.execution_id)
    except HeadlessError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.detail}, sort_keys=True))
        return 1
    run = record.get("run") or {}
    ok = record["status"] in {"launched", "exited", "cancelled", "interrupted"} and (
        record["status"] != "exited" or run.get("status") in {"COMPLETED", "PARTIAL"})
    print(json.dumps({"ok": ok, **record}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
