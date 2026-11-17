"""Tiny generated placeholder photos for demo data (no image library needed).

Real suppliers upload their own photos; these only let seeded demo listings
meet the minimum-photos rule. Each image is a labelled-by-colour PNG.
"""
import struct
import zlib


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def placeholder_png(rgb_top, rgb_bottom, size=160):
    """A vertical two-tone gradient PNG, e.g. mango yellow -> leaf green."""
    rows = []
    for y in range(size):
        t = y / (size - 1)
        r, g, b = (round(a + (b_ - a) * t) for a, b_ in zip(rgb_top, rgb_bottom))
        # a darker round "fruit" in the middle
        row = bytearray([0])
        for x in range(size):
            inside = (x - size / 2) ** 2 + (y - size / 2) ** 2 < (size * 0.32) ** 2
            px = (min(255, r + 40), max(0, g - 30), max(0, b - 30)) if inside else (r, g, b)
            row += bytes(px)
        rows.append(bytes(row))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


DEMO_PALETTES = [((246, 190, 40), (86, 150, 60)), ((240, 140, 30), (200, 170, 60)), ((250, 210, 80), (60, 120, 50))]
