"""Bounded, read-only MTP transactions using an existing USB session.

This module never opens a USB device, changes a driver/configuration, resets a
device, clears a halt, writes phone files, or switches accessory mode. The caller
owns the supplied usb_transport.Session. One reader must own its bulk interface at
a time; use it inside the existing HID server, not in a competing USB process.
"""
import ctypes as c
from datetime import datetime
from io import BytesIO
from pathlib import Path
import struct
import time
import uuid

from PIL import Image


OK = 0x2001
TIMEOUT = -7
MAX_METADATA = 4 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_SCREENSHOT_HANDLES = 8192
MAX_SCREENSHOT_OBJECTS = 16384


class MtpError(RuntimeError):
    pass


class InvalidObjectHandleError(MtpError):
    def __init__(self, operation, handle):
        self.operation = operation
        self.handle = handle
        self.response_code = 0x2009
        name = {0x1007: 'GetObjectHandles', 0x1008: 'GetObjectInfo',
                0x1009: 'GetObject'}.get(operation, f'Operation 0x{operation:04x}')
        super().__init__(f'{name} rejected: 0x2009, handle 0x{handle:08x}')


class ScreenshotDirectoryChangedError(MtpError):
    pass


class _Endpoint(c.Structure):
    _fields_ = [
        ('length', c.c_uint8), ('descriptor_type', c.c_uint8),
        ('address', c.c_uint8), ('attributes', c.c_uint8),
        ('max_packet_size', c.c_uint16), ('interval', c.c_uint8),
        ('refresh', c.c_uint8), ('synch_address', c.c_uint8),
        ('extra', c.c_void_p), ('extra_length', c.c_int),
    ]


class _AltSetting(c.Structure):
    _fields_ = [
        ('length', c.c_uint8), ('descriptor_type', c.c_uint8),
        ('number', c.c_uint8), ('alternate', c.c_uint8),
        ('endpoint_count', c.c_uint8), ('interface_class', c.c_uint8),
        ('subclass', c.c_uint8), ('protocol', c.c_uint8),
        ('string_index', c.c_uint8), ('endpoints', c.POINTER(_Endpoint)),
        ('extra', c.c_void_p), ('extra_length', c.c_int),
    ]


class _Interface(c.Structure):
    _fields_ = [('alternates', c.POINTER(_AltSetting)), ('alternate_count', c.c_int)]


class _Configuration(c.Structure):
    _fields_ = [
        ('length', c.c_uint8), ('descriptor_type', c.c_uint8),
        ('total_length', c.c_uint16), ('interface_count', c.c_uint8),
        ('configuration_value', c.c_uint8), ('string_index', c.c_uint8),
        ('attributes', c.c_uint8), ('max_power', c.c_uint8),
        ('interfaces', c.POINTER(_Interface)), ('extra', c.c_void_p),
        ('extra_length', c.c_int),
    ]


def _array(data, offset, width=4, maximum=65536):
    if (type(offset) is not int or offset < 0 or width not in (2, 4)
            or type(maximum) is not int or maximum < 0):
        raise MtpError('Invalid MTP array parsing bounds')
    if offset + 4 > len(data):
        raise MtpError('Truncated MTP array count')
    count = struct.unpack_from('<I', data, offset)[0]
    offset += 4
    if count > maximum:
        raise MtpError(f'MTP array count {count} exceeds limit {maximum}')
    required = count * width
    available = len(data) - offset
    if required > available:
        raise MtpError(f'Truncated MTP array payload: count {count}, width {width}, '
                       f'need {required} bytes, have {available}')
    values = list(struct.unpack_from('<' + ('I' if width == 4 else 'H') * count,
                                    data, offset)) if count else []
    return values, offset + count * width


def _string(data, offset):
    if type(offset) is not int or offset < 0 or offset >= len(data):
        raise MtpError('Truncated MTP string count')
    units = data[offset]
    offset += 1
    end = offset + 2 * units
    if end > len(data):
        raise MtpError('Truncated MTP string')
    if not units:
        return '', offset
    raw = data[offset:end]
    if raw[-2:] != b'\0\0':
        raise MtpError('MTP string lacks terminator')
    try:
        return raw[:-2].decode('utf-16-le'), end
    except UnicodeError as error:
        raise MtpError('Invalid MTP string encoding') from error


