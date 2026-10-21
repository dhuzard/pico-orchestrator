#!/usr/bin/env python3
"""Deterministic tests for token spend, cache accounting and partial-cost reporting."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import usage  # noqa: E402
import workspace_scheduler as ws  # noqa: E402


def parent(lead=None, workers=None):
    return {"context": {"lead_usage": lead or {}, "worker_usage": workers or {}}}


class UsageTest(unittest.TestCase):
    def test_cache_writes_are_billed_and_cache_reads_are_not(self):
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
                                         {"claude-code": {"invocations": 1, "duration_ms": 5}}))
        self.assertEqual(tokens["unpriced"], ["openai-codex/gpt"])
        self.assertIsNone(tokens["estimated_cost_usd"])

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
        self.assertIn("cost excludes: openai-codex/gpt-6.1, pi-subagent", text)
        self.assertIn("launch gate", text)
        self.assertIn("can still overshoot", text)

    def test_render_complete_cost_has_no_exclusion_note(self):
        budget = {"max_tokens": 10, "spent_tokens": 1, "max_cost_usd": None, "spent_cost_usd": 0.0, "runs": 0,
                  "exhausted": False, "reasons": [], "cost_complete": True, "unpriced": []}
        text = ws.render({"workspace_id": "w", "status": "OK", "cap": 1, "tasks": [], "counts": {}, "budget": budget})
        self.assertNotIn("cost excludes", text)
        self.assertIn("launch gate", text)


if __name__ == "__main__":
    unittest.main()
