#!/usr/bin/env python3
"""Approval-gated builder for a new workspace manifest (`pico workspace init`).

Interactive (a terminal is required):

    pico workspace init [--root DIR] [--output FILE]

discovers Git checkouts below a root (default: the directory the command runs
in), lets the user search and select repositories by number, add repositories
that are not cloned yet, and choose an identity for a checkout with several
GitHub remotes. Non-interactive:

    pico workspace init --spec FILE [--output FILE] [--approve]

reads a builder spec (YAML or JSON, see docs/workspace-interactive-design.md).
Without --approve it only prints the preview.

Both modes resolve every repository read-only (`git rev-parse`/`remote` and
`gh repo view`/`gh api -X GET`), build an unchanged schema_version 1 manifest
with one planned task per repository and preview it together with the
planner's waves and blockers, computed in memory. A file is written only
after an explicit "yes" or --approve, and only as a new file: an existing
file, directory or symlink is never replaced. Nothing is cloned, fetched,
pulled or launched, and the discovery root grants no permission.

Exit codes: 0 previewed or saved, 1 not saved (cancelled, declined,
resolution failed, destination exists), 2 invalid invocation or spec,
130 interrupted.
"""
from __future__ import annotations

import sys

# A preview must leave no trace: no __pycache__ in the runtime snapshot or checkout.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import unicodedata  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, NoReturn, TextIO  # noqa: E402

import yaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_integration  # noqa: E402
import github_sync as gh  # noqa: E402
import provenance  # noqa: E402
import session_scope  # noqa: E402
import workspace_plan as wp  # noqa: E402

SPEC_VERSION = 1
MIN_REPOSITORIES = 2
MAX_REPOSITORIES = 64
MAX_SPEC_BYTES = 1024 * 1024
# Discovery bounds: directories deeper than DISCOVERY_MAX_DEPTH levels below
# the root are not searched, the walk stops after DISCOVERY_MAX_DIRECTORIES
# directories, and at most DISCOVERY_MAX_CANDIDATES checkouts are listed.
# Every bound that is hit is reported.
DISCOVERY_MAX_DEPTH = 6
DISCOVERY_MAX_DIRECTORIES = 20000
DISCOVERY_MAX_CANDIDATES = 256
# Directory names never descended into: Git metadata, dependency and vendor
# trees, caches, build output and orchestrator runtime/state trees (`.pi`
# holds PICO worktrees, `.claude` Claude Code worktrees, `pi-coding-orchestrator`
# the launcher cache and state). `*.egg-info` is pruned too.
PRUNED_DIRECTORY_NAMES = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor", ".venv", "venv", "site-packages", ".eggs",
    "__pycache__", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    ".gradle", ".terraform",
    "build", "dist", "target", ".next", ".nuxt",
    ".pi", ".claude", ".pico", "pi-coding-orchestrator",
})
PRUNED_DIRECTORY_SUFFIXES = (".egg-info",)

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Same as schemas/workspace.schema.json repositories[].repository.
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
BASE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SPEC_KEYS = frozenset({"version", "root", "id", "goal", "max_parallel", "repositories"})
SPEC_REQUIRED = ("version", "root", "id", "goal", "repositories")
ENTRY_KEYS = frozenset({"id", "repository", "path", "expected_path", "task"})
TASK_KEYS = frozenset({"title", "objective"})
# github_integration reads Git output as UTF-8; a checkout whose path is not is refused, not guessed.
NOT_UTF8_PATH = "Git reports a path that is not UTF-8, which the GitHub identity check cannot read"
MALFORMED_REMOTE = "a Git remote URL is malformed and cannot be checked for a GitHub identity"
# Variables that would make git answer for another repository than the path asked about.
GIT_ENV_CONFLICTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")

EXIT_OK, EXIT_NOT_SAVED, EXIT_INVALID, EXIT_INTERRUPTED = 0, 1, 2, 130

Github = Callable[[str], dict]


class InitError(Exception):
    def __init__(self, code: str, detail: str, exit_code: int = EXIT_INVALID):
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        super().__init__(f"{code}: {detail}")


class Cancelled(Exception):
    """The user cancelled (EOF, blank required answer or `q`); nothing is written."""


# -- terminal display -------------------------------------------------------------
# Filesystem names, Git remotes, GitHub answers, environment values and OS errors
# are untrusted text. Each value is passed through display() before it is put in a
# terminal line, so it cannot add lines, move the cursor or reorder the text. The
# raw value is still what reaches the filesystem, Git, comparisons and the saved YAML.

# Cc: C0, DEL and C1 controls; Cf: format characters, bidi controls included;
# Cs: surrogates (os.fsdecode turns undecodable filename bytes into U+DC80-U+DCFF);
# Zl/Zp: the Unicode line and paragraph separators.
DISPLAY_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_char(ch: str) -> str:
    if ch in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[ch]
    code = ord(ch)
    if code < 0x80:
        return f"\\x{code:02x}"
    if 0xDC80 <= code <= 0xDCFF:  # a surrogateescaped filename byte: show the byte
        return f"\\x{code - 0xDC00:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def display(value: Any) -> str:
    """One untrusted value as a single terminal-safe line; printable text is unchanged."""
    text = str(value)
    if not any(unicodedata.category(ch) in DISPLAY_ESCAPED_CATEGORIES for ch in text):
        return text
    return "".join(_escape_char(ch) if unicodedata.category(ch) in DISPLAY_ESCAPED_CATEGORIES else ch
                   for ch in text)


def display_lines(text: str) -> str:
    """display() for each line of a message whose own line breaks are its layout."""
    return "\n".join(display(line) for line in text.split("\n"))


def _display_tree(value: Any) -> Any:
    """A copy of a plan with every string escaped, for rendering only."""
    if isinstance(value, (str, Path)):
        return display(value)
    if isinstance(value, dict):
        return {_display_tree(key): _display_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_display_tree(item) for item in value]
    return value


# -- strict spec loading -------------------------------------------------------

