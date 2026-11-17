"""Real fake-provider launches exercise central admission, not mocked start()."""
import os

from test_headless_run import HeadlessFixture
import budget_ledger
import headless_run as hr


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
