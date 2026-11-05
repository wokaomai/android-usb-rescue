from pathlib import Path
import tempfile
import unittest

from android_usb_rescue.settings import load_config, parse_usb_id, save_config


class SettingsTests(unittest.TestCase):
    def test_usb_ids_are_hex_and_bounded(self):
        self.assertEqual(parse_usb_id('2717'), 0x2717)
        self.assertEqual(parse_usb_id('0xFF40'), 0xff40)
        self.assertEqual(parse_usb_id(0x2717), 0x2717)
        for value in ('', 'not-an-id', '10000', -1, True):
            with self.assertRaises(ValueError):
                parse_usb_id(value)

    def test_empty_and_corrupt_config_are_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
            self.assertEqual(load_config(root)['serial'], '')
            (root / 'config.json').write_text('{bad-json', encoding='utf-8')
            self.assertEqual(load_config(root)['serial'], '')

    def test_only_supported_settings_are_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
            save_config(root, {'serial': ' TEST-DEVICE ', 'vendor_id': '0x2717',
                               'product_id': 'invalid', 'unknown_private_field': 'omit'})
            values = load_config(root)
            self.assertEqual(values['serial'], 'TEST-DEVICE')
            self.assertEqual(values['vendor_id'], '2717')
            self.assertEqual(values['product_id'], 'ff40')
            self.assertNotIn('unknown_private_field', values)
            self.assertEqual(list(root.glob('*.tmp')), [])
