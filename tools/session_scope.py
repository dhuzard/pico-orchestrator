#!/usr/bin/env python3
"""Repository scope check for backend worker sessions.

A parent orchestration run is bound to one target repository. A Claude worker
runs in the Pi process working directory, and a resumed backend session carries
state from wherever it was created, so the parent verifies before every worker
invocation that the worker directory belongs to the same Git repository as the
target. Linked worktrees of the target share its common Git directory and are
therefore in scope; any other repository, or a directory that is not a Git
checkout, fails closed.

Operations (JSON on stdin, closed JSON verdict on stdout):

  {"target_cwd", "worker_cwd"}                  same repository?
  {"operation": "worktree", "target_cwd", "path"}
      is `path` a linked worktree of the target, distinct from the checkout the
      parent works in? Mutating workers must be bound to one. A path with a
      `.git` component (for example `<repo>/.git/worktrees-x/s1`) is refused
      with WORKER_WORKSPACE_PROTECTED_PATH: Claude Code treats every `.git`
      directory as protected and denies headless edits there even in
      acceptEdits mode, so a worker bound to it cannot write at all.
  {"operation": "snapshot", "path", "deadline_ms"?}
      complete digest of a checkout's HEAD (commit and symbolic ref name, or
      detached), its index (every entry's mode, object id, stage and
      assume-unchanged/skip-worktree flag), the worktree content of every tracked
      path that differs from the index or that Git hides from status, the lstat
      type and permission bits of every tracked path, every untracked file, and
      every root-level `.env`/`.env.*` entry even when ignored, used to detect the
      parent checkout changing while a worker runs. Other ignored paths are not
      covered. Only read-only plumbing runs (never `git diff`, which rewrites
      the index to refresh stat data), and never a program named by configuration
      (see snapshot_git_env). Every Git command and file read runs inside one monotonic
      deadline (`deadline_ms`, default SNAPSHOT_DEFAULT_DEADLINE_MS) so a slow
      checkout is reported as a typed failure before the caller's own timeout.
      Any failure -- a Git command that cannot start, exits nonzero, is killed,
      times out or prints malformed output, or an untracked file that cannot be
      hashed -- returns `ok: false` with metadata only (operation,
      suboperation, error kind, exit code, signal, errno, elapsed and deadline
      milliseconds) and never a digest: there is no partial digest.
  {"operation": "head", "path"}
      the checkout's HEAD commit id (`head` is null only for an unborn branch,
      verified as such), the commit part of a failure signature. Any other
      failure to resolve HEAD is `ok: false`.

Diagnostic Git runs with GIT_OPTIONAL_LOCKS=0 (no opportunistic index refresh
writes) and without repository-locating environment variables inherited from
the caller, so `-C <path>` alone decides which repository is read. Snapshot and
head Git commands additionally run without any inherited GIT_* variable and
without global or system configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

SNAPSHOT_FORMAT = b"pico-snapshot-v3"
SNAPSHOT_DEFAULT_DEADLINE_MS = 100_000
SNAPSHOT_MIN_DEADLINE_MS = 100
SNAPSHOT_MAX_DEADLINE_MS = 600_000
MAX_LISTING_BYTES = 256 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_STATUS_ENTRIES_REPORTED = 200
READ_CHUNK = 1024 * 1024
HEX_OID = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")
GITLINK_MODE = b"160000"
# Root-level ignored entries the snapshot still covers: exactly `.env` and `.env.<anything>` (case-sensitive).
ROOT_ENV_NAME = b".env"
# Inherited variables that would redirect `git -C <path>` to another repository, index or object store.
REPOSITORY_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")
# Programs that the commands a snapshot runs (rev-parse, symbolic-ref, show-ref, config, ls-files, status and the
# `git status` that status runs inside each populated submodule) could start from repository-local configuration.
# fsmonitor runs whenever an index is read; hooks are disabled outright. Clean filters run when status re-hashes a
# stat-dirty file; they are neutralised per driver name (see snapshot_git_env).
SNAPSHOT_CONFIG = (("core.fsmonitor", "false"), ("core.hooksPath", os.devnull))
FILTER_OVERRIDES = (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))


def git_env() -> dict:
    env = {key: value for key, value in os.environ.items() if key not in REPOSITORY_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def snapshot_git_env(filters=()) -> dict:
    """Environment for every snapshot and head Git command.

    No inherited GIT_* variable survives: not the configuration channels (GIT_CONFIG_COUNT/KEY_n/VALUE_n,
    GIT_CONFIG_PARAMETERS, GIT_CONFIG_GLOBAL/SYSTEM), not GIT_EXEC_PATH (which picks the `git` a submodule status
    runs), not trace targets, not repository locators. Global and system configuration are off, so only the
    repository's own configuration is read, and the executable integrations it could name are overridden at
    command-line precedence through GIT_CONFIG_COUNT, which Git passes on to the status it runs in submodules.
    Each filter driver named in a configuration the snapshot reads gets an empty clean/smudge/process command
    (Git then applies no filter) and required=false.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull})
    pairs = list(SNAPSHOT_CONFIG) + [(f"filter.{name}.{key}", value) for name in sorted(set(filters))
                                     for key, value in FILTER_OVERRIDES]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for number, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{number}"] = key
        env[f"GIT_CONFIG_VALUE_{number}"] = value
    return env


