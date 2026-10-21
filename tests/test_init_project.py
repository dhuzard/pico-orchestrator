#!/usr/bin/env python3
"""Tests for tools/init_project.py."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import init_project  # noqa: E402


class InitProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-init-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_init_project_writes_routing_agents_and_pr_workflow(self):
        actions = init_project.init_project(self.tmp)

        self.assertTrue((self.tmp / ".pi" / "routing.yaml").exists())
        self.assertTrue((self.tmp / ".pi" / "autonomy.yaml").exists())
        self.assertTrue((self.tmp / "AGENTS.md").exists())
        workflow = self.tmp / ".pi" / "orchestrator-pr-workflow.md"
        self.assertTrue(workflow.exists())
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("github-pr-link", text)
        self.assertNotIn("github-pr-create", text)
        self.assertIn("Refs #N", text)
        self.assertIn("issue_reference", text)
        self.assertIn("trace --latest", text)
        self.assertIn("pico/batch-<n>", text)
        self.assertTrue(any("routing.yaml" in action for action in actions))

    def test_initialized_autonomy_policy_yields_wave_and_pr_limits(self):
        import autonomy_policy
        self.assertEqual(autonomy_policy.limits(self.tmp)["source"], "default")
        init_project.init_project(self.tmp)
        limits = autonomy_policy.limits(self.tmp)
        self.assertEqual((limits["parallel_tasks"], limits["open_prs"], limits["pr_granularity"],
                          limits["issue_reference"]), (3, 2, "batch", "refs"))
        policy = self.tmp / ".pi" / "autonomy.yaml"
        # Files written before pr_granularity/open_prs/issue_reference existed keep validating with defaults.
        legacy = "\n".join(line for line in policy.read_text(encoding="utf-8").splitlines()
                           if not line.startswith(("pr_granularity", "  open_prs", "issue_reference")))
        policy.write_text(legacy.replace("parallel_tasks: 3", "parallel_tasks: 5"), encoding="utf-8")
        limits = autonomy_policy.limits(self.tmp)
        self.assertEqual((limits["parallel_tasks"], limits["open_prs"], limits["pr_granularity"],
                          limits["issue_reference"]), (5, 2, "batch", "refs"))
        policy.write_text(legacy + "\nissue_reference: closes\n", encoding="utf-8")
        self.assertEqual(autonomy_policy.limits(self.tmp)["issue_reference"], "closes")
        for invalid in ("\npr_granularity: per_commit\n", "\nissue_reference: fixes\n"):
            policy.write_text(legacy + invalid, encoding="utf-8")
            with self.assertRaises(autonomy_policy.AutonomyPolicyError):
                autonomy_policy.limits(self.tmp)
        policy.write_text(legacy + "\npr_granularity: per_commit\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "autonomy_policy.py"), "limits",
                                 "--worktree", str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["blocker_code"], "AUTONOMY_POLICY_INVALID")

    def test_init_project_keeps_existing_files_unless_overwrite(self):
        agents = self.tmp / "AGENTS.md"
        agents.write_text("custom\n", encoding="utf-8")

        init_project.init_project(self.tmp)
        self.assertEqual(agents.read_text(encoding="utf-8"), "custom\n")

        init_project.init_project(self.tmp, overwrite=True)
        self.assertNotEqual(agents.read_text(encoding="utf-8"), "custom\n")

    def test_copy_extension_is_optional(self):
        init_project.init_project(self.tmp)
        self.assertFalse((self.tmp / ".pi" / "extensions" / "model-router.ts").exists())
        self.assertFalse((self.tmp / ".pi" / "extensions" / "mission-controller.ts").exists())
        self.assertFalse((self.tmp / ".pi" / "tools" / "mission.py").exists())

        init_project.init_project(self.tmp, copy_extension=True)
        self.assertTrue((self.tmp / ".pi" / "extensions" / "model-router.ts").exists())
        self.assertTrue((self.tmp / ".pi" / "extensions" / "mission-controller.ts").exists())
        self.assertTrue((self.tmp / ".pi" / "extensions" / "workflow-commands.ts").exists())
        self.assertTrue((self.tmp / ".pi" / "prompts" / "orchestrate.md").exists())
        self.assertTrue((self.tmp / ".pi" / "prompts" / "orchestrate-core.md").exists())
        self.assertTrue((self.tmp / ".pi" / "tools" / "mission.py").exists())
        self.assertTrue((self.tmp / ".pi" / "tools" / "route_task.py").exists())
        self.assertTrue((self.tmp / ".pi" / "tools" / "model_profiles.py").exists())
        self.assertTrue((self.tmp / ".pi" / "tools" / "doctor.py").exists())
        self.assertTrue((self.tmp / ".pi" / "schemas" / "mission.schema.json").exists())
        self.assertTrue((self.tmp / ".pi" / "schemas" / "model-runtime.schema.json").exists())
        self.assertTrue((self.tmp / ".pi" / "schemas" / "task.schema.json").exists())
        self.assertTrue((self.tmp / ".pi" / "schemas" / "delegation-policy.schema.json").exists())
        self.assertTrue((self.tmp / ".pi" / "routing" / "default-policy.yaml").exists())
        self.assertTrue((self.tmp / ".pi" / "routing" / "delegation-policy.yaml").exists())
        self.assertTrue((self.tmp / ".pi" / "routing" / "backend-compatibility.json").exists())

    def test_github_templates_are_opt_in_and_preserve_existing_files(self):
        init_project.init_project(self.tmp)
        self.assertFalse((self.tmp / ".github" / "pull_request_template.md").exists())
        self.assertFalse((self.tmp / ".github" / "ISSUE_TEMPLATE" / "bug.yml").exists())

        init_project.init_project(self.tmp, github_templates=True)
        pr_template = self.tmp / ".github" / "pull_request_template.md"
        bug_template = self.tmp / ".github" / "ISSUE_TEMPLATE" / "bug.yml"
        change_template = self.tmp / ".github" / "ISSUE_TEMPLATE" / "change.yml"
        work_template = self.tmp / ".github" / "ISSUE_TEMPLATE" / "work-item.yml"
        self.assertTrue(pr_template.exists())
        self.assertTrue(bug_template.exists())
        self.assertTrue(change_template.exists())
        self.assertTrue(work_template.exists())
        self.assertIn("Risk and rollback", pr_template.read_text(encoding="utf-8"))
        self.assertIn("Acceptance criteria", bug_template.read_text(encoding="utf-8"))

        pr_template.write_text("project-specific\n", encoding="utf-8")
        init_project.init_project(self.tmp, github_templates=True)
        self.assertEqual(pr_template.read_text(encoding="utf-8"), "project-specific\n")

        init_project.init_project(self.tmp, github_templates=True, overwrite=True)
        self.assertNotEqual(pr_template.read_text(encoding="utf-8"), "project-specific\n")

    def test_copied_mission_cli_runtime_can_create_mission(self):
        init_project.init_project(self.tmp, copy_extension=True)

        import subprocess

        result = subprocess.run(
            ["python3", str(self.tmp / ".pi" / "tools" / "mission.py"), "--repo-root", str(self.tmp), "create", "--goal", "Deliver objective"],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "missions" / "missions.yaml").exists())

    def test_copied_router_runtime_can_load_policy_assets(self):
        init_project.init_project(self.tmp, copy_extension=True)

        import importlib.util

        routing_engine_path = self.tmp / ".pi" / "tools" / "routing_engine.py"
        spec = importlib.util.spec_from_file_location("copied_routing_engine", routing_engine_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["copied_routing_engine"] = module
        spec.loader.exec_module(module)

        assets = module.load_policy_assets(self.tmp / ".pi")
        self.assertIn("CODEX", assets.policy["routes"])


if __name__ == "__main__":
    unittest.main()


class VendoredBundleTest(unittest.TestCase):
    """The --copy-extension bundle is a complete, governed runtime (audit issue 7)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-bundle-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        init_project.init_project(self.tmp, copy_extension=True)
        self.bundle = self.tmp / ".pi"

    def test_bundle_contains_every_resource_its_extensions_and_tools_load(self):
        import re
        for extension in (self.bundle / "extensions").glob("*.ts"):
            text = extension.read_text(encoding="utf-8")
            for directory, name in re.findall(r'join\((?:REPO_)?ROOT, "([a-z]+)", "([A-Za-z0-9_.-]+)"\)', text):
                self.assertTrue((self.bundle / directory / name).exists(), f"{extension.name} needs {directory}/{name}")
            for module in re.findall(r'from "\./([a-z-]+\.ts)"', text):
                self.assertTrue((self.bundle / "extensions" / module).exists(), f"{extension.name} imports {module}")
        tools = {path.stem for path in (REPO_ROOT / "tools").glob("*.py")}
        for tool in (self.bundle / "tools").glob("*.py"):
            for module in re.findall(r"^\s*(?:import|from)\s+([a-z_]+)", tool.read_text(encoding="utf-8"), re.M):
                if module in tools:
                    self.assertTrue((self.bundle / "tools" / f"{module}.py").exists(), f"{tool.name} imports {module}")
        for name in ("orchestration-lifecycle.ts", "context-budget.ts", "claude-backend-adapter.ts", "claude-worker.ts"):
            self.assertTrue((self.bundle / "extensions" / name).exists())
        self.assertTrue((self.bundle / "templates" / "AGENTS.md").exists())
        self.assertTrue(os.access(self.bundle / "scripts" / "run-local.sh", os.X_OK))

    def test_bundle_launcher_loads_lifecycle_and_keeps_state_out_of_the_target(self):
        bin_dir, agent, home = self.tmp.parent / f"{self.tmp.name}-bin", self.tmp.parent / f"{self.tmp.name}-agent", \
            self.tmp.parent / f"{self.tmp.name}-home"
        for path in (bin_dir, agent, home):
            path.mkdir()
            self.addCleanup(lambda path=path: shutil.rmtree(path, ignore_errors=True))
        capture = bin_dir / "launch.json"
        (bin_dir / "pi").write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                                    f"json.dump({{'args': sys.argv[1:], 'state': os.environ.get('PI_ORCHESTRATOR_STATE_DIR')}}, open({str(capture)!r}, 'w'))\n")
        (bin_dir / "pi").chmod(0o755)
        env = {key: value for key, value in os.environ.items()
               if key not in {"PI_ORCHESTRATOR_STATE_DIR", "XDG_STATE_HOME", "PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY"}}
        env.update(PATH=os.pathsep.join([str(bin_dir), os.path.dirname(sys.executable), "/usr/bin", "/bin"]),
                   HOME=str(home), PI_CODING_AGENT_DIR=str(agent))
        proc = subprocess.run(["bash", str(self.bundle / "scripts" / "run-local.sh"), str(self.tmp)],
                              cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("vendored bundle", proc.stderr)
        launch = json.loads(capture.read_text())
        self.assertIn(str(self.bundle / "extensions" / "orchestration-lifecycle.ts"), launch["args"])
        self.assertEqual(Path(launch["state"]), home / ".local" / "state" / "pi-coding-orchestrator")

    def test_next_steps_point_to_the_governed_launchers(self):
        output = io.StringIO()
        with redirect_stdout(output):
            init_project.main([str(self.tmp), "--copy-extension"])
        self.assertIn("pico run .", output.getvalue())
        self.assertIn(".pi/scripts/run-local.sh .", output.getvalue())
        self.assertNotIn("pi -e", output.getvalue())
