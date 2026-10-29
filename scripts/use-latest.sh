#!/usr/bin/env bash
# Fetch/update pi-coding-orchestrator and use it from any target repository.
# Bootstrap with curl from the public repository (or `gh api` for a private
# fork), then install the short `pico` command for normal use.
#
# Layout:
#   $CACHE_DIR/repo                Git checkout that tracks the remote default
#                                  branch; it hosts this launcher script.
#   $CACHE_DIR/runtimes/<commit>   Immutable snapshot of one orchestrator
#                                  commit. Every command executes from a
#                                  snapshot, so concurrent sessions pinned to
#                                  different commits never change each other's
#                                  code.
#   $STATE_DIR                     Mutable runtime state (provenance, checkpoints,
#                                  missions).

set -euo pipefail

REPO_URL="${PI_CODING_ORCHESTRATOR_REPO_URL:-https://github.com/dhuzard/pico-orchestrator.git}"
REF="${PI_CODING_ORCHESTRATOR_REF:-}"
CACHE_DIR="${PI_CODING_ORCHESTRATOR_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/pi-coding-orchestrator}"
ORCH_REPO="$CACHE_DIR/repo"
RUNTIMES_DIR="$CACHE_DIR/runtimes"
LOCK_DIR="$CACHE_DIR/.lock"
BIN_DIR="${PI_CODING_ORCHESTRATOR_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
STATE_DIR="${PI_CODING_ORCHESTRATOR_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/pi-coding-orchestrator}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pi-coding-orchestrator"
export PI_ORCHESTRATOR_STATE_DIR="$STATE_DIR"
# Snapshots are shared by concurrent sessions; never write bytecode into them.
export PYTHONDONTWRITEBYTECODE=1
RESOLVED_SHA=""
RUNTIME=""
LOCK_HELD=false
# Set when REF came from a target's .pi/orchestrator.lock (untrusted input)
# rather than from the user (PI_CODING_ORCHESTRATOR_REF or `pin <target> <ref>`).
PIN_SOURCE=""
PIN_LOCK=""
PIN_TARGET=""
PIN_APPROVED=false
# doctor diagnoses with the default branch instead of failing on a rejected pin.
PIN_FALLBACK=false

