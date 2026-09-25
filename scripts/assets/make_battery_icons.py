#!/usr/bin/env python3
"""Generate the 5x2 battery icon textures from the artist's renders.

Each level comes in two flavours: idle (white-on-black, no bolt) and
charging (green fill + bolt). Both are rasterised into 22x12 RGB565 + A8
textures so the firmware can pick one with a single drawTexture() call.

The source PNGs are 1254x1254 black canvases with the battery centered.
We sample the actual ink (anything not near-black), crop to its bounding
box, area-average down to the slot size with a BOX filter, and emit the
two planes. No thresholding: the artist draws anti-aliased edges, and
thresholding erases the green-versus-white contrast in mid-level
charging icons where the bolt crosses the fill boundary.

    python scripts/assets/make_battery_icons.py            # write main/BatteryIcons.h
    python scripts/assets/make_battery_icons.py --sheet    # also write outputs/battery_icons.png
    python scripts/assets/make_battery_icons.py --check    # fail if out of date (CI)

Naming follows the firmware convention: kBatteryIdle1..kBatteryIdle5
map to 0-20%, 20-40%, ... 80-100% (rounded up), and kBatteryCharging1..5
are their charging counterparts.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

from PIL import Image

# 22 x wide x 12 tall: the existing layout reserves a 19-wide body + 3-wide
# nub, and 12 px of vertical height. Keeping the slot square in the
# source-ratio (1.76) would need 21x12, so we add 1 px of width for the
# body to better match the artist's 1.67 body aspect ratio.
SLOT_W = 22
SLOT_H = 12

# A radius around the bounding box to keep the chamfered corners intact.
CROP_MARGIN = 8

DL = r"D:\Downloads"
PREFIX = "ChatGPT Image 2026年9月23日 "

# 10 sources. Each tuple is the user's filename; the (n) suffix is the
# level (1=lowest, 5=full), not ChatGPT's generation counter.
SOURCES = {
    "kBatteryIdle1":     "20_26_41 (1).png",
    "kBatteryIdle2":     "20_26_42 (2).png",
    "kBatteryIdle3":     "20_26_42 (3).png",
    "kBatteryIdle4":     "20_26_43 (4).png",
    "kBatteryIdle5":     "20_26_44 (5).png",
    "kBatteryCharging1": "20_25_20 (1).png",
    "kBatteryCharging2": "20_25_20 (2).png",
    "kBatteryCharging3": "20_25_21 (3).png",
    "kBatteryCharging4": "20_25_22 (4).png",
    "kBatteryCharging5": "20_25_22 (5).png",
}


def rgb565_pack(r: int, g: int, b: int) -> int:
    """Pack RGB into RGB565, big-endian, matching the panel's byte order."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def crop_to_ink(im: Image.Image, margin: int = CROP_MARGIN) -> Image.Image:
    """Tight crop of the non-black ink, with a small safety margin."""
    rgb = im.convert("RGB")
    px = rgb.load()
    w, h = rgb.size
    xs: list[int] = []
    ys: list[int] = []
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if max(r, g, b) > 40:
                xs.append(x)
                ys.append(y)
    if not xs:
        raise SystemExit("empty image: no non-black pixels")
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return rgb.crop((max(0, x0 - margin), max(0, y0 - margin),
                     min(w, x1 + margin), min(h, y1 + margin)))