def _device_info(data):
    if len(data) < 9:
        raise MtpError('Truncated device info')
    standard, vendor, version = struct.unpack_from('<HIH', data)
    _, offset = _string(data, 8)
    if offset + 2 > len(data):
        raise MtpError('Truncated device functional mode')
    offset += 2
    operations, offset = _array(data, offset, 2, 4096)
    for _ in range(4):
        _, offset = _array(data, offset, 2, 4096)
    manufacturer, offset = _string(data, offset)
    model, offset = _string(data, offset)
    device_version, offset = _string(data, offset)
    # Do not return the serial number or raw device-info payload.
    _, offset = _string(data, offset)
    return {
        'response_code': '0x2001', 'payload_bytes': len(data),
        'standard_version': standard, 'vendor_extension_id': vendor,
        'vendor_extension_version': version, 'manufacturer': manufacturer,
        'model': model, 'device_version': device_version,
        'supports_get_object': 0x1009 in operations,
    }


class _Reader:
    def __init__(self, session, seconds=20):
        if not getattr(session, 'handle', None):
            raise MtpError('An already-open USB session is required')
        self.session = session
        self.u = session.u
        self.handle = session.handle
        self.deadline = time.monotonic() + seconds
        self.interface = None
        self.claimed = False
        self.opened = False
        self.healthy = True
        self.transaction = 0
        self.buffer = bytearray()
        self.report = {'ok': False, 'stage': 'descriptor', 'cleanup': {}}
        pointer = c.c_void_p
        for name, arguments, result in [
            ('libusb_get_device', [pointer], pointer),
            ('libusb_get_active_config_descriptor',
             [pointer, c.POINTER(c.POINTER(_Configuration))], c.c_int),
            ('libusb_free_config_descriptor', [c.POINTER(_Configuration)], None),
            ('libusb_claim_interface', [pointer, c.c_int], c.c_int),
            ('libusb_release_interface', [pointer, c.c_int], c.c_int),
            ('libusb_bulk_transfer',
             [pointer, c.c_ubyte, c.POINTER(c.c_ubyte), c.c_int,
              c.POINTER(c.c_int), c.c_uint], c.c_int),
        ]:
            function = getattr(self.u, name)
            function.argtypes, function.restype = arguments, result

    def _remaining(self, maximum=2000):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            self.healthy = False
            raise MtpError('MTP operation deadline exceeded')
        return max(1, min(maximum, remaining))

    def connect(self):
        configuration = c.POINTER(_Configuration)()
        device = self.u.libusb_get_device(self.handle)
        result = self.u.libusb_get_active_config_descriptor(device, c.byref(configuration))
        if result < 0 or not configuration:
            raise MtpError(f'Cannot read active USB configuration: {result}')
        candidates = []
        try:
            config = configuration.contents
            if config.interface_count > 32:
                raise MtpError('Unexpected USB interface count')
            for index in range(config.interface_count):
                interface = config.interfaces[index]
                if not 0 < interface.alternate_count <= 32:
                    raise MtpError('Unexpected USB alternate-setting count')
                for alternate in range(interface.alternate_count):
                    setting = interface.alternates[alternate]
                    if (setting.interface_class, setting.subclass, setting.protocol) != (6, 1, 1):
                        continue
                    if setting.alternate != 0 or setting.endpoint_count > 16:
                        continue
                    bulk_in, bulk_out = [], []
                    for endpoint_index in range(setting.endpoint_count):
                        endpoint = setting.endpoints[endpoint_index]
                        if endpoint.attributes & 3 == 2:
                            (bulk_in if endpoint.address & 0x80 else bulk_out).append(endpoint.address)
                    if len(bulk_in) == len(bulk_out) == 1:
                        candidates.append((setting.number, bulk_in[0], bulk_out[0]))
        finally:
            self.u.libusb_free_config_descriptor(configuration)
        if len(candidates) != 1:
            raise MtpError(f'Expected one unambiguous MTP interface; found {len(candidates)}')
        self.interface, self.endpoint_in, self.endpoint_out = candidates[0]
        self.report.update(interface=self.interface,
                           bulk_in=f'0x{self.endpoint_in:02x}',
                           bulk_out=f'0x{self.endpoint_out:02x}')
        self.report['stage'] = 'claim_interface'
        result = self.u.libusb_claim_interface(self.handle, self.interface)
        if result < 0:
            raise MtpError(f'Cannot claim MTP interface: {result}')
        self.claimed = True
        self.report['stage'] = 'drain_pending'
        self._drain_pending()

    def _read_chunk(self, timeout=None):
        # Packet-sized buffers avoid overflow from reading only the 12-byte header.
        buffer = (c.c_ubyte * 65536)()
        count = c.c_int()
        result = self.u.libusb_bulk_transfer(
            self.handle, self.endpoint_in, buffer, len(buffer), c.byref(count),
            self._remaining() if timeout is None else min(timeout, self._remaining()))
        if not 0 <= count.value <= len(buffer):
            self.healthy = False
            raise MtpError('Invalid USB read byte count')
        if result not in (0, TIMEOUT):
            self.healthy = False
            raise MtpError(f'MTP bulk read failed: {result}, bytes={count.value}')
        return bytes(buffer[:count.value]), result

    def _container(self, limit, deadline=None):
        while True:
            if len(self.buffer) >= 12:
                length, kind, code, transaction = struct.unpack_from('<IHHI', self.buffer)
                if length < 12 or length > limit + 12 or kind not in (2, 3, 4):
                    self.healthy = False
                    raise MtpError('Invalid, unexpected, or oversized MTP container')
                if len(self.buffer) >= length:
                    body = bytes(self.buffer[12:length])
                    del self.buffer[:length]
                    return kind, code, transaction, body
            if deadline is not None and time.monotonic() >= deadline:
                self.healthy = False
                raise MtpError('Incomplete pending MTP container')
            chunk, result = self._read_chunk()
            self.buffer.extend(chunk)
            if result == TIMEOUT and not chunk:
                self.healthy = False
                raise MtpError('MTP read timed out with no bytes')

    def _drain_pending(self):
        # A previous one-shot probe may have left its final response unread.
        count = 0
        total = 0
        deadline = time.monotonic() + 2
        while count < 4 and total <= 131072:
            if not self.buffer:
                chunk, result = self._read_chunk(100)
                if not chunk:
                    self.report['drained_containers'] = count
                    return
                self.buffer.extend(chunk)
            _, _, _, body = self._container(131072, deadline)
            count += 1
            total += len(body) + 12
        self.healthy = False
        raise MtpError('Excessive pending MTP traffic; stopped without resetting')

    def request(self, operation, parameters=(), *, data=False, limit=MAX_METADATA, transaction=None):
        if not self.healthy:
            raise MtpError('MTP transport is no longer synchronized')
        if transaction is None:
            self.transaction += 1
            transaction = self.transaction
        wire = struct.pack('<IHHI', 12 + 4 * len(parameters), 1, operation, transaction)
        wire += b''.join(struct.pack('<I', value) for value in parameters)
        buffer = (c.c_ubyte * len(wire)).from_buffer_copy(wire)
        written = c.c_int()
        result = self.u.libusb_bulk_transfer(
            self.handle, self.endpoint_out, buffer, len(wire), c.byref(written), self._remaining())
        if result < 0 or written.value != len(wire):
            self.healthy = False
            raise MtpError(f'MTP bulk write failed: {result}, bytes={written.value}/{len(wire)}')
        payload = None
        stale = 0
        while True:
            kind, code, incoming_transaction, body = self._container(limit)
            if incoming_transaction != transaction or kind == 4:
                stale += 1
                if stale > 4:
                    self.healthy = False
                    raise MtpError('Too many nonmatching MTP responses')
                continue
            if kind == 2:
                if code != operation or payload is not None or not data:
                    self.healthy = False
                    raise MtpError('Unexpected MTP data phase')
                payload = body
                continue
            # Always consume this final response, even when the request failed.
            if code == OK and data and payload is None:
                raise MtpError('MTP success response lacked expected data')
            return code, payload, body

    def start_session(self):
        self.report['stage'] = 'open_session'
        code, _, _ = self.request(0x1002, (1,), transaction=0)
        self.report['open_session_response'] = f'0x{code:04x}'
        if code not in (OK, 0x201E):
            raise MtpError(f'OpenSession rejected: 0x{code:04x}')
        # No other process can own this WinUSB interface; 0x201E is a stale session.
        self.opened = True

    def close(self):
        cleanup = self.report['cleanup']
        if self.opened and self.healthy:
            previous_deadline = self.deadline
            self.deadline = time.monotonic() + 2
            try:
                code, _, _ = self.request(0x1003)
                cleanup['close_session_response'] = f'0x{code:04x}'
            except Exception as error:
                cleanup['close_session_error'] = str(error)
            finally:
                self.deadline = previous_deadline
        elif self.opened:
            cleanup['close_session'] = 'skipped_after_transport_failure'
        if self.claimed:
            cleanup['release_interface_status'] = self.u.libusb_release_interface(
                self.handle, self.interface)
            self.claimed = False


