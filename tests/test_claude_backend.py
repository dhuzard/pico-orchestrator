import json
import os
import stat
import subprocess
import io
from contextlib import redirect_stdout
from unittest.mock import patch
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import claude_backend as backend
import backend_preflight as preflight
import routing_engine as engine


class ClaudeBackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assets = engine.load_policy_assets(ROOT)
        cases = engine.load_yaml(ROOT / "tests/routing-cases.yaml")["cases"]
        task = next(case["task"] for case in cases if case["expected_route"] == "CODEX_CLAUDE_CODEX")
        resolution = engine.resolve_route(task, assets.policy, None, assets.task_schema)
        cls.route = engine.build_result(resolution, assets.policy, None, assets.execution_plan_schema)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.agent = self.base / "agent"
        self.agent.mkdir()
        self.target = self.base / "target"
        self.target.mkdir()
        self.env = patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.agent)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake(self, name, version):
        path = self.base / name
        path.write_text(f"#!/bin/sh\nif [ \"$1\" = '--version' ]; then echo '{version} (Claude Code)'; exit; fi\n"
                        "if [ \"$1\" = 'auth' ]; then echo '{\"loggedIn\":true}'; exit; fi\n"
                        "printf '%s\\n' \"$@\" >> \"$CLAUDE_CALL_LOG\"\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_host_selection_overrides_stale_bundled_sdk_runtime(self):
        stale = self.agent / "npm/node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude"
        stale.parent.mkdir(parents=True)
        stale.write_text("#!/bin/sh\necho '2.1.141 (Claude Code)'\n")
        stale.chmod(0o755)
        self.assertEqual(backend.resolve(self.target), stale.resolve())
        host = self.fake("host-claude", "2.1.287")
        with patch.dict(os.environ, {"CLAUDE_CALL_LOG": str(self.base / "calls")}), \
             patch("claude_backend.shutil.which", return_value=str(host)):
            selected = backend.configure_host(self.target)
            self.assertEqual(selected["backend_version_output"], "2.1.287 (Claude Code)")
            self.assertEqual(backend.resolve(self.target), host.resolve())
            config = json.loads((self.agent / "claude-bridge.json").read_text())
            # The stable launcher path as found on PATH, not its resolved target
            # (on macOS, /var is itself a symlink to /private/var).
            self.assertEqual(config["provider"]["pathToClaudeCodeExecutable"], os.path.abspath(host))
            before = backend.identity(backend.resolve(self.target))
            subprocess.run([str(backend.resolve(self.target)), "--model", "claude-opus-5-5", "-p", "fixture"], check=True)
            self.assertEqual(backend.identity(backend.resolve(self.target)), before)
            self.assertIn("claude-opus-5-5", (self.base / "calls").read_text())
            self.assertNotIn("anthropic/claude-opus-5-5", (self.base / "calls").read_text())

    def test_project_override_is_detected(self):
        host = self.fake("host", "2.1.287")
        stale = self.fake("stale", "2.1.141")
        project = self.target / ".pi"
        project.mkdir()
        (project / "claude-bridge.json").write_text(json.dumps({"provider": {"pathToClaudeCodeExecutable": str(stale)}}))
        with self.assertRaisesRegex(ValueError, "MODEL_BACKEND_MISMATCH"):
            backend.configure_host(self.target, which=lambda _: str(host))

    def test_preflight_verification_fails_closed_on_runtime_change(self):
        host = self.fake("host", "2.1.287")
        stale = self.fake("stale", "2.1.141")
        backend.configure_host(self.target, which=lambda _: str(host))
        expected = backend.identity(host)
        payload = {"verify_backend": True, "backend_identity": expected, "cwd": str(self.target)}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(preflight.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "READY")
        config_path = self.agent / "claude-bridge.json"
        config_path.write_text(json.dumps({"provider": {"pathToClaudeCodeExecutable": str(stale)}}))
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(preflight.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["blocker_code"], "MODEL_BACKEND_MISMATCH")

    def test_routed_preflight_rejects_backend_identity_drift_from_launcher_pin(self):
        host = self.fake("host", "2.1.287")
        stale = self.fake("stale", "2.1.287")
        backend.configure_host(self.target, which=lambda _: str(host))
        pinned = json.dumps(backend.identity(host))
        (self.agent / "claude-bridge.json").write_text(json.dumps({"provider": {"pathToClaudeCodeExecutable": str(stale)}}))
        with patch.dict(os.environ, {"PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY": pinned}):
            result = preflight.preflight(self.route, ["spawn_subagent", "ClaudeWorker"], cwd=self.target)
        self.assertEqual(result["blocker_code"], "MODEL_BACKEND_MISMATCH")

    def test_bridge_uses_stable_launcher_path_and_isolated_default(self):
        versions = self.base / "versions"
        versions.mkdir()
        real = self.fake("versions/2.1.287", "2.1.287")
        link = self.base / "claude"
        link.symlink_to(real)
        (self.agent / "claude-bridge.json").write_text(json.dumps(
            {"askClaude": {"enabled": True}, "provider": {"other": 1}}))
        pinned = backend.configure_host(self.target, which=lambda _: str(link))
        config = json.loads((self.agent / "claude-bridge.json").read_text())
        self.assertEqual(config["provider"], {"other": 1, "pathToClaudeCodeExecutable": str(link)})
        self.assertEqual(config["askClaude"], {"enabled": True, "defaultIsolated": True})
        self.assertEqual(pinned["backend_executable"], str(real.resolve()))
        self.assertEqual(list(self.agent.glob("*.tmp")), [])
        # An update behind the stable path is detected as drift, never followed silently.
        newer = self.fake("versions/2.1.300", "2.1.300")
        link.unlink()
        link.symlink_to(newer)
        with patch.dict(os.environ, {"PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY": json.dumps(pinned)}):
            result = preflight.preflight(self.route, ["spawn_subagent", "ClaudeWorker"], cwd=self.target)
        self.assertEqual(result["blocker_code"], "MODEL_BACKEND_MISMATCH")


if __name__ == "__main__":
    unittest.main()
