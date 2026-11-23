"""Read-only GitHub issue snapshots for standing-rule shadow evaluation.

The caller supplies a GitHub API client with the intended scoped credentials.
These normalized values are observations, not approval, activation, or authority
to execute. The caller must still attest the credential and observation path.
"""
from __future__ import annotations

import re


REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SnapshotError(Exception):
    """A required GitHub identity or field could not be verified."""


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def observe_issue(gh_json, repository: str, number: int) -> dict:
    """Fetch and normalize immutable repository, issue, and author identities.

    A repository transfer, fork, public visibility, missing field, or inconsistent
    issue URL is refused. Content remains untrusted even after identity checks.
    """
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or any(part in (".", "..") for part in repository.split("/")) or not _positive_int(number)):
        raise SnapshotError("invalid repository or issue selector")
    try:
        repo = gh_json(["api", f"repos/{repository}"])
        issue = gh_json(["api", f"repos/{repository}/issues/{number}"])
    except Exception as exc:
        raise SnapshotError("GitHub issue observation unavailable") from exc
    if not isinstance(repo, dict) or not isinstance(issue, dict):
        raise SnapshotError("invalid GitHub issue observation")
    name = repo.get("full_name")
    if (not _positive_int(repo.get("id")) or not isinstance(name, str)
            or name.casefold() != repository.casefold() or repo.get("private") is not True
            or repo.get("fork") is not False):
        raise SnapshotError("repository identity or visibility mismatch")
    if (issue.get("repository_url") != f"https://api.github.com/repos/{name}"
            or issue.get("number") != number or not _positive_int(issue.get("id"))
            or not isinstance(issue.get("user"), dict) or not _positive_int(issue["user"].get("id"))):
        raise SnapshotError("issue identity mismatch")
    if (not isinstance(issue.get("title"), str) or not isinstance(issue.get("body"), str)
            or issue.get("state") not in ("open", "closed")
            or not isinstance(issue.get("labels"), list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in issue["labels"])):
        raise SnapshotError("incomplete issue content")
    return {
        "id": issue["id"], "number": number,
        "repository": {"id": repo["id"], "name": name,
                       "private": True, "is_fork": False},
        "author_id": issue["user"]["id"], "is_pull_request": "pull_request" in issue,
        "state": issue["state"].upper(),
        "labels": [label["name"] for label in issue["labels"]],
        "title": issue["title"], "body": issue["body"],
    }
