#!/usr/bin/env python3
"""Generate simple solid PNG extension icons with the Python standard library."""

import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "extension" / "icons"


def chunk(kind, data):
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)


def png(size, rgb):
    rows = []
    for _ in range(size):
        rows.append(b"\x00" + bytes(rgb) * size)
    raw = b"".join(rows)
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def main():
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 48, 128):
        (ICON_DIR / f"{size}.png").write_bytes(png(size, (37, 99, 235)))


if __name__ == "__main__":
    main()
