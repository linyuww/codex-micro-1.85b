#!/usr/bin/env python3
"""Offline renderer for the 360x360 Codex Micro dashboard.

Mirrors main/gfx.cpp and main/dashboard_ui.h closely enough to eyeball the
layout on a PC: it parses the same VLW blobs, uses the same primitives and the
same dial geometry, and writes a PNG.

Its real job is regression checking. Every string the dial draws is measured,
and any text pixel that lands outside the dial face (or outside the 180 px
round panel) is reported as a spill. Run it after touching dashboard_ui.h.

    python scripts/assets/dial_preview.py
"""

import math
import os
import re
import struct
import sys
import zlib

WIDTH = 360
HEIGHT = 360
CENTER_X = 180
CENTER_Y = 180
AGENT_RADIUS = 42
SEND_RADIUS = 80
DIAL_FACE_RADIUS = SEND_RADIUS - 8 - 6
DIAL_TEXT_RADIUS = DIAL_FACE_RADIUS - 4
PANEL_RADIUS = 180

AGENT_CENTERS = [(180, 56), (287, 118), (287, 242), (180, 304), (73, 242), (73, 118)]

BACKGROUND = 0x0000
PANEL = 0x10C4
PANEL_PRESSED = 0x1946
TEXT = 0xEF7E
MUTED = 0x7C53
TRACK = 0x2187
ACCENT = 0x16B6
VOICE = 0x5F17
WARNING = 0xFDA9
DANGER = 0xFAED


def rgb565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def to_rgb888(color):
    r = (color >> 11) & 0x1F
    g = (color >> 5) & 0x3F
    b = color & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


# --------------------------------------------------------------------- fonts


class Font:
    def __init__(self, data):
        def be32(offset):
            return int.from_bytes(data[offset:offset + 4], "big")

        self.data = data
        self.glyph_count = be32(0)
        self.size = be32(8)
        self.ascent = be32(16)
        self.descent = be32(20)
        base = 24 + self.glyph_count * 28
        self.glyphs = {}
        offset = 0
        for i in range(self.glyph_count):
            record = 24 + i * 28
            code = be32(record)
            height, width, advance, top, left = (be32(record + 4 + 4 * k) for k in range(5))
            self.glyphs[code] = (width, height, advance, top, left, base + offset)
            offset += width * height
        cap = self.glyphs.get(ord("A"))
        self.cap_height = cap[1] if cap else self.size
        self._fallback_advance = self.glyphs[ord("!")][2] if ord("!") in self.glyphs else self.size // 2

    def baseline_offset(self):
        return (self.ascent - self.descent) // 2

    def text_height(self):
        return self.cap_height

    def advance(self, code):
        glyph = self.glyphs.get(code)
        return glyph[2] if glyph else self._fallback_advance

    def text_width(self, text):
        return sum(self.advance(ord(ch)) for ch in text)


def load_fonts(root):
    source = open(os.path.join(root, "main", "SpaceMonoVlw.h"), encoding="utf-8").read()
    fonts = {}
    for name in ("kSpaceMono18Vlw", "kSpaceMono46Vlw"):
        match = re.search(r"static constexpr std::uint8_t " + name + r"\[\] = \{(.*?)\};", source, re.S)
        blob = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", match.group(1)))
        fonts[name] = Font(blob)
    return fonts["kSpaceMono18Vlw"], fonts["kSpaceMono46Vlw"]


# -------------------------------------------------------------------- canvas


