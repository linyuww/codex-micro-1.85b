#!/usr/bin/env python3
"""Convert the scene PNGs into raw RGB565 blobs the firmware can blit.

The firmware framebuffer is stored big-endian (see gfx.h: the ST77916 takes
RGB565 most-significant-byte first over QSPI), and `board_display::flush()`
documents that it expects "panel byte order (high byte first)". Storing the
blob in exactly that order means the renderer can memcpy a row straight into
the framebuffer with no per-pixel work at all -- which matters, because the
background is 259,200 bytes and is the single largest thing drawn per frame.

Each output is 360 * 360 * 2 = 259,200 bytes (253.1 KB), two of them = 506 KB.

The round mask is deliberately NOT baked in. The panel is physically round, so
the square corners are never visible; masking them would only make the blob
differ from the PNG for no benefit.

    python scripts/assets/make_backgrounds.py
    python scripts/assets/make_backgrounds.py --check
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image

WIDTH = 360
HEIGHT = 360

SCENES = ("day", "night")


def rgb565_word(r: int, g: int, b: int) -> int:
    """The same quantisation the preview uses, so PNG and panel agree."""
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def convert(source: str, destination: str) -> int:
    image = Image.open(source)
    if image.size != (WIDTH, HEIGHT):
        raise SystemExit(f"{source}: need {WIDTH}x{HEIGHT}, got {image.size}")
    if image.mode != "RGB":
        image = image.convert("RGB")

    raw = image.tobytes()
    out = bytearray(WIDTH * HEIGHT * 2)
    for i in range(WIDTH * HEIGHT):
        base = i * 3
        word = rgb565_word(raw[base], raw[base + 1], raw[base + 2])
        # Big-endian on purpose: matches the framebuffer's stored order.
        out[i * 2] = (word >> 8) & 0xFF
        out[i * 2 + 1] = word & 0xFF

    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(out)
    return len(out)


def sample(path: str, points) -> None:
    """Print a few decoded pixels, to prove the round-trip is not byte-swapped."""
    with open(path, "rb") as handle:
        data = handle.read()
    print(f"  {os.path.basename(path)}: {len(data)} bytes")
    for x, y in points:
        i = (y * WIDTH + x) * 2
        word = (data[i] << 8) | data[i + 1]
        r = ((word >> 11) & 0x1F) << 3
        g = ((word >> 5) & 0x3F) << 2
        b = (word & 0x1F) << 3
        print(f"    ({x:3d},{y:3d}) -> #{r:02X}{g:02X}{b:02X}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--check", action="store_true",
                        help="verify the blobs on disk are up to date")
    parser.add_argument("--out-dir",
                        default=os.path.join(root, "main", "assets"))
    args = parser.parse_args()

    expected = WIDTH * HEIGHT * 2
    stale = []
    for scene in SCENES:
        source = os.path.join(root, "scripts", "assets", "preview", f"bg-{scene}.png")
        # Underscore rather than a dash: objcopy derives the linker symbol from
        # the file name, and a dash would become `_binary_bg_day_bin_start`
        # either way -- but underscores keep the mapping obvious by inspection.
        destination = os.path.join(args.out_dir, f"bg_{scene}.bin")

        if args.check:
            if not os.path.exists(destination):
                print(f"MISSING {destination}")
                stale.append(scene)
                continue
            source_mtime = os.path.getmtime(source)
            target_mtime = os.path.getmtime(destination)
            size = os.path.getsize(destination)
            ok = size == expected and target_mtime >= source_mtime
            print(f"{scene}: {size} bytes, "
                  f"{'up to date' if ok else 'STALE'}")
            if not ok:
                stale.append(scene)
            continue

        size = convert(source, destination)
        assert size == expected, (size, expected)
        print(f"{scene}: {source} -> {destination} ({size} bytes)")

    if args.check:
        return 1 if stale else 0

    print("sample pixels (decoded from the blob, must look like the scene):")
    for scene in SCENES:
        sample(os.path.join(args.out_dir, f"bg_{scene}.bin"),
               [(180, 20), (180, 180), (180, 340), (20, 180)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
