#!/usr/bin/env python3
"""Deterministic backlog CLI and GitHub-synchronization composition layer.

This is the single place an LLM/orchestrator should invoke to perform
routine backlog transitions and GitHub synchronization -- it should never
need to hand-edit backlog/backlog.yaml or provenance/runs/*.json directly.

Layering:
  tools/backlog_store.py  -- backlog data model, transitions, validation, I/O
  tools/provenance.py     -- append-only run records
  tools/github_sync.py    -- pure, allowlisted `gh` subprocess boundary
  tools/backlog.py (here) -- CLI + composition: decides *when* to call
                              github_sync.py's functions and reconciles the
                              result with backlog_store.py's local state.

tools/backlog.py and parent-owned tools/github_integration.py use the same
allowlisted tools/github_sync.py boundary. No GitHub credential/token is ever read,
stored, or forwarded by this module -- `gh` resolves its own auth. Ordinary
workers spawned per an execution plan never call this module and must not be
given a path to `gh`; they operate on the working tree only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backlog_store as store  # noqa: E402
import github_sync as gh  # noqa: E402
import provenance  # noqa: E402


# ---------------------------------------------------------------------------
# GitHub composition errors
# ---------------------------------------------------------------------------

class SyncConflictError(Exception):
    """Local and remote state diverged independently since the last known
    sync snapshot, and the authoritative update cannot be determined safely.
    Neither side is overwritten; the caller must resolve manually."""

    def __init__(self, item_id: str, detail: str):
        self.item_id = item_id
        self.detail = detail
        super().__init__(f"{item_id}: sync conflict: {detail}")


class CloseGuardError(Exception):
    """Refused to close a GitHub issue because the corresponding backlog
    item is not (yet) deterministically 'completed'."""


class UnmappedIssueError(Exception):
    """No backlog item is mapped to the given GitHub issue number."""


class PrLifecycleError(Exception):
    """Refused to create/link a PR because lifecycle preconditions are unmet."""




def _repo_name(repo_root: Path) -> str:
    data = gh.repo_view(cwd=str(repo_root))
    name = data.get("nameWithOwner")
    if not name:
        raise gh.GhOutputError("gh repo view: missing nameWithOwner")
    return gh.validate_repo(name)


def _ensure_snapshot_matches(item: dict, repo: str, issue_number: int) -> None:
    snapshot = item.get("github_sync")
    if snapshot and (snapshot.get("repo") != repo or snapshot.get("issue") != issue_number):
        raise SyncConflictError(item["id"], "github_sync baseline identity does not match mapped GitHub issue")


def _ensure_pr_snapshot_matches(item: dict, repo: str, pr_number: int) -> None:
    snapshot = item.get("github_pr_sync")
    if snapshot and (snapshot.get("repo") != repo or snapshot.get("pr") != pr_number):
        raise SyncConflictError(item["id"], "github_pr_sync baseline identity does not match mapped GitHub PR")


# ---------------------------------------------------------------------------
# GitHub composition: status / sync / push / pull
# ---------------------------------------------------------------------------

def github_sync_status(repo_root: Path = REPO_ROOT) -> dict:
    """Read-only diagnostic: is `gh` installed, authenticated, and which
    repository does it resolve to. Never mutates anything."""
    if not gh.gh_available():
        return {"gh_available": False, "authenticated": False, "repo": None, "error": "gh CLI not found on PATH"}
    try:
        auth = gh.auth_status()
    except gh.GithubSyncError as exc:
        return {"gh_available": True, "authenticated": False, "repo": None, "error": str(exc)}
    if not auth["authenticated"]:
        return {"gh_available": True, "authenticated": False, "repo": None, "error": auth["detail"]}
    try:
        repo = gh.repo_view(cwd=str(repo_root))
    except gh.GithubSyncError as exc:
        return {"gh_available": True, "authenticated": True, "repo": None, "error": str(exc)}
    return {"gh_available": True, "authenticated": True, "repo": repo, "error": None}


def _desired_remote_state(item: dict) -> str:
    return "closed" if item["status"] == "completed" else "open"


def _guard_close(item: dict) -> None:
    """The sole, independently-enforced source of truth for "is it safe to
    close this issue". Must be called immediately before every
    gh.close_issue(...) invocation in this module, regardless of how the
    caller arrived at a desire to close (i.e. regardless of how
    `desired_state` was computed). Deliberately does NOT trust
    `_desired_remote_state`'s own construction as a proxy for this check --
    if `_desired_remote_state` is ever changed later (e.g. to also close on
    'cancelled'), this guard remains the independent last line of defense."""
    if item["status"] != "completed":
        raise CloseGuardError(
            f"{item['id']}: refusing to close issue #{item.get('github_issue')}: backlog item "
            f"status is {item['status']!r}, not 'completed'"
        )


def _normalize_labels(labels: Any) -> list[str]:
    return sorted((l["name"] if isinstance(l, dict) else l) for l in (labels or []))


def _compare_remote(item: dict, remote: dict) -> dict:
    """Pure comparison of a backlog item's local state against a
    freshly-fetched remote issue and (if any) the item's last-known sync
    snapshot. Shared by github_sync_task, github_push_task, and
    github_pull_task so all three apply identical title/state/labels
    conflict semantics.

    Critically: when no snapshot exists yet (`has_baseline` is False --
    meaning this backlog item has never been synced against this GitHub
    issue before), the absence of a *detected conflict* must never be read
    by a caller as "verified no divergence, safe to overwrite". Without a
    baseline there is no prior state to diff against, so conflict detection
    (which requires knowing what changed *since* a known-good point) is
    structurally unable to run -- title_conflict/state_conflict/
    labels_conflict are always False in that case not because nothing
    diverged, but because divergence can't even be evaluated yet. Callers
    MUST check `has_baseline` themselves before treating "no conflict" as
    "safe"."""
    snapshot = item.get("github_sync") or {}
    has_baseline = bool(snapshot)

    desired_state = _desired_remote_state(item)
    remote_state = remote["state"].lower()
    remote_labels = _normalize_labels(remote.get("labels", []))
    local_labels = _normalize_labels(item.get("labels", []))

    remote_title_changed = has_baseline and remote["title"] != snapshot.get("remote_title")
    local_title_changed = has_baseline and item["title"] != snapshot.get("remote_title")
    title_conflict = remote_title_changed and local_title_changed and remote["title"] != item["title"]

    baseline_state = snapshot.get("remote_state") if has_baseline else None
    remote_state_changed = has_baseline and remote_state != baseline_state
    local_wants_state_change = has_baseline and desired_state != baseline_state
    state_conflict = remote_state_changed and local_wants_state_change and remote_state != desired_state

    baseline_labels = snapshot.get("remote_labels", []) if has_baseline else []
    remote_labels_changed = has_baseline and remote_labels != baseline_labels
    local_labels_changed = has_baseline and local_labels != baseline_labels
    labels_conflict = remote_labels_changed and local_labels_changed and remote_labels != local_labels

    in_sync = (
        remote["title"] == item["title"]
        and remote_state == desired_state
        and remote_labels == local_labels
    )

    return {
        "has_baseline": has_baseline,
        "desired_state": desired_state,
        "remote_state": remote_state,
        "remote_labels": remote_labels,
        "local_labels": local_labels,
        "title_conflict": title_conflict,
        "state_conflict": state_conflict,
        "labels_conflict": labels_conflict,
        "any_conflict": title_conflict or state_conflict or labels_conflict,
        "in_sync": in_sync,
    }


def github_sync_task(id: str, repo_root: Path = REPO_ROOT) -> dict:
    """Read-only comparison of a backlog item against its associated GitHub
    issue (if any). Never mutates local or remote state; use
    github_push_task/github_pull_task to actually reconcile.

    When no prior sync snapshot exists for this item/issue pair
    (`has_baseline` is False in the result), `status` is reported as
    'baseline_required' rather than 'in_sync' whenever the remote's current
    title/state/labels don't already match local -- there is no verified
    baseline to prove that divergence is benign, so this is deliberately
    distinct from a verified 'diverged'/'conflict' result. See
    github_push_task's `establish_baseline` parameter and
    github_pull_task, either of which create a baseline."""
    item = store.backlog_get(id, repo_root)
    issue_number = item.get("github_issue")
    if issue_number is None:
        return {"id": id, "status": "no_issue"}

    repo = _repo_name(repo_root)
    _ensure_snapshot_matches(item, repo, issue_number)
    remote = gh.view_issue(issue_number, repo=repo)
    cmp = _compare_remote(item, remote)

    if not cmp["has_baseline"]:
        status = "in_sync" if cmp["in_sync"] else "baseline_required"
    else:
        status = "conflict" if cmp["any_conflict"] else ("in_sync" if cmp["in_sync"] else "diverged")

    return {
        "id": id,
        "status": status,
        "has_baseline": cmp["has_baseline"],
        "local": {
            "title": item["title"],
            "desired_remote_state": cmp["desired_state"],
            "labels": cmp["local_labels"],
        },
        "remote": {"title": remote["title"], "state": cmp["remote_state"], "labels": cmp["remote_labels"]},
        "title_conflict": cmp["title_conflict"],
        "state_conflict": cmp["state_conflict"],
        "labels_conflict": cmp["labels_conflict"],
    }


def github_push_task(id: str, repo_root: Path = REPO_ROOT, *, establish_baseline: bool = False) -> dict:
    """Push local backlog item `id` to GitHub: create the issue if none
    exists yet, otherwise reconcile title/labels/open-closed state -- unless
    a conflict is detected (fail-and-report, no mutation on either side).

    Baseline requirement: if `id` is already linked to a `github_issue` but
    has never been synced against it before (no `github_sync` snapshot --
    i.e. it was linked via `backlog_add`/`backlog_update(github_issue=...)`
    directly, not via this function's own create-issue path), this function
    refuses to push unless either (a) the remote's current title/state/
    labels already exactly match what would be pushed (nothing to
    overwrite, so recording the baseline is safe), or (b) the caller passes
    `establish_baseline=True` to explicitly acknowledge that this is a
    deliberate initial link that should overwrite remote fields to match
    local, or (c) a baseline already exists because github_pull_task or a
    prior github_push_task call recorded one. This prevents silently
    clobbering a pre-existing GitHub issue's independently-set state. The
    brand-new-issue path (`github_issue` is None) never needs this guard:
    this function just created that issue, so there is nothing pre-existing
    to clobber.

    Close-guard: the associated issue is only ever closed here when the
    local item's status is deterministically 'completed' (enforced by the
    standalone `_guard_close`, called immediately before every close). This
    function is the sole caller of github_sync.close_issue in this
    repository."""
    item = store.backlog_get(id, repo_root)
    issue_number = item.get("github_issue")

    if issue_number is None:
        body = _format_issue_body(item)
        repo = _repo_name(repo_root)
        created = gh.create_issue(item["title"], body, labels=item.get("labels"), repo=repo)
        snapshot = {
            "last_synced_at": store.now_iso(),
            "repo": repo,
            "issue": created["number"],
            "remote_title": item["title"],
            "remote_state": "open",
            "remote_labels": _normalize_labels(item.get("labels", [])),
        }
        store.backlog_update(id, github_issue=created["number"], github_sync=snapshot, repo_root=repo_root)
        if item["status"] == "completed":
            _guard_close(item)
            gh.close_issue(created["number"], repo=repo)
            snapshot["last_synced_at"] = store.now_iso()
            snapshot["remote_state"] = "closed"
            store.backlog_update(id, github_sync=snapshot, repo_root=repo_root)
            return {"id": id, "action": ["created", "closed"], "issue": created["number"], "url": created["url"]}
        return {"id": id, "action": "created", "issue": created["number"], "url": created["url"]}

    repo = _repo_name(repo_root)
    _ensure_snapshot_matches(item, repo, issue_number)
    remote = gh.view_issue(issue_number, repo=repo)
    cmp = _compare_remote(item, remote)

    if cmp["has_baseline"] and cmp["any_conflict"]:
        raise SyncConflictError(
            id,
            f"local and remote diverged independently since last sync "
            f"(title_conflict={cmp['title_conflict']}, state_conflict={cmp['state_conflict']}, "
            f"labels_conflict={cmp['labels_conflict']}); refusing to overwrite either side",
        )

    if not cmp["has_baseline"] and not cmp["in_sync"] and not establish_baseline:
        raise SyncConflictError(
            id,
            f"backlog item {id!r} is linked to GitHub issue #{issue_number} but has never been "
            "synced against it before (no github_sync baseline snapshot exists), and the remote "
            "issue's current title/state/labels differ from what would be pushed; refusing to "
            "silently overwrite the remote. Run github_pull_task or github_push_task/"
            "github_sync_task to establish a verified baseline first, or call github_push_task(..., "
            "establish_baseline=True) if this is a deliberate initial link that should take over "
            "the pre-existing issue's fields.",
        )

    desired_state = cmp["desired_state"]
    state_changing = cmp["remote_state"] != desired_state
    # Validate the close-guard *before* issuing any mutating gh call (title
    # or labels included): if closing would be refused, nothing should be
    # partially pushed either -- fail atomically, mutate nothing.
    if state_changing and desired_state == "closed":
        _guard_close(item)

    actions: list[str] = []
    edit_kwargs: dict[str, Any] = {}
    if remote["title"] != item["title"]:
        edit_kwargs["title"] = item["title"]

    added_labels = sorted(set(cmp["local_labels"]) - set(cmp["remote_labels"]))
    removed_labels = sorted(set(cmp["remote_labels"]) - set(cmp["local_labels"]))
    if added_labels:
        edit_kwargs["add_labels"] = added_labels
    if removed_labels:
        edit_kwargs["remove_labels"] = removed_labels

    if edit_kwargs:
        gh.edit_issue(issue_number, repo=repo, **edit_kwargs)
        if "title" in edit_kwargs:
            actions.append("title_updated")
        if "add_labels" in edit_kwargs or "remove_labels" in edit_kwargs:
            actions.append("labels_updated")

    if state_changing:
        if desired_state == "closed":
            # Guard already validated above; re-affirm immediately before
            # the mutating call per this module's invariant that _guard_close
            # is called right before every close_issue invocation.
            _guard_close(item)
            gh.close_issue(issue_number, repo=repo)
            actions.append("closed")
        else:
            gh.reopen_issue(issue_number, repo=repo)
            actions.append("reopened")

    snapshot = {
        "last_synced_at": store.now_iso(),
        "repo": repo,
        "issue": issue_number,
        "remote_title": item["title"],
        "remote_state": desired_state,
        "remote_labels": cmp["local_labels"],
    }
    store.backlog_update(id, github_sync=snapshot, repo_root=repo_root)
    return {"id": id, "action": actions or ["no_change"], "issue": issue_number}


def github_pull_task(issue_number: int, repo_root: Path = REPO_ROOT) -> dict:
    """Pull GitHub issue `issue_number` into the backlog item mapped to it.

    Deliberately conservative: this NEVER changes a backlog item's `status`
    (status is governed solely by tools/backlog_store.py's deterministic
    transition table -- GitHub's open/closed state is a coarser, human-facing
    signal that must not silently drive workflow state). It refreshes the
    cached github_sync snapshot and the mirrored `title`/`labels`, unless a
    conflict is detected, in which case it fails and reports rather than
    guessing which side is authoritative. If the remote is closed but the
    local item is not completed/cancelled, that discrepancy is reported in
    the result for a human/orchestrator to resolve -- it is not auto-repaired.

    Pulling is safe to run even with no prior sync snapshot: it only ever
    writes to the LOCAL mirrored fields (title/labels/github_sync), never to
    the remote issue, so there is no "silently overwrite the remote" risk
    here. Running this function is itself the recommended way to establish
    a verified baseline before a subsequent github_push_task call. The
    result's `has_baseline` field reports whether a prior baseline already
    existed (True) or this call itself establishes the first one (False),
    so callers can distinguish "verified no conflict" from "first sync"."""
    items = store.load_backlog(repo_root)
    matches = [i for i in items if i.get("github_issue") == issue_number]
    if not matches:
        raise UnmappedIssueError(f"no backlog item is mapped to GitHub issue #{issue_number}")
    if len(matches) > 1:
        raise UnmappedIssueError(
            f"GitHub issue #{issue_number} is mapped to multiple backlog items: "
            f"{[m['id'] for m in matches]} (data integrity error)"
        )
    item = matches[0]
    id = item["id"]

    repo = _repo_name(repo_root)
    _ensure_snapshot_matches(item, repo, issue_number)
    remote = gh.view_issue(issue_number, repo=repo)
    cmp = _compare_remote(item, remote)

    # State conflicts are intentionally NOT blocked here: pulling never
    # writes remote/open-closed state into the backlog item's `status`
    # (see docstring above), so a state divergence is reported as a
    # `discrepancy` below rather than refused as a conflict. Title/labels
    # ARE mirrored into local fields, so those conflicts must block --
    # otherwise an independent local edit could be silently clobbered.
    if cmp["has_baseline"] and (cmp["title_conflict"] or cmp["labels_conflict"]):
        raise SyncConflictError(
            id,
            f"remote diverged from local independently since last sync "
            f"(title_conflict={cmp['title_conflict']}, labels_conflict={cmp['labels_conflict']}); "
            "refusing to pick a side",
        )

    new_snapshot = {
        "last_synced_at": store.now_iso(),
        "repo": repo,
        "issue": issue_number,
        "remote_title": remote["title"],
        "remote_state": cmp["remote_state"],
        "remote_labels": cmp["remote_labels"],
    }
    update_kwargs: dict[str, Any] = {"github_sync": new_snapshot}
    if remote["title"] != item["title"]:
        update_kwargs["title"] = remote["title"]
    if cmp["remote_labels"] != cmp["local_labels"]:
        update_kwargs["labels"] = cmp["remote_labels"]

    updated = store.backlog_update(id, repo_root=repo_root, **update_kwargs)

    discrepancy = None
    desired_state = _desired_remote_state(updated)
    if cmp["remote_state"] != desired_state:
        discrepancy = (
            f"remote issue state is {cmp['remote_state']!r} but local backlog status "
            f"{updated['status']!r} implies {desired_state!r}; status was NOT changed automatically"
        )

    return {
        "id": id,
        "issue": issue_number,
        "has_baseline": cmp["has_baseline"],
        "updated_fields": sorted(update_kwargs),
        "discrepancy": discrepancy,
    }


# ---------------------------------------------------------------------------
# GitHub PR lifecycle composition
# ---------------------------------------------------------------------------

_PR_LIFECYCLE_STATUSES = frozenset({"review", "completed"})
_PR_CLOSING_KEYWORDS = ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved")


def _require_mapped_issue_for_pr(item: dict) -> int:
    issue_number = item.get("github_issue")
    if issue_number is None:
        raise PrLifecycleError(f"{item['id']}: PR lifecycle requires a mapped GitHub issue")
    return issue_number


def _require_pr_lifecycle_preconditions(item: dict) -> int:
    if item.get("status") not in _PR_LIFECYCLE_STATUSES:
        raise PrLifecycleError(
            f"{item['id']}: PR create/link requires backlog status 'review' or 'completed' "
            f"(current: {item.get('status')!r})"
        )
    return _require_mapped_issue_for_pr(item)


def _normalize_pr_state(state: str) -> str:
    return str(state).lower()


def _format_pr_body(item: dict) -> str:
    """Body generated for PR creation. Intentionally contains only a
    non-closing GitHub issue reference (`Refs #N`) and controlled metadata;
    it does not copy arbitrary backlog prose that could accidentally include
    GitHub auto-closing keywords."""
    issue_number = item.get("github_issue")
    lines = [
        f"Backlog id: {item['id']}",
        f"Version: {item['version']}",
        f"GitHub issue: Refs #{issue_number}",
        "",
        "This pull request is associated with the backlog item above.",
    ]
    body = "\n".join(lines)
    for keyword in _PR_CLOSING_KEYWORDS:
        if f"{keyword} #" in body.lower():
            raise PrLifecycleError(f"{item['id']}: generated PR body unexpectedly contains a closing keyword")
    if f"Refs #{issue_number}" not in body:
        raise PrLifecycleError(f"{item['id']}: generated PR body is missing the required Refs issue reference")
    return body


def _pr_snapshot(repo: str, item: dict, pr_number: int, remote: dict) -> dict:
    return {
        "last_synced_at": store.now_iso(),
        "repo": repo,
        "pr": pr_number,
        "issue": item["github_issue"],
        "remote_title": remote["title"],
        "remote_state": _normalize_pr_state(remote["state"]),
        "remote_body": remote["body"],
        "head_ref": remote.get("headRefName"),
        "base_ref": remote.get("baseRefName"),
        "is_draft": remote.get("isDraft"),
    }


def _compare_pr_remote(item: dict, remote: dict) -> dict:
    snapshot = item.get("github_pr_sync") or {}
    has_baseline = bool(snapshot)
    desired_body = _format_pr_body(item)
    remote_state = _normalize_pr_state(remote["state"])

    remote_title_changed = has_baseline and remote["title"] != snapshot.get("remote_title")
    local_title_changed = has_baseline and item["title"] != snapshot.get("remote_title")
    title_conflict = remote_title_changed and local_title_changed and remote["title"] != item["title"]

    remote_body_changed = has_baseline and remote["body"] != snapshot.get("remote_body")
    local_body_changed = has_baseline and desired_body != snapshot.get("remote_body")
    body_conflict = remote_body_changed and local_body_changed and remote["body"] != desired_body

    in_sync = remote["title"] == item["title"] and remote["body"] == desired_body
    return {
        "has_baseline": has_baseline,
        "desired_body": desired_body,
        "remote_state": remote_state,
        "title_conflict": title_conflict,
        "body_conflict": body_conflict,
        "any_conflict": title_conflict or body_conflict,
        "in_sync": in_sync,
    }


def github_pr_sync_task(id: str, repo_root: Path = REPO_ROOT) -> dict:
    """Read-only comparison of a backlog item's linked PR. Never mutates
    local state or GitHub. PR state is observed but never driven from backlog
    status; this module does not close, merge, checkout, mark ready, or push
    PRs."""
    item = store.backlog_get(id, repo_root)
    pr_number = item.get("github_pr")
    if pr_number is None:
        return {"id": id, "status": "no_pr"}
    _require_mapped_issue_for_pr(item)
    repo = _repo_name(repo_root)
    _ensure_pr_snapshot_matches(item, repo, pr_number)
    remote = gh.view_pr(pr_number, repo=repo)
    cmp = _compare_pr_remote(item, remote)
    if not cmp["has_baseline"]:
        status = "in_sync" if cmp["in_sync"] else "baseline_required"
    else:
        status = "conflict" if cmp["any_conflict"] else ("in_sync" if cmp["in_sync"] else "diverged")
    return {
        "id": id,
        "status": status,
        "has_baseline": cmp["has_baseline"],
        "pr": pr_number,
        "issue": item["github_issue"],
        "local": {"title": item["title"], "body": cmp["desired_body"]},
        "remote": {
            "title": remote["title"],
            "state": cmp["remote_state"],
            "body": remote["body"],
            "head_ref": remote.get("headRefName"),
            "base_ref": remote.get("baseRefName"),
            "is_draft": remote.get("isDraft"),
        },
        "title_conflict": cmp["title_conflict"],
        "body_conflict": cmp["body_conflict"],
    }


def github_pr_link_task(id: str, pr_number: int, repo_root: Path = REPO_ROOT) -> dict:
    """Link an existing PR to a review/completed backlog item locally and
    record a baseline snapshot. This does not edit the PR body or any remote
    metadata. PRs are opened only through the gated `github_integration --publish` path."""
    item = store.backlog_get(id, repo_root)
    issue_number = _require_pr_lifecycle_preconditions(item)
    existing = item.get("github_pr")
    if existing is not None and existing != pr_number:
        raise PrLifecycleError(f"{id}: already linked to GitHub PR #{existing}")
    repo = _repo_name(repo_root)
    _ensure_pr_snapshot_matches(item, repo, pr_number)
    remote = gh.view_pr(pr_number, repo=repo)
    snapshot = _pr_snapshot(repo, item, pr_number, remote)
    store.backlog_update(id, github_pr=pr_number, github_pr_sync=snapshot, repo_root=repo_root)
    issue_ref = f"Refs #{issue_number}"
    return {
        "id": id,
        "action": "linked",
        "issue": issue_number,
        "pr": pr_number,
        "url": remote["url"],
        "issue_reference_present": issue_ref in remote.get("body", ""),
    }


def _format_issue_body(item: dict) -> str:
    lines = [item["description"], "", f"Backlog id: {item['id']}", f"Version: {item['version']}"]
    if item.get("acceptance_criteria"):
        lines.append("")
        lines.append("Acceptance criteria:")
        lines.extend(f"- {ac}" for ac in item["acceptance_criteria"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reporting (joins backlog state with provenance attribution)
# ---------------------------------------------------------------------------

def _summarize_run_steps(run: dict) -> list[dict]:
    return [
        {
            "task_id": s["task_id"],
            "role": s.get("role"),
            "family": s.get("family"),
            "backend": s.get("backend"),
            "model": s.get("model"),
            "status": s.get("status"),
            "outcome": s.get("outcome"),
            "review_findings": s.get("review_findings"),
        }
        for s in run.get("steps", [])
    ]


def backlog_report(repo_root: Path = REPO_ROOT) -> dict:
    core = store.backlog_report_core(repo_root).as_dict()
    items = {i["id"]: i for i in store.load_backlog(repo_root)}
    attribution: dict[str, list[dict]] = {}
    for item_id in items:
        runs = provenance.runs_for_backlog(repo_root, item_id)
        if not runs:
            continue
        attribution[item_id] = [
            {
                "run_id": r["run_id"],
                "outcome": r["outcome"],
                "selected_route": (r.get("route_task_result") or {}).get("selected_route"),
                "steps": _summarize_run_steps(r),
            }
            for r in runs
        ]
    core["attribution"] = attribution
    return core


def _render_report_text(report: dict) -> str:
    lines = []
    lines.append(f"Generated at: {report['generated_at']}")
    lines.append(f"In progress: {report['in_progress'] or '(none)'}")
    lines.append(f"Ready next: {report['ready'] or '(none)'}")
    if report["blocked"]:
        lines.append("Blocked:")
        for b in report["blocked"]:
            lines.append(f"  {b['id']}: {b['reason']}")
    else:
        lines.append("Blocked: (none)")
    lines.append("Completed by version:")
    for version in sorted(report["completed_by_version"]):
        lines.append(f"  {version}: {report['completed_by_version'][version]}")
    lines.append("")
    for item_id, runs in report.get("attribution", {}).items():
        lines.append(item_id)
        for run in runs:
            for step in run["steps"]:
                family_label = {"codex": "Codex", "claude": "Claude"}.get(step["family"], step["family"] or "unknown")
                model_label = step["model"] or "unknown"
                outcome_label = step["outcome"] or "unknown"
                lines.append(
                    f"  {step['task_id']:<18} * {family_label:<8} * {model_label:<28} * {outcome_label}"
                )
            lines.append(f"Final status: {run['outcome']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _cmd_list(args: argparse.Namespace) -> int:
    items = store.backlog_list(REPO_ROOT, status=args.status, version=args.version)
    _print_json(items)
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    try:
        _print_json(store.backlog_get(args.id, REPO_ROOT))
        return 0
    except store.NotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_add(args: argparse.Namespace) -> int:
    try:
        item = store.backlog_add(
            id=args.id,
            title=args.title,
            description=args.description,
            version=args.version,
            priority=args.priority,
            status=args.status,
            dependencies=args.dependencies or [],
            acceptance_criteria=args.acceptance_criteria or [],
            labels=args.labels or [],
            github_issue=args.github_issue,
            github_pr=args.github_pr,
            repo_root=REPO_ROOT,
        )
        _print_json(item)
        return 0
    except store.BacklogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_update(args: argparse.Namespace) -> int:
    kwargs: dict[str, Any] = {}
    if args.status is not None:
        kwargs["status"] = args.status
    if args.title is not None:
        kwargs["title"] = args.title
    if args.description is not None:
        kwargs["description"] = args.description
    if args.priority is not None:
        kwargs["priority"] = args.priority
    if args.version is not None:
        kwargs["version"] = args.version
    if args.dependencies is not None:
        kwargs["dependencies"] = args.dependencies
    if args.acceptance_criteria is not None:
        kwargs["acceptance_criteria"] = args.acceptance_criteria
    if args.labels is not None:
        kwargs["labels"] = args.labels
    if args.blocked_reason is not None:
        kwargs["blocked_reason"] = args.blocked_reason
    if args.github_issue is not None:
        kwargs["github_issue"] = args.github_issue
    if args.github_pr is not None:
        kwargs["github_pr"] = args.github_pr
    try:
        item = store.backlog_update(args.id, repo_root=REPO_ROOT, **kwargs)
        _print_json(item)
        return 0
    except store.BacklogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_next(args: argparse.Namespace) -> int:
    _print_json(store.backlog_next(REPO_ROOT))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    report = backlog_report(REPO_ROOT)
    if args.json:
        _print_json(report)
    else:
        print(_render_report_text(report))
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    if args.run_id is not None and (args.backlog_id is not None or args.latest):
        print("error: RUN_ID is mutually exclusive with --backlog-id/--latest", file=sys.stderr)
        return 1
    if args.run_id is None and args.backlog_id is None and not args.latest:
        print("error: specify RUN_ID, --backlog-id, or --latest", file=sys.stderr)
        return 1
    try:
        runs = provenance.select_trace_runs(
            REPO_ROOT, run_id=args.run_id, backlog_id=args.backlog_id, latest=args.latest
        )
    except provenance.ProvenanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not runs:
        print("error: no matching run found", file=sys.stderr)
        return 1
    reports = [provenance.build_trace_report(REPO_ROOT, r) for r in runs]
    if args.json:
        _print_json({"schema_version": provenance.TRACE_SCHEMA_VERSION, "runs": reports})
    else:
        print("\n\n".join(provenance.render_trace_text(r) for r in reports))
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    summary = provenance.summarize_token_usage(provenance.list_runs(REPO_ROOT))
    if args.json:
        _print_json(summary)
    else:
        print(f"Runs: {summary['runs']}; observed wall time: {summary['wall_clock_ms']} ms "
              f"({summary['runs_without_wall_clock']} without timing); "
              f"{summary['runs_without_lead_calls']} without per-call usage")
        for task_type, group in summary["lead_by_task_type"].items():
            print(f"{task_type}: {group['calls']} lead calls; {group['duration_ms']} ms observed; "
                  f"input {group['input_tokens']}, cached {group['cached_tokens']}, "
                  f"output {group['output_tokens']}, total {group['total_tokens']} "
                  f"({group['missing_total_tokens_calls']} calls without total)")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = store.load_schema(REPO_ROOT)
    items = store.load_backlog(REPO_ROOT)
    issues = store.validate_backlog_collection(items, schema)
    if issues:
        print(f"FAIL: {len(issues)} backlog validation error(s):", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    print(f"OK: {len(items)} backlog item(s) valid.")
    return 0


def _cmd_github_status(args: argparse.Namespace) -> int:
    _print_json(github_sync_status(REPO_ROOT))
    return 0


def _cmd_github_sync(args: argparse.Namespace) -> int:
    try:
        _print_json(github_sync_task(args.id, REPO_ROOT))
        return 0
    except (store.BacklogError, gh.GithubSyncError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_github_push(args: argparse.Namespace) -> int:
    try:
        _print_json(github_push_task(args.id, REPO_ROOT, establish_baseline=args.establish_baseline))
        return 0
    except (store.BacklogError, gh.GithubSyncError, SyncConflictError, CloseGuardError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_github_pull(args: argparse.Namespace) -> int:
    try:
        _print_json(github_pull_task(args.issue, REPO_ROOT))
        return 0
    except (store.BacklogError, gh.GithubSyncError, SyncConflictError, UnmappedIssueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_github_pr_sync(args: argparse.Namespace) -> int:
    try:
        _print_json(github_pr_sync_task(args.id, REPO_ROOT))
        return 0
    except (store.BacklogError, gh.GithubSyncError, SyncConflictError, PrLifecycleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_github_pr_link(args: argparse.Namespace) -> int:
    try:
        _print_json(github_pr_link_task(args.id, args.pr, REPO_ROOT))
        return 0
    except (store.BacklogError, gh.GithubSyncError, SyncConflictError, PrLifecycleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backlog.py", description="Deterministic backlog CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List backlog items.")
    p_list.add_argument("--status", default=None, choices=store.STATUSES)
    p_list.add_argument("--version", default=None)
    p_list.set_defaults(func=_cmd_list)

    p_get = sub.add_parser("get", help="Get one backlog item.")
    p_get.add_argument("id")
    p_get.set_defaults(func=_cmd_get)

    p_add = sub.add_parser("add", help="Add a new backlog item.")
    p_add.add_argument("--id", default=None, help="Explicit id; auto-generated if omitted.")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--description", required=True)
    p_add.add_argument("--version", required=True)
    p_add.add_argument("--priority", default="medium", choices=["low", "medium", "high", "critical"])
    p_add.add_argument("--status", default="proposed", choices=store.STATUSES)
    p_add.add_argument("--dependencies", nargs="*", default=None)
    p_add.add_argument("--acceptance-criteria", dest="acceptance_criteria", nargs="*", default=None)
    p_add.add_argument("--labels", nargs="*", default=None)
    p_add.add_argument("--github-issue", dest="github_issue", type=int, default=None)
    p_add.add_argument("--github-pr", dest="github_pr", type=int, default=None)
    p_add.set_defaults(func=_cmd_add)

    p_update = sub.add_parser("update", help="Update an existing backlog item.")
    p_update.add_argument("id")
    p_update.add_argument("--status", default=None, choices=store.STATUSES)
    p_update.add_argument("--title", default=None)
    p_update.add_argument("--description", default=None)
    p_update.add_argument("--priority", default=None, choices=["low", "medium", "high", "critical"])
    p_update.add_argument("--version", default=None)
    p_update.add_argument("--dependencies", nargs="*", default=None)
    p_update.add_argument("--acceptance-criteria", dest="acceptance_criteria", nargs="*", default=None)
    p_update.add_argument("--labels", nargs="*", default=None)
    p_update.add_argument("--blocked-reason", dest="blocked_reason", default=None)
    p_update.add_argument("--github-issue", dest="github_issue", type=int, default=None)
    p_update.add_argument("--github-pr", dest="github_pr", type=int, default=None)
    p_update.set_defaults(func=_cmd_update)

    p_next = sub.add_parser("next", help="List 'ready' items, priority-ranked.")
    p_next.set_defaults(func=_cmd_next)

    p_report = sub.add_parser("report", help="Deterministic backlog + provenance report.")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=_cmd_report)

    p_trace = sub.add_parser(
        "trace",
        help="Render one or more orchestration run traces from provenance/runs/.",
    )
    p_trace.add_argument("run_id", nargs="?", default=None, metavar="RUN_ID")
    p_trace.add_argument("--backlog-id", dest="backlog_id", default=None)
    p_trace.add_argument("--latest", action="store_true", help="Select only the most recent matching run.")
    p_trace.add_argument("--json", action="store_true")
    p_trace.set_defaults(func=_cmd_trace)

    p_usage = sub.add_parser("usage", help="Summarize observed lead token usage by task type across runs.")
    p_usage.add_argument("--json", action="store_true")
    p_usage.set_defaults(func=_cmd_usage)

    p_validate = sub.add_parser("validate", help="Validate backlog/backlog.yaml.")
    p_validate.set_defaults(func=_cmd_validate)

    p_gh_status = sub.add_parser("github-status", help="Check gh availability/auth/repo detection.")
    p_gh_status.set_defaults(func=_cmd_github_status)

    p_gh_sync = sub.add_parser("github-sync", help="Read-only local/remote comparison for one item.")
    p_gh_sync.add_argument("id")
    p_gh_sync.set_defaults(func=_cmd_github_sync)

    p_gh_push = sub.add_parser("github-push", help="Push one backlog item to GitHub (create/update issue).")
    p_gh_push.add_argument("id")
    p_gh_push.add_argument(
        "--establish-baseline",
        dest="establish_baseline",
        action="store_true",
        help=(
            "Required when linking to a pre-existing GitHub issue (no prior sync snapshot) whose "
            "current title/state/labels differ from what would be pushed. Explicitly acknowledges "
            "this deliberate initial link should overwrite the remote issue's fields to match local."
        ),
    )
    p_gh_push.set_defaults(func=_cmd_github_push)

    p_gh_pull = sub.add_parser("github-pull", help="Pull one GitHub issue into its mapped backlog item.")
    p_gh_pull.add_argument("issue", type=int)
    p_gh_pull.set_defaults(func=_cmd_github_pull)

    p_gh_pr_sync = sub.add_parser("github-pr-sync", help="Read-only local/remote comparison for one linked PR.")
    p_gh_pr_sync.add_argument("id")
    p_gh_pr_sync.set_defaults(func=_cmd_github_pr_sync)

    p_gh_pr_link = sub.add_parser("github-pr-link", help="Link an existing PR to a review/completed backlog item.")
    p_gh_pr_link.add_argument("id")
    p_gh_pr_link.add_argument("pr", type=int)
    p_gh_pr_link.set_defaults(func=_cmd_github_pr_link)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
