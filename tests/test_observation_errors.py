"""Unavailable GitHub observation is classified (#111) without keeping tokens, URLs or response bodies."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import delivery_observer as obs
import delivery_store as store
import issue_dispatcher as dispatch

SECRET = 'ghp_SECRETTOKEN0123456789abcdefghijklmnop'
BODY = '{"message":"private response body","documentation_url":"https://docs.github.com/x"}'

# What gh writes to stderr for each failure, as observed from gh 2.4x-2.10x.
FAILURES = {
    'auth': f'HTTP 401: Bad credentials (https://api.github.com/repos/acme/tool/pulls/3?access_token={SECRET})',
    'auth_403': 'gh: Resource not accessible by personal access token (HTTP 403)',
    'not_found': 'gh: Not Found (HTTP 404)',
    'not_found_graphql': "GraphQL: Could not resolve to a PullRequest with the number of 3. (repository.pullRequest)",
    'rate_limited': 'gh: API rate limit exceeded for user ID 1. (HTTP 403)',
    'rate_limited_429': 'gh: Too Many Requests (HTTP 429)',
    'gh_too_old': 'unknown flag: --slurp',
    'network': 'error connecting to api.github.com: dial tcp: lookup api.github.com: no such host',
    'network_5xx': 'gh: Server Error (HTTP 502)',
    'unexpected': f'something nobody anticipated {SECRET}',
}
EXPECTED = {'auth': ('auth', '401'), 'auth_403': ('auth', '403'), 'not_found': ('not_found', '404'),
            'not_found_graphql': ('not_found', 'none'), 'rate_limited': ('rate_limited', '403'),
            'rate_limited_429': ('rate_limited', '429'), 'gh_too_old': ('gh_too_old', 'none'),
            'network': ('network', 'none'), 'network_5xx': ('network', '502'), 'unexpected': ('unexpected', 'none')}


class ObservationErrorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        sha = 'a' * 40
        self.row = {'number': 7, 'repo': 'acme/tool', 'execution_id': 'issue-123', 'state': 'completed',
                    'checkout': '/tmp/x', 'mode': 'read-only'}
        parent = {'run_id': 'run-parent', 'parent_orchestration': {
            'target_repository': 'acme/tool', 'waves': [{'id': 'wave', 'status': 'integrated', 'task_ids': ['phase']}],
            'phases': [{'id': 'phase', 'child_run_ids': ['run-child'], 'diff_floor_checks': [
                {'status': 'passed', 'operation': 'parent', 'wave_id': 'wave', 'head': sha}]}]}}
        child = {'run_id': 'run-child', 'github_integration': {'target_repository': 'acme/tool', 'audit': [
            {'operation': 'create_draft_pr', 'status': 'applied', 'pr': 3}]}}
        (self.path / 'run-parent.json').write_text(json.dumps(parent))
        (self.path / 'run-child.json').write_text(json.dumps(child))
        self.record = {'status': 'exited', 'run': {'status': 'COMPLETED', 'run_id': 'run-parent',
                                                   'trace_path': str(self.path / 'run-parent.json')}}
        dispatch._NOTICES.clear()
        self.addCleanup(dispatch._NOTICES.clear)

    def fake_gh(self, stderr, *, sleep=0):
        """A gh that fails like the real one: a response body (with a token) on stdout, the cause on stderr."""
        script = self.path / 'gh'
        script.write_text('#!/bin/sh\n'
                          + (f'sleep {sleep}\n' if sleep else '')
                          + f"printf '%s\\n' '{BODY} {SECRET}'\n"
                          + "printf '%s\\n' \"$PI_FAKE_GH_STDERR\" >&2\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PI_FAKE_GH_STDERR': stderr}
        return str(script), env

    def observe(self, gh, env, timeout=40):
        return obs.observe(self.row, self.record,
                           lambda args: dispatch.command([gh, *args], env=env, timeout=timeout), ['test'])

    def assert_clean(self, text):
        for leak in (SECRET, 'private response body', 'api.github.com', 'docs.github.com', 'acme/tool/pulls'):
            self.assertNotIn(leak, text)

    def test_each_simulated_gh_failure_is_classified(self):
        for name, stderr in FAILURES.items():
            with self.subTest(name):
                item = self.observe(*self.fake_gh(stderr))
                cause, status = EXPECTED[name]
                self.assertEqual(item['checks'], 'unavailable')
                self.assertEqual(item['evidence'], {'observation_error': cause, 'observation_operation': 'api',
                                                    'observation_http_status': status})
                self.assertIn(f'({cause})', item['reason'])
                self.assertEqual(item['next_action'], obs.OBSERVATION_ERRORS[cause][1])
                self.assert_clean(json.dumps(item))

    def test_auth_next_action_points_at_the_token(self):
        item = self.observe(*self.fake_gh(FAILURES['auth']))
        self.assertEqual(item['next_action'], "Check the target token's permissions and expiry")

    def test_missing_gh(self):
        item = self.observe(str(self.path / 'absent-gh'), {})
        self.assertEqual(item['evidence']['observation_error'], 'gh_missing')

    def test_timeout_is_network(self):
        gh, env = self.fake_gh('never read', sleep=5)
        item = self.observe(gh, env, timeout=1)
        self.assertEqual(item['evidence']['observation_error'], 'network')

    def test_malformed_pull_request_is_invalid_response(self):
        item = obs.observe(self.row, self.record, lambda args: {'number': 999}, ['test'])
        self.assertEqual(item['evidence']['observation_error'], 'invalid_response')

    def test_unmatched_exception_is_unexpected_with_no_raw_text(self):
        def call(args):
            raise RuntimeError(f'{BODY} {SECRET}')
        item = obs.observe(self.row, self.record, call, ['test'])
        self.assertEqual(item['evidence']['observation_error'], 'unexpected')
        self.assertEqual(item['next_action'], 'Restore target access and retry')
        self.assert_clean(json.dumps(item))

    def test_failed_gh_reports_stderr_even_when_stdout_is_not_json(self):
        script = self.path / 'gh'
        script.write_text("#!/bin/sh\necho 'unknown flag: --slurp' >&2\nexit 1\n")
        script.chmod(0o700)
        item = self.observe(str(script), {'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
        self.assertEqual(item['evidence']['observation_error'], 'gh_too_old')

    def reconcile(self, cfg, db, gh, env):
        with patch.object(dispatch, 'pico_action', return_value=self.record), \
                patch.object(dispatch, 'target_env', return_value=env), \
                patch.object(dispatch, 'handoff_comment'):
            dispatch.reconcile_delivery(cfg, db, db.execute('SELECT * FROM approvals').fetchone())

    def test_stored_evidence_and_journal_carry_no_token_or_body_and_log_each_cause_once(self):
        cfg = {'state': self.path / 'state.sqlite', 'queue': 'acme/queue', 'target_gh_config_dir': self.path}
        err = io.StringIO()
        with dispatch.database(cfg) as db, contextlib.redirect_stderr(err):
            db.execute('INSERT INTO approvals (number,digest,repo,checkout,execution_id,mode,state) VALUES (?,?,?,?,?,?,?)',
                       (7, 'd', 'acme/tool', '/tmp/x', 'issue-123', 'read-only', 'completed'))
            db.commit()
            gh, env = self.fake_gh(FAILURES['auth'])
            cfg['gh'] = gh
            self.reconcile(cfg, db, gh, env)
            self.reconcile(cfg, db, gh, env)
            env = dict(env, PI_FAKE_GH_STDERR=FAILURES['rate_limited'])
            self.reconcile(cfg, db, gh, env)
            raw = db.execute('SELECT projection FROM deliveries').fetchone()[0]
        log = err.getvalue()
        self.assertEqual(log.splitlines(), [
            'Issue #7: GitHub observation unavailable: auth (gh api, HTTP 401)',
            'Issue #7: GitHub observation unavailable: rate_limited (gh api, HTTP 403)'])
        self.assert_clean(log)
        self.assert_clean(raw)
        stored = store.list_deliveries(cfg['state'])[0]
        self.assertEqual(stored['evidence'], {'observation_error': 'rate_limited', 'observation_operation': 'api',
                                              'observation_http_status': '403'})


if __name__ == '__main__':
    unittest.main()
