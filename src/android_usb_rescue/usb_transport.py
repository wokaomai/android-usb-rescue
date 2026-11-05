"""Explicit-target AOA keyboard and touch transport using native libusb.

No driver replacement, accessory switch, credential input, or USB reset.
The caller supplies a serial number and USB IDs. Windows requires a compatible
WinUSB driver already installed by the owner. This module never selects UsbDk.
"""
import ctypes as c
import ctypes.util
import importlib
import math
import os
from pathlib import Path
import sys
import time

TIMEOUT = 1500


def _library_path(explicit=None):
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError('The configured libusb library does not exist')
        return str(path)
    try:
        package = importlib.import_module('libusb_package')
        bundled = package.get_library_path()
    except ImportError:
        bundled = None
    if bundled:
        return str(bundled)
    library = ctypes.util.find_library('usb-1.0')
    if not library:
        raise RuntimeError('libusb was not found; install libusb-package or set its library path')
    return library


class Descriptor(c.Structure):
    _fields_ = [('length', c.c_uint8), ('type', c.c_uint8),
                ('usb', c.c_uint16), ('device_class', c.c_uint8),
                ('subclass', c.c_uint8), ('protocol', c.c_uint8),
                ('packet', c.c_uint8), ('vendor', c.c_uint16),
                ('product', c.c_uint16), ('version', c.c_uint16),
                ('manufacturer_index', c.c_uint8), ('product_index', c.c_uint8),
                ('serial_index', c.c_uint8), ('configurations', c.c_uint8)]

KEYBOARD = bytes.fromhex(
    '05 01 09 06 a1 01 05 07 19 e0 29 e7 15 00 25 01 '
    '75 01 95 08 81 02 95 01 75 08 81 01 95 06 75 08 '
    '15 00 26 ff 00 05 07 19 00 29 ff 81 00 c0'
)
TOUCH = bytes.fromhex(
    '05 0d 09 04 a1 01 05 0d 09 22 a1 02 05 0d 09 42 15 00 25 01 '
    '75 01 95 01 81 02 75 07 95 01 81 01 05 0d 09 51 15 00 25 0f '
    '75 04 95 01 81 02 05 01 09 30 15 00 26 ff 7f 75 10 95 01 81 02 '
    '05 01 09 31 15 00 26 ff 7f 75 10 95 01 81 02 75 04 95 01 81 01 '
    'c0 05 0d 09 54 15 00 25 01 75 01 95 01 81 02 75 07 95 01 81 01 c0'
)
CONSUMER = bytes.fromhex(
    '05 0c 09 01 a1 01 15 00 26 ff 03 19 00 2a ff 03 '
    '75 10 95 01 81 00 c0'
)


