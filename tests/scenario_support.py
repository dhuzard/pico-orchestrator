"""Harness of the deterministic synthetic pilot (issue #49).

It builds the synthetic workspace of benchmarks/synthetic-workspace/ (two repositories, four tasks, a
provides/requires contract, a budget and time limits), installs a scripted fake `pi`, and runs
everything else for real: the scheduler, the launcher, the run lease, the recorder, the contract tools
(validate, publish, ci, promote against a fake GitHub that serves a bare repository on disk) and the
benchmark metrics. No model is called and nothing touches the network.

Scenario scripts are data (see tests/scenario_lead.py). `implement()` is the usual script of a task
that completes with the reference solution.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import benchmark_metrics as bm  # noqa: E402
import github_integration as gi  # noqa: E402
import headless_run as hr  # noqa: E402
import provenance  # noqa: E402
import run_lease  # noqa: E402
import session_scope  # noqa: E402
import workspace_contracts as wc  # noqa: E402
import workspace_run as wr  # noqa: E402
import workspace_scheduler as ws  # noqa: E402
from scenario_lead import ERROR_MARKER  # noqa: E402
from test_headless_run import FAKE_PI, git  # noqa: E402
from test_workspace_contracts import GREEN, PENDING, FakeGithub  # noqa: E402,F401

BENCH = ROOT / "benchmarks" / "synthetic-workspace"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
UPDATE_ENV = "PICO_UPDATE_GOLDEN"
OWNER = "pico-bench-sandbox"
WORKSPACE = "pico-bench"
ISSUES = {"lib-slugify": 1, "lib-stopwords": 2, "app-post-url": 3}
REPO_OF = {"lib-slugify": "synth-lib", "lib-stopwords": "synth-lib", "app-post-url": "synth-app"}
POLL_TIMEOUT = 20

SCENARIO_BRANCH = '''elif behavior == "scenario":
    sys.path.insert(0, os.environ["FAKE_SCENARIO_LEAD_DIR"])
    import scenario_lead
    scenario_lead.run(tools=tools, run_id=run_id, owner=owner, session_dir=sdir)
elif behavior == "crash":'''


def fake_pi_source() -> str:
    """The suite's fake `pi` (lease, run start/restore, session) with the scenario behavior added."""
    source = FAKE_PI.replace('elif behavior == "crash":', SCENARIO_BRANCH, 1)
    assert source != FAKE_PI, "the fake pi no longer has the crash branch the scenario behavior hooks into"
    first, rest = source.split("\n", 1)
    return f"#!{sys.executable}\n{rest}"


# -- scripts ------------------------------------------------------------------------------------------

def ev(**event) -> dict:
    return {"op": "event", "event": event}


