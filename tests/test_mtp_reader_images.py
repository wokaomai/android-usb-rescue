"""Image integrity and decoding limits; all samples are synthetic."""
from io import BytesIO
import struct
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, PngImagePlugin, features

from android_usb_rescue import mtp_reader as mtp


def sample(format_name):
    output = BytesIO()
    Image.new('RGB', (2, 3), '#296593').save(output, format=format_name)
    return output.getvalue()


class ImageValidationTests(unittest.TestCase):
    def test_valid_images_and_jpeg_alias(self):
        for format_name, extension in [('PNG', '.png'), ('JPEG', '.jpg'), ('JPEG', '.jpeg')]:
            with self.subTest(format_name=format_name, extension=extension):
                self.assertTrue(mtp._valid_image(sample(format_name), extension))
        if features.check('webp'):
            self.assertTrue(mtp._valid_image(sample('WEBP'), '.webp'))

    def test_signature_only_and_truncated_images_rejected(self):
        for data, extension in [(b'\x89PNG\r\n\x1a\n', '.png'),
                                (sample('PNG')[:-10], '.png'),
                                (sample('JPEG')[:-10], '.jpg')]:
            with self.subTest(extension=extension, size=len(data)):
                self.assertFalse(mtp._valid_image(data, extension))

    def test_corrupted_png_chunk_rejected(self):
        data = bytearray(sample('PNG'))
        index = data.index(b'IDAT')
        data[index + 4] ^= 1
        self.assertFalse(mtp._valid_image(data, '.png'))

    def test_format_must_match_extension(self):
        self.assertFalse(mtp._valid_image(sample('JPEG'), '.png'))
        self.assertFalse(mtp._valid_image(sample('PNG'), '.jpg'))
        self.assertFalse(mtp._valid_image(sample('PNG'), '.gif'))

    def test_pixel_limit_checked_before_verify_or_decode(self):
        data = bytearray(sample('PNG'))
        data[16:24] = struct.pack('>II', mtp.MAX_IMAGE_PIXELS + 1, 1)
        data[29:33] = struct.pack('>I', zlib.crc32(data[12:29]))
        with patch.object(PngImagePlugin.PngImageFile, 'verify', side_effect=AssertionError('verify called')), \
                patch.object(PngImagePlugin.PngImageFile, 'load', side_effect=AssertionError('decode called')):
            self.assertFalse(mtp._valid_image(data, '.png'))

    def test_byte_limit_and_empty_payload_rejected(self):
        with patch.object(mtp, 'MAX_IMAGE', 8), \
                patch.object(mtp.Image, 'open', side_effect=AssertionError('header parsed')):
            self.assertFalse(mtp._valid_image(sample('PNG'), '.png'))
            self.assertFalse(mtp._valid_image(b'', '.png'))


if __name__ == '__main__':
    unittest.main()