def common_dir(path: Path, *, run=subprocess.run) -> Path | None:
    try:
        result = run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                     shell=False, capture_output=True, text=True, timeout=10, env=git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def check(target_cwd: str, worker_cwd: str, *, run=subprocess.run) -> dict:
    target = common_dir(Path(target_cwd), run=run)
    if target is None:
        return {"ok": False, "blocker_code": "SESSION_TARGET_UNRESOLVED"}
    worker = common_dir(Path(worker_cwd), run=run)
    if worker is None or worker != target:
        return {"ok": False, "blocker_code": "SESSION_REPOSITORY_MISMATCH"}
    return {"ok": True, "blocker_code": None}


def _git_out(path: Path, *args: str, run=subprocess.run) -> bytes | None:
    try:
        result = run(["git", "-C", str(path), *args], shell=False, capture_output=True, timeout=60, env=git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _toplevel(path: Path, *, run=subprocess.run) -> Path | None:
    out = _git_out(path, "rev-parse", "--show-toplevel", run=run)
    return Path(out.decode().strip()).resolve() if out and out.strip() else None


def in_git_dir(path: Path) -> bool:
    """True when any component of the resolved path is a `.git` directory name."""
    return ".git" in path.resolve().parts


def worktree(target_cwd: str, path: str, *, run=subprocess.run) -> dict:
    if not check(target_cwd, path, run=run)["ok"]:
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_INVALID"}
    target_top, workspace_top = _toplevel(Path(target_cwd), run=run), _toplevel(Path(path), run=run)
    if workspace_top is None or workspace_top == target_top or workspace_top != Path(path).resolve():
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_INVALID"}
    if in_git_dir(workspace_top):
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_PROTECTED_PATH"}
    if target_top is None or workspace_top.is_relative_to(target_top) or target_top.is_relative_to(workspace_top):
        return {"ok": False, "blocker_code": "WORKER_WORKSPACE_NESTED"}
    return {"ok": True, "blocker_code": None, "path": str(workspace_top)}


class SnapshotFailure(Exception):
    """A snapshot step that could not complete; carries metadata only, never Git output."""

    def __init__(self, suboperation: str, error_kind: str, *, exit_code: int | None = None,
                 signal_number: int | None = None, errno_value: int | None = None,
                 blocker_code: str = "WORKER_SCOPE_SNAPSHOT_FAILED") -> None:
        super().__init__(f"{suboperation}: {error_kind}")
        self.suboperation = suboperation
        self.error_kind = error_kind
        self.exit_code = exit_code
        self.signal_number = signal_number
        self.errno_value = errno_value
        self.blocker_code = blocker_code


# Git children running now, so a terminating signal can take their process groups down with the helper.
_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _terminate(signum, _frame) -> None:  # pragma: no cover - exercised through the subprocess tests
    with _ACTIVE_LOCK:
        active = list(_ACTIVE)
    for proc in active:
        _kill(proc)
    os._exit(128 + signum)


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _run_git(root: Path, args: list[str], deadline: float, suboperation: str, *, sink=None,
             limit: int | None = None, ok_codes: tuple[int, ...] = (0,), env: dict | None = None) -> tuple[int, bytes]:
    """Run one Git command inside the deadline, streaming stdout to `sink` or collecting at most `limit` bytes.

    The child runs in its own process group (POSIX) so a timeout kills everything it started, and its
    stderr is discarded: diagnostics carry the exit status, never Git's text. The environment is always the
    hardened snapshot_git_env (by default without filter-driver overrides).
    """
    if _remaining(deadline) <= 0:
        raise SnapshotFailure(suboperation, "DeadlineExceeded")
    try:
        proc = subprocess.Popen(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env if env is not None else snapshot_git_env(),
                                start_new_session=os.name == "posix")
    except OSError as exc:
        raise SnapshotFailure(suboperation, "SpawnError", errno_value=exc.errno) from None
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    chunks: list[bytes] = []
    state = {"size": 0, "overflow": False, "error": None}

    def reader() -> None:
        try:
            while True:
                data = proc.stdout.read1(READ_CHUNK)
                if not data:
                    return
                if sink is not None:
                    sink(data)
                    continue
                state["size"] += len(data)
                if limit is not None and state["size"] > limit:
                    state["overflow"] = True
                    _kill(proc)
                    return
                chunks.append(data)
        except Exception as exc:  # noqa: BLE001 - reported as a typed failure, never swallowed
            state["error"] = exc

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        try:
            proc.wait(timeout=max(0.0, _remaining(deadline)))
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise SnapshotFailure(suboperation, "Timeout") from None
        # A descendant that still holds stdout open keeps the reader alive: bounded by the same deadline.
        thread.join(max(0.0, _remaining(deadline)))
        if thread.is_alive():
            _kill(proc)
            raise SnapshotFailure(suboperation, "Timeout")
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    if state["overflow"]:
        raise SnapshotFailure(suboperation, "OutputTooLarge")
    if state["error"] is not None:
        raise SnapshotFailure(suboperation, "ReadError")
    code = proc.returncode
    if code is not None and code < 0:
        raise SnapshotFailure(suboperation, "Signal", signal_number=-code)
    if code not in ok_codes:
        raise SnapshotFailure(suboperation, "NonzeroExit", exit_code=code)
    return code, b"".join(chunks)


def _resolve_head(root: Path, deadline: float) -> tuple[str | None, str]:
    """HEAD commit id and `commit`, or None and `unborn` only for a symbolic HEAD whose branch does not exist."""
    code, out = _run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], deadline, "head",
                         limit=1024, ok_codes=(0, 1))
    if code == 0:
        value = out.strip()
        if not HEX_OID.fullmatch(value):
            raise SnapshotFailure("head", "MalformedOutput")
        return value.decode("ascii"), "commit"
    if out.strip():
        raise SnapshotFailure("head", "MalformedOutput")
    code, ref = _run_git(root, ["symbolic-ref", "--quiet", "HEAD"], deadline, "head_symbolic",
                         limit=4096, ok_codes=(0, 1))
    ref = ref.strip()
    if code != 0 or not ref.startswith(b"refs/heads/"):
        # A detached HEAD that does not name a commit is broken, not unborn.
        raise SnapshotFailure("head_symbolic", "HeadUnresolved", exit_code=code)
    # Exit 1: the branch does not exist (unborn). A dangling or unreadable ref exits 128 and fails.
    code, _ = _run_git(root, ["show-ref", "--verify", "--quiet", ref.decode("utf-8", "surrogateescape")],
                       deadline, "head_ref", limit=4096, ok_codes=(0, 1))
    if code == 0:
        raise SnapshotFailure("head_ref", "HeadUnresolved", exit_code=0)
    return None, "unborn"


