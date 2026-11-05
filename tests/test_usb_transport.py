"""USB transport checks use fake native functions; no device is opened."""
import ctypes as c
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from android_usb_rescue import usb_transport as usb


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeLib:
    def __init__(self, serials, protocol=2):
        self.serials = serials
        self.protocol = protocol
        self.opened = []
        self.closed = []
        self.controls = []
        self.freed = 0
        self.exited = 0
        self.array = (c.c_void_p * (len(serials) + 1))(
            *range(100, 100 + len(serials)), None)
        for name in ('init', 'get_device_list', 'free_device_list',
                     'get_device_descriptor', 'open', 'close', 'exit',
                     'get_string_descriptor_ascii', 'control_transfer'):
            setattr(self, 'libusb_' + name, Function(getattr(self, name)))

    def init(self, result):
        result._obj.value = 900
        return 0

    def get_device_list(self, context, result):
        c.cast(result, c.POINTER(c.POINTER(c.c_void_p)))[0] = c.cast(
            self.array, c.POINTER(c.c_void_p))
        return len(self.serials)

    def free_device_list(self, devices, unref):
        self.freed += 1

    def get_device_descriptor(self, device, result):
        descriptor = result._obj
        descriptor.vendor = 0x2717
        descriptor.product = 0xff40
        descriptor.serial_index = 1
        return 0

    def open(self, device, result):
        self.opened.append(device)
        result._obj.value = device + 1000
        return 0

    def close(self, handle):
        self.closed.append(handle.value)

    def exit(self, context):
        self.exited += 1

    def get_string_descriptor_ascii(self, handle, index, buffer, size):
        serial = self.serials[handle.value - 1100]
        buffer[:len(serial)] = serial
        return len(serial)

    def control_transfer(self, handle, kind, request, value, index, buffer,
                         length, timeout):
        self.controls.append((kind, request, value, index, length))
        if request == 51:
            buffer[:2] = self.protocol.to_bytes(2, 'little')
            return 2
        return length


class TransportTests(unittest.TestCase):
    def create(self, serials, serial='TARGET', protocol=2):
        library = FakeLib(serials, protocol)
        with patch.object(usb, '_library_path', return_value='test-library'), \
                patch.object(usb.c, 'CDLL', return_value=library):
            session = usb.Session(serial)
        self.addCleanup(session.close)
        return session, library

    def test_invalid_target_never_loads_library(self):
        for serial in ('', ' ', ' TARGET', 'é', 'A\0B'):
            with self.subTest(serial=repr(serial)), \
                    patch.object(usb, '_library_path') as locate, \
                    patch.object(usb.c, 'CDLL') as load:
                with self.assertRaises(ValueError):
                    usb.Session(serial)
                locate.assert_not_called()
                load.assert_not_called()
        with patch.object(usb, '_library_path') as locate:
            with self.assertRaises(ValueError):
                usb.Session('TARGET', vendor_id=-1)
            locate.assert_not_called()

    def test_multiple_same_usb_ids_match_exact_serial(self):
        session, library = self.create([b'OTHER', b'TARGET'])
        self.assertEqual(session.handle.value, 1101)
        self.assertEqual(library.closed, [1100])
        self.assertEqual([x[1] for x in library.controls], [51])

    def test_no_serial_match_sends_no_aoa_requests(self):
        library = FakeLib([b'OTHER', b'target'])
        with patch.object(usb, '_library_path', return_value='test-library'), \
                patch.object(usb.c, 'CDLL', return_value=library):
            with self.assertRaisesRegex(RuntimeError, 'No accessible device'):
                usb.Session('TARGET')
        self.assertEqual(library.controls, [])
        self.assertCountEqual(library.closed, [1100, 1101])
        self.assertEqual((library.freed, library.exited), (1, 1))

    def test_duplicate_serial_is_rejected_before_hid(self):
        library = FakeLib([b'TARGET', b'TARGET'])
        with patch.object(usb, '_library_path', return_value='test-library'), \
                patch.object(usb.c, 'CDLL', return_value=library):
            with self.assertRaisesRegex(RuntimeError, 'More than one'):
                usb.Session('TARGET')
        self.assertEqual(library.controls, [])
        self.assertCountEqual(library.closed, [1100, 1101])
        self.assertEqual((library.freed, library.exited), (1, 1))

    def test_unsupported_aoa_closes_native_resources(self):
        library = FakeLib([b'TARGET'], protocol=1)
        with patch.object(usb, '_library_path', return_value='test-library'), \
                patch.object(usb.c, 'CDLL', return_value=library):
            with self.assertRaisesRegex(RuntimeError, 'AOA 2 not confirmed'):
                usb.Session('TARGET')
        self.assertEqual(library.closed, [1100])
        self.assertEqual((library.freed, library.exited), (1, 1))

    def test_invalid_input_and_accessory_switch_are_not_sent(self):
        session, library = self.create([b'TARGET'])
        with patch.object(usb.time, 'sleep'):
            session.register()
            session.register_touch()
        before = len(library.controls)
        for bad in (-0.1, 1.1, math.nan, math.inf, True, '0.5'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                session.contact(True, bad, 0.5)
        for usage, modifier in [(-1, 0), (256, 0), (1, 256), (True, 0)]:
            with self.assertRaises(ValueError):
                session.key(usage, modifier)
        with self.assertRaises(ValueError):
            session.consumer_key(233.0)
        with self.assertRaises(ValueError):
            session.send(52)
        self.assertEqual(len(library.controls), before)

    def test_close_is_idempotent_and_releases_hid_first(self):
        session, library = self.create([b'TARGET'])
        with patch.object(usb.time, 'sleep'):
            session.register()
            session.register_touch()
            session.consumer_key(0x223)
        session.close()
        count = len(library.controls)
        session.close()
        self.assertEqual(len(library.controls), count)
        self.assertEqual(sum(row[1] == 55 for row in library.controls), 3)
        self.assertEqual(library.closed, [1100])
        self.assertEqual((library.freed, library.exited), (1, 1))
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            session.send(57)

    def test_dll_directory_is_closed_on_load_failure(self):
        directory = SimpleNamespace(close=unittest.mock.Mock())
        with patch.object(usb.sys, 'platform', 'win32'), \
                patch.object(usb, '_library_path', return_value=str(Path.cwd() / 'fake.dll')), \
                patch.object(usb.os, 'add_dll_directory', return_value=directory, create=True), \
                patch.object(usb.c, 'CDLL', side_effect=OSError('missing')):
            with self.assertRaises(OSError):
                usb.Session('TARGET')
        directory.close.assert_called_once()

    def test_packaged_library_api_is_preferred(self):
        package = SimpleNamespace(get_library_path=lambda: Path('/test/libusb.so'))
        with patch.object(usb.importlib, 'import_module', return_value=package), \
                patch.object(usb.ctypes.util, 'find_library') as fallback:
            self.assertEqual(usb._library_path(), str(Path('/test/libusb.so')))
            fallback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