usage() {
  cat <<'EOF'
Usage:
  use-latest.sh install
  use-latest.sh uninstall [--purge-cache] [--purge-state] [--purge-config]
  use-latest.sh update [target-repo]
  use-latest.sh reset-cache --force
  use-latest.sh path
  use-latest.sh state-path
  use-latest.sh pin [target-repo] [ref]
  use-latest.sh unpin [target-repo]
  use-latest.sh init|setup [target-repo] [--copy-extension] [--github-templates] [--overwrite]
  use-latest.sh run [target-repo] [--repo owner/repository] [-- pi-args...]
  use-latest.sh watch [target-repo] [--repo owner/repository | --all | --workspace <id|manifest> [--controls]] [--port 8765]
  use-latest.sh plan <workspace-manifest> [--json] [--no-access-check]
  use-latest.sh workspace init [--root DIR] [--output FILE] | --spec FILE [--output FILE] [--approve]
  use-latest.sh workspace validate|publish|ci|promote <workspace-manifest> --task <id> [--head pico/<branch>] [--apply --plan-hash <hash>] [--new-branch]
  use-latest.sh workspace schedule <workspace-manifest> [--max-parallel N] [--allow-mutation] [--resume-interrupted] [--json] [--no-access-check]
  use-latest.sh workspace launch|status|cancel <workspace-manifest> [--task <id>] [--allow-mutation] [--resume] [--no-wait] [--ignore-budget] [--no-access-check]
  use-latest.sh workspace preflight <workspace-manifest> [--allow-mutation] [--json] [--no-access-check]
  use-latest.sh exec start|status|cancel --repo owner/repository --execution-id <id> [--target repo] [--objective ...] [--allow-mutation] [--resume] [--no-wait] [--max-duration SECONDS] [--max-lead-calls N] [--lead-calls-warn-at N]
  use-latest.sh preflight|doctor [target-repo] [--json]
  use-latest.sh sync [target-repo] [--copy-extension] [--github-templates] [--overwrite]
  use-latest.sh models show|validate|set|reset [...]
  use-latest.sh prompt
  use-latest.sh report --title "..." --body-file report.md [--apply --plan-hash <hash>]

Environment:
  PI_CODING_ORCHESTRATOR_REPO_URL  Git URL to clone/pull.
  PI_CODING_ORCHESTRATOR_REF       Branch/tag/commit to checkout (default: remote default branch).
  PI_CODING_ORCHESTRATOR_HOME      Cache dir (default: ~/.cache/pi-coding-orchestrator).
  PI_CODING_ORCHESTRATOR_BIN_DIR   Install dir for pico (default: ~/.local/bin).
  PI_CODING_ORCHESTRATOR_STATE_DIR Persistent runtime state dir (default: ~/.local/state/pi-coding-orchestrator).
  PI_ORCHESTRATOR_MODEL_CONFIG      User-owned model config path (default: ~/.config/pi-coding-orchestrator/models.yaml when present).
  PI_TARGET_REPOSITORY              Explicit parent target; must match a target GitHub remote.
  PI_CODING_ORCHESTRATOR_ALLOW_UNREVIEWED_PIN
                                    1 runs a project pin that is neither on the remote default
                                    branch nor in a tag, and was not approved with `pin` (warns).

Examples:
  ./scripts/use-latest.sh install
  pico setup .
  pico run .
  pico doctor .
  pico doctor . --json
  pico workspace init               # pick repositories, preview, save a new manifest on approval
  pico plan workspace.yaml          # read-only multi-repository plan
  pico watch . --workspace workspace.yaml   # browser view of one workspace across repositories
  pico watch . --workspace workspace.yaml --controls   # plus confirmed actions: validate, request PR, CI, promote
  pico exec start --repo o/r --execution-id nightly-1 --objective "..."   # headless bounded run
  pico sync .
  pico pin .
  pico models show                 # light, economy, standard, high, max
  pico models set codex high openai-codex/gpt-6.1-sol
  pico report --title "PICO bug" --body-file report.md   # preview issue in PICO repository
  PI_CODING_ORCHESTRATOR_REF=v1.2.3 pico run ../my-project -- --model claude
EOF
}

# -- cache lock ---------------------------------------------------------------
# mkdir is atomic on every supported platform (flock is absent on macOS). A
# lock whose owner process has exited is reclaimed.

acquire_lock() {
  [[ "$LOCK_HELD" == true ]] && return 0
  mkdir -p "$CACHE_DIR"
  local waited=0
  until mkdir "$LOCK_DIR" 2>/dev/null; do
    local owner
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
      rm -rf "$LOCK_DIR"
      continue
    fi
    if (( waited >= 300 )); then
      echo "Timed out waiting for orchestrator cache lock: $LOCK_DIR" >&2
      return 1
    fi
    sleep 0.2
    waited=$((waited + 1))
  done
  echo "$$" > "$LOCK_DIR/pid"
  LOCK_HELD=true
  trap release_lock EXIT
}

release_lock() {
  if [[ "$LOCK_HELD" == true ]]; then
    rm -rf "$LOCK_DIR"
    LOCK_HELD=false
  fi
}

# -- project pin --------------------------------------------------------------

project_lock_path() {
  local target="${1:-.}"
  printf '%s\n' "$target/.pi/orchestrator.lock"
}