def _head_name(root: Path, deadline: float) -> bytes:
    """The ref HEAD points at (`symbolic` + name, also for an unborn branch), or `detached`."""
    code, ref = _run_git(root, ["symbolic-ref", "--quiet", "HEAD"], deadline, "head_symbolic",
                         limit=4096, ok_codes=(0, 1))
    ref = ref.removesuffix(b"\n")
    if code == 0 and ref and b"\n" not in ref:
        return b"symbolic\0" + ref
    if code == 1 and not ref:
        return b"detached"
    raise SnapshotFailure("head_symbolic", "MalformedOutput")


def _status_entries(status: bytes) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """Porcelain v1 -z records, untracked paths, and tracked paths whose worktree differs from the index.

    A rename/copy record carries its source as the next field.
    """
    if status and not status.endswith(b"\0"):
        raise SnapshotFailure("status", "MalformedOutput")
    tokens = status.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries: list[bytes] = []
    untracked: list[bytes] = []
    dirty: list[bytes] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4 or token[2:3] != b" ":
            raise SnapshotFailure("status", "MalformedOutput")
        code, path = token[:2], token[3:]
        if b"R" in code or b"C" in code:
            if index >= len(tokens) or not tokens[index]:
                raise SnapshotFailure("status", "MalformedOutput")
            index += 1
        entries.append(token)
        if code == b"??":
            untracked.append(path)
        elif code[1:2] not in (b" ", b"D", b"!"):
            dirty.append(path)
    return entries, untracked, dirty


