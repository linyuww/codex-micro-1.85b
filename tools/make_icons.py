#!/usr/bin/env python3
"""Rasterise the six task-key glyphs into 8-bit coverage masks.

The design is pixel art, so every glyph is authored on a 20x20 integer grid and
composed from pixel-exact primitives -- midpoint circles and Bresenham lines
rather than Pillow's anti-aliased ellipse/line -- so the output is hard-edged by
construction. Nothing here is anti-aliased; a mask byte is 0 or 255.

20 px is not a guess: probing the mockup's A2 button puts its icon at
x 153..170, y 230..249, i.e. 18x20 inside a 77x35 frame. 20x20 is the nearest
clean grid.

Masks are shape-only and tinted at draw time, which is what lets one glyph serve
every status colour and every fade level.

    python tools/make_icons.py            # write main/IconMask.h
    python tools/make_icons.py --check    # fail if out of date (CI)
    python tools/make_icons.py --sheet    # also write outputs/icon-sheet.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

GRID = 20
MID = (GRID - 1) / 2.0          # 9.5 -- the grid centre
ON, OFF = 1, 0


def blank(n: int = GRID) -> list[list[int]]:
    return [[OFF] * n for _ in range(n)]


def put(g, x, y, value=ON):
    if 0 <= x < len(g[0]) and 0 <= y < len(g):
        g[y][x] = value


def disc(g, cx, cy, r):
    """Filled circle, integer centres only."""
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                put(g, x, y)


def circle(g, cx, cy, r):
    """One-pixel outline via the midpoint algorithm -- no gaps, no wobble."""
    x, y, d = r, 0, 1 - r
    while x >= y:
        for px, py in ((x, y), (y, x), (-x, y), (-y, x),
                       (-x, -y), (-y, -x), (x, -y), (y, -x)):
            put(g, cx + px, cy + py)
        y += 1
        if d < 0:
            d += 2 * y + 1
        else:
            x -= 1
            d += 2 * (y - x) + 1


def line(g, x0, y0, x1, y1, value=ON):
    """Bresenham, so the stroke stays one pixel wide."""
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        put(g, x0, y0, value)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def thick_line(g, x0, y0, x1, y1):
    """Bresenham with a second parallel stroke, for 2 px emphasis."""
    line(g, x0, y0, x1, y1)
    if abs(x1 - x0) >= abs(y1 - y0):
        line(g, x0, y0 + 1, x1, y1 + 1)
    else:
        line(g, x0 + 1, y0, x1 + 1, y1)


def triangle_outline(g, apex, left, right):
    line(g, apex[0], apex[1], left[0], left[1])
    line(g, apex[0], apex[1], right[0], right[1])
    line(g, left[0], left[1], right[0], right[1])


def or_into(dst, src):
    for y in range(len(dst)):
        for x in range(len(dst[0])):
            dst[y][x] |= src[y][x]


def from_art(art: list[str]) -> list[list[int]]:
    assert len(art) == GRID, f"art has {len(art)} rows, need {GRID}"
    for i, row in enumerate(art):
        assert len(row) == GRID, f"row {i} has {len(row)} cols, need {GRID}"
    return [[ON if ch == "#" else OFF for ch in row] for row in art]


# ------------------------------------------------------------------ glyphs ---

def icon_done():
    """Tick inside a ring -- the completion state."""
    g = blank()
    circle(g, 9, 9, 8)
    tick = blank()
    # Centred on the ring: down-stroke to (8,12), then up to (14,5).
    line(tick, 5, 9, 8, 12)
    line(tick, 8, 12, 14, 5)
    or_into(g, tick)
    return g


def icon_error():
    """Alert triangle carrying an exclamation mark."""
    g = blank()
    triangle_outline(g, (9, 2), (1, 18), (17, 18))
    line(g, 9, 7, 9, 12)
    put(g, 9, 15)
    return g


def icon_empty():
    """Dashed ring -- an unassigned key, deliberately not a solid glyph."""
    ring = blank()
    circle(ring, 9, 9, 8)
    g = blank()
    for y in range(GRID):
        for x in range(GRID):
            if not ring[y][x]:
                continue
            # Punch four gaps at the diagonals so the ring reads as dashed.
            if (y - 9) in (-6, -5, 5, 6) and abs(x - 9) >= 6:
                continue
            if (x - 9) in (-6, -5, 5, 6) and abs(y - 9) >= 6:
                continue
            put(g, x, y)
    return g


def icon_idle():
    """Crescent moon -- idle, nothing running.

    Built from explicit per-row spans rather than a disc-minus-disc subtraction,
    so the concave edge stays smooth instead of going ragged where the two
    integer circles disagree.
    """
    g = blank()
    ocx, ocy, orad = 8, 9, 7
    ccx, ccy, crad = 13, 9, 7
    for y in range(GRID):
        dy = y - ocy
        if abs(dy) > orad:
            continue
        half = int((orad * orad - dy * dy) ** 0.5)
        lo, hi = ocx - half, ocx + half
        dy2 = y - ccy
        if abs(dy2) <= crad:
            cut = int((crad * crad - dy2 * dy2) ** 0.5)
            hi = min(hi, ccx - cut - 1)
        for x in range(max(0, lo), min(GRID - 1, hi) + 1):
            put(g, x, y)
    return g


def boundary(shape):
    """Keep only the outline of a filled shape.

    Overlapping circle outlines leave interior arcs where the discs intersect,
    which at 20 px reads as scribble. Filling first and then extracting the
    boundary guarantees exactly one clean contour.
    """
    n = len(shape)
    out = blank(n)
    for y in range(n):
        for x in range(n):
            if not shape[y][x]:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < n and 0 <= ny < n) or not shape[ny][nx]:
                    out[y][x] = ON
                    break
    return out


def icon_input():
    """Pencil -- the key wants input. Body up-right, tip at lower-left.

    Generated rather than hand-drawn so the 45-degree edge steps by exactly one
    pixel per row: a hand-typed diagonal always drifts.
    """
    g = blank()
    # Body: a 5 px band running down-left, from (12,1) to (2,11).
    for y in range(1, 12):
        for dx in range(5):
            put(g, 12 - y + dx + 1, y)
    # Ferrule: one blank row, so the tip reads as separate from the body.
    # Tip: taper to a single point at (1,17).
    put(g, 2, 12)
    put(g, 1, 13)
    put(g, 1, 14)
    put(g, 0, 15)
    put(g, 0, 16)
    put(g, 0, 17)
    put(g, 1, 12)
    put(g, 2, 13)
    put(g, 2, 14)
    return g


def icon_think():
    """Brain -- two lobes plus a crown, outlined then fissured.

    Filled discs are unioned and the boundary extracted, so the silhouette is a
    single clean contour instead of overlapping arcs. A vertical fissure is then
    cut through the interior and two fold ticks are added, which is what makes
    it read as a brain rather than a cloud.
    """
    shape = blank()
    disc(shape, 6, 11, 5)        # left lobe
    disc(shape, 13, 11, 5)       # right lobe
    disc(shape, 9, 7, 5)         # crown
    disc(shape, 4, 8, 3)         # left shoulder
    disc(shape, 15, 8, 3)        # right shoulder

    g = boundary(shape)
    # Fissure: split the hemispheres without opening the top of the silhouette.
    for y in range(5, 17):
        for x in (9, 10):
            put(g, x, y, OFF)
    # Fold ticks, one per lobe, a row in from the contour.
    for x in range(3, 7):
        put(g, x, 10)
    for x in range(13, 17):
        put(g, x, 13)
    return g


GLYPHS = {
    "kIconThink": icon_think,
    "kIconDone": icon_done,
    "kIconInput": icon_input,
    "kIconEmpty": icon_empty,
    "kIconError": icon_error,
    "kIconIdle": icon_idle,
}


def to_mask(g) -> bytes:
    return bytes(255 if v else 0 for row in g for v in row)


def array_text(name: str, payload: bytes) -> str:
    rows = []
    for offset in range(0, len(payload), 16):
        chunk = payload[offset:offset + 16]
        rows.append("  " + ", ".join(f"0x{v:02X}" for v in chunk) + ",")
    return (f"static constexpr std::uint8_t {name}[] = {{\n"
            + "\n".join(rows) + "\n};\n")


def build() -> str:
    parts = [
        "// Generated by tools/make_icons.py. Do not edit manually.\n"
        "//\n"
        "// 20x20 8-bit coverage masks, authored on an integer pixel grid from\n"
        "// midpoint circles and Bresenham strokes. Every byte is 0x00 or 0xFF:\n"
        "// the glyphs are hard-edged by construction, never anti-aliased.\n"
        "//\n"
        "// 20 px matches the mockup, whose A2 button icon measures 18x20 inside\n"
        "// a 77x35 frame.\n"
        "//\n"
        "// Tint at draw time so one shape serves every status colour.\n"
        "#pragma once\n\n"
        "#include <cstdint>\n\n"
        "namespace dashboard {\nnamespace icon_data {\n\n"
        f"constexpr int kIconSize = {GRID};\n\n"
    ]
    for name, builder in GLYPHS.items():
        payload = to_mask(builder())
        parts.append(array_text(name, payload))
        parts.append("\n")
    parts.append("}  // namespace icon_data\n}  // namespace dashboard\n")
    return "".join(parts)


def write_sheet(out: Path, zoom: int = 10):
    names = list(GLYPHS)
    cell = GRID * zoom
    pad = 8
    width = pad + len(names) * (cell + pad)
    height = pad + cell + pad
    sheet = Image.new("RGB", (width, height), (28, 30, 36))
    for index, name in enumerate(names):
        g = GLYPHS[name]()
        img = Image.new("L", (GRID, GRID), 0)
        img.putdata([255 if v else 0 for row in g for v in row])
        img = img.resize((cell, cell), Image.NEAREST).convert("RGB")
        sheet.paste(img, (pad + index * (cell + pad), pad))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=root / "main" / "IconMask.h")
    parser.add_argument("--sheet", nargs="?", const="", default=None,
                        help="also write a zoomed contact sheet PNG")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    text = build()

    if args.check:
        if not args.output.exists():
            print(f"missing {args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} is out of date", file=sys.stderr)
            return 1
        print(f"{args.output} up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    for name, builder in GLYPHS.items():
        g = builder()
        ink = sum(sum(row) for row in g)
        ys = [y for y in range(GRID) for x in range(GRID) if g[y][x]]
        xs = [x for y in range(GRID) for x in range(GRID) if g[y][x]]
        print(f"  {name:12s} ink {ink:3d}px  bbox x {min(xs)}..{max(xs)} "
              f"y {min(ys)}..{max(ys)}")

    if args.sheet is not None:
        out = Path(args.sheet) if args.sheet else \
            Path(r"C:\Users\86147\Documents\Codex\2026-09-16"
                 r"\https-github-com-digitsisyph-codex-micro\outputs\icon-sheet.png")
        print(f"wrote {write_sheet(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
