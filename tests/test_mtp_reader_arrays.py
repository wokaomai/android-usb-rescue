"""Array/handle parsing checks without any USB access."""
import struct
import unittest

from android_usb_rescue.mtp_reader import (
    MAX_SCREENSHOT_HANDLES, MAX_SCREENSHOT_OBJECTS, MtpError, OK, _array, _handles,
)


def array_bytes(count):
    return struct.pack('<I', count) + struct.pack('<' + 'I' * count, *range(count))


class ArrayParsingTests(unittest.TestCase):
    def test_invalid_parsing_bounds_rejected(self):
        for offset, width, maximum in [(-1, 4, 10), (0, 3, 10), (0, 4, -1)]:
            with self.subTest(offset=offset, width=width, maximum=maximum):
                with self.assertRaisesRegex(MtpError, 'Invalid MTP array parsing bounds'):
                    _array(array_bytes(1), offset, width, maximum)

    def test_more_than_old_limit_is_complete(self):
        raw = array_bytes(300)
        values, end = _array(raw, 0, maximum=MAX_SCREENSHOT_HANDLES)
        self.assertEqual(values, list(range(300)))
        self.assertEqual(end, len(raw))

    def test_exact_limit_is_complete(self):
        raw = array_bytes(MAX_SCREENSHOT_HANDLES)
        values, end = _array(raw, 0, maximum=MAX_SCREENSHOT_HANDLES)
        self.assertEqual(len(values), MAX_SCREENSHOT_HANDLES)
        self.assertEqual(values[-1], MAX_SCREENSHOT_HANDLES - 1)
        self.assertEqual(end, len(raw))
        self.assertEqual(MAX_SCREENSHOT_OBJECTS, 16384)

    def test_excess_count_reports_actual_and_limit(self):
        raw = array_bytes(MAX_SCREENSHOT_HANDLES + 1)
        with self.assertRaisesRegex(MtpError, r'count 8193 exceeds limit 8192'):
            _array(raw, 0, maximum=MAX_SCREENSHOT_HANDLES)

    def test_truncated_payload_is_not_treated_as_excess(self):
        raw = struct.pack('<III', 3, 10, 20)
        with self.assertRaisesRegex(MtpError, r'Truncated.*count 3.*need 12 bytes, have 8'):
            _array(raw, 0, maximum=MAX_SCREENSHOT_HANDLES)

    def test_truncated_count_rejected(self):
        with self.assertRaisesRegex(MtpError, 'Truncated MTP array count'):
            _array(b'\x01\x00\x00', 0)

    def test_handle_reader_keeps_all_handles(self):
        class Reader:
            def request(self, code, parameters, data):
                self.call = code, parameters, data
                return OK, array_bytes(300), None
        reader = Reader()
        self.assertEqual(_handles(reader, 65537, 42, maximum=MAX_SCREENSHOT_HANDLES),
                         list(range(300)))
        self.assertEqual(reader.call, (0x1007, (65537, 0, 42), True))

    def test_handle_reader_rejects_trailing_data(self):
        class Reader:
            def request(self, *_args, **_kwargs):
                return OK, array_bytes(300) + b'\x00', None
        with self.assertRaisesRegex(MtpError, 'Unexpected trailing object-handle data'):
            _handles(Reader(), 65537, 42, maximum=MAX_SCREENSHOT_HANDLES)


if __name__ == '__main__':
    unittest.main()