def _hash_paths(top: bytes, paths: list[bytes], deadline: float, suboperation: str,
                directory_error: str | None = None) -> bytes:
    """Digest of the current content of every path: one record per path, or a failure. Never a partial result.

    Regular files are hashed with their executable bit, symlinks by target; an absent path is recorded as
    absent. An untracked nested repository is recorded by name only: its contents belong to that repository.
    Where a directory cannot be verified by name alone (a dirty tracked submodule, a root `.env` directory),
    `directory_error` makes it a failure.
    """
    digest = hashlib.sha256()
    hashed = 0
    for relative in paths:
        if _remaining(deadline) <= 0:
            raise SnapshotFailure(suboperation, "DeadlineExceeded")
        digest.update(len(relative).to_bytes(8, "big") + relative)
        full = top + b"/" + relative.rstrip(b"/")
        try:
            info = os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            digest.update(b"X")
            hashed += 1
            continue
        except OSError as exc:
            raise SnapshotFailure(suboperation, "OSError", errno_value=exc.errno) from None
        if stat.S_ISDIR(info.st_mode):
            if directory_error is not None:
                raise SnapshotFailure(suboperation, directory_error)
            digest.update(b"D")
        elif stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(full)
            except OSError as exc:
                raise SnapshotFailure(suboperation, "OSError", errno_value=exc.errno) from None
            digest.update(b"L" + hashlib.sha256(target).digest())
        elif stat.S_ISREG(info.st_mode):
            content = hashlib.sha256()
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                descriptor = os.open(full, flags)
                try:
                    while True:
                        if _remaining(deadline) <= 0:
                            raise SnapshotFailure(suboperation, "DeadlineExceeded")
                        block = os.read(descriptor, READ_CHUNK)
                        if not block:
                            break
                        content.update(block)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise SnapshotFailure(suboperation, "OSError", errno_value=exc.errno) from None
            executable = b"x" if info.st_mode & 0o111 else b"-"
            digest.update(b"F" + executable + content.digest())
        else:
            digest.update(b"O" + stat.S_IFMT(info.st_mode).to_bytes(4, "big"))
        hashed += 1
    if hashed != len(paths):
        raise SnapshotFailure(suboperation, "Incomplete")
    return digest.digest()