def probe(session):
    """Return JSON-safe transport/device-info/storage status; no raw file content."""
    reader = _Reader(session)
    try:
        reader.connect()
        reader.report['stage'] = 'device_info'
        code, payload, _ = reader.request(0x1001, data=True, transaction=0)
        reader.report['device_info_response'] = f'0x{code:04x}'
        if code != OK:
            raise MtpError(f'GetDeviceInfo rejected: 0x{code:04x}')
        reader.report['device_info'] = _device_info(payload)
        reader.start_session()
        reader.report['stage'] = 'storage_ids'
        code, payload, _ = reader.request(0x1004, data=True)
        reader.report['storage_response'] = f'0x{code:04x}'
        if code != OK:
            raise MtpError(f'GetStorageIDs rejected: 0x{code:04x}')
        storages, end = _array(payload, 0, maximum=32)
        if end != len(payload):
            raise MtpError('Unexpected trailing storage data')
        reader.report.update(ok=True, stage='complete', storage_ids=storages,
                             storage_accessible=bool(storages))
        if not storages:
            reader.report['note'] = 'MTP responds but exposes no storage; phone-side USB data access may be disabled.'
    except Exception as error:
        reader.report['error'] = str(error)
    finally:
        reader.close()
    return reader.report


def _handles(reader, storage, parent, image_format=0, maximum=256):
    code, payload, _ = reader.request(0x1007, (storage, image_format, parent), data=True)
    if code == 0x2009:
        raise InvalidObjectHandleError(0x1007, parent)
    if code != OK:
        raise MtpError(f'GetObjectHandles rejected: 0x{code:04x}')
    handles, end = _array(payload, 0, maximum=maximum)
    if end != len(payload):
        raise MtpError('Unexpected trailing object-handle data')
    return handles


