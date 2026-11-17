"""Offline checks for benchmark selection and analysis; never launches a model."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import launch_brief

spec = importlib.util.spec_from_file_location("efficiency_analysis", ROOT / "benchmarks/efficiency/analyze.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_benchmark_selector_fails_closed():
    rev = "a" * 40
    env = {"PICO_REAL_BENCHMARK": "1", "PICO_BENCHMARK_VARIANT": "baseline",
           "PICO_BENCHMARK_PAIR": "pair-1", "PICO_BENCHMARK_REVISION": rev,
           "PICO_BENCHMARK_LEAD_MODEL": "provider/model", "PICO_BENCHMARK_LEAD_EFFORT": "high"}
    assert launch_brief.benchmark_selection({}, rev) is None
    assert launch_brief.benchmark_selection(env, rev) == {"variant": "baseline", "pair": "pair-1", "revision": rev, "lead_model": "provider/model", "lead_effort": "high"}
    for broken in ({**env, "PICO_REAL_BENCHMARK": "0"}, {**env, "PICO_BENCHMARK_VARIANT": "other"},
                   {**env, "PICO_BENCHMARK_PAIR": "../x"}, {**env, "PICO_BENCHMARK_REVISION": "b" * 40},
                   {"PICO_BENCHMARK_VARIANT": "baseline"}):
        with pytest.raises(ValueError):
            launch_brief.benchmark_selection(broken, rev)


def report(tokens=100, verified=True, complete=True):
    return {"selector_observed": True, "config_snapshots_unchanged": True, "elapsed_seconds": 2, "launch": {"exit_code": 0},
            "validation": [{"task": "pilot", "validation": "VALIDATED", "exit_code": 0}],
            "metrics": {"runs": [{"tokens": {"lead": {"model": "m"}}}],
                        "totals": {"tasks": {"runs": 1, "completed": 1, "verified": int(verified)},
                                   "spend_tokens": tokens, "spend_complete": complete,
                                   "calls": {"lead": 5, "workers": {"x": 1}}, "tokens": {},
                                   "cost": {"estimated_usd": .1, "cost_complete": complete, "unpriced": []}}}}


def test_paired_analysis_never_imputes_incomplete_or_failed_data(tmp_path):
    fixed = {k: "pinned" for k in analysis.FIXED}
    fixed.update(lead_model="m", task_id="pilot", provider_caps={"tokens": 1000})
    def write(name, data, variant):
        data["provenance"] = {**fixed, "variant": variant, "pair": "one"}
        (tmp_path / name).write_text(json.dumps(data))
    write("b.json", report(), "baseline")
    write("c.json", report(80, complete=False), "candidate")
    sample = {"id": "one", "baseline": {"report": "b.json"}, "candidate": {"report": "c.json"}}
    manifest = {"version": 1, "samples": [sample]}
    result = analysis.analyze(manifest, tmp_path)
    assert result["summary"]["tokens"]["mean_delta"] is None
    assert result["summary"]["lead_calls"]["mean_delta"] == 0
    assert result["summary"]["lead_calls"]["sample_stddev"] is None
    write("c.json", report(80, verified=False), "candidate")
    assert analysis.analyze(manifest, tmp_path)["summary"]["lead_calls"]["mean_delta"] is None
    changed = report()
    write("c.json", changed, "candidate")
    data = json.loads((tmp_path / "c.json").read_text())
    data["provenance"]["lead_effort"] = "different"
    (tmp_path / "c.json").write_text(json.dumps(data))
    with pytest.raises(analysis.Invalid, match="unmatched fixed inputs"):
        analysis.analyze(manifest, tmp_path)


def test_fixture_has_reproducible_base_and_trusted_check(tmp_path):
    import subprocess
    spec = importlib.util.spec_from_file_location("efficiency_setup", ROOT / "benchmarks/efficiency/setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    a = setup.create(tmp_path / "a", 10000, 20, 600)
    b = setup.create(tmp_path / "b", 10000, 20, 600)
    assert a["base_commit"] == b["base_commit"]
    assert a["task_sha256"] == b["task_sha256"]
    repo = tmp_path / "a/pilot"
    argv = [sys.executable, str(ROOT / "benchmarks/efficiency/acceptance.py"), a["base_commit"]]
    assert subprocess.run(argv, cwd=repo, capture_output=True).returncode != 0
    file = repo / "arithmetic.py"
    file.write_text(file.read_text().replace("return a + b", "return a - b"))
    assert subprocess.run(argv, cwd=repo, capture_output=True).returncode == 0
    file.write_text(file.read_text() + "# unrelated\n")
    assert subprocess.run(argv, cwd=repo, capture_output=True).returncode != 0


def test_execution_rejects_changing_selection_on_resume(monkeypatch, tmp_path):
    import headless_run
    rev = "a" * 40
    for name, value in {"PICO_REAL_BENCHMARK": "1", "PICO_BENCHMARK_VARIANT": "candidate",
                        "PICO_BENCHMARK_PAIR": "pair-1", "PICO_BENCHMARK_REVISION": rev,
                        "PICO_BENCHMARK_LEAD_MODEL": "provider/model", "PICO_BENCHMARK_LEAD_EFFORT": "high"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(headless_run.workspace_plan, "orchestrator_revision", lambda root: rev)
    monkeypatch.setattr(headless_run, "load_record", lambda path: {"benchmark": {"variant": "baseline"}})
    with pytest.raises(headless_run.HeadlessError, match="BENCHMARK_SELECTOR_MISMATCH"):
        headless_run._launch_locked(tmp_path / "execution.json", worktree=tmp_path, identity="x/y", common=tmp_path,
                                   execution_id="x", objective="x", allow_mutation=False, resume=True,
                                   pi_executable="pi", python=sys.executable)


def test_real_runner_refuses_without_opt_in_and_creates_nothing(tmp_path):
    import os
    import subprocess
    output = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k != "PICO_REAL_BENCHMARK"}
    result = subprocess.run([sys.executable, str(ROOT / "benchmarks/efficiency/run.py"), "a" * 40,
                             "pair-1", "baseline", str(output), "--max-tokens", "1000", "--max-lead-calls", "5",
                             "--duration", "30", "--lead-model", "x/y", "--lead-effort", "high",
                             "--model-config", str(tmp_path / "models")], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "PICO_REAL_BENCHMARK=1" in result.stderr
    assert not output.exists()


def test_provider_cost_ceiling_excludes_launches_and_requires_settled_requests():
    settled_launch = {"state": "settled", "reserved": {"requests": 0}, "actual": {"requests": 0, "cost_usd": "99"}}
    settled_request = {"state": "settled", "reserved": {"requests": 1}, "actual": {"requests": 1, "cost_usd": "0.01234"}}
    unresolved = {"state": "uncertain", "reserved": {"requests": 1}, "actual": None}
    assert analysis.provider_contract_cost_ceiling({"entries": [settled_launch]})["ceiling_usd"] is None
    priced = analysis.provider_contract_cost_ceiling({"entries": [settled_launch, settled_request]})
    assert priced["coverage"] == "complete_observed_requests"
    assert priced["ceiling_usd"] == .01234
    incomplete = analysis.provider_contract_cost_ceiling({"entries": [settled_launch, settled_request, unresolved]})
    assert incomplete["coverage"] == "incomplete"
    assert incomplete["ceiling_usd"] is None
    assert incomplete["unsettled_requests"] == 1
    malformed = analysis.provider_contract_cost_ceiling({"entries": [settled_request, {"reserved": {}}]})
    assert malformed["ceiling_usd"] is None


def test_effort_observation_is_independent_of_other_metrics():
    assert analysis.effort_observation({}, "high")["status"] == "unobserved"
    evidence = {"provider_ledger": {"lead_effort_observations": [{"source": "gateway-request", "value": "high"}]}}
    assert analysis.effort_observation(evidence, "high")["status"] == "matches_configured"
    assert analysis.effort_observation(evidence, "low")["status"] == "differs_from_configured"


def test_private_snapshots_and_runtime_fingerprint(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("efficiency_runner", ROOT / "benchmarks/efficiency/run.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    snapshot, expected = runner.snapshot_config(config, "provider.json", b'{"a": 1}')
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert runner.digest(snapshot) == expected
    snapshot.write_bytes(b'{"a": 2}')
    assert runner.digest(snapshot) != expected
    pi = tmp_path / "pi"
    node = tmp_path / "node"
    extension = tmp_path / "extension.ts"
    for path in (pi, node, extension):
        path.write_text(path.name)
    monkeypatch.setattr(runner.shutil, "which", lambda name: str(node))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: type("Result", (), {"stdout": "1.2.3\n"})())
    before = runner.runtime_fingerprint(str(pi), {"extensions": [str(extension)]})
    extension.write_text("changed")
    after = runner.runtime_fingerprint(str(pi), {"extensions": [str(extension)]})
    assert before["pi_target_sha256"] == after["pi_target_sha256"]
    assert before["configured_extensions_sha256"] != after["configured_extensions_sha256"]


def test_analysis_excludes_changed_snapshots_and_mismatched_runtime(tmp_path):
    fixed = {k: "pin" for k in analysis.FIXED}
    fixed.update(lead_model="m", lead_effort="high", task_id="pilot", provider_caps={"tokens": 1000},
                 runtime_fingerprint={"pi_version": "1"})
    for variant in ("baseline", "candidate"):
        payload = report()
        payload["provenance"] = {**fixed, "variant": variant, "pair": "one"}
        if variant == "candidate":
            payload["config_snapshots_unchanged"] = False
        (tmp_path / (variant + ".json")).write_text(json.dumps(payload))
    manifest = {"version": 1, "samples": [{"id": "one", "baseline": {"report": "baseline.json"},
                                             "candidate": {"report": "candidate.json"}}]}
    result = analysis.analyze(manifest, tmp_path)
    assert result["summary"]["lead_calls"]["mean_delta"] is None
    candidate = json.loads((tmp_path / "candidate.json").read_text())
    candidate["provenance"]["runtime_fingerprint"] = {"pi_version": "2"}
    (tmp_path / "candidate.json").write_text(json.dumps(candidate))
    with pytest.raises(analysis.Invalid, match="runtime_fingerprint"):
        analysis.analyze(manifest, tmp_path)