class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate and non-string mapping keys."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(None, None, f"mapping key {key!r} is not a string",
                                                    key_node.start_mark)
        if key in mapping:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def strict_load(text: str) -> Any:
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass


def load_spec(path: Path) -> Any:
    try:
        info = os.stat(path)
    except OSError as exc:
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", f"spec is not readable: {display(exc)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", f"spec is not a regular file: {display(path)}")
    if info.st_size > MAX_SPEC_BYTES:
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", f"spec is larger than {MAX_SPEC_BYTES} bytes")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", f"spec is not readable UTF-8: {display(exc)}") from exc
    try:
        return strict_load(text)
    except yaml.YAMLError as exc:
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", f"spec is not valid YAML/JSON: {display_lines(str(exc))}") from exc


# -- normalized spec ------------------------------------------------------------

@dataclass
class Entry:
    id: str
    repository: str | None
    path: Path  # absolute and normalized; resolved for local checkouts during resolution
    local: bool
    title: str | None = None
    objective: str | None = None
    identity_choice: list[str] | None = None  # remotes the user chose among (interactive)


@dataclass
class Spec:
    root: Path
    id: str
    goal: str
    max_parallel: int
    entries: list[Entry]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normal(base: Path, value: str) -> Path:
    return Path(os.path.normpath(base / os.path.expanduser(value)))


def _check_objective(value: str) -> str | None:
    if not value or len(value) > 4000 or CONTROL_RE.search(value):
        return "objective must be one line of 1-4000 characters"
    if provenance._contains_secret_like_content(value):
        return "objective looks like it contains a secret"
    return None


def validate_spec(raw: Any, *, base: Path) -> Spec:
    """Validate a builder spec strictly; `root` is relative to `base` (the spec's directory)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", "spec must be a mapping")
    for key in sorted(set(raw) - SPEC_KEYS):
        errors.append(f"unknown field {key!r}")
    for key in SPEC_REQUIRED:
        if key not in raw:
            errors.append(f"missing field {key!r}")
    if "version" in raw and (not _is_int(raw["version"]) or raw["version"] != SPEC_VERSION):
        errors.append(f"version must be the integer {SPEC_VERSION}")
    root: Path | None = None
    if "root" in raw:
        if not isinstance(raw["root"], str) or not raw["root"]:
            errors.append("root must be a non-empty string")
        else:
            candidate = _normal(base, raw["root"])
            if not candidate.is_dir():
                errors.append(f"root is not an existing directory: {display(candidate)}")
            else:
                root = candidate.resolve()
    if "id" in raw and (not isinstance(raw["id"], str) or not SLUG_RE.fullmatch(raw["id"])):
        errors.append("id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    if "goal" in raw and (not isinstance(raw["goal"], str) or not 1 <= len(raw["goal"]) <= 2000):
        errors.append("goal must be a string of 1-2000 characters")
    max_parallel = raw.get("max_parallel", 1)
    if not _is_int(max_parallel) or not 1 <= max_parallel <= 64:
        errors.append("max_parallel must be an integer from 1 to 64")
    entries: list[Entry] = []
    repositories = raw.get("repositories")
    if "repositories" in raw:
        if not isinstance(repositories, list):
            errors.append("repositories must be a list")
        elif not MIN_REPOSITORIES <= len(repositories) <= MAX_REPOSITORIES:
            errors.append(f"repositories must list {MIN_REPOSITORIES} to {MAX_REPOSITORIES} entries")
        else:
            for index, item in enumerate(repositories):
                entry = _validate_entry(item, f"repositories/{index}", root, errors)
                if entry is not None:
                    entries.append(entry)
    seen_ids: set[str] = set()
    seen_identities: dict[str, str] = {}
    seen_paths: dict[Path, str] = {}
    for entry in entries:
        if entry.id in seen_ids:
            errors.append(f"duplicate repository id {entry.id!r}")
        seen_ids.add(entry.id)
        if entry.repository is not None:
            key = entry.repository.casefold()
            if key in seen_identities:
                errors.append(f"{entry.id}: same repository {entry.repository} as {seen_identities[key]}")
            seen_identities.setdefault(key, entry.id)
        if entry.path in seen_paths:
            errors.append(f"{entry.id}: same path as {seen_paths[entry.path]}")
        seen_paths.setdefault(entry.path, entry.id)
    if errors:
        raise InitError("WORKSPACE_INIT_SPEC_INVALID", "; ".join(errors))
    assert root is not None
    return Spec(root=root, id=raw["id"], goal=raw["goal"], max_parallel=max_parallel, entries=entries)


def _validate_entry(item: Any, where: str, root: Path | None, errors: list[str]) -> Entry | None:
    if not isinstance(item, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    ok = True
    for key in sorted(set(item) - ENTRY_KEYS):
        errors.append(f"{where}: unknown field {key!r}")
        ok = False
    entry_id = item.get("id")
    if not isinstance(entry_id, str) or not SLUG_RE.fullmatch(entry_id):
        errors.append(f"{where}/id: must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$")
        ok = False
    repository = item.get("repository")
    if "repository" in item and (not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository)
                                 or ".." in repository):
        errors.append(f"{where}/repository: must be owner/repository")
        ok = False
    present = [key for key in ("path", "expected_path") if key in item]
    if len(present) != 1:
        errors.append(f"{where}: needs exactly one of path (local checkout) or expected_path (not cloned yet)")
        return None
    value = item[present[0]]
    if not isinstance(value, str) or not value or CONTROL_RE.search(value):
        errors.append(f"{where}/{present[0]}: must be a non-empty single-line string")
        return None
    local = present[0] == "path"
    if not local and "repository" not in item:
        errors.append(f"{where}: expected_path needs an explicit repository")
        ok = False
    title = objective = None
    if "task" in item:
        task = item["task"]
        if not isinstance(task, dict):
            errors.append(f"{where}/task: must be a mapping")
            ok = False
        else:
            for key in sorted(set(task) - TASK_KEYS):
                errors.append(f"{where}/task: unknown field {key!r}")
                ok = False
            title = task.get("title")
            if "title" in task and (not isinstance(title, str) or not 1 <= len(title) <= 200
                                    or CONTROL_RE.search(title)):
                errors.append(f"{where}/task/title: must be one line of 1-200 characters")
                ok = False
            objective = task.get("objective")
            if "objective" in task:
                problem = _check_objective(objective) if isinstance(objective, str) else "objective must be a string"
                if problem:
                    errors.append(f"{where}/task/objective: {problem}")
                    ok = False
    if not ok or root is None:
        return None
    return Entry(id=entry_id, repository=repository, path=_normal(root, value), local=local,
                 title=title, objective=objective)


# -- read-only Git and GitHub ---------------------------------------------------

def _git(path: Path, *args: str) -> str | None:
    try:
        proc = _run_git(["git", "-C", str(path), *args], shell=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _run_git(command: list[str], *, text: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run whose text is decoded like os.fsdecode and without newline translation, so a path
    with a non-UTF-8 byte or a carriage return compares equal to the directory it names."""
    proc = subprocess.run(command, **kwargs)
    if text:
        proc.stdout = None if proc.stdout is None else os.fsdecode(proc.stdout)
        proc.stderr = None if proc.stderr is None else os.fsdecode(proc.stderr)
    return proc