def _object_info(reader, handle):
    code, payload, _ = reader.request(0x1008, (handle,), data=True, limit=65536)
    if code == 0x2009:
        raise InvalidObjectHandleError(0x1008, handle)
    if code != OK:
        raise MtpError(f'GetObjectInfo rejected: 0x{code:04x}')
    if len(payload) < 53:
        raise MtpError('Truncated object info')
    name, offset = _string(payload, 52)
    created, offset = _string(payload, offset)
    modified, offset = _string(payload, offset)
    _, offset = _string(payload, offset)
    return {
        'handle': handle, 'storage': struct.unpack_from('<I', payload, 0)[0],
        'format': struct.unpack_from('<H', payload, 4)[0],
        'size': struct.unpack_from('<I', payload, 8)[0],
        'parent': struct.unpack_from('<I', payload, 38)[0],
        'name': name, 'created': created, 'modified': modified,
    }


def _timestamp(info):
    values = []
    for field in ('modified', 'created'):
        value = info[field]
        try:
            # Android's MTP timestamps use phone-local YYYYMMDDThhmmss.
            # Compare timestamps from this same device, without timezone guessing.
            if len(value) >= 15:
                values.append(datetime.strptime(value[:15], '%Y%m%dT%H%M%S'))
        except ValueError:
            pass
    return max(values) if values else None


