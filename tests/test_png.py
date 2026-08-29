"""The PNG encoder, checked by decoding what it writes.

Every assertion here round-trips through zlib and struct rather than
comparing against a golden blob: a hand-written encoder that produces
*consistent* bytes is easy, and consistent-but-wrong is exactly the failure
a golden file would lock in. The CRCs, the chunk framing and the scanline
filter bytes are all verified against RFC 2083's actual rules.
"""

import os
import struct
import tempfile
import unittest
import zlib

from pyguitest import png

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunks(data):
    """Split a PNG into {type: payload}, checking every CRC on the way."""
    assert data[:8] == SIGNATURE, "missing PNG signature"
    found = {}
    offset = 8
    order = []
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"bad CRC on {kind}"
        found[kind] = payload
        order.append(kind)
        offset += 12 + length
    return found, order


def scanlines(data, width, height):
    """Decompress IDAT and return the raw RGB rows, filter bytes stripped."""
    found, _ = chunks(data)
    raw = zlib.decompress(found[b"IDAT"])
    stride = width * 3 + 1
    rows = []
    for index in range(height):
        line = raw[index * stride : (index + 1) * stride]
        assert line[0] == 0, f"scanline {index} uses filter {line[0]}, expected 0"
        rows.append(line[1:])
    return rows


class TestEncode(unittest.TestCase):
    def test_header_describes_an_8_bit_rgb_image(self):
        rows = [bytes([1, 2, 3] * 4) for _ in range(3)]
        found, order = chunks(png.encode_rgb(4, 3, rows))
        width, height, depth, colour, compression, filt, interlace = struct.unpack(
            ">IIBBBBB", found[b"IHDR"]
        )
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(depth, 8)
        self.assertEqual(colour, 2, "colour type 2 is truecolour RGB")
        self.assertEqual((compression, filt, interlace), (0, 0, 0))
        self.assertEqual(order, [b"IHDR", b"IDAT", b"IEND"])

    def test_pixels_survive_the_round_trip_exactly(self):
        rows = [
            bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9]),
            bytes([0, 0, 0, 255, 255, 255, 1, 2, 3, 4, 5, 6]),
        ]
        self.assertEqual(scanlines(png.encode_rgb(4, 2, rows), 4, 2), rows)

    def test_a_single_pixel_is_valid(self):
        self.assertEqual(
            scanlines(png.encode_rgb(1, 1, [bytes([7, 8, 9])]), 1, 1),
            [bytes([7, 8, 9])],
        )

    def test_rows_may_be_a_generator(self):
        # A full-screen capture is large enough that materializing every
        # scanline before encoding is worth avoiding.
        rows = (bytes([index % 256] * 9) for index in range(5))
        data = png.encode_rgb(3, 5, rows)
        self.assertEqual(scanlines(data, 3, 5)[4], bytes([4] * 9))

    def test_a_short_scanline_is_rejected_rather_than_padded(self):
        with self.assertRaises(ValueError) as caught:
            png.encode_rgb(4, 1, [bytes(9)])
        self.assertIn("expected 12", str(caught.exception))

    def test_too_few_scanlines_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            png.encode_rgb(2, 3, [bytes(6), bytes(6)])
        self.assertIn("expected 3", str(caught.exception))

    def test_an_empty_image_is_rejected(self):
        for size in ((0, 1), (1, 0), (-1, 4)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                png.encode_rgb(size[0], size[1], [])

    def test_compression_level_changes_the_bytes_but_not_the_pixels(self):
        rows = [bytes(range(0, 30, 1))[:30] for _ in range(10)]
        low = png.encode_rgb(10, 10, rows, compresslevel=0)
        high = png.encode_rgb(10, 10, rows, compresslevel=9)
        self.assertNotEqual(low, high)
        self.assertEqual(scanlines(low, 10, 10), scanlines(high, 10, 10))


class TestWrite(unittest.TestCase):
    def test_write_rgb_produces_a_readable_file_and_returns_its_path(self):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(os.unlink, path)
        rows = [bytes([200, 100, 50] * 2) for _ in range(2)]
        self.assertEqual(png.write_rgb(path, 2, 2, rows), path)
        with open(path, "rb") as handle:
            data = handle.read()
        self.assertEqual(data[:8], SIGNATURE)
        self.assertEqual(scanlines(data, 2, 2), rows)


if __name__ == "__main__":
    unittest.main()