def common_dir(path: Path) -> Path | None:
    return session_scope.common_dir(path, run=_run_git)


def checkout_toplevel(path: Path) -> Path | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def git_dir(path: Path) -> Path | None:
    value = _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    return Path(value).resolve() if value else None


def local_head(path: Path) -> str | None:
    """HEAD commit id, or None when HEAD is unborn or unreadable."""
    return _git(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")


def environment_conflicts(env: dict[str, str]) -> list[str]:
    problems = [f"{name} is set; unset it so each checkout is resolved by its own path"
                for name in GIT_ENV_CONFLICTS if env.get(name)]
    host = env.get("GH_HOST")
    if host and host.strip().casefold() != gh.GITHUB_HOSTNAME:
        problems.append(f"GH_HOST={display(host)} would send `gh repo view` to another host than {gh.GITHUB_HOSTNAME}")
    return problems


class GithubFailure(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def verify_github(identity: str) -> dict:
    """Canonical identity, default branch and its head commit on github.com (read-only)."""
    conflicts = [p for p in environment_conflicts(dict(os.environ)) if p.startswith("GH_HOST")]
    if conflicts:
        raise GithubFailure("WORKSPACE_INIT_GH_HOST_CONFLICT", conflicts[0])
    if not gh.gh_available():
        raise GithubFailure("GITHUB_ACCESS_UNVERIFIED", "gh CLI unavailable")
    try:
        gh.require_authenticated()
        view = gh.repo_view(repo=identity)
        canonical = view.get("nameWithOwner")
        if not isinstance(canonical, str) or canonical.casefold() != identity.casefold():
            raise GithubFailure("WORKSPACE_REPOSITORY_REDIRECTED",
                                f"GitHub answered for {canonical!r}, not {display(identity)}; a renamed or transferred "
                                "repository must be named by its current identity")
        try:
            canonical = gh.validate_repo(canonical)
        except ValueError as exc:
            raise GithubFailure("WORKSPACE_INIT_GITHUB_MALFORMED", display(exc)) from exc
        branch = gh.default_branch(repo=canonical)
        head = gh.branch_head(branch, repo=canonical)
    except (gh.GhAuthError, gh.GhUnavailableError, gh.GhTimeoutError) as exc:
        raise GithubFailure("GITHUB_ACCESS_UNVERIFIED", display(exc)) from exc
    except gh.GhOutputError as exc:
        raise GithubFailure("WORKSPACE_INIT_GITHUB_MALFORMED", display(exc)) from exc
    except gh.GithubSyncError as exc:
        raise GithubFailure("WORKSPACE_REPOSITORY_ACCESS_DENIED",
                            f"gh could not read the repository (denied, missing or network failure): {display(exc)}") from exc
    if head is None:
        raise GithubFailure("WORKSPACE_INIT_DEFAULT_BRANCH_MISSING", f"default branch {display(branch)} does not exist")
    if not SHA_RE.fullmatch(head) or not BASE_BRANCH_RE.fullmatch(branch):
        raise GithubFailure("WORKSPACE_INIT_GITHUB_MALFORMED",
                            f"default branch {branch!r} @ {head!r} is not a branch name and 40-hex commit")
    return {"repository": canonical, "default_branch": branch, "default_head": head,
            "private": bool(view.get("isPrivate"))}


def resolve_entries(spec: Spec, *, github: Github = verify_github) -> tuple[list[dict], list[dict]]:
    """Resolve every entry; failures never stop the remaining entries."""
    resolutions: list[dict] = []
    errors: list[dict] = []
    seen_common: dict[Path, str] = {}
    seen_identity: dict[str, str] = {}
    seen_path: dict[Path, str] = {}

    def fail(entry: Entry, code: str, detail: str) -> None:
        errors.append({"id": entry.id, "code": code, "detail": detail})

    for entry in spec.entries:
        item: dict[str, Any] = {"id": entry.id, "local": entry.local}
        if entry.local:
            if not entry.path.is_dir():
                fail(entry, "WORKSPACE_REPOSITORY_PATH_MISSING", f"checkout directory does not exist: {display(entry.path)}")
                continue
            path = entry.path.resolve()
            top = checkout_toplevel(path)
            if top is None:
                fail(entry, "TARGET_REPOSITORY_UNRESOLVED", f"not a readable Git checkout: {display(path)}")
                continue
            if top != path:
                fail(entry, "WORKSPACE_INIT_NOT_CHECKOUT_ROOT", f"{display(path)} is inside the checkout {display(top)}, not its root")
                continue
            common = common_dir(path)
            if common is None:
                fail(entry, "SESSION_TARGET_UNRESOLVED", "git common directory cannot be resolved")
                continue
            audit: dict = {}
            try:
                identity = github_integration.resolve_target(path, entry.repository, audit=audit)
            except github_integration.IntegrationError as exc:
                found = ", ".join(display(r) for r in audit.get("discovered_repositories") or []) or "none"
                fail(entry, exc.code, f"{display(exc.detail)} (GitHub remotes: {found})")
                continue
            except UnicodeDecodeError:
                fail(entry, "TARGET_REPOSITORY_UNRESOLVED", NOT_UTF8_PATH)
                continue
            except ValueError as exc:  # the remote parser rejects a malformed URL (e.g. a bad port)
                fail(entry, "WORKSPACE_INIT_REMOTE_MALFORMED", f"{MALFORMED_REMOTE}: {display(exc)}")
                continue
            head = local_head(path)
            if head is None:
                fail(entry, "WORKSPACE_INIT_HEAD_UNBORN", "the checkout has no commit (unborn HEAD)")
                continue
            if not SHA_RE.fullmatch(head):
                fail(entry, "WORKSPACE_INIT_HEAD_INVALID", f"HEAD {head!r} is not a 40-hex commit id")
                continue
            if common in seen_common:
                fail(entry, "WORKSPACE_REPOSITORY_DUPLICATE", f"same Git repository as entry {seen_common[common]}")
                continue
            seen_common[common] = entry.id
            item.update(path=path, git_common_dir=common, head=head, remotes=audit.get("discovered_repositories"),
                        selection="explicit" if entry.repository else "inferred")
        else:
            path = entry.path
            if os.path.lexists(path):
                fail(entry, "WORKSPACE_INIT_EXPECTED_PATH_EXISTS",
                     f"expected_path already exists: {display(path)}; name an existing checkout with path instead")
                continue
            identity = entry.repository
            item.update(path=path, selection="explicit")
        if path in seen_path:
            fail(entry, "WORKSPACE_REPOSITORY_DUPLICATE", f"same path as entry {seen_path[path]}")
            continue
        seen_path[path] = entry.id
        try:
            remote = github(identity)
        except GithubFailure as exc:
            fail(entry, exc.code, f"{display(identity)}: {display(exc.detail)}")
            continue
        key = remote["repository"].casefold()
        if key in seen_identity:
            fail(entry, "WORKSPACE_REPOSITORY_DUPLICATE", f"same GitHub identity as entry {seen_identity[key]}")
            continue
        seen_identity[key] = entry.id
        item.update(repository=remote["repository"], base_branch=remote["default_branch"],
                    default_head=remote["default_head"],
                    pinned_commit=item["head"] if entry.local else remote["default_head"])
        resolutions.append(item)
    return resolutions, errors


# -- manifest, in-memory plan and preview ------------------------------------------

def placeholder_title(entry_id: str) -> str:
    return f"TODO: define the task for repository {entry_id}"


def build_manifest(spec: Spec, resolutions: list[dict]) -> tuple[dict, list[str]]:
    by_id = {r["id"]: r for r in resolutions}
    warnings: list[str] = []
    repositories, tasks = [], []
    for entry in spec.entries:
        res = by_id[entry.id]
        repositories.append({"id": entry.id, "repository": res["repository"], "path": str(res["path"]),
                             "pinned_commit": res["pinned_commit"], "base_branch": res["base_branch"]})
        task = {"id": entry.id, "repository": entry.id, "title": entry.title or placeholder_title(entry.id)}
        if entry.objective:
            task["objective"] = entry.objective
        if not entry.title:
            warnings.append(f"task {entry.id} has a placeholder title; edit it before scheduling")
        tasks.append(task)
    manifest = {"schema_version": 1, "id": spec.id, "goal": spec.goal, "max_parallel": spec.max_parallel,
                "repositories": repositories, "tasks": tasks}
    return manifest, warnings


MANIFEST_HEADER = ("# Workspace manifest generated by `pico workspace init`.\n"
                   "# Tasks have no dependencies or contracts; add them deliberately. Check it with: pico plan "
                   "<this file>\n")


def manifest_text(manifest: dict) -> str:
    body = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)
    text = MANIFEST_HEADER + body
    if strict_load(text) != manifest:
        raise InitError("WORKSPACE_INIT_INTERNAL", "generated YAML does not read back as the manifest", EXIT_NOT_SAVED)
    return text


class _DisplayDumper(yaml.SafeDumper):
    """Writes a string that display() would change double-quoted, so it stays on its own line."""


def _represent_display_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"' if display(value) != value else None)


_DisplayDumper.add_representer(str, _represent_display_str)


def manifest_display_text(manifest: dict) -> str:
    """The manifest as YAML for the terminal: equal to manifest_text() unless a value holds a control,
    line-break or format character, which is then shown as an escape in a double-quoted scalar."""
    body = yaml.dump(manifest, Dumper=_DisplayDumper, sort_keys=False, default_flow_style=False, allow_unicode=True,
                     width=1000)
    return display_lines(MANIFEST_HEADER + body)


def plan_in_memory(manifest: dict, destination: Path) -> dict:
    """The same result `workspace_plan.plan(destination, access_check=None)` gives once saved."""
    repositories, repo_blockers = wp.resolve_repositories(manifest["repositories"], base=destination.parent,
                                                          access_check=None)
    tasks, waves, task_blockers = wp.plan_tasks(manifest["tasks"], {r["id"] for r in manifest["repositories"]}, None)
    base_branches = {r["id"]: r.get("base_branch", "main") for r in manifest["repositories"]}
    for repo in repositories:
        repo["base_branch"] = base_branches[repo["id"]]
    blockers = repo_blockers + task_blockers
    return {"plan_version": wp.PLAN_VERSION, "workspace_id": manifest["id"], "goal": manifest["goal"],
            "status": "BLOCKED" if blockers else "PLANNED", "orchestrator_revision": wp.orchestrator_revision(),
            "manifest": str(destination), "max_parallel": manifest["max_parallel"],
            "access_check": "skipped", "repositories": repositories,
            "budget": manifest.get("budget"), "max_duration_seconds": manifest.get("max_duration_seconds"),
            "max_lead_calls": manifest.get("max_lead_calls"), "lead_calls_warn_at": manifest.get("lead_calls_warn_at"),
            "failure_retry_budget": manifest.get("failure_retry_budget"),
            "tasks": tasks, "waves": waves, "summary": wp.summarize_tasks(tasks), "blockers": blockers,
            "mutations": {"git": 0, "github": 0, "workers": 0}}


@dataclass
class Prepared:
    manifest: dict
    text: str
    destination: Path
    resolutions: list[dict]
    warnings: list[str]
    plan: dict
    preview: str = ""


def _evidence(res: dict) -> list[str]:
    pin = res["pinned_commit"]
    name, repository, path, branch = (display(res[key]) for key in ("id", "repository", "path", "base_branch"))
    if res["local"]:
        lines = [f"  {name}: {repository} local checkout {path}"]
        if res["selection"] == "inferred":
            how = "inferred from its only GitHub remote"
        elif res.get("remotes") and len(res["remotes"]) > 1:
            how = "explicitly selected among remotes " + ", ".join(display(r) for r in res["remotes"])
        else:
            how = "explicit, matches a GitHub remote"
        lines.append(f"      identity {how}; verified on GitHub")
        same = "same commit" if pin == res["default_head"] else "differs from the pin"
        lines.append(f"      pin {pin} = local HEAD; GitHub {branch} @ {res['default_head'][:12]} ({same})")
    else:
        lines = [f"  {name}: {repository} remote-only, expected at {path} (absent, not cloned)",
                 f"      pin {pin} = head of GitHub default branch {branch}"]
    return lines


def render_preview(prepared: Prepared) -> str:
    """The preview's own line breaks are its layout; every untrusted fragment is escaped before it is placed."""
    shown = manifest_display_text(prepared.manifest)
    lines = [f"Workspace manifest preview (nothing has been written). Destination: {display(prepared.destination)}",
             "", shown.rstrip("\n")]
    if shown != prepared.text:
        lines.append("(Values with control, line-break or invisible characters are shown as escapes above; "
                     "the file saves them unchanged.)")
    lines += ["", "Repository resolution (verified by the builder, read-only):"]
    for res in prepared.resolutions:
        lines.extend(_evidence(res))
    if prepared.warnings:
        lines.append("Warnings:")
        lines.extend(f"  {w}" for w in prepared.warnings)
    lines += ["", "Planner preview, computed in memory from this manifest (its access check is skipped; "
              "GitHub was verified above):", wp.render(_display_tree(prepared.plan)), "",
              "Saving writes only this one new file. It launches no worker, creates no worktree and does not "
              "clone, fetch or pull any repository."]
    return "\n".join(lines)


def prepare(spec: Spec, destination: Path, *, github: Github = verify_github) -> Prepared:
    resolutions, errors = resolve_entries(spec, github=github)
    if errors:
        detail = "\n".join(f"  {e['code']} ({e['id']}): {e['detail']}" for e in errors)
        raise InitError("WORKSPACE_INIT_RESOLUTION_FAILED", "repositories could not be resolved:\n" + detail,
                        EXIT_NOT_SAVED)
    manifest, warnings = build_manifest(spec, resolutions)
    for res in resolutions:
        if not res["local"]:
            warnings.append(f"{res['id']} is not cloned: the planner reports WORKSPACE_REPOSITORY_PATH_MISSING "
                            f"until it is checked out at {display(res['path'])} with commit {res['pinned_commit']}")
    problems = wp.validate_manifest(manifest)
    if problems:
        raise InitError("WORKSPACE_MANIFEST_INVALID", "; ".join(display(p) for p in problems))
    prepared = Prepared(manifest=manifest, text=manifest_text(manifest), destination=destination,
                        resolutions=resolutions, warnings=warnings, plan=plan_in_memory(manifest, destination))
    prepared.preview = render_preview(prepared)
    return prepared


# -- destination and exclusive save -------------------------------------------------

def destination_path(output: str | None, *, root: Path, workspace_id: str, cwd: Path) -> Path:
    """`output` is relative to the caller's directory; the default is <root>/<id>.workspace.yaml."""
    raw = Path(os.path.expanduser(output)) if output else root / f"{workspace_id}.workspace.yaml"
    raw = Path(os.path.normpath(cwd / raw))
    if not raw.parent.is_dir():
        raise InitError("WORKSPACE_INIT_OUTPUT_PARENT_MISSING", f"output directory does not exist: {display(raw.parent)}")
    destination = raw.parent.resolve() / raw.name
    if os.path.lexists(destination):
        raise InitError("WORKSPACE_INIT_OUTPUT_EXISTS",
                        f"{display(destination)} already exists; it is never overwritten. Choose another --output",
                        EXIT_NOT_SAVED)
    return destination


def _remove_if_owned(path: Path, created: os.stat_result) -> None:
    """Remove `path` only while it still names the inode this process created."""
    try:
        current = os.lstat(path)
    except OSError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
        try:
            os.unlink(path)
        except OSError:
            pass


def save_exclusive(destination: Path, text: str) -> None:
    """Create `destination` as a new regular file; never follow or replace an existing entry."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(destination, flags, 0o644)
    except FileExistsError as exc:
        raise InitError("WORKSPACE_INIT_OUTPUT_EXISTS", f"{display(destination)} already exists; it is never overwritten",
                        EXIT_NOT_SAVED) from exc
    except OSError as exc:
        raise InitError("WORKSPACE_INIT_OUTPUT_UNWRITABLE", f"cannot create {display(destination)}: {display(exc)}",
                        EXIT_NOT_SAVED) from exc
    created = os.fstat(fd)
    try:
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException as exc:
        os.close(fd)
        _remove_if_owned(destination, created)
        if isinstance(exc, OSError):
            raise InitError("WORKSPACE_INIT_OUTPUT_UNWRITABLE", f"writing {display(destination)} failed: {display(exc)}; "
                            "the partial file was removed", EXIT_NOT_SAVED) from exc
        raise
    os.close(fd)


# -- discovery ------------------------------------------------------------------------

@dataclass
class Candidate:
    path: Path
    label: str
    identities: list[str]
    remote: bool = False

    @property
    def ambiguous(self) -> bool:
        return len(self.identities) > 1


@dataclass
class Discovery:
    root: Path
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    directories: int = 0


def _pruned(name: str) -> bool:
    return name in PRUNED_DIRECTORY_NAMES or name.endswith(PRUNED_DIRECTORY_SUFFIXES)


def discover(root: Path, *, max_depth: int = DISCOVERY_MAX_DEPTH, max_directories: int = DISCOVERY_MAX_DIRECTORIES,
             max_candidates: int = DISCOVERY_MAX_CANDIDATES) -> Discovery:
    """Sorted, bounded walk below `root` that never follows a symlink and never runs a mutating command."""
    root = root.resolve(strict=True)
    found = Discovery(root=root)
    seen_common: dict[Path, str] = {}
    too_deep = 0

    def label(path: Path) -> str:
        rel = path.relative_to(root).as_posix()
        return "." if rel == "." else rel

    def onerror(exc: OSError) -> None:
        name = Path(exc.filename) if exc.filename else root
        try:
            where = label(name)
        except ValueError:
            where = str(name)
        found.unreadable.append((where, exc.strerror or str(exc)))

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        current = Path(dirpath)
        found.directories += 1
        if found.directories > max_directories:
            found.truncated.append(f"stopped after {max_directories} directories")
            break
        if ".git" in dirnames or ".git" in filenames:
            if len(found.candidates) >= max_candidates:
                found.truncated.append(f"listed the first {max_candidates} repositories")
                break
            _classify(current, label(current), root, found, seen_common)
        depth = len(current.relative_to(root).parts)
        keep = []
        for name in sorted(dirnames):
            if _pruned(name) or os.path.islink(current / name):
                continue
            if depth + 1 > max_depth:
                too_deep += 1
                continue
            keep.append(name)
        dirnames[:] = keep
    if too_deep:
        found.truncated.append(f"{too_deep} directories deeper than {max_depth} levels were not searched")
    return found


def _classify(current: Path, where: str, root: Path, found: Discovery, seen_common: dict[Path, str]) -> None:
    if os.path.islink(current / ".git"):
        found.skipped.append((where, "its .git is a symlink, which is not followed"))
        return
    top = checkout_toplevel(current)
    if top is None:
        found.unreadable.append((where, "git cannot read this checkout"))
        return
    if top != current:
        found.skipped.append((where, f"not a checkout root (git reports {top})"))
        return
    own, common = git_dir(current), common_dir(current)
    if own is None or common is None:
        found.unreadable.append((where, "git directory cannot be resolved"))
        return
    if own != common:
        found.skipped.append((where, f"linked worktree of {common.parent}"))
        return
    if not common.is_relative_to(root):
        found.skipped.append((where, f"its Git directory {common} is outside the discovery root"))
        return
    if common in seen_common:
        found.skipped.append((where, f"same Git repository as {seen_common[common]}"))
        return
    seen_common[common] = where
    audit: dict = {}
    try:
        identities = [github_integration.resolve_target(current, None, audit=audit)]
    except github_integration.IntegrationError as exc:
        if exc.code != "TARGET_REPOSITORY_AMBIGUOUS":
            found.skipped.append((where, exc.detail))
            return
        identities = list(audit.get("discovered_repositories") or [])
    except UnicodeDecodeError:
        found.skipped.append((where, NOT_UTF8_PATH))
        return
    except ValueError as exc:  # the remote parser rejects a malformed URL (e.g. a bad port)
        found.skipped.append((where, f"WORKSPACE_INIT_REMOTE_MALFORMED: {MALFORMED_REMOTE}: {exc}"))
        return
    if local_head(current) is None:
        found.skipped.append((where, "no commit yet (unborn HEAD)"))
        return
    found.candidates.append(Candidate(path=current, label=where, identities=identities))


def discovery_report(found: Discovery) -> list[str]:
    lines = [f"Searched {found.directories} directories below {display(found.root)} "
             f"(depth <= {DISCOVERY_MAX_DEPTH}; skipped: {', '.join(sorted(PRUNED_DIRECTORY_NAMES))}, *.egg-info, "
             "symlinks)."]
    for where, reason in found.skipped:
        lines.append(f"  not selectable: {display(where)}: {display(reason)}")
    for where, reason in found.unreadable:
        lines.append(f"  unreadable: {display(where)}: {display(reason)}")
    for reason in found.truncated:
        lines.append(f"  search truncated: {reason}")
    return lines


# -- interactive ------------------------------------------------------------------------

class Prompter:
    def __init__(self, stdin: TextIO, stdout: TextIO):
        self.stdin = stdin
        self.stdout = stdout

    def say(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def ask(self, prompt: str) -> str:
        """One stripped line; EOF cancels."""
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise Cancelled()
        return line.rstrip("\r\n").strip()

    def required(self, prompt: str) -> str:
        answer = self.ask(prompt)
        if not answer:
            raise Cancelled()
        return answer


def slugify(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-_") or "repo"
    if not base[0].isalnum():
        base = "repo-" + base
    base = base[:56]
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


SELECTION_HELP = ("Commands: numbers or ranges toggle a repository (e.g. 1 3 5-7); /text shows only matches "
                  "(/ alone shows all); r adds a GitHub repository that is not cloned; d finishes; q cancels.")


def _show(p: Prompter, candidates: list[Candidate], selected: set[int], needle: str) -> None:
    shown = 0
    for index, cand in enumerate(candidates):
        identity = ("ambiguous: " + ", ".join(cand.identities)) if cand.ambiguous else cand.identities[0]
        where = f"remote-only at {cand.path} (not cloned)" if cand.remote else cand.label
        # The search sees the raw text and its escaped form; the terminal only the escaped form.
        line = f"  [{'*' if index in selected else ' '}] {index + 1:>3}. {display(where)}  {display(identity)}"
        if needle and not any(needle.casefold() in text.casefold() for text in (f"{where}  {identity}", line)):
            continue
        p.say(line)
        shown += 1
    if not shown:
        p.say("  (no repository matches)" if needle else "  (no selectable repository found)")
    p.say(f"Selected {len(selected)} of {MIN_REPOSITORIES}-{MAX_REPOSITORIES}.")


def _number(text: str, high: int) -> int | None:
    """An ASCII decimal from 1 to `high`, else None. str.isdigit() accepts '²', which int() refuses,
    and int() refuses decimals longer than Python's digit limit, so neither is converted. Leading
    zeros are stripped before conversion, so only the bounded significant digits reach int()."""
    if not re.fullmatch(r"[0-9]+", text):
        return None
    digits = text.lstrip("0")
    if len(digits) > len(str(high)):
        return None
    value = int(digits or "0")
    return value if 1 <= value <= high else None


def _parse_numbers(text: str, count: int) -> list[int] | None:
    picked: list[int] = []
    for token in re.split(r"[\s,]+", text.strip()):
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", token)
        if not match:
            return None
        low, high = _number(match.group(1), count), _number(match.group(2) or match.group(1), count)
        if low is None or high is None or low > high:
            return None
        picked.extend(range(low - 1, high))
    return picked


def _add_remote(p: Prompter, root: Path, candidates: list[Candidate]) -> int | None:
    identity = p.ask("GitHub owner/repository to add (blank to go back): ")
    if not identity:
        return None
    if not REPOSITORY_RE.fullmatch(identity) or ".." in identity:
        p.say("Not an owner/repository identity.")
        return None
    if any(identity.casefold() in (i.casefold() for i in c.identities) for c in candidates):
        p.say(f"{display(identity)} is already listed.")
        return None
    default = root / identity.split("/", 1)[1]
    answer = p.ask(f"Where will it be cloned? Relative to the root; it must not exist yet [{display(default)}]: ")
    path = _normal(root, answer) if answer else default
    if os.path.lexists(path):
        p.say(f"{display(path)} already exists; select the checkout instead, or choose a free path.")
        return None
    candidates.append(Candidate(path=path, label=str(path), identities=[identity], remote=True))
    return len(candidates) - 1


def select(p: Prompter, found: Discovery) -> list[Candidate]:
    candidates = list(found.candidates)
    selected: set[int] = set()
    needle = ""
    p.say(SELECTION_HELP)
    _show(p, candidates, selected, needle)
    while True:
        answer = p.ask("Select> ")
        if answer.lower() in ("q", "quit", "cancel"):
            raise Cancelled()
        if answer.startswith("/"):
            needle = answer[1:].strip()
        elif answer.lower() in ("r", "remote"):
            added = _add_remote(p, found.root, candidates)
            if added is not None:
                selected.add(added)
                needle = ""
        elif answer.lower() in ("d", "done"):
            chosen = sorted(selected)
            if not MIN_REPOSITORIES <= len(chosen) <= MAX_REPOSITORIES:
                p.say(f"Select {MIN_REPOSITORIES} to {MAX_REPOSITORIES} repositories (now {len(chosen)}).")
                continue
            single = [candidates[i].identities[0].casefold() for i in chosen if not candidates[i].ambiguous]
            if len(single) != len(set(single)):
                p.say("Two selected checkouts have the same GitHub identity; keep one of them.")
                continue
            return [candidates[i] for i in chosen]
        elif answer:
            picked = _parse_numbers(answer, len(candidates))
            if picked is None:
                p.say(f"Not a selection. {SELECTION_HELP}")
                continue
            selected.symmetric_difference_update(picked)
        else:
            p.say(SELECTION_HELP)
        _show(p, candidates, selected, needle)


def choose_identity(p: Prompter, cand: Candidate) -> str:
    p.say(f"{display(cand.label)} has several GitHub remotes. Choose the identity for this workspace (there is no default):")
    for index, identity in enumerate(cand.identities, start=1):
        p.say(f"  {index}. {display(identity)}")
    while True:
        answer = p.required("Identity number (blank cancels): ")
        number = _number(answer, len(cand.identities))
        if number is not None:
            return cand.identities[number - 1]
        p.say("Enter one of the listed numbers.")


def _ask_slug(p: Prompter, prompt: str, default: str | None, taken: set[str]) -> str:
    while True:
        answer = p.ask(prompt) if default is not None else p.required(prompt)
        value = answer or default
        if value and SLUG_RE.fullmatch(value) and value not in taken:
            return value
        p.say("Use letters, digits, '-' or '_' (start with a letter or digit, at most 64), not used yet.")


def run_interactive(p: Prompter, *, root_default: Path, output: str | None, cwd: Path,
                    github: Github = verify_github) -> int:
    p.say("pico workspace init: build a workspace manifest from repositories you select.")
    p.say("Nothing is written until you approve the preview. Nothing is cloned, fetched or launched.")
    while True:
        answer = p.ask(f"Discovery root [{display(root_default)}]: ")
        root = _normal(cwd, answer) if answer else root_default
        if root.is_dir():
            break
        p.say(f"{display(root)} is not a directory.")
    found = discover(root)
    for line in discovery_report(found):
        p.say(line)
    chosen = select(p, found)
    identities = [choose_identity(p, c) if c.ambiguous else c.identities[0] for c in chosen]
    workspace_id = _ask_slug(p, "Workspace id (blank cancels): ", None, set())
    goal = p.required("Workspace goal, one line (blank cancels): ")
    while not 1 <= len(goal) <= 2000:
        p.say("The goal must be 1-2000 characters.")
        goal = p.required("Workspace goal, one line (blank cancels): ")
    while True:
        answer = p.ask("Maximum parallel tasks [1]: ") or "1"
        number = _number(answer, 64)
        if number is not None:
            max_parallel = number
            break
        p.say("Enter a whole number from 1 to 64.")
    entries: list[Entry] = []
    taken: set[str] = set()
    for cand, identity in zip(chosen, identities):
        default = slugify(cand.path.name if not cand.remote else identity.split("/", 1)[1], taken)
        entry_id = _ask_slug(p, f"Repository id for {display(identity)} [{default}]: ", default, taken)
        taken.add(entry_id)
        title = p.ask(f"Task title for {entry_id} [placeholder: {placeholder_title(entry_id)}]: ") or None
        while title and (len(title) > 200 or CONTROL_RE.search(title)):
            p.say("A title is one line of at most 200 characters.")
            title = p.ask(f"Task title for {entry_id} [placeholder]: ") or None
        while True:
            objective = p.ask(f"Task objective for {entry_id}, scoped to this repository (blank for none): ") or None
            problem = _check_objective(objective) if objective else None
            if not problem:
                break
            p.say(problem)
        entries.append(Entry(id=entry_id, repository=identity, path=cand.path, local=not cand.remote,
                             title=title, objective=objective,
                             identity_choice=cand.identities if cand.ambiguous else None))
    spec = Spec(root=found.root, id=workspace_id, goal=goal, max_parallel=max_parallel, entries=entries)
    default_output = output or str(found.root / f"{workspace_id}.workspace.yaml")
    while True:
        answer = p.ask(f"Save to [{display(default_output)}]: ") or default_output
        try:
            destination = destination_path(answer, root=found.root, workspace_id=workspace_id, cwd=cwd)
            break
        except InitError as exc:
            p.say(exc.detail)
    prepared = prepare(spec, destination, github=github)
    p.say(prepared.preview)
    answer = p.ask(f"Write {display(destination)}? Type yes to save [no]: ")
    if answer.lower() not in ("y", "yes"):
        p.say("Not saved; nothing was written.")
        return EXIT_NOT_SAVED
    save_exclusive(destination, prepared.text)
    p.say(f"Saved {display(destination)}. Review it, then run: pico plan {display(destination)}")
    return EXIT_OK


# -- CLI --------------------------------------------------------------------------------

def run_spec(spec_arg: str, *, output: str | None, approve: bool, cwd: Path, out: TextIO,
             github: Github = verify_github) -> int:
    spec_path = Path(os.path.normpath(cwd / os.path.expanduser(spec_arg)))
    spec = validate_spec(load_spec(spec_path), base=spec_path.parent)
    destination = destination_path(output, root=spec.root, workspace_id=spec.id, cwd=cwd)
    prepared = prepare(spec, destination, github=github)
    out.write(prepared.preview + "\n")
    if not approve:
        out.write("Preview only: nothing was written. Rerun with --approve to save it.\n")
        return EXIT_OK
    save_exclusive(destination, prepared.text)
    out.write(f"Saved {display(destination)}\n")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """argparse writing to main()'s streams. Its error messages quote the command line, which is
    untrusted, so the message is escaped; the usage text and prog are this file's own layout."""

    def __init__(self, *args: Any, out: TextIO, err: TextIO, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._out, self._err = out, err

    def print_usage(self, file: TextIO | None = None) -> None:
        super().print_usage(file or self._out)

    def print_help(self, file: TextIO | None = None) -> None:
        super().print_help(file or self._out)

    def error(self, message: str) -> NoReturn:
        self.print_usage(self._err)
        self.exit(EXIT_INVALID, f"{self.prog}: error: {display(message)}\n")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            self._err.write(message)
        raise SystemExit(status)


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None, cwd: Path | None = None, env: dict[str, str] | None = None,
         github: Github = verify_github) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = _Parser(prog="pico workspace init", out=stdout, err=stderr,
                     description="Build a workspace manifest; it is saved only after approval.")
    parser.add_argument("--root", help="discovery root (interactive; default: the current directory)")
    parser.add_argument("--output", help="manifest to create (default: <root>/<id>.workspace.yaml); never overwritten")
    parser.add_argument("--spec", help="non-interactive builder spec (YAML or JSON)")
    parser.add_argument("--approve", action="store_true", help="with --spec: save the previewed manifest")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_INVALID if exc.code else EXIT_OK
    cwd = Path(os.path.abspath(cwd or os.getcwd()))
    try:
        if args.approve and not args.spec:
            raise InitError("WORKSPACE_INIT_USAGE", "--approve is only valid with --spec; interactive mode asks")
        if args.spec and args.root:
            raise InitError("WORKSPACE_INIT_USAGE", "--root is interactive only; a spec names its own root")
        conflicts = environment_conflicts(dict(os.environ) if env is None else env)
        if conflicts:
            raise InitError("WORKSPACE_INIT_ENVIRONMENT_CONFLICT", "; ".join(conflicts))
        if args.spec:
            return run_spec(args.spec, output=args.output, approve=args.approve, cwd=cwd, out=stdout, github=github)
        if not (stdin.isatty() and stdout.isatty()):
            raise InitError("WORKSPACE_INIT_TTY_REQUIRED",
                            "interactive mode needs a terminal; use --spec FILE for a non-interactive preview")
        root_default = _normal(cwd, args.root) if args.root else cwd
        if not root_default.is_dir():
            raise InitError("WORKSPACE_INIT_USAGE", f"--root is not a directory: {display(root_default)}")
        return run_interactive(Prompter(stdin, stdout), root_default=root_default, output=args.output, cwd=cwd,
                               github=github)
    except Cancelled:
        stdout.write("\nCancelled; nothing was written.\n")
        return EXIT_NOT_SAVED
    except KeyboardInterrupt:
        stderr.write("\nInterrupted; nothing was written.\n")
        return EXIT_INTERRUPTED
    except InitError as exc:
        stderr.write(f"{exc.code}: {exc.detail}\n")
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
