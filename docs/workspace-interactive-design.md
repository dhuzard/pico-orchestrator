# Workspace manifest builder (`pico workspace init`)

`pico workspace init` builds a new workspace manifest
(`schemas/workspace.schema.json`, `schema_version: 1`) from repositories the
user selects, previews it with the planner's waves and blockers, and saves it
only after explicit approval. It is a convenience for writing the first
manifest. It does not change the manifest format, the planner, the scheduler
or any policy, and it grants no permission.

```bash
pico workspace init [--root DIR] [--output FILE]                 # interactive, needs a terminal
pico workspace init --spec FILE [--output FILE] [--approve]      # non-interactive
scripts/run-local.sh workspace init ...                           # same, from a local checkout
python3 tools/workspace_init.py ...                               # same, direct
```

Exit codes: `0` preview printed or manifest saved, `1` not saved (cancelled,
declined, a repository could not be resolved, the destination exists), `2`
invalid invocation, spec or environment, `130` interrupted (Ctrl-C).

## What it never does

- It writes exactly one file, only after an interactive `yes` (or `y`) or
  `--spec ... --approve`. Any other answer, a blank answer, end of input,
  `q` or Ctrl-C writes nothing.
- It never overwrites: the file is created with `O_CREAT|O_EXCL|O_NOFOLLOW`
  in an existing directory, so an existing file, directory or symlink (also a
  dangling one) is refused, and of two concurrent saves exactly one succeeds.
  If writing fails or is interrupted after the file was created, the partial
  file is removed, but only while the path still names the inode this run
  created.
- It never clones, fetches, pulls, creates worktrees, launches workers or
  runs Pi or Claude. Its subprocesses are read-only `git rev-parse` and
  `git remote` and the `gh` calls below.
- Previewing writes no bytecode (`sys.dont_write_bytecode` is set before any
  import; both launchers also export `PYTHONDONTWRITEBYTECODE=1`).
- The discovery root is not an authorization and not a project-pin source:
  `pico workspace init` does not read `.pi/orchestrator.lock`, does not
  activate a model config and does not pin a backend. Like every `pico`
  command, `use-latest.sh` still bootstraps its cache first (clone or fetch
  of the orchestrator repository into `$PI_CODING_ORCHESTRATOR_HOME`, a
  runtime snapshot under `runtimes/<commit>`); `PI_CODING_ORCHESTRATOR_REF`
  is honored. `run-local.sh` uses its own checkout and writes nothing else.
- It refuses to run when `GIT_DIR`, `GIT_WORK_TREE` or `GIT_COMMON_DIR` is
  set (git would answer for another repository than the path asked about) or
  when `GH_HOST` names a host other than `github.com` (`gh repo view`
  inherits it, while the `gh api` reads pin `--hostname github.com`).

## Interactive flow

A terminal is required on both stdin and stdout; otherwise the command exits
`2` and points to `--spec`. Prompts are plain standard-library line reads. An
invalid answer (an unknown selection, a malformed id, a secret-looking
objective, an existing destination) is explained and asked again; end of
input cancels at every prompt.

1. **Discovery root**, shown with its default (the directory the command runs
   in, or `--root`); press Enter to accept or type another path.
2. **Discovery** (below) lists the selectable checkouts with stable numbers
   and reports what was not selectable, unreadable or cut off by a bound.
3. **Selection**: numbers or ranges toggle entries (`1 3 5-7`); `/text`
   shows only lines containing the text (case-insensitive, matched on the
   path and identities; numbers do not change); `/` shows all again; `r`
   adds a GitHub repository that is not cloned yet with the path where it
   will be cloned (relative to the root, which must not exist yet); `d`
   finishes once 2 to 64 repositories are selected; `q` cancels. Two selected
   checkouts with the same GitHub identity are refused.
4. **Identity choice** for every selected checkout with several GitHub
   remotes. There is no default and `origin` is not preferred: the user picks
   one of the listed identities by number, and resolution then verifies it
   with `github_integration.resolve_target(path, identity)`.
5. Workspace **id** (slug), **goal** (one line), **max parallel** (default 1),
   then per repository its **id** (default derived from the directory name),
   optional **task title** and optional **objective**. A blank required
   answer cancels.
