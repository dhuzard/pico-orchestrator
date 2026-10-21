#!/usr/bin/env python3
"""Parent-owned, target-scoped GitHub integration and reviewed publication.

Only the parent calls this CLI. Workers return proposal JSON; they never receive
an arbitrary gh command or this adapter as a delegated mutation tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_sync as gh
import provenance

ORCHESTRATOR_ROOT = Path(__file__).resolve().parent.parent
OPERATIONS = {
    "create_issue", "update_issue", "comment_issue", "close_issue", "reopen_issue",
    "update_pr", "comment_pr", "create_label", "add_label", "remove_label",
}
DESTRUCTIVE = {"close_issue", "remove_label"}
FIELDS = {
    "create_issue": {"title", "body"},
    "update_issue": {"issue", "title", "body"},
    "comment_issue": {"issue", "body"},
    "close_issue": {"issue"},
    "reopen_issue": {"issue"},
    "update_pr": {"pr", "title", "body"},
    "comment_pr": {"pr", "body"},
    "create_label": {"label", "color", "description"},
    "add_label": {"issue", "label"},
    "remove_label": {"issue", "label"},
}
REQUIRED = {
    "create_issue": {"title"}, "update_issue": {"issue"},
    "comment_issue": {"issue", "body"},
    "close_issue": {"issue"}, "reopen_issue": {"issue"},
    "update_pr": {"pr"}, "comment_pr": {"pr", "body"},
    "create_label": {"label"}, "add_label": {"issue", "label"},
    "remove_label": {"issue", "label"},
}


class IntegrationError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(worktree), *args], shell=False,
                            text=True, capture_output=True, timeout=10)
    if result.returncode:
        raise IntegrationError("TARGET_REPOSITORY_UNRESOLVED", "target worktree is not a readable Git checkout")
    return result.stdout.strip()


def parse_remote(url: str) -> str:
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:"):]
    elif url.startswith("ssh://git@github.com/"):
        path = url[len("ssh://git@github.com/"):]
    else:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise IntegrationError("TARGET_REPOSITORY_UNRESOLVED", "remote is not an unambiguous github.com URL")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    try:
        return gh.validate_repo(path)
    except ValueError as exc:
        raise IntegrationError("TARGET_REPOSITORY_UNRESOLVED", "remote lacks a valid owner/repository") from exc


def resolve_target(worktree: Path, explicit: str | None = None, *, audit: dict | None = None) -> str:
    """Resolve a GitHub identity; an explicit repo must belong to this worktree."""
    if audit is not None:
        audit["discovered_repositories"] = []
        audit["repository_selection"] = {"method": "explicit" if explicit is not None else "inferred",
                                         "requested": None, "resolved": None}
    try:
        worktree = worktree.resolve(strict=True)
    except OSError as exc:
        raise IntegrationError("TARGET_REPOSITORY_UNRESOLVED", "target worktree does not exist") from exc
    _git(worktree, "rev-parse", "--show-toplevel")
    configured = None
    if explicit is not None:
        try:
            configured = gh.validate_repo(explicit)
        except ValueError as exc:
            raise IntegrationError("TARGET_REPOSITORY_UNRESOLVED", "invalid explicit repository") from exc
        if audit is not None:
            audit["repository_selection"]["requested"] = configured
    remotes = _git(worktree, "remote").splitlines()
    identities: dict[str, str] = {}
    for remote in remotes:
        try:
            identity = parse_remote(_git(worktree, "remote", "get-url", remote))
            key = identity.casefold()
            if key not in identities or identity < identities[key]:
                identities[key] = identity
        except IntegrationError:
            # A non-GitHub remote can coexist with one clear GitHub remote.
            continue
    if audit is not None:
        audit["discovered_repositories"] = sorted(identities.values(), key=str.casefold)
    if configured is not None:
        if configured.casefold() not in identities:
            raise IntegrationError("TARGET_REPOSITORY_CONFLICT", "explicit repository is absent from worktree GitHub remotes")
        if audit is not None:
            audit["repository_selection"]["resolved"] = configured
        return configured
    if len(identities) > 1:
        raise IntegrationError("TARGET_REPOSITORY_AMBIGUOUS", "multiple GitHub repositories require an explicit target")
    if not identities:
        raise IntegrationError("TARGET_REPOSITORY_UNRESOLVED", "no GitHub repository remote")
    inferred = next(iter(identities.values()))
    if audit is not None:
        audit["repository_selection"]["resolved"] = inferred
    return inferred


def _text(value: object, field: str, *, nonempty: bool = True, maximum: int = 65536) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()) or len(value) > maximum or "\x00" in value:
        raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", f"invalid {field}")
    return value


def validate_proposals(raw: object, repository: str) -> list[dict]:
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "expected 1 to 100 proposals")
    proposals = []
    for item in raw:
        if not isinstance(item, dict) or item.get("operation") not in OPERATIONS:
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "unsupported operation")
        op = item["operation"]
        allowed = {"operation", "repository", "rationale"} | FIELDS[op]
        if set(item) - allowed or not REQUIRED[op] <= set(item) or not {"operation", "repository", "rationale"} <= set(item):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "unexpected or missing proposal fields")
        if item["repository"] != repository:
            raise IntegrationError("TARGET_REPOSITORY_CONFLICT", "proposal repository does not match resolved target")
        _text(item["rationale"], "rationale", maximum=1000)
        if "issue" in item and (type(item["issue"]) is not int or item["issue"] < 1):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "issue must be a positive integer")
        if "pr" in item and (type(item["pr"]) is not int or item["pr"] < 1):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "pr must be a positive integer")
        for field in ("title", "body", "label", "description"):
            if field in item:
                nonempty = field not in ("body", "description") or op in ("comment_issue", "comment_pr")
                _text(item[field], field, nonempty=nonempty,
                      maximum=256 if field in ("title", "label") else 65536)
        if "label" in item and (item["label"].startswith("-") or re.search(r"[\x00-\x1f\x7f]", item["label"])):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "invalid label")
        if op == "update_issue" and not ({"title", "body"} & set(item)):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "update requires title or body")
        if op == "update_pr" and not ({"title", "body"} & set(item)):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "PR update requires title or body")
        if "color" in item and (not isinstance(item["color"], str) or not re.fullmatch(r"[0-9a-fA-F]{6}", item["color"])):
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "invalid label color")
        proposals.append(dict(item))
    return proposals


def _labels(issue: dict) -> set[str]:
    return {x["name"] for x in issue["labels"] if isinstance(x, dict) and isinstance(x.get("name"), str)}


def _comment_present(resource: dict, body: str) -> bool:
    comments = resource.get("comments")
    if not isinstance(comments, list):
        raise gh.GhOutputError("GitHub read omitted required comments")
    return any(isinstance(comment, dict) and comment.get("body") == body for comment in comments)


def _state(proposal: dict, repo: str, adapter=gh) -> object:
    op = proposal["operation"]
    if op == "create_label":
        matches = [x for x in adapter.list_labels(repo=repo) if x["name"].casefold() == proposal["label"].casefold()]
        return matches[0] if matches else None
    if op == "create_issue":
        issues = adapter.list_issues(state="all", limit=1000, repo=repo)
        match = next((x for x in issues if x["title"] == proposal["title"]), None)
        return adapter.view_issue(match["number"], repo=repo) if match else None
    if op in ("update_pr", "comment_pr"):
        pr = adapter.view_pr(proposal["pr"], repo=repo)
        if op == "update_pr":
            if any(k not in pr for k in ("title", "body") if k in proposal):
                raise gh.GhOutputError("PR read omitted required content")
            return {k: pr[k] for k in ("title", "body") if k in proposal}
        return {"state": str(pr["state"]).upper(), "comment_present": _comment_present(pr, proposal["body"])}
    issue = adapter.view_issue(proposal["issue"], repo=repo)
    if op == "update_issue":
        if any(k not in issue for k in ("title", "body") if k in proposal):
            raise gh.GhOutputError("issue read omitted required content")
        return {k: issue[k] for k in ("title", "body") if k in proposal}
    if op == "comment_issue":
        return {"state": str(issue["state"]).upper(), "comment_present": _comment_present(issue, proposal["body"])}
    if op in ("close_issue", "reopen_issue"):
        return {**issue, "state": issue["state"].upper()}
    return proposal["label"] in _labels(issue)


def _after(proposal: dict, before: object) -> object:
    op = proposal["operation"]
    if op == "create_issue":
        return {"title": proposal["title"], "body": proposal.get("body", "")}
    if op == "create_label":
        return {"name": proposal["label"], "color": proposal.get("color", "ededed"), "description": proposal.get("description", "")}
    if op in ("update_issue", "update_pr"):
        return {**before, **{k: proposal[k] for k in ("title", "body") if k in proposal}}
    if op in ("comment_issue", "comment_pr"):
        return {**before, "comment_present": True}
    if op in ("close_issue", "reopen_issue"):
        return {**before, "state": "CLOSED" if op == "close_issue" else "OPEN"}
    return op == "add_label"


def build_plan(proposals: list[dict], repo: str, adapter=gh) -> list[dict]:
    plan = []
    for p in proposals:
        before = _state(p, repo, adapter)
        after = _after(p, before)
        noop = before == after or (p["operation"] == "create_label" and before is not None)
        if p["operation"] == "create_issue" and before is not None:
            if before.get("body") != after["body"]:
                raise IntegrationError("GITHUB_STATE_CONFLICT", "existing issue title has different body")
            noop = True
        if p["operation"] == "create_label" and before is not None and (
            before.get("color", "").casefold() != after["color"].casefold() or before.get("description", "") != after["description"]):
            raise IntegrationError("GITHUB_STATE_CONFLICT", "existing label differs from proposed definition")
        plan.append({"repository": repo, "operation": p["operation"], "issue": p.get("issue"),
                     "pr": p.get("pr"), "label": p.get("label"), "before": before, "after": after,
                     "rationale": p["rationale"], "destructive": p["operation"] in DESTRUCTIVE,
                     "reversible": p["operation"] not in ("create_issue", "create_label", "comment_issue", "comment_pr"),
                     "noop": noop})
    return plan


def plan_hash(plan: list[dict]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _apply_one(p: dict, repo: str, adapter=gh) -> None:
    op = p["operation"]
    if op == "create_issue":
        adapter.create_issue(p["title"], p.get("body", ""), repo=repo)
    elif op == "update_issue":
        adapter.edit_issue(p["issue"], title=p.get("title"), body=p.get("body"), repo=repo)
    elif op == "comment_issue":
        adapter.comment_issue(p["issue"], p["body"], repo=repo)
    elif op == "update_pr":
        adapter.edit_pr(p["pr"], title=p.get("title"), body=p.get("body"), repo=repo)
    elif op == "comment_pr":
        adapter.comment_pr(p["pr"], p["body"], repo=repo)
    elif op == "close_issue":
        adapter.close_issue(p["issue"], repo=repo)
    elif op == "reopen_issue":
        adapter.reopen_issue(p["issue"], repo=repo)
    elif op == "create_label":
        adapter.create_label(p["label"], color=p.get("color", "ededed"), description=p.get("description", ""), repo=repo)
    elif op == "add_label":
        adapter.edit_issue(p["issue"], add_labels=[p["label"]], repo=repo)
    elif op == "remove_label":
        adapter.edit_issue(p["issue"], remove_labels=[p["label"]], repo=repo)


def execute(*, worktree: Path, proposal_file: Path, explicit_repo: str | None = None,
            apply: bool = False, allow_destructive: bool = False, phase: str | None = None,
            route: str | None = None, worker: str | None = None, model: str | None = None,
            expected_plan_hash: str | None = None,
            run_id: str | None = None, adapter=gh, provenance_root: Path = ORCHESTRATOR_ROOT) -> dict:
    """Resolve, validate, plan, authorize and execute. Every outcome gets a run record."""
    path_text = str(worktree.resolve())
    if provenance._contains_secret_like_content(path_text) or "\n" in path_text:
        path_text = "[redacted]"
    safe_phase = phase if phase is None or (re.fullmatch(r"[A-Za-z0-9 ._-]{1,100}", phase) and
                                           not provenance._contains_secret_like_content(phase)) else None
    safe_route = route if route in (None, "CODEX", "CLAUDE", "CLAUDE_CODEX", "CODEX_CLAUDE_CODEX") else None
    safe_worker = worker if worker is None or (re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", worker) and
                                                not provenance._contains_secret_like_content(worker)) else None
    safe_model = model if model is None or (re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", model) and
                                             not provenance._contains_secret_like_content(model)) else None
    policy_text = (ORCHESTRATOR_ROOT / "routing/default-policy.yaml").read_text(encoding="utf-8")
    policy_match = re.search(r"(?m)^version: ([0-9]+)$", policy_text)
    policy_version = int(policy_match.group(1)) if policy_match else 5
    meta = {"target_repository": None, "target_worktree": path_text, "phase": safe_phase,
            "route": safe_route, "worker": safe_worker, "model": safe_model, "policy_version": policy_version,
            "discovered_repositories": [],
            "repository_selection": {"method": "explicit" if explicit_repo is not None else "inferred",
                                     "requested": None, "resolved": None},
            "blocker_code": None, "reason": None, "mutations_planned": 0,
            "mutations_applied": 0, "qa_status": "not_run", "audit": []}
    record = provenance.start_run(provenance_root, run_id=run_id, backlog_id=None,
                                  github_issue=None, user_objective="GitHub backlog mutation batch",
                                  route_task_result=None, starting_commit=None, github_integration=meta)
    plan = []
    mutating = False
    try:
        repo = resolve_target(worktree, explicit_repo, audit=meta)
        meta["target_repository"] = repo
        raw = json.loads(proposal_file.read_text(encoding="utf-8"))
        proposals = validate_proposals(raw, repo)
        plan = build_plan(proposals, repo, adapter)
        meta["mutations_planned"] = sum(not x["noop"] for x in plan)
        if not apply:
            outcome = "dry_run"
        else:
            if not isinstance(expected_plan_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_hash):
                raise IntegrationError("GITHUB_MUTATION_NOT_AUTHORIZED", "apply requires a parent-reviewed dry-run plan hash")
            if expected_plan_hash != plan_hash(plan):
                raise IntegrationError("GITHUB_STATE_CONFLICT", "GitHub plan changed since dry-run")
            if any(x["destructive"] and not x["noop"] for x in plan) and not allow_destructive:
                raise IntegrationError("GITHUB_MUTATION_NOT_AUTHORIZED", "destructive operation requires parent authorization")
            for p, item in zip(proposals, plan, strict=True):
                if item["noop"]:
                    status = "noop"
                else:
                    if _state(p, repo, adapter) != item["before"]:
                        raise IntegrationError("GITHUB_STATE_CONFLICT", "GitHub state changed since planning")
                    mutating = True
                    _apply_one(p, repo, adapter)
                    mutating = False
                    meta["mutations_applied"] += 1
                    status = "applied"
                meta["audit"].append({"operation": p["operation"], "issue": p.get("issue"), "pr": p.get("pr"),
                                      "label": (p.get("label") if p.get("label") and
                                                not provenance._contains_secret_like_content(p["label"]) else None),
                                      "status": status})
                # Persist each applied mutation, including partial batches.
                provenance.update_github_integration(provenance_root, record["run_id"], meta)
            outcome = "completed"
    except IntegrationError as exc:
        meta["blocker_code"], meta["reason"] = exc.code, exc.detail
        outcome = "partial" if meta["mutations_applied"] else "blocked"
    except (gh.GhUnavailableError, gh.GhAuthError):
        meta["blocker_code"], meta["reason"] = "GITHUB_AUTH_UNAVAILABLE", "GitHub CLI or authentication unavailable"
        outcome = "partial" if meta["mutations_applied"] else "blocked"
    except (gh.GithubSyncError, ValueError):
        meta["blocker_code"], meta["reason"] = ("GITHUB_MUTATION_FAILED" if mutating else "GITHUB_READ_FAILED"), "GitHub operation failed"
        outcome = "partial" if meta["mutations_applied"] else "failed"
    except (OSError, json.JSONDecodeError, UnicodeError):
        meta["blocker_code"], meta["reason"] = "GITHUB_MUTATION_VALIDATION_FAILED", "proposal file is unreadable or invalid JSON"
        outcome = "blocked"
    except (KeyError, TypeError, subprocess.TimeoutExpired):
        meta["blocker_code"], meta["reason"] = "GITHUB_READ_FAILED", "invalid or unavailable GitHub state"
        outcome = "partial" if meta["mutations_applied"] else "failed"
    provenance.finish_run(provenance_root, record["run_id"], outcome=outcome, github_integration=meta)
    return {"run_id": record["run_id"], "status": outcome.upper(), "target_repository": meta["target_repository"],
            "blocker_code": meta["blocker_code"], "plan": plan, "plan_hash": plan_hash(plan) if plan else None,
            "mutations_applied": meta["mutations_applied"]}


def read_backlog(*, worktree: Path, explicit_repo: str | None = None, issue: int | None = None,
                 adapter=gh, resolution_audit: dict | None = None) -> dict:
    """Parent reads target backlog and passes this data to analysis workers."""
    repo = resolve_target(worktree, explicit_repo, audit=resolution_audit)
    if issue is not None:
        if type(issue) is not int or issue < 1:
            raise IntegrationError("GITHUB_MUTATION_VALIDATION_FAILED", "issue must be positive")
        return {"repository": repo, "issue": adapter.view_issue(issue, repo=repo)}
    return {"repository": repo, "issues": adapter.list_issues(state="all", limit=1000, repo=repo),
            "labels": adapter.list_labels(repo=repo)}


def read_with_provenance(*, worktree: Path, explicit_repo: str | None = None,
                         issue: int | None = None, adapter=gh,
                         provenance_root: Path = ORCHESTRATOR_ROOT) -> dict:
    """Read requests, including failed ones, are orchestration outcomes."""
    path_text = str(worktree.resolve())
    if provenance._contains_secret_like_content(path_text) or "\n" in path_text:
        path_text = "[redacted]"
    meta = {"target_repository": None, "target_worktree": path_text, "phase": None,
            "discovered_repositories": [],
            "repository_selection": {"method": "explicit" if explicit_repo is not None else "inferred",
                                     "requested": None, "resolved": None},
            "blocker_code": None, "reason": None, "mutations_planned": 0,
            "mutations_applied": 0, "qa_status": "not_run", "audit": []}
    record = provenance.start_run(provenance_root, backlog_id=None,
                                  github_issue=issue if type(issue) is int and issue > 0 else None,
                                  user_objective="Read GitHub backlog", route_task_result=None,
                                  starting_commit=None, github_integration=meta)
    data = None
    try:
        data = read_backlog(worktree=worktree, explicit_repo=explicit_repo, issue=issue,
                            adapter=adapter, resolution_audit=meta)
        meta["target_repository"] = data["repository"]
        outcome = "completed"
    except IntegrationError as exc:
        meta["blocker_code"], meta["reason"] = exc.code, exc.detail
        outcome = "blocked"
    except (gh.GhUnavailableError, gh.GhAuthError):
        meta["blocker_code"], meta["reason"] = "GITHUB_AUTH_UNAVAILABLE", "GitHub CLI or authentication unavailable"
        outcome = "blocked"
    except (gh.GithubSyncError, KeyError, TypeError, subprocess.TimeoutExpired):
        meta["blocker_code"], meta["reason"] = "GITHUB_READ_FAILED", "GitHub read failed"
        outcome = "failed"
    provenance.finish_run(provenance_root, record["run_id"], outcome=outcome, github_integration=meta)
    return {"run_id": record["run_id"], "status": outcome.upper(), "blocker_code": meta["blocker_code"],
            "target_repository": meta["target_repository"], "data": data}


ONE_SHOT_PERMISSIONS = ("push_branch", "create_pr")


def _publication_authority(policy: dict | None, grant: dict | None = None) -> dict:
    """Authorize publication from the base-branch autonomy policy, or from a one-shot grant.

    `policy` is the schema-valid `.pi/autonomy.yaml` GitHub reports at the
    verified base commit (`_remote_policy`), or None when the base has none.
    The working tree is never consulted: a worker can edit, commit or
    symlink its own copy. A grant is passed in process only (never from the
    command line) by a caller that has shown the exact reviewed plan and
    obtained an explicit confirmation for it, such as the Garden. It covers
    only the permissions the base policy withholds for this single
    publication; an unreadable or invalid base policy is never overridden.
    """
    permissions = (policy or {}).get("permissions") or {}
    evaluated = {name: permissions.get(name) is True for name in ONE_SHOT_PERMISSIONS}
    missing = [name for name in ONE_SHOT_PERMISSIONS if not evaluated[name]]
    if not missing:
        return {"source": "policy", "granted_by": None, "granted": [], "permissions": evaluated}
    if not (isinstance(grant, dict) and grant.get("one_shot") is True
            and isinstance(grant.get("granted_by"), str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", grant["granted_by"])):
        detail = ".pi/autonomy.yaml on the base branch on GitHub is required for publication" if policy is None \
            else "push_branch and create_pr must be granted by .pi/autonomy.yaml on the base branch on GitHub"
        raise IntegrationError("PUBLICATION_NOT_AUTHORIZED", detail)
    return {"source": "one_shot_grant", "granted_by": grant["granted_by"], "granted": missing,
            "permissions": evaluated}


def _policy_drift(worktree: Path, policy: dict | None) -> list[str]:
    """Permissions the checkout's own `.pi/autonomy.yaml` sets differently from the base policy.

    Informational only (see docs): it never grants anything and is not part of
    the plan. Unreadable, symlinked or absent copies report nothing.
    """
    import autonomy_policy
    path = autonomy_policy.policy_path(worktree)
    try:
        if path.is_symlink() or not path.is_file():
            return []
        local = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(local, dict) or not isinstance(local.get("permissions"), dict):
        return []
    base = (policy or {}).get("permissions") or {}
    return sorted(name for name in local["permissions"]
                  if local["permissions"].get(name) != base.get(name, False))


# GitHub closes an issue when a PR body uses one of these keywords before an
# issue reference (`Closes #3`, `fixes: owner/repo#3`, `Fixes:#3`,
# `resolves https://github.com/owner/repo/issues/3`), anywhere in the body.
# `\s` is Unicode-aware, so a non-breaking space counts as a separator. The
# match is deliberately not code- or comment-aware: GitHub ignores keywords in
# code spans and HTML comments, but stripping them here would have to mirror
# GitHub's Markdown parser exactly, and any mismatch would let a keyword
# through. The gate therefore refuses such keywords there too.
_CLOSING_REFERENCE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s*"
    r"(?:(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>[0-9]+)"
    r"|https?://(?:www\.)?github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_number>[0-9]+))")
_POLICY_FILE = ".pi/autonomy.yaml"


def _closing_targets(body: str, repository: str) -> set[tuple[str, int]]:
    """Every issue a body would close on merge, as (casefolded owner/repo, number)."""
    targets = set()
    for match in _CLOSING_REFERENCE.finditer(body):
        repo = match["repo"] or match["url_repo"] or repository
        targets.add((repo.casefold(), int(match["number"] or match["url_number"])))
    return targets


def _git_top(worktree: Path) -> Path:
    import autonomy_policy
    return autonomy_policy.policy_path(worktree).parent.parent


def _remote_policy(repo: str, base_commit: str | None, adapter=gh) -> tuple[dict | None, str | None]:
    """The validated `.pi/autonomy.yaml` at `base_commit` on GitHub, and its blob SHA; (None, None) when absent.

    Both the base commit (`_remote_base_commit`) and the policy are read
    through the authenticated GitHub API adapter, never through local git:
    every worktree shares the repository's refs and git config (`refs/heads`,
    `refs/remotes`, `core.sshCommand`, `remote.<name>.uploadpack`,
    `include.path`, `git replace`), so anything local git reports about the
    base branch can be forged by a worker. This one read feeds both the
    publication permissions and `issue_reference`.
    """
    import autonomy_policy
    if base_commit is None:
        return None, None
    entry = adapter.policy_file_at(base_commit, repo=repo)
    if entry is None:
        return None, None
    if entry.get("type") == "symlink" or entry.get("path") != _POLICY_FILE:
        raise IntegrationError("AUTONOMY_POLICY_PATH_UNSAFE", "the committed .pi/autonomy.yaml is a symlink")
    if entry.get("type") != "file" or not isinstance(entry.get("text"), str):
        raise IntegrationError("AUTONOMY_POLICY_INVALID", "the committed publication policy is not a readable file")
    try:
        data = yaml.safe_load(entry["text"])
    except yaml.YAMLError as exc:
        raise IntegrationError("AUTONOMY_POLICY_INVALID", "the committed publication policy is unreadable") from exc
    issues = autonomy_policy.validate(data)
    if issues:
        raise IntegrationError("AUTONOMY_POLICY_INVALID", f"committed policy: {issues[0]}"[:300])
    return data, entry["sha"]


def _issue_reference(policy: dict | None) -> str:
    import autonomy_policy
    return (policy or {}).get("issue_reference", autonomy_policy.DEFAULTS["issue_reference"])


def remote_issue_reference(repo: str, base_commit: str | None, adapter=gh) -> tuple[str, str | None]:
    """`issue_reference` in `.pi/autonomy.yaml` at `base_commit` on GitHub, and that policy blob's SHA.

    Without a base branch on GitHub or a policy at that commit the answer is
    the safe default `refs` with no blob.
    """
    policy, blob = _remote_policy(repo, base_commit, adapter)
    return _issue_reference(policy), blob


def _require_clean_policy_checkout(worktree: Path) -> None:
    """Refuse a symlinked or uncommitted-edited `.pi/autonomy.yaml`: a tampered checkout is never published from."""
    top = _git_top(worktree)
    unsafe = "AUTONOMY_POLICY_PATH_UNSAFE"
    if (top / ".pi").is_symlink() or (top / _POLICY_FILE).is_symlink():
        raise IntegrationError(unsafe, ".pi or .pi/autonomy.yaml is a symlink")
    proc = subprocess.run(["git", "-C", str(top), "status", "--porcelain", "--", _POLICY_FILE], shell=False,
                          capture_output=True, text=True, timeout=10)
    if proc.returncode or proc.stdout.strip():
        raise IntegrationError(unsafe, ".pi/autonomy.yaml has uncommitted changes; commit it on the base "
                                       "branch before publishing")


def _check_issue_reference(body: str, *, repository: str, policy_closes, closes_issue: int | None) -> dict:
    """Require a `Refs #N` (or, when closing is authorized, `Closes #N`) line and check every closing target.

    `closes_issue` is a caller's explicit opt-in for exactly that issue (a
    workspace task with `closes_issue: true`). Otherwise `policy_closes` (a
    callable, so the policy is consulted only when the body would close
    something) says whether the committed policy is `closes`; then each issue
    the body closes must be declared on its own `Closes #N` line, in this
    repository. Any other closing target is refused.
    """
    targets = _closing_targets(body, repository)
    declared = {int(n) for n in re.findall(r"(?m)^Closes #([1-9][0-9]*)\b", body)}
    allowed: set[int] = set()
    if targets:
        if closes_issue is not None:
            allowed = {closes_issue}
        elif policy_closes():
            allowed = declared
        if not allowed:
            raise IntegrationError("PUBLICATION_BODY_INVALID",
                                   "PR body uses a closing keyword (Closes/Fixes/Resolves #N) but neither the "
                                   "repository's committed issue_reference policy (closes) nor the caller allows "
                                   "closing issues; reference the issue with Refs #N")
        stray = sorted(f"{repo}#{number}" for repo, number in targets
                       if repo != repository.casefold() or number not in allowed)
        if stray:
            raise IntegrationError("PUBLICATION_BODY_INVALID",
                                   f"PR body would close {', '.join(stray)[:120]}, which this publication may not "
                                   "close; each closed issue needs its own Closes #N line in this repository")
    line = r"(?m)^(?:Refs|Closes) #[1-9][0-9]*\b" if targets else r"(?m)^Refs #[1-9][0-9]*\b"
    if not re.search(line, body):
        raise IntegrationError("PUBLICATION_BODY_INVALID",
                               "PR body must reference an issue with Refs #N or Closes #N" if targets
                               else "PR body must reference an issue with Refs #N")
    return {"issue_reference": "closes" if targets else "refs",
            "closes_issues": sorted(number for _, number in targets)}


def _branch(worktree: Path, name: str, *, head: bool) -> str:
    if not isinstance(name, str) or not name or name.startswith("-") or "\n" in name:
        raise IntegrationError("PUBLICATION_REF_INVALID", "invalid branch name")
    if head and not name.startswith("pico/"):
        raise IntegrationError("PUBLICATION_REF_INVALID", "head branch must be under pico/")
    proc = subprocess.run(["git", "-C", str(worktree), "check-ref-format", "--branch", name],
                          capture_output=True, text=True, timeout=10)
    if proc.returncode:
        raise IntegrationError("PUBLICATION_REF_INVALID", "invalid branch name")
    return name


def _publication_remote(worktree: Path, repo: str) -> str:
    """The remote whose fetch and push URLs both resolve to the target repository."""
    remotes = _git(worktree, "remote").splitlines()
    candidates = []
    for remote in remotes:
        try:
            fetch_url = _git(worktree, "remote", "get-url", remote)
            push_url = _git(worktree, "remote", "get-url", "--push", remote)
            if parse_remote(fetch_url).casefold() == repo.casefold() and parse_remote(push_url).casefold() == repo.casefold():
                candidates.append(remote)
        except IntegrationError:
            continue
    if not candidates:
        raise IntegrationError("PUBLICATION_REMOTE_MISMATCH", "no matching GitHub push remote")
    return "origin" if "origin" in candidates else sorted(candidates)[0]


def _remote_head_commit(worktree: Path, remote: str, branch: str) -> str | None:
    """The commit the pico/ head branch points to on `remote`, or None when it is not pushed yet."""
    proc = subprocess.run(["git", "-C", str(worktree), "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise IntegrationError("PUBLICATION_REMOTE_UNAVAILABLE", "could not inspect the remote branch")
    if not proc.stdout.strip():
        return None
    parts = proc.stdout.strip().split("\t")
    if len(parts) != 2 or parts[1] != f"refs/heads/{branch}" or not re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
        raise IntegrationError("PUBLICATION_REMOTE_UNAVAILABLE", "unexpected remote branch response")
    return parts[0]


def _remote_base_commit(repo: str, base: str, adapter=gh) -> str | None:
    """The base branch's commit as the GitHub API reports it, or None when GitHub has no such branch."""
    try:
        gh.validate_branch(base)
    except ValueError as exc:
        raise IntegrationError("PUBLICATION_REF_INVALID",
                               "base branch must be [A-Za-z0-9._-] segments separated by '/'") from exc
    return adapter.branch_head(base, repo=repo)


