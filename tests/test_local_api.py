import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from android_usb_rescue import local_api as module


class LocalControlApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.api = module.LocalControlApi(self.base, 12345)
        self.calls = 0

    def check(self):
        self.calls += 1
        return True

    def request(self, action='check_files', **changes):
        request_id = uuid.uuid4().hex
        payload = {'id': request_id, 'action': action, 'target_pid': 12345,
                   'created_at': time.time()}
        payload.update(changes)
        path = self.api.requests / (request_id + '.json')
        path.write_text(json.dumps(payload), encoding='utf-8')
        return request_id, payload, path

    def response(self, request_id):
        return json.loads((self.api.results / (request_id + '.json')).read_text(encoding='utf-8'))

    def test_invalid_requests_never_execute_callback(self):
        for changes in ({'created_at': time.time()-31}, {'target_pid': 54321},
                        {'action': 'send_key'}, {'created_at': float('nan')},
                        {'created_at': time.time()+3}, {'id': '0'*32},
                        {'path': 'unapproved'}):
            with self.subTest(changes=changes):
                request_id, _, _ = self.request(**changes)
                self.api.poll({}, self.check)
                self.assertFalse(self.response(request_id)['ok'])
        self.assertEqual(self.calls, 0)

    def test_status_is_read_only_and_oversized_status_fails_safely(self):
        request_id, _, _ = self.request('status')
        self.api.poll({'connected': True}, self.check)
        self.assertEqual(self.response(request_id)['state'], {'connected': True})
        request_id, _, _ = self.request('status')
        self.api.poll({'large': 'x' * 70000}, self.check)
        self.assertEqual(self.response(request_id)['error'], 'invalid_state')
        self.assertEqual(self.calls, 0)

    def test_check_executes_once_even_after_service_restart(self):
        request_id, payload, path = self.request()
        self.api.poll({}, self.check)
        path.write_text(json.dumps(payload), encoding='utf-8')
        module.LocalControlApi(self.base, 12345).poll({}, self.check)
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.response(request_id)['accepted'])

    def test_busy_does_not_retry_and_cli_reports_not_accepted(self):
        request_id, _, _ = self.request()
        calls = []
        self.api.poll({}, lambda: calls.append(1) or False)
        self.api.poll({}, self.check)
        self.assertEqual(calls, [1])
        self.assertFalse(self.response(request_id)['accepted'])
        with patch('sys.argv', ['local_api', 'check_files']), patch.object(
                module, 'run_client', return_value={'ok': True, 'accepted': False}):
            self.assertEqual(module.main(), 2)

    def test_capture_uses_independent_callback_without_replay(self):
        request_id, payload, path = self.request('capture_screenshot')
        captures = []
        capture = lambda: captures.append(1) or True
        self.api.poll({}, self.check, capture)
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.api.poll({}, self.check, capture)
        self.assertEqual(captures, [1])
        self.assertEqual(self.calls, 0)
        self.assertTrue(self.response(request_id)['accepted'])

    def test_capture_without_callback_has_no_fallback(self):
        request_id, _, _ = self.request('capture_screenshot')
        self.api.poll({}, self.check)
        self.assertEqual(self.response(request_id)['error'], 'capture_unavailable')
        self.assertEqual(self.calls, 0)

    def test_callback_error_does_not_disclose_exception(self):
        request_id, _, _ = self.request()
        def failing():
            raise RuntimeError('private exception detail')
        self.api.poll({}, failing)
        result = self.response(request_id)
        self.assertEqual(result['error'], 'check_failed')
        self.assertNotIn('private', json.dumps(result))

    def test_invalid_filenames_cannot_starve_valid_requests(self):
        for name in ['a.json', 'b.json', 'c.json', 'd.json', 'e.json']:
            (self.api.requests / name).write_text('{}', encoding='utf-8')
        request_id, _, _ = self.request()
        self.api.poll({}, self.check)
        self.assertTrue(self.response(request_id)['accepted'])
        self.assertEqual(self.calls, 1)

    def test_cleanup_removes_only_expired_protocol_files(self):
        old = time.time() - module.RETENTION_SECONDS - 1
        request_id, payload, _ = self.request()
        self.api.poll({}, self.check)
        marker = self.api.results / (request_id + '.claim')
        os.utime(marker, (old, old))
        os.utime(self.api.results / (request_id + '.json'), (old, old))
        unrelated = self.api.results / 'user-notes.txt'
        unrelated.write_text('keep', encoding='utf-8')
        os.utime(unrelated, (old, old))
        self.api._sweep_iterators.clear()
        self.api.poll({}, self.check)
        self.assertFalse(marker.exists())
        self.assertTrue(unrelated.exists())
        payload['created_at'] = old
        path = self.api.requests / (request_id + '.json')
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.api.poll({}, self.check)
        self.assertEqual(self.response(request_id)['error'], 'request_expired')
        self.assertEqual(self.calls, 1)

    def test_client_round_trip_against_recent_enabled_service(self):
        (self.base / 'status.json').write_text(json.dumps({
            'pid': 12345, 'time': time.time(), 'closed': False,
            'local_api_enabled': True}), encoding='utf-8')
        outcome = []
        client = threading.Thread(target=lambda: outcome.append(
            module.run_client(self.base, 'status', 1)))
        client.start()
        deadline = time.monotonic()+1
        while client.is_alive() and time.monotonic() < deadline:
            self.api.poll({'connected': True}, self.check)
            time.sleep(0.01)
        client.join(1)
        self.assertEqual(outcome[0]['state'], {'connected': True})
        self.assertEqual(self.calls, 0)

    def test_disabled_or_stale_client_does_not_submit(self):
        for values in ({'local_api_enabled': False, 'time': time.time()},
                       {'local_api_enabled': True, 'time': time.time()-6}):
            (self.base / 'status.json').write_text(json.dumps({
                'pid': 12345, 'closed': False, **values}), encoding='utf-8')
            self.assertFalse(module.run_client(self.base, 'check_files', 1)['ok'])
        self.assertEqual(list(self.api.requests.glob('*.json')), [])


if __name__ == '__main__':
    unittest.main()
