import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from android_usb_rescue.reference_catalog import load_references


class ReferenceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()

    def image(self, relative, size=(60, 130)):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', size, 'white').save(path)
        return path

    def write(self, relative, value):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_empty_directory_is_valid(self):
        self.assertEqual(load_references(self.base), [])

    def test_standard_sixteen_by_nine_portrait_is_accepted(self):
        portrait = self.image('references/standard.png', (108, 192))
        self.image('references/square.png', (108, 108))
        self.assertEqual([row['path'] for row in load_references(self.base)], [portrait])

    def test_folder_and_manifest_merge_without_duplicate_paths(self):
        path = self.image('references/portrait.png')
        self.image('references/sub/second.jpg')
        self.write('references.json', {'references': [{
            'path': 'references/portrait.png', 'label': 'Settings',
            'kind': 'Imported reference', 'recorded_at': '2026-01-02T10:30:00Z',
        }]})
        result = load_references(self.base)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {'path': path, 'label': 'Settings',
                                    'kind': 'Imported reference',
                                    'recorded_at': '2026-01-02T10:30:00Z'})

    def test_corrupt_sources_do_not_remove_valid_folder_image(self):
        path = self.image('references/portrait.png')
        (self.base / 'references/broken.png').write_bytes(b'not an image')
        (self.base / 'references.json').write_text('{broken', encoding='utf-8')
        self.write('config.json', {'extra_manifests': ['missing.json']})
        self.assertEqual([x['path'] for x in load_references(self.base)], [path])

    def test_manifest_boundary_landscape_and_invalid_metadata(self):
        portrait = self.image('extra/good.png')
        self.image('outside.png')
        self.image('extra/landscape.png', (130, 60))
        self.write('extra/references.json', {'references': [
            {'path': 'good.png', 'recorded_at': 'not a date', 'label': '\n Good\t label '},
            {'path': '../outside.png'}, {'path': 'landscape.png'}, {'path': 'good.png'},
        ]})
        self.write('config.json', {'extra_manifests': ['extra/references.json']})
        result = load_references(self.base)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['path'], portrait)
        self.assertEqual(result[0]['label'], 'Good label')
        self.assertIsNone(result[0]['recorded_at'])

    def test_symlink_cannot_escape_reference_root(self):
        outside = self.image('outside.png')
        folder = self.base / 'references'
        folder.mkdir()
        try:
            (folder / 'escape.png').symlink_to(outside)
        except OSError:
            self.skipTest('Symlinks unavailable on this Windows account')
        self.assertEqual(load_references(self.base), [])


if __name__ == '__main__':
    unittest.main()