def _real_parents(top: bytes, relative: bytes, directories: dict[bytes, bool], suboperation: str) -> bool:
    """True when every leading component of `relative` is a real directory (lstat, never following a symlink)."""
    end = relative.find(b"/")
    while end != -1:
        prefix = relative[:end]
        known = directories.get(prefix)
        if known is None:
            try:
                known = stat.S_ISDIR(os.lstat(top + b"/" + prefix).st_mode)
            except (FileNotFoundError, NotADirectoryError):
                known = False
            except OSError as exc:
                raise SnapshotFailure(suboperation, "OSError", errno_value=exc.errno) from None
            directories[prefix] = known
        if not known:
            return False
        end = relative.find(b"/", end + 1)
    return True


def _tracked_modes(top: bytes, paths: list[bytes], deadline: float, suboperation: str = "modes") -> bytes:
    """Digest of the lstat file type and permission bits of every path, without reading any content.

    Git ignores permission bits other than the executable bit, and under core.fileMode=false that one too, so a
    chmod of a clean tracked file never reaches status. A symlink is never followed (neither as the path nor as a
    leading component, which is recorded as such); a gitlink directory is lstat'ed, never entered.
    """
    digest = hashlib.sha256()
    directories: dict[bytes, bool] = {}
    for relative in paths:
        if _remaining(deadline) <= 0:
            raise SnapshotFailure(suboperation, "DeadlineExceeded")
        digest.update(len(relative).to_bytes(8, "big") + relative)
        if not _real_parents(top, relative, directories, suboperation):
            digest.update(b"P")
            continue
        try:
            info = os.lstat(top + b"/" + relative)
        except (FileNotFoundError, NotADirectoryError):
            digest.update(b"X")
            continue
        except OSError as exc:
            raise SnapshotFailure(suboperation, "OSError", errno_value=exc.errno) from None
        digest.update(b"M" + info.st_mode.to_bytes(4, "big"))
    return digest.digest()


def _index_entries(listing: bytes, suboperation: str = "index") -> list[tuple[bytes, bytes, bytes, bytes]]:
    """`ls-files -v --stage -z` records as (tag, mode, stage, path); the tag marks assume-unchanged/skip-worktree."""
    if listing and not listing.endswith(b"\0"):
        raise SnapshotFailure(suboperation, "MalformedOutput")
    tokens = listing.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    records = []
    for token in tokens:
        meta, tab, path = token.partition(b"\t")
        fields = meta.split(b" ")
        if (not tab or not path or len(fields) != 4 or len(fields[0]) != 1 or not fields[1].isdigit()
                or not HEX_OID.fullmatch(fields[2]) or fields[3] not in (b"0", b"1", b"2", b"3")):
            raise SnapshotFailure(suboperation, "MalformedOutput")
        records.append((fields[0], fields[1], fields[3], path))
    return records


def _filter_drivers(repo: Path, deadline: float, suboperation: str) -> set[str]:
    """Names of the filter drivers the repository's own configuration (with its includes) defines."""
    code, out = _run_git(repo, ["config", "-z", "--get-regexp", r"^filter\."], deadline, suboperation,
                         limit=MAX_CONFIG_BYTES, ok_codes=(0, 1))
    if code == 1:  # no filter key at all
        if out:
            raise SnapshotFailure(suboperation, "MalformedOutput")
        return set()
    if not out.endswith(b"\0"):
        raise SnapshotFailure(suboperation, "MalformedOutput")
    names = set()
    for record in out[:-1].split(b"\0"):
        key = record.partition(b"\n")[0]
        if not key.startswith(b"filter."):
            raise SnapshotFailure(suboperation, "MalformedOutput")
        if key.count(b".") >= 2:  # filter.<driver>.<variable>; a key without a driver name defines nothing
            names.add(os.fsdecode(key[len(b"filter."):key.rindex(b".")]))
    return names


