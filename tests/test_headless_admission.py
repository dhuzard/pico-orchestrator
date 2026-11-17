"""Real fake-provider launches exercise central admission, not mocked start()."""
import os
import json
from unittest import mock

from test_headless_run import HeadlessFixture
import budget_ledger
import headless_run as hr
import provider_budget
from test_provider_budget import config


class HeadlessAdmissionTest(HeadlessFixture):
    def setUp(self):
        super().setUp()
        os.environ.pop(budget_ledger.ENV, None)

    def test_direct_launch_counts_once_and_idempotent_start_does_not_charge(self):
        path = self.state / 'ledger.json'
        budget_ledger.initialize(path, {'lifetime': {'launches': 1}})
        os.environ[budget_ledger.ENV] = str(path)
        first = self.start(wait=True)
        self.assertEqual(first['status'], 'exited')
        self.assertIn('aggregate_admission_id', first)
        again = self.start(wait=True)
        self.assertEqual(again['outcome'], 'already_finished')
        self.assertEqual(len(budget_ledger.inspect(path)['reservations']), 1)
        with self.assertRaises(hr.HeadlessError) as error:
            self.start(execution_id='exec-2')
        self.assertEqual(error.exception.code, 'AGGREGATE_BUDGET_EXHAUSTED')

    def test_provider_ceiling_refused_before_any_model_execution(self):
        path = self.state / 'ledger.json'
        budget_ledger.initialize(path, {'lifetime': {'cost_usd': '1.00'}})
        with self.assertRaises(hr.HeadlessError) as error:
            self.start(aggregate_ledger=path)
        self.assertEqual(error.exception.code, 'AGGREGATE_BUDGET_UNSUPPORTED')
        self.assertEqual(budget_ledger.inspect(path)['reservations'], {})
        self.assertEqual(list((self.state / 'runs').glob('*.json')), [])

    def test_provider_mode_missing_api_key_refuses_before_child_or_reservation(self):
        path = self.state / 'provider-ledger.json'
        config_path = self.state / 'provider-config.json'
        budget_ledger.initialize(path, {'lifetime': {'tokens': 2000000, 'cost_usd': '1'}})
        config_path.write_text(json.dumps(config()))
        with mock.patch.dict(os.environ, {provider_budget.CONFIG_ENV: str(config_path)}), mock.patch.object(
                provider_budget, 'catalog_today', return_value=provider_budget.date(2026, 11, 17)):
            os.environ.pop('OPENAI_API_KEY', None)
            with self.assertRaises(hr.HeadlessError) as error:
                self.start(aggregate_ledger=path)
        self.assertEqual(error.exception.code, 'PROVIDER_BUDGET_CREDENTIAL_UNAVAILABLE')
        self.assertFalse(budget_ledger.inspect(path)['reservations'])
        self.assertEqual(list((self.state / 'runs').glob('*.json')), [])

    def test_provider_mode_without_ledger_is_structured_refusal(self):
        with mock.patch.dict(os.environ, {provider_budget.CONFIG_ENV: '/unused/config.json'}):
            with self.assertRaises(hr.HeadlessError) as error:
                self.start()
        self.assertEqual(error.exception.code, 'PROVIDER_BUDGET_UNSUPPORTED')

    def test_global_serialization_holds_other_execution_but_not_idempotent_lookup(self):
        first = self.start(behavior='hang', wait=False, serialize_global=True)
        self.assertEqual(first['status'], 'launched')
        try:
            again = self.start(behavior='hang', wait=False, serialize_global=True)
            self.assertEqual(again['outcome'], 'already_running')
            with self.assertRaises(hr.HeadlessError) as error:
                self.start(execution_id='exec-2', serialize_global=True)
            self.assertEqual(error.exception.code, 'EXECUTION_GLOBAL_BARRIER')
        finally:
            hr.cancel(target=self.target, explicit_repo='acme/target', execution_id='exec-1')