read_project_pin() {
  local target="${1:-.}"
  local lock
  lock="$(project_lock_path "$target")"
  [[ -f "$lock" ]] || return 1
  local value
  value="$(tr -d '[:space:]' < "$lock")"
  if [[ ! "$value" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "Invalid orchestrator lock: $lock" >&2
    return 2
  fi
  printf '%s\n' "$value"
}

apply_project_pin() {
  local target="${1:-.}"
  if [[ -n "$REF" ]]; then
    return 0
  fi
  local pin
  if pin="$(read_project_pin "$target")"; then
    REF="$pin"
    PIN_SOURCE=project
    PIN_TARGET="$target"
    PIN_LOCK="$(project_lock_path "$target")"
    check_pin_approval "$target" "$pin"
    echo "pi-coding-orchestrator: using project pin $pin" >&2
  else
    local rc=$?
    if [[ "$rc" -eq 2 ]]; then
      return 1
    fi
  fi
}

write_project_pin() {
  local target="$1"
  local sha="$2"
  local lock
  lock="$(project_lock_path "$target")"
  mkdir -p "$(dirname "$lock")"
  local tmp
  tmp="$(mktemp "${lock}.tmp.XXXXXX")"
  printf '%s\n' "$sha" > "$tmp"
  mv "$tmp" "$lock"
  echo "Pinned orchestrator: $lock -> $sha"
}

# -- pin approval and review ----------------------------------------------------
# A target repository chooses its pin, so the launcher remembers, per target
# checkout, the last pin the user approved (`pin` or `update`), warns when the
# lock names another commit, and runs an unapproved pin only when it is on the
# remote default branch or in a tag. Signature verification of release tags is
# not implemented.

# State file holding the last approved pin of a target checkout (keyed by its
# physical path).
pin_approval_path() {
  local root
  root="$(cd -P "$1" 2>/dev/null && pwd)" || return 1
  printf '%s/approved-pins/%s\n' "$STATE_DIR" "$(printf '%s' "$root" | git hash-object --stdin)"
}

record_pin_approval() {
  local target="$1" sha="$2" path root tmp
  path="$(pin_approval_path "$target")" || { echo "Cannot record pin approval for $target" >&2; return 1; }
  root="$(cd -P "$target" && pwd)"
  mkdir -p "$(dirname "$path")"
  tmp="$(mktemp "${path}.tmp.XXXXXX")"
  printf '%s\t%s\n' "$sha" "$root" > "$tmp"
  mv "$tmp" "$path"
}

# Sets PIN_APPROVED and warns (non-fatal) when the pin is not the one last approved.
check_pin_approval() {
  local target="$1" pin="$2" path approved=""
  PIN_APPROVED=false
  if path="$(pin_approval_path "$target")" && [[ -f "$path" ]]; then
    approved="$(cut -f1 "$path" | head -n 1)"
  fi
  if [[ "$approved" == "$pin" ]]; then
    PIN_APPROVED=true
  elif [[ -n "$approved" ]]; then
    echo "pi-coding-orchestrator: warning: project pin changed from ${approved:0:12} (last approved) to ${pin:0:12} in $(project_lock_path "$target"); review it, then run 'pico pin $target $pin' to approve it" >&2
  else
    echo "pi-coding-orchestrator: warning: project pin ${pin:0:12} in $(project_lock_path "$target") has not been approved on this machine; review it, then run 'pico pin $target $pin' to approve it" >&2
  fi
}

refresh_default_head() {
  git -C "$ORCH_REPO" remote set-head origin --auto >/dev/null 2>&1 || true
}

# True when a release can contain the commit: it is an ancestor of the remote
# default branch, or a tag contains it. Uses local refs only (no network).
pin_on_release_history() {
  local sha="$1" default
  default="$(git -C "$ORCH_REPO" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -n "$default" ]] && git -C "$ORCH_REPO" merge-base --is-ancestor "$sha" "$default" 2>/dev/null; then
    return 0
  fi
  [[ -n "$(git -C "$ORCH_REPO" tag --contains "$sha" 2>/dev/null || true)" ]]
}

# Decide whether a project pin may run: 0 run it, 1 refuse, 3 refuse but let
# doctor fall back to the default branch. The check needs only local refs when
# they already show the pin on release history, so a cached reviewed pin keeps
# running offline; otherwise remote refs are refreshed once (`fetched` says
# whether ensure_repo already fetched) and an unverifiable pin is refused.
review_project_pin() {
  local sha="$1" fetched="$2" detail=""
  [[ "$PIN_SOURCE" == project && "$PIN_APPROVED" != true ]] || return 0
  pin_on_release_history "$sha" && return 0
  if [[ "$fetched" != true ]] && ! git -C "$ORCH_REPO" fetch -q --tags --prune origin; then
    detail=" (remote refs could not be refreshed from $REPO_URL, so it could not be checked again)"
  else
    refresh_default_head
    pin_on_release_history "$sha" && return 0
  fi
  if [[ "${PI_CODING_ORCHESTRATOR_ALLOW_UNREVIEWED_PIN:-}" == 1 ]]; then
    echo "pi-coding-orchestrator: warning: running project pin $sha from $PIN_LOCK although it is not on release history (PI_CODING_ORCHESTRATOR_ALLOW_UNREVIEWED_PIN=1)" >&2
    return 0
  fi
  echo "PIN_NOT_ON_RELEASE_HISTORY: project pin $sha from $PIN_LOCK is neither an ancestor of the remote default branch nor contained in a tag$detail. Refusing to run it. Review the commit, then approve it for this repository with 'pico pin $PIN_TARGET $sha', or set PI_CODING_ORCHESTRATOR_ALLOW_UNREVIEWED_PIN=1 for one run." >&2
  [[ "$PIN_FALLBACK" == true ]] && return 3
  return 1
}

# ensure_repo for a resolved project pin: continue, fail, or redo with the default branch.
apply_pin_review() {
  local review=0
  review_project_pin "$RESOLVED_SHA" "$1" || review=$?
  case "$review" in
    0) return 0 ;;
    3)
      echo "pi-coding-orchestrator: ignoring rejected project pin for diagnosis" >&2
      REF=""
      PIN_SOURCE=""
      return 3
      ;;
    *) return 1 ;;
  esac
}

