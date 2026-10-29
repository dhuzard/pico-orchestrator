#!/usr/bin/env python3
"""Mutation authorization of workspace tasks, checked before any child run spends tokens.

An implementation task makes repository-local changes: its run creates
worktrees and commits on a `pico/` branch that `pico workspace validate`
checks later. Both need `.pi/autonomy.yaml` to grant `create_worktrees` and
`commit`. A task declared `mutation: none` (read-only) and a qualification
task need nothing.

The authorizing policy is the one committed at the task repository's pinned
commit, read from Git objects, never from the working tree: the pin is what
the child run checks out, so an untracked or edited copy cannot authorize
anything until a human commits it on the base branch and re-pins the
manifest. A committed policy that grants mutation still needs the operator's
explicit `--allow-mutation`, as for any headless run.

Verdicts are deterministic and read-only. `fingerprint` identifies the
authorization state a child was launched under, so a run that stopped to ask
for approval is retried as a new attempt only after that state changed
(never in a loop).

Push, PR, mark-ready, merge and deploy are never part of this check, and the
bounded change offered for approval never grants them: publication stays
governed by the policy on the default branch on GitHub.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autonomy_policy  # noqa: E402

REQUIRED = ("create_worktrees", "commit")
NEVER_GRANTED = ("push_branch", "create_pr", "mark_pr_ready", "merge", "deploy")
PERMISSION_FLAGS = ("create_backlog_items", "create_github_issues", "create_worktrees", "commit", "push_branch",
                    "create_pr", "comment_on_issue", "comment_on_pr", "mark_pr_ready", "deploy")
REQUIRED_CODE = "MUTATION_AUTHORIZATION_REQUIRED"
FLAG_CODE = "MUTATION_NOT_AUTHORIZED"  # headless_run's refusal: the policy grants, --allow-mutation was not passed
# Blocker codes a run (or a refused launch) reports when it stopped for mutation approval. The
# second is the free-text code leads chose before this check existed; it is matched only together
# with proof that the run changed nothing (see stopped_for_authorization).
CODES = frozenset({REQUIRED_CODE, FLAG_CODE, "HUMAN_MUTATION_APPROVAL_REQUIRED"})
POLICY_CODES = frozenset({"AUTONOMY_POLICY_INVALID", "AUTONOMY_POLICY_PATH_UNSAFE"})
AWAITING_CODES = CODES | POLICY_CODES


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(checkout), *args], shell=False, capture_output=True, timeout=10)


def committed_policy(checkout: Path, commit: str) -> tuple[dict | None, str | None]:
    """The schema-valid `.pi/autonomy.yaml` committed at `commit`, and its blob SHA; (None, None) when absent.

    Raises AutonomyPolicyError for a symlink, an unreadable blob or a policy that does not validate.
    """
    listing = _git(checkout, "ls-tree", "-z", commit, "--", str(autonomy_policy.RELATIVE_PATH))
    if listing.returncode != 0:
        raise autonomy_policy.AutonomyPolicyError("the pinned commit cannot be read")
    line = listing.stdout.decode("utf-8", "replace").rstrip("\0")
    if not line:
        return None, None
    meta, _, _ = line.partition("\t")
    mode, kind, blob = (meta.split() + ["", "", ""])[:3]
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise autonomy_policy.AutonomyPolicyError("the committed .pi/autonomy.yaml is a symlink or not a file")
    content = _git(checkout, "cat-file", "blob", blob)
    if content.returncode != 0:
        raise autonomy_policy.AutonomyPolicyError("the committed .pi/autonomy.yaml cannot be read")
    try:
        data = yaml.safe_load(content.stdout.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise autonomy_policy.AutonomyPolicyError("the committed .pi/autonomy.yaml is unreadable") from exc
    issues = autonomy_policy.validate(data)
    if issues:
        raise autonomy_policy.AutonomyPolicyError(issues[0])
    return data, blob


def _working_copy_differs(checkout: Path, blob: str | None) -> bool:
    """True when the checkout's own `.pi/autonomy.yaml` is not the committed one (untracked, edited or removed)."""
    path = checkout / autonomy_policy.RELATIVE_PATH
    if not path.is_file() or path.is_symlink():
        return blob is not None or path.is_symlink()
    hashed = _git(checkout, "hash-object", "--", str(autonomy_policy.RELATIVE_PATH))
    return hashed.returncode != 0 or hashed.stdout.decode().strip() != blob