6. **Destination**, default `<root>/<id>.workspace.yaml` or `--output`; a
   relative answer is relative to the caller's directory. An existing path is
   refused and asked again.
7. Resolution, preview and the approval question.

## Discovery

`os.walk(root, followlinks=False)` over the resolved root, directory names
sorted, so the listing is deterministic. Bounds: directories deeper than
6 levels below the root are not searched, the walk stops after 20000
directories, and at most 256 checkouts are listed; each bound that is hit is
reported. Symlinked directories are never entered.

These directory names are never descended into (and `*.egg-info`):
<!-- pruned -->
`.git`, `.hg`, `.svn`, `node_modules`, `bower_components`, `vendor`, `.venv`,
`venv`, `site-packages`, `.eggs`, `__pycache__`, `.cache`, `.mypy_cache`,
`.pytest_cache`, `.ruff_cache`, `.tox`, `.nox`, `.gradle`, `.terraform`,
`build`, `dist`, `target`, `.next`, `.nuxt`, `.pi`, `.claude`, `.pico`,
`pi-coding-orchestrator`
<!-- pruned -->

`.pi` holds PICO's worktrees, `.claude` Claude Code's, and
`pi-coding-orchestrator` the launcher cache and state.

A directory containing `.git` is a candidate only when all of these hold:

- `.git` is not a symlink;
- `git rev-parse --show-toplevel` is that directory itself (a directory
  inside another checkout is never reported as that checkout);
- its Git directory is its common directory (linked worktrees are reported
  and skipped; submodules and other nested repositories are kept);
- its common directory lies inside the root and was not seen already;
- `resolve_target` finds exactly one GitHub identity (several are listed as
  ambiguous and need the identity choice; none is reported as not
  selectable);
- `HEAD` has a commit (an unborn `HEAD` is reported as not selectable).

Nested repositories outside the pruned names are listed separately. Unreadable
directories (`os.walk` errors, checkouts git cannot read) are reported.

## Non-interactive spec

A builder spec is a separate format, not a manifest. YAML or JSON, read with a
loader that rejects duplicate keys (at any level) and non-string keys.
Unknown fields and wrong types (for example `version: "1"`, `max_parallel:
true`) are rejected.

```yaml
version: 1              # required, the integer 1
root: ..                # required, relative to the spec file's directory
id: evidence-workflow   # required, manifest id slug
goal: Integrate claims and evidence across repositories.   # required, 1-2000 characters
max_parallel: 2         # optional, 1-64, default 1
repositories:           # required, 2-64 entries
  - id: producer
    path: producer                 # an existing checkout root, relative to root
    task:                          # optional
      title: Claim adapter         # optional, 1-200 characters, one line
      objective: Expose the structured claim contract from producer only.   # optional
  - id: consumer
    repository: owner/consumer     # optional for a local checkout, required with expected_path
    expected_path: consumer        # not cloned yet: this path must not exist
```

Each entry has exactly one of `path` and `expected_path`. Repository ids,
paths and GitHub identities (case-insensitive) must be unique, and two local
entries may not share a Git common directory. Without `--approve` the spec
mode prints the preview and exits `0`. `--approve` is only valid with
`--spec`, and `--root` only without it. `--output` is relative to the caller's
directory; saved repository paths are absolute.

## Resolution (read-only)

For a local checkout: the directory must exist, be its own checkout root and
have a common directory; `github_integration.resolve_target(path,
repository)` must succeed, so a checkout with several GitHub remotes and no
`repository` fails with `TARGET_REPOSITORY_AMBIGUOUS` and an identity absent
from the remotes with `TARGET_REPOSITORY_CONFLICT`. A remote URL its parser
rejects (a non-numeric port, a stray `[`) fails that entry with
`WORKSPACE_INIT_REMOTE_MALFORMED` and is listed as not selectable in
discovery; the other repositories are still resolved. The pin is the actual
`HEAD`, which must be a 40-character lowercase hex commit
(`WORKSPACE_INIT_HEAD_UNBORN` otherwise).

For a remote-only entry: `expected_path` must be absent, including a dangling
symlink (`WORKSPACE_INIT_EXPECTED_PATH_EXISTS`).