def _integration_filters(top: bytes, records: list, deadline: float) -> set[str]:
    """Filter drivers defined by the checkout and by every populated submodule `git status` will recurse into."""
    names = _filter_drivers(Path(os.fsdecode(top)), deadline, "config")
    pending = [(top, records)]
    seen = {os.path.realpath(top)}
    while pending:
        repo, entries = pending.pop()
        directories: dict[bytes, bool] = {}
        for path in sorted({path for _, mode, _, path in entries if mode == GITLINK_MODE}):
            if _remaining(deadline) <= 0:
                raise SnapshotFailure("submodule_config", "DeadlineExceeded")
            child = repo + b"/" + path
            if not _real_parents(repo, path, directories, "submodule_config"):
                continue
            try:
                populated = stat.S_ISDIR(os.lstat(child).st_mode) and os.path.lexists(child + b"/.git")
            except (FileNotFoundError, NotADirectoryError):
                populated = False
            except OSError as exc:
                raise SnapshotFailure("submodule_config", "OSError", errno_value=exc.errno) from None
            real = os.path.realpath(child)
            if not populated or real in seen:
                continue
            seen.add(real)
            names |= _filter_drivers(Path(os.fsdecode(child)), deadline, "submodule_config")
            _, listing = _run_git(Path(os.fsdecode(child)), ["ls-files", "-v", "--stage", "-z"], deadline,
                                  "submodule_index", limit=MAX_LISTING_BYTES)
            pending.append((child, _index_entries(listing, "submodule_index")))
    return names


def _root_env(top: bytes, deadline: float) -> list[bytes]:
    """Root-level entries named `.env` or `.env.*`, from one shallow listing of the checkout root."""
    names = []
    try:
        with os.scandir(top) as entries:
            for entry in entries:
                if _remaining(deadline) <= 0:
                    raise SnapshotFailure("root_env", "DeadlineExceeded")
                if entry.name == ROOT_ENV_NAME or entry.name.startswith(ROOT_ENV_NAME + b"."):
                    names.append(entry.name)
    except OSError as exc:
        raise SnapshotFailure("root_env", "OSError", errno_value=exc.errno) from None
    return sorted(names)


def _deadline_ms(value) -> int:
    if value is None:
        return SNAPSHOT_DEFAULT_DEADLINE_MS
    if type(value) is not int or not SNAPSHOT_MIN_DEADLINE_MS <= value <= SNAPSHOT_MAX_DEADLINE_MS:
        raise TypeError("deadline_ms")
    return value


def snapshot(path: str, *, deadline_ms: int | None = None) -> dict:
    budget = _deadline_ms(deadline_ms)
    started = time.monotonic()
    deadline = started + budget / 1000
    root = Path(path)

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        try:
            _, top = _run_git(root, ["rev-parse", "--show-toplevel"], deadline, "repository", limit=65536)
        except SnapshotFailure as failure:
            if failure.error_kind == "NonzeroExit":
                failure.blocker_code = "SESSION_TARGET_UNRESOLVED"
            raise
        top = top.removesuffix(b"\n")
        if not top:
            raise SnapshotFailure("repository", "MalformedOutput")
        root = Path(os.fsdecode(top))
        head, head_state = _resolve_head(root, deadline)
        head_name = _head_name(root, deadline)
        digest = hashlib.sha256(SNAPSHOT_FORMAT + b"\0")

        def section(name: str, value: bytes) -> None:
            digest.update(name.encode("ascii") + b"\0" + value)

        # The commit, and which ref HEAD names (two branches at one commit, detached, or an unborn branch's name).
        section("head", (head or "").encode("ascii") + b"\0" + head_state.encode("ascii") + b"\0" + head_name)
        # The index itself: mode, object id and stage of every entry, so staged content is covered exactly, and the
        # assume-unchanged/skip-worktree tags. Plumbing only: unlike `git diff`, nothing here rewrites the index.
        _, listing = _run_git(root, ["ls-files", "-v", "--stage", "-z"], deadline, "index", limit=MAX_LISTING_BYTES)
        records = _index_entries(listing)
        filters = _integration_filters(top, records, deadline)
        _, status = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"], deadline,
                             "status", limit=MAX_LISTING_BYTES, env=snapshot_git_env(filters))
        entries, untracked, dirty = _status_entries(status)
        section("status", hashlib.sha256(status).digest())
        section("index", hashlib.sha256(listing).digest())
        # Unstaged content: the worktree bytes of every tracked path status reports as differing from the index,
        # and of every path Git hides from status; then every untracked path.
        hidden = [path for tag, _, _, path in records if tag.islower() or tag == b"S"]
        tracked = sorted(set(dirty) | set(hidden))
        section("worktree", len(tracked).to_bytes(8, "big") + _hash_paths(top, tracked, deadline, "worktree_hash",
                                                                          "TrackedDirectoryUnverifiable"))
        # Type and permission bits of every tracked path (conflict stages once), by lstat only.
        paths = sorted({path for _, _, _, path in records})
        section("modes", len(paths).to_bytes(8, "big") + _tracked_modes(top, paths, deadline))
        section("untracked", len(untracked).to_bytes(8, "big") + _hash_paths(top, untracked, deadline, "untracked_hash"))
        # Root `.env`/`.env.*` whether ignored or not: content, symlink target, and type and permission bits.
        env_files = _root_env(top, deadline)
        section("root_env", len(env_files).to_bytes(8, "big")
                + _hash_paths(top, env_files, deadline, "root_env", "DirectoryUnverifiable")
                + _tracked_modes(top, env_files, deadline, "root_env"))
    except SnapshotFailure as failure:
        return {"ok": False, "blocker_code": failure.blocker_code, "operation": "snapshot",
                "suboperation": failure.suboperation, "error_kind": failure.error_kind,
                "exit_code": failure.exit_code, "signal": failure.signal_number, "errno": failure.errno_value,
                "elapsed_ms": elapsed(), "deadline_ms": budget}
    return {"ok": True, "digest": digest.hexdigest(), "head": head, "head_state": head_state,
            "status": [entry.decode("utf-8", errors="replace") for entry in entries[:MAX_STATUS_ENTRIES_REPORTED]],
            "status_count": len(entries), "untracked_count": len(untracked), "index_count": len(records),
            "elapsed_ms": elapsed(), "deadline_ms": budget}