def check(task: dict, repo: dict, *, allow_mutation: bool) -> dict:
    """Authorization verdict of one task against its repository's committed policy at the pinned commit.

    `status` is `not_required`, `authorized`, `awaiting` (carrying `code` and, for a missing grant, the
    `missing` permissions) or `unresolved` (the repository itself has a blocker, which the launcher
    reports as a refusal, never as awaiting authorization). Nothing is written.
    """
    verdict = {"status": "not_required", "code": None, "repository": repo["id"],
               "github": repo.get("repository"), "pinned_commit": repo.get("pinned_commit"),
               "base_branch": repo.get("base_branch", "main"), "required": [], "missing": [],
               "policy_source": None, "policy_blob": None, "uncommitted_policy": False,
               "allow_mutation": bool(allow_mutation)}
    if task.get("kind") == "qualification" or task.get("mutation") == "none":
        return verdict
    verdict["required"] = list(REQUIRED)
    checkout = Path(repo["resolved_path"]) if repo.get("resolved_path") else None
    if checkout is None or not repo.get("pinned_commit") or repo.get("blocker_code"):
        return {**verdict, "status": "unresolved", "code": repo.get("blocker_code") or "WORKSPACE_REPOSITORY_PATH_MISSING"}
    try:
        policy, blob = committed_policy(checkout, repo["pinned_commit"])
    except autonomy_policy.AutonomyPolicyError as exc:
        unsafe = "symlink" in str(exc)
        return {**verdict, "status": "awaiting", "code": "AUTONOMY_POLICY_PATH_UNSAFE" if unsafe else "AUTONOMY_POLICY_INVALID",
                "missing": list(REQUIRED), "policy_source": "committed", "detail": str(exc)[:300]}
    permissions = (policy or {}).get("permissions") or {}
    verdict.update({"policy_source": "committed" if policy is not None else "default",
                    "policy_blob": blob, "uncommitted_policy": _working_copy_differs(checkout, blob),
                    "missing": [name for name in REQUIRED if permissions.get(name) is not True]})
    if verdict["missing"]:
        return {**verdict, "status": "awaiting", "code": REQUIRED_CODE}
    if not allow_mutation:
        return {**verdict, "status": "awaiting", "code": FLAG_CODE}
    return {**verdict, "status": "authorized"}


def fingerprint(verdict: dict | None) -> str | None:
    """Digest of what authorized a launch: repository, pin, policy blob, grants and the operator flag."""
    if not verdict or verdict.get("status") != "authorized":
        return None
    basis = {k: verdict.get(k) for k in ("repository", "pinned_commit", "policy_blob", "required", "allow_mutation")}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def stopped_for_authorization(run: dict | None, checkout: dict | None = None) -> bool:
    """A finished run that stopped to ask for mutation approval and provably changed nothing.

    Only BLOCKED with an authorization code, zero code and GitHub mutations, no worker in any phase,
    and (when known) a parent checkout left clean with no kept worktree. Anything else stays a failure.
    """
    parent = (run or {}).get("parent_orchestration") if isinstance(run, dict) else None
    if not isinstance(parent, dict) or parent.get("final_status") != "BLOCKED" or parent.get("blocker_code") not in CODES:
        return False
    mutations = parent.get("mutations") or {}
    if mutations.get("code") != 0 or mutations.get("github") != 0:
        return False
    if any((phase or {}).get("workers") for phase in parent.get("phases") or []):
        return False
    if checkout and (checkout.get("dirty_count") or checkout.get("kept_worktrees") or checkout.get("new_untracked_count")):
        return False
    return True


