#!/usr/bin/env bash
set -euo pipefail
ORCH_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# `run-local.sh workspace init [...]` builds a workspace manifest in the
# caller's directory; nothing below (target, model config, backend, Pi) applies.
# A target directory literally named `workspace` followed by a Pi argument
# `init` is written ./workspace.
if [[ "${1:-}" == "workspace" && "${2:-}" == "init" ]]; then
  shift 2
  export PYTHONDONTWRITEBYTECODE=1
  exec python3 "$ORCH_REPO/tools/workspace_init.py" "$@"
fi
target="${1:-.}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "${1:-}" == "--repo" ]]; then
  if [[ $# -lt 2 || -z "$2" ]]; then
    echo "run-local --repo requires owner/repository" >&2
    exit 2
  fi
  export PI_TARGET_REPOSITORY="$2"
  shift 2
fi
if [[ "${1:-}" == "--" ]]; then shift; fi
cd "$target"
# A vendored bundle (.pi/ inside the target) has no Git checkout of its own;
# keep its provenance in the user state directory instead of the target tree.
if [[ -z "${PI_ORCHESTRATOR_STATE_DIR:-}" && ! -e "$ORCH_REPO/.git" ]]; then
  export PI_ORCHESTRATOR_STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/pi-coding-orchestrator"
fi
export PYTHONDONTWRITEBYTECODE=1
if [[ -e "$ORCH_REPO/.git" ]]; then
  revision="$(git -C "$ORCH_REPO" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  if [[ -n "$(git -C "$ORCH_REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    revision="$revision+dirty"
  fi
else
  revision="vendored bundle"
fi
echo "pi-coding-orchestrator: using local checkout @ $revision ($ORCH_REPO)" >&2
model_config="$(python3 "$ORCH_REPO/tools/model_profiles.py" path)"
if [[ -n "${PI_ORCHESTRATOR_MODEL_CONFIG:-}" && ! -f "$model_config" ]]; then
  echo "Configured model runtime file does not exist: $model_config" >&2
  exit 1
fi
if [[ -f "$model_config" ]]; then
  export PI_ORCHESTRATOR_MODEL_CONFIG="$model_config"
  python3 "$ORCH_REPO/tools/model_profiles.py" --config "$model_config" validate >/dev/null
  echo "pi-coding-orchestrator: model config $model_config" >&2
fi
# Pin the exact Claude executable before Pi loads extensions; routed
# preflight fails closed (BACKEND_IDENTITY_UNINITIALIZED) without it.
backend_identity="$(python3 "$ORCH_REPO/tools/claude_backend.py" "$PWD")"
export PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY="$backend_identity"
exec pi -e "$ORCH_REPO/extensions/model-router.ts" -e "$ORCH_REPO/extensions/workflow-commands.ts" -e "$ORCH_REPO/extensions/mission-controller.ts" -e "$ORCH_REPO/extensions/orchestration-lifecycle.ts" "$@"