def head(path: str) -> dict:
    root = Path(path)
    deadline = time.monotonic() + SNAPSHOT_DEFAULT_DEADLINE_MS / 1000
    try:
        try:
            _run_git(root, ["rev-parse", "--git-dir"], deadline, "repository", limit=65536)
        except SnapshotFailure as failure:
            if failure.error_kind == "NonzeroExit":
                failure.blocker_code = "SESSION_TARGET_UNRESOLVED"
            raise
        value, _ = _resolve_head(root, deadline)
    except SnapshotFailure as failure:
        return {"ok": False, "blocker_code": failure.blocker_code if failure.blocker_code != "WORKER_SCOPE_SNAPSHOT_FAILED"
                else "SESSION_HEAD_UNRESOLVED", "operation": "head", "suboperation": failure.suboperation,
                "error_kind": failure.error_kind, "exit_code": failure.exit_code, "signal": failure.signal_number}
    return {"ok": True, "head": value}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        operation = payload.get("operation", "check")
        if operation == "head":
            if not isinstance(payload["path"], str):
                raise TypeError
            print(json.dumps(head(payload["path"])))
            return 0
        if operation == "snapshot":
            if not isinstance(payload["path"], str) or set(payload) - {"operation", "path", "deadline_ms"}:
                raise TypeError
            deadline_ms = _deadline_ms(payload.get("deadline_ms"))
            if os.name == "posix":
                # The caller's timeout sends SIGTERM to this process group first: take running Git with us.
                signal.signal(signal.SIGTERM, _terminate)
            print(json.dumps(snapshot(payload["path"], deadline_ms=deadline_ms)))
            return 0
        target_cwd = payload["target_cwd"]
        other = payload["path"] if operation == "worktree" else payload["worker_cwd"]
        if operation not in {"check", "worktree"} or not isinstance(target_cwd, str) or not isinstance(other, str):
            raise TypeError
    except (ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"ok": False, "blocker_code": "SESSION_SCOPE_INPUT_INVALID"}))
        return 0
    print(json.dumps(worktree(target_cwd, other) if operation == "worktree" else check(target_cwd, other)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
