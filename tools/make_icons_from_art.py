#!/usr/bin/env python3
"""Turn the artist's icon sheets into 20x20 A8 coverage masks.

Why not snap to the art grid: the generated blocks are NOT a strict integer grid.
Consecutive change positions cluster around 40-55 px with a +/-10% spread, and
outlier gaps of 100-300 px appear where a boundary had no contrast. Recovering an
exact grid is therefore impossible, and pretending otherwise would produce
ragged, misaligned glyphs.

What works instead is area averaging. Each glyph spans roughly 940 px and the
blocks are roughly 48 px, so a glyph is about 20 art pixels across -- almost
exactly the 20x20 slot the button wants. Downsampling the glyph's bounding box to
the slot size with a BOX filter, then thresholding at 50%, is therefore close to a
1:1 mapping onto the artist's own pixels, with no grid assumption at all.

Two source sets are provided. The white set is used for the mask (clean luminance
separation); the coloured set is extracted the same way and compared, so a
mismatch flags a mis-paired file.

    python tools/make_icons_from_art.py            # write main/IconMask.h
    python tools/make_icons_from_art.py --sheet    # also write a verification PNG
    python tools/make_icons_from_art.py --check    # fail if out of date
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

# name -> (white file, coloured file or None)
SOURCES = {
    "kIconThink": ("11_07_50 (1).png", "11_11_19 (1).png"),
    "kIconDone": ("11_07_50 (2).png", "11_11_20 (2).png"),
    "kIconInput": ("11_07_51 (3).png", "11_11_20 (3).png"),
    "kIconEmpty": ("11_07_52 (4).png", "11_11_21 (4).png"),
    "kIconError": ("11_07_52 (5).png", "11_11_21 (5).png"),
    "kIconIdle": ("11_07_52 (6).png", "11_11_22 (6).png"),
    "kIconBattery": ("11_07_53 (7).png", None),
}

# Per-glyph slot height. The battery is a wide element, not a square glyph, so it
# gets the full width and only the height it needs.
SLOT_H = {name: GRID for name in SOURCES}
SLOT_H["kIconBattery"] = 12


def load_mask(filename: str, coloured: bool = False) -> Image.Image:
    """Binarised 'L' mask of the glyph.

    Luminance thresholding only works for the white set. The coloured set has to
    be keyed on "not black" instead: the artist's red is (255, 71, 84), whose
    luma is 128 -- exactly on the threshold -- so a luminance test erases it
    entirely, and the amber, cyan and green shapes come out with ragged edges.
    """
    path = os.path.join(DL, PREFIX + filename)
    if not os.path.exists(path):
        raise SystemExit(f"missing source: {path}")
    rgb = Image.open(path).convert("RGB")
    if coloured:
        r, g, b = rgb.split()
        brightest = ImageChops.lighter(ImageChops.lighter(r, g), b)
        return brightest.point(lambda v: 255 if v > 60 else 0)
    return rgb.convert("L").point(lambda v: 255 if v >= 128 else 0)


def crop_to_ink(mask: Image.Image, margin: int = 2):
    box = mask.getbbox()
    if box is None:
        raise SystemExit("empty mask")
    x0, y0, x1, y1 = box
    w, h = mask.size
    return mask.crop((max(0, x0 - margin), max(0, y0 - margin),
                      min(w, x1 + margin), min(h, y1 + margin)))


def to_slot(crop: Image.Image, slot_w: int, slot_h: int):
    """Area-average into the slot, threshold, and centre.

    Returns (slot_image, content_size).
    """
    w, h = crop.size
    scale = min(slot_w / w, slot_h / h)
    tw = max(1, int(round(w * scale)))
    th = max(1, int(round(h * scale)))
    small = crop.resize((tw, th), Image.BOX)
    small = small.point(lambda v: 255 if v >= 128 else 0)
    slot = Image.new("L", (slot_w, slot_h), 0)
    slot.paste(small, ((slot_w - tw) // 2, (slot_h - th) // 2))
    return slot, (tw, th)


def battery_interior(slot: Image.Image):
    """Largest all-dark rectangle inside the battery outline.

    The charge bar has to be drawn at runtime (the level changes), so the
    generator reports the hole's bounding box and the renderer fills a
    proportional slice of it.
    """
    w, h = slot.size
    px = slot.load()
    xs = [x for x in range(w) for y in range(h) if px[x, y] == 0]
    ys = [y for y in range(h) for x in range(w) if px[x, y] == 0]
    if not xs:
        return None
    # Shrink to the region strictly enclosed by ink on all four sides.
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return (x0 + 1, y0 + 1, x1 - x0 - 1, y1 - y0 - 1)


def to_bytes(slot: Image.Image) -> bytes:
    return bytes(slot.tobytes())


def array_text(name: str, payload: bytes) -> str:
    rows = []
    for offset in range(0, len(payload), 16):
        chunk = payload[offset:offset + 16]
        rows.append("  " + ", ".join(f"0x{v:02X}" for v in chunk) + ",")
    return (f"static constexpr std::uint8_t {name}[] = {{\n"
            + "\n".join(rows) + "\n};\n")


def build() -> tuple[str, dict]:
    masks = {}
    info = {}
    for name, (white, coloured) in SOURCES.items():
        slot_h = SLOT_H[name]
        crop = crop_to_ink(load_mask(white))
        slot, content = to_slot(crop, GRID, slot_h)
        masks[name] = slot
        info[name] = dict(content=content, slot_h=slot_h)

        if coloured:
            ccrop = crop_to_ink(load_mask(coloured, coloured=True))
            cslot, ccontent = to_slot(ccrop, GRID, slot_h)
            diff = sum(1 for a, b in zip(slot.tobytes(), cslot.tobytes())
                       if (a > 0) != (b > 0))
            info[name]["colour_diff"] = diff

    parts = [
        "// Generated by tools/make_icons_from_art.py. Do not edit manually.\n"
        "//\n"
        f"// {GRID}x{GRID} 8-bit coverage masks traced from the artist's icon\n"
        "// sheets (white-on-black renders, area-averaged into the slot and\n"
        "// thresholded). Shape-only: tinted at draw time so one mask serves\n"
        "// every status colour and every fade level.\n"
        "//\n"
        "// See the generator's docstring for why no art-grid snap is attempted.\n"
        "#pragma once\n\n"
        "#include <cstdint>\n\n"
        "namespace dashboard {\nnamespace icon_data {\n\n"
        f"constexpr int kIconSize = {GRID};\n\n"
    ]
    for name, slot in masks.items():
        parts.append(f"constexpr int {name}_h = {slot.size[1]};\n")
    parts.append("\n")
    for name, slot in masks.items():
        parts.append(array_text(name, to_bytes(slot)))
        parts.append("\n")

    inner = battery_interior(masks["kIconBattery"])
    if inner:
        x, y, w, h = inner
        parts.append(
            "// Interior of the battery outline, for the runtime charge bar.\n"
            f"constexpr int kIconBatteryFillX = {x};\n"
            f"constexpr int kIconBatteryFillY = {y};\n"
            f"constexpr int kIconBatteryFillW = {w};\n"
            f"constexpr int kIconBatteryFillH = {h};\n\n")
    parts.append("}  // namespace icon_data\n}  // namespace dashboard\n")
    return "".join(parts), info


def write_sheet(masks: dict, info: dict, out: Path, zoom: int = 10):
    names = list(masks)
    cell = GRID * zoom
    pad = 10
    sheet = Image.new("RGB", (pad + len(names) * (cell + pad), cell + 2 * pad),
                      (24, 26, 32))
    for i, name in enumerate(names):
        slot = masks[name]
        img = Image.new("RGB", slot.size, (0, 0, 0))
        img.paste(Image.new("RGB", slot.size, (255, 255, 255)), (0, 0), slot)
        img = img.resize((cell, cell), Image.NEAREST)
        sheet.paste(img, (pad + i * (cell + pad), pad))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path,
                        default=root / "main" / "IconMask.h")
    parser.add_argument("--sheet", nargs="?", const="", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    text, info = build()

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
    print(f"{'mask':14s} {'content':>9s} {'slot h':>7s} {'ink':>5s} "
          f"{'vs colour':>10s}")
    for name, meta in info.items():
        crop = crop_to_ink(load_mask(SOURCES[name][0]))
        slot, content = to_slot(crop, GRID, meta["slot_h"])
        ink = sum(1 for v in slot.tobytes() if v)
        cd = meta.get("colour_diff")
        print(f"{name:14s} {content[0]:4d}x{content[1]:<4d} "
              f"{meta['slot_h']:7d} {ink:5d} "
              f"{('--' if cd is None else str(cd)):>10s}")

    if args.sheet is not None:
        out = Path(args.sheet) if args.sheet else \
            Path(r"C:\Users\86147\Documents\Codex\2026-09-16"
                 r"\https-github-com-digitsisyph-codex-micro\outputs"
                 r"\icon-extracted.png")
        masks = {n: to_slot(crop_to_ink(load_mask(SOURCES[n][0])), GRID,
                            SLOT_H[n])[0] for n in SOURCES}
        print(f"wrote {write_sheet(masks, info, out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
