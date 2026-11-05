"""Optional local-file API; no network listener or arbitrary input endpoint.

The GUI creates this service only when explicitly enabled. Request IDs are
retained longer than the request lifetime; expired protocol files are cleaned
in bounded batches. Status reads never send phone input.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
from pathlib import Path
import re
import time
import uuid


REQUEST_ID = re.compile(r'[0-9a-f]{32}\Z')
TEMP_NAME = re.compile(r'[0-9a-f]{32}\.json\.[0-9a-f]{32}\.tmp\Z')
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 65536
REQUEST_LIFETIME = 30
RETENTION_SECONDS = 300
REQUEST_KEYS = {'id', 'action', 'target_pid', 'created_at'}
ACTIONS = ('status', 'check_files', 'capture_screenshot')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _atomic_json(path, data, maximum=MAX_RESPONSE_BYTES):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > maximum:
            raise ValueError('metadata_too_large')
        with temporary.open('xb') as output:
            output.write(encoded)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class LocalControlApi:
    def __init__(self, data_dir, pid):
        self.base = Path(data_dir).expanduser().resolve()
        self.pid = pid
        self.requests = self.base / 'control-requests'
        self.results = self.base / 'control-results'
        self.ready = False
        self._sweep_iterators = {}
        try:
            self.requests.mkdir(parents=True, exist_ok=True)
            self.results.mkdir(parents=True, exist_ok=True)
            self.ready = all(directory.resolve() == directory
                             and not directory.is_symlink()
                             for directory in (self.requests, self.results))
        except OSError:
            pass

    def _respond(self, request_id, **fields):
        try:
            _atomic_json(self.results / (request_id + '.json'), {'id': request_id, **fields})
        except (TypeError, ValueError, RecursionError):
            try:
                _atomic_json(self.results / (request_id + '.json'),
                             {'id': request_id, 'ok': False, 'error': 'invalid_state'})
            except (OSError, TypeError, ValueError, RecursionError):
                pass
        except OSError:
            pass

    def _consume(self, path, status, on_check, on_capture):
        request_id = path.stem
        marker = self.results / (request_id + '.claim')
        claimed = self.requests / (request_id + '.claimed')
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        except FileExistsError:
            path.unlink(missing_ok=True)
            return
        except OSError:
            return
        try:
            os.replace(path, claimed)
            if claimed.is_symlink() or not claimed.is_file():
                raise ValueError('invalid_request_file')
            with claimed.open('rb') as source:
                raw = source.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError('request_too_large')
            try:
                request = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError('invalid_json') from None
            if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
                raise ValueError('invalid_request_fields')
            if request['id'] != request_id:
                raise ValueError('id_mismatch')
            if type(request['target_pid']) is not int or request['target_pid'] != self.pid:
                raise ValueError('pid_mismatch')
            stamp = request['created_at']
            if not _number(stamp) or not -2 <= time.time() - stamp <= REQUEST_LIFETIME:
                raise ValueError('request_expired')
            action = request['action']
            if action == 'status':
                self._respond(request_id, ok=True, state=status)
            elif action in ('check_files', 'capture_screenshot'):
                callback = on_check if action == 'check_files' else on_capture
                if callback is None:
                    self._respond(request_id, ok=False, error='capture_unavailable')
                    return
                try:
                    accepted = callback()
                except Exception:
                    self._respond(request_id, ok=False,
                                  error='check_failed' if action == 'check_files' else 'capture_failed')
                    return
                if type(accepted) is not bool:
                    self._respond(request_id, ok=False, error='invalid_callback_result')
                else:
                    self._respond(request_id, ok=True, accepted=accepted)
            else:
                raise ValueError('action_not_allowed')
        except ValueError as error:
            self._respond(request_id, ok=False, error=str(error))
        except OSError:
            self._respond(request_id, ok=False, error='request_io_failed')
        finally:
            try:
                claimed.unlink(missing_ok=True)
            except OSError:
                pass

    def _maintain(self):
        """Delete only stale files with names owned by this protocol."""
        cutoff = time.time() - RETENTION_SECONDS
        for directory in (self.requests, self.results):
            iterator = self._sweep_iterators.setdefault(directory, iter(directory.iterdir()))
            for _ in range(16):
                try:
                    path = next(iterator)
                except StopIteration:
                    self._sweep_iterators.pop(directory, None)
                    break
                owned = (path.suffix in ('.json', '.claim', '.claimed')
                         and REQUEST_ID.fullmatch(path.stem)) or TEMP_NAME.fullmatch(path.name)
                if not owned:
                    continue
                try:
                    if path.stat().st_mtime < cutoff and not path.is_symlink():
                        path.unlink(missing_ok=True)
                except OSError:
                    continue

    def poll(self, status: dict, on_check, on_capture=None):
        """Handle up to four validly named requests and bounded housekeeping."""
        if not self.ready:
            return
        try:
            paths = (path for path in self.requests.glob('*.json')
                     if REQUEST_ID.fullmatch(path.stem))
            for path in itertools.islice(paths, 4):
                try:
                    self._consume(path, status, on_check, on_capture)
                except Exception:
                    # A malformed local file must not stop the GUI event loop.
                    continue
            self._maintain()
        except OSError:
            pass


def run_client(data_dir, action, timeout=5):
    """Submit exactly one request against a recent, explicitly enabled GUI."""
    base = Path(data_dir).expanduser().resolve()
    if action not in ACTIONS:
        return {'ok': False, 'error': 'action_not_allowed'}
    if not _number(timeout) or not 0 < timeout <= 60:
        return {'ok': False, 'error': 'invalid_timeout'}
    try:
        with (base / 'status.json').open('rb') as source:
            raw = source.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return {'ok': False, 'error': 'invalid_app_status'}
        state = json.loads(raw)
        if not isinstance(state, dict):
            return {'ok': False, 'error': 'invalid_app_status'}
        if state.get('local_api_enabled') is not True:
            return {'ok': False, 'error': 'local_api_disabled'}
        pid, stamp = state.get('pid'), state.get('time')
        if (type(pid) is not int or pid <= 0 or state.get('closed') is not False
                or not _number(stamp) or not -2 <= time.time()-stamp <= 5):
            return {'ok': False, 'error': 'app_not_ready_or_stale'}
        api = LocalControlApi(base, pid)
        if not api.ready:
            return {'ok': False, 'error': 'local_directories_unavailable'}
        request_id = uuid.uuid4().hex
        _atomic_json(api.requests / (request_id + '.json'),
                     {'id': request_id, 'action': action, 'target_pid': pid,
                      'created_at': time.time()}, MAX_REQUEST_BYTES)
        result_path = api.results / (request_id + '.json')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with result_path.open('rb') as source:
                    raw = source.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return {'ok': False, 'error': 'invalid_response'}
                response = json.loads(raw)
                if isinstance(response, dict) and response.get('id') == request_id:
                    return response
            except (OSError, ValueError, UnicodeError, RecursionError):
                pass
            time.sleep(0.05)
        try:
            (api.requests / (request_id + '.json')).unlink(missing_ok=True)
        except OSError:
            pass
        return {'id': request_id, 'ok': False, 'error': 'response_timeout'}
    except (OSError, ValueError, UnicodeError, TypeError, AttributeError, RecursionError):
        return {'ok': False, 'error': 'local_api_unavailable'}


def main():
    from .settings import get_data_dir

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--data-dir')
    parser.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args()
    result = run_client(get_data_dir(args.data_dir), args.action, args.timeout)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if not result.get('ok'):
        return 1
    return 2 if result.get('accepted') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