# -- cache and runtimes -------------------------------------------------------

migrate_legacy_provenance() {
  [[ -d "$ORCH_REPO/.git" ]] || return 0
  mkdir -p "$STATE_DIR/runs"
  while IFS= read -r -d '' untracked_path; do
    case "$untracked_path" in
      provenance/runs/run-*.json)
        local src="$ORCH_REPO/$untracked_path"
        local dst="$STATE_DIR/runs/${untracked_path##*/}"
        if [[ ! -f "$src" || -L "$src" ]]; then
          echo "Refusing unsafe legacy provenance path: $src" >&2
          return 1
        fi
        if [[ -e "$dst" ]]; then
          if ! cmp -s "$src" "$dst"; then
            echo "Refusing provenance migration collision: $dst" >&2
            return 1
          fi
          rm "$src"
        else
          mv "$src" "$dst"
        fi
        ;;
    esac
  done < <(git -C "$ORCH_REPO" ls-files --others --exclude-standard -z -- provenance/runs)
}

is_full_sha() {
  [[ "$1" =~ ^[0-9a-fA-F]{40}$ ]]
}

# Resolve REF (default: the remote default branch) to RESOLVED_SHA and make sure
# its runtime snapshot exists. Only default-branch resolution moves the cache
# working tree (which hosts this launcher); explicit refs and project pins are
# served from snapshots without touching it.
ensure_repo() {
  acquire_lock || return 1
  mkdir -p "$CACHE_DIR"
  if [[ ! -e "$ORCH_REPO" ]]; then
    git clone "$REPO_URL" "$ORCH_REPO"
  elif [[ ! -d "$ORCH_REPO/.git" ]]; then
    echo "Refusing to use non-Git cache path: $ORCH_REPO" >&2
    return 1
  fi

  # Mutable runtime provenance belongs in STATE_DIR, not the Git checkout.
  # Migrate legacy untracked run records once, then require a strictly clean cache.
  migrate_legacy_provenance || return 1
  if ! git -C "$ORCH_REPO" diff --quiet || ! git -C "$ORCH_REPO" diff --cached --quiet; then
    echo "Refusing to update dirty orchestrator cache: $ORCH_REPO" >&2
    return 1
  fi
  if [[ -n "$(git -C "$ORCH_REPO" ls-files --others --exclude-standard)" ]]; then
    echo "Refusing to update dirty orchestrator cache: $ORCH_REPO" >&2
    return 1
  fi
  actual_url="$(git -C "$ORCH_REPO" remote get-url origin)"
  if [[ "$actual_url" != "$REPO_URL" ]]; then
    echo "Cache origin mismatch at $ORCH_REPO (expected $REPO_URL)" >&2
    return 1
  fi

  ref="$REF"
  # A pinned commit that is already cached needs no network access.
  if is_full_sha "$ref" && git -C "$ORCH_REPO" cat-file -e "$ref^{commit}" 2>/dev/null; then
    RESOLVED_SHA="$(git -C "$ORCH_REPO" rev-parse "$ref^{commit}")"
    local review=0
    apply_pin_review false || review=$?
    if [[ "$review" -eq 3 ]]; then ensure_repo; return; fi
    [[ "$review" -eq 0 ]] || return 1
    echo "pi-coding-orchestrator: using ${RESOLVED_SHA:0:12} (cached, no fetch)" >&2
    materialize_runtime
    return
  fi

  if ! git -C "$ORCH_REPO" fetch --tags --prune origin; then
    if is_full_sha "$ref"; then
      echo "Cannot fetch $REPO_URL and pinned commit $ref is not cached" >&2
    else
      echo "Cannot fetch $REPO_URL; pin a cached commit (pico pin) to run offline" >&2
    fi
    return 1
  fi

  local track_default=false
  if [[ -z "$ref" ]]; then
    track_default=true
    # Track the remote's *current* default branch, not the one recorded at clone time.
    git -C "$ORCH_REPO" remote set-head origin --auto >/dev/null 2>&1 || true
    ref="$(git -C "$ORCH_REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    ref="${ref#origin/}"
    if [[ -z "$ref" ]] || ! git -C "$ORCH_REPO" rev-parse --verify --quiet "origin/$ref" >/dev/null; then
      ref="$(git -C "$ORCH_REPO" remote show origin | awk '/HEAD branch:/ {print $NF; exit}')"
    fi
    if [[ -z "$ref" ]]; then
      echo "Could not determine remote default branch; set PI_CODING_ORCHESTRATOR_REF" >&2
      return 1
    fi
  fi

  if git -C "$ORCH_REPO" rev-parse --verify --quiet "origin/$ref" >/dev/null; then
    target_rev="origin/$ref"
  else
    target_rev="$ref"
  fi
  if ! git -C "$ORCH_REPO" rev-parse --verify --quiet "$target_rev^{commit}" >/dev/null; then
    echo "Orchestrator ref not found after fetch: $ref" >&2
    return 1
  fi
  RESOLVED_SHA="$(git -C "$ORCH_REPO" rev-parse "$target_rev^{commit}")"
  if [[ "$PIN_SOURCE" == project ]]; then
    local pin_review=0
    apply_pin_review true || pin_review=$?
    if [[ "$pin_review" -eq 3 ]]; then ensure_repo; return; fi
    [[ "$pin_review" -eq 0 ]] || return 1
  fi
  if [[ "$track_default" == true ]]; then
    git -C "$ORCH_REPO" checkout -q "$ref" 2>/dev/null || git -C "$ORCH_REPO" checkout -q -B "$ref" "origin/$ref"
    git -C "$ORCH_REPO" reset --hard -q "origin/$ref"
    # Never run a silently stale installation.
    actual="$(git -C "$ORCH_REPO" rev-parse HEAD)"
    if [[ "$RESOLVED_SHA" != "$actual" ]]; then
      echo "Orchestrator cache is at $actual, expected $RESOLVED_SHA for $ref" >&2
      return 1
    fi
  fi
  echo "pi-coding-orchestrator: using $ref @ ${RESOLVED_SHA:0:12} ($ORCH_REPO)" >&2
  materialize_runtime
}

