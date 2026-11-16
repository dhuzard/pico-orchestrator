#!/usr/bin/env python3
"""Deterministic tests for token spend, cache accounting and partial-cost reporting."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import usage  # noqa: E402
import workspace_scheduler as ws  # noqa: E402


def parent(lead=None, workers=None):
    return {"context": {"lead_usage": lead or {}, "worker_usage": workers or {}}}


class UsageTest(unittest.TestCase):
    def test_token_work_counts_cache_writes_and_excludes_cache_reads(self):
        claude = {"invocations": 1, "input_tokens": 22, "output_tokens": 1000, "cache_write_tokens": 30000,
                  "cached_tokens": 900000, "estimated_cost_usd": 1.5}
        tokens = usage.run_tokens(parent(workers={"claude-code": claude}))
        self.assertEqual(tokens["cache_write_tokens"], 30000)
        self.assertEqual(usage.spent(tokens), 22 + 1000 + 30000)
        self.assertEqual(tokens["cached_tokens"], 900000)

    def test_unpriced_participant_makes_cost_incomplete(self):
        priced = {"invocations": 1, "input_tokens": 5, "output_tokens": 5, "estimated_cost_usd": 0.2}
        unpriced = {"invocations": 1, "input_tokens": 7, "output_tokens": 1}
        lead = {"model": "m/lead", "input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0.1}
        partial = usage.run_tokens(parent(lead, {"claude-code": priced, "pi-subagent": unpriced}))
        self.assertFalse(partial["cost_complete"])
        self.assertEqual(partial["unpriced"], ["pi-subagent"])
        self.assertEqual(partial["estimated_cost_usd"], 0.3)
        whole = usage.run_tokens(parent(lead, {"claude-code": priced}))
        self.assertTrue(whole["cost_complete"])
        self.assertEqual(whole["unpriced"], [])
        total = usage.sum_tokens([partial, whole])
        self.assertFalse(total["cost_complete"])
        self.assertEqual(total["unpriced"], ["pi-subagent"])

    def test_lead_without_cost_is_named_unpriced_and_tokenless_participants_are_ignored(self):
        tokens = usage.run_tokens(parent({"model": "openai-codex/gpt", "input_tokens": 9, "output_tokens": 1},
                                         {"claude-code": {"invocations": 0}}))
        self.assertEqual(tokens["unpriced"], ["openai-codex/gpt"])
        self.assertIsNone(tokens["estimated_cost_usd"])

    def test_calls_with_no_counts_zero_cost_and_incomplete_flags_are_unpriced(self):
        ask = {"invocations": 3, "duration_ms": 900}  # AskClaude records no tokens and no cost
        self.assertEqual(usage.run_tokens(parent(workers={"claude-code": ask}))["unpriced"], ["claude-code"])
        zero = {"invocations": 1, "input_tokens": 50, "output_tokens": 5, "estimated_cost_usd": 0}
        self.assertEqual(usage.run_tokens(parent(workers={"claude-code": zero}))["unpriced"], ["claude-code"])
        flagged = {"invocations": 2, "input_tokens": 5, "output_tokens": 5, "estimated_cost_usd": 0.4,
                   "cost_complete": False}
        self.assertEqual(usage.run_tokens(parent(workers={"claude-code": flagged}))["unpriced"], ["claude-code"])
        self.assertTrue(usage.run_tokens(parent(workers={"claude-code": {**flagged, "cost_complete": True}}))["cost_complete"])

    def test_old_blocks_and_records_without_new_fields_still_sum(self):
        old = {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2, "total_tokens": None,
               "estimated_cost_usd": 0.5}
        total = usage.sum_tokens([old])
        self.assertEqual((total["input_tokens"], total["cache_write_tokens"], total["estimated_cost_usd"]), (10, None, 0.5))
        self.assertTrue(total["cost_complete"])
        self.assertEqual(usage.spent(total), 15)

    def test_workspace_usage_counts_extra_in_flight_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            body = {"parent_orchestration": parent(workers={"claude-code": {
                "invocations": 1, "input_tokens": 100, "output_tokens": 50, "cache_write_tokens": 25}})}
            (runs / "run-a.json").write_text(json.dumps(body))
            record = {"tasks": {"t1": {"run_id": "run-a"}}}
            total = usage.workspace_usage(runs, record, extra_run_ids=["run-a", "run-a"])
            self.assertEqual((total["runs"], total["spent_tokens"]), (1, 175))
            self.assertEqual(usage.workspace_usage(runs, {"tasks": {}}, extra_run_ids=["run-a"])["spent_tokens"], 175)


class RenderTest(unittest.TestCase):
    def test_render_names_unpriced_participants_and_the_launch_gate_caveat(self):
        budget = {"max_tokens": 120000, "spent_tokens": 538446, "max_cost_usd": None, "spent_cost_usd": 3.635674,
                  "runs": 2, "exhausted": True, "reasons": ["tokens"], "cost_complete": False,
                  "unpriced": ["openai-codex/gpt-6.1", "pi-subagent"]}
        result = {"workspace_id": "w", "status": "ATTENTION", "cap": 2, "tasks": [], "counts": {}, "budget": budget}
        text = ws.render(result)
        self.assertIn("538,446 of 120,000 tokens", text)
        self.assertIn("$3.64 reported cost", text)
        self.assertIn("lower bound; cost excludes: openai-codex/gpt-6.1, pi-subagent", text)
        self.assertIn("launch gate", text)
        self.assertIn("can still overshoot", text)

    def test_render_complete_cost_has_no_exclusion_note(self):
        budget = {"max_tokens": 10, "spent_tokens": 1, "max_cost_usd": None, "spent_cost_usd": 0.0, "runs": 0,
                  "exhausted": False, "reasons": [], "cost_complete": True, "unpriced": []}
        text = ws.render({"workspace_id": "w", "status": "OK", "cap": 1, "tasks": [], "counts": {}, "budget": budget})
        self.assertNotIn("cost excludes", text)
        self.assertIn("launch gate", text)


class InflightTest(unittest.TestCase):
    def plan(self, base: Path) -> dict:
        return {"workspace_id": "w", "budget": {"max_tokens": 100},
                "repositories": [{"id": "r", "git_common_dir": str(base / "git")}],
                "tasks": [{"id": "t1", "repository": "r"}]}

    def test_missing_or_unreadable_execution_record_makes_the_budget_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plan = self.plan(base)
            ids, problems = ws.inflight_usage(plan, {"t1"})
            self.assertEqual(ids, [])
            self.assertEqual(len(problems), 1)
            state = ws.budget_state(plan, {"tasks": {}}, base, running={"t1"})
            self.assertTrue(state["incomplete"])
            self.assertIn("t1", state["incomplete_reasons"][0])
            # Corrupt record: the loader's error is caught, not raised.
            path = ws.workspace_run.headless_run.execution_path(base / "git", "w--t1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json")
            self.assertEqual(len(ws.inflight_usage(plan, {"t1"})[1]), 1)

    def test_found_run_id_is_cached_and_not_rediscovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plan = self.plan(base)
            path = ws.workspace_run.headless_run.execution_path(base / "git", "w--t1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"record_version": ws.workspace_run.headless_run.RECORD_VERSION,
                                        "session_dir": str(base), "session_id": "s", "run_id": None}))
            cache: dict = {}
            with mock.patch.object(ws.workspace_run.headless_run, "discover_run_id", return_value="run-x") as found:
                self.assertEqual(ws.inflight_usage(plan, {"t1"}, cache), (["run-x"], []))
                self.assertEqual(ws.inflight_usage(plan, {"t1"}, cache), (["run-x"], []))
            self.assertEqual(found.call_count, 1)


class FailClosedAccountingTest(unittest.TestCase):
    def test_linked_missing_corrupt_and_malformed_usage_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            for content in (None, "{bad", "[]", "{}", '{"parent_orchestration":{}}'):
                with self.subTest(content=content):
                    path = runs / "run-a.json"
                    if content is not None:
                        path.write_text(content)
                    result = usage.workspace_usage(runs, {"tasks": {"t": {"run_id": "run-a"}}})
                    self.assertTrue(result["incomplete"])
                    self.assertFalse(result["cost_complete"])
                    self.assertIn("run-a", result["incomplete_reasons"][0])

    def test_prior_attempt_missing_is_not_erased_by_current_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "run-current.json").write_text(json.dumps({"parent_orchestration": parent(
                {"input_tokens": 1, "estimated_cost_usd": 0.1})}))
            record = {"tasks": {"t": {"run_id": "run-current", "attempts": [{"run_id": "run-old"}]}}}
            result = usage.workspace_usage(runs, record)
            self.assertEqual(result["spent_tokens"], 1)
            self.assertTrue(result["incomplete"])
            self.assertFalse(result["cost_complete"])

    def test_unpriced_usage_blocks_monetary_gate_but_not_token_work_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / "run-a.json").write_text(json.dumps({"parent_orchestration": parent(
                {"model": "unknown", "input_tokens": 5, "output_tokens": 0, "cost_complete": False})}))
            record = {"tasks": {"t": {"run_id": "run-a"}}}
            plan = {"workspace_id": "w", "budget": {"max_tokens": 100}}
            self.assertFalse(ws.budget_state(plan, record, runs)["incomplete"])
            plan["budget"]["max_cost_usd"] = 10
            state = ws.budget_state(plan, record, runs)
            self.assertTrue(state["incomplete"])
            self.assertFalse(state["cost_complete"])
            self.assertIn("missing prices", state["incomplete_reasons"][0])

    def test_explicit_incomplete_flag_survives_aggregation_without_names(self):
        self.assertFalse(usage.sum_tokens([{"cost_complete": False, "estimated_cost_usd": 1}])["cost_complete"])

    def test_cache_read_only_cost_counts_money_not_token_work(self):
        result = usage.run_tokens(parent({"cached_tokens": 10000, "estimated_cost_usd": 0.25}))
        self.assertEqual(usage.spent(result), 0)
        self.assertEqual(result["estimated_cost_usd"], 0.25)
        self.assertTrue(result["cost_complete"])
        self.assertFalse(result['token_work_complete'])
        for value in (float("inf"), float("nan")):
            self.assertFalse(usage.run_tokens(parent({"input_tokens": 1, "estimated_cost_usd": value}))["cost_complete"])

    def test_missing_worker_counters_hold_token_gate_even_when_cost_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            context = parent({'input_tokens': 0, 'output_tokens': 0, 'estimated_cost_usd': 0})
            context['context']['worker_usage'] = {'worker': {'invocations': 1, 'cached_tokens': 100, 'estimated_cost_usd': 1}}
            (runs / 'run-a.json').write_text(json.dumps({'parent_orchestration': context}))
            record = {'tasks': {'t': {'run_id': 'run-a'}}}
            token_gate = ws.budget_state({'workspace_id': 'w', 'budget': {'max_tokens': 100}}, record, runs)
            cost_gate = ws.budget_state({'workspace_id': 'w', 'budget': {'max_cost_usd': 10}}, record, runs)
            self.assertTrue(token_gate['incomplete'])
            self.assertFalse(cost_gate['incomplete'])


if __name__ == "__main__":
    unittest.main()
