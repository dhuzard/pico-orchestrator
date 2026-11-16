"""Delivery HTTP reads cannot hide corruption, leak another repo or bypass local controls."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import delivery_store
import watch
import garden_controls


class DeliveryEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def get(self, *, repository=None, controls=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), watch.make_handler(
            self.root / 'runs', repository, controls=controls))
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/deliveries', timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
        finally:
            thread.join(timeout=5)

    def test_filters_target_repository_and_no_creation_on_missing_database(self):
        code, body = self.get()
        self.assertEqual((code, body), (200, {'ok': True, 'deliveries': []}))
        self.assertFalse((self.root / 'issue-dispatcher.sqlite3').exists())
        with patch.object(delivery_store, 'list_deliveries', return_value=[{'repository': 'acme/app'}, {'repository': 'private/other'}]):
            code, body = self.get(repository='ACME/app')
        self.assertEqual(body['deliveries'], [{'repository': 'acme/app'}])

    def test_corruption_reports_unavailable(self):
        (self.root / 'issue-dispatcher.sqlite3').write_bytes(b'corrupt')
        code, body = self.get()
        self.assertEqual(code, 503)
        self.assertFalse(body['ok'])
        self.assertNotIn('corrupt', json.dumps(body))

    def test_controls_token_required_before_delivery_read(self):
        class Controls:
            def check_token(self, _headers):
                raise garden_controls.ControlError(403, 'CONTROL_TOKEN_INVALID', 'Token required')
        with patch.object(delivery_store, 'list_deliveries') as read:
            code, _ = self.get(controls=Controls())
        self.assertEqual(code, 403)
        read.assert_not_called()
