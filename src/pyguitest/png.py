"""Writing a PNG from raw pixels, with nothing but the standard library.

Needed because one capture path produces pixels rather than a file. Every
other backend shells out to a tool that writes its own PNG, but X11's
GetImage hands back a buffer, and encoding it is the only step between that
buffer and the file every other capture path returns.

Pillow would do this in a line. It is deliberately not a dependency: this
package has no hard dependencies at all (see
docs/developers/adr-001-dependencies.md), and the PNG a screenshot needs is
the format's simplest case -- 8-bit RGB, no palette, no interlacing, no
ancillary chunks. zlib and struct cover it in under a hundred lines, which
is a smaller cost than an image library that would otherwise be pulled in
for this one function.

Format per RFC 2083: an 8-byte signature, then length-prefixed, CRC-suffixed
chunks. IHDR describes the image, IDAT carries zlib-compressed scanlines
each prefixed with a filter byte, IEND terminates.
"""

import struct
import zlib

__all__ = ["write_rgb", "encode_rgb", "png_size"]

_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_COLOR_TYPE_RGB = 2
_BIT_DEPTH = 8

# Filter type 0 ("None") per scanline: store the bytes as they are. The other
# four filters exist to help compression on photographic data and cost a pass
# over every row to choose between; a screenshot is mostly flat colour, which
# zlib already handles well, so the extra passes would buy little.
_FILTER_NONE = 0


def _chunk(kind, payload):
    """One PNG chunk: length, type, payload, CRC over type and payload."""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_rgb(width, height, rows, compresslevel=6):
    """Encode 8-bit RGB scanlines as a PNG, returning the bytes.

    `rows` is an iterable of `height` byte strings, each `width * 3` bytes
    of R, G, B triplets. It is consumed once and never held whole: the
    scanlines are fed through a streaming zlib compressor as they arrive,
    so a 4K screenshot does not need a second full-size copy in memory on
    top of the buffer it came from.

    `compresslevel` defaults to zlib's own 6 rather than 9. A screenshot is
    written to be looked at or diffed, not archived, and level 9 costs
    noticeably more time for a few percent of size.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"an image needs a positive size, got {width}x{height}")

    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        _BIT_DEPTH,
        _COLOR_TYPE_RGB,
        0,  # compression method: deflate, the only one defined
        0,  # filter method: the only one defined
        0,  # interlace: none
    )

    compressor = zlib.compressobj(compresslevel)
    stride = width * 3
    parts = []
    written = 0
    for row in rows:
        if len(row) != stride:
            raise ValueError(
                f"scanline {written} is {len(row)} bytes, expected {stride} "
                f"({width} pixels of RGB)"
            )
        parts.append(compressor.compress(bytes([_FILTER_NONE]) + bytes(row)))
        written += 1
    if written != height:
        raise ValueError(f"got {written} scanlines, expected {height}")
    parts.append(compressor.flush())

    return (
        _SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", b"".join(parts))
        + _chunk(b"IEND", b"")
    )


def write_rgb(path, width, height, rows, compresslevel=6):
    """Write 8-bit RGB scanlines to `path` as a PNG, returning that path."""
    data = encode_rgb(width, height, rows, compresslevel=compresslevel)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def png_size(path):
    """A PNG's (width, height) from its IHDR chunk, or None if it is not one.

    Eight bytes of signature, then IHDR is always the first chunk: its length
    and type take bytes 8-16, and width and height are the next two big-endian
    words. No decoder is needed for that, and this package has none.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != _SIGNATURE or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
