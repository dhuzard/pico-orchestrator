"""Offline delivery observation and crash-reconciliation tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import delivery_observer as obs
import delivery_store as store
import issue_dispatcher as dispatch


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.sha = 'a' * 40
        self.row = {'number': 7, 'repo': 'acme/tool', 'execution_id': 'issue-123', 'state': 'completed', 'checkout': '/tmp/x', 'mode': 'read-only'}
        parent = {'run_id': 'run-parent', 'parent_orchestration': {'target_repository': 'acme/tool', 'waves': [{'id': 'wave', 'status': 'integrated', 'task_ids': ['phase']}], 'phases': [{'id': 'phase', 'child_run_ids': ['run-child'], 'diff_floor_checks': [{'status': 'passed', 'operation': 'parent', 'wave_id': 'wave', 'head': self.sha}]}]}}
        child = {'run_id': 'run-child', 'github_integration': {'target_repository': 'acme/tool', 'audit': [{'operation': 'create_draft_pr', 'status': 'applied', 'pr': 3}]}}
        (self.path / 'run-parent.json').write_text(json.dumps(parent))
        (self.path / 'run-child.json').write_text(json.dumps(child))
        self.record = {'run': {'run_id': 'run-parent', 'trace_path': str(self.path / 'run-parent.json')}}
        self.pr = {'number': 3, 'state': 'open', 'merged_at': None, 'head': {'sha': self.sha, 'repo': {'full_name': 'acme/tool'}}, 'base': {'ref': 'main', 'repo': {'full_name': 'acme/tool'}}}
        self.checks = {'number': 3, 'headRefOid': self.sha, 'statusCheckRollup': [{'name': 'test', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
        self.review = []
        self.rules = {'required_status_checks': {'contexts': ['test']}}
        self.end = self.pr

    def call(self, args):
        if args[0] == 'pr': return self.checks
        if '/rules/branches/' in args[-1]: return [[]]
        if args[-1].endswith('/protection'): return self.rules
        if args[-1].endswith('/reviews'): return [self.review]
        if not hasattr(self, 'seen'):
            self.seen = True
            return self.pr
        return self.end

    def observe(self):
        self.__dict__.pop('seen', None)
        return obs.observe(self.row, self.record, self.call, ['test'])

    def test_exact_head_check_and_human_review(self):
        result = self.observe()
        self.assertEqual((result['delivery'], result['checks']), ('needs_human_review', 'passed'))
        self.review = [{'user': {'type': 'Bot', 'login': 'bot'}, 'state': 'APPROVED', 'commit_id': self.sha}]
        self.assertEqual(self.observe()['review'], 'not_requested')
        self.review = [{'user': {'type': 'User', 'login': 'alice'}, 'state': 'APPROVED', 'commit_id': 'b' * 40}]
        self.assertEqual(self.observe()['review'], 'stale')
        self.review.append({'user': {'type': 'User', 'login': 'alice'}, 'state': 'DISMISSED', 'commit_id': self.sha})
        self.assertEqual(self.observe()['review'], 'not_requested')
        self.end = {**self.pr, 'head': {**self.pr['head'], 'sha': 'b' * 40}}
        self.assertEqual(self.observe()['checks'], 'stale')

    def test_checks_fail_closed(self):
        for verdict, expected in [('SKIPPED', 'failed'), ('NEUTRAL', 'failed'), ('CANCELLED', 'failed')]:
            self.checks['statusCheckRollup'][0]['conclusion'] = verdict
            self.assertEqual(self.observe()['checks'], expected)
        self.checks['statusCheckRollup'] = []
        self.assertEqual(self.observe()['checks'], 'unknown')
        self.checks['statusCheckRollup'] = [{'name': 'test', 'status': 'IN_PROGRESS'}]
        self.assertEqual(self.observe()['delivery'], 'waiting_ci')
        self.rules = {'required_status_checks': {'contexts': ['missing']}}
        self.assertEqual(self.observe()['checks'], 'unknown')

    def test_publication_identity_and_unavailable(self):
        child = json.loads((self.path / 'run-child.json').read_text())
        child['github_integration']['target_repository'] = 'evil/repo'
        (self.path / 'run-child.json').write_text(json.dumps(child))
        self.assertEqual(self.observe()['publication'], 'none')
        (self.path / 'run-child.json').unlink()
        self.assertEqual(self.observe()['publication'], 'unavailable')
        self.assertEqual(obs.observe(self.row, self.record, lambda _: (_ for _ in ()).throw(ValueError()), ['test'])['delivery'], 'needs_decision')

    def test_restart_completed_persists_and_does_not_launch(self):
        cfg = {'state': self.path / 'state.sqlite', 'queue': 'acme/queue', 'gh': 'gh'}
        with dispatch.database(cfg) as db:
            db.execute('INSERT INTO approvals (number,digest,repo,checkout,execution_id,mode,state) VALUES (?,?,?,?,?,?,?)', (7, 'd', 'acme/tool', '/tmp/x', 'issue-123', 'read-only', 'completed'))
            db.commit()
            with patch.object(dispatch, 'pico_action', return_value={'status': 'exited', 'run': {'status': 'COMPLETED'}}) as action, patch.object(dispatch, 'handoff_comment'):
                dispatch.tick(cfg, db)
                self.assertEqual(action.call_args.args[2], 'status')
            self.assertEqual(db.execute('SELECT state FROM approvals').fetchone()[0], 'completed')
        self.assertEqual(store.list_deliveries(cfg['state'])[0]['execution_state'], 'completed')
        self.assertEqual(store.list_deliveries(self.path / 'absent'), [])

    def test_unknown_rules_still_invite_review_with_explicit_limitation(self):
        def call(args):
            if args[-1].endswith('/protection'):
                raise ValueError('forbidden or absent')
            return self.call(args)
        item = obs.observe(self.row, self.record, call, ['test'])
        self.assertEqual(item['delivery'], 'needs_human_review')
        self.assertEqual(item['evidence']['rules'], 'unknown')
        self.assertEqual(item['evidence']['merge_readiness'], 'unknown')

    def test_uncertain_launch_blocks_next_approval(self):
        cfg = {'state': self.path / 'state.sqlite', 'queue': 'acme/queue', 'gh': 'gh',
               'targets': {'acme/tool': '/tmp/x'}}
        issue = {'number': 8, 'title': 'work', 'state': 'OPEN', 'labels': [{'name': 'pico:queued'}],
                 'body': '## Target repository\nacme/tool\n## Objective\nwork\n## Acceptance criteria\npass'}
        task = dispatch.parse_task(issue, cfg)
        with dispatch.database(cfg) as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL)', (7, 'd', 'acme/tool', '/tmp/x', 'issue-123', 'read-only', 'launch_uncertain'))
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL)', (8, task.digest, task.repo, task.checkout, task.execution_id, 'read-only', 'approved'))
            db.commit()
            with patch.object(dispatch, 'pico_action', return_value={'status': 'launch_uncertain'}) as action, patch.object(dispatch, 'issue_view', return_value=issue), patch.object(dispatch, 'report'):
                dispatch.tick(cfg, db)
            self.assertEqual(action.call_count, 1)
            self.assertEqual(action.call_args.args[2], 'status')
            self.assertEqual([x[0] for x in db.execute('SELECT state FROM approvals ORDER BY number')], ['launch_uncertain', 'approved'])

    def test_comment_reconciliation_after_unknown_post(self):
        cfg = {'queue': 'acme/queue', 'gh': 'gh'}
        item = {'delivery': 'needs_decision', 'execution': 'completed', 'publication': 'none', 'checks': 'unknown', 'review': 'unknown', 'reason': 'No PR', 'next_action': 'Inspect', 'pr_url': None}
        remote = []
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == 'user': return {'id': 42}
            if '--paginate' in argv: return [remote]
            body = argv[-1][len('body='):]
            if '--method' in argv and 'POST' in argv:
                remote.append({'id': 1, 'body': body, 'user': {'id': 42}})
                raise dispatch.DispatcherError('timeout after write')
            raise AssertionError('duplicate or unexpected update')
        with patch.object(dispatch, 'command', side_effect=command):
            dispatch.handoff_comment(cfg, self.row, item)
            dispatch.handoff_comment(cfg, self.row, item)
        self.assertEqual(sum('POST' in call for call in calls), 1)

    def test_corrupt_or_malformed_delivery_is_unavailable_not_empty(self):
        path = self.path / 'bad.sqlite'
        path.write_bytes(b'not a sqlite database')
        with self.assertRaises(store.DeliveryStoreError):
            store.list_deliveries(path)
        path.unlink()
        cfg = {'state': path}
        with dispatch.database(cfg) as db:
            item = self.observe()
            store.save(db, item)
            item['head_sha'] = {'private': 'must not appear'}
            db.execute('UPDATE deliveries SET projection=?', (json.dumps(item),))
            db.commit()
        with self.assertRaises(store.DeliveryStoreError):
            store.list_deliveries(path)

    def test_copied_marker_from_another_author_is_never_edited(self):
        item = self.observe()
        marker = f"<!-- pico-delivery:{self.row['execution_id']} -->"
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == 'user': return {'id': 42}
            if '--paginate' in argv: return [[{'id': 99, 'user': {'id': 7}, 'body': marker + '\nHuman note'}]]
            self.assertIn('POST', argv)
            self.assertNotIn('PATCH', argv)
            return {'id': 100}
        with patch.object(dispatch, 'command', side_effect=command):
            dispatch.handoff_comment({'queue': 'acme/queue', 'gh': 'gh'}, self.row, item)
        self.assertEqual(sum('POST' in argv for argv in calls), 1)

    def test_receipt_binds_final_head_in_a_multiple_wave_run(self):
        parent = json.loads((self.path / 'run-parent.json').read_text())
        phase = parent['parent_orchestration']['phases'][0]
        phase['diff_floor_checks'].append({**phase['diff_floor_checks'][0], 'head': 'b' * 40})
        (self.path / 'run-parent.json').write_text(json.dumps(parent))
        child = json.loads((self.path / 'run-child.json').read_text())
        child['github_integration']['audit'] = []
        child['github_integration']['publication_receipt'] = {'stage': 'pr_observed', 'repository': 'acme/tool',
                                                              'parent_run_id': 'run-parent', 'commit': self.sha, 'pr': 3}
        (self.path / 'run-child.json').write_text(json.dumps(child))
        self.assertEqual(self.observe()['delivery'], 'needs_human_review')

    def test_human_repair_allowance_is_durable_and_cannot_be_reset(self):
        cfg = {'state': self.path / 'repairs.sqlite', 'queue': 'acme/queue', 'gh': 'gh',
               'targets': {'acme/tool': '/tmp/x'}}
        def issue(number):
            return {'number': number, 'title': 'Scoped repair', 'state': 'OPEN', 'labels': ['pico:queued'],
                    'body': '## Target repository\nacme/tool\n## Objective\nRepair one failing check in the approved scope\n## Acceptance criteria\nOriginal check passes'}
        parent = {'head_sha': self.sha}
        with patch.object(dispatch.sys.stdin, 'isatty', return_value=True), \
                patch.object(dispatch, 'issue_view', side_effect=lambda _cfg, n: issue(n)), \
                patch.object(dispatch, 'repair_parent', return_value=parent), \
                patch('builtins.input', side_effect=lambda _: 'approve ' + dispatch.parse_task(issue(8), cfg).digest[:12]):
            dispatch.approve(cfg, 8, True, repair_of=7)
            # Restart/reapproval is idempotent for this same exact task.
            dispatch.approve(cfg, 8, True, repair_of=7)
        with patch.object(dispatch.sys.stdin, 'isatty', return_value=True), \
                patch.object(dispatch, 'issue_view', side_effect=lambda _cfg, n: issue(n)), \
                patch.object(dispatch, 'repair_parent', return_value=parent), \
                patch('builtins.input', side_effect=lambda _: 'approve ' + dispatch.parse_task(issue(9), cfg).digest[:12]):
            with self.assertRaisesRegex(dispatch.DispatcherError, 'allowance exhausted'):
                dispatch.approve(cfg, 9, True, repair_of=7)
        with dispatch.database(cfg) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_repairs').fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT 1 FROM approvals WHERE number=9').fetchone())

    def test_uncertainty_with_error_is_still_an_active_barrier(self):
        self.assertEqual(dispatch.classify({'status': 'launch_uncertain', 'error_code': 'RECOVERY_REQUIRED'}), 'launch_uncertain')
        self.assertEqual(dispatch.classify({'error_code': 'EXECUTION_GLOBAL_BARRIER'}), 'approved')

    def test_approval_cannot_overwrite_a_run_that_started_while_prompt_waited(self):
        cfg = {'state': self.path / 'approval-race.sqlite', 'queue': 'acme/queue', 'gh': 'gh', 'targets': {'acme/tool': '/tmp/x'}}
        issue = {'number': 8, 'title': 'work', 'state': 'OPEN', 'labels': ['pico:queued'],
                 'body': '## Target repository\nacme/tool\n## Objective\nwork\n## Acceptance criteria\npass'}
        task = dispatch.parse_task(issue, cfg)
        with dispatch.database(cfg) as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL)', (8, task.digest, task.repo, task.checkout, task.execution_id, 'local-mutation', 'approved'))
            db.commit()
        def answer(_):
            with dispatch.database(cfg) as db:
                db.execute("UPDATE approvals SET state='running' WHERE number=8")
                db.commit()
            return 'approve ' + task.digest[:12]
        with patch.object(dispatch.sys.stdin, 'isatty', return_value=True), \
                patch.object(dispatch, 'issue_view', return_value=issue), patch('builtins.input', side_effect=answer):
            with self.assertRaisesRegex(dispatch.DispatcherError, 'changed while approval'):
                dispatch.approve(cfg, 8, True)
        with dispatch.database(cfg) as db:
            self.assertEqual(db.execute('SELECT state FROM approvals').fetchone()[0], 'running')

    def test_recovered_launch_observes_child_before_changed_queue_text(self):
        cfg = {'state': self.path / 'launch-race.sqlite', 'queue': 'acme/queue', 'gh': 'gh'}
        with dispatch.database(cfg) as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL)', (8, 'old-digest', 'acme/tool', '/tmp/x', 'issue-child', 'local-mutation', 'launching'))
            db.commit()
            with patch.object(dispatch, 'pico_action', return_value={'status': 'launched'}) as action, \
                    patch.object(dispatch, 'issue_view') as read_issue:
                dispatch.tick(cfg, db)
            self.assertEqual(action.call_args.args[2], 'status')
            read_issue.assert_not_called()
            self.assertEqual(db.execute('SELECT state FROM approvals').fetchone()[0], 'running')

    def test_partial_execution_remains_immutable_and_asks_for_human_decision(self):
        cfg = {'state': self.path / 'partial.sqlite', 'queue': 'acme/queue', 'gh': 'gh'}
        with dispatch.database(cfg) as db:
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL)', (8, 'digest', 'acme/tool', '/tmp/x', 'issue-partial', 'local-mutation', 'partial'))
            db.commit()
            with patch.object(dispatch, 'pico_action', return_value={'status': 'exited', 'run': {'status': 'PARTIAL'}}) as action, \
                    patch.object(dispatch, 'handoff_comment'):
                dispatch.tick(cfg, db)
            self.assertEqual(action.call_args.args[2], 'status')
            self.assertEqual(db.execute('SELECT state FROM approvals').fetchone()[0], 'partial')
        item = store.list_deliveries(cfg['state'])[0]
        self.assertEqual((item['execution_state'], item['delivery_state']), ('partial', 'needs_decision'))

if __name__ == '__main__': unittest.main()
