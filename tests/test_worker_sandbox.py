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
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import autonomy_policy  # noqa: E402
import github_sync  # noqa: E402
import parent_orchestration as po  # noqa: E402
import provenance  # noqa: E402
import yaml  # noqa: E402
import worker_sandbox_policy as wsp  # noqa: E402
from test_run_lease import REPO, StateTestCase  # noqa: E402


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
            "network": {"allowed_domains": ["registry.npmjs.org", "*.pythonhosted.org", "pypi.org:443", "x.example.org:65535"]},
            "cache_directories": ["pip", "ms-playwright", "uv/wheels"]})
        self.assertEqual(verdict["worker_sandbox"], {
            "allowed_domains": ["registry.npmjs.org", "*.pythonhosted.org", "pypi.org:443", "x.example.org:65535"],
            "cache_directories": ["pip", "ms-playwright", "uv/wheels"]})
        self.assertEqual(verdict["source"], ".pi/autonomy.yaml")

    def test_invalid_or_unknown_entries_fail_the_whole_policy(self):
        bad_sections = [
            {"network": {"allowed_domains": ["*"]}},                 # everything
            {"network": {"allowed_domains": ["*.com"]}},             # a whole TLD
            {"network": {"allowed_domains": ["10.0.0.1"]}},          # IP literal
            {"network": {"allowed_domains": ["Registry.NPMJS.org"]}},
            {"network": {"allowed_domains": ["evil.example/path"]}},
            {"network": {"allowed_domains": ["pypi.org:0"]}},
            {"network": {"allowed_domains": ["pypi.org:65536"]}},
            {"network": {"allowed_domains": ["pypi.org:0443"]}},
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

    def assert_valid(self):
        self.assertEqual(provenance.validate_run_record(provenance.get_run(self.records, self.run_id),
                                                        provenance.load_schema(self.records)), [])

    def test_default_posture_is_recorded_and_validates(self):
        result = self.event({"state": "enabled", "source": "default", "allowed_domains": [], "cache_directories": [],
                             "policy_source": "default"})
        self.assertEqual(result["worker_sandbox_effective"], {"allowed_domains": [], "cache_directories": []})
        [entry] = self.history()
        self.assertEqual({key: value for key, value in entry.items() if key != "at"},
                         {"state": "enabled", "source": "default", "allowed_domains": [], "cache_directories": [],
                          "policy_source": "default", "policy_commit": None, "local_drift": False,
                          "user_settings_widening": []})
        self.assert_valid()

    def test_an_explicit_opt_out_stays_on_the_run(self):
        enabled = {"state": "enabled", "source": "default", "allowed_domains": ["registry.npmjs.org"],
                   "cache_directories": ["pip"], "policy_source": "default_branch", "policy_commit": "a" * 40}
        self.event(enabled)
        self.event(enabled)  # a resume with the same posture adds nothing
        self.event({"state": "opted_out", "source": "env", "policy_source": "default"})
        self.event(enabled)
        self.assertEqual([entry["state"] for entry in self.history()], ["enabled", "opted_out", "enabled"])
        self.assertEqual(self.history()[1]["source"], "env")
        self.assertEqual(self.history()[0]["allowed_domains"], ["registry.npmjs.org"])
        self.assert_valid()

    def test_the_run_start_widening_is_pinned_and_a_resume_cannot_widen_it(self):
        start = {"state": "enabled", "source": "default", "allowed_domains": ["registry.npmjs.org", "pypi.org"],
                 "cache_directories": ["pip"], "policy_source": "default_branch", "policy_commit": "a" * 40}
        self.assertEqual(self.event(start)["worker_sandbox_effective"],
                         {"allowed_domains": ["registry.npmjs.org", "pypi.org"], "cache_directories": ["pip"]})
        widened = {**start, "allowed_domains": ["registry.npmjs.org", "evil.example.org"],
                   "cache_directories": ["pip", "ms-playwright"], "policy_commit": "b" * 40}
        effective = self.event(widened)["worker_sandbox_effective"]
        # Narrowing (pypi.org dropped) is kept; widening (evil.example.org, ms-playwright) is refused.
        self.assertEqual(effective, {"allowed_domains": ["registry.npmjs.org"], "cache_directories": ["pip"]})
        resumed = self.history()[-1]
        self.assertEqual(resumed["refused_allowed_domains"], ["evil.example.org"])
        self.assertEqual(resumed["refused_cache_directories"], ["ms-playwright"])
        self.assertEqual(resumed["policy_commit"], "b" * 40)
        self.assertEqual(self.history()[0]["allowed_domains"], ["registry.npmjs.org", "pypi.org"], "the pin is kept")
        # A run that started with nothing widened stays that way.
        self.assert_valid()

    def test_a_run_started_without_widening_never_gains_any(self):
        self.event({"state": "enabled", "source": "default", "policy_source": "unavailable"})
        effective = self.event({"state": "enabled", "source": "default", "allowed_domains": ["pypi.org"],
                                "policy_source": "default_branch", "policy_commit": "c" * 40})["worker_sandbox_effective"]
        self.assertEqual(effective, {"allowed_domains": [], "cache_directories": []})
        self.assertEqual(self.history()[-1]["refused_allowed_domains"], ["pypi.org"])
        self.assert_valid()

    def test_user_settings_widening_and_local_drift_are_recorded_only(self):
        self.event({"state": "enabled", "source": "default", "policy_source": "default", "local_drift": True,
                    "user_settings_widening": ["sandbox.excludedCommands", "sandbox.network.allowedDomains"]})
        [entry] = self.history()
        self.assertEqual((entry["local_drift"], entry["user_settings_widening"]),
                         (True, ["sandbox.excludedCommands", "sandbox.network.allowedDomains"]))
        self.assert_valid()

    def test_the_pinning_entry_survives_trimming(self):
        self.event({"state": "enabled", "source": "default", "allowed_domains": ["pypi.org"],
                    "policy_source": "default_branch", "policy_commit": "a" * 40})
        for index in range(po.MAX_WORKER_SANDBOX_ENTRIES + 5):
            state = "opted_out" if index % 2 else "enabled"
            self.event({"state": state, "source": "env", "allowed_domains": ["pypi.org"], "policy_source": "default_branch"})
        history = self.history()
        self.assertEqual(len(history), po.MAX_WORKER_SANDBOX_ENTRIES)
        self.assertEqual((history[0]["source"], history[0]["policy_commit"]), ("default", "a" * 40))
        self.assert_valid()

    def test_invalid_postures_are_refused(self):
        for payload in ({"state": "opted_out", "source": "default"},  # an opt-out is never implicit
                        {"state": "off", "source": "env"},
                        {"state": "enabled", "source": "repository"},
                        {"state": "enabled", "source": "env", "policy_source": "worktree"},
                        {"state": "enabled", "source": "env", "policy_source": "policy"},
                        {"state": "enabled", "source": "env", "allowed_domains": ["*"]},
                        {"state": "enabled", "source": "env", "allowed_domains": ["pypi.org:0"]},
                        {"state": "enabled", "source": "env", "allowed_domains": ["pypi.org:65536"]},
                        {"state": "enabled", "source": "env", "allowed_domains": ["pypi.org:0443"]},
                        {"state": "enabled", "source": "env", "allowed_domains": "pypi.org"},
                        {"state": "enabled", "source": "env", "cache_directories": ["../x"]},
                        {"state": "enabled", "source": "env", "policy_commit": "main"},
                        {"state": "enabled", "source": "env", "local_drift": "yes"},
                        {"state": "enabled", "source": "env", "user_settings_widening": ["permissions.allow"]}):
            with self.subTest(payload=payload), self.assertRaises(po.ParentRunError) as refused:
                self.event(payload)
            self.assertEqual(refused.exception.error_code, "PARENT_EVENT_FIELDS_INVALID")
        for payload in ({"state": "enabled"}, {"state": "enabled", "source": "env", "detail": "x"}):
            with self.subTest(payload=payload), self.assertRaises(po.ParentRunError):
                self.event(payload)
        self.assertEqual(self.history(), [])

    def test_an_unavailable_sandbox_is_a_prerequisite_not_a_loop(self):
        self.assertTrue(po.is_prerequisite("WORKER_SANDBOX_UNAVAILABLE"))


class FakeAdapter:
    """The GitHub API adapter as worker_sandbox_policy uses it: default branch, branch head, policy file."""

    def __init__(self, policy_text: str | None, *, fail: Exception | None = None, entry_type: str = "file"):
        self.policy_text, self.fail, self.entry_type = policy_text, fail, entry_type
        self.calls: list[tuple] = []

    def default_branch(self, *, repo):
        self.calls.append(("default_branch", repo))
        if self.fail:
            raise self.fail
        return "main"

    def branch_head(self, branch, *, repo):
        self.calls.append(("branch_head", branch, repo))
        return "d" * 40

    def policy_file_at(self, commit, *, repo):
        self.calls.append(("policy_file_at", commit, repo))
        if self.policy_text is None:
            return None
        return {"type": self.entry_type, "path": ".pi/autonomy.yaml", "text": self.policy_text, "sha": "e" * 40}


def policy_text(**extra) -> str:
    schema = json.loads((ROOT / "schemas" / "autonomy.schema.json").read_text())
    permissions = {name: False for name in schema["properties"]["permissions"]["required"]}
    permissions["merge"] = {"enabled": False, "max_failure_cost": "low", "require_ci": True,
                            "require_review_if_policy_demands": True}
    return yaml.safe_dump({"profile": "manual", "permissions": permissions,
                           "limits": {"parallel_tasks": 3, "nested_agents_per_task": 1, "repair_attempts": 1,
                                      "replans": 1}, **extra})


class DefaultBranchPolicyTest(StateTestCase):
    """The widening comes from the default branch on GitHub, never from the checkout a worker writes into."""

    WIDE = {"network": {"allowed_domains": ["registry.npmjs.org"]}, "cache_directories": ["pip"]}

    def resolve(self, adapter, env=None):
        return wsp.resolve(self.target, REPO, adapter=adapter, env=env or {"HOME": str(self.target / "nohome")})

    def test_the_default_branch_policy_widens_and_the_working_tree_does_not(self):
        # The checkout (where worker commits are integrated) asks for more than the default branch grants.
        write_policy(self.target, worker_sandbox={"network": {"allowed_domains": ["evil.example.org"]}})
        verdict = self.resolve(FakeAdapter(policy_text(worker_sandbox=self.WIDE)))
        self.assertEqual(verdict["policy_source"], "default_branch")
        self.assertEqual(verdict["worker_sandbox"], {"allowed_domains": ["registry.npmjs.org"], "cache_directories": ["pip"]})
        self.assertEqual((verdict["policy_branch"], verdict["policy_commit"], verdict["policy_blob"]), ("main", "d" * 40, "e" * 40))
        self.assertTrue(verdict["local_drift"], "the checkout's different copy is reported, never used")
        # Only the working tree declares a widening: nothing is widened.
        verdict = self.resolve(FakeAdapter(policy_text()))
        self.assertEqual(verdict["worker_sandbox"], {"allowed_domains": [], "cache_directories": []})
        self.assertTrue(verdict["local_drift"])

    def test_no_policy_on_the_default_branch_widens_nothing(self):
        verdict = self.resolve(FakeAdapter(None))
        self.assertEqual((verdict["policy_source"], verdict["worker_sandbox"]),
                         ("default", {"allowed_domains": [], "cache_directories": []}))
        self.assertFalse(verdict["local_drift"])

    def test_an_unreadable_or_invalid_remote_policy_fails_closed(self):
        for adapter, source in ((FakeAdapter(None, fail=github_sync.GhAuthError("not logged in")), "unavailable"),
                                (FakeAdapter(None, fail=github_sync.GhUnavailableError("no gh")), "unavailable"),
                                (FakeAdapter(policy_text(worker_sandbox={"network": {"allowed_domains": ["*"]}})), "invalid"),
                                (FakeAdapter("profile: [", ), "invalid"),
                                (FakeAdapter(policy_text(worker_sandbox=self.WIDE), entry_type="symlink"), "invalid")):
            with self.subTest(source=source, adapter=adapter.entry_type):
                verdict = self.resolve(adapter)
                self.assertEqual(verdict["policy_source"], source)
                self.assertEqual(verdict["worker_sandbox"], {"allowed_domains": [], "cache_directories": []})
                self.assertTrue(verdict["ok"])
                self.assertIsNotNone(verdict["error_code"])

    def test_an_unresolved_repository_fails_closed(self):
        verdict = wsp.resolve(self.target, "other/repository", adapter=FakeAdapter(policy_text(worker_sandbox=self.WIDE)),
                              env={})
        self.assertEqual((verdict["policy_source"], verdict["error_code"]), ("unavailable", "TARGET_REPOSITORY_CONFLICT"))
        self.assertEqual(verdict["worker_sandbox"], {"allowed_domains": [], "cache_directories": []})

    def test_user_claude_settings_that_widen_every_sandbox_are_reported(self):
        config = self.target.parent / "claude-config"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({"sandbox": {
            "excludedCommands": ["docker *"], "filesystem": {"allowWrite": []},
            "network": {"allowedDomains": ["github.com"]}}}))
        self.assertEqual(wsp.user_settings_widening({"CLAUDE_CONFIG_DIR": str(config)}),
                         ["sandbox.excludedCommands", "sandbox.network.allowedDomains"])
        home = self.target.parent / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"filesystem": {"allowWrite": ["/tmp/x"]}}}))
        self.assertEqual(wsp.user_settings_widening({"HOME": str(home)}), ["sandbox.filesystem.allowWrite"])
        (home / ".claude" / "settings.json").write_text("{not json")
        self.assertEqual(wsp.user_settings_widening({"HOME": str(home)}), [])
        self.assertEqual(self.resolve(FakeAdapter(None), env={"CLAUDE_CONFIG_DIR": str(config)})["user_settings_widening"],
                         ["sandbox.excludedCommands", "sandbox.network.allowedDomains"])

    def test_cli_prints_a_closed_verdict_without_github(self):
        import subprocess
        env = {key: value for key, value in os.environ.items() if key not in {"GH_TOKEN", "GITHUB_TOKEN"}}
        env["PATH"] = "/nonexistent"  # no gh: the read must fail closed, not crash
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "worker_sandbox_policy.py"), "--worktree",
                               str(self.target), "--repo", REPO], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual((verdict["policy_source"], verdict["worker_sandbox"]),
                         ("unavailable", {"allowed_domains": [], "cache_directories": []}))


if __name__ == "__main__":
    unittest.main()