class Canvas:
    def __init__(self):
        self.pixels = bytearray(WIDTH * HEIGHT * 3)
        self.spills = []

    def blend(self, x, y, color, coverage=1.0):
        if x < 0 or y < 0 or x >= WIDTH or y >= HEIGHT or coverage <= 0.0:
            return
        index = (y * WIDTH + x) * 3
        sr, sg, sb = to_rgb888(color)
        if coverage >= 1.0:
            self.pixels[index] = sr
            self.pixels[index + 1] = sg
            self.pixels[index + 2] = sb
            return
        dr, dg, db = self.pixels[index], self.pixels[index + 1], self.pixels[index + 2]
        self.pixels[index] = int(dr + (sr - dr) * coverage + 0.5)
        self.pixels[index + 1] = int(dg + (sg - dg) * coverage + 0.5)
        self.pixels[index + 2] = int(db + (sb - db) * coverage + 0.5)

    def fill_screen(self, color):
        r, g, b = to_rgb888(color)
        for i in range(WIDTH * HEIGHT):
            self.pixels[i * 3] = r
            self.pixels[i * 3 + 1] = g
            self.pixels[i * 3 + 2] = b

    def fill_circle(self, cx, cy, radius, color):
        outer = radius + 0.5
        inner = radius - 0.5
        for y in range(cy - radius, cy + radius + 1):
            dy = y - cy
            outer_sq = outer * outer - dy * dy
            if outer_sq <= 0:
                continue
            outer_half = math.sqrt(outer_sq)
            left = math.ceil(cx - outer_half)
            right = math.floor(cx + outer_half)
            inner_sq = inner * inner - dy * dy
            inner_half = math.sqrt(inner_sq) if inner_sq > 0 else -1.0
            solid_left = math.ceil(cx - inner_half) if inner_half > 0 else 0
            solid_right = math.floor(cx + inner_half) if inner_half > 0 else -1
            for x in range(left, right + 1):
                if solid_left <= x <= solid_right:
                    self.blend(x, y, color)
                else:
                    dx = x - cx
                    self.blend(x, y, color, max(0.0, min(1.0, outer - math.sqrt(dx * dx + dy * dy))))

    def fill_ring_arc(self, cx, cy, r_outer, r_inner, start, sweep, color):
        if sweep <= 0:
            return
        full = sweep >= 359.5
        sweep = min(sweep, 360.0)
        outer = r_outer + 0.5
        inner = r_inner - 0.5
        outer_sq = outer * outer
        inner_sq = inner * inner if inner > 0 else 0.0
        for y in range(cy - r_outer - 1, cy + r_outer + 2):
            dy = y - cy
            for x in range(cx - r_outer - 1, cx + r_outer + 2):
                dx = x - cx
                dist_sq = dx * dx + dy * dy
                if dist_sq > outer_sq or (inner > 0 and dist_sq < inner_sq):
                    continue
                dist = math.sqrt(dist_sq)
                coverage = max(0.0, min(1.0, outer - dist))
                if inner > 0:
                    coverage = min(coverage, max(0.0, min(1.0, dist - inner)))
                if not full:
                    angle = math.degrees(math.atan2(dx, -dy))
                    if angle < 0:
                        angle += 360.0
                    offset = angle - start
                    if offset < 0:
                        offset += 360.0
                    if offset > sweep:
                        continue
                    radius = max(dist, 1.0)
                    coverage = min(coverage, max(0.0, min(1.0, offset * math.pi / 180.0 * radius + 0.5)))
                    coverage = min(coverage, max(0.0, min(1.0, (sweep - offset) * math.pi / 180.0 * radius + 0.5)))
                if coverage > 0:
                    self.blend(x, y, color, coverage)

    def fill_round_rect(self, x, y, w, h, radius, color):
        radius = min(radius, w // 2, h // 2)
        left = x + radius
        right = x + w - radius
        top = y + radius
        bottom = y + h - radius
        for py in range(y, y + h):
            fy = py + 0.5
            dy = max(top - fy, fy - bottom, 0.0)
            for px in range(x, x + w):
                fx = px + 0.5
                dx = max(left - fx, fx - right, 0.0)
                distance = math.sqrt(dx * dx + dy * dy) - radius
                coverage = 0.5 - distance
                if coverage > 0:
                    self.blend(px, py, color, min(coverage, 1.0))

    def fill_triangle(self, x0, y0, x1, y1, x2, y2, color):
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 0.0001:
            return
        for py in range(min(y0, y1, y2), max(y0, y1, y2) + 1):
            fy = py + 0.5
            for px in range(min(x0, x1, x2), max(x0, x1, x2) + 1):
                fx = px + 0.5
                w0 = (x1 - x0) * (fy - y0) - (fx - x0) * (y1 - y0)
                w1 = (x2 - x1) * (fy - y1) - (fx - x1) * (y2 - y1)
                w2 = (x0 - x2) * (fy - y2) - (fx - x2) * (y0 - y2)
                inside = (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0)
                if inside:
                    self.blend(px, py, color)

    # ---------------------------------------------------------------- text

    def _glyph_pixels(self, font, code, pen_x, baseline, scale, watch=None):
        glyph = font.glyphs.get(code)
        if glyph is None:
            return None
        width, height, advance, top, left, bitmap_offset = glyph
        dst_w = max(1, round(width * scale))
        dst_h = max(1, round(height * scale))
        top_y = baseline - round(top * scale)
        left_x = pen_x + round(left * scale)
        for row in range(dst_h):
            src_row = row * height // dst_h
            py = top_y + row
            for col in range(dst_w):
                src_col = col * width // dst_w
                alpha = font.data[bitmap_offset + src_row * width + src_col]
                if alpha == 0:
                    continue
                yield left_x + col, top_y + row, alpha / 255.0

    def draw_text(self, font, text, x, y, color, datum="center", max_width=None, watch=None):
        if not text:
            return
        width = font.text_width(text)
        scale = 1.0
        if max_width is not None and width > max_width > 0:
            scale = max(0.66, max_width / width)
        baseline = y + round(font.baseline_offset() * scale)
        pen_x = x
        if datum == "center":
            pen_x = x - round(font.text_width(text) * scale * 0.5)
        for ch in text:
            code = ord(ch)
            glyph = font.glyphs.get(code)
            if glyph is not None and glyph[0] > 0 and glyph[1] > 0:
                for px, py, alpha in self._glyph_pixels(font, code, pen_x, baseline, scale):
                    self.blend(px, py, color, alpha)
                    if watch is not None:
                        watch.add(px, py)
            pen_x += round((glyph[2] if glyph else font.advance(code)) * scale)
        return scale


# ------------------------------------------------------------- spill report


class SpillWatch:
    """Collects text pixels and reports any that fall outside the dial face."""

    def __init__(self, name, limit_radius):
        self.name = name
        self.limit = limit_radius
        self.points = []
        self.max_radius = 0.0
        self.box = None

    def add(self, x, y):
        self.points.append((x, y))

    def evaluate(self):
        if not self.points:
            return None
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        self.box = (min(xs), min(ys), max(xs), max(ys))
        worst = 0.0
        for x, y in self.points:
            worst = max(worst, math.hypot(x - CENTER_X, y - CENTER_Y))
        self.max_radius = worst
        return worst - self.limit


def dial_budget(dy, height):
    reach = abs(dy) + height // 2 + 1
    if reach >= DIAL_TEXT_RADIUS:
        return 0
    return 2 * int(math.sqrt(DIAL_TEXT_RADIUS ** 2 - reach ** 2))


# ------------------------------------------------------------------- render


def draw_dial(canvas, state):
    canvas.fill_ring_arc(CENTER_X, CENTER_Y, SEND_RADIUS, SEND_RADIUS - 8, 0, 360, TRACK)
    if state["quota_available"]:
        remaining = max(0.0, min(100.0, state["remaining"]))
        color = rgb565(116, 85, 39) if state["stale"] else (ACCENT if remaining > 20 else WARNING)
        canvas.fill_ring_arc(CENTER_X, CENTER_Y, SEND_RADIUS, SEND_RADIUS - 8, 0, remaining * 3.6, color)
    canvas.fill_round_rect(CENTER_X - 2, CENTER_Y - SEND_RADIUS, 4, 12, 2, BACKGROUND)
    canvas.fill_round_rect(CENTER_X + SEND_RADIUS - 8 - 2, CENTER_Y - 2, 12, 4, 2, BACKGROUND)
    canvas.fill_round_rect(CENTER_X - 2, CENTER_Y + SEND_RADIUS - 8 - 2, 4, 12, 2, BACKGROUND)
    canvas.fill_round_rect(CENTER_X - SEND_RADIUS, CENTER_Y - 2, 12, 4, 2, BACKGROUND)
    canvas.fill_circle(CENTER_X, CENTER_Y, SEND_RADIUS - 8 - 3, TRACK)
    canvas.fill_circle(CENTER_X, CENTER_Y, SEND_RADIUS - 8 - 6, BACKGROUND)


def draw_agents(canvas, state):
    for i, (px, py) in enumerate(AGENT_CENTERS):
        status = state["threads"][i]
        if status is None:
            canvas.fill_circle(px, py, AGENT_RADIUS, TRACK)
            canvas.fill_circle(px, py, AGENT_RADIUS - 2, PANEL)
            continue
        canvas.fill_circle(px, py, AGENT_RADIUS, status)
        canvas.fill_circle(px, py, AGENT_RADIUS - 4, status)


def draw_battery_row(canvas, small, state, cy, report):
    icon_w, icon_h, gap = 18, 10, 4
    budget = dial_budget(cy - CENTER_Y, icon_h + 2)
    if budget <= icon_w:
        return
    watch = SpillWatch("battery", DIAL_FACE_RADIUS)
    battery = max(-1, min(100, state["battery"]))
    color = MUTED if battery < 0 else (ACCENT if state["docked"] else (WARNING if battery <= 20 else TEXT))
    label = "--%" if battery < 0 else "%d%%" % battery
    text_budget = budget - icon_w - gap
    text_width = min(small.text_width(label), text_budget)
    left = CENTER_X - (icon_w + gap + text_width) // 2
    top = cy - icon_h // 2
    canvas.fill_round_rect(left, top, icon_w, icon_h, 2, color)
    canvas.fill_round_rect(left + 2, top + 2, icon_w - 4, icon_h - 4, 1, BACKGROUND)
    canvas.fill_round_rect(left + icon_w, cy - 2, 2, 4, 1, color)
    if battery > 0:
        fill = max(2, (icon_w - 6) * battery // 100)
        canvas.fill_round_rect(left + 3, top + 3, fill, icon_h - 6, 1, color)
    if state["charging"]:
        bx = left + icon_w // 2
        canvas.fill_triangle(bx + 1, top + 1, bx - 3, cy + 1, bx, cy + 1, BACKGROUND)
        canvas.fill_triangle(bx, cy, bx + 3, cy, bx - 2, top + icon_h - 1, BACKGROUND)
    watch = SpillWatch("battery:" + label, DIAL_FACE_RADIUS)
    canvas.draw_text(small, label, left + icon_w + gap, cy, color, datum="left",
                     max_width=text_budget, watch=watch)
    report.append(watch)


def draw_dial_text(canvas, small, large, state, report_sink):
    draw_battery_row(canvas, small, state, CENTER_Y - 46, report_sink)

    label = {"live": "CODEX LIVE", "ble": "BLE ONLY", "offline": "OFFLINE"}[state["link"]]
    color = {"live": VOICE, "ble": rgb565(66, 146, 245), "offline": DANGER}[state["link"]]
    watch = SpillWatch("link:" + label, DIAL_FACE_RADIUS)
    canvas.draw_text(small, label, CENTER_X, CENTER_Y - 26, color, max_width=dial_budget(-26, small.text_height()), watch=watch)
    report_sink.append(watch)

    if state["stale"]:
        detail, detail_color = "STALE", WARNING
    elif not state["quota_available"]:
        detail, detail_color = ("WAITING" if state["link"] == "offline" else "NO QUOTA"), MUTED
    else:
        detail, detail_color = state["reset_text"], TEXT
    watch = SpillWatch("detail:" + detail, DIAL_FACE_RADIUS)
    canvas.draw_text(small, detail, CENTER_X, CENTER_Y + 42, detail_color, max_width=dial_budget(42, small.text_height()), watch=watch)
    report_sink.append(watch)

    value = "%.0f%%" % state["remaining"] if state["quota_available"] else "--"
    watch = SpillWatch("quota:" + value, DIAL_FACE_RADIUS)
    canvas.draw_text(large, value, CENTER_X, CENTER_Y + 6, MUTED if state["stale"] else TEXT,
                     max_width=dial_budget(6, large.text_height()), watch=watch)
    report_sink.append(watch)


def draw_transient(canvas, small, state):
    if not state.get("banner"):
        return
    message, color = state["banner"]
    w, h = 150, 46
    canvas.fill_round_rect(CENTER_X - w // 2, CENTER_Y - h // 2, w, h, 14, BACKGROUND)
    canvas.fill_round_rect(CENTER_X - w // 2, CENTER_Y - h // 2, w, h, 14, color)
    canvas.fill_round_rect(CENTER_X - w // 2 + 4, CENTER_Y - h // 2 + 4, w - 8, h - 8, 11, PANEL)
    canvas.draw_text(small, message, CENTER_X, CENTER_Y + 1, color, max_width=w - 24)


def render(state, small, large):
    canvas = Canvas()
    canvas.fill_screen(BACKGROUND)
    reports = []
    draw_dial(canvas, state)
    draw_agents(canvas, state)
    draw_dial_text(canvas, small, large, state, reports)
    draw_transient(canvas, small, state)
    for i, (px, py) in enumerate(AGENT_CENTERS):
        canvas.draw_text(small, "A%d" % (i + 1), px, py - 3, TEXT if state["threads"][i] else MUTED)
    return canvas, [r for r in reports if isinstance(r, SpillWatch)]


def write_png(path, canvas):
    raw = bytearray()
    for y in range(HEIGHT):
        raw.append(0)
        raw.extend(canvas.pixels[y * WIDTH * 3:(y + 1) * WIDTH * 3])

    def chunk(tag, data):
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)


SCENARIOS = [
    ("live-72", dict(link="live", quota_available=True, remaining=72.0, stale=False,
                     reset_text="5H 12M", battery=86, charging=True, docked=True,
                     threads=[None, 0x4292F5, None, 0x2BC96E, None, 0xF7AC42])),
    ("live-100-long", dict(link="live", quota_available=True, remaining=100.0, stale=False,
                           reset_text="6D 23H", battery=100, charging=False, docked=False,
                           threads=[0xB7C2CD] * 6)),
    ("offline", dict(link="offline", quota_available=False, remaining=0.0, stale=False,
                     reset_text="--", battery=-1, charging=False, docked=False,
                     threads=[None] * 6)),
    ("stale", dict(link="ble", quota_available=True, remaining=8.0, stale=True,
                   reset_text="12M", battery=14, charging=False, docked=False,
                   threads=[0xF55A68, None, None, None, None, None])),
    ("listening", dict(link="live", quota_available=True, remaining=42.0, stale=False,
                       reset_text="3H 04M", battery=57, charging=False, docked=False,
                       threads=[None] * 6, banner=("LISTENING", ACCENT))),
    # Mirrors a real `windows_companion.py --once` snapshot: the weekly window
    # is exhausted (used 100% -> remaining 0%) while the 5-hour window still has
    # room. 0% here is correct data, not a missing update, so the value must
    # render in kText (not kMuted) with the reset countdown still live.
    ("live-real-weekly-0", dict(link="live", quota_available=True, remaining=0.0,
                                stale=False, reset_text="4H 20M", battery=100,
                                charging=True, docked=True,
                                threads=[None, 0x4292F5, None, 0x2BC96E, None, 0xF7AC42])),
]


def main():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    small, large = load_fonts(root)
    out_dir = os.path.join(root, "scripts", "assets", "preview")
    os.makedirs(out_dir, exist_ok=True)

    failures = 0
    print("font 18px: advance=%d cap=%d   font 46px: advance=%d cap=%d"
          % (small.advance(ord("A")), small.cap_height, large.advance(ord("A")), large.cap_height))
    print("dial face radius=%d  text radius=%d" % (DIAL_FACE_RADIUS, DIAL_TEXT_RADIUS))
    print("-" * 72)
    for name, state in SCENARIOS:
        canvas, reports = render(state, small, large)
        write_png(os.path.join(out_dir, name + ".png"), canvas)
        worst = -999.0
        for report in reports:
            over = report.evaluate()
            if over is None:
                continue
            worst = max(worst, over)
            flag = "OK " if over <= 0 else "SPILL"
            if over > 0:
                failures += 1
            print("%-6s %-18s box=%s max_r=%.1f overhang=%+.1f"
                  % (flag, report.name, report.box, report.max_radius, over))
        print("  -> %s (worst overhang %+.1f px)" % (name, worst))
    print("-" * 72)
    print("SPILLS: %d" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
