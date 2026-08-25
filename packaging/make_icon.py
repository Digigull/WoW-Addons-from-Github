#!/usr/bin/env python3
"""Draw the application icon, for both platforms, from one description.

    python3 packaging/make_icon.py

Writes the 256x256 PNG the AppImage recipe wants and the multi-size .ico
PyInstaller wants. One script, so the two builds cannot end up looking like
different programs.

Generated rather than committed as an opaque blob: changing the icon stays a
diff somebody can read and re-run, and the repo needs no drawing tool nobody
has. zlib and struct are enough for both formats -- a .ico is a small header
followed by, in this case, ordinary PNGs, which Windows has accepted since
Vista.

The shape: a rounded slate tile, a downward arrow into a tray -- an addon
arriving in a folder -- in the gold the client uses for its own UI.

Sizes matter on Windows in a way they do not on Linux. 16x16 is what the
taskbar and title bar use, and a 256x256 image scaled down to it turns to
mush, so each size is drawn at its own scale instead.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# What Windows actually asks for: 16 for the title bar and taskbar, 32 for the
# alt-tab switcher, 48 and 256 for Explorer's icon views.
ICO_SIZES = (16, 32, 48, 256)
PNG_SIZE = 256
SS = 4  # supersampling factor; the whole anti-aliasing strategy

BACKGROUND = (0x1E, 0x22, 0x28)
EDGE = (0x33, 0x3B, 0x45)
GOLD = (0xF2, 0xC3, 0x4C)
TRAY = (0x8E, 0xA6, 0xC0)


def rounded_rect(x, y, w, h, radius):
    """A predicate: is (x, y) inside this rounded rectangle?"""
    left, top, right, bottom = x, y, x + w, y + h

    def inside(px, py):
        if not (left <= px <= right and top <= py <= bottom):
            return False
        cx = min(max(px, left + radius), right - radius)
        cy = min(max(py, top + radius), bottom - radius)
        return (px - cx) ** 2 + (py - cy) ** 2 <= radius ** 2 or (
            left + radius <= px <= right - radius or top + radius <= py <= bottom - radius
        )

    return inside


def arrow(px, py):
    """A thick downward arrow: a stem, then a head, in 256-unit coordinates."""
    if 112 <= px <= 144 and 52 <= py <= 132:
        return True
    # Head: a triangle from (78,124) to (178,124) down to the point at (128,182).
    if 124 <= py <= 182:
        half = 50 * (182 - py) / 58
        return abs(px - 128) <= half
    return False


def tray(px, py):
    """The open box the arrow points into."""
    if not (56 <= px <= 200 and 176 <= py <= 216):
        return False
    # Hollow: walls and a floor, open at the top.
    return px <= 72 or px >= 184 or py >= 200


def render(size: int = PNG_SIZE) -> list[list[tuple[int, int, int, int]]]:
    """Draw the icon at `size`, in RGBA rows.

    The shape is described once in 256-unit coordinates and sampled at whatever
    scale is asked for, so a 16x16 is drawn rather than shrunk.
    """
    scale = 256 / size
    tile = rounded_rect(10, 10, 236, 236, 46)
    inner = rounded_rect(16, 16, 224, 224, 40)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            # Supersample: average SS x SS samples per pixel. Slow and obvious,
            # which is the right trade for something run by hand once.
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = (x + (sx + 0.5) / SS) * scale
                    py = (y + (sy + 0.5) / SS) * scale
                    if arrow(px, py):
                        sample = GOLD
                    elif tray(px, py):
                        sample = TRAY
                    elif inner(px, py):
                        sample = BACKGROUND
                    elif tile(px, py):
                        sample = EDGE
                    else:
                        continue
                    r += sample[0]
                    g += sample[1]
                    b += sample[2]
                    a += 255
            n = SS * SS
            if a == 0:
                row.append((0, 0, 0, 0))
            else:
                covered = a // n
                # Un-premultiply so partly covered edge pixels keep their colour
                # instead of fading towards black.
                weight = a // 255
                row.append((r // weight, g // weight, b // weight, covered))
        rows.append(row)
    return rows


def png_bytes(rows) -> bytes:
    size = len(rows)
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBBB", *pixel) for pixel in row) for row in rows
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, rows) -> None:
    path.write_bytes(png_bytes(rows))


def ico_bytes(images: dict) -> bytes:
    """Pack {size: png bytes} into a .ico.

    An .ico is an ICONDIR, then one ICONDIRENTRY per image, then the image data.
    The entries store PNG streams verbatim rather than the old BMP-with-AND-mask
    encoding: Windows has read PNG-compressed icons since Vista, and the BMP
    form would need a hand-rolled alpha mask for no benefit here.

    A 256-pixel image records its width and height as 0, which is how the format
    says 256 in a single byte.
    """
    entries, blobs = b"", b""
    offset = 6 + 16 * len(images)
    for size, blob in sorted(images.items()):
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # width
            0 if size >= 256 else size,  # height
            0,  # palette size; 0 means "not paletted"
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(blob),
            offset,
        )
        blobs += blob
        offset += len(blob)
    # ICONDIR: reserved, type 1 (icon, as opposed to 2 for cursor), count.
    return struct.pack("<HHH", 0, 1, len(images)) + entries + blobs


def write_ico(path: Path, sizes=ICO_SIZES) -> None:
    path.write_bytes(ico_bytes({size: png_bytes(render(size)) for size in sizes}))


ROOT = Path(__file__).resolve().parent
PNG_PATH = ROOT / "appimage" / "WoW-Addons-from-GitHub" / "wow-addons-from-github.png"
ICO_PATH = ROOT / "windows" / "wow-addons-from-github.ico"


if __name__ == "__main__":
    for path, write in ((PNG_PATH, lambda p: write_png(p, render())), (ICO_PATH, write_ico)):
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path)
        print(f"wrote {path} ({path.stat().st_size} bytes)")