def _image_extension(info):
    extension = Path(info['name']).suffix.lower()
    return extension if extension in ('.png', '.jpg', '.jpeg', '.webp') else None


def _valid_image(payload, extension):
    expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}.get(extension)
    if (expected is None or not isinstance(payload, (bytes, bytearray))
            or not 0 < len(payload) <= MAX_IMAGE):
        return False
    try:
        with Image.open(BytesIO(payload)) as image:
            if (image.format != expected or min(image.size) <= 0
                    or image.width * image.height > MAX_IMAGE_PIXELS):
                return False
            image.verify()
        # verify() checks container integrity; loading also checks pixel decoding.
        # Reopen because verify() invalidates the decoder's underlying stream.
        with Image.open(BytesIO(payload)) as image:
            image.load()
        return True
    except (OSError, ValueError, SyntaxError, struct.error, Image.DecompressionBombError):
        return False


def _find_screenshot_directories(reader, storages):
    reader.report['stage'] = 'find_screenshot_directories'
    directories = []
    for storage in storages:
        for root_handle in _handles(reader, storage, 0xFFFFFFFF, 0x3001, 128):
            root = _object_info(reader, root_handle)
            if root['storage'] != storage or root['format'] != 0x3001:
                raise MtpError('Unexpected root directory metadata')
            if root['name'].casefold() not in ('dcim', 'pictures'):
                continue
            for child_handle in _handles(reader, storage, root_handle, 0x3001, 128):
                child = _object_info(reader, child_handle)
                if child['storage'] != storage or child['parent'] != root_handle:
                    raise MtpError('Directory changed during narrow traversal')
                if child['format'] == 0x3001 and child['name'].casefold() == 'screenshots':
                    directories.append((storage, child_handle))
    reader.report['screenshot_directories'] = len(directories)
    if not directories:
        raise MtpError('No DCIM/Screenshots or Pictures/Screenshots directory is exposed')

    return directories


def _scan_screenshot_candidates(reader, directories):
    reader.report['stage'] = 'find_newest_screenshot'
    newest = None
    newest_time = None
    image_count = 0
    object_count = 0
    snapshot = {}
    for storage, directory in directories:
        handles = _handles(reader, storage, directory, maximum=MAX_SCREENSHOT_HANDLES)
        snapshot[(storage, directory)] = frozenset(handles)
        object_count += len(handles)
        reader.report['screenshot_object_count'] = object_count
        if object_count > MAX_SCREENSHOT_OBJECTS:
            raise MtpError(f'Screenshot-folder object count {object_count} exceeds '
                           f'limit {MAX_SCREENSHOT_OBJECTS}; bounded scan stopped')
        for handle in handles:
            info = _object_info(reader, handle)
            if info['storage'] != storage or info['parent'] != directory:
                raise MtpError('Screenshot object moved during traversal')
            if not _image_extension(info):
                continue
            image_count += 1
            timestamp = _timestamp(info)
            if timestamp is None:
                raise MtpError('An image lacks a valid date; cannot safely identify the newest')
            if newest is None or (timestamp, handle) > (newest_time, newest['handle']):
                newest, newest_time = info, timestamp
    reader.report['images_examined'] = image_count
    if newest is None:
        raise MtpError('No PNG, JPEG, or WebP image found in the screenshot directories')
    if not 0 < newest['size'] <= MAX_IMAGE:
        raise MtpError('Newest screenshot has an invalid size or exceeds the 32 MiB limit')

    return newest, newest_time, snapshot