def verified_issue_reference(repo: str, base: str, adapter=gh) -> tuple[str, str | None, str | None]:
    """`issue_reference` at the base commit GitHub reports: (value, policy blob, base commit)."""
    base_commit = _remote_base_commit(repo, base, adapter)
    return (*remote_issue_reference(repo, base_commit, adapter), base_commit)


def _publication_state(worktree: Path, repo: str, base: str, head: str, adapter=gh,
                       base_commit: str | None = None) -> dict:
    remote = _publication_remote(worktree, repo)
    commit = _git(worktree, "rev-parse", "--verify", f"refs/heads/{head}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise IntegrationError("PUBLICATION_REF_INVALID", "head is not a local commit")
    remote_commit = _remote_head_commit(worktree, remote, head)
    prs = adapter.list_prs(base=base, head=head, repo=repo)
    open_prs = [pr for pr in prs if pr["state"].lower() == "open"]
    if len(open_prs) > 1:
        raise IntegrationError("PUBLICATION_PR_CONFLICT", "multiple open PRs match this branch")
    if not open_prs and prs:
        raise IntegrationError("PUBLICATION_PR_CONFLICT", "this branch already has a closed or merged PR")
    return {"remote": remote, "commit": commit, "remote_commit": remote_commit, "base_commit": base_commit,
            "existing_pr": open_prs[0]["number"] if open_prs else None}


def publish(*, worktree: Path, base: str, head: str, title: str, body_file: Path,
            explicit_repo: str | None = None, apply: bool = False,
            expected_plan_hash: str | None = None, adapter=gh,
            provenance_root: Path = ORCHESTRATOR_ROOT, grant: dict | None = None,
            closes_issue: int | None = None) -> dict:
    """Dry-run and publish one reviewed pico branch through a fixed, non-force path.

    The body must reference its issue with `Refs #N`. A closing keyword is
    accepted only for an issue of this repository that either an in-process
    caller names in `closes_issue` (a workspace task with `closes_issue:
    true`), or that has its own `Closes #N` line while `.pi/autonomy.yaml` at
    the base commit GitHub reports sets `issue_reference: closes`. The same
    single policy read supplies `push_branch`/`create_pr`. A symlinked or
    uncommitted-edited checkout copy is refused. Base commit and policy
    are read through the GitHub API adapter, not local git. The plan records
    the base commit, policy blob and evaluated permissions, so the plan hash binds them; a merge to the base between the dry
    run and the apply therefore gives GITHUB_STATE_CONFLICT.
    """
    meta = {"target_repository": None, "target_worktree": str(worktree.resolve()), "phase": "publication",
            "blocker_code": None, "reason": None, "mutations_planned": 0,
            "mutations_applied": 0, "qa_status": "not_run", "audit": []}
    record = provenance.start_run(provenance_root, backlog_id=None, github_issue=None,
                                  user_objective="Publish reviewed pico branch", route_task_result=None,
                                  starting_commit=None, github_integration=meta)
    plan = None
    drift: list[str] = []
    outcome = "blocked"
    try:
        repo = resolve_target(worktree, explicit_repo)
        meta["target_repository"] = repo
        # One API read of the base commit and one of the policy blob: they authorize push/PR and decide
        # issue_reference. The checkout's own copy is never trusted, so a symlinked or uncommitted-edited
        # one is refused, and nothing else is inspected before authorization.
        _require_clean_policy_checkout(worktree)
        base = _branch(worktree, base, head=False)
        base_commit = _remote_base_commit(repo, base, adapter)
        policy, policy_blob = _remote_policy(repo, base_commit, adapter)
        authorization = _publication_authority(policy, grant)
        meta["authorization"] = {k: v for k, v in authorization.items() if k != "permissions"}
        drift = _policy_drift(worktree, policy)
        head = _branch(worktree, head, head=True)
        if base == head:
            raise IntegrationError("PUBLICATION_REF_INVALID", "head and base must differ")
        title = _text(title, "title", maximum=256)
        body = _text(body_file.read_text(encoding="utf-8"), "body")
        if closes_issue is not None and (type(closes_issue) is not int or closes_issue < 1):
            raise IntegrationError("PUBLICATION_BODY_INVALID", "closes_issue must be an issue number")
        state = _publication_state(worktree, repo, base, head, adapter, base_commit)

        def policy_closes() -> bool:
            return _issue_reference(policy) == "closes"
        reference = _check_issue_reference(body, repository=repo, policy_closes=policy_closes,
                                           closes_issue=closes_issue)
        plan = {"repository": repo, "base": base, "head": head, **state,
                "title": title, "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "draft": True,
                "authorization": authorization["source"], **reference, "policy_blob": policy_blob,
                "publication_permissions": authorization["permissions"]}
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        meta["mutations_planned"] = int(state["remote_commit"] != state["commit"]) + int(state["existing_pr"] is None)
        if not apply:
            outcome = "dry_run"
        else:
            if expected_plan_hash != digest:
                raise IntegrationError("GITHUB_STATE_CONFLICT", "publication plan changed since dry-run")
            if state["remote_commit"] != state["commit"]:
                proc = subprocess.run(["git", "-C", str(worktree), "push", "--porcelain", state["remote"],
                                       f"{state['commit']}:refs/heads/{head}"], capture_output=True, text=True, timeout=120)
                if proc.returncode:
                    raise IntegrationError("PUBLICATION_PUSH_FAILED", "non-force branch push failed")
                meta["mutations_applied"] += 1
                meta["audit"].append({"operation": "push_branch", "issue": None, "pr": None,
                                      "label": None, "status": "applied"})
                provenance.update_github_integration(provenance_root, record["run_id"], meta)
            if state["existing_pr"] is None:
                created = adapter.create_pr(title, body, base=base, head=head, repo=repo, draft=True)
                pr_number = created["number"]
                meta["mutations_applied"] += 1
                meta["audit"].append({"operation": "create_draft_pr", "issue": None, "pr": pr_number,
                                      "label": None, "status": "applied"})
                provenance.update_github_integration(provenance_root, record["run_id"], meta)
            else:
                pr_number = state["existing_pr"]
            outcome = "completed"
    except IntegrationError as exc:
        meta["blocker_code"], meta["reason"] = exc.code, exc.detail
        outcome = "partial" if meta["mutations_applied"] else "blocked"
    except (gh.GithubSyncError, OSError, UnicodeDecodeError, ValueError, subprocess.TimeoutExpired) as exc:
        meta["blocker_code"] = "PUBLICATION_FAILED"
        meta["reason"] = type(exc).__name__
        outcome = "partial" if meta["mutations_applied"] else "blocked"
    provenance.finish_run(provenance_root, record["run_id"], outcome=outcome, github_integration=meta)
    return {"run_id": record["run_id"], "status": outcome.upper(), "blocker_code": meta["blocker_code"],
            "plan": plan, "plan_hash": digest if plan else None,
            "policy_drift": drift,
            "pr": pr_number if outcome == "completed" else None,
            "mutations_applied": meta["mutations_applied"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--repo", help="explicit owner/repository; checked against worktree remotes")
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--read", action="store_true", help="read issue and label state for parent analysis")
    parser.add_argument("--publish", action="store_true", help="dry-run or publish one reviewed pico branch")
    parser.add_argument("--base", help="publication base branch")
    parser.add_argument("--head", help="local pico/ branch to publish")
    parser.add_argument("--title", help="draft PR title")
    parser.add_argument("--body-file", type=Path, help="draft PR body file, including Refs #N (Closes #N when "
                        ".pi/autonomy.yaml sets issue_reference: closes)")
    parser.add_argument("--issue", type=int, help="inspect one issue with --read")
    parser.add_argument("--apply", action="store_true", help="parent authorizes this batch")
    parser.add_argument("--plan-hash", help="SHA-256 plan_hash returned by the reviewed dry-run")
    parser.add_argument("--allow-destructive", action="store_true", help="parent authorizes close/remove operations")
    parser.add_argument("--phase")
    parser.add_argument("--route", help="observed route from route_task")
    parser.add_argument("--worker", help="observed worker identifier")
    parser.add_argument("--model", help="observed model identifier")
    args = parser.parse_args()
    if args.publish:
        if args.read or args.proposals or args.issue or args.allow_destructive:
            parser.error("--publish cannot be combined with issue mutation or read options")
        if not all((args.base, args.head, args.title, args.body_file)):
            parser.error("--publish requires --base, --head, --title, and --body-file")
        result = publish(worktree=args.worktree, base=args.base, head=args.head, title=args.title,
                         body_file=args.body_file, explicit_repo=args.repo, apply=args.apply,
                         expected_plan_hash=args.plan_hash)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] in ("COMPLETED", "DRY_RUN") else 1
    if args.read:
        if args.proposals or args.apply or args.allow_destructive:
            parser.error("--read cannot be combined with mutation options")
        result = read_with_provenance(worktree=args.worktree, explicit_repo=args.repo, issue=args.issue)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "COMPLETED" else 1
    if args.proposals is None:
        parser.error("--proposals is required unless --read is used")
    result = execute(worktree=args.worktree, proposal_file=args.proposals, explicit_repo=args.repo,
                     apply=args.apply, allow_destructive=args.allow_destructive, phase=args.phase,
                     route=args.route, worker=args.worker, model=args.model,
                     expected_plan_hash=args.plan_hash)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in ("COMPLETED", "DRY_RUN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