def implement(*, tokens: int | None = None, cost: float | None = None, route: str = "CLAUDE_CODEX") -> list[dict]:
    """What a competent lead and extension record for a task that ends COMPLETED with the reference solution."""
    ops = [
        ev(kind="phase", phase="$TASK", status="in_progress", route=route),
        ev(kind="worker", phase="$TASK", step_id="claude_worker", status="in_progress", model="claude-sonnet-5-5",
           backend="claude-code", backend_model="claude-sonnet-5-5"),
        ev(kind="activity", status="started", tool="ClaudeWorker", task_id="$TASK", step_id="claude_worker"),
        {"op": "work"},
        ev(kind="checkpoint", phase="$TASK", reason="worker_approval", workspace_bound=True),
    ]
    if tokens:
        usage = {"kind": "usage", "backend": "claude-code", "input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}
        if cost is not None:
            usage["estimated_cost_usd"] = cost
        ops.append({"op": "event", "event": usage})
    ops += [
        ev(kind="activity", status="finished", tool="ClaudeWorker", task_id="$TASK", step_id="claude_worker"),
        ev(kind="worker", phase="$TASK", step_id="claude_worker", status="completed"),
        ev(kind="worker", phase="$TASK", step_id="codex_reviewer", status="completed", model="gpt-6.1-sol",
           backend="codex", backend_model="gpt-6.1-sol"),
        ev(kind="phase", phase="$TASK", status="completed"),
        ev(kind="wave", wave_id="w-$TASK", task_ids=["$TASK"], status="declared"),
        ev(kind="wave", wave_id="w-$TASK", task_ids=["$TASK"], status="integrating"),
        ev(kind="wave", wave_id="w-$TASK", task_ids=["$TASK"], status="integrated"),
        {"op": "finish", "status": "COMPLETED"},
    ]
    return ops


def start_work(*extra: dict) -> list[dict]:
    """A task that is under way: routed, one worker running. `extra` ops follow (hang, crash, ...)."""
    return [ev(kind="phase", phase="$TASK", status="in_progress", route="CLAUDE_CODEX"),
            ev(kind="worker", phase="$TASK", step_id="claude_worker", status="in_progress", model="claude-sonnet-5-5",
               backend="claude-code", backend_model="claude-sonnet-5-5"),
            ev(kind="checkpoint", phase="$TASK", reason="worker_approval"), *extra]


def open_items(parent: dict) -> list[str]:
    """Phases, workers and waves still not_started, in_progress, declared or integrating."""
    open_states = {"not_started", "in_progress"}
    return ([f"phase {p['id']}" for p in parent["phases"] if p["status"] in open_states]
            + [f"worker {p['id']}/{w['step_id']}" for p in parent["phases"] for w in p["workers"] if w["status"] in open_states]
            + [f"wave {w['id']}" for w in parent.get("waves") or [] if w["status"] in {"declared", "integrating"}])


def load_setup():
    spec = importlib.util.spec_from_file_location("synthetic_workspace_setup", BENCH / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stop_group(pid: int) -> None:
    """Kill a child's whole process group and wait until it is gone, so nothing writes into the temp state afterwards."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.02)
    raise AssertionError(f"process group {pid} survived SIGKILL")


class SyntheticPilot(unittest.TestCase):
    """The synthetic workspace with a scripted fake lead, a fake GitHub and an isolated state directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        created = load_setup().create(self.base / "ws", publication=True, issues=ISSUES)
        self.manifest = Path(created["manifest"])
        self.repos = {name: Path(path) for name, path in created["repositories"].items()}
        self.pins = created["pinned_commits"]
        self.bare = {}
        for name, path in self.repos.items():
            # CI runners have no global Git identity: every repository the tests commit to gets a local one.
            git("-C", str(path), "config", "user.name", "Pilot Test")
            git("-C", str(path), "config", "user.email", "pilot@example.invalid")
            self.bare[name] = self.base / f"{name}.git"
            git("init", "-q", "--bare", str(self.bare[name]))
        self.state = self.base / "state"
        self.signals = self.base / "signals"
        self.signals.mkdir()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        self.pi = bin_dir / "pi"
        self.pi.write_text(fake_pi_source())
        self.pi.chmod(0o755)
        saved = dict(os.environ)
        self.addCleanup(self._restore_env, saved)
        for name in [n for n in os.environ if n.startswith(("PI_ORCHESTRATION_", "FAKE_PI", "FAKE_SCENARIO"))]:
            del os.environ[name]
        os.environ.update({
            provenance.STATE_ENV: str(self.state), "FAKE_PI_TOOLS": str(ROOT / "tools"),
            "PI_CODING_AGENT_DIR": str(self.base / "agent"), "FAKE_PI_BEHAVIOR": "scenario",
            "FAKE_PI_LOG": str(self.base / "fake.log"), "FAKE_SCENARIO": str(self.base / "scenario.json"),
            "FAKE_SCENARIO_SIGNALS": str(self.signals), "FAKE_SCENARIO_REFERENCE": str(BENCH / "reference"),
            "FAKE_SCENARIO_LEAD_DIR": str(ROOT / "tests")})
        self.fake = FakeGithub()
        for name, path in self.repos.items():
            # GitHub's base branch carries the repository's publication policy; the checkout's copy is never authority.
            self.fake.merge_policy(f"{OWNER}/{name}", (path / ".pi" / "autonomy.yaml").read_text())
        real_run = subprocess.run

        def redirect(argv, *args, **kwargs):
            if isinstance(argv, list) and argv[:1] == ["git"] and len(argv) > 3 and argv[3] in {"ls-remote", "push"}:
                target = Path(argv[2]).resolve()
                repo = next(n for n, p in self.repos.items() if p.resolve() == target)
                argv = [str(self.bare[repo]) if a == "origin" else a for a in argv]
            return real_run(argv, *args, **kwargs)
        patcher = mock.patch.object(gi.subprocess, "run", side_effect=redirect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.children: list[int] = []
        self.addCleanup(self._stop_children)

    @staticmethod
    def _restore_env(saved: dict) -> None:
        os.environ.clear()
        os.environ.update(saved)

    def _stop_children(self) -> None:
        for pid in self.children:
            stop_group(pid)

    def tearDown(self):
        super().tearDown()
        self.assertEqual(self.fake_errors(), [], "a scripted child reported a scenario error")

    # -- scripting ------------------------------------------------------------------------------------
    def script(self, scripts: dict[str, list[list[dict]]]) -> None:
        """Per task, the ops of each launch (a resume is the next launch)."""
        (self.base / "scenario.json").write_text(json.dumps(scripts))

    def edit_manifest(self, tasks: dict[str, dict] | None = None, **top) -> None:
        data = yaml.safe_load(self.manifest.read_text())
        data.update(top)
        for task in data["tasks"]:
            task.update((tasks or {}).get(task["id"], {}))
        self.manifest.write_text(yaml.safe_dump(data, sort_keys=False, width=120))

    def fake_errors(self) -> list[str]:
        return [str(log) for log in self.state.rglob("*.log") if ERROR_MARKER in log.read_text(errors="replace")]

    def answers(self, task: str) -> list[dict]:
        """Every recorder answer the scripted child of `task` received, in order."""
        found = []
        for path in sorted(self.state.rglob(f"events-{task}.jsonl")):
            found += [json.loads(line) for line in path.read_text().splitlines()]
        return found

    # -- running --------------------------------------------------------------------------------------
    def schedule(self, **kwargs) -> dict:
        return ws.run(self.manifest, access_check=None, pi_executable=str(self.pi), poll_seconds=0.05,
                      allow_mutation=True, **kwargs)

    def launch(self, task: str, **kwargs) -> dict:
        return wr.launch(manifest=self.manifest, task_id=task, access_check=None, pi_executable=str(self.pi),
                         allow_mutation=True, **kwargs)

    def launch_background(self, task: str, **kwargs) -> dict:
        result = self.launch(task, wait=False, **kwargs)
        self.children.append(self.execution(task)["pid"])
        return result

    def execution(self, task: str) -> dict:
        repo = REPO_OF[task]
        return hr.load_record(hr.execution_path(session_scope.common_dir(self.repos[repo]), wr.execution_id(WORKSPACE, task)))

    def wait_for(self, condition, what: str, timeout: float = POLL_TIMEOUT):
        """Bounded polling; no fixed sleeps."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def run_id(self, task: str) -> str:
        record = self.execution(task)
        return self.wait_for(lambda: hr.discover_run_id(Path(record["session_dir"]), record["session_id"]), f"{task} to start its run")

    def parent_record(self, task: str) -> dict:
        return provenance.get_run(hr.ROOT, self.run_id(task))["parent_orchestration"]

    def wait_for_phase(self, task: str, status: str = "in_progress") -> None:
        self.wait_for(lambda: any(p["id"] == task and p["status"] == status for p in self.parent_record(task)["phases"]),
                      f"{task} to record its phase as {status}")

    def workspace(self) -> dict:
        return wr.load_workspace(wr.workspace_record_path(WORKSPACE))

    def plan_task(self, task: str) -> dict:
        return next(t for t in wr._plan(self.manifest, access_check=None)["tasks"] if t["id"] == task)

    def peak_overlap(self) -> tuple[int, dict]:
        """Peak concurrent children overall and per repository, from the fake pi's start/end log."""
        events = sorted(line.split() for line in (self.base / "fake.log").read_text().splitlines())
        active, peak, per_repo, repo_peak = 0, 0, {}, {}
        for _, event, repo in events:
            delta = 1 if event == "start" else -1
            active += delta
            per_repo[repo] = per_repo.get(repo, 0) + delta
            peak = max(peak, active)
            repo_peak[repo] = max(repo_peak.get(repo, 0), per_repo[repo])
        return peak, repo_peak

    def states(self, result: dict) -> dict:
        return {row["id"]: row["state"] for row in result["tasks"]}

    # -- the contract lifecycle through the fake GitHub -----------------------------------------------
    def validate(self, task: str) -> dict:
        result = wc.validate(manifest=self.manifest, task_id=task, head=f"pico/{task}")
        self.assertEqual(result["status"], "VALIDATED", result)
        return result

    def publish(self, task: str) -> int:
        preview = wc.publish(manifest=self.manifest, task_id=task, adapter=self.fake)
        self.assertEqual(preview["status"], "DRY_RUN", preview)
        applied = wc.publish(manifest=self.manifest, task_id=task, apply=True, plan_hash=preview["plan_hash"], adapter=self.fake)
        self.assertEqual(applied["status"], "COMPLETED", applied)
        return applied["pr"]

    def release(self, task: str) -> None:
        """validate -> publish (draft) -> CI green -> promote: the only way a task becomes ready_for_downstream."""
        commit = self.validate(task)["observed"]["validated_commit"]
        number = self.publish(task)
        self.fake.checks[number] = ("OPEN", commit, GREEN)
        promoted = wc.promote(manifest=self.manifest, task_id=task, adapter=self.fake)
        self.assertEqual(promoted["readiness"], "ready_for_downstream")

    def pending_ci(self, task: str, number: int, commit: str) -> None:
        self.fake.checks[number] = ("OPEN", commit, PENDING)

    def complete_lib_slugify(self, **kwargs) -> dict:
        """Run lib-slugify to COMPLETED and release it, so the other tasks can start."""
        result = self.launch("lib-slugify", **kwargs)
        self.assertEqual(result["task"]["run_status"], "COMPLETED", result)
        self.release("lib-slugify")
        return result

    # -- golden facts -----------------------------------------------------------------------------------
    def snapshot(self, scenario: str, tasks: list[str]) -> dict:
        """What the scenario left in provenance: facts per run (identities and statuses, no timestamps or ids)."""
        record = self.workspace()
        schema = json.loads((ROOT / "schemas" / "run-record.schema.json").read_text())
        runs, workspace = {}, {}
        for task in tasks:
            entry = record["tasks"][task]
            run = bm.load_run(self.state, entry["run_id"])
            self.assertEqual(provenance.validate_run_record(run, schema), [], f"{task}: the run record violates the schema")
            report = bm.run_report(run, state=self.state, task=task, entry=entry)
            facts = [{k: v for k, v in fact.items() if k not in {"ids", "evidence"}} for fact in bm.provenance_facts(run)]
            runs[task] = {"facts": facts, "spend_tokens": report["spend_tokens"], "lead_calls": report["calls"]["lead"],
                          "lead_limit": report["calls"].get("lead_limit"),
                          "open_items": open_items(run["parent_orchestration"])}
            workspace[task] = {key: entry.get(key) for key in ("execution_status", "run_status", "run_blocker_code", "launches")}
            workspace[task]["state"] = ws.state_of({"id": task}, entry, {})
        return {"scenario": scenario, "runs": runs, "workspace": workspace}

    def assert_golden(self, snapshot: dict) -> None:
        path = GOLDEN / f"{snapshot['scenario']}.json"
        if os.environ.get(UPDATE_ENV):
            if os.environ.get("CI"):
                self.fail(f"{UPDATE_ENV} must never be set in CI")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return
        golden = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["workspace"], golden["workspace"])
        for task, expected in golden["runs"].items():
            actual = snapshot["runs"][task]
            # The metrics tool's own completeness check, both ways: nothing lost, nothing unexpected.
            self.assertEqual(bm.diff_facts(expected["facts"], actual["facts"]), [], f"{task}: provenance lost against the golden record")
            self.assertEqual(bm.diff_facts(actual["facts"], expected["facts"]), [], f"{task}: provenance not in the golden record")
            self.assertEqual({k: v for k, v in actual.items() if k != "facts"}, {k: v for k, v in expected.items() if k != "facts"}, task)
        self.assertEqual(sorted(snapshot["runs"]), sorted(golden["runs"]))

    def lease(self, task: str) -> dict:
        return run_lease.status(self.run_id(task), repo_root=hr.ROOT)
