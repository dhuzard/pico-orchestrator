#!/usr/bin/env python3
"""The boundary of the real-agent benchmark (issue #49, part 2): it only starts when asked, and CI never asks.

benchmarks/synthetic-workspace/run_real.py is the single entry point. These tests check that it refuses
without PICO_REAL_BENCHMARK=1, and that with it, it drives setup, launch, validate and metrics and writes a
report. The "real" backends here are the fake `pi` of the deterministic suite and a `pico` stand-in that maps
`pico workspace ...` to the tools of this checkout, as the launcher does: no model is called. The variable is
set only in the environment of the child process under test, never in this process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario_support import ROOT, fake_pi_source, implement  # noqa: E402

RUN_REAL = ROOT / "benchmarks" / "synthetic-workspace" / "run_real.py"
RUN_REAL_SH = ROOT / "benchmarks" / "synthetic-workspace" / "run-real.sh"
REVISION = "a" * 40

PICO_STAND_IN = '''#!{python}
"""Stands in for the `pico` launcher: `pico workspace schedule|validate` run the tools of this checkout."""
import os, shutil, sys
assert os.environ["PI_CODING_ORCHESTRATOR_REF"] == os.environ["EXPECTED_REF"], "the revision pin did not reach the launcher"
os.environ["PI_ORCHESTRATOR_STATE_DIR"] = os.environ["PI_CODING_ORCHESTRATOR_STATE_DIR"]
verb, rest = sys.argv[2], sys.argv[3:]
tools = os.path.join(os.environ["SHIM_TOOLS"])
if verb == "schedule":
    argv = [os.path.join(tools, "workspace_scheduler.py"), *rest, "--pi", shutil.which("pi")]
else:
    argv = [os.path.join(tools, "workspace_contracts.py"), verb, *rest]
os.execv(sys.executable, [sys.executable, *argv])
'''

RECORDING_PICO = '''#!{python}
"""Records each `pico` argv and answers `schedule` with an empty plan: enough to check what run_real asks for."""
import json, os, sys
with open(os.environ["ARGV_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
records = os.path.join(os.environ["PI_CODING_ORCHESTRATOR_STATE_DIR"], "workspaces")
os.makedirs(records, exist_ok=True)
with open(os.path.join(records, "pico-bench.json"), "w") as record:
    json.dump({{"record_version": 1, "workspace_id": "pico-bench", "tasks": {{}}}}, record)
print(json.dumps({{"status": "NOTHING_DISPATCHABLE", "tasks": []}}))
'''


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(("PICO_REAL", "PI_ORCHESTRAT", "PI_CODING", "FAKE_"))}


class EntryPointRefusalTest(unittest.TestCase):
    def run_entry(self, env: dict, *args: str, script: Path = RUN_REAL):
        argv = [sys.executable, str(script), *args] if script.suffix == ".py" else [str(script), *args]
        return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)

    def test_refuses_unless_the_variable_is_exactly_1_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            for value in (None, "", "0", "true", "yes", "1 "):
                env = clean_env()
                if value is not None:
                    env["PICO_REAL_BENCHMARK"] = value
                for script in (RUN_REAL, RUN_REAL_SH):
                    done = self.run_entry(env, REVISION, "before", "--output-dir", str(out), script=script)
                    self.assertEqual(done.returncode, 2, (value, script.name, done.stderr))
                    self.assertIn("PICO_REAL_BENCHMARK=1", done.stderr)
                    self.assertFalse(out.exists(), "a refused run must not create anything")

    def test_ci_workflow_never_sets_the_variable_or_calls_the_entry_point(self):
        import yaml
        for workflow in (ROOT / ".github" / "workflows").glob("*.y*ml"):
            data = yaml.safe_load(workflow.read_text())
            environments, commands = [data.get("env") or {}], []
            for job in (data.get("jobs") or {}).values():
                environments.append(job.get("env") or {})
                for step in job.get("steps") or []:
                    environments.append(step.get("env") or {})
                    commands.append(str(step.get("run") or ""))
            for env in environments:
                self.assertNotIn("PICO_REAL_BENCHMARK", env, workflow.name)
            for command in commands:
                self.assertNotIn("PICO_REAL_BENCHMARK", command, workflow.name)
                self.assertNotIn("run_real", command, workflow.name)
                self.assertNotIn("run-real", command, workflow.name)

    def test_an_unpinned_or_unknown_revision_is_refused(self):
        env = {**clean_env(), "PICO_REAL_BENCHMARK": "1"}
        with tempfile.TemporaryDirectory() as tmp:
            for rev in ("main", "e11fafa", "A" * 40, REVISION):
                done = self.run_entry(env, rev, "before", "--output-dir", str(Path(tmp) / "run"))
                self.assertEqual(done.returncode, 2, (rev, done.stderr))
                self.assertFalse((Path(tmp) / "run").exists())

    def test_missing_launcher_or_backend_is_reported_not_faked(self):
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        env = {**clean_env(), "PICO_REAL_BENCHMARK": "1", "PATH": os.defpath}
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_entry(env, head, "before", "--output-dir", str(Path(tmp) / "run"), "--pico", "pico-does-not-exist")
            self.assertEqual(done.returncode, 3, done.stderr)
            self.assertFalse((Path(tmp) / "run").exists())


class EntryPointRunTest(unittest.TestCase):
    def test_setup_launch_validate_and_metrics_write_a_report(self):
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            pi, pico = bin_dir / "pi", bin_dir / "pico"
            pi.write_text(fake_pi_source())
            pico.write_text(PICO_STAND_IN.format(python=sys.executable))
            for tool in (pi, pico):
                tool.chmod(0o755)
            (base / "scenario.json").write_text(json.dumps({"lib-slugify": [implement(tokens=1200, cost=0.05)]}))
            (base / "signals").mkdir()
            env = {**clean_env(), "PICO_REAL_BENCHMARK": "1", "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   "EXPECTED_REF": head, "SHIM_TOOLS": str(ROOT / "tools"), "HOME": str(base),
                   "FAKE_PI_TOOLS": str(ROOT / "tools"), "FAKE_PI_BEHAVIOR": "scenario", "PI_CODING_AGENT_DIR": str(base / "agent"),
                   "FAKE_SCENARIO": str(base / "scenario.json"), "FAKE_SCENARIO_SIGNALS": str(base / "signals"),
                   "FAKE_SCENARIO_REFERENCE": str(ROOT / "benchmarks" / "synthetic-workspace" / "reference"),
                   "FAKE_SCENARIO_LEAD_DIR": str(ROOT / "tests")}
            out = base / "run"
            done = subprocess.run([sys.executable, str(RUN_REAL), head, "after", "--output-dir", str(out)],
                                  env=env, capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual((report["kind"], report["label"], report["pico_revision"]), ("real-agent-benchmark", "after", head))
            # Only the first wave can run without GitHub; the dependents wait for a published, CI-green upstream.
            states = {row["id"]: row["state"] for row in report["launch"]["tasks"]}
            self.assertEqual(states, {"lib-slugify": "completed", "lib-stopwords": "waiting_for_readiness",
                                      "app-post-url": "waiting_for_readiness", "qualify-integration": "waiting_for_readiness"})
            self.assertEqual([(v["task"], v["validation"]) for v in report["validation"]], [("lib-slugify", "VALIDATED")])
            totals = report["metrics"]["totals"]["tasks"]
            self.assertEqual((totals["completed"], totals["verified"]), (1, 1))
            self.assertEqual(report["metrics"]["headline"]["value"], 1200.0)
            self.assertEqual(report["metrics"]["totals"]["cost"]["estimated_usd"], 0.05)
            # A second run into the same directory is refused; --continue repeats launch, validate and metrics on it.
            again = subprocess.run([sys.executable, str(RUN_REAL), head, "after", "--output-dir", str(out)],
                                   env=env, capture_output=True, text=True, check=False)
            self.assertEqual(again.returncode, 2)
            resumed = subprocess.run([sys.executable, str(RUN_REAL), head, "after", "--output-dir", str(out), "--continue"],
                                     env=env, capture_output=True, text=True, check=False)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertTrue(json.loads((out / "report.json").read_text())["continued"])
            self.assertEqual(json.loads((out / "report.json").read_text())["metrics"]["totals"]["tasks"]["completed"], 1)

    def test_sandbox_mode_commits_the_policy_bounds_the_budget_and_keeps_the_access_check(self):
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            (bin_dir / "pi").write_text("#!/bin/sh\nexit 0\n")
            (bin_dir / "pico").write_text(RECORDING_PICO.format(python=sys.executable))
            for tool in bin_dir.iterdir():
                tool.chmod(0o755)
            log = base / "argv.jsonl"
            env = {**clean_env(), "PICO_REAL_BENCHMARK": "1", "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   "HOME": str(base), "ARGV_LOG": str(log)}
            out = base / "run"
            sandbox = ["--owner", "someone", "--publication", "--max-tokens", "120000", "--issue", "lib-slugify=3"]
            # Publication for an owner other than the default needs the explicit ownership confirmation.
            refused = subprocess.run([sys.executable, str(RUN_REAL), head, "sandbox", "--output-dir", str(out), *sandbox],
                                     env=env, capture_output=True, text=True, check=False)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertFalse(out.exists())
            done = subprocess.run([sys.executable, str(RUN_REAL), head, "sandbox", "--output-dir", str(out), *sandbox,
                                   "--i-own-these-repos"], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            import yaml
            manifest = yaml.safe_load((out / "work" / "workspace.yaml").read_text())
            self.assertEqual(manifest["budget"], {"max_tokens": 120000})
            self.assertEqual(manifest["repositories"][0]["repository"], "someone/synth-lib")
            self.assertEqual({t["id"]: t.get("issue") for t in manifest["tasks"]}["lib-slugify"], 3)
            self.assertTrue((out / "work" / "synth-lib" / ".pi" / "autonomy.yaml").is_file())
            marker = json.loads((out / "run.json").read_text())
            self.assertEqual((marker["publication"], marker["max_tokens"], marker["issues"]), (True, 120000, {"lib-slugify": 3}))
            report = json.loads((out / "report.json").read_text())
            self.assertEqual((report["publication"], report["access_check"]), (True, True))
            self.assertFalse(report["allow_mutation"])
            # --continue reuses the recorded mode: the access check stays on. Mutation is asked for explicitly.
            again = subprocess.run([sys.executable, str(RUN_REAL), head, "sandbox", "--output-dir", str(out), "--continue",
                                    "--allow-mutation"], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertTrue(json.loads((out / "report.json").read_text())["allow_mutation"])
            schedules = [json.loads(line) for line in log.read_text().splitlines() if json.loads(line)[:2] == ["workspace", "schedule"]]
            self.assertEqual(len(schedules), 2)
            for argv in schedules:
                self.assertNotIn("--no-access-check", argv)
            self.assertEqual(["--allow-mutation" in argv for argv in schedules], [False, True])


if __name__ == "__main__":
    unittest.main()
