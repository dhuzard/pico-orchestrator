#!/usr/bin/env python3
"""File a user-authored bug report in the canonical PICO repository."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import github_sync


PICO_REPOSITORY = "dhuzard/pico-orchestrator"


def report(*, title: str, body_file: Path, apply: bool = False,
           plan_hash: str | None = None, adapter=github_sync) -> dict:
    if not title.strip() or title != title.strip() or "\n" in title or "\r" in title or len(title) > 256:
        raise ValueError("title must be one non-empty line of at most 256 characters")
    body = body_file.read_text(encoding="utf-8")
    if not body.strip() or len(body) > 65536:
        raise ValueError("body file must contain 1 to 65536 characters of report text")
    plan = {"repository": PICO_REPOSITORY, "title": title, "body": body}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not apply:
        return {"status": "DRY_RUN", "plan": plan, "plan_hash": digest}
    if plan_hash != digest:
        raise ValueError("report changed since preview; review it again and use the new plan hash")
    actual = adapter.repo_view(repo=PICO_REPOSITORY)
    if actual["nameWithOwner"].casefold() != PICO_REPOSITORY.casefold():
        raise ValueError("PICO issue destination did not resolve to the canonical repository")
    created = adapter.create_issue(title, body, repo=PICO_REPOSITORY)
    return {"status": "COMPLETED", "repository": PICO_REPOSITORY,
            "issue": created["number"], "url": created["url"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True, help="single-line issue title")
    parser.add_argument("--body-file", type=Path, required=True, help="user-written report file")
    parser.add_argument("--apply", action="store_true", help="create the reviewed GitHub issue")
    parser.add_argument("--plan-hash", help="hash from the exact preview to create")
    args = parser.parse_args(argv)
    try:
        result = report(title=args.title, body_file=args.body_file,
                        apply=args.apply, plan_hash=args.plan_hash)
    except (OSError, UnicodeError, ValueError, github_sync.GithubSyncError) as exc:
        parser.exit(1, f"pico report: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
