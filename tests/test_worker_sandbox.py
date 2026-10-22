#!/usr/bin/env python3
"""Deterministic tests for the worker sandbox policy and its provenance (issue #63).

Policy level: the `worker_sandbox` section of `.pi/autonomy.yaml` (network allowlist and cache directories) is
schema-validated with unknown keys rejected, and `autonomy_policy.limits` always reports both lists. Recorder
level: the lifecycle extension's `worker_sandbox` event keeps the posture on the run, an opt-out only as an
explicit `env` choice, and the run record still validates. The settings Claude Code receives, the default-on
posture and the fail-closed check are covered by tests/orchestration-lifecycle.test.ts. No test here starts a
model or a sandbox.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import autonomy_policy  # noqa: E402
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import yaml  # noqa: E402
from test_run_lease import StateTestCase  # noqa: E402


def write_policy(directory: Path, **extra) -> None:
    schema = json.loads((ROOT / "schemas" / "autonomy.schema.json").read_text())
    permissions = {name: False for name in schema["properties"]["permissions"]["required"]}
    permissions["merge"] = {"enabled": False, "max_failure_cost": "low", "require_ci": True,
                            "require_review_if_policy_demands": True}
    (directory / ".pi").mkdir(exist_ok=True)
    (directory / ".pi" / "autonomy.yaml").write_text(yaml.safe_dump({
        "profile": "manual", "permissions": permissions,
        "limits": {"parallel_tasks": 3, "nested_agents_per_task": 1, "repair_attempts": 1, "replans": 1}, **extra}))


class PolicyTest(unittest.TestCase):
    def limits(self, **extra) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            write_policy(Path(tmp), **extra)
            return autonomy_policy.limits(Path(tmp))

    def test_absent_section_or_file_widens_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(autonomy_policy.limits(Path(tmp))["worker_sandbox"],
                             {"allowed_domains": [], "cache_directories": []})
        self.assertEqual(self.limits()["worker_sandbox"], {"allowed_domains": [], "cache_directories": []})
        self.assertEqual(self.limits(worker_sandbox={})["worker_sandbox"], {"allowed_domains": [], "cache_directories": []})

    def test_allowlist_and_cache_directories_are_reported(self):
        verdict = self.limits(worker_sandbox={
            "network": {"allowed_domains": ["registry.npmjs.org", "*.pythonhosted.org", "pypi.org:443"]},
            "cache_directories": ["pip", "ms-playwright", "uv/wheels"]})
        self.assertEqual(verdict["worker_sandbox"], {
            "allowed_domains": ["registry.npmjs.org", "*.pythonhosted.org", "pypi.org:443"],
            "cache_directories": ["pip", "ms-playwright", "uv/wheels"]})
        self.assertEqual(verdict["source"], ".pi/autonomy.yaml")

    def test_invalid_or_unknown_entries_fail_the_whole_policy(self):
        bad_sections = [
            {"network": {"allowed_domains": ["*"]}},                 # everything
            {"network": {"allowed_domains": ["*.com"]}},             # a whole TLD
            {"network": {"allowed_domains": ["10.0.0.1"]}},          # IP literal
            {"network": {"allowed_domains": ["Registry.NPMJS.org"]}},
            {"network": {"allowed_domains": ["evil.example/path"]}},
            {"network": {"allowed_domains": ["a.example", "a.example"]}},
            {"network": {"allowed_domains": "registry.npmjs.org"}},
            {"network": {"allowed_domains": [], "denied_domains": []}},  # unknown key
            {"network": {"allowed_domains": [f"h{i}.example.org" for i in range(51)]}},
            {"cache_directories": ["../.ssh"]},
            {"cache_directories": ["pip/../../.config"]},
            {"cache_directories": ["/abs/path"]},
            {"cache_directories": ["~/.npm"]},
            {"cache_directories": ["pip/*"]},
            {"cache_directories": ["."]},
            {"cache_directories": ["pi-coding-orchestrator"]},
            {"cache_directories": ["pi-coding-orchestrator/runtimes"]},
            {"enabled": False},                                      # a repository cannot turn the sandbox off
            {"allow_write": ["/tmp"]},
        ]
        for section in bad_sections:
            with self.subTest(section=section), self.assertRaises(autonomy_policy.AutonomyPolicyError):
                self.limits(worker_sandbox=section)

    def test_a_name_that_only_starts_like_the_pico_cache_is_allowed(self):
        verdict = self.limits(worker_sandbox={"cache_directories": ["pi-coding-orchestrator-other"]})
        self.assertEqual(verdict["worker_sandbox"]["cache_directories"], ["pi-coding-orchestrator-other"])

    def test_cli_reports_an_invalid_section_as_autonomy_policy_invalid(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            write_policy(Path(tmp), worker_sandbox={"network": {"allowed_domains": ["*"]}})
            proc = subprocess.run([sys.executable, str(ROOT / "tools" / "autonomy_policy.py"), "limits", "--worktree", tmp],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["blocker_code"], "AUTONOMY_POLICY_INVALID")


class RecorderTest(StateTestCase):
    def setUp(self):
        super().setUp()
        self.owner = "owner-a"
        self.run_id = self.start_run(self.owner)

    def event(self, payload: dict) -> dict:
        return po.record_event(self.run_id, {"kind": "worker_sandbox", **payload}, owner_id=self.owner,
                               repo_root=self.records)

    def history(self) -> list[dict]:
        return self.parent(self.run_id).get("worker_sandbox", [])

    def test_default_posture_is_recorded_and_validates(self):
        self.event({"state": "enabled", "source": "default", "allowed_domains": [], "cache_directories": [],
                    "policy_source": "default"})
        [entry] = self.history()
        self.assertEqual({key: value for key, value in entry.items() if key != "at"},
                         {"state": "enabled", "source": "default", "allowed_domains": [], "cache_directories": [],
                          "policy_source": "default"})
        self.assertEqual(provenance.validate_run_record(provenance.get_run(self.records, self.run_id), provenance.load_schema(self.records)), [])

    def test_an_explicit_opt_out_stays_on_the_run(self):
        enabled = {"state": "enabled", "source": "default", "allowed_domains": ["registry.npmjs.org"],
                   "cache_directories": ["pip"], "policy_source": "policy"}
        self.event(enabled)
        self.event(enabled)  # a resume with the same posture adds nothing
        self.event({"state": "opted_out", "source": "env", "policy_source": "default"})
        self.event(enabled)
        self.assertEqual([entry["state"] for entry in self.history()], ["enabled", "opted_out", "enabled"])
        self.assertEqual(self.history()[1]["source"], "env")
        self.assertEqual(self.history()[0]["allowed_domains"], ["registry.npmjs.org"])
        self.assertEqual(provenance.validate_run_record(provenance.get_run(self.records, self.run_id), provenance.load_schema(self.records)), [])

    def test_invalid_postures_are_refused(self):
        for payload in ({"state": "opted_out", "source": "default"},  # an opt-out is never implicit
                        {"state": "off", "source": "env"},
                        {"state": "enabled", "source": "repository"},
                        {"state": "enabled", "source": "env", "policy_source": "worktree"},
                        {"state": "enabled", "source": "env", "allowed_domains": ["*"]},
                        {"state": "enabled", "source": "env", "allowed_domains": "pypi.org"},
                        {"state": "enabled", "source": "env", "cache_directories": ["../x"]}):
            with self.subTest(payload=payload), self.assertRaises(po.ParentRunError) as refused:
                self.event(payload)
            self.assertEqual(refused.exception.error_code, "PARENT_EVENT_FIELDS_INVALID")
        for payload in ({"state": "enabled"}, {"state": "enabled", "source": "env", "detail": "x"}):
            with self.subTest(payload=payload), self.assertRaises(po.ParentRunError):
                self.event(payload)
        self.assertEqual(self.history(), [])

    def test_an_unavailable_sandbox_is_a_prerequisite_not_a_loop(self):
        self.assertTrue(po.is_prerequisite("WORKER_SANDBOX_UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
