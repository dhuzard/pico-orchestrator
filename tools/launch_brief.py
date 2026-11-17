"""A compact, factual launch snapshot; orchestration guards remain authoritative."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def create(*, worktree: Path, repository: str, common_dir: Path, session_id: str,
           executable: str, python: str, mutation_permissions: list[str]) -> str:
    def git(*args):
        result = subprocess.run(['git', '-C', str(worktree), *args], capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else None

    head = git('rev-parse', '--verify', 'HEAD^{commit}')
    policy = ROOT / 'routing' / 'default-policy.yaml'
    data = {
        'version': 1,
        'target_repository_verified': repository,
        'checkout': str(worktree),
        'git_common_dir': str(common_dir),
        'base_commit_observed': head,
        'base_branch_observed': git('symbolic-ref', '--quiet', '--short', 'HEAD'),
        'session_id': session_id,
        'runtime_executable': shutil.which(executable),
        'python_executable': shutil.which(python),
        'routing_policy': str(policy),
        'routing_policy_sha256': hashlib.sha256(policy.read_bytes()).hexdigest(),
        'mutation_permissions_observed': mutation_permissions,
        'reference': str(ROOT / 'prompts' / 'orchestrate.md'),
        'publication_tool': str(ROOT / 'tools' / 'github_integration.py'),
        'worktree_plan': 'Not yet registered; create and bind only after route preflight READY.',
        'branch_name_pattern': f'pico/{session_id}/batch-<n>',
    }
    sequence = (
        'Call orchestration_record status; inspect target instructions and classify each task; '
        'call route_task and honor its backend preflight. For mutation, prepare and bind an external '
        'worktree from the verified base after preflight; declare a wave only for concurrent tasks '
        'with disjoint scopes. Run routed steps with their required independent review. Validate, '
        'integrate_begin, merge and check in an external integration worktree, then integrate_end. '
        'Record completed phases; when publication is authorized, dry-run and apply the reviewed '
        'publication plan with the parent run ID. Finish with the observed outcome. '
        'All lifecycle, scope, policy, review and publication guards remain mandatory; '
        'the snapshot is not authorization or evidence of a registered worktree.'
    )
    return 'PICO_LAUNCH_BRIEF_V1\n' + json.dumps(data, sort_keys=True) + '\nRequired sequence: ' + sequence + '\n\n'


def benchmark_selection(env: dict, revision: str | None) -> dict | None:
    """Explicit benchmark-only prompt treatment; never modifies guard behavior."""
    names = ("PICO_BENCHMARK_VARIANT", "PICO_BENCHMARK_PAIR", "PICO_BENCHMARK_REVISION",
             "PICO_BENCHMARK_LEAD_MODEL", "PICO_BENCHMARK_LEAD_EFFORT")
    if not any(name in env for name in names):
        return None
    variant, pair, pin, model, effort = (env.get(name, "") for name in names)
    if (env.get("PICO_REAL_BENCHMARK") != "1" or variant not in {"baseline", "candidate"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", pair)
            or not re.fullmatch(r"[0-9a-f]{40}", pin) or pin != revision
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model)
            or effort not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}):
        raise ValueError("benchmark selectors require explicit opt-in, variant, pair and matching full runtime revision")
    return {"variant": variant, "pair": pair, "revision": pin, "lead_model": model, "lead_effort": effort}
