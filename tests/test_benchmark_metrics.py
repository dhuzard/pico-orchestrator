#!/usr/bin/env python3
"""Tests for tools/benchmark_metrics.py on sanitized fixture records, and for the synthetic benchmark workspace."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import benchmark_metrics as bm  # noqa: E402
import provenance  # noqa: E402
import workspace_plan  # noqa: E402
import workspace_run as wr  # noqa: E402

BENCH = REPO_ROOT / "benchmarks" / "synthetic-workspace"
FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "partial-run-open-work.json").read_text(encoding="utf-8"))


def make_record(run_id: str, *, status: str = "COMPLETED", lead=None, workers=None, calls: int = 3, phases=None,
                waves=None, started="2026-01-01T00:00:00Z", ended="2026-01-01T00:10:00Z") -> dict:
    lead = lead if lead is not None else {"model": "lead-model", "input_tokens": 1000, "output_tokens": 200,
                                          "cached_tokens": 50000, "cache_write_tokens": 3000, "total_tokens": 4200,
                                          "total_tokens_complete": True, "estimated_cost_usd": 0.5}
    workers = workers if workers is not None else {
        "claude-code": {"invocations": 2, "input_tokens": 400, "output_tokens": 100, "cached_tokens": 9000,
                        "cache_write_tokens": 700, "total_tokens": 500, "total_tokens_complete": True,
                        "estimated_cost_usd": 0.25, "cost_complete": True}}
    parent = {"target_repository": "acme/repo", "policy_version": 1, "final_status": status, "blocker_code": None,
              "mutations": {"github": 0, "code": 0},
              "phases": copy.deepcopy(phases if phases is not None else [
                  {"id": "t1", "status": "completed", "route": "CLAUDE", "blocker_code": None, "wave_id": "w1", "child_run_ids": [],
                   "workers": [{"step_id": "impl", "route_epoch": 0, "model": "sonnet", "backend": "claude-code",
                                "backend_model": "sonnet", "status": "completed", "blocker_code": None}]},
                  {"id": "t2", "status": "completed", "route": "CODEX", "blocker_code": None, "wave_id": "w1", "child_run_ids": [],
                   "workers": [{"step_id": "impl", "route_epoch": 0, "model": "codex", "backend": "codex",
                                "backend_model": "codex", "status": "completed", "blocker_code": None}]}]),
              "waves": copy.deepcopy(waves if waves is not None else [{"id": "w1", "task_ids": ["t1", "t2"], "status": "integrated"}]),
              "context": {"lead_usage": lead, "worker_usage": workers, "lead_calls": [{"sequence": i + 1} for i in range(calls)]}}
    return {"run_id": run_id, "started_at": started, "ended_at": ended, "outcome": status.lower(),
            "parent_orchestration": parent}


def write_state(state: Path, runs: dict[str, dict], tasks: dict[str, dict] | None = None, workspace_id: str = "ws") -> None:
    (state / "runs").mkdir(parents=True, exist_ok=True)
    for run_id, record in runs.items():
        (state / "runs" / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
    if tasks is not None:
        (state / "workspaces").mkdir(exist_ok=True)
        (state / "workspaces" / f"{workspace_id}.json").write_text(
            json.dumps({"record_version": 1, "workspace_id": workspace_id, "tasks": tasks}), encoding="utf-8")


def passed(task_run: str, ok: bool = True) -> dict:
    return {"run_id": task_run, "execution_status": "exited", "run_status": "COMPLETED",
            "observed": {"checks": [{"id": "c", "status": "passed" if ok else "failed"}],
                         "validation": {"status": "passed" if ok else "failed"}}}


class TokenSeparation(unittest.TestCase):
    def test_lead_and_backend_tokens_stay_separate_and_unobserved_stays_null(self):
        report = bm.run_report(make_record("run-1"))
        self.assertEqual(report["tokens"]["lead"], {"input": 1000, "output": 200, "cache_read": 50000, "cache_write": 3000,
                                                    "cost_usd": 0.5, "model": "lead-model"})
        worker = report["tokens"]["workers"]["claude-code"]
        self.assertEqual((worker["input"], worker["output"], worker["cache_read"], worker["cache_write"]), (400, 100, 9000, 700))
        self.assertEqual(report["spend_tokens"], 1000 + 200 + 3000 + 400 + 100 + 700)  # cache reads never counted
        self.assertTrue(report["spend_complete"])
        self.assertEqual(report["calls"]["lead"], 3)
        self.assertEqual(report["calls"]["workers"], {"claude-code": 2})
        self.assertIsNone(report["calls"]["orchestration_record"])  # no session file
        self.assertEqual(report["wall_clock_seconds"], 600.0)

        lead = {"model": "m", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        sparse = bm.run_report(make_record("run-2", lead=lead, workers={}))
        self.assertIsNone(sparse["tokens"]["lead"]["cache_write"])
        self.assertIsNone(sparse["tokens"]["lead"]["cache_read"])
        self.assertFalse(sparse["spend_complete"])
        self.assertEqual(sparse["spend_tokens"], 15)

    def test_cost_coverage_lists_unpriced_participants(self):
        workers = {"codex": {"invocations": 1, "input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}
        report = bm.run_report(make_record("run-1", workers=workers))
        self.assertFalse(report["cost"]["cost_complete"])
        self.assertEqual(report["cost"]["unpriced"], ["codex"])
        self.assertEqual(report["cost"]["estimated_usd"], 0.5)

    def test_orchestration_record_calls_counted_by_action_from_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            sessions = state / "executions" / "k" / "sessions"
            sessions.mkdir(parents=True)
            call = lambda i, args: {"type": "message", "message": {"content": [  # noqa: E731
                {"type": "toolCall", "id": i, "name": "orchestration_record", "arguments": args}]}}
            lines = [{"type": "custom", "customType": bm.SESSION_ENTRY, "data": {"run_id": "run-1"}},
                     call("a", {"action": "status"}), call("b", {"action": "event", "event": {"kind": "phase"}}),
                     call("c", {"action": "event", "event": {"kind": "interaction"}}), call("c", {"action": "event"}),
                     call("d", {"action": "finish", "status": "COMPLETED"}),
                     {"type": "message", "message": {"content": [{"type": "toolCall", "id": "e", "name": "bash", "arguments": {}}]}}]
            (sessions / "20260101_s.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            write_state(state, {"run-1": make_record("run-1")})
            calls = bm.run_report(make_record("run-1"), state=state)["calls"]["orchestration_record"]
        self.assertEqual(calls["total"], 4)  # duplicate id counted once, other tools ignored
        self.assertEqual(calls["by_action"], {"event": 2, "finish": 1, "status": 1})
        self.assertEqual(calls["event_kinds"], {"interaction": 1, "phase": 1})


class SessionBindsAndAttribution(unittest.TestCase):
    """CRT-like session excerpt: two runs in one session file, binds with and without task_id."""

    @staticmethod
    def call(call_id, **args):
        return {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": call_id, "name": "orchestration_record", "arguments": args}]}}

    def session(self, state: Path):
        sessions = state / "executions" / "k" / "sessions"
        sessions.mkdir(parents=True)
        lines = [self.call("pre", action="status"),  # before any run: belongs to none
                 {"type": "custom", "customType": bm.SESSION_ENTRY, "data": {"run_id": "run-old"}},
                 self.call("o1", action="bind_workspace", task_id="t9", step_id="impl"),
                 self.call("o2", action="status"),
                 {"type": "custom", "customType": bm.SESSION_ENTRY, "data": {"run_id": "run-1"}},
                 self.call("b1", action="bind_workspace", task_id="t1", step_id="impl"),
                 self.call("b2", action="bind_workspace", task_id="t2", step_id="impl"),
                 self.call("b3", action="bind_workspace", task_id="t2", step_id="impl"),  # rebind: one fact
                 self.call("w", action="declare_wave", wave_id="w1", tasks=[{"task_id": "t1", "write_scope": ["a"]}]),
                 self.call("f", action="finish", status="COMPLETED")]
        (sessions / "20260101_s.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    def test_only_calls_after_the_runs_start_and_both_binds_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.session(state)
            report = bm.run_report(make_record("run-1"), state=state)
            old = bm.run_report(make_record("run-old"), state=state)
        self.assertEqual(report["calls"]["orchestration_record"]["by_action"],
                         {"bind_workspace": 3, "declare_wave": 1, "finish": 1})
        self.assertEqual(old["calls"]["orchestration_record"]["by_action"], {"bind_workspace": 1, "status": 1})
        binds = [(f["key"], f["evidence"]) for f in report["provenance_facts"] if f["category"] == "bind"]
        self.assertEqual(binds, [(["t1", "impl"], "session"), (["t2", "impl"], "session")])

    def test_bind_evidence_is_not_compared_and_a_missing_bind_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.session(state)
            before = bm.run_report(make_record("run-1"), state=state)["provenance_facts"]
        after = make_record("run-2")
        after["parent_orchestration"]["activity"] = [
            {"tool": "ClaudeWorker", "status": "started", "task_id": "t1", "step_id": "impl"},
            {"tool": "ClaudeWorker", "status": "started", "task_id": "t2", "step_id": "impl"}]
        same = bm.diff_facts(before, bm.provenance_facts(after))
        self.assertEqual([m for m in same if m["category"] == "bind"], [])  # different evidence, same binds
        after["parent_orchestration"]["activity"].pop()
        missing = [m for m in bm.diff_facts(before, bm.provenance_facts(after)) if m["category"] == "bind"]
        self.assertEqual([m["key"] for m in missing], [["t2", "impl"]])

    def test_bind_without_task_id_resolves_to_the_sole_phase(self):
        calls = [{"action": "bind_workspace", "step_id": "impl"}]
        self.assertEqual(bm.session_binds(calls, ["only"]), {"resolved": [("only", "impl")], "unresolved": []})
        self.assertIsNone(bm.session_binds(None, ["a"]))

    def test_unattributable_bind_is_unresolved_and_kept_out_of_the_diff(self):
        calls = [{"action": "bind_workspace", "task_id": "a", "step_id": "impl"},
                 {"action": "bind_workspace", "step_id": "review"}]
        found = bm.session_binds(calls, ["a", "b"])
        self.assertEqual(found, {"resolved": [("a", "impl")], "unresolved": [{"step": "review", "order": 2}]})
        facts = bm.provenance_facts(make_record("r"), session_bind_keys=found["resolved"])
        self.assertEqual([f["key"] for f in facts if f["category"] == "bind"], [["a", "impl"]])  # no "?" key

    def test_compare_flags_bind_source_mismatch_and_lists_unresolved_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.session(base / "b")
            write_state(base / "b", {"run-1": make_record("run-1")}, {"ta": passed("run-1")})
            sessions = base / "b" / "executions" / "k" / "sessions" / "20260101_s.jsonl"
            extra = json.dumps(self.call("u", action="bind_workspace", step_id="review"))
            sessions.write_text(sessions.read_text(encoding="utf-8") + "\n" + extra, encoding="utf-8")
            after = make_record("run-2")
            after["parent_orchestration"]["activity"] = [  # fewer binds, but seen through another source
                {"tool": "ClaudeWorker", "status": "started", "task_id": "t1", "step_id": "impl"}]
            write_state(base / "a", {"run-2": after}, {"ta": passed("run-2")})
            result = bm.compare(bm.workspace_report(base / "b", "ws"), bm.workspace_report(base / "a", "ws"))
            text = bm.render_compare(result)
        prov = result["provenance"]
        self.assertTrue(prov["bind_source_mismatch"])
        self.assertEqual(prov["bind_sources"], [{"task": "ta", "before": "session", "after": "activity"}])
        self.assertEqual([m for m in prov["missing"] if m["category"] == "bind"], [])  # no false "missing"
        self.assertFalse(prov["complete"])
        self.assertEqual(prov["unresolved_binds"]["before"], [{"task": "ta", "step": "review", "order": 4}])
        self.assertIn("bind_source_mismatch", text)
        self.assertIn("Unresolved binds", text)


class Durations(unittest.TestCase):
    def test_sum_and_wall_clock_span_differ_for_overlapping_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_state(state, {
                "run-a": make_record("run-a", started="2026-01-01T00:00:00Z", ended="2026-01-01T00:10:00Z"),
                "run-b": make_record("run-b", started="2026-01-01T00:05:00Z", ended="2026-01-01T00:20:00Z")},
                {"ta": passed("run-a"), "tb": passed("run-b")})
            totals = bm.workspace_report(state, "ws")["totals"]
        self.assertEqual(totals["run_seconds_sum"], 600 + 900)
        self.assertEqual(totals["wall_clock_span_seconds"], 1200)
        self.assertNotIn("wall_clock_seconds", totals)

    def test_span_is_unknown_when_a_run_has_not_ended(self):
        running = make_record("run-a")
        del running["ended_at"]
        self.assertIsNone(bm._span([bm.run_report(running)]))


class Headline(unittest.TestCase):
    def test_headline_is_tokens_per_verified_task_and_na_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_state(state, {"run-a": make_record("run-a"), "run-b": make_record("run-b"), "run-c": make_record("run-c", status="PARTIAL")},
                        {"ta": passed("run-a"), "tb": passed("run-b", ok=False),
                         "tc": {**passed("run-c"), "run_status": "PARTIAL"}})
            report = bm.workspace_report(state, "ws")
        per_run = 1000 + 200 + 3000 + 400 + 100 + 700
        self.assertEqual(report["totals"]["tasks"], {"runs": 3, "completed": 2, "verified": 1, "qualification": []})
        self.assertEqual(report["headline"]["value"], 3 * per_run)  # all spend over the single verified task
        self.assertEqual(report["headline"]["verified_tasks"], 1)

    def test_no_verified_task_gives_na_not_zero_or_infinity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_state(state, {"run-a": make_record("run-a")}, {"ta": passed("run-a", ok=False)})
            failed_checks = bm.workspace_report(state, "ws")
            by_run_id = bm.runs_report(state, ["run-a"])  # no workspace record: checks unknown
        for report in (failed_checks, by_run_id):
            self.assertEqual(report["headline"]["value"], "n/a")
            self.assertEqual(report["headline"]["verified_tasks"], 0)
            self.assertIn("HEADLINE tokens per verified task: n/a", bm.render_report(report))
        self.assertIsNone(by_run_id["runs"][0]["outcome"]["checks_passed"])

    def test_qualification_task_is_reported_but_not_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_state(state, {"run-a": make_record("run-a")},
                        {"ta": passed("run-a"), "q": {"observed": {"validation": {"status": "passed"}, "checks": []}}})
            report = bm.workspace_report(state, "ws")
        self.assertEqual(report["totals"]["tasks"]["runs"], 1)
        self.assertEqual(report["totals"]["tasks"]["qualification"], [{"task": "q", "validation": "passed"}])


class Comparison(unittest.TestCase):
    def sides(self, tmp: Path):
        before, after = tmp / "before", tmp / "after"
        write_state(before, {"run-a": make_record("run-a", calls=49)}, {"ta": passed("run-a")})
        cheaper = {"model": "lead-model", "input_tokens": 600, "output_tokens": 150, "cached_tokens": 20000,
                   "cache_write_tokens": 2000, "total_tokens": 2750, "total_tokens_complete": True, "estimated_cost_usd": 0.3}
        write_state(after, {"run-b": make_record("run-b", calls=24, lead=cheaper)}, {"ta": passed("run-b")})
        return f"{before}@ws", f"{after}@ws"

    def test_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            before_spec, after_spec = self.sides(Path(tmp))
            result = bm.compare(bm.load_side(before_spec, None, []), bm.load_side(after_spec, None, []))
        d = result["deltas"]
        self.assertEqual(d["lead_calls"], {"before": 49, "after": 24, "delta": -25, "percent": -51.02})
        self.assertEqual(d["lead_tokens"]["input"]["delta"], -400)
        self.assertEqual(d["lead_tokens"]["cache_read"]["delta"], -30000)
        self.assertEqual(d["worker_tokens"]["claude-code"]["input"]["delta"], 0)
        self.assertEqual(d["spend_tokens"]["delta"], (600 + 150 + 2000) - (1000 + 200 + 3000))
        self.assertEqual(d["headline_tokens_per_verified_task"]["delta"], d["spend_tokens"]["delta"])  # one verified task each
        self.assertEqual(d["estimated_cost_usd"]["delta"], -0.2)
        self.assertIsNone(d["orchestration_record_calls"]["delta"])  # unobserved on both sides
        self.assertTrue(result["provenance"]["complete"])
        self.assertIn("Provenance completeness: COMPLETE", bm.render_compare(result))

    def test_delta_with_na_headline_is_null(self):
        self.assertEqual(bm._delta("n/a", 5000), {"before": "n/a", "after": 5000, "delta": None, "percent": None})

    def test_report_file_round_trips_as_compare_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            before_spec, after_spec = self.sides(Path(tmp))
            saved = Path(tmp) / "before.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bm.main(["--workspace", "ws", "--state-dir", before_spec.split("@")[0], "--json", "--output", str(saved)])
            self.assertEqual(code, 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bm.main(["--compare", str(saved), after_spec, "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out.getvalue())["provenance"]["complete"])

    def test_missing_state_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bm.main(["--workspace", "nope", "--state-dir", "/nonexistent-pico-state"]), 2)


class ProvenanceDiff(unittest.TestCase):
    def test_identical_records_and_different_order_are_complete(self):
        before = make_record("run-a")
        after = make_record("run-b")
        after["parent_orchestration"]["phases"].reverse()
        for phase in after["parent_orchestration"]["phases"]:
            phase["workers"].reverse()
        self.assertEqual(bm.provenance_diff(before, after), [])

    def test_diff_facts_compares_multiplicities(self):
        fact = {"category": "bind", "key": ["t1", "s"], "value": {}}
        other = {"category": "bind", "key": ["t1", "s"], "value": {"x": 1}}
        self.assertEqual(bm.diff_facts([fact], [fact]), [])
        self.assertEqual(bm.diff_facts([fact, fact], [fact, fact]), [])
        # A duplicated same-key fact is caught from the side that has fewer copies, and a lost copy likewise.
        for before, after in (([fact, fact], [fact]), ([fact, fact], [fact, other])):
            [entry] = bm.diff_facts(before, after)
            self.assertEqual((entry["reason"], entry["expected"], entry["found"]), ("count", 2, 1))
        self.assertEqual(bm.diff_facts([fact], [fact, fact]), [], "extra facts in AFTER are still not reported")
        self.assertEqual([m["reason"] for m in bm.diff_facts([fact], [other])], ["differs"])
        self.assertEqual([m["reason"] for m in bm.diff_facts([fact], [])], ["absent"])

    def test_detects_missing_worker(self):
        before, after = make_record("run-a"), make_record("run-b")
        after["parent_orchestration"]["phases"][1]["workers"] = []
        missing = bm.provenance_diff(before, after)
        self.assertEqual([(m["category"], m["key"], m["reason"]) for m in missing], [("worker", ["t2", "impl", "0"], "absent")])

    def test_detects_missing_phase_wave_integrate_and_finish_and_value_changes(self):
        before = make_record("run-a")
        after = make_record("run-b", status="PARTIAL", waves=[{"id": "w1", "task_ids": ["t1", "t2"], "status": "declared"}])
        del after["parent_orchestration"]["phases"][0]
        after["parent_orchestration"]["phases"][0]["workers"][0]["status"] = "failed"
        found = {(m["category"], tuple(m["key"]), m["reason"]) for m in bm.provenance_diff(before, after)}
        self.assertEqual(found, {
            ("phase", ("t1",), "absent"), ("worker", ("t1", "impl", "0"), "absent"),
            ("worker", ("t2", "impl", "0"), "differs"),
            ("integrate", ("t1,t2", "0", "begin"), "absent"), ("integrate", ("t1,t2", "0", "end"), "absent"),
            ("finish", ("run",), "differs")})

    def test_waves_match_by_task_set_not_id_and_integrate_facts_follow(self):
        before = make_record("run-a", waves=[{"id": "wave-1", "task_ids": ["t1", "t2"], "status": "integrated"}])
        after = make_record("run-b", waves=[{"id": "auto-1f2e3d4c-t1", "task_ids": ["t2", "t1"], "status": "integrated"}])
        for phase in after["parent_orchestration"]["phases"]:
            phase["wave_id"] = "auto-1f2e3d4c-t1"
        self.assertEqual(bm.provenance_diff(before, after), [])
        facts = {(f["category"], tuple(f["key"])): f for f in bm.provenance_facts(after)}
        self.assertEqual(facts[("wave", ("t1,t2", "0"))]["ids"], ["auto-1f2e3d4c-t1"])  # raw id kept as metadata
        # A wave that never reached integration is still missing its begin/end, whatever its id.
        after["parent_orchestration"]["waves"][0]["status"] = "declared"
        self.assertEqual({(m["category"], tuple(m["key"])) for m in bm.provenance_diff(before, after)},
                         {("integrate", ("t1,t2", "0", "begin")), ("integrate", ("t1,t2", "0", "end"))})
        # A different task set is a different wave: its absence is reported.
        other = make_record("run-c", waves=[{"id": "w9", "task_ids": ["t1"], "status": "integrated"}])
        self.assertIn(("wave", ("t1,t2", "0")), {(m["category"], tuple(m["key"])) for m in bm.provenance_diff(before, other)})

    def test_two_waves_for_one_task_set_count_once_at_the_furthest_status(self):
        before = make_record("run-a", waves=[{"id": "w", "task_ids": ["t1"], "status": "integrated"}])
        after = make_record("run-b", waves=[{"id": "auto-a", "task_ids": ["t1"], "status": "integrated"},
                                            {"id": "auto-b", "task_ids": ["t1"], "status": "declared"}])
        self.assertEqual([m for m in bm.provenance_diff(before, after) if m["category"] in {"wave", "integrate"}], [])

    def test_optional_phase_without_route_workers_or_wave_is_dropped_not_missing(self):
        optional = {"id": "inspection", "status": "completed", "route": None, "blocker_code": None, "workers": []}
        before = make_record("run-a")
        before["parent_orchestration"]["phases"].insert(0, optional)
        after = make_record("run-b")
        self.assertEqual([(m["category"], m["key"]) for m in bm.provenance_diff(before, after)], [("phase", ["inspection"])])
        with tempfile.TemporaryDirectory() as tmp:
            b, a = Path(tmp) / "b", Path(tmp) / "a"
            write_state(b, {"run-a": before})
            write_state(a, {"run-b": after})
            result = bm.compare(bm.runs_report(b, ["run-a"]), bm.runs_report(a, ["run-b"]))
        prov = result["provenance"]
        self.assertTrue(prov["complete"])
        self.assertEqual(prov["missing"], [])
        self.assertEqual([m["key"] for m in prov["dropped_optional_phases"]], [["inspection"]])
        self.assertIn("Dropped optional phases", bm.render_compare(result))
        # Not on the allow-list, or not a plain completed phase: it stays missing, never "dropped".
        for phase in ({"id": "routing", "status": "blocked", "route": None, "blocker_code": "WORKER_SCOPE_VIOLATION", "workers": []},
                      {"id": "inspection", "status": "blocked", "route": None, "blocker_code": "X_BLOCKED", "workers": []},
                      {"id": "inspection", "status": "completed", "route": None, "blocker_code": None, "workers": [],
                       "escalations": [{"from_route": "CODEX", "to_route": "CLAUDE_CODEX", "reason": "AMBIGUITY"}]},
                      {"id": "scratch", "status": "completed", "route": None, "blocker_code": None, "workers": []}):
            before["parent_orchestration"]["phases"][0] = copy.deepcopy(phase)
            gone, lost = bm.split_dropped_optional(bm.provenance_facts(before), bm.provenance_diff(before, after))
            self.assertEqual((gone, [m["key"] for m in lost]), ([], [[phase["id"]]]), phase)
            with tempfile.TemporaryDirectory() as tmp:
                b, a = Path(tmp) / "b", Path(tmp) / "a"
                write_state(b, {"run-a": before})
                write_state(a, {"run-b": after})
                prov = bm.compare(bm.runs_report(b, ["run-a"]), bm.runs_report(a, ["run-b"]))["provenance"]
            self.assertFalse(prov["complete"], phase)
            self.assertEqual(prov["dropped_optional_phases"], [])
        # A phase that had a worker, a route or a wave is not optional.
        routed = {"id": "inspection", "status": "completed", "route": "CODEX", "blocker_code": None, "workers": []}
        before["parent_orchestration"]["phases"][0] = routed
        self.assertEqual(bm.split_dropped_optional(bm.provenance_facts(before), bm.provenance_diff(before, after))[0], [])

    def test_extra_facts_in_after_are_not_missing(self):
        before, after = make_record("run-a"), make_record("run-b")
        after["parent_orchestration"]["phases"].append({"id": "t3", "status": "completed", "route": "CLAUDE", "workers": []})
        self.assertEqual(bm.provenance_diff(before, after), [])

    def test_binds_from_worker_activity_and_fixture_partial_run(self):
        before = make_record("run-a", phases=FIXTURE["phases"], waves=[])
        before["parent_orchestration"]["activity"] = [{"tool": "ClaudeWorker", "status": "started", "task_id": "phase-2", "step_id": "claude_worker"}]
        after = copy.deepcopy(before)
        self.assertEqual(bm.provenance_diff(before, after), [])
        after["parent_orchestration"]["activity"] = []
        missing = bm.provenance_diff(before, after)
        self.assertEqual([(m["category"], m["key"]) for m in missing], [("bind", ["phase-2", "claude_worker"])])

    def test_completeness_across_reports_pairs_runs_by_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, a = Path(tmp) / "b", Path(tmp) / "a"
            write_state(b, {"run-a": make_record("run-a")}, {"ta": passed("run-a")})
            lacking = make_record("run-b")
            lacking["parent_orchestration"]["phases"][1]["workers"] = []
            write_state(a, {"run-b": lacking}, {"ta": passed("run-b")})
            result = bm.compare(bm.workspace_report(b, "ws"), bm.workspace_report(a, "ws"))
        self.assertFalse(result["provenance"]["complete"])
        self.assertEqual(result["provenance"]["missing"][0]["task"], "ta")
        with contextlib.redirect_stdout(io.StringIO()):
            with tempfile.TemporaryDirectory() as tmp:
                b, a = Path(tmp) / "b", Path(tmp) / "a"
                write_state(b, {"run-a": make_record("run-a")}, {"ta": passed("run-a")})
                write_state(a, {"run-b": lacking}, {"ta": passed("run-b")})
                self.assertEqual(bm.main(["--compare", f"{b}@ws", f"{a}@ws"]), 1)


def load_setup():
    spec = importlib.util.spec_from_file_location("synthetic_workspace_setup", BENCH / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_check(argv: list[str], cwd: Path) -> int:
    return subprocess.run(argv, cwd=cwd, capture_output=True, check=False).returncode


class SyntheticWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.setup = load_setup()

    def tearDown(self):
        self._tmp.cleanup()

    def manifest(self, name="one", **kwargs):
        return Path(self.setup.create(self.base / name, **kwargs)["manifest"])

    def test_setup_script_creates_a_workspace_that_the_planner_validates(self):
        target = self.base / "ws"
        done = subprocess.run(["sh", str(BENCH / "setup.sh"), str(target)], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        plan = workspace_plan.plan(target / "workspace.yaml", access_check=None)
        self.assertEqual(plan["status"], "PLANNED", plan["blockers"])
        self.assertEqual(plan["waves"], [["lib-slugify"], ["app-post-url", "lib-stopwords"], ["qualify-integration"]])
        self.assertEqual(plan["summary"]["dispatchable"], ["lib-slugify"])
        tasks = {t["id"]: t for t in plan["tasks"]}
        self.assertEqual(tasks["lib-stopwords"]["depends_on"], ["lib-slugify"])
        self.assertEqual(tasks["app-post-url"]["requires"], tasks["lib-slugify"]["provides"])
        self.assertNotEqual(tasks["app-post-url"]["repository"], tasks["lib-slugify"]["repository"])
        self.assertEqual(tasks["qualify-integration"]["kind"], "qualification")
        self.assertEqual({r["repository"] for r in plan["repositories"]},
                         {"pico-bench-sandbox/synth-lib", "pico-bench-sandbox/synth-app"})
        self.assertEqual(plan["budget"], {"max_tokens": 5_000_000})
        for task in plan["tasks"]:
            if task["kind"] == "implementation":
                self.assertTrue(task["objective"] and task["checks"] and task["max_duration_seconds"], task["id"])
        self.assertIn("APPROVED_INPUTS_UNAVAILABLE", tasks["lib-stopwords"]["objective"])
        self.assertEqual(json.loads(json.dumps(plan))["mutations"], {"git": 0, "github": 0, "workers": 0})

    def test_pins_are_reproducible_and_repositories_are_independent(self):
        first = self.setup.create(self.base / "a")
        second = self.setup.create(self.base / "b", owner="other-owner")
        self.assertEqual(first["pinned_commits"], second["pinned_commits"])
        self.assertEqual(len({str(Path(p).resolve()) for p in first["repositories"].values()}), 2)

    def test_refuses_to_write_inside_the_pico_repository_or_a_used_directory(self):
        with self.assertRaises(self.setup.SetupError):
            self.setup.create(REPO_ROOT / "benchmarks" / "synthetic-workspace" / "out")
        used = self.base / "used"
        used.mkdir()
        (used / "file").write_text("x")
        with self.assertRaises(self.setup.SetupError):
            self.setup.create(used)

    def test_baseline_checks_fail_and_reference_solutions_make_them_pass(self):
        manifest = self.manifest()
        data = yaml.safe_load(manifest.read_text())
        tasks = {t["id"]: t for t in data["tasks"]}
        lib, app = manifest.parent / "synth-lib", manifest.parent / "synth-app"
        for task in ("lib-slugify", "lib-stopwords", "app-post-url"):
            check = tasks[task]["checks"][0]
            cwd = {"synth-lib": lib, "synth-app": app}[check["repository"]]
            self.assertNotEqual(run_check(check["argv"], cwd), 0, f"{task} must fail before the work is done")
        for src in (BENCH / "reference").rglob("*.py"):
            repo, rel = src.relative_to(BENCH / "reference").parts[0], Path(*src.relative_to(BENCH / "reference").parts[1:])
            (manifest.parent / repo / rel).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        for task in ("lib-slugify", "app-post-url"):
            check = tasks[task]["checks"][0]
            cwd = {"synth-lib": lib, "synth-app": app}[check["repository"]]
            self.assertEqual(run_check(check["argv"], cwd), 0, f"{task} must pass with the reference solution")
        qualify = tasks["qualify-integration"]["checks"]
        self.assertEqual([c["repository"] for c in qualify], ["synth-app", "synth-lib"])
        for check in qualify:
            self.assertEqual(run_check(check["argv"], {"synth-lib": lib, "synth-app": app}[check["repository"]]), 0)
        # The PARTIAL task cannot be satisfied without the missing input.
        self.assertNotEqual(run_check(tasks["lib-stopwords"]["checks"][0]["argv"], lib), 0)
        self.assertFalse((lib / "data" / "approved-stopwords.txt").exists())

    def test_publication_flag_and_issue_numbers(self):
        off = self.manifest("off")
        self.assertFalse((off.parent / "synth-lib" / ".pi").exists())
        on = self.manifest("on", publication=True, issues={"lib-slugify": 7})
        policy = yaml.safe_load((on.parent / "synth-lib" / ".pi" / "autonomy.yaml").read_text())
        jsonschema.validate(policy, json.loads((REPO_ROOT / "schemas" / "autonomy.schema.json").read_text()))
        self.assertTrue(policy["permissions"]["push_branch"] and policy["permissions"]["create_pr"])
        self.assertFalse(policy["permissions"]["merge"]["enabled"])
        self.assertEqual({t["id"]: t.get("issue") for t in yaml.safe_load(on.read_text())["tasks"]}["lib-slugify"], 7)

    def test_max_tokens_bounds_the_budget(self):
        self.assertEqual(yaml.safe_load(self.manifest("default").read_text())["budget"], {"max_tokens": 5_000_000})
        bounded = self.manifest("bounded", max_tokens=120_000)
        self.assertEqual(yaml.safe_load(bounded.read_text())["budget"], {"max_tokens": 120_000})
        for bad in (0, -1, True):
            with self.assertRaises(self.setup.SetupError):
                self.setup.create(self.base / f"bad-{bad}", max_tokens=bad)
        with self.assertRaises(self.setup.SetupError):
            self.setup.create(self.base / "bad", issues={"nope": 1})


class SetupGuards(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.setup = load_setup()

    def tearDown(self):
        self._tmp.cleanup()

    def test_refuses_any_target_inside_a_work_tree_or_its_control_directory(self):
        outer = self.base / "outer"
        outer.mkdir()
        subprocess.run(["git", "init", "-q", str(outer)], check=True)
        for target in (outer / "bench", outer / "deep" / "er" / "bench", outer / ".git" / "bench"):
            with self.assertRaises(self.setup.SetupError, msg=str(target)):
                self.setup.create(target)
            self.assertFalse(target.exists())

    def fake_git(self, body: str) -> dict:
        bin_dir = self.base / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "git"
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def with_env(self, env: dict, action):
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return action()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_git_errors_other_than_not_a_repository_fail_closed(self):
        ownership = self.fake_git('echo "fatal: detected dubious ownership in repository at /x" >&2; exit 128')
        with self.assertRaises(self.setup.SetupError) as raised:
            self.with_env(ownership, lambda: self.setup.create(self.base / "owned"))
        self.assertIn("dubious ownership", str(raised.exception))
        self.assertFalse((self.base / "owned").exists())
        broken = self.fake_git('echo "fatal: something else" >&2; exit 1')
        with self.assertRaises(self.setup.SetupError):
            self.with_env(broken, lambda: self.setup.create(self.base / "broken"))
        empty = self.base / "emptybin"
        empty.mkdir()
        with self.assertRaises(self.setup.SetupError):
            self.with_env({"PATH": str(empty)}, lambda: self.setup.inside_git(self.base / "nogit"))

    def test_only_git_not_a_repository_answer_means_outside(self):
        self.assertFalse(self.setup.inside_git(self.base / "not" / "yet" / "there"))
        said = self.fake_git('echo "fatal: not a git repository (or any of the parent directories): .git" >&2; exit 128')
        self.assertFalse(self.with_env(said, lambda: self.setup.inside_git(self.base)))

    def test_refuses_this_checkouts_common_dir(self):
        common = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        with self.assertRaises(self.setup.SetupError):
            self.setup.create(Path(common) / "bench")

    def test_publication_for_another_owner_needs_confirmation(self):
        with self.assertRaises(self.setup.SetupError):
            self.setup.create(self.base / "a", owner="someone", publication=True)
        self.assertFalse((self.base / "a").exists())
        self.setup.create(self.base / "b", owner="someone", publication=True, confirm_owned=True)
        self.setup.create(self.base / "c", publication=True)  # default sandbox owner needs no flag
        script = str(BENCH / "setup.py")
        refused = subprocess.run([sys.executable, script, str(self.base / "d"), "--owner", "someone", "--publication"],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(refused.returncode, 1)
        accepted = subprocess.run([sys.executable, script, str(self.base / "e"), "--owner", "someone", "--publication",
                                   "--i-own-these-repos"], capture_output=True, text=True, check=False)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_pins_ignore_the_users_git_configuration(self):
        baseline = self.setup.create(self.base / "plain")["pinned_commits"]
        hostile = self.base / "gitconfig"
        hostile.write_text("[user]\n\tname = Someone Else\n[commit]\n\tgpgsign = true\n[core]\n\tautocrlf = true\n"
                           f"\thooksPath = {self.base}/nohooks\n[init]\n\tdefaultBranch = trunk\n", encoding="utf-8")
        old = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "HOME")}
        os.environ.update({"GIT_CONFIG_GLOBAL": str(hostile), "HOME": str(self.base)})
        try:
            hostile_pins = self.setup.create(self.base / "hostile")["pinned_commits"]
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(hostile_pins, baseline)


class SyntheticWorkspaceFakePi(unittest.TestCase):
    """Plumbing only: the first wave launched with the fake `pi` of the test suite, then measured."""

    def setUp(self):
        from test_headless_run import FAKE_PI
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.state = self.base / "state"
        self.pi = self.base / "pi"
        self.pi.write_text(FAKE_PI)
        self.pi.chmod(0o755)
        self._env = dict(os.environ)
        os.environ.update({provenance.STATE_ENV: str(self.state), "FAKE_PI_TOOLS": str(REPO_ROOT / "tools"),
                           "PI_CODING_AGENT_DIR": str(self.base / "agent"), "FAKE_PI_BEHAVIOR": "finish",
                           "FAKE_PI_USAGE": "1200,300,0.05"})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_launch_first_wave_and_collect_metrics_without_a_model(self):
        manifest = Path(load_setup().create(self.base / "ws")["manifest"])
        launched = wr.launch(manifest=manifest, task_id="lib-slugify", access_check=None, pi_executable=str(self.pi))
        self.assertEqual(launched["task"]["run_status"], "PARTIAL")  # the fake lead finishes PARTIAL
        blocked = None
        try:
            wr.launch(manifest=manifest, task_id="app-post-url", access_check=None, pi_executable=str(self.pi))
        except wr.WorkspaceRunError as exc:
            blocked = exc.code
        self.assertEqual(blocked, "WORKSPACE_TASK_BLOCKED")  # waits for lib-slugify to be ready_for_downstream
        report = bm.workspace_report(self.state, "pico-bench")
        run = report["runs"][0]
        self.assertEqual(run["task"], "lib-slugify")
        self.assertEqual(run["tokens"]["workers"]["claude-code"]["input"], 1200)
        self.assertEqual(run["calls"]["workers"], {"claude-code": 1})
        self.assertEqual(run["outcome"]["final_status"], "PARTIAL")
        self.assertEqual(report["headline"]["value"], "n/a")  # nothing completed and verified


if __name__ == "__main__":
    unittest.main()
