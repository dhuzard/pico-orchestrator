#!/usr/bin/env python3
"""Create the synthetic benchmark workspace: two throwaway repositories and a workspace manifest.

    python3 benchmarks/synthetic-workspace/setup.py TARGET_DIR [--owner OWNER] [--workspace-id ID]
                                                    [--publication [--i-own-these-repos]] [--issue TASK=N ...]
                                                    [--max-tokens N]

TARGET_DIR must not exist yet (or be empty) and must not lie inside the PICO
repository. It receives `synth-lib/`, `synth-app/` and `workspace.yaml`.

Nothing here contacts GitHub. Repository identity is a remote URL naming a
sandbox owner (default `pico-bench-sandbox`); the repositories need not exist
there. Commits use a fixed identity and date, so the pinned commits are the same
on every run of this script: two PICO revisions can be benchmarked against
identical pins. Publication stays off (no `.pi/autonomy.yaml`) unless
`--publication` is given.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_OWNER = "pico-bench-sandbox"
DEFAULT_WORKSPACE_ID = "pico-bench"
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "PICO Benchmark", "GIT_AUTHOR_EMAIL": "bench@example.invalid",
    "GIT_COMMITTER_NAME": "PICO Benchmark", "GIT_COMMITTER_EMAIL": "bench@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
REPOSITORIES = ("synth-lib", "synth-app")
TASK_IDS = ("lib-slugify", "lib-stopwords", "app-post-url", "qualify-integration")
# What CI runs in each repository. synth-lib runs only the slugify tests: test_stopwords cannot pass (its approved
# input never exists, lib-stopwords ends PARTIAL), and the whole suite would keep lib-slugify's PR red forever, so
# nothing downstream could be promoted. synth-app runs its whole suite.
CI_COMMANDS = {"synth-lib": "python3 -m unittest tests.test_slugify",
               "synth-app": "python3 -m unittest discover -s tests -t ."}
DEFAULT_MAX_TOKENS = 5_000_000

AUTONOMY = {
    "profile": "guarded", "pr_granularity": "task", "issue_reference": "refs",
    "permissions": {"create_backlog_items": False, "create_github_issues": False, "create_worktrees": True,
                    "commit": True, "push_branch": True, "create_pr": True, "comment_on_issue": False,
                    "comment_on_pr": False, "mark_pr_ready": False, "deploy": False,
                    "merge": {"enabled": False, "max_failure_cost": "low", "require_ci": True,
                              "require_review_if_policy_demands": True}},
    "limits": {"parallel_tasks": 2, "open_prs": 2, "nested_agents_per_task": 1, "repair_attempts": 1, "replans": 1},
}


class SetupError(Exception):
    pass


def git(repo: Path, *args: str) -> str:
    # Independent of the user's Git configuration: no global/system config, no signing, no hooks.
    env = {**os.environ, **COMMIT_ENV, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args], env=env, capture_output=True, text=True, check=False)
    if done.returncode:
        raise SetupError(f"git {' '.join(args)} failed in {repo}: {done.stderr.strip()}")
    return done.stdout.strip()


def build_repository(target: Path, name: str, owner: str, publication: bool) -> str:
    repo = target / name
    shutil.copytree(HERE / "files" / name, repo)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = (HERE / "files" / "ci" / "tests.yml").read_text(encoding="utf-8")
    (workflows / "tests.yml").write_text(workflow.replace("__CI_COMMAND__", CI_COMMANDS[name]), encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    if publication:
        (repo / ".pi").mkdir()
        (repo / ".pi" / "autonomy.yaml").write_text(yaml.safe_dump(AUTONOMY, sort_keys=False), encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", f"https://github.com/{owner}/{name}.git")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Synthetic benchmark baseline")
    return git(repo, "rev-parse", "HEAD")


def check(check_id: str, repository: str, *argv: str, timeout: int = 120) -> dict:
    return {"id": check_id, "repository": repository, "argv": ["python3", *argv], "timeout_seconds": timeout}


def build_manifest(workspace_id: str, owner: str, pins: dict[str, str], issues: dict[str, int],
                   max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    tasks = [
        {"id": "lib-slugify", "repository": "synth-lib", "title": "Add slugify to synth-lib",
         "objective": ("In synth-lib, add function slugify(text) to synthlib/text.py. Semantics: raise TypeError if text is "
                       "not a str; lowercase the text; replace every run of characters outside a-z and 0-9 with one hyphen "
                       "(non-ASCII letters are separators, not transliterated); strip leading and trailing hyphens; return "
                       "an empty string if nothing remains. Make tests/test_slugify.py pass without editing any test."),
         "max_duration_seconds": 900,
         "provides": [{"contract": "slugify", "version": "1"}],
         "checks": [check("slugify-tests", "synth-lib", "-m", "unittest", "tests.test_slugify")]},
        {"id": "lib-stopwords", "repository": "synth-lib", "title": "Add strip_stopwords to synth-lib",
         "objective": ("In synth-lib, add function strip_stopwords(text, path) to synthlib/text.py. It removes, from the "
                       "whitespace-separated words of text, every word listed in the stopword file at path, and joins the "
                       "rest with single spaces. The approved stopword list data/approved-stopwords.txt is NOT in the "
                       "repository and nobody has supplied it. Do not invent, generate or guess its content and do not "
                       "weaken tests/test_stopwords.py. If the file is absent, record the missing input as the blocker "
                       "APPROVED_INPUTS_UNAVAILABLE and finish the run PARTIAL."),
         "depends_on": ["lib-slugify"], "max_duration_seconds": 600,
         "checks": [check("stopwords-tests", "synth-lib", "-m", "unittest", "tests.test_stopwords")]},
        {"id": "app-post-url", "repository": "synth-app", "title": "Add post_url to synth-app",
         "objective": ("In synth-app, add function post_url(title, slugify) to synthapp/links.py. It calls the injected "
                       "slugify callable (contract slugify version 1 from synth-lib) on title and returns '/posts/' followed "
                       "by the slug; it raises ValueError if the slug is empty. Do not import or copy code from synth-lib and "
                       "do not edit any other repository. Make tests/test_links.py pass without editing any test."),
         "depends_on": ["lib-slugify"], "max_duration_seconds": 900,
         "requires": [{"contract": "slugify", "version": "1"}],
         "checks": [check("post-url-tests", "synth-app", "-m", "unittest", "tests.test_links")]},
        # Checks both repositories, each at the commit its dependency was validated at. Only the slugify tests run in
        # synth-lib: lib-stopwords is meant to end PARTIAL, and qualification does not depend on it.
        {"id": "qualify-integration", "repository": "synth-app", "title": "Qualify the validated tasks together",
         "kind": "qualification", "depends_on": ["lib-slugify", "app-post-url"],
         "checks": [check("app-suite", "synth-app", "-m", "unittest", "discover", "-s", "tests", "-t", "."),
                    check("lib-slugify-suite", "synth-lib", "-m", "unittest", "tests.test_slugify")]},
    ]
    for task in tasks:
        if task["id"] in issues:
            task["issue"] = issues[task["id"]]
    return {"schema_version": 1, "id": workspace_id,
            "goal": "Synthetic benchmark: add slugify, use it from another repository, and end one task PARTIAL.",
            "max_parallel": 2, "max_duration_seconds": 1800,
            "budget": {"max_tokens": max_tokens},
            "repositories": [{"id": name, "repository": f"{owner}/{name}", "path": name,
                              "pinned_commit": pins[name], "base_branch": "main"} for name in REPOSITORIES],
            "tasks": tasks}


def inside_git(target: Path) -> bool:
    """Whether target lies inside any Git work tree or Git directory (checked from its nearest existing parent)."""
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        probe = probe.parent
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        done = subprocess.run(["git", "-C", str(probe), "rev-parse", "--git-dir"], capture_output=True, text=True,
                              check=False, env=env)
    except OSError as exc:
        raise SetupError(f"cannot check whether the target is inside a Git repository: git is unavailable ({exc})") from exc
    if done.returncode == 0:
        return True
    # Only Git's own "not a git repository" answer (exit 128) means the target is outside every repository.
    if done.returncode == 128 and "not a git repository" in done.stderr.lower():
        return False
    raise SetupError("cannot tell whether the target is inside a Git repository "
                     f"(git exited {done.returncode}: {done.stderr.strip()[:200]}); refusing to continue")


def create(target: Path, *, owner: str = DEFAULT_OWNER, workspace_id: str = DEFAULT_WORKSPACE_ID,
           publication: bool = False, issues: dict[str, int] | None = None, confirm_owned: bool = False,
           max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    target = target.expanduser().resolve()
    if target == ROOT or ROOT in target.parents:
        raise SetupError("target must be outside the PICO repository")
    try:
        common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=False)
    except OSError as exc:
        raise SetupError(f"git is unavailable ({exc})") from exc
    if common.returncode == 0 and (Path(common.stdout.strip()) == target or Path(common.stdout.strip()) in target.parents):
        raise SetupError("target must not be inside this checkout's Git directory")
    if inside_git(target):
        raise SetupError("target must not be inside any Git repository or work tree")
    if publication and owner != DEFAULT_OWNER and not confirm_owned:
        raise SetupError(f"--publication with owner {owner!r} commits push and PR permissions for that owner's "
                         "repositories: pass --i-own-these-repos to confirm they are your sandbox repositories")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise SetupError("target exists and is not an empty directory")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise SetupError("max_tokens must be a positive integer")
    issues = issues or {}
    unknown = sorted(set(issues) - set(TASK_IDS))
    if unknown:
        raise SetupError("unknown task id for --issue: " + ", ".join(unknown))
    target.mkdir(parents=True, exist_ok=True)
    pins = {name: build_repository(target, name, owner, publication) for name in REPOSITORIES}
    manifest = build_manifest(workspace_id, owner, pins, issues, max_tokens)
    path = target / "workspace.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, width=120), encoding="utf-8")
    return {"manifest": str(path), "workspace_id": workspace_id, "owner": owner, "publication": publication,
            "max_tokens": max_tokens,
            "repositories": {name: str(target / name) for name in REPOSITORIES}, "pinned_commits": pins}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the synthetic PICO benchmark workspace in TARGET_DIR.")
    parser.add_argument("target", type=Path)
    parser.add_argument("--owner", default=DEFAULT_OWNER, help="GitHub owner named in the remote URLs (default %(default)s)")
    parser.add_argument("--workspace-id", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--publication", action="store_true",
                        help="commit .pi/autonomy.yaml allowing push and draft PRs (sandbox repositories only)")
    parser.add_argument("--i-own-these-repos", dest="confirm_owned", action="store_true",
                        help="required with --publication and an owner other than the default: confirms the named "
                             "repositories are your own sandbox repositories")
    parser.add_argument("--issue", action="append", default=[], metavar="TASK=N",
                        help="issue number in the task's repository that its draft PR references")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="workspace budget.max_tokens (input + output + cache-write tokens; default %(default)s)")
    args = parser.parse_args(argv)
    try:
        issues = {}
        for item in args.issue:
            task, _, number = item.partition("=")
            if not number.isdigit() or int(number) < 1:
                raise SetupError(f"--issue expects TASK=N, got {item!r}")
            issues[task] = int(number)
        result = create(args.target, owner=args.owner, workspace_id=args.workspace_id,
                        publication=args.publication, issues=issues, confirm_owned=args.confirm_owned,
                        max_tokens=args.max_tokens)
    except SetupError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
