"""Deterministic, no-inference coverage of the supported launch chain:

canonical launcher -> Claude backend identity pin -> external target repo ->
route_task -> backend preflight -> READY, plus the fail-closed blockers.

`pi` and `claude` are fakes on PATH. The fake Claude answers only the
`--version` and `auth status --json` probes; any other invocation (an
inference attempt) is logged and asserted absent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import routing_engine as engine  # noqa: E402

PIN_ENV = "PI_ORCHESTRATOR_CLAUDE_BACKEND_IDENTITY"
TOOLS = ["route_task", "spawn_subagent", "ClaudeWorker", "orchestration_record"]
SEED_PARTS = ("extensions", "tools", "schemas", "routing", "prompts", "scripts", "templates", ".github")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class _ChainFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = engine.load_yaml(ROOT / "tests/routing-cases.yaml")["cases"]
        # Routes to CODEX_CLAUDE_CODEX (Claude high tier -> minimum backend version)
        # and may be rerouted to CODEX when the Claude candidate is unavailable.
        cls.task = next(c["task"] for c in cases if c["id"] == "case-5-broad-critical-semantics")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.agent = self.base / "agent"
        self.agent.mkdir()
        (self.agent / "settings.json").write_text(json.dumps(
            {"packages": ["npm:pi-claude-bridge", "npm:pi-subagent-workflows"]}))
        self.inference_log = self.base / "inference.log"
        self.capture = self.base / "pi-launch.json"
        self.target = self.base / "external-target"
        _git("init", "-q", str(self.target), cwd=self.base)
        _git("remote", "add", "origin", "git@github.com:Examplecorp/examplelab.git", cwd=self.target)
        self.write_fake_claude("2.1.287")
        fake_pi = self.bin / "pi"
        fake_pi.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"json.dump({{'cwd': os.getcwd(), 'pin': os.environ.get({PIN_ENV!r}),"
            " 'repo': os.environ.get('PI_TARGET_REPOSITORY'),"
            " 'model_config': os.environ.get('PI_ORCHESTRATOR_MODEL_CONFIG'),"
            " 'args': sys.argv[1:]},"
            f" open({str(self.capture)!r}, 'w'))\n"
        )
        fake_pi.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixtures -----------------------------------------------------------

    def write_fake_claude(self, version: str, name: str = "claude") -> Path:
        path = self.bin / name
        tmp = path.with_suffix(".new")
        tmp.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = '--version' ]; then echo '{version} (Claude Code)'; exit 0; fi\n"
            "if [ \"$1\" = 'auth' ]; then echo '{\"loggedIn\":true}'; exit 0; fi\n"
            f"echo \"$@\" >> '{self.inference_log}'\nexit 97\n"
        )
        tmp.chmod(0o755)
        tmp.replace(path)  # new inode, same path
        return path

    def env(self, **extra: str) -> dict:
        env = {key: value for key, value in os.environ.items() if key != PIN_ENV}
        env.update(PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}", PI_CODING_AGENT_DIR=str(self.agent))
        env.update(extra)
        return env

    def launch_local(self) -> dict:
        proc = subprocess.run(["bash", str(ROOT / "scripts/run-local.sh"), str(self.target),
                               "--repo", "Examplecorp/examplelab"],
                              cwd=self.base, env=self.env(), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pi-coding-orchestrator: using local checkout", proc.stderr)
        return json.loads(self.capture.read_text())

    def route(self, unavailable: list[str] | None = None) -> dict:
        payload = {"classified_task": self.task, "unavailable_routes": unavailable or []}
        proc = subprocess.run([sys.executable, str(ROOT / "tools/route_task.py"), "--project-routing",
                               str(self.target / ".pi" / "routing.yaml")],
                              cwd=self.target, input=json.dumps(payload), env=self.env(),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def preflight(self, route: dict, pin: str | None, tools: list[str] = TOOLS) -> dict:
        env = self.env() if pin is None else self.env(**{PIN_ENV: pin})
        proc = subprocess.run([sys.executable, str(ROOT / "tools/backend_preflight.py")], cwd=ROOT,
                              input=json.dumps({"route_task_result": route, "available_tools": tools,
                                                "cwd": str(self.target)}),
                              env=env, capture_output=True, text=True)
        result = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0 if result["ok"] else 1, proc.stderr)
        return result

    def assert_no_inference_and_clean_target(self):
        self.assertFalse(self.inference_log.exists(), "preflight must never invoke inference")
        self.assertEqual(_git("status", "--porcelain", "--ignored", cwd=self.target), "")
        self.assertFalse((self.target / ".pi" / "provenance").exists())


class LauncherPreflightChainTest(_ChainFixture):
    # -- supported chain ----------------------------------------------------

    def test_run_local_pin_external_target_route_preflight_ready(self):
        launch = self.launch_local()
        self.assertEqual(Path(launch["cwd"]).resolve(), self.target.resolve())
        self.assertEqual(launch["repo"], "Examplecorp/examplelab")
        for extension in ("model-router.ts", "workflow-commands.ts", "mission-controller.ts",
                          "orchestration-lifecycle.ts"):
            self.assertIn(str(ROOT / "extensions" / extension), launch["args"])
        pin = json.loads(launch["pin"])
        self.assertEqual(pin["backend_executable"], str((self.bin / "claude").resolve()))
        route = self.route()
        self.assertEqual(route["selected_route"], "CODEX_CLAUDE_CODEX")
        result = self.preflight(route, launch["pin"])
        self.assertEqual(result["status"], "READY", result)
        claude = next(step for step in result["checked_steps"] if step["backend"] == "claude")
        self.assertEqual(claude["backend_model"], "claude-opus-5-5")
        self.assertEqual(claude["installed_backend_version"], "2.1.287")
        self.assertEqual(claude["model_access"], "unverified")
        self.assert_no_inference_and_clean_target()

    # -- blockers -----------------------------------------------------------

    def test_missing_pin_is_uninitialized_with_launcher_guidance(self):
        self.launch_local()
        result = self.preflight(self.route(), None)
        self.assertEqual(result["blocker_code"], "BACKEND_IDENTITY_UNINITIALIZED")
        self.assertEqual(result["identity_state"], "missing")
        self.assertIn("scripts/use-latest.sh", result["remediation"])
        self.assertIn("scripts/run-local.sh", result["remediation"])
        without_ask_claude = self.preflight(self.route(), None, tools=["spawn_subagent", "orchestration_record"])
        self.assertEqual(without_ask_claude["blocker_code"], "BACKEND_IDENTITY_UNINITIALIZED")
        self.assert_no_inference_and_clean_target()

    def test_changed_identity_is_backend_mismatch(self):
        pin = self.launch_local()["pin"]
        self.write_fake_claude("2.1.287")  # same path and version, replaced executable
        result = self.preflight(self.route(), pin)
        self.assertEqual(result["blocker_code"], "MODEL_BACKEND_MISMATCH")
        other = self.write_fake_claude("2.1.287", name="other-claude")
        (self.agent / "claude-bridge.json").write_text(json.dumps(
            {"provider": {"pathToClaudeCodeExecutable": str(other)}}))
        self.assertEqual(self.preflight(self.route(), pin)["blocker_code"], "MODEL_BACKEND_MISMATCH")
        self.assert_no_inference_and_clean_target()

    def test_unsupported_version_blocks_and_reroute_requires_policy_valid_candidate(self):
        self.write_fake_claude("2.1.251")
        pin = self.launch_local()["pin"]
        route = self.route()
        result = self.preflight(route, pin)
        self.assertEqual(result["blocker_code"], "MODEL_BACKEND_VERSION_UNSUPPORTED")
        self.assertEqual(result["required_backend_version"], ">=2.1.280")
        rerouted = self.route([route["selected_route"]])
        self.assertEqual(rerouted["selected_route"], "CODEX")
        self.assertEqual(self.preflight(rerouted, pin)["status"], "READY")
        exhausted = self.route([route["selected_route"], "CODEX"])
        self.assertEqual(exhausted["blocker_code"], "NO_AVAILABLE_ROUTE")
        self.assert_no_inference_and_clean_target()

    def test_unavailable_backend_blocks(self):
        pin = self.launch_local()["pin"]
        route = self.route()
        self.assertEqual(self.preflight(route, pin, tools=["spawn_subagent", "orchestration_record"])
                         ["blocker_code"], "MODEL_BACKEND_UNAVAILABLE")
        (self.bin / "claude").unlink()
        self.assertEqual(self.preflight(route, pin)["blocker_code"], "MODEL_BACKEND_UNAVAILABLE")
        self.assert_no_inference_and_clean_target()

    def test_launcher_without_claude_starts_and_reroutes_claude_routes(self):
        (self.bin / "claude").unlink()
        # Keep the test interpreter (with its dependencies) ahead of the system one.
        python_dir = os.path.dirname(sys.executable)
        env = self.env(PATH=os.pathsep.join([str(self.bin), python_dir, "/usr/bin", "/bin"]))
        if shutil.which("claude", path=env["PATH"]):
            self.skipTest("a system-wide claude is installed in /usr/bin or /bin")
        proc = subprocess.run(["bash", str(ROOT / "scripts/run-local.sh"), str(self.target)],
                              cwd=self.base, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Claude Code is not installed", proc.stderr)
        pin = json.loads(self.capture.read_text())["pin"]
        self.assertEqual(json.loads(pin), {"backend": "claude-code", "unavailable": True})
        # A Claude step is a reroutable availability blocker, not an identity failure.
        blocked = self.preflight(self.route(), pin)
        self.assertEqual(blocked["blocker_code"], "MODEL_BACKEND_UNAVAILABLE")
        self.assertEqual(self.preflight(self.route(["CODEX_CLAUDE_CODEX"]), pin)["status"], "READY")


class _CacheFixture(_ChainFixture):
    """A seeded origin, cache, and state directory for use-latest.sh."""

    def setUp(self):
        super().setUp()
        self.seed = self.base / "seed"
        self.seed.mkdir()
        for part in SEED_PARTS:
            shutil.copytree(ROOT / part, self.seed / part, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / ".gitignore", self.seed / ".gitignore")
        _git("init", "-q", "-b", "main", str(self.seed), cwd=self.base)
        self.commit("fixture")
        self.origin = self.base / "origin.git"
        _git("clone", "-q", "--bare", str(self.seed), str(self.origin), cwd=self.base)
        _git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        self.cache = self.base / "cache"
        self.state = self.base / "state"
        fake_gh = self.bin / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 0\n")
        fake_gh.chmod(0o755)

    def commit(self, message: str) -> str:
        _git("add", "-A", cwd=self.seed)
        _git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-q", "--allow-empty", "-m", message, cwd=self.seed)
        return _git("rev-parse", "HEAD", cwd=self.seed)

    def use_latest(self, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = self.env(PI_CODING_ORCHESTRATOR_REPO_URL=str(self.origin),
                       PI_CODING_ORCHESTRATOR_HOME=str(self.cache),
                       PI_CODING_ORCHESTRATOR_STATE_DIR=str(self.state))
        env.pop("PI_CODING_ORCHESTRATOR_REF", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(["bash", str(ROOT / "scripts/use-latest.sh"), *args],
                              cwd=self.base, env=env, capture_output=True, text=True)

    def cache_head(self) -> str:
        return _git("rev-parse", "HEAD", cwd=self.cache / "repo")



class UseLatestCacheTest(_CacheFixture):
    """use-latest.sh refreshes the cache before execution and preserves records."""

    def test_install_creates_reusable_pico_wrapper(self):
        install_bin = self.base / "installed-bin"
        env = self.env(
            PI_CODING_ORCHESTRATOR_REPO_URL=str(self.origin),
            PI_CODING_ORCHESTRATOR_HOME=str(self.cache),
            PI_CODING_ORCHESTRATOR_STATE_DIR=str(self.state),
            PI_CODING_ORCHESTRATOR_BIN_DIR=str(install_bin),
        )
        env.pop("PI_CODING_ORCHESTRATOR_REF", None)
        proc = subprocess.run(
            ["bash", str(ROOT / "scripts/use-latest.sh"), "install"],
            cwd=self.base, env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pico = install_bin / "pico"
        self.assertTrue(pico.is_file())
        self.assertTrue(os.access(pico, os.X_OK))
        self.assertIn(f"Installed: {pico}", proc.stdout)

        path_proc = subprocess.run(
            [str(pico), "path"], cwd=self.base, env=env, capture_output=True, text=True,
        )
        self.assertEqual(path_proc.returncode, 0, path_proc.stderr)
        runtime = Path(path_proc.stdout.strip())
        self.assertEqual(runtime.parent.resolve(), (self.cache / "runtimes").resolve())
        self.assertEqual((runtime / ".pico-runtime").read_text().strip(), self.cache_head())

    def test_use_latest_pins_and_reaches_ready_from_external_target(self):
        proc = self.use_latest("run", str(self.target), "--repo", "Examplecorp/examplelab")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        launch = json.loads(self.capture.read_text())
        runtime = self.cache / "runtimes" / self.cache_head()
        self.assertIn(str(runtime / "extensions" / "workflow-commands.ts"), launch["args"])
        self.assertEqual(self.preflight(self.route(), launch["pin"])["status"], "READY")
        self.assert_no_inference_and_clean_target()

    def test_pico_models_config_is_user_owned_and_propagated_to_run(self):
        model_config = self.base / "user-config" / "models.yaml"
        env_extra = {"PI_ORCHESTRATOR_MODEL_CONFIG": str(model_config)}
        set_proc = self.use_latest(
            "models", "set", "codex", "high", "openai-codex/gpt-7-pro",
            "--ambiguity-capacity", "8",
            env_extra=env_extra,
        )
        self.assertEqual(set_proc.returncode, 0, set_proc.stderr)
        self.assertTrue(model_config.exists())

        show = self.use_latest("models", "show", env_extra=env_extra)
        self.assertEqual(show.returncode, 0, show.stderr)
        summary = json.loads(show.stdout)
        self.assertEqual(summary["models"]["codex"]["high"], "openai-codex/gpt-7-pro")

        doctor = self.use_latest("doctor", str(self.target), env_extra=env_extra)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn("Effective model profiles:", doctor.stdout)
        self.assertIn("openai-codex/gpt-7-pro", doctor.stdout)

        run = self.use_latest("run", str(self.target), env_extra=env_extra)
        self.assertEqual(run.returncode, 0, run.stderr)
        launch = json.loads(self.capture.read_text())
        self.assertEqual(Path(launch["model_config"]).resolve(), model_config.resolve())

    def test_cache_is_refreshed_to_origin_main_before_run(self):
        first = self.use_latest("run", str(self.target))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.cache_head(), _git("rev-parse", "HEAD", cwd=self.seed))
        newer = self.commit("upstream advance")
        _git("push", "-q", "origin", "main", cwd=self.seed)
        second = self.use_latest("run", str(self.target))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.cache_head(), newer, "run must not execute a stale cached commit")
        self.assertIn(f"using main @ {newer[:12]}", second.stderr)

    def test_generated_records_migrate_to_state_and_unrelated_cache_data_is_rejected(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        legacy_runs = self.cache / "repo" / "provenance" / "runs"
        legacy_runs.mkdir(parents=True, exist_ok=True)
        record = legacy_runs / "run-0123456789ab.json"
        record.write_text('{"generated": true}\n')

        self.commit("upstream advance")
        _git("push", "-q", "origin", "main", cwd=self.seed)
        proc = self.use_latest("update")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        migrated = self.state / "runs" / record.name
        self.assertTrue(migrated.exists())
        self.assertEqual(migrated.read_text(), '{"generated": true}\n')
        self.assertFalse(record.exists())

        unrelated = self.cache / "repo" / "notes.txt"
        unrelated.write_text("keep me\n")
        dirty = self.use_latest("update")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("Refusing to update dirty orchestrator cache", dirty.stderr)
        self.assertEqual(unrelated.read_text(), "keep me\n")

    def test_doctor_json_reports_revision_models_state_and_pin(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        pin = self.use_latest("pin", str(self.target))
        self.assertEqual(pin.returncode, 0, pin.stderr)
        doctor = self.use_latest("doctor", str(self.target), "--json")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        report = json.loads(doctor.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(Path(report["orchestrator"]["state_dir"]).resolve(), self.state.resolve())
        self.assertEqual(report["orchestrator"]["project_pin"], (self.target / ".pi" / "orchestrator.lock").read_text().strip())
        self.assertIn("codex", report["model_profiles"]["models"])
        self.assertEqual(len(report["orchestrator"]["revision"]), 40)

    def test_project_pin_is_reproducible_and_update_advances_it(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        pinned_head = self.cache_head()
        pin = self.use_latest("pin", str(self.target))
        self.assertEqual(pin.returncode, 0, pin.stderr)
        lock = self.target / ".pi" / "orchestrator.lock"
        self.assertEqual(lock.read_text().strip(), pinned_head)

        newer = self.commit("upstream after pin")
        _git("push", "-q", "origin", "main", cwd=self.seed)
        run = self.use_latest("run", str(self.target))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.cache_head(), pinned_head, "run must honor the project lock")

        update = self.use_latest("update", str(self.target))
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertEqual(self.cache_head(), newer)
        self.assertEqual(lock.read_text().strip(), newer)

        unpin = self.use_latest("unpin", str(self.target))
        self.assertEqual(unpin.returncode, 0, unpin.stderr)
        self.assertFalse(lock.exists())

    def test_sync_smoke_preserves_existing_repo_and_initializes_fresh_repo(self):
        agents = self.target / "AGENTS.md"
        agents.write_text("project-owned\n")
        existing = self.use_latest("sync", str(self.target), "--github-templates")
        self.assertEqual(existing.returncode, 0, existing.stderr)
        self.assertEqual(agents.read_text(), "project-owned\n")
        self.assertTrue((self.target / ".pi" / "routing.yaml").exists())
        self.assertTrue((self.target / ".github" / "pull_request_template.md").exists())

        fresh = self.base / "fresh-target"
        _git("init", "-q", str(fresh), cwd=self.base)
        result = self.use_latest("sync", str(fresh), "--github-templates")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((fresh / "AGENTS.md").exists())
        self.assertTrue((fresh / ".pi" / "routing.yaml").exists())
        self.assertTrue((fresh / ".github" / "ISSUE_TEMPLATE" / "bug.yml").exists())

    def test_reset_cache_recovers_dirty_checkout_without_touching_state(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        self.state.mkdir(parents=True, exist_ok=True)
        sentinel = self.state / "keep.txt"
        sentinel.write_text("persistent\n")
        (self.cache / "repo" / "junk.txt").write_text("dirty\n")
        blocked = self.use_latest("update")
        self.assertNotEqual(blocked.returncode, 0)
        reset = self.use_latest("reset-cache", "--force")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertFalse((self.cache / "repo" / "junk.txt").exists())
        self.assertEqual(sentinel.read_text(), "persistent\n")

    def test_uninstall_removes_wrapper_and_optional_data_only_when_requested(self):
        install_bin = self.base / "installed-bin"
        env = self.env(
            PI_CODING_ORCHESTRATOR_REPO_URL=str(self.origin),
            PI_CODING_ORCHESTRATOR_HOME=str(self.cache),
            PI_CODING_ORCHESTRATOR_STATE_DIR=str(self.state),
            PI_CODING_ORCHESTRATOR_BIN_DIR=str(install_bin),
            XDG_CONFIG_HOME=str(self.base / "config"),
        )
        proc = subprocess.run(["bash", str(ROOT / "scripts/use-latest.sh"), "install"],
                              cwd=self.base, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pico = install_bin / "pico"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "keep.txt").write_text("state\n")

        uninstall = subprocess.run([str(pico), "uninstall"], cwd=self.base, env=env,
                                   capture_output=True, text=True)
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertFalse(pico.exists())
        self.assertTrue((self.cache / "repo").exists())
        self.assertTrue((self.state / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()


class PicoAuditRegressionTest(_CacheFixture):
    """Regressions for the pico launcher audit (issues 1, 4, 8, 9, 10)."""

    def launched_runtime(self) -> Path:
        args = json.loads(self.capture.read_text())["args"]
        return Path(args[args.index("-e") + 1]).parent.parent

    def test_concurrent_sessions_keep_their_own_immutable_runtime(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        pinned = self.cache_head()
        self.assertEqual(self.use_latest("pin", str(self.target)).returncode, 0)
        newer = self.commit("upstream advance")
        _git("push", "-q", "origin", "main", cwd=self.seed)

        pinned_run = self.use_latest("run", str(self.target))
        self.assertEqual(pinned_run.returncode, 0, pinned_run.stderr)
        pinned_runtime = self.launched_runtime()
        self.assertEqual(pinned_runtime.name, pinned)
        before = sorted(str(path.relative_to(pinned_runtime)) for path in pinned_runtime.rglob("*"))

        other = self.base / "svc"
        _git("init", "-q", str(other), cwd=self.base)
        unpinned_run = self.use_latest("run", str(other))
        self.assertEqual(unpinned_run.returncode, 0, unpinned_run.stderr)
        self.assertEqual(self.launched_runtime().name, newer)
        # The pinned session's code is untouched by the other session's update.
        self.assertEqual(sorted(str(path.relative_to(pinned_runtime)) for path in pinned_runtime.rglob("*")), before)
        self.assertEqual((pinned_runtime / ".pico-runtime").read_text().strip(), pinned)
        self.assertFalse(list(pinned_runtime.rglob("__pycache__")), "snapshots must stay bytecode-free")

    def test_pinned_run_works_offline_and_unpinned_offline_fails_clearly(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        self.assertEqual(self.use_latest("pin", str(self.target)).returncode, 0)
        self.origin.rename(self.base / "origin.offline")
        offline = self.use_latest("run", str(self.target))
        self.assertEqual(offline.returncode, 0, offline.stderr)
        self.assertIn("cached, no fetch", offline.stderr)
        (self.target / ".pi" / "orchestrator.lock").unlink()
        unpinned = self.use_latest("run", str(self.target))
        self.assertNotEqual(unpinned.returncode, 0)
        self.assertIn("pin a cached commit", unpinned.stderr)

    def test_failed_reset_clone_keeps_the_working_cache(self):
        self.assertEqual(self.use_latest("update").returncode, 0)
        head = self.cache_head()
        self.origin.rename(self.base / "origin.offline")
        reset = self.use_latest("reset-cache", "--force")
        self.assertNotEqual(reset.returncode, 0)
        self.assertIn("kept existing cache", reset.stderr)
        self.assertEqual(self.cache_head(), head)
        self.assertTrue((self.cache / "repo" / "scripts" / "use-latest.sh").exists())

    def test_purge_refuses_paths_that_contain_home(self):
        home = self.base / "home"
        home.mkdir()
        (home / "precious.txt").write_text("keep\n")
        proc = subprocess.run(["bash", str(ROOT / "scripts/use-latest.sh"), "uninstall", "--purge-cache"],
                              cwd=self.base, capture_output=True, text=True,
                              env=self.env(HOME=str(home), PI_CODING_ORCHESTRATOR_HOME=str(home),
                                           PI_CODING_ORCHESTRATOR_BIN_DIR=str(self.base / "bin2")))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Refusing to purge unsafe cache path", proc.stderr)
        self.assertEqual((home / "precious.txt").read_text(), "keep\n")

    def test_run_options_are_order_independent(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        proc = subprocess.run(["bash", str(ROOT / "scripts/use-latest.sh"), "run", "--repo",
                               "Examplecorp/examplelab", str(self.target), "--", "--model", "test"],
                              cwd=elsewhere, capture_output=True, text=True,
                              env=self.env(PI_CODING_ORCHESTRATOR_REPO_URL=str(self.origin),
                                           PI_CODING_ORCHESTRATOR_HOME=str(self.cache),
                                           PI_CODING_ORCHESTRATOR_STATE_DIR=str(self.state)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        launch = json.loads(self.capture.read_text())
        self.assertEqual(Path(launch["cwd"]).resolve(), self.target.resolve())
        self.assertEqual(launch["repo"], "Examplecorp/examplelab")
        self.assertEqual(launch["args"][-2:], ["--model", "test"])
        self.assertNotIn(str(self.target), launch["args"])

    def test_doctor_reports_invalid_pin_and_broken_models_as_json(self):
        (self.target / ".pi").mkdir(exist_ok=True)
        (self.target / ".pi" / "orchestrator.lock").write_text("garbage\n")
        models = self.base / "models.yaml"
        models.write_text("schema_version: 1\nmodels: [\n")
        doctor = self.use_latest("doctor", str(self.target), "--json",
                                 env_extra={"PI_ORCHESTRATOR_MODEL_CONFIG": str(models)})
        self.assertEqual(doctor.returncode, 1)
        report = json.loads(doctor.stdout)
        self.assertFalse(report["checks"]["project_pin_valid"])
        self.assertFalse(report["checks"]["models"])
        self.assertTrue(report["model_error"])
        self.assertEqual(report["orchestrator"]["revision"], self.cache_head())

    def test_doctor_requires_the_worker_tool_packages(self):
        (self.agent / "settings.json").write_text(json.dumps({"packages": ["npm:pi-claude-bridge"]}))
        report = json.loads(self.use_latest("doctor", str(self.target), "--json").stdout)
        self.assertTrue(report["checks"]["pi_claude_bridge"])
        self.assertFalse(report["checks"]["pi_subagent_workflows"])
        self.assertFalse(report["ok"])