materialize_runtime() {
  RUNTIME="$RUNTIMES_DIR/$RESOLVED_SHA"
  if [[ ! -f "$RUNTIME/.pico-runtime" ]]; then
    mkdir -p "$RUNTIMES_DIR"
    rm -rf "$RUNTIME"
    local tmp
    tmp="$(mktemp -d "$RUNTIMES_DIR/.tmp.XXXXXX")"
    if ! git -C "$ORCH_REPO" archive --format=tar "$RESOLVED_SHA" | tar -x -C "$tmp"; then
      rm -rf "$tmp"
      echo "Could not materialize orchestrator runtime $RESOLVED_SHA" >&2
      return 1
    fi
    printf '%s\n' "$RESOLVED_SHA" > "$tmp/.pico-runtime"
    mv "$tmp" "$RUNTIME"
  fi
  release_lock
}

activate_model_config() {
  local model_config
  model_config="$(python3 "$RUNTIME/tools/model_profiles.py" path)" || return 1
  if [[ -n "${PI_ORCHESTRATOR_MODEL_CONFIG:-}" && ! -f "$model_config" ]]; then
    echo "Configured model runtime file does not exist: $model_config" >&2
    return 1
  fi
  if [[ -f "$model_config" ]]; then
    export PI_ORCHESTRATOR_MODEL_CONFIG="$model_config"
    python3 "$RUNTIME/tools/model_profiles.py" --config "$model_config" validate >/dev/null || return 1
    echo "pi-coding-orchestrator: model config $model_config" >&2
  fi
}

