#!/usr/bin/env python3
"""Read-only open-issues view for the Pico Garden (`GET /api/issues`).

The Garden lists the open issues of its target repository (or of every
repository in a workspace manifest) so that the work waiting to be done is
visible next to the work in motion. Nothing here mutates GitHub or Git.

Rules, all fail closed:
- The repository identity is re-resolved on every request with
  github_integration.resolve_target, the same resolution every other PICO
  command uses. An ambiguous, unresolved or conflicting identity is reported
  with its existing code (TARGET_REPOSITORY_AMBIGUOUS, _UNRESOLVED, _CONFLICT)
  and no issue is fetched for it.
- `gh` is reached only through tools/github_sync.py's fixed-argv wrappers.
  A missing, unauthenticated, rate-limited or failing `gh` makes the
  repository entry `available: false` with a code and a fixed reason, and
  `issues: null`. An unavailable list is never served as an empty list.
- Results, including failures, are cached per repository for CACHE_TTL
  seconds, so the Garden's polling never hammers `gh`.
- Only display fields are served: number, title, label names, assignee
  logins, update time and a URL rebuilt from the verified identity. The page
  renders them as text, never as HTML.

References (`references` on each issue) are computed deterministically:
- `branches`: local `refs/heads/pico/*` branches whose name carries the issue
  number as an `issue-<n>`, `issues-<n>` or `gh-<n>` token
  (for example `pico/issue-33` or `pico/gh-33-garden`).
- `pull_requests`: open PRs (first PR_SCAN_LIMIT) whose body uses a GitHub
  closing keyword for the issue (`Closes #33`, `fixes owner/repo#33`, ...) or
  whose head branch carries the same `issue-<n>` token. Links made only in the
  GitHub UI, and references such as `Refs #33`, are not seen.
Each is `null` when it could not be determined, never an empty guess.

A future per-row action (the issue queue) addresses an issue by the pair
(repository entry `id` or `repository`, issue `number`).
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_integration  # noqa: E402
import github_sync  # noqa: E402

CAP = 50
PR_SCAN_LIMIT = 100
CACHE_TTL = 60.0
GH_TIMEOUT = 10  # seconds per read-only gh call; the CLI default is 30
MAX_PARALLEL = 4
MAX_TITLE = 512
MAX_LABEL = 100
MAX_LABELS = 20
MAX_ASSIGNEES = 10
STAMP = re.compile(r"^[0-9T:.Z+-]{1,40}$")
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BRANCH = re.compile(r"^pico/[A-Za-z0-9._/-]{1,200}$")
ISSUE_TOKEN = re.compile(r"(?:^|[/_.-])(?:issues?|gh)[-_]?([1-9][0-9]{0,9})(?=$|[/_.-])", re.IGNORECASE)
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
                     r"(?:([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*))?#([1-9][0-9]{0,9})\b",
                     re.IGNORECASE)

REFERENCE_RULES = {
    "branches": "local refs/heads/pico/* whose name has an issue-<n>, issues-<n> or gh-<n> token",
    "pull_requests": f"open PRs (first {PR_SCAN_LIMIT}) with a GitHub closing keyword for the issue in "
                     "their body, or an issue-<n> token in their head branch; UI-only links are not seen",
}

REASONS = {
    "GH_UNAVAILABLE": "the gh CLI is not installed or not on PATH",
    "GH_UNAUTHENTICATED": "gh is not authenticated; run gh auth login",
    "GH_RATE_LIMITED": "GitHub rate limit reached; the list will retry after the cache expires",
    "GH_TIMEOUT": "gh did not answer in time",
    "GH_OUTPUT_INVALID": "gh returned output in an unexpected shape",
    "GH_COMMAND_FAILED": "gh issue list failed",
    "TARGET_REPOSITORY_AMBIGUOUS": "the checkout has several GitHub remotes; start pico watch with --repo",
    "TARGET_REPOSITORY_UNRESOLVED": "the checkout's GitHub repository cannot be resolved",
    "TARGET_REPOSITORY_CONFLICT": "the checkout's GitHub remotes no longer match the watched repository",
    "WORKSPACE_REPOSITORY_PATH_MISSING": "the repository checkout is missing",
    "TARGET_REPOSITORY_UNREADABLE": "git could not read the checkout (git missing or timed out)",
    "ISSUES_FETCH_IN_PROGRESS": "the issues of this repository are still being fetched",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_error_code(exc: Exception) -> str:
    """Map a github_sync failure to a stable code; raw gh output is never served."""
    text = str(exc).lower()
    if isinstance(exc, github_sync.GhUnavailableError):
        return "GH_UNAVAILABLE"
    if "rate limit" in text:
        return "GH_RATE_LIMITED"  # checked before auth: rate-limit messages mention authentication
    if isinstance(exc, github_sync.GhAuthError):
        return "GH_UNAUTHENTICATED"
    if isinstance(exc, github_sync.GhTimeoutError):
        return "GH_TIMEOUT"
    if isinstance(exc, github_sync.GhOutputError):
        return "GH_OUTPUT_INVALID"
    return "GH_COMMAND_FAILED"


def issue_tokens(name: str) -> set[int]:
    return {int(match) for match in ISSUE_TOKEN.findall(name)}


def closing_references(body: str, repository: str) -> set[int]:
    numbers = set()
    for repo, number in CLOSING.findall(body or ""):
        if not repo or repo.casefold() == repository.casefold():
            numbers.add(int(number))
    return numbers


def local_pico_branches(checkout: Path) -> list[str] | None:
    try:
        proc = subprocess.run(["git", "-C", str(checkout), "for-each-ref", "--format=%(refname:short)",
                               "refs/heads/pico/"], shell=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode:
        return None
    return sorted(name for name in proc.stdout.splitlines() if BRANCH.fullmatch(name))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _issue(raw: dict, repository: str) -> dict:
    if type(raw.get("number")) is not int or raw["number"] < 1:  # a bool is not an issue number
        raise github_sync.GhOutputError("gh issue list: invalid issue number")
    title = _clip(raw["title"], MAX_TITLE)
    labels = [_clip(item["name"], MAX_LABEL) for item in raw["labels"] if isinstance(item.get("name"), str)][:MAX_LABELS]
    assignees = [item["login"] for item in raw["assignees"]
                 if isinstance(item.get("login"), str) and LOGIN.fullmatch(item["login"])][:MAX_ASSIGNEES]
    updated = raw.get("updatedAt")
    return {"number": raw["number"], "title": title, "labels": labels, "assignees": assignees,
            "updated_at": updated if isinstance(updated, str) and STAMP.fullmatch(updated) else None,
            "url": f"https://github.com/{repository}/issues/{raw['number']}"}


class Target:
    """One repository whose issues the Garden shows."""

    def __init__(self, *, id: str | None, repository: str | None, checkout: Path | None,
                 explicit: str | None, blocker_code: str | None = None):
        self.id, self.repository, self.checkout = id, repository, checkout
        self.explicit, self.blocker_code = explicit, blocker_code


class IssuesSource:
    """Serves `GET /api/issues`; `targets` is called on every request."""

    def __init__(self, targets: Callable[[], list[Target]], *, mode: str, adapter=github_sync,
                 clock: Callable[[], float] = time.monotonic, ttl: float = CACHE_TTL, cap: int = CAP):
        self.targets, self.mode, self.adapter = targets, mode, adapter
        self.clock, self.ttl, self.cap = clock, ttl, cap
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._inflight: set[tuple] = set()
        # Guards only the cache and the in-flight set; it is never held while gh or git runs.
        self._lock = threading.Lock()

    def view(self) -> dict:
        targets = self.targets()
        seen: set[tuple] = set()
        if len(targets) > 1:
            # Repositories are read in parallel, so one slow repository does not delay the others.
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(targets))) as pool:
                entries = list(pool.map(lambda target: self._entry(target, seen), targets))
        else:
            entries = [self._entry(target, seen) for target in targets]
        self._prune(seen)
        return {"ok": True, "mode": self.mode, "cap": self.cap, "cache_ttl_seconds": self.ttl,
                "reference_rules": REFERENCE_RULES, "repositories": entries}

    def _prune(self, current: set[tuple]) -> None:
        """Drop expired entries and repositories that are no longer watched, so the cache stays bounded."""
        now = self.clock()
        with self._lock:
            # An expired entry being refetched is kept: concurrent requests serve it as stale meanwhile.
            for key in [k for k, (at, _) in self._cache.items()
                        if k not in current or (now - at >= self.ttl and k not in self._inflight)]:
                del self._cache[key]

    @staticmethod
    def _blank(target: Target, repository: str | None) -> dict:
        return {"id": target.id if isinstance(target.id, str) and REPO_ID.fullmatch(target.id) else None,
                "repository": repository, "available": False, "error_code": None, "error": None,
                "fetched_at": None, "cached": False, "truncated": False, "issues": None,
                "pull_requests": {"available": False, "error_code": None, "scanned": 0, "truncated": False},
                "branches": {"available": False, "scanned": 0}}

    def _failed(self, target: Target, repository: str | None, code: str) -> dict:
        entry = self._blank(target, repository)
        entry.update({"error_code": code, "error": REASONS.get(code, "the repository cannot be read")})
        return entry

    def _entry(self, target: Target, seen: set[tuple] | None = None) -> dict:
        expected = target.repository if isinstance(target.repository, str) else None
        try:
            expected = github_sync.validate_repo(expected) if expected else None
        except ValueError:
            expected = None
        if target.blocker_code:
            return self._failed(target, expected, target.blocker_code)
        if target.checkout is None:
            return self._failed(target, expected, "WORKSPACE_REPOSITORY_PATH_MISSING")
        try:
            identity = github_integration.resolve_target(target.checkout, target.explicit)
        except github_integration.IntegrationError as exc:
            return self._failed(target, expected, exc.code)
        except (OSError, subprocess.SubprocessError):
            # git missing (FileNotFoundError) or hung (TimeoutExpired): fail this repository only.
            return self._failed(target, expected, "TARGET_REPOSITORY_UNREADABLE")
        if expected is not None and identity.casefold() != expected.casefold():
            return self._failed(target, expected, "TARGET_REPOSITORY_CONFLICT")
        key = (target.id, identity.casefold(), str(target.checkout))
        with self._lock:
            if seen is not None:
                seen.add(key)
            cached = self._cache.get(key)
            if cached is not None and self.clock() - cached[0] < self.ttl:
                return {**cached[1], "cached": True}
            if key in self._inflight:
                # Another request is already fetching: never start a second gh call or wait for it.
                if cached is not None:
                    return {**cached[1], "cached": True}
                return self._failed(target, identity, "ISSUES_FETCH_IN_PROGRESS")
            self._inflight.add(key)
        try:
            entry = self._fetch(target, identity)
        except Exception:  # never leave a repository marked in flight
            entry = self._failed(target, identity, "GH_COMMAND_FAILED")
            entry["fetched_at"] = _now()
        with self._lock:
            self._inflight.discard(key)
            self._cache[key] = (self.clock(), entry)
        return entry

    def _fetch(self, target: Target, identity: str) -> dict:
        entry = self._blank(target, identity)
        entry["fetched_at"] = _now()
        try:
            raw = self.adapter.list_issue_summaries(self.cap + 1, repo=identity, timeout=GH_TIMEOUT)
            issues = [_issue(item, identity) for item in raw[:self.cap]]
        except (github_sync.GithubSyncError, ValueError) as exc:
            code = gh_error_code(exc)
            entry.update({"error_code": code, "error": REASONS[code]})
            return entry
        entry.update({"available": True, "truncated": len(raw) > self.cap, "issues": issues})

        branches = local_pico_branches(target.checkout)
        if branches is not None:
            entry["branches"] = {"available": True, "scanned": len(branches)}
        try:
            prs = self.adapter.list_open_prs(PR_SCAN_LIMIT + 1, repo=identity, timeout=GH_TIMEOUT)
            entry["pull_requests"] = {"available": True, "error_code": None, "scanned": min(len(prs), PR_SCAN_LIMIT),
                                      "truncated": len(prs) > PR_SCAN_LIMIT}
            prs = prs[:PR_SCAN_LIMIT]
        except (github_sync.GithubSyncError, ValueError) as exc:
            prs = None
            entry["pull_requests"]["error_code"] = gh_error_code(exc)

        for issue in issues:
            number = issue["number"]
            linked_prs = None
            if prs is not None:
                linked_prs = [{"number": pr["number"], "draft": pr["isDraft"],
                               "url": f"https://github.com/{identity}/pull/{pr['number']}"}
                              for pr in prs
                              if number in closing_references(pr["body"], identity) | issue_tokens(pr["headRefName"])]
                linked_prs.sort(key=lambda pr: pr["number"])
            issue["references"] = {
                "branches": None if branches is None else [b for b in branches if number in issue_tokens(b)],
                "pull_requests": linked_prs}
        return entry


def repository_targets(checkout: Path, explicit: str | None, repository: str) -> Callable[[], list[Target]]:
    """Single-repository mode: the checkout `pico watch` was started on."""
    return lambda: [Target(id=None, repository=repository, checkout=checkout, explicit=explicit)]


def workspace_targets(plan: Callable[[], dict | None]) -> Callable[[], list[Target]]:
    """Workspace mode: every repository of the manifest, with its declared identity as the explicit target."""
    def targets() -> list[Target]:
        current = plan() or {"repositories": []}
        return [Target(id=repo.get("id"), repository=repo.get("repository"),
                       checkout=Path(repo["resolved_path"]) if repo.get("resolved_path") else None,
                       explicit=repo.get("repository"), blocker_code=repo.get("blocker_code"))
                for repo in current["repositories"]]
    return targets
