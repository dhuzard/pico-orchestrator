#!/usr/bin/env python3
"""Trusted exact-diff acceptance check for the configuration-edit pilot."""
import json
import subprocess
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("expected pinned base commit")
changed = subprocess.run(["git", "diff", "--name-only", sys.argv[1], "--"], check=True,
                         capture_output=True, text=True).stdout.splitlines()
if changed != ["settings.json"]:
    raise SystemExit(f"unexpected changed files: {changed}")
source = Path("settings.json").read_text(encoding="utf-8")
expected = '{\n  "display": {\n    "max_results": 20\n  }\n}\n'
if source != expected or json.loads(source)["display"]["max_results"] != 20:
    raise SystemExit("settings.json must contain exactly the requested one-value change")
print("config pilot acceptance passed")