class Session:
    def __init__(self, serial: str, vendor_id: int = 0x2717,
                 product_id: int = 0xff40, libusb_path: str | None = None):
        if not isinstance(serial, str) or not serial or serial != serial.strip():
            raise ValueError('An exact, nonempty USB serial number is required')
        try:
            target_serial = serial.encode('ascii')
        except UnicodeError as error:
            raise ValueError('USB serial number must contain ASCII characters') from error
        if len(target_serial) > 255 or b'\0' in target_serial:
            raise ValueError('USB serial number is invalid')
        for value in (vendor_id, product_id):
            if type(value) is not int or not 0 <= value <= 0xffff:
                raise ValueError('USB vendor and product IDs must be 16-bit integers')
        self.u = None
        self.dll_directory = None
        p = c.c_void_p
        self.ctx, self.handle = p(), p()
        self.devices = c.POINTER(p)()
        self._closed = False
        self.cleanup_errors = []
        self.registered = False
        self.touch_registered = False
        self.consumer_registered = False
        self.hid_id = 0x4b60
        try:
            library = _library_path(libusb_path)
            if sys.platform == 'win32' and Path(library).is_absolute():
                self.dll_directory = os.add_dll_directory(str(Path(library).parent))
            self.u = c.CDLL(library)
            for name, args, result in [
                ('libusb_init', [c.POINTER(p)], c.c_int),
                ('libusb_get_device_list', [p, c.POINTER(c.POINTER(p))], c.c_ssize_t),
                ('libusb_free_device_list', [c.POINTER(p), c.c_int], None),
                ('libusb_get_device_descriptor', [p, c.POINTER(Descriptor)], c.c_int),
                ('libusb_open', [p, c.POINTER(p)], c.c_int),
                ('libusb_close', [p], None),
                ('libusb_exit', [p], None),
                ('libusb_get_string_descriptor_ascii', [p, c.c_uint8, c.POINTER(c.c_ubyte), c.c_int], c.c_int),
                ('libusb_control_transfer', [p, c.c_uint8, c.c_uint8, c.c_uint16, c.c_uint16, c.POINTER(c.c_ubyte), c.c_uint16, c.c_uint], c.c_int),
            ]:
                f = getattr(self.u, name)
                f.argtypes, f.restype = args, result
            # Use the platform's default backend; never capture through UsbDk.
            self.check(self.u.libusb_init(c.byref(self.ctx)), 'initialize native USB backend')
            n = self.u.libusb_get_device_list(self.ctx, c.byref(self.devices))
            self.check(n, 'enumerate')
            candidate_count = 0
            for i in range(n):
                d = Descriptor()
                if self.u.libusb_get_device_descriptor(self.devices[i], c.byref(d)) == 0:
                    if (d.vendor, d.product) != (vendor_id, product_id):
                        continue
                    candidate_count += 1
                    candidate = p()
                    if self.u.libusb_open(self.devices[i], c.byref(candidate)) < 0:
                        continue
                    try:
                        if not candidate or not d.serial_index:
                            continue
                        buf = (c.c_ubyte * 256)()
                        length = self.u.libusb_get_string_descriptor_ascii(
                            candidate, d.serial_index, buf, len(buf))
                        if length < 0 or bytes(buf[:length]) != target_serial:
                            continue
                        if self.handle:
                            raise RuntimeError('More than one accessible device has the target serial')
                        self.handle, candidate = candidate, p()
                    finally:
                        if candidate:
                            self.u.libusb_close(candidate)
            if not self.handle:
                raise RuntimeError(f'No accessible device matched the configured USB IDs and serial ({candidate_count} USB candidates)')
            buf = (c.c_ubyte * 2)()
            n = self.u.libusb_control_transfer(self.handle, 0xc0, 51, 0, 0, buf, 2, TIMEOUT)
            if n != 2 or int.from_bytes(bytes(buf), 'little') < 2:
                raise RuntimeError(f'AOA 2 not confirmed, response={n}')
        except Exception:
            self.close()
            raise

    @staticmethod
    def check(status, name):
        if status < 0:
            raise RuntimeError(f'{name}: libusb {status}')

    def send(self, request, index=0, data=b'', hid_id=None):
        if type(request) is not int or request not in {54, 55, 56, 57}:
            raise ValueError('Only AOA HID requests are supported')
        if (type(index) is not int or not 0 <= index <= 0xffff
                or hid_id is not None and (type(hid_id) is not int or not 0 <= hid_id <= 0xffff)
                or not isinstance(data, (bytes, bytearray)) or len(data) > 0xffff):
            raise ValueError('Invalid AOA HID report parameters')
        if self._closed or not self.handle:
            raise RuntimeError('USB session is closed')
        buf = (c.c_ubyte * len(data)).from_buffer_copy(data) if data else None
        n = self.u.libusb_control_transfer(self.handle, 0x40, request, self.hid_id if hid_id is None else hid_id, index, buf, len(data), TIMEOUT)
        if n != len(data):
            raise RuntimeError(f'AOA request {request}: {n}, expected {len(data)}')

    def register(self):
        if self.registered:
            return
        self.send(54, len(KEYBOARD))
        self.registered = True
        self.send(56, 0, KEYBOARD)
        time.sleep(0.35)
        self.send(57, data=bytes(8))

    def register_touch(self):
        if self.touch_registered:
            return
        self.send(54, len(TOUCH), hid_id=self.hid_id + 1)
        self.touch_registered = True
        self.send(56, data=TOUCH, hid_id=self.hid_id + 1)
        time.sleep(0.35)
        self.send(57, data=bytes(7), hid_id=self.hid_id + 1)

    def consumer_key(self, usage):
        if type(usage) is not int or usage not in {0x223, 0x221, 0x196, 0xe9}:
            raise ValueError('Only Home, Search, Browser and Volume Up are allowed')
        consumer_id = self.hid_id + 2
        if not self.consumer_registered:
            self.send(54, len(CONSUMER), hid_id=consumer_id)
            self.consumer_registered = True
            self.send(56, 0, CONSUMER, hid_id=consumer_id)
            time.sleep(0.35)
        try:
            self.send(57, data=usage.to_bytes(2, 'little'), hid_id=consumer_id)
            time.sleep(0.08)
        finally:
            self.send(57, data=bytes(2), hid_id=consumer_id)
        time.sleep(0.25)

    def contact(self, down, x, y):
        if type(down) is not bool:
            raise ValueError('Touch state must be a boolean')
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 <= value <= 1 for value in (x, y)):
            raise ValueError('Touch coordinate outside screen')
        if not self.touch_registered:
            raise RuntimeError('Touch device has not been registered')
        value = int(down) | (1 << 8) | (round(x * 32767) << 12) | (round(y * 32767) << 28) | (1 << 48)
        self.send(57, data=value.to_bytes(7, 'little'), hid_id=self.hid_id + 1)

    def swipe(self, x1, y1, x2, y2):
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 <= value <= 1 for value in (x1, y1, x2, y2)):
            raise ValueError('Touch coordinate outside screen')
        self.register_touch()
        self.contact(True, x1, y1)
        try:
            for i in range(1, 21):
                self.contact(True, x1 + (x2 - x1) * i / 20, y1 + (y2 - y1) * i / 20)
                time.sleep(0.025)
        finally:
            self.contact(False, x2, y2)
            self.send(57, data=bytes(7), hid_id=self.hid_id + 1)
        time.sleep(0.7)

    def key(self, usage=0, modifiers=0):
        if any(type(value) is not int or not 0 <= value <= 255
               for value in (usage, modifiers)):
            raise ValueError('Keyboard usage and modifiers must be byte values')
        if not self.registered:
            raise RuntimeError('Keyboard device has not been registered')
        self.send(57, data=bytes([modifiers, 0, usage, 0, 0, 0, 0, 0]))
        time.sleep(0.08)
        self.send(57, data=bytes(8))
        time.sleep(0.2)

    def close(self):
        if self._closed:
            return
        if self.handle:
            if self.consumer_registered:
                for request, data in [(57, bytes(2)), (55, b'')]:
                    try:
                        self.send(request, data=data, hid_id=self.hid_id + 2)
                    except RuntimeError:
                        self.cleanup_errors.append('consumer_release_failed')
            if self.touch_registered:
                for request, data in [(57, bytes(7)), (55, b'')]:
                    try:
                        self.send(request, data=data, hid_id=self.hid_id + 1)
                    except RuntimeError:
                        self.cleanup_errors.append('touch_release_failed')
            if self.registered:
                for request, data in [(57, bytes(8)), (55, b'')]:
                    try:
                        self.send(request, data=data)
                    except RuntimeError:
                        self.cleanup_errors.append('keyboard_release_failed')
            try:
                self.u.libusb_close(self.handle)
            except Exception:
                self.cleanup_errors.append('close_handle_failed')
            self.handle = c.c_void_p()
        if self.devices:
            try:
                self.u.libusb_free_device_list(self.devices, 1)
            except Exception:
                self.cleanup_errors.append('free_device_list_failed')
            self.devices = c.POINTER(c.c_void_p)()
        if self.ctx:
            try:
                self.u.libusb_exit(self.ctx)
            except Exception:
                self.cleanup_errors.append('exit_context_failed')
            self.ctx = c.c_void_p()
        if self.dll_directory is not None:
            try:
                self.dll_directory.close()
            except OSError:
                self.cleanup_errors.append('close_dll_directory_failed')
            self.dll_directory = None
        self._closed = True