def objective_line(verdict: dict) -> str:
    """The sentence a child's objective carries when its launch was authorized by this check."""
    return ("Mutation authorization (checked by PICO before launch): the .pi/autonomy.yaml committed at the pinned "
            "commit grants " + " and ".join(verdict["required"]) + ", and the operator allowed mutation, so "
            "repository-local worktrees and commits are authorized. Push, pull requests, mark-ready, merge and "
            "deployment are not authorized by this.")


def request_lines(verdict: dict, tasks: list[str], manifest: str | Path) -> list[str]:
    """Plain-text, repository-specific approval request with the next steps, for the CLI."""
    repo, github = verdict["repository"], verdict.get("github") or verdict["repository"]
    code = verdict["code"]
    head = f"  {repo} ({github}): {code} for {', '.join(tasks)}"
    lines = [head]
    if code == FLAG_CODE:
        lines.append("    The committed policy grants " + " and ".join(verdict["required"]) +
                     ". Rerun with --allow-mutation to let these tasks create worktrees and commit.")
        return lines
    if code in POLICY_CODES:
        lines.append(f"    The .pi/autonomy.yaml committed at {str(verdict.get('pinned_commit'))[:12]} cannot be used"
                     + (f" ({verdict['detail']})" if verdict.get("detail") else "") + ". Fix it on the base branch, then re-pin.")
        return lines
    source = ("no .pi/autonomy.yaml is committed at the pinned commit, so the default policy applies (no mutation)"
              if verdict.get("policy_source") == "default" else "the committed .pi/autonomy.yaml does not grant them")
    lines.append(f"    Needs {', '.join(verdict['missing'])}: {source}.")
    if verdict.get("uncommitted_policy"):
        lines.append("    The checkout has an uncommitted .pi/autonomy.yaml; it does not count until committed and pinned.")
    lines += [f"    1. Approve: pico watch --workspace {manifest} --controls, then \"Approve local mutation\" for {repo}"
              f" (or set permissions.{' and permissions.'.join(verdict['missing'])} to true in .pi/autonomy.yaml yourself).",
              f"    2. Commit .pi/autonomy.yaml on {verdict.get('base_branch') or 'main'} (\"Commit policy\" in the Garden);"
              " push it to the default branch if the repository's publication should see it too.",
              f"    3. Set pinned_commit of {repo} in the manifest to that commit.",
              "    4. Rerun pico workspace schedule with --allow-mutation; a task whose run stopped for approval starts a new"
              " attempt linked to it.",
              "    This grants no push, pull request, mark-ready, merge or deployment permission."]
    return lines


def bounded_policy(before: str, template: str, missing: list[str]) -> str:
    """Policy text after granting only `missing` (a subset of REQUIRED).

    An existing policy keeps every other line. A new one starts from the template with every
    permission denied and merge disabled, so approval never grants more than local mutation.
    """
    names = [name for name in missing if name in REQUIRED]
    if before:
        source = before
    else:
        flags = "|".join(PERMISSION_FLAGS)
        source = re.sub(rf"^(\s+(?:{flags}):\s*)true(\s*(#.*)?)$", r"\1false\2", template, flags=re.M)
    for name in names:
        pattern = re.compile(rf"^(\s+{name}:\s*)(true|false)(\s*(#.*)?)$", re.M)
        if len(pattern.findall(source)) != 1:
            raise ValueError(f"{name} is not a single plain line in the policy")
        source = pattern.sub(lambda m: f"{m.group(1)}true{m.group(3)}", source)
    data = yaml.safe_load(source)
    if autonomy_policy.validate(data):
        raise ValueError("the edited policy would not validate")
    if not before and granted_beyond_local(data):
        raise ValueError("a new policy must not grant publication, merge or deployment")
    return source


def granted_beyond_local(policy: dict) -> list[str]:
    """Publication, merge and deployment permissions a policy grants."""
    permissions = policy.get("permissions") or {}
    granted = [name for name in NEVER_GRANTED if name != "merge" and permissions.get(name) is True]
    return granted + (["merge"] if (permissions.get("merge") or {}).get("enabled") is True else [])
