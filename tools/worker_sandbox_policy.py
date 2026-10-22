#!/usr/bin/env python3
"""The authoritative `worker_sandbox` widening for a run's Claude workers (#63).

The opt-in network allowlist and cache directories come from `.pi/autonomy.yaml`
on the repository's DEFAULT branch, read through the GitHub API adapter exactly
like publication permissions (`github_integration.authority_policy`, #39/#64).
The checkout's working tree is never authority: worker commits are integrated
into it, so a worker could otherwise widen its own successors' sandbox.

The verdict always widens nothing unless the default-branch policy says so:

- `policy_source: default_branch`: the policy GitHub reports at the default
  branch's commit (recorded as `policy_commit` and `policy_blob`);
- `default`: GitHub reports no policy on the default branch;
- `unavailable`: the repository identity or the GitHub read failed (no `gh`,
  no authentication, offline), so nothing is widened (fail closed, as
  publication refuses without a readable policy);
- `invalid`: the default-branch policy exists but is not schema-valid.

It also reports, for the record only, whether the checkout's own copy declares
a different `worker_sandbox` (`local_drift`), and which widening keys the
user's own Claude Code settings contain (`user_settings_widening`): those merge
into every worker's settings and PICO cannot remove them.

CLI: `worker_sandbox_policy.py --worktree <target> [--repo owner/name]` prints a
JSON verdict and exits 0; it never fails the run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autonomy_policy  # noqa: E402
import github_integration  # noqa: E402
import github_sync  # noqa: E402

# Keys of a user's Claude Code settings that widen every sandbox they merge into (arrays merge across sources).
USER_WIDENING_KEYS = (("sandbox", "excludedCommands"), ("sandbox", "filesystem", "allowWrite"),
                      ("sandbox", "network", "allowedDomains"))


def _nothing() -> dict:
    return autonomy_policy.worker_sandbox(None)


def _local_section(worktree: Path) -> dict | None:
    """The checkout's own validated `worker_sandbox`, or None when absent, unreadable or invalid (record only)."""
    try:
        path = autonomy_policy.policy_path(worktree)
        if path.is_symlink() or not path.is_file():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or autonomy_policy.validate(data):
        return None
    return autonomy_policy.worker_sandbox(data.get("worker_sandbox"))


def user_settings_widening(env: dict | None = None) -> list[str]:
    """Widening keys present (non-empty) in the user's Claude Code settings: `$CLAUDE_CONFIG_DIR/settings.json`,
    else `~/.claude/settings.json`. Unreadable or absent settings report nothing."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or str(Path(env.get("HOME") or Path.home()) / ".claude")
    try:
        data = json.loads((Path(base) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    found = []
    for path in USER_WIDENING_KEYS:
        value = data
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list) and value:
            found.append(".".join(path))
    return found


def resolve(worktree: Path, explicit_repo: str | None = None, *, adapter=github_sync,
            env: dict | None = None) -> dict:
    verdict = {"ok": True, "policy_source": "unavailable", "worker_sandbox": _nothing(), "repository": None,
               "policy_branch": None, "policy_commit": None, "policy_blob": None, "error_code": None,
               "user_settings_widening": user_settings_widening(env)}
    try:
        repo = github_integration.resolve_target(worktree, explicit_repo)
        verdict["repository"] = repo
        authority = github_integration.authority_policy(repo, adapter)
    except github_integration.IntegrationError as exc:
        code = exc.code
        verdict.update(policy_source="invalid" if code in {"AUTONOMY_POLICY_INVALID", "AUTONOMY_POLICY_PATH_UNSAFE"}
                       else "unavailable", error_code=code)
    except (github_sync.GithubSyncError, ValueError, OSError):
        verdict["error_code"] = "WORKER_SANDBOX_POLICY_UNAVAILABLE"
    else:
        verdict.update(policy_branch=authority["branch"], policy_commit=authority["commit"],
                       policy_blob=authority["blob"])
        if authority["policy"] is None:
            verdict["policy_source"] = "default"
        else:
            verdict["policy_source"] = "default_branch"
            verdict["worker_sandbox"] = autonomy_policy.worker_sandbox(authority["policy"].get("worker_sandbox"))
    local = _local_section(worktree)
    verdict["local_drift"] = local is not None and local != verdict["worker_sandbox"]
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--repo", help="explicit owner/repository; checked against the worktree's remotes")
    args = parser.parse_args()
    print(json.dumps(resolve(args.worktree, args.repo), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