preflight_target() {
  local target="${1:-.}"
  shift || true
  # Doctor exists to diagnose broken configuration: report it, do not abort.
  activate_model_config || echo "pi-coding-orchestrator: model configuration is invalid; see doctor report" >&2
  python3 "$RUNTIME/tools/doctor.py" "$target" "$@"
}

# Refuse to delete a directory that is, or contains, the user's home.
safe_to_purge() {
  local path="$1"
  [[ -n "$path" && "$path" != "/" ]] || return 1
  local resolved="$path"
  if [[ -d "$path" ]]; then resolved="$(cd -P "$path" && pwd)"; fi
  local home
  home="$(cd -P "$HOME" 2>/dev/null && pwd || printf '%s' "$HOME")"
  [[ "$resolved" != "/" && "$resolved" != "$home" && "$home/" != "$resolved/"* ]]
}

reset_cache() {
  local force="${1:-}"
  if [[ "$force" != "--force" ]]; then
    echo "reset-cache is destructive to the cached code checkout; pass --force" >&2
    return 2
  fi
  if ! safe_to_purge "$CACHE_DIR" || [[ "$ORCH_REPO" != "$CACHE_DIR/repo" ]]; then
    echo "Refusing unsafe cache path: $ORCH_REPO" >&2
    return 1
  fi
  acquire_lock
  # Clone first and swap afterwards: a failed clone keeps the working launcher.
  local tmp
  tmp="$(mktemp -d "$CACHE_DIR/.reset.XXXXXX")"
  if ! git clone -q "$REPO_URL" "$tmp/repo"; then
    rm -rf "$tmp"
    echo "Clone failed; kept existing cache: $ORCH_REPO" >&2
    return 1
  fi
  if [[ -e "$ORCH_REPO" ]]; then mv "$ORCH_REPO" "$tmp/old"; fi
  mv "$tmp/repo" "$ORCH_REPO"
  rm -rf "$tmp"
  ensure_repo
  echo "Reset cache: $ORCH_REPO"
}

uninstall_pico() {
  local purge_cache=false purge_state=false purge_config=false
  for arg in "$@"; do
    case "$arg" in
      --purge-cache) purge_cache=true ;;
      --purge-state) purge_state=true ;;
      --purge-config) purge_config=true ;;
      *) echo "Unknown uninstall option: $arg" >&2; return 2 ;;
    esac
  done
  local name path
  for name in cache state config; do
    eval "local wanted=\$purge_$name"
    case "$name" in cache) path="$CACHE_DIR" ;; state) path="$STATE_DIR" ;; config) path="$CONFIG_DIR" ;; esac
    if [[ "$wanted" == true ]] && ! safe_to_purge "$path"; then
      echo "Refusing to purge unsafe $name path: $path" >&2
      return 1
    fi
  done
  local wrapper="$BIN_DIR/pico"
  if [[ -e "$wrapper" || -L "$wrapper" ]]; then
    rm -f "$wrapper"
    echo "Removed: $wrapper"
  else
    echo "Already absent: $wrapper"
  fi
  if [[ "$purge_cache" == true ]]; then rm -rf "$CACHE_DIR"; echo "Removed cache: $CACHE_DIR"; fi
  if [[ "$purge_state" == true ]]; then rm -rf "$STATE_DIR"; echo "Removed state: $STATE_DIR"; fi
  if [[ "$purge_config" == true ]]; then rm -rf "$CONFIG_DIR"; echo "Removed config: $CONFIG_DIR"; fi
}

cmd="${1:-}"
if [[ -z "$cmd" || "$cmd" == "-h" || "$cmd" == "--help" ]]; then
  usage
  exit 0
fi
shift || true

