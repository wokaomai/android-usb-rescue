"""Application settings and runtime files stay outside the source checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid


DEFAULTS = {
    'serial': '',
    'vendor_id': '2717',
    'product_id': 'ff40',
    'libusb_path': '',
    'reference': '',
    'view': 'fit',
    'extra_manifests': [],
}


def get_data_dir(override=None):
    if override is not None:
        return Path(override).expanduser().resolve()
    custom = os.environ.get('ANDROID_USB_RESCUE_HOME')
    if custom:
        return Path(custom).expanduser().resolve()
    if sys.platform == 'win32':
        parent = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        return (parent / 'AndroidUsbRescue').resolve()
    parent = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    return (parent / 'android-usb-rescue').resolve()


def parse_usb_id(value):
    """IDs use conventional hexadecimal notation, with an optional 0x prefix."""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.strip():
        try:
            number = int(value.strip(), 16)
        except ValueError as error:
            raise ValueError('USB ID must be a hexadecimal number') from error
    else:
        raise ValueError('USB ID must be a hexadecimal number')
    if not 0 <= number <= 0xffff:
        raise ValueError('USB ID must be between 0000 and FFFF')
    return number


def _normalized(values):
    config = {**DEFAULTS, 'extra_manifests': []}
    if not isinstance(values, dict):
        return config
    for key in ('serial', 'libusb_path', 'reference'):
        value = values.get(key)
        if isinstance(value, str) and len(value) <= 4096:
            config[key] = value.strip()
    if len(config['serial']) > 256:
        config['serial'] = ''
    for key in ('vendor_id', 'product_id'):
        if key in values:
            try:
                config[key] = f'{parse_usb_id(values[key]):04x}'
            except ValueError:
                pass
    if values.get('view') in ('fit', 'original'):
        config['view'] = values['view']
    manifests = values.get('extra_manifests')
    if isinstance(manifests, list):
        config['extra_manifests'] = [item for item in manifests[:32]
                                     if isinstance(item, str) and 0 < len(item) <= 4096]
    return config


def load_config(data_dir):
    try:
        path = Path(data_dir) / 'config.json'
        if path.stat().st_size > 65536:
            return _normalized({})
        values = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        values = {}
    return _normalized(values)


def save_config(data_dir, values):
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / ('config.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            json.dump(_normalized(values), output, ensure_ascii=False, indent=2,
                      allow_nan=False)
            output.write('\n')
        temporary.replace(root / 'config.json')
    finally:
        temporary.unlink(missing_ok=True)
