#!/usr/bin/env python3
"""Trusted acceptance check, run from the checked-out pilot repository."""
import ast
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("expected pinned base commit")
changed = subprocess.run(["git", "diff", "--name-only", sys.argv[1], "--"], check=True, capture_output=True, text=True).stdout.splitlines()
if changed != ["arithmetic.py"]:
    raise SystemExit(f"unexpected changed files: {changed}")

source = Path("arithmetic.py").read_text(encoding="utf-8")
expected = '"""Small arithmetic helper."""\n\ndef difference(a, b):\n    return a - b\n'
if source != expected:
    raise SystemExit("arithmetic.py must contain exactly the requested one-line change")
namespace = {}
exec(compile(ast.parse(source), "arithmetic.py", "exec"), namespace)
for a, b, expected_value in ((9, 4, 5), (0, 3, -3), (-2, -5, 3), (1.5, .5, 1.0)):
    if namespace["difference"](a, b) != expected_value:
        raise SystemExit("difference returned an incorrect value")
print("pilot acceptance passed")
