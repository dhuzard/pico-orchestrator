"""Offline fault/concurrency tests; these never contact a provider."""
import multiprocessing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import budget_ledger as ledger


def compete(args):
    path, number = args
    try:
        return ledger.reserve_launch(path, task_id=f"t{number}", attempt_id="a")
    except ledger.BudgetError as exc:
        return exc.code


def rebind_or_admit(args):
    path, operation = args
    try:
        if operation == "rebind":
            ledger.rebind_empty_provider_contract(path, expected="a" * 64, replacement="b" * 64,
                                                  reason="Offline concurrency test", attempt_cost_usd="15")
        elif operation == "launch":
            ledger.reserve_launch(path, task_id="t", attempt_id="a", provider_bound=True, provider_contract="a" * 64)
        else:
            ledger.reserve(path, operation_id="request", task_id="t", attempt_id="a", maximum={"tokens": 10, "cost_usd": "1"},
                           supported={"tokens", "cost_usd"}, provider_contract="a" * 64)
        return operation
    except ledger.BudgetError as exc:
        return exc.code


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.json"

    def reserve(self, operation="r1", task="t", attempt="a", maximum=None):
        maximum = maximum or {"requests": 1, "tokens": 100, "cost_usd": "0.2"}
        return ledger.reserve(self.path, operation_id=operation, task_id=task, attempt_id=attempt,
                              maximum=maximum, supported=set(maximum))

    def test_concurrent_processes_cannot_overbook_last_slots(self):
        ledger.initialize(self.path, {"lifetime": {"launches": 4}})
        with multiprocessing.get_context("fork").Pool(8) as pool:
            results = pool.map(compete, [(self.path, i) for i in range(20)])
        self.assertEqual(results.count("AGGREGATE_BUDGET_EXHAUSTED"), 16)
        self.assertEqual(len(ledger.inspect(self.path)["reservations"]), 4)

    def test_empty_rebind_preserves_global_limits_and_records_explicit_attempt_adjustment(self):
        limits = {"lifetime": {"tokens": 8000000, "cost_usd": "30", "requests": 200, "launches": 6},
                  "attempt": {"tokens": 4000000, "cost_usd": "10", "requests": 100, "launches": 3}}
        ledger.initialize(self.path, limits)
        ledger.bind_provider_contract(self.path, "a" * 64)
        result = ledger.rebind_empty_provider_contract(self.path, expected="a" * 64, replacement="b" * 64,
            reason="Unavailable initial model; authorized provider switch", attempt_cost_usd="15")
        expected_limits = json.loads(json.dumps(limits))
        expected_limits["attempt"]["cost_usd"] = "15"
        self.assertEqual(result["limits"], expected_limits)
        self.assertEqual(result["reservations"], {})
        event = result["provider_contract_history"][0]
        self.assertEqual((event["previous"], event["replacement"]), ("a" * 64, "b" * 64))
        self.assertEqual(event["attempt_cost_usd"], {"previous": "10", "replacement": "15"})
        self.assertEqual(ledger.inspect(self.path), result)
        with self.assertRaisesRegex(ledger.BudgetError, "CONFLICT"):
            ledger.rebind_empty_provider_contract(self.path, expected="a" * 64, replacement="c" * 64, reason="stale")
        self.assertEqual(ledger.inspect(self.path), result)

    def test_rebind_refuses_every_reservation_state_even_settled_zero_usage(self):
        for state in ("reserved", "uncertain", "settled"):
            path = self.path.with_name(state + ".json")
            ledger.initialize(path, {"lifetime": {"tokens": 8000000, "cost_usd": "30"},
                                     "attempt": {"cost_usd": "10"}})
            ledger.bind_provider_contract(path, "a" * 64)
            ledger.reserve(path, operation_id="zero", task_id="t", attempt_id="a", maximum={"tokens": 0, "cost_usd": "0"},
                           supported={"tokens", "cost_usd"})
            if state != "reserved":
                ledger.settle(path, "zero", actual={"tokens": 0, "cost_usd": "0"}, complete=state == "settled")
            before = path.read_bytes()
            with self.assertRaisesRegex(ledger.BudgetError, "zero reservations"):
                ledger.rebind_empty_provider_contract(path, expected="a" * 64, replacement="b" * 64,
                                                      reason="must refuse", attempt_cost_usd="15")
            self.assertEqual(path.read_bytes(), before)

    def test_rebind_cannot_raise_global_cap_or_reuse_stale_gateway_contract(self):
        ledger.initialize(self.path, {"lifetime": {"tokens": 8000000, "cost_usd": "30"}, "attempt": {"cost_usd": "10"}})
        ledger.bind_provider_contract(self.path, "a" * 64)
        with self.assertRaisesRegex(ledger.BudgetError, "unchanged lifetime"):
            ledger.rebind_empty_provider_contract(self.path, expected="a" * 64, replacement="b" * 64,
                                                  reason="too high", attempt_cost_usd="31")
        ledger.rebind_empty_provider_contract(self.path, expected="a" * 64, replacement="b" * 64, reason="change")
        with self.assertRaisesRegex(ledger.BudgetError, "CONFLICT"):
            ledger.reserve(self.path, operation_id="old", task_id="t", attempt_id="a", maximum={"tokens": 10, "cost_usd": "1"},
                           supported={"tokens", "cost_usd"}, provider_contract="a" * 64)
        self.assertFalse(ledger.inspect(self.path)["reservations"])

    def test_concurrent_rebind_and_admit_are_serialized(self):
        for operation in ("request", "launch"):
            path = self.path.with_name(operation + ".json")
            ledger.initialize(path, {"lifetime": {"tokens": 8000000, "cost_usd": "30"}, "attempt": {"cost_usd": "10"}})
            ledger.bind_provider_contract(path, "a" * 64)
            with multiprocessing.get_context("fork").Pool(2) as pool:
                results = pool.map(rebind_or_admit, [(path, "rebind"), (path, operation)])
            self.assertEqual(results.count("AGGREGATE_BUDGET_CONFLICT"), 1)
            data = ledger.inspect(path)
            if "rebind" in results:
                self.assertEqual(data["reservations"], {})
                self.assertEqual(data["provider_contract"], "b" * 64)
            else:
                self.assertEqual(len(data["reservations"]), 1)
                self.assertEqual(data["provider_contract"], "a" * 64)
                self.assertNotIn("provider_contract_history", data)

    def test_crash_after_reservation_and_next_day_retains_full_allowance(self):
        ledger.initialize(self.path, {"day": {"requests": 1}})
        code = ("import os,sys;sys.path.insert(0,sys.argv[1]);import budget_ledger as l;"
                "l.reserve(sys.argv[2],operation_id='crashed',task_id='t',attempt_id='a',"
                "maximum={'requests':1},supported={'requests'});os._exit(9)")
        result = subprocess.run([sys.executable, "-c", code, str(Path(ledger.__file__).parent), str(self.path)])
        self.assertEqual(result.returncode, 9)
        with mock.patch.object(ledger, "_day", return_value="2099-01-01"):
            with self.assertRaisesRegex(ledger.BudgetError, "EXHAUSTED"):
                self.reserve("r2", maximum={"requests": 1})
        self.assertEqual(ledger.inspect(self.path)["reservations"]["crashed"]["state"], "reserved")

    def test_settlement_missing_price_retains_then_complete_releases(self):
        ledger.initialize(self.path, {"lifetime": {"requests": 4, "tokens": 200, "cost_usd": "0.3"}})
        self.reserve()
        ledger.settle(self.path, "r1", actual={"requests": 1, "tokens": 5}, complete=False)
        with self.assertRaisesRegex(ledger.BudgetError, "EXHAUSTED"):
            self.reserve("r2")
        ledger.settle(self.path, "r1", actual={"requests": 1, "tokens": 5, "cost_usd": "0.1"}, complete=True)
        self.assertTrue(self.reserve("r2")[1])
        self.assertFalse(self.reserve("r2")[1])  # reconciliation authorizes no second request
        with self.assertRaisesRegex(ledger.BudgetError, "CONFLICT"):
            ledger.settle(self.path, "r1", actual={"requests": 0, "tokens": 0, "cost_usd": 0}, complete=True)

    def test_attempt_retry_resume_cannot_reset_task_or_lifetime(self):
        ledger.initialize(self.path, {"attempt": {"launches": 2}, "task": {"launches": 3},
                                      "lifetime": {"launches": 4}})
        first = ledger.reserve_launch(self.path, task_id="t", attempt_id="a1")
        ledger.settle(self.path, first, actual={"launches": 1}, complete=True)
        ledger.reserve_launch(self.path, task_id="t", attempt_id="a1")  # resume
        with self.assertRaisesRegex(ledger.BudgetError, "attempt launches"):
            ledger.reserve_launch(self.path, task_id="t", attempt_id="a1")
        ledger.reserve_launch(self.path, task_id="t", attempt_id="a2")  # retry
        with self.assertRaisesRegex(ledger.BudgetError, "task launches"):
            ledger.reserve_launch(self.path, task_id="t", attempt_id="a3")
        ledger.reserve_launch(self.path, task_id="other", attempt_id="a1")
        with self.assertRaisesRegex(ledger.BudgetError, "lifetime launches"):
            ledger.reserve_launch(self.path, task_id="another", attempt_id="a1")

    def test_launch_adapter_refuses_fictitious_provider_caps(self):
        for dimension in ("requests", "tokens", "cost_usd"):
            with self.subTest(dimension=dimension):
                path = self.path.with_name(dimension + ".json")
                ledger.initialize(path, {"lifetime": {dimension: 1}})
                with self.assertRaisesRegex(ledger.BudgetError, "UNSUPPORTED"):
                    ledger.reserve_launch(path, task_id="t", attempt_id="a")
                self.assertFalse(ledger.inspect(path)["reservations"])

    def test_corrupt_missing_or_reset_ledger_fails_closed(self):
        with self.assertRaisesRegex(ledger.BudgetError, "UNREADABLE"):
            ledger.reserve_launch(self.path, task_id="t", attempt_id="a")
        ledger.initialize(self.path, {"lifetime": {"launches": 1}})
        with self.assertRaisesRegex(ledger.BudgetError, "refusing to reset"):
            ledger.initialize(self.path, {"lifetime": {"launches": 100}})
        self.path.write_text('{"version":1,"limits":{},"reservations":{}}')
        with self.assertRaisesRegex(ledger.BudgetError, "UNREADABLE"):
            ledger.reserve_launch(self.path, task_id="t", attempt_id="a")

    def test_unknown_nonfinite_negative_or_incomplete_maximum_refused(self):
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ledger.BudgetError):
                ledger.initialize(self.path, {"lifetime": {"cost_usd": value}})
        ledger.initialize(self.path, {"lifetime": {"cost_usd": 1}})
        with self.assertRaisesRegex(ledger.BudgetError, "UNSUPPORTED"):
            self.reserve(maximum={"requests": 1})

    def test_overrun_is_recorded_and_blocks_following_admission(self):
        ledger.initialize(self.path, {"lifetime": {"tokens": 100}})
        self.reserve(maximum={"tokens": 80})
        result = ledger.settle(self.path, "r1", actual={"tokens": 120}, complete=True)
        self.assertTrue(result["overrun"])
        with self.assertRaisesRegex(ledger.BudgetError, "EXHAUSTED"):
            self.reserve("r2", maximum={"tokens": 1})

    def test_launch_operation_replay_cannot_authorize_twice(self):
        ledger.initialize(self.path, {"lifetime": {"launches": 3}})
        ledger.reserve_launch(self.path, task_id="t", attempt_id="a", operation_id="stable")
        with self.assertRaisesRegex(ledger.BudgetError, "REPLAY"):
            ledger.reserve_launch(self.path, task_id="t", attempt_id="a", operation_id="stable")
