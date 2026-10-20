#!/usr/bin/env python3
"""Safe subprocess boundary around the authenticated `gh` CLI.

This is the ONLY module in this repository that may invoke `gh`. Parent-owned
tools/backlog.py and tools/github_integration.py call its constrained functions.
Workers/subagents must not receive this adapter as a mutation tool.
The local-backlog close guard is applied by tools/backlog.py; the external
integration requires separate parent authorization for destructive changes.

Safety properties, all enforced here:
  - `shell=False` always; every invocation is an explicit argv list, never a
    shell string. No shell metacharacter can ever be interpreted.
  - A fixed allowlist of (noun, verb) command pairs. No `gh api`, and no
    caller-supplied arbitrary flags: every wrapper function below builds its
    own fixed argv shape and only interpolates validated positional values
    (issue/PR numbers, titles, bodies, labels, branch refs) as separate argv
    elements -- never concatenated into a single string. PR support is limited
    to list/view/create/edit/comment operations; issue comments are also explicit
    wrappers. No PR merge, checkout, ready, branch push, or `gh api` path exists here.
  - No secrets/tokens are read, stored, or logged by this module. Whatever
    `gh` itself resolves from its own auth configuration is used implicitly
    by the `gh` binary; this module never inspects or forwards a token.

This module contains no backlog/provenance knowledge of its own (no reads or
writes of backlog/backlog.yaml or provenance/runs/): it is a pure, testable
`gh` boundary. Composition (deciding *when* to call these functions, and
reconciling the result with local state) lives in tools/backlog.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

GH_BINARY = "gh"

# Fixed allowlist of "gh <noun> <verb>" pairs this module may ever invoke.
# Deliberately excludes `gh api` and any pair not implemented below.
ALLOWED_COMMANDS = frozenset(
    {
        ("repo", "view"),
        ("issue", "list"),
        ("issue", "view"),
        ("issue", "create"),
        ("issue", "edit"),
        ("issue", "close"),
        ("issue", "reopen"),
        ("issue", "comment"),
        ("label", "list"),
        ("label", "create"),
        ("pr", "view"),
        ("pr", "list"),
        ("pr", "create"),
        ("pr", "edit"),
        ("pr", "comment"),
        ("auth", "status"),
    }
)

ISSUE_STATES = ("open", "closed", "all")

DEFAULT_TIMEOUT_SECONDS = 30


class GithubSyncError(Exception):
    """Base class for all github_sync errors."""


class GhUnavailableError(GithubSyncError):
    """The `gh` binary is not present on PATH."""


class GhAuthError(GithubSyncError):
    """`gh` is present but not authenticated (or auth could not be verified)."""


class GhCommandError(GithubSyncError):
    """A `gh` invocation returned a nonzero exit status."""

    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)!r} failed (exit {returncode}): {stderr.strip()}")


class GhOutputError(GithubSyncError):
    """`gh` returned output that could not be parsed as expected (e.g.
    malformed/non-JSON output where JSON was required)."""


class GhTimeoutError(GithubSyncError):
    """A `gh` invocation exceeded its timeout."""


# ---------------------------------------------------------------------------
# Core safe invocation
# ---------------------------------------------------------------------------

def gh_available() -> bool:
    return shutil.which(GH_BINARY) is not None


def _validate_allowlisted(argv: list[str]) -> None:
    if not argv or argv[0] != GH_BINARY:
        raise GithubSyncError(f"internal error: argv must start with {GH_BINARY!r}, got {argv!r}")
    if len(argv) < 3:
        raise GithubSyncError(f"internal error: argv too short to contain a noun/verb pair: {argv!r}")
    pair = (argv[1], argv[2])
    if pair not in ALLOWED_COMMANDS:
        raise GithubSyncError(
            f"internal error: command {pair!r} is not in the allowlist {sorted(ALLOWED_COMMANDS)}"
        )


def _run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_SECONDS, cwd: str | None = None) -> subprocess.CompletedProcess:
    """The single point every `gh` invocation passes through. Always
    `shell=False`, always argv-only, always allowlist-checked."""
    _validate_allowlisted(argv)
    if not gh_available():
        raise GhUnavailableError("the 'gh' CLI is not installed or not on PATH")
    try:
        return subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise GhUnavailableError("the 'gh' CLI is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhTimeoutError(f"{' '.join(argv)!r} timed out after {timeout}s") from exc


def _parse_json(proc: subprocess.CompletedProcess, context: str) -> Any:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GhOutputError(
            f"{context}: 'gh' did not return valid JSON output: {exc}; stdout={proc.stdout!r}"
        ) from exc


def _require_success(proc: subprocess.CompletedProcess, argv: list[str]) -> None:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").lower()
        if "auth" in detail or "login" in detail or "not logged" in detail or "authentication" in detail:
            raise GhAuthError((proc.stderr or proc.stdout or "gh is not authenticated").strip())
        raise GhCommandError(argv, proc.returncode, proc.stderr)


def _validate_issue_number(number: int) -> None:
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"issue number must be a positive integer, got {number!r}")


def _validate_pr_number(number: int) -> None:
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"PR number must be a positive integer, got {number!r}")


def _validate_text(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")


def validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo) or ".." in repo:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return repo


def _with_repo(argv: list[str], repo: str | None) -> list[str]:
    if repo is None:
        return argv
    return argv + ["--repo", validate_repo(repo)]


def _require_keys(data: dict, keys: list[str], context: str) -> None:
    missing = [k for k in keys if k not in data]
    if missing:
        raise GhOutputError(f"{context}: missing required JSON field(s): {missing}")


def _validate_issue_shape(data: dict, context: str) -> dict:
    _require_keys(data, ["number", "title", "state", "labels"], context)
    if not isinstance(data["number"], int) or data["number"] < 1:
        raise GhOutputError(f"{context}: invalid issue number")
    if not isinstance(data["title"], str):
        raise GhOutputError(f"{context}: invalid title")
    if str(data["state"]).lower() not in {"open", "closed"}:
        raise GhOutputError(f"{context}: invalid state {data['state']!r}")
    if not isinstance(data["labels"], list):
        raise GhOutputError(f"{context}: labels must be an array")
    return data


def _validate_pr_shape(data: dict, context: str) -> dict:
    _require_keys(data, ["number", "title", "state", "body", "url", "headRefName", "baseRefName", "isDraft"], context)
    if not isinstance(data["number"], int) or data["number"] < 1:
        raise GhOutputError(f"{context}: invalid PR number")
    if not isinstance(data["title"], str):
        raise GhOutputError(f"{context}: invalid title")
    if str(data["state"]).lower() not in {"open", "closed", "merged"}:
        raise GhOutputError(f"{context}: invalid state {data['state']!r}")
    if not isinstance(data["body"], str):
        raise GhOutputError(f"{context}: invalid body")
    if not isinstance(data["url"], str):
        raise GhOutputError(f"{context}: invalid url")
    if not isinstance(data["headRefName"], str):
        raise GhOutputError(f"{context}: invalid headRefName")
    if not isinstance(data["baseRefName"], str):
        raise GhOutputError(f"{context}: invalid baseRefName")
    if not isinstance(data["isDraft"], bool):
        raise GhOutputError(f"{context}: invalid isDraft")
    return data


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def auth_status() -> dict:
    """Verify authentication. Returns {'authenticated': bool, 'detail': str}.
    Never raises GhCommandError for a plain not-authenticated result -- that
    is a normal, well-formed outcome the caller must handle -- but does raise
    GhUnavailableError if `gh` itself is missing."""
    argv = [GH_BINARY, "auth", "status"]
    proc = _run(argv)
    detail = (proc.stdout or proc.stderr or "").strip()
    return {"authenticated": proc.returncode == 0, "detail": detail}


def require_authenticated() -> None:
    status = auth_status()
    if not status["authenticated"]:
        raise GhAuthError(f"gh is not authenticated: {status['detail']}")


def repo_view(*, cwd: str | None = None, repo: str | None = None) -> dict:
    """Detect the current repository, or view an explicit owner/repository.
    Supports private repos identically to public ones (gh resolves this
    transparently); returns 'isPrivate' from the JSON payload so callers can
    branch on it if needed."""
    argv = [GH_BINARY, "repo", "view"]
    if repo is not None:
        argv.append(validate_repo(repo))
    argv += ["--json", "nameWithOwner,owner,name,isPrivate,url"]
    proc = _run(argv, cwd=cwd)
    _require_success(proc, argv)
    data = _parse_json(proc, "gh repo view")
    if not isinstance(data, dict):
        raise GhOutputError(f"gh repo view: expected a JSON object, got {type(data).__name__}")
    _require_keys(data, ["nameWithOwner", "isPrivate"], "gh repo view")
    return data


def list_issues(state: str = "open", limit: int = 200, *, repo: str | None = None) -> list[dict]:
    if state not in ISSUE_STATES:
        raise ValueError(f"state must be one of {ISSUE_STATES}, got {state!r}")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    argv = [
        GH_BINARY, "issue", "list",
        "--state", state,
        "--json", "number,title,state,labels",
        "--limit", str(limit),
    ]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
    data = _parse_json(proc, "gh issue list")
    if not isinstance(data, list):
        raise GhOutputError(f"gh issue list: expected a JSON array, got {type(data).__name__}")
    for i, issue in enumerate(data):
        if not isinstance(issue, dict):
            raise GhOutputError(f"gh issue list[{i}]: expected object")
        _validate_issue_shape(issue, f"gh issue list[{i}]")
    return data


def view_issue(number: int, *, repo: str | None = None) -> dict:
    _validate_issue_number(number)
    argv = [
        GH_BINARY, "issue", "view", str(number),
        "--json", "number,title,state,labels,body,url,comments",
    ]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
    data = _parse_json(proc, f"gh issue view {number}")
    if not isinstance(data, dict):
        raise GhOutputError(f"gh issue view {number}: expected a JSON object, got {type(data).__name__}")
    return _validate_issue_shape(data, f"gh issue view {number}")


def list_labels(*, repo: str, limit: int = 1000) -> list[dict]:
    validate_repo(repo)
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    argv = [GH_BINARY, "label", "list", "--json", "name,color,description", "--limit", str(limit), "--repo", repo]
    proc = _run(argv)
    _require_success(proc, argv)
    data = _parse_json(proc, "gh label list")
    if not isinstance(data, list) or any(not isinstance(x, dict) or not isinstance(x.get("name"), str) for x in data):
        raise GhOutputError("gh label list: invalid labels")
    return data


def create_label(name: str, *, repo: str, color: str = "ededed", description: str = "") -> None:
    validate_repo(repo)
    _validate_text(name, "name")
    _validate_text(description, "description")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        raise ValueError("color must be six hexadecimal digits")
    argv = [GH_BINARY, "label", "create", name, "--color", color, "--description", description, "--repo", repo]
    proc = _run(argv)
    _require_success(proc, argv)


def view_pr(number: int, *, repo: str | None = None) -> dict:
    _validate_pr_number(number)
    argv = [
        GH_BINARY, "pr", "view", str(number),
        "--json", "number,title,state,body,url,headRefName,baseRefName,isDraft,comments",
    ]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
    data = _parse_json(proc, f"gh pr view {number}")
    if not isinstance(data, dict):
        raise GhOutputError(f"gh pr view {number}: expected a JSON object, got {type(data).__name__}")
    return _validate_pr_shape(data, f"gh pr view {number}")


def pr_checks(number: int, *, repo: str) -> dict:
    """Read a PR's state, head commit and status-check rollup (read-only `gh pr view`)."""
    _validate_pr_number(number)
    argv = _with_repo([GH_BINARY, "pr", "view", str(number),
                       "--json", "number,state,url,headRefOid,isDraft,statusCheckRollup"], repo)
    proc = _run(argv)
    _require_success(proc, argv)
    data = _parse_json(proc, f"gh pr view {number}")
    if not isinstance(data, dict):
        raise GhOutputError(f"gh pr view {number}: expected a JSON object")
    _require_keys(data, ["number", "state", "url", "headRefOid", "isDraft", "statusCheckRollup"], f"gh pr view {number}")
    if data["number"] != number or str(data["state"]).lower() not in {"open", "closed", "merged"}:
        raise GhOutputError(f"gh pr view {number}: unexpected number or state")
    if not isinstance(data["headRefOid"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", data["headRefOid"]):
        raise GhOutputError(f"gh pr view {number}: invalid headRefOid")
    if not isinstance(data["statusCheckRollup"], list) or not all(isinstance(c, dict) for c in data["statusCheckRollup"]):
        raise GhOutputError(f"gh pr view {number}: invalid statusCheckRollup")
    return data


_CHECK_FAILED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE", "ERROR"}
_CHECK_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def classify_checks(rollup: list[dict]) -> str:
    """green only when at least one check exists and every check passed; none when CI reported nothing."""
    if not rollup:
        return "none"
    verdicts = []
    for check in rollup:
        if check.get("__typename") == "StatusContext" or "state" in check and "conclusion" not in check:
            state = str(check.get("state") or "").upper()
            verdicts.append("passed" if state == "SUCCESS" else "failed" if state in _CHECK_FAILED else "pending")
        else:
            status = str(check.get("status") or "").upper()
            conclusion = str(check.get("conclusion") or "").upper()
            if status != "COMPLETED":
                verdicts.append("pending")
            else:
                verdicts.append("passed" if conclusion in _CHECK_PASSED else "failed")
    if "failed" in verdicts:
        return "red"
    return "pending" if "pending" in verdicts else "green"


# ---------------------------------------------------------------------------
# Mutating operations (backlog-integration-layer only)
# ---------------------------------------------------------------------------

_ISSUE_URL_NUMBER_RE = re.compile(r"/issues/(\d+)\s*$")
_PR_URL_NUMBER_RE = re.compile(r"/pull/(\d+)\s*$")


def create_issue(title: str, body: str = "", labels: list[str] | None = None, *, repo: str | None = None) -> dict:
    _validate_text(title, "title")
    _validate_text(body, "body")
    argv = [GH_BINARY, "issue", "create", "--title", title, "--body", body]
    for label in labels or []:
        _validate_text(label, "label")
        argv += ["--label", label]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
    output = proc.stdout.strip()
    last_line = output.splitlines()[-1] if output else ""
    match = _ISSUE_URL_NUMBER_RE.search(last_line)
    if not match:
        raise GhOutputError(f"gh issue create: could not parse issue number from output: {output!r}")
    return {"number": int(match.group(1)), "url": last_line}


def list_prs(*, base: str, head: str, repo: str | None = None) -> list[dict]:
    """Find PRs for one exact head/base pair, including closed PRs."""
    for value, field in ((base, "base"), (head, "head")):
        _validate_text(value, field)
        if not value.strip():
            raise ValueError(f"{field} must be non-empty")
    argv = [GH_BINARY, "pr", "list", "--state", "all", "--base", base, "--head", head,
            "--limit", "100", "--json", "number,title,state,body,url,headRefName,baseRefName,isDraft"]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
    data = _parse_json(proc, "gh pr list")
    if not isinstance(data, list):
        raise GhOutputError("gh pr list: expected an array")
    return [_validate_pr_shape(pr, "gh pr list") for pr in data]


def create_pr(title: str, body: str, *, base: str, head: str, repo: str | None = None,
              draft: bool = False) -> dict:
    """Create a PR for an already-pushed head branch. This wrapper never
    pushes branches, checks out branches, marks PRs ready, merges PRs, or
    calls `gh api`; callers must provide an explicit --head and --base."""
    _validate_text(title, "title")
    _validate_text(body, "body")
    _validate_text(base, "base")
    _validate_text(head, "head")
    if not base.strip():
        raise ValueError("base must be a non-empty branch/ref name")
    if not head.strip():
        raise ValueError("head must be a non-empty branch/ref name")
    argv = [GH_BINARY, "pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
    if draft:
        argv.append("--draft")
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
    output = proc.stdout.strip()
    last_line = output.splitlines()[-1] if output else ""
    match = _PR_URL_NUMBER_RE.search(last_line)
    if not match:
        raise GhOutputError(f"gh pr create: could not parse PR number from output: {output!r}")
    return {"number": int(match.group(1)), "url": last_line}


def comment_issue(number: int, body: str, *, repo: str | None = None) -> None:
    _validate_issue_number(number)
    _validate_text(body, "body")
    if not body.strip():
        raise ValueError("body must be non-empty")
    argv = [GH_BINARY, "issue", "comment", str(number), "--body", body]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)


def edit_pr(
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    repo: str | None = None,
) -> None:
    _validate_pr_number(number)
    argv = [GH_BINARY, "pr", "edit", str(number)]
    if title is not None:
        _validate_text(title, "title")
        if not title.strip():
            raise ValueError("title must be non-empty")
        argv += ["--title", title]
    if body is not None:
        _validate_text(body, "body")
        argv += ["--body", body]
    if len(argv) == 4:
        raise ValueError("edit_pr requires at least one of title/body")
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)


def comment_pr(number: int, body: str, *, repo: str | None = None) -> None:
    _validate_pr_number(number)
    _validate_text(body, "body")
    if not body.strip():
        raise ValueError("body must be non-empty")
    argv = [GH_BINARY, "pr", "comment", str(number), "--body", body]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)


