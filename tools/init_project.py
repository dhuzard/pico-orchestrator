#!/usr/bin/env python3
"""Initialize a target repository for use with pi-coding-orchestrator.

This is intentionally small and conservative: it creates project-local routing
constraints and explicit PR workflow instructions, and can optionally copy a
minimal local extension runtime bundle or professional GitHub collaboration
templates into the target repo. It does not install dependencies, create backlog
items, or mutate GitHub.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PR_WORKFLOW_TEXT = """# Pi Coding Orchestrator PR workflow

Use this checklist when `/orchestrate` or an agentic run completes implementation
work in this repository.

1. Validate locally before opening a PR.
   - Run the validation commands requested by this repository's `AGENTS.md`.
   - If this repository vendors the orchestrator backlog tools, also run
     `python3 tools/backlog.py validate`.

2. Commit and push a human/orchestrator-owned branch.
   - Workers/subagents must not push branches or mutate GitHub.
   - The lead orchestrator or human owner reviews the diff, commits, and pushes.
   - With `pr_granularity: batch` in `.pi/autonomy.yaml` (the default), the
     tasks of one wave are validated in their own worktrees, merged locally into
     one `pico/batch-<n>` branch, and published with one push and one PR that
     references every included issue. `pr_granularity: task` opens one PR per
     task. Pushing and PRs still require `push_branch` and `create_pr`.
   - CI never blocks the next wave: after a push the orchestrator records
     `waiting_ci` and continues; a CI failure becomes a `ci_fix` task in a later
     wave, and only merge waits for CI. At `limits.open_prs` open batch PRs,
     new batches stack on the newest batch branch instead of opening a PR.

3. Create or link the PR.
   - If this repo vendors the backlog tools and has a mapped backlog item:
     `python3 tools/backlog.py github-pr-create <ITEM> --base <base> --head <branch>`
     or `python3 tools/backlog.py github-pr-link <ITEM> <pr-number>`.
   - Otherwise create the PR with the repository's normal GitHub workflow.
   - PR bodies should reference issues with `Refs #N`, not `Closes #N`, unless
     the repository explicitly wants GitHub auto-close behavior.

4. Communicate through GitHub professionally.
   - Use the repository issue/PR templates when present.
   - Comments should state status or findings, cite validation evidence, and
     distinguish blockers from follow-up suggestions.
   - The constrained adapter supports issue/PR comments and PR title/body
     updates through reviewed dry-run plans; it never exposes arbitrary `gh`.

5. Record the final orchestration trace when available.
   - If provenance tools are available: `python3 tools/backlog.py trace --latest`
     or `python3 tools/backlog.py trace --backlog-id <ITEM> --latest`.
   - Include the trace path/summary in the final user-facing report.

6. Close issues only after acceptance.
   - In backlog-managed repos, mark the item `completed` and run
     `python3 tools/backlog.py github-push <ITEM>`; the close guard lives there.
   - Do not close issues merely because a PR was opened.
"""


def copy_if_needed(src: Path, dst: Path, *, overwrite: bool) -> str:
    if dst.exists() and not overwrite:
        return f"kept existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return f"wrote {dst}"


def write_if_needed(dst: Path, text: str, *, overwrite: bool) -> str:
    if dst.exists() and not overwrite:
        return f"kept existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    return f"wrote {dst}"


# The vendored runtime is every file the extensions and their Python tools load,
# plus the local launcher that pins the Claude backend identity. Whole runtime
# directories are copied so the bundle cannot drift from the extensions' needs.
RUNTIME_DIRECTORIES = (("extensions", "*.ts"), ("tools", "*.py"), ("schemas", "*.json"),
                       ("routing", "*"), ("prompts", "*.md"), ("templates", "*"))


def extension_runtime_files() -> list[tuple[str, str]]:
    files = [(f"{directory}/{path.name}", f".pi/{directory}/{path.name}")
             for directory, pattern in RUNTIME_DIRECTORIES
             for path in sorted((REPO_ROOT / directory).glob(pattern)) if path.is_file()]
    return files + [("scripts/run-local.sh", ".pi/scripts/run-local.sh")]

GITHUB_TEMPLATE_FILES = (
    (".github/ISSUE_TEMPLATE/bug.yml", ".github/ISSUE_TEMPLATE/bug.yml"),
    (".github/ISSUE_TEMPLATE/change.yml", ".github/ISSUE_TEMPLATE/change.yml"),
    (".github/ISSUE_TEMPLATE/work-item.yml", ".github/ISSUE_TEMPLATE/work-item.yml"),
    (".github/ISSUE_TEMPLATE/config.yml", ".github/ISSUE_TEMPLATE/config.yml"),
    (".github/pull_request_template.md", ".github/pull_request_template.md"),
)


def init_project(
    target: Path,
    *,
    overwrite: bool = False,
    copy_extension: bool = False,
    github_templates: bool = False,
) -> list[str]:
    target = target.resolve()
    if not target.exists() or not target.is_dir():
        raise SystemExit(f"target repository directory does not exist: {target}")

    actions: list[str] = []
    actions.append(copy_if_needed(REPO_ROOT / "templates" / "project-routing.yaml", target / ".pi" / "routing.yaml", overwrite=overwrite))
    actions.append(copy_if_needed(REPO_ROOT / "templates" / "autonomy.yaml", target / ".pi" / "autonomy.yaml", overwrite=overwrite))
    actions.append(copy_if_needed(REPO_ROOT / "templates" / "AGENTS.md", target / "AGENTS.md", overwrite=overwrite))
    actions.append(write_if_needed(target / ".pi" / "orchestrator-pr-workflow.md", PR_WORKFLOW_TEXT, overwrite=overwrite))

    if copy_extension:
        for src, dst in extension_runtime_files():
            actions.append(copy_if_needed(REPO_ROOT / src, target / dst, overwrite=overwrite))

    if github_templates:
        for src, dst in GITHUB_TEMPLATE_FILES:
            actions.append(copy_if_needed(REPO_ROOT / src, target / dst, overwrite=overwrite))

    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize a repository for pi-coding-orchestrator use.")
    parser.add_argument("target", type=Path, help="Target repository directory.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing generated files.")
    parser.add_argument("--copy-extension", action="store_true", help="Also copy extensions plus their minimal Python policy/mission runtime into .pi/.")
    parser.add_argument("--github-templates", action="store_true",
                        help="Copy missing professional issue/PR templates into .github/; existing files are preserved unless --overwrite is also set.")
    args = parser.parse_args(argv)

    actions = init_project(
        args.target,
        overwrite=args.overwrite,
        copy_extension=args.copy_extension,
        github_templates=args.github_templates,
    )
    for action in actions:
        print(action)
    print("\nNext steps:")
    print("  1. Review AGENTS.md, .pi/routing.yaml, .pi/autonomy.yaml, and .pi/orchestrator-pr-workflow.md.")
    if args.github_templates:
        print("     Review the copied .github issue/PR templates and adapt labels or repository-specific fields as needed.")
    print("  2. Start Pi with the orchestrator loaded: pico run .")
    print("     or, if --copy-extension was used: .pi/scripts/run-local.sh .")
    print("     Plain `pi` does not load the orchestrator, its backend pin, or its safety checks.")
    print("  3. Inside Pi, use /initialize to re-check conservative project setup, then /orchestrate, /audit, or /deliver.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
