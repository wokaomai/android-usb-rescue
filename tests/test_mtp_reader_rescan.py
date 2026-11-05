"""Regression checks for changing screenshot directories; no USB is opened."""
from collections import Counter, deque
from io import BytesIO
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from android_usb_rescue import mtp_reader as mtp


_png_buffer = BytesIO()
Image.new('RGB', (1, 1), 'white').save(_png_buffer, format='PNG')
PNG = _png_buffer.getvalue()


class Scenario:
    def __init__(self, listings, bad_info=None, download_codes=None):
        self.listings = deque(listings)
        self.last_listing = None
        self.bad_info = bad_info or {}
        self.download_codes = deque(download_codes or [])
        self.info_calls = Counter()
        self.list_calls = 0
        self.downloads = []
        self.readers = []

    def reader(self, session, seconds):
        scenario = self

        class Reader:
            def __init__(self):
                self.deadline = time.monotonic() + seconds
                self.healthy = True
                self.report = {'ok': False, 'cleanup': {}}
                self.closed = False
                self.seconds = seconds

            def connect(self):
                pass

            def start_session(self):
                pass

            def request(self, operation, parameters=(), **kwargs):
                if operation == 0x1004:
                    return mtp.OK, struct.pack('<II', 1, 65537), b''
                if operation == 0x1009:
                    handle = parameters[0]
                    scenario.downloads.append(handle)
                    code = scenario.download_codes.popleft() if scenario.download_codes else mtp.OK
                    return code, PNG if code == mtp.OK else None, b''
                raise AssertionError(f'Unexpected request {operation:x}')

            def close(self):
                self.closed = True

        reader = Reader()
        self.readers.append(reader)
        return reader

    def handles(self, reader, storage, parent, image_format=0, maximum=256):
        if parent == 0xFFFFFFFF:
            return [10]
        if parent == 10:
            return [20]
        if parent != 20:
            raise AssertionError('Unexpected directory')
        self.list_calls += 1
        if self.listings:
            self.last_listing = self.listings.popleft()
        return list(self.last_listing)

    def info(self, reader, handle):
        self.info_calls[handle] += 1
        if handle in self.bad_info:
            raise self.bad_info[handle]
        if handle in (10, 20):
            return {'handle': handle, 'storage': 65537, 'format': 0x3001,
                    'parent': 0xFFFFFFFF if handle == 10 else 10,
                    'name': 'Pictures' if handle == 10 else 'Screenshots'}
        return {'handle': handle, 'storage': 65537, 'format': 0x3801,
                'size': len(PNG), 'parent': 20, 'name': f'Screenshot_{handle}.png',
                'created': f'20261105T1200{handle-100:02d}', 'modified': ''}

    def run(self):
        with tempfile.TemporaryDirectory(prefix='k60-mtp-rescan-') as folder:
            with patch.object(mtp, '_Reader', self.reader), \
                    patch.object(mtp, '_handles', self.handles), \
                    patch.object(mtp, '_object_info', self.info), \
                    patch.object(mtp.time, 'sleep', lambda _: None):
                result = mtp.latest_screenshot(object(), folder)
            files = list(Path(folder).glob('*'))
            payloads = [p.read_bytes() for p in files]
        self.assert_cleanup()
        return result, payloads

    def assert_cleanup(self):
        assert len(self.readers) == 1
        assert self.readers[0].seconds == 45
        assert self.readers[0].closed


class ScreenshotRescanTests(unittest.TestCase):
    def test_invalid_downloaded_image_is_not_saved_or_successful(self):
        with patch(f'{__name__}.PNG', b'\x89PNG\r\n\x1a\n'):
            report, files = Scenario([[101]]).run()
        self.assertFalse(report['ok'])
        self.assertNotIn('screenshot_path', report)
        self.assertEqual(files, [])

    def test_deleted_handle_causes_complete_scan_of_new_list(self):
        scenario = Scenario([[101, 102], [101, 103], [101, 103]],
                            bad_info={102: mtp.InvalidObjectHandleError(0x1008, 102)})
        report, files = scenario.run()
        self.assertTrue(report['ok'])
        self.assertEqual(report['full_rescans'], 1)
        self.assertEqual(scenario.info_calls[101], 2)
        self.assertEqual(scenario.info_calls[103], 1)
        self.assertEqual(scenario.downloads, [103])
        self.assertEqual(report['screenshot_object_handle'], 103)
        self.assertEqual(files, [PNG])

    def test_persistent_invalid_handle_fails_without_skipping(self):
        scenario = Scenario([[101, 102], [101, 102]],
                            bad_info={102: mtp.InvalidObjectHandleError(0x1008, 102)})
        report, files = scenario.run()
        self.assertFalse(report['ok'])
        self.assertIn('0x2009', report['error'])
        self.assertTrue(report['refresh_exhausted'])
        self.assertEqual(scenario.info_calls[102], 2)
        self.assertEqual(scenario.downloads, [])
        self.assertEqual(files, [])

    def test_other_metadata_error_is_not_retried(self):
        scenario = Scenario([[101, 102]], bad_info={102: mtp.MtpError('rejected: 0x2005')})
        report, files = scenario.run()
        self.assertFalse(report['ok'])
        self.assertEqual(report['full_rescans'], 0)
        self.assertEqual(scenario.list_calls, 1)
        self.assertEqual(files, [])

    def test_deleted_during_download_restarts_entire_scan(self):
        scenario = Scenario([[101], [103], [103]], download_codes=[0x2009, mtp.OK])
        report, files = scenario.run()
        self.assertTrue(report['ok'])
        self.assertEqual(scenario.downloads, [101, 103])
        self.assertEqual(report['full_rescans'], 1)
        self.assertEqual(files, [PNG])

    def test_new_image_during_scan_requires_rescan_before_save(self):
        scenario = Scenario([[101], [101, 103], [101, 103], [101, 103]])
        report, files = scenario.run()
        self.assertTrue(report['ok'])
        self.assertEqual(scenario.downloads, [101, 103])
        self.assertEqual(report['screenshot_time'], '2026-11-05T12:00:03')
        self.assertEqual(report['full_rescans'], 1)
        self.assertEqual(files, [PNG])

    def test_repeated_directory_changes_fail_without_saving_old_image(self):
        scenario = Scenario([[101], [101, 102], [101, 102], [101, 103]])
        report, files = scenario.run()
        self.assertFalse(report['ok'])
        self.assertIn('directory changed', report['error'])
        self.assertEqual(report['scan_attempts'], 2)
        self.assertEqual(files, [])

    def test_other_download_error_is_not_retried(self):
        scenario = Scenario([[101]], download_codes=[0x2005])
        report, files = scenario.run()
        self.assertFalse(report['ok'])
        self.assertEqual(report['full_rescans'], 0)
        self.assertEqual(scenario.downloads, [101])
        self.assertEqual(files, [])

    def test_object_info_raises_structured_error(self):
        class Reader:
            def request(self, *_args, **_kwargs):
                return 0x2009, None, b''
        with self.assertRaises(mtp.InvalidObjectHandleError) as caught:
            mtp._object_info(Reader(), 102)
        self.assertEqual(caught.exception.handle, 102)
        self.assertEqual(caught.exception.operation, 0x1008)


if __name__ == '__main__':
    unittest.main()
