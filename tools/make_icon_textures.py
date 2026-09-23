#!/usr/bin/env python3
"""Turn the artist's icon art into ready-to-blit icon textures.

Two sources, and the default is the mockup
------------------------------------------
`--from mockup` (default) traces the six status icons straight out of the design
mockup, where each one is ~77 px. `--from render` traces the thirteen icons
generated separately, downsampled from ~940 px.

The mockup wins on shape fidelity, which is what matters here. Side by side, the
generated set diverges from the design on four of the six: EMPTY's ring comes
out solid where the design clearly dashes it, INPUT comes out as a filled bar
where the design draws an outlined pencil, DONE loses its hexagonal ring, and
THINK loses the brain's stem. The generated set is crisper, but it is a
reinterpretation; the mockup is the design.

Why textures rather than a mask plus a runtime tint
---------------------------------------------------
A single-channel coverage mask tinted at draw time is the right choice when the
colour varies. For these icons it does not: each status has exactly one colour
in the design (THINK cyan, DONE green, INPUT amber, EMPTY grey, ERROR red, IDLE
violet), so tinting buys nothing and costs the artist's internal shading -- the
brain's highlights, the pencil's highlight stripe, the moon's gradient. Six
20x20 RGB565+A8 textures cost 7.2 KB, which is not worth economising.

The battery is the exception: its colour tracks the charge level, so it stays a
coverage mask and is tinted at runtime.

Format
------
Premultiplied. For the renders (glyph-on-black) RGB is already multiplied by
coverage and alpha is the brightest channel. For the mockup the icons sit on the
button's flat fill, so the fill is recovered from the crop's own corner and the
coverage solved for. Either way compositing onto any destination is

    out = src + dst * (255 - a) / 255

which is both exact and cheap. It also reproduces the design faithfully: the
brain's fold lines really are the button background showing through, not black
ink, and premultiplied alpha gives exactly that.

RGB565 is stored big-endian, matching the panel's byte order.

    python tools/make_icon_textures.py            # write main/IconTextures.h
    python tools/make_icon_textures.py --sheet    # also write a preview PNG
    python tools/make_icon_textures.py --check    # fail if out of date
    python tools/make_icon_textures.py --from render
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageChops

GRID = 20
DL = r"D:\Downloads"
PREFIX = "ChatGPT Image 2026年9月23日 "

# name -> coloured source file
SOURCES = {
    "kIconThink": "11_11_19 (1).png",
    "kIconDone": "11_11_20 (2).png",
    "kIconInput": "11_11_20 (3).png",
    "kIconEmpty": "11_11_21 (4).png",
    "kIconError": "11_11_21 (5).png",
    "kIconIdle": "11_11_22 (6).png",
}

# Expected dominant colour, from the design mockup. A large deviation means the
# files are mis-paired, which is easy to do with thirteen near-identically named
# downloads.
EXPECT = {
    "kIconThink": (50, 220, 254),
    "kIconDone": (16, 255, 168),
    "kIconInput": (255, 189, 61),
    "kIconEmpty": (200, 214, 230),
    "kIconError": (255, 71, 84),
    "kIconIdle": (140, 138, 255),
}

BG_TOL = 12            # the render background must be this close to black
COVER_TOL = 60         # max-channel value above which a pixel is "ink"

# --- mockup tracing -------------------------------------------------------
# An alternative source: the design mockup itself, which carries the icons at
# ~77 raw px each. That is a better sampling of the design than the separately
# generated renders, and it is the only place the dashed EMPTY ring exists.
MOCKUP = r"D:\Downloads\ChatGPT Image 2026年9月23日 09_33_56.png"
MOCKUP_PANEL = 360
BTN_W = 77
BTN_COLS = (98, 180, 262)
BTN_ROWS = (240, 281)
BTN_ICON_DX = 19       # icon centre, from the button's left edge


def mockup_transform(img):
    """Device 360 space -> raw pixel space, as measure_chrome.normalise does."""
    w, h = img.size
    px = img.load()
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if sum(px[x, y][:3]) > 60:
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
    side = max(maxx - minx, maxy - miny) + 1
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    left = int(round(cx - side / 2.0))
    top = int(round(cy - side / 2.0))
    s = side / MOCKUP_PANEL
    return (lambda x: left + x * s), (lambda y: top + y * s), s


def build_mockup():
    """Trace the six status icons straight out of the design mockup.

    The icons are saturated art drawn on the button's flat fill, so the fill can
    be recovered from the crop's own corner and the coverage solved for. With
    `p = C*a + bg*(1-a)` and `m = max_c(p_c - bg_c)`, normalising m by the core's
    own m gives a directly, and `p - bg*(1-a)` is then the premultiplied colour.
    """
    img = Image.open(MOCKUP).convert("RGB")
    fx, fy, s = mockup_transform(img)
    print(f"mockup {img.size}, device scale {s:.4f} px/device-px")

    icons = {}
    report = []
    for i, (name, col, row) in enumerate(
            [(n, c, r) for n, c, r in
             (("kIconThink", 0, 0), ("kIconDone", 1, 0), ("kIconInput", 2, 0),
              ("kIconEmpty", 0, 1), ("kIconError", 1, 1),
              ("kIconIdle", 2, 1))]):
        cx = BTN_COLS[col] - BTN_W / 2 + BTN_ICON_DX
        cy = BTN_ROWS[row]
        # A 22 device px window: the 20 px slot plus 1 px of margin each side,
        # which is then trimmed, so the frame never enters the sample.
        half = (GRID + 2) / 2
        box = (int(round(fx(cx - half))), int(round(fy(cy - half))),
               int(round(fx(cx + half))), int(round(fy(cy + half))))
        crop = img.crop(box)

        # Background: the corner is pure button fill.
        bg = crop.getpixel((1, 1))
        px = crop.load()
        w, h = crop.size
        vals, premul = [], []
        for y in range(h):
            for x in range(w):
                p = px[x, y]
                d = [max(0, p[c] - bg[c]) for c in range(3)]
                m = max(d)
                vals.append(m)
                premul.append(d)
        vals_sorted = sorted(vals)
        core = vals_sorted[int(len(vals_sorted) * 0.97)] or 1

        alpha = bytearray(w * h)
        colour = [(0, 0, 0)] * (w * h)
        for idx, m in enumerate(vals):
            a = min(1.0, m / core)
            if a < 0.12:
                a = 0.0
            alpha[idx] = int(a * 255 + 0.5)
            colour[idx] = tuple(int(min(255, premul[idx][c] / a)) if a > 0
                                else 0 for c in range(3))

        col_img = Image.new("RGB", (w, h))
        col_img.putdata(colour)
        a_img = Image.frombytes("L", (w, h), bytes(alpha))

        # Trim the 1 px margin, then area-average to the slot.
        t = max(1, int(round(s)))
        col_img = col_img.crop((t, t, w - t, h - t))
        a_img = a_img.crop((t, t, w - t, h - t))
        col_small = col_img.resize((GRID, GRID), Image.BOX)
        a_small = a_img.resize((GRID, GRID), Image.BOX)

        words = [rgb565(col_small.getpixel((x, y)))
                 for y in range(GRID) for x in range(GRID)]
        alphas = bytes(a_small.tobytes())
        icons[name] = (words, alphas)
        report.append((name, "mockup", (w - 2 * t, h - 2 * t),
                       dominant(col_small, a_small), EXPECT[name], 0))

    return icons, report



def load_source(filename: str) -> Image.Image:
    path = os.path.join(DL, PREFIX + filename)
    if not os.path.exists(path):
        raise SystemExit(f"missing source: {path}")
    img = Image.open(path).convert("RGB")
    # The premultiplied contract only holds if the background really is black.
    for corner in ((0, 0), (img.size[0] - 1, 0), (0, img.size[1] - 1),
                   (img.size[0] - 1, img.size[1] - 1)):
        px = img.getpixel(corner)
        if max(px) > BG_TOL:
            raise SystemExit(f"{filename}: background is {px}, not black")
    return img


def coverage(img: Image.Image) -> Image.Image:
    """A8 coverage: the brightest channel.

    Keying on "not black" rather than luminance matters here. The artist's red
    is (255, 71, 84), whose luma is exactly 128, so a luminance test puts the
    whole ERROR triangle on the threshold.
    """
    r, g, b = img.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def crop_to_ink(img: Image.Image, mask: Image.Image, margin: int = 2):
    box = mask.point(lambda v: 255 if v > COVER_TOL else 0).getbbox()
    if box is None:
        raise SystemExit("empty glyph")
    x0, y0, x1, y1 = box
    w, h = img.size
    box = (max(0, x0 - margin), max(0, y0 - margin),
           min(w, x1 + margin), min(h, y1 + margin))
    return img.crop(box), mask.crop(box)


def to_slot(img: Image.Image, mask: Image.Image, slot_w: int, slot_h: int):
    """Area-average into the slot, preserving aspect, centred."""
    w, h = img.size
    scale = min(slot_w / w, slot_h / h)
    tw = max(1, int(round(w * scale)))
    th = max(1, int(round(h * scale)))
    # BOX is a true area average, so premultiplied colour stays consistent with
    # alpha: averaging (colour, alpha) separately is the same as averaging the
    # premultiplied pairs.
    small = img.resize((tw, th), Image.BOX)
    smask = mask.resize((tw, th), Image.BOX)
    slot = Image.new("RGB", (slot_w, slot_h), (0, 0, 0))
    amask = Image.new("L", (slot_w, slot_h), 0)
    ox, oy = (slot_w - tw) // 2, (slot_h - th) // 2
    slot.paste(small, (ox, oy))
    amask.paste(smask, (ox, oy))
    return slot, amask, (tw, th)


def rgb565(px) -> int:
    r, g, b = px
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def dominant(img: Image.Image, mask: Image.Image):
    """Mean colour over the strongly-covered pixels."""
    px, mp = img.load(), mask.load()
    w, h = img.size
    sr = sg = sb = n = 0
    for y in range(h):
        for x in range(w):
            if mp[x, y] >= 200:
                r, g, b = px[x, y]
                sr += r
                sg += g
                sb += b
                n += 1
    if not n:
        return (0, 0, 0)
    return (sr // n, sg // n, sb // n)


def array_u16(name: str, values) -> str:
    rows = []
    for off in range(0, len(values), 8):
        rows.append("  " + ", ".join(f"0x{v:04X}" for v in values[off:off + 8])
                    + ",")
    return (f"static constexpr std::uint16_t {name}[] = {{\n"
            + "\n".join(rows) + "\n};\n")


def array_u8(name: str, payload: bytes) -> str:
    rows = []
    for off in range(0, len(payload), 16):
        rows.append("  " + ", ".join(f"0x{v:02X}" for v in payload[off:off + 16])
                    + ",")
    return (f"static constexpr std::uint8_t {name}[] = {{\n"
            + "\n".join(rows) + "\n};\n")


def emit(icons) -> str:
    parts = [
        "// Generated by tools/make_icon_textures.py. Do not edit manually.\n"
        "//\n"
        f"// {GRID}x{GRID} premultiplied icon textures traced from the artist's\n"
        "// coloured renders: RGB565 (big-endian, panel order) plus an A8\n"
        "// coverage plane.\n"
        "//\n"
        "// Composite onto any destination with\n"
        "//     out = src + dst * (255 - a) / 255\n"
        "//\n"
        "// The battery is deliberately absent: its colour tracks the charge\n"
        "// level, so it stays an A8 mask (IconMask.h) tinted at draw time.\n"
        "#pragma once\n\n"
        "#include <cstdint>\n\n"
        "namespace dashboard {\nnamespace icon_data {\n\n"
        f"constexpr int kIconTextureSize = {GRID};\n\n"
    ]
    for name, (words, alphas) in icons.items():
        parts.append(array_u16(name + "_rgb", words))
        parts.append("\n")
        parts.append(array_u8(name + "_a", alphas))
        parts.append("\n")
    parts.append("}  // namespace icon_data\n}  // namespace dashboard\n")
    return "".join(parts)


def build():
    icons = {}
    report = []
    for name, filename in SOURCES.items():
        img = load_source(filename)
        cov = coverage(img)
        crop, ccov = crop_to_ink(img, cov)
        slot, aslot, content = to_slot(crop, ccov, GRID, GRID)

        got = dominant(slot, aslot)
        want = EXPECT[name]
        err = max(abs(a - b) for a, b in zip(got, want))
        report.append((name, filename, content, got, want, err))

        px = slot.load()
        ap = aslot.load()
        words, alphas = [], []
        for y in range(GRID):
            for x in range(GRID):
                words.append(rgb565(px[x, y]))
                alphas.append(ap[x, y])
        icons[name] = (words, bytes(alphas))

    return emit(icons), icons, report


def write_sheet(icons, out: Path, zoom: int = 12):
    names = list(icons)
    cell = GRID * zoom
    pad = 10
    sheet = Image.new("RGB", (pad + len(names) * (cell + pad), cell + 2 * pad),
                      (2, 11, 26))
    for i, name in enumerate(names):
        words, alphas = icons[name]
        tile = Image.new("RGB", (GRID, GRID))
        for y in range(GRID):
            for x in range(GRID):
                v = words[y * GRID + x]
                r = ((v >> 11) & 0x1F) << 3
                g = ((v >> 5) & 0x3F) << 2
                b = (v & 0x1F) << 3
                a = alphas[y * GRID + x] / 255.0
                # Composite over the design's button background.
                bg = (2, 11, 26)
                tile.putpixel((x, y), tuple(
                    min(255, int(c + d * (1 - a)))
                    for c, d in zip((r, g, b), bg)))
        sheet.paste(tile.resize((cell, cell), Image.NEAREST),
                    (pad + i * (cell + pad), pad))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", default="mockup",
                        choices=("mockup", "render"),
                        help="trace straight from the design mockup (default) "
                             "or from the separately generated renders")
    parser.add_argument("--output", type=Path,
                        default=root / "main" / "IconTextures.h")
    parser.add_argument("--sheet", nargs="?", const="", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.source == "mockup":
        icons, report = build_mockup()
        text = emit(icons)
        print(f"{'icon':14s} {'source':18s} {'content':>9s} "
              f"{'dominant':>16s} {'expected':>16s}")
        for name, src, content, got, want, _err in report:
            print(f"{name:14s} {src:18s} {content[0]:3d}x{content[1]:<5d} "
                  f"{str(got):>16s} {str(want):>16s}")
    else:
        text, icons, report = build()

        print(f"{'icon':14s} {'source':18s} {'content':>9s} "
              f"{'dominant':>16s} {'expected':>16s} {'err':>4s}")
        worst = 0
        for name, filename, content, got, want, err in report:
            worst = max(worst, err)
            print(f"{name:14s} {filename:18s} {content[0]:3d}x{content[1]:<5d} "
                  f"{str(got):>16s} {str(want):>16s} {err:4d}")
        if worst > 48:
            print(f"\nWARNING: worst channel deviation {worst} -- check pairing",
                  file=sys.stderr)

    if args.check:
        if not args.output.exists():
            print(f"missing {args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} is out of date", file=sys.stderr)
            return 1
        print(f"\n{args.output} up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    total = sum(len(w) * 2 + len(a) for w, a in icons.values())
    print(f"\nwrote {args.output}  ({len(text)} chars source, "
          f"{total} bytes data)")

    if args.sheet is not None:
        out = Path(args.sheet) if args.sheet else \
            Path(r"C:\Users\86147\Documents\Codex\2026-09-16"
                 r"\https-github-com-digitsisyph-codex-micro\outputs"
                 f"\\icon-textures-{args.source}.png")
        print(f"wrote {write_sheet(icons, out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
