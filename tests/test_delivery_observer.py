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
        child = {'github_integration': {'target_repository': 'acme/tool', 'audit': [{'operation': 'create_draft_pr', 'status': 'applied', 'pr': 3}]}}
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
            if '--paginate' in argv: return [remote]
            body = argv[-1][len('body='):]
            if '--method' in argv and 'POST' in argv:
                remote.append({'id': 1, 'body': body})
                raise dispatch.DispatcherError('timeout after write')
            raise AssertionError('duplicate or unexpected update')
        with patch.object(dispatch, 'command', side_effect=command):
            dispatch.handoff_comment(cfg, self.row, item)
            dispatch.handoff_comment(cfg, self.row, item)
        self.assertEqual(sum('POST' in call for call in calls), 1)

if __name__ == '__main__': unittest.main()