For every entry, through `tools/github_sync.py`: `gh auth status`, `gh repo
view <identity>` (the answer must name the same repository, compared
case-insensitively; a rename or transfer redirect fails with
`WORKSPACE_REPOSITORY_REDIRECTED`), then `default_branch` and `branch_head`
(`gh api -X GET ... --hostname github.com`). The default branch head must be
a 40-character hex commit. GitHub's spelling of the identity is saved. Missing
`gh` or authentication and timeouts fail with `GITHUB_ACCESS_UNVERIFIED`, a
refused or failed read (denied, missing, network) with
`WORKSPACE_REPOSITORY_ACCESS_DENIED`, an unexpected answer with
`WORKSPACE_INIT_GITHUB_MALFORMED`. All entries are resolved and every failure
is reported; any failure means nothing is saved (exit `1`).

## Generated manifest

`schema_version: 1`, the spec's id, goal and `max_parallel`, one repository
per entry (`repository`, absolute `path`, `pinned_commit`, `base_branch` = the
GitHub default branch) and exactly one planned task per repository with the
same id. No dependencies, contracts, checks or budgets are invented. A missing
title becomes `TODO: define the task for repository <id>` and is listed as a
warning; the workspace goal is never copied into task objectives. The
manifest passes `workspace_plan.validate_manifest`, including its
secret-like-instruction guard.

A local pin is the local `HEAD`, which may not be on GitHub; the preview shows
it next to the GitHub default branch head. A remote-only pin is the default
branch head.

## Preview

The preview shows the destination, the exact YAML that would be saved, the
resolution evidence per repository (identity and how it was chosen, pin and
its source), warnings (placeholder titles, repositories not cloned) and the
planner output, followed by a statement that saving launches nothing.

The planner output is computed in memory from the generated manifest with the
planner's own functions (`validate_manifest`, `resolve_repositories`,
`plan_tasks`, `summarize_tasks`, `render`), never from a temporary file, and
it equals `pico plan --no-access-check` on the saved file. Its access check is
skipped (the builder verified GitHub itself), so a remote-only repository
shows `WORKSPACE_REPOSITORY_PATH_MISSING` and the plan is `BLOCKED` until the
repository is cloned at that path and the pinned commit checked out.

## Terminal display

Directory names, Git remotes, GitHub answers, environment values and
operating system errors are untrusted text. Before such a value is put in a
terminal line (the discovery report, the selection list, prompts and their
defaults, the preview, warnings, the planner output, the saved message and
every error), it is escaped so it cannot add a line, move the cursor, set the
window title or reorder the text:

| Characters | Shown as |
| --- | --- |
| line feed, carriage return, tab | `\n`, `\r`, `\t` |
| other C0 controls (escape included) and DEL | `\x1b`, `\x7f`, ... |
| C1 controls, format characters (bidirectional controls, zero-width characters), line and paragraph separators | `\u009b`, `\u202e`, `\u2028`, ... |
| a filename byte that is not UTF-8 | `\xff`, ... |

Other text, accented and non-Latin letters included, is shown unchanged. Only
the display is escaped: the raw path goes to the filesystem and Git, is
compared and searched (a `/` search matches the raw name or its escaped form)
and is saved in the YAML unchanged. In the preview, a string that would be
escaped is shown as a double-quoted YAML scalar, which reads back as the same
value, and a note says so; the file itself is the exact text `manifest_text`
produces. The preview's own layout (headings, the YAML block, the planner
lines) is not escaped. A command-line error escapes argparse's message, which
quotes the arguments, but not the usage text above it; both go to the
caller's standard error.

Numeric answers (selection numbers and ranges, the identity number, maximum
parallel tasks) are ASCII decimals checked against their range before they are
converted, so a character such as `²` or a decimal too long to convert is
re-asked rather than ending the session.

A checkout whose path Git reports in bytes that are not UTF-8 is listed as not
selectable (and fails resolution in a spec): the GitHub identity check reads
Git output as UTF-8 and the builder does not guess.

## Launchers

`use-latest.sh workspace init ...` is dispatched before the `workspace`
operation and manifest check. `run-local.sh workspace init ...` is dispatched
before the target directory is interpreted; to start Pi in a target directory
literally named `workspace` with a Pi argument `init`, write `./workspace`.
Arguments are passed through quoted, the command runs in the caller's
directory and its exit code is returned unchanged.