def edit_issue(
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    repo: str | None = None,
) -> None:
    _validate_issue_number(number)
    argv = [GH_BINARY, "issue", "edit", str(number)]
    if title is not None:
        _validate_text(title, "title")
        argv += ["--title", title]
    if body is not None:
        _validate_text(body, "body")
        argv += ["--body", body]
    for label in add_labels or []:
        _validate_text(label, "label")
        argv += ["--add-label", label]
    for label in remove_labels or []:
        _validate_text(label, "label")
        argv += ["--remove-label", label]
    if len(argv) == 4:
        raise ValueError("edit_issue requires at least one of title/body/add_labels/remove_labels")
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)


def close_issue(number: int, comment: str | None = None, *, repo: str | None = None) -> None:
    """Close a GitHub issue. Callers MUST apply the backlog completed-status
    close-guard before calling this (see tools/backlog.py github_push_task);
    this module has no backlog awareness and cannot enforce that guard
    itself, so it must never be called directly from anywhere else."""
    _validate_issue_number(number)
    argv = [GH_BINARY, "issue", "close", str(number)]
    if comment is not None:
        _validate_text(comment, "comment")
        argv += ["--comment", comment]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)


def reopen_issue(number: int, *, repo: str | None = None) -> None:
    _validate_issue_number(number)
    argv = [GH_BINARY, "issue", "reopen", str(number)]
    argv = _with_repo(argv, repo)
    proc = _run(argv)
    _require_success(proc, argv)