def latest_screenshot(session, output_dir):
    """Copy one newest dated image from DCIM/Screenshots or Pictures/Screenshots.

    Traversal is limited to those exact paths (case insensitive), at most 32
    storages, 128 folders at each level, 8192 objects per Screenshots folder,
    and 16384 objects across those folders. The total deadline remains 45 seconds.
    If a limit prevents finding the actual newest image, fail instead of silently
    choosing an older image. One full rescan handles changed/deleted objects,
    within the same deadline; incomplete scans never produce a screenshot.
    No other phone files are downloaded or returned.
    """
    reader = _Reader(session, seconds=45)
    destination = None
    try:
        reader.connect()
        reader.start_session()
        reader.report['stage'] = 'storage_ids'
        code, payload, _ = reader.request(0x1004, data=True)
        reader.report['storage_response'] = f'0x{code:04x}'
        if code != OK:
            raise MtpError(f'GetStorageIDs rejected: 0x{code:04x}')
        storages, end = _array(payload, 0, maximum=32)
        if end != len(payload):
            raise MtpError('Unexpected trailing storage data')
        reader.report['storage_ids'] = storages
        if not storages:
            raise MtpError('MTP exposes no storage; cannot retrieve screenshots')

        # A newly created or removed image can invalidate an enumerated handle.
        # Restart the entire bounded scan once; never skip a missing candidate.
        reader.report['full_rescans'] = 0
        for attempt in range(2):
            reader.report['scan_attempts'] = attempt + 1
            try:
                directories = _find_screenshot_directories(reader, storages)
                newest, newest_time, snapshot = _scan_screenshot_candidates(reader, directories)
                reader.report['stage'] = 'download_screenshot'
                code, payload, _ = reader.request(0x1009, (newest['handle'],),
                                                  data=True, limit=MAX_IMAGE)
                if code == 0x2009:
                    raise InvalidObjectHandleError(0x1009, newest['handle'])
                if code != OK:
                    raise MtpError(f'GetObject rejected: 0x{code:04x}')
                if len(payload) != newest['size']:
                    raise MtpError('Screenshot size changed during read; no output file written')
                extension = _image_extension(newest)
                if not _valid_image(payload, extension):
                    raise MtpError('Screenshot data has an unexpected image signature')
                reader.report['stage'] = 'verify_screenshot_directory'
                for storage, directory in directories:
                    current = frozenset(_handles(reader, storage, directory,
                                                 maximum=MAX_SCREENSHOT_HANDLES))
                    previous = snapshot[(storage, directory)]
                    if current != previous:
                        raise ScreenshotDirectoryChangedError(
                            f'Screenshot directory changed during read: '
                            f'{len(previous)} to {len(current)} objects; no image saved')
                break
            except (InvalidObjectHandleError, ScreenshotDirectoryChangedError) as error:
                reader.report['refresh_reason'] = str(error)
                if isinstance(error, InvalidObjectHandleError):
                    reader.report['invalid_object_handle'] = error.handle
                    reader.report['invalid_object_operation'] = f'0x{error.operation:04x}'
                if attempt or not reader.healthy:
                    reader.report['refresh_exhausted'] = bool(attempt)
                    raise
                reader.report['full_rescans'] = 1
                # Wait briefly for MediaStore registration, inside the same deadline.
                time.sleep(min(0.25, max(0.0, reader.deadline - time.monotonic())))

        reader.report['stage'] = 'save_screenshot'
        directory = Path(output_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        filename = ('android-screenshot-' + newest_time.strftime('%Y%m%d-%H%M%S') +
                    '-' + uuid.uuid4().hex[:8] + extension)
        destination = directory / filename
        # Fixed local filename, no phone-supplied path, and no overwrite.
        with destination.open('xb') as stream:
            stream.write(payload)
        reader.report.update(ok=True, stage='complete', screenshot_path=str(destination),
                             screenshot_bytes=len(payload),
                             screenshot_object_handle=newest['handle'],
                             screenshot_time=newest_time.isoformat())
    except Exception as error:
        reader.report['error'] = str(error)
    finally:
        reader.close()
    return reader.report
