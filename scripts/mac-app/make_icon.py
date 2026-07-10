#!/usr/bin/env python3
"""Generate the BankApp icon as a 1024x1024 RGBA PNG using only the stdlib.

No Pillow (the install venv doesn't have it). We hand-write a PNG: a rounded square
with a teal->green vertical gradient and three ascending white bars (a little
bar-chart mark for a finance dashboard). build.sh turns this PNG into AppIcon.icns
with sips + iconutil.

    python3 make_icon.py [output.png]   # default: icon_1024.png next to this file
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 1024
MARGIN = 44           # transparent margin around the rounded square
RADIUS = 190          # corner radius of the rounded square
TOP = (46, 196, 182)  # teal
BOT = (16, 122, 90)   # deep green
BAR = (255, 255, 255)


def _rounded_alpha(x: float, y: float, lo: float, hi: float, r: float) -> float:
    """Coverage (0..1) for the rounded square, with a 1px-ish soft edge for antialiasing."""
    # distance OUTSIDE the rounded-rect [lo,hi]^2 with corner radius r
    dx = max(lo + r - x, 0.0, x - (hi - r))
    dy = max(lo + r - y, 0.0, y - (hi - r))
    if dx > 0.0 and dy > 0.0:
        dist = (dx * dx + dy * dy) ** 0.5 - r  # rounded corner
    else:
        # straight edges: distance past the flat sides
        dist = max(lo - x, x - hi, lo - y, y - hi)
    if dist <= -1.0:
        return 1.0
    if dist >= 0.0:
        return 0.0
    return -dist  # soft 1px edge


def _bars(x: float, y: float) -> bool:
    """True inside one of the three ascending bars (icon-space coords)."""
    heights = (300, 470, 640)          # ascending
    bar_w = 150
    gap = 60
    total_w = 3 * bar_w + 2 * gap
    x0 = (SIZE - total_w) / 2
    base = SIZE - 300                  # common baseline
    for i, h in enumerate(heights):
        bx = x0 + i * (bar_w + gap)
        if bx <= x <= bx + bar_w and (base - h) <= y <= base:
            return True
    return False


def build_png(out: Path) -> None:
    lo, hi = float(MARGIN), float(SIZE - MARGIN)
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)  # PNG filter type 0 for this scanline
        t = y / (SIZE - 1)
        gr = int(TOP[0] + (BOT[0] - TOP[0]) * t)
        gg = int(TOP[1] + (BOT[1] - TOP[1]) * t)
        gb = int(TOP[2] + (BOT[2] - TOP[2]) * t)
        yf = y + 0.5
        for x in range(SIZE):
            cov = _rounded_alpha(x + 0.5, yf, lo, hi, float(RADIUS))
            if cov <= 0.0:
                raw += b"\x00\x00\x00\x00"
                continue
            if _bars(x + 0.5, yf):
                r, g, b = BAR
            else:
                r, g, b = gr, gg, gb
            a = int(round(255 * cov))
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    out.write_bytes(png)


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("icon_1024.png")
    build_png(dest)
    print(dest)
