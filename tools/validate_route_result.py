#!/usr/bin/env python3
"""Thin JSON-over-stdio validator for route_task runtime results.

This exists for the Pi TypeScript extension boundary: it lets the extension
fail closed on an ok:true result whose execution_plan is structurally or
semantically invalid without re-implementing plan semantics in TypeScript.

stdin:  {"result": {...}}
stdout: {"ok": true, "errors": []} or {"ok": false, "errors": [...]}
exit 0 for completed validation; nonzero only for malformed stdin/bootstrap
failures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_engine as engine  # noqa: E402


def _fail(message: str) -> NoReturn:
    print(f"validate_route_result: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        _fail(f"malformed stdin: not valid JSON: {exc}")
    if not isinstance(payload, dict) or "result" not in payload:
        _fail("malformed stdin: expected object with key 'result'")

    try:
        assets = engine.load_policy_assets(REPO_ROOT)
    except engine.RoutingAssetError as exc:
        _fail(f"bootstrap failure: {exc}")

    errors = engine.validate_route_task_result(payload["result"], assets)
    print(json.dumps({"ok": not errors, "errors": [e.as_dict() for e in errors]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