def to_slot(im: Image.Image, slot_w: int, slot_h: int) -> Image.Image:
    """Area-average into the slot. No threshold: edges stay smooth."""
    w, h = im.size
    scale = min(slot_w / w, slot_h / h)
    tw = max(1, int(round(w * scale)))
    th = max(1, int(round(h * scale)))
    small = im.resize((tw, th), Image.BOX)
    slot = Image.new("RGB", (slot_w, slot_h), (0, 0, 0))
    slot.paste(small, ((slot_w - tw) // 2, (slot_h - th) // 2))
    return slot


def split_alpha(rgb: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Return (rgb_with_alpha_white_bg, alpha_mask). Alpha is luminance."""
    # Background is black in the source, so luminance directly measures
    # coverage; an inverted value gives the alpha mask.
    l = rgb.convert("L")
    alpha = l
    return rgb, alpha


def quantise(rgb: Image.Image) -> Image.Image:
    """Snap every pixel to its RGB565 value, the way the panel sees it."""
    px = rgb.load()
    w, h = rgb.size
    out = Image.new("RGB", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            packed = rgb565_pack(r, g, b)
            r2 = (packed >> 8) & 0xF8
            g2 = (packed >> 3) & 0xFC
            b2 = (packed << 3) & 0xF8
            op[x, y] = (r2, g2, b2)
    return out


def emit_rgba(slot: Image.Image) -> tuple[list, bytes]:
    """Return (rgb565_words, alpha_bytes) for a slot.

    The colour plane is emitted as whole uint16 words rather than a byte
    stream. IconTextures.h declares its arrays the same way, and it matters:
    a uint8_t[] reinterpret_cast to uint16_t* has only 1-byte alignment, and
    Xtensa faults on a misaligned 16-bit load. Matching the existing
    convention removes the cast in drawBattery entirely.

    Word order is big-endian RGB565 ("panel order"), which is what
    Canvas::drawTexture expects -- it calls swap16() on each word before
    writing to the framebuffer.
    """
    quant = quantise(slot)
    _, alpha = split_alpha(quant)
    rpx = quant.load()
    w, h = quant.size
    words = []
    alpha_bytes = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            r, g, b = rpx[x, y]
            words.append(rgb565_pack(r, g, b))
            alpha_bytes[y * w + x] = alpha.getpixel((x, y))
    return words, bytes(alpha_bytes)


def array_text(name: str, payload: bytes, width: int = 16) -> str:
    rows = []
    for off in range(0, len(payload), width):
        chunk = payload[off:off + width]
        rows.append("  " + ", ".join(f"0x{v:02X}" for v in chunk) + ",")
    return (f"static constexpr std::uint8_t {name}[] = {{\n"
            + "\n".join(rows) + "\n};\n")


def array_text_words(name: str, words: list, width: int = 8) -> str:
    """A uint16_t array, 8 words per row -- the IconTextures.h layout."""
    rows = []
    for off in range(0, len(words), width):
        chunk = words[off:off + width]
        rows.append("  " + ", ".join(f"0x{v:04X}" for v in chunk) + ",")
    return (f"static constexpr std::uint16_t {name}[] = {{\n"
            + "\n".join(rows) + "\n};\n")


def build() -> tuple[str, str, dict]:
    """Return (c_header_text, python_module_text, info)."""
    slots = {}
    info = {}
    for name, fname in SOURCES.items():
        path = os.path.join(DL, PREFIX + fname)
        if not os.path.exists(path):
            raise SystemExit(f"missing source: {path}")
        src = Image.open(path)
        cropped = crop_to_ink(src)
        slot = to_slot(cropped, SLOT_W, SLOT_H)
        slots[name] = slot
        info[name] = dict(
            source=fname,
            crop_size=cropped.size,
            slot_size=slot.size,
        )
    parts = [
        "// Generated by scripts/assets/make_battery_icons.py. Do not edit manually.\n"
        "//\n"
        f"// {SLOT_W}x{SLOT_H} RGB565+A8 battery textures traced from the\n"
        "// artist's 5-level x 2-state renders. Indexed by (charging,\n"
        "// level): kBattery{Idle,Charging}{1..5} map to the 0-20%,\n"
        "// 20-40%, 40-60%, 60-80%, 80-100% bins respectively.\n"
        "//\n"
        "// Composite onto any destination with\n"
        "//     out = src + dst * (255 - a) / 255\n"
        "#pragma once\n\n"
        "#include <cstdint>\n\n"
        "namespace dashboard {\nnamespace battery_data {\n\n"
        f"constexpr int kBatteryIconW = {SLOT_W};\n"
        f"constexpr int kBatteryIconH = {SLOT_H};\n\n"
    ]
    # Also build the Python module side-by-side so pixel_preview.py and the
    # firmware share the exact same RGB words.
    py_parts = [
        "# Auto-generated by scripts/assets/make_battery_icons.py. Do not edit.\n"
        "\"\"\"Battery icon textures shared between firmware and preview.\n\n"
        "Each entry is (rgb565_words, alpha_bytes) for one level, matching\n"
        "main/BatteryIcons.h word for word so the preview cannot drift away\n"
        "from the panel.\n"
        "\"\"\"\n\n"
        "ICON_W = " + str(SLOT_W) + "\n"
        "ICON_H = " + str(SLOT_H) + "\n\n"
        "BATTERY_IDLE = [\n"
    ]
    for name, slot in slots.items():
        words, alpha_bytes = emit_rgba(slot)
        parts.append(array_text_words(name + "_rgb", words))
        parts.append("\n")
        parts.append(array_text(name + "_a", alpha_bytes))
        parts.append("\n")
        # Python side: same words, grouped by state.
        if name.startswith("kBatteryIdle") or name.startswith("kBatteryCharging"):
            py_parts.append("    (\n        [\n")
            for i in range(0, len(words), 8):
                py_parts.append("            " +
                                ", ".join(f"0x{v:04X}" for v in words[i:i+8]) + ",\n")
            py_parts.append("        ],\n        bytes([\n")
            for i in range(0, len(alpha_bytes), 12):
                py_parts.append("            " +
                                ", ".join(f"0x{b:02X}" for b in alpha_bytes[i:i+12]) + ",\n")
            py_parts.append("        ])),\n")
        if name == "kBatteryIdle5":
            py_parts.append("]\n\nBATTERY_CHARGING = [\n")
    parts.append("}  // namespace battery_data\n}  // namespace dashboard\n")
    py_parts.append("]\n")
    return "".join(parts), "".join(py_parts), info


def write_sheet(slots: dict, out: Path, zoom: int = 8) -> Path:
    """Write a verification PNG showing all 10 icons in two rows."""
    names = list(slots)
    cell_w = SLOT_W * zoom
    cell_h = SLOT_H * zoom
    pad = 8
    sheet_w = pad + len(names) * (cell_w + pad)
    sheet_h = pad * 3 + cell_h * 2
    sheet = Image.new("RGB", (sheet_w, sheet_h), (8, 14, 32))
    for i, name in enumerate(names):
        slot = slots[name]
        out_img = Image.new("RGB", (SLOT_W, SLOT_H), (0, 0, 0))
        # Composite onto a mid-gray backdrop so the alpha channel is
        # visible; otherwise black-on-black hides any alpha error.
        backdrop = Image.new("RGB", (SLOT_W, SLOT_H), (40, 60, 80))
        for y in range(SLOT_H):
            for x in range(SLOT_W):
                r, g, b = slot.getpixel((x, y))
                a = (max(r, g, b)) / 255.0
                br, bg, bb = backdrop.getpixel((x, y))
                out_img.putpixel((x, y), (
                    int(r * a + br * (1 - a)),
                    int(g * a + bg * (1 - a)),
                    int(b * a + bb * (1 - a)),
                ))
        big = out_img.resize((cell_w, cell_h), Image.NEAREST)
        row = 0 if "Idle" in name else 1
        sheet.paste(big, (pad + i * (cell_w + pad), pad + row * (cell_h + pad)))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path,
                        default=root / "main" / "BatteryIcons.h")
    parser.add_argument("--py-output", type=Path,
                        default=root / "scripts" / "assets" / "battery_icon_data.py")
    parser.add_argument("--sheet", nargs="?", const="", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    c_text, py_text, info = build()

    if args.check:
        ok = True
        if not args.output.exists():
            print(f"missing {args.output}", file=sys.stderr)
            ok = False
        elif args.output.read_text(encoding="utf-8") != c_text:
            print(f"{args.output} is out of date", file=sys.stderr)
            ok = False
        if not args.py_output.exists():
            print(f"missing {args.py_output}", file=sys.stderr)
            ok = False
        elif args.py_output.read_text(encoding="utf-8") != py_text:
            print(f"{args.py_output} is out of date", file=sys.stderr)
            ok = False
        if not ok:
            return 1
        print(f"{args.output} up to date")
        print(f"{args.py_output} up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(c_text, encoding="utf-8")
    args.py_output.parent.mkdir(parents=True, exist_ok=True)
    args.py_output.write_text(py_text, encoding="utf-8")
    print(f"wrote {args.output}")
    print(f"wrote {args.py_output}")
    for name, meta in info.items():
        print(f"  {name:22s} source={meta['source']} "
              f"crop={meta['crop_size']} slot={meta['slot_size']}")

    if args.sheet is not None:
        out = Path(args.sheet) if args.sheet else \
            Path(r"C:\Users\86147\Documents\Codex\2026-09-16"
                 r"\https-github-com-digitsisyph-codex-micro\outputs"
                 r"\battery_icons.png")
        slots = {}
        for n, f in SOURCES.items():
            slots[n] = to_slot(crop_to_ink(Image.open(os.path.join(DL, PREFIX + f))),
                               SLOT_W, SLOT_H)
        print(f"wrote {write_sheet(slots, out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())