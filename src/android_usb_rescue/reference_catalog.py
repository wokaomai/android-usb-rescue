"""Load local, validated portrait references without touching a USB device."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from PIL import Image


EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_REFERENCES = 2048
MAX_IMAGE_PIXELS = 32_000_000


def _read_json(path):
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return None
        return json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def _resolve(value, parent):
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else parent / path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _text(value, default, maximum=160):
    return ' '.join(value.split())[:maximum] if isinstance(value, str) and value.strip() else default


def _recorded_at(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return value


def load_references(data_dir):
    """Merge a standard manifest, optional manifests, and ``references/``.

    Manifest image paths must stay inside that manifest's own directory. A bad
    entry or unavailable source does not remove references from other sources.
    Dates are optional source metadata, never evidence of a live phone screen.
    """
    base = Path(data_dir).expanduser().resolve()
    references, seen = [], set()

    def append(path, root, label=None, kind='本地参考图', recorded_at=None):
        if len(references) >= MAX_REFERENCES:
            return
        try:
            if (path is None or path in seen or not path.is_relative_to(root)
                    or path.suffix.lower() not in EXTENSIONS or not path.is_file()):
                return
            with Image.open(path) as image:
                if (image.width <= 0 or image.height <= image.width
                        or image.width * image.height > MAX_IMAGE_PIXELS):
                    return
                image.verify()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            return
        references.append({
            'path': path,
            'label': _text(label, path.stem),
            'kind': _text(kind, '本地参考图', 60),
            'recorded_at': _recorded_at(recorded_at),
        })
        seen.add(path)

    config = _read_json(base / 'config.json')
    extras = config.get('extra_manifests', []) if isinstance(config, dict) else []
    manifests = [base / 'references.json']
    if isinstance(extras, list):
        manifests.extend(path for value in extras[:64]
                         if (path := _resolve(value, base)) is not None)
    for manifest in dict.fromkeys(manifests):
        data = _read_json(manifest)
        rows = data.get('references', []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows[:MAX_REFERENCES]:
            if isinstance(row, dict):
                append(_resolve(row.get('path'), manifest.parent), manifest.parent,
                       row.get('label'), row.get('kind', '本地参考图'), row.get('recorded_at'))

    folder = base / 'references'
    try:
        if folder.resolve() == folder and folder.is_dir():
            for path in sorted(folder.rglob('*')):
                if len(references) >= MAX_REFERENCES:
                    break
                resolved = _resolve(str(path), base)
                append(resolved, folder, path.stem)
    except (OSError, ValueError, RuntimeError):
        pass
    return references