case "$cmd" in
  install)
    ensure_repo
    mkdir -p "$BIN_DIR"
    wrapper="$BIN_DIR/pico"
    tmp="$(mktemp "${wrapper}.tmp.XXXXXX")"
    {
      echo '#!/usr/bin/env bash'
      echo 'set -euo pipefail'
      printf 'DEFAULT_REPO_URL=%q\n' "$REPO_URL"
      printf 'DEFAULT_CACHE_DIR=%q\n' "$CACHE_DIR"
      printf 'DEFAULT_STATE_DIR=%q\n' "$STATE_DIR"
      cat <<'EOF'
export PI_CODING_ORCHESTRATOR_REPO_URL="${PI_CODING_ORCHESTRATOR_REPO_URL:-$DEFAULT_REPO_URL}"
export PI_CODING_ORCHESTRATOR_HOME="${PI_CODING_ORCHESTRATOR_HOME:-$DEFAULT_CACHE_DIR}"
export PI_CODING_ORCHESTRATOR_STATE_DIR="${PI_CODING_ORCHESTRATOR_STATE_DIR:-$DEFAULT_STATE_DIR}"
exec bash "$PI_CODING_ORCHESTRATOR_HOME/repo/scripts/use-latest.sh" "$@"
EOF
    } > "$tmp"
    chmod 0755 "$tmp"
    mv "$tmp" "$wrapper"
    echo "Installed: $wrapper"
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
      echo "Add $BIN_DIR to PATH to call 'pico' directly." >&2
    fi
    ;;
  uninstall)
    uninstall_pico "$@"
    ;;
  update)
    target="${1:-.}"
    # Explicit update advances an existing project lock to the resolved update ref.
    ensure_repo
    if [[ -f "$(project_lock_path "$target")" ]]; then
      write_project_pin "$target" "$RESOLVED_SHA"
      record_pin_approval "$target" "$RESOLVED_SHA"
    fi
    echo "Updated: $ORCH_REPO"
    printf '%s\n' "${RESOLVED_SHA:0:7}"
    ;;
  reset-cache)
    reset_cache "${1:-}"
    ;;
  path)
    ensure_repo
    echo "$RUNTIME"
    ;;
  state-path)
    mkdir -p "$STATE_DIR"
    echo "$STATE_DIR"
    ;;
  pin)
    target="${1:-.}"
    if [[ $# -gt 0 ]]; then shift; fi
    requested="${1:-}"
    if [[ -n "$requested" ]]; then REF="$requested"; fi
    ensure_repo
    if ! pin_on_release_history "$RESOLVED_SHA"; then
      echo "pi-coding-orchestrator: note: $RESOLVED_SHA is neither on the remote default branch nor in a tag; it is approved for $target only" >&2
    fi
    write_project_pin "$target" "$RESOLVED_SHA"
    # Pinning is the user's approval of this commit for this target checkout.
    record_pin_approval "$target" "$RESOLVED_SHA"
    ;;
  unpin)
    target="${1:-.}"
    lock="$(project_lock_path "$target")"
    if [[ -f "$lock" ]]; then rm -f "$lock"; echo "Removed pin: $lock"; else echo "No project pin: $lock"; fi
    ;;
  init|setup)
    target="${1:-.}"
    if [[ $# -gt 0 ]]; then shift; fi
    apply_project_pin "$target"
    ensure_repo
    python3 "$RUNTIME/tools/init_project.py" "$target" "$@"
    ;;
  preflight|doctor)
    target="."
    if [[ $# -gt 0 && "$1" != "--json" ]]; then target="$1"; shift; fi
    # An invalid or rejected pin is one of the things doctor reports, so keep going.
    PIN_FALLBACK=true
    if ! apply_project_pin "$target"; then
      echo "pi-coding-orchestrator: ignoring invalid project pin for diagnosis" >&2
    fi
    ensure_repo
    preflight_target "$target" "$@"
    ;;
  sync)
    target="${1:-.}"
    if [[ $# -gt 0 ]]; then shift; fi
    apply_project_pin "$target"
    ensure_repo
    python3 "$RUNTIME/tools/init_project.py" "$target" "$@"
    preflight_target "$target"
    ;;
  models)
    ensure_repo
    python3 "$RUNTIME/tools/model_profiles.py" "$@"
    ;;
  run)
    target=""
    pi_args=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --repo)
          if [[ $# -lt 2 || -z "$2" ]]; then
            echo "run --repo requires owner/repository" >&2
            exit 2
          fi
          export PI_TARGET_REPOSITORY="$2"
          shift 2
          ;;
        --) shift; pi_args+=("$@"); break ;;
        -*) pi_args+=("$1"); shift ;;
        *)
          if [[ -z "$target" ]]; then target="$1"; else pi_args+=("$1"); fi
          shift
          ;;
      esac
    done
    target="${target:-.}"
    if [[ ! -d "$target" ]]; then
      echo "Target repository directory does not exist: $target" >&2
      exit 2
    fi
    apply_project_pin "$target"
    ensure_repo
    activate_model_config
    cd "$target"
    # Pin the exact Claude executable before Pi loads extensions; routed
    # preflight fails closed (BACKEND_IDENTITY_UNINITIALIZED) without it.
    backend_identity="$(python3 "$RUNTIME/tools/claude_backend.py" "$PWD")"
    export PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY="$backend_identity"
    exec pi -e "$RUNTIME/extensions/model-router.ts" -e "$RUNTIME/extensions/workflow-commands.ts" -e "$RUNTIME/extensions/mission-controller.ts" -e "$RUNTIME/extensions/orchestration-lifecycle.ts" ${pi_args[@]+"${pi_args[@]}"}
    ;;
  watch)
    target="."
    if [[ $# -gt 0 && "$1" != --* ]]; then target="$1"; shift; fi
    apply_project_pin "$target"
    ensure_repo
    exec python3 "$RUNTIME/tools/watch.py" "$target" --state-dir "$STATE_DIR" "$@"
    ;;
  plan)
    # Read-only: validates a workspace manifest and prints execution waves.
    if [[ $# -lt 1 || "$1" == --* ]]; then
      echo "plan requires a workspace manifest path" >&2
      exit 2
    fi
    manifest="$1"; shift
    ensure_repo
    exec python3 "$RUNTIME/tools/workspace_plan.py" "$manifest" "$@"
    ;;
  workspace)
    # `workspace init` builds a new manifest, so it takes none: dispatch it
    # before the manifest check. It reads no project pin and activates no model
    # config or backend; it runs in the caller's directory.
    if [[ "${1:-}" == "init" ]]; then
      shift
      ensure_repo
      exec python3 "$RUNTIME/tools/workspace_init.py" "$@"
    fi
    # One workspace task -> one repository-scoped headless child run.
    if [[ $# -lt 2 || "$1" == --* ]]; then
      echo "workspace requires init, or schedule|preflight|launch|status|cancel|validate|publish|ci|promote and a manifest path" >&2
      exit 2
    fi
    op="$1"; manifest="$2"; shift 2
    ensure_repo
    activate_model_config
    if [[ "$op" == "schedule" ]]; then
      exec python3 "$RUNTIME/tools/workspace_scheduler.py" "$manifest" "$@"
    fi
    if [[ "$op" == "validate" || "$op" == "publish" || "$op" == "ci" || "$op" == "promote" ]]; then
      exec python3 "$RUNTIME/tools/workspace_contracts.py" "$op" "$manifest" "$@"
    fi
    exec python3 "$RUNTIME/tools/workspace_run.py" "$op" "$manifest" "$@"
    ;;
  exec)
    # Headless bounded run: one Pi print-mode process with the orchestrator
    # extensions; the lifecycle guards and autonomy policy still apply.
    if [[ $# -lt 1 || "$1" == --* ]]; then
      echo "exec requires start, status or cancel" >&2
      exit 2
    fi
    op="$1"; shift
    target="."
    exec_args=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --target)
          if [[ $# -lt 2 || -z "$2" ]]; then echo "exec --target requires a path" >&2; exit 2; fi
          target="$2"; shift 2 ;;
        *) exec_args+=("$1"); shift ;;
      esac
    done
    if [[ ! -d "$target" ]]; then
      echo "Target repository directory does not exist: $target" >&2
      exit 2
    fi
    apply_project_pin "$target"
    ensure_repo
    activate_model_config
    exec python3 "$RUNTIME/tools/headless_run.py" "$op" --target "$target" ${exec_args[@]+"${exec_args[@]}"}
    ;;
  prompt)
    ensure_repo
    cat "$RUNTIME/prompts/orchestrate-core.md"
    ;;
  report)
    ensure_repo
    exec python3 "$RUNTIME/tools/report_pico_issue.py" "$@"
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
