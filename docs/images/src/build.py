#!/usr/bin/env python3
"""Rebuild the four README diagrams in docs/images/.

Style: dark blueprint plate. All illustration, including arrows, connectors and the
empty label tags, comes from codex-image (prompts/ -> scenes/). This script only
adds typography (Inter, JetBrains Mono) and single-color vector logos, placed into
the empty tags and sockets the scenes leave for them. No word is model-drawn.

Usage (from the repo root):
    python3 -m venv /tmp/dvenv && /tmp/dvenv/bin/pip install pillow cairosvg
    sh docs/images/src/fetch-fonts.sh
    /tmp/dvenv/bin/python docs/images/src/build.py

Coordinates are in the 1536x1024 scene space. If you redraw a scene, re-measure
its tags and sockets.
"""
import io
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw, ImageFont

SRC = Path(__file__).resolve().parent
OUT = SRC.parent
FONTS = SRC / "fonts"

W = 1600
K = W / 1536  # scene -> output scale
SS = 2  # supersampling for the type layer
QCOLORS = 192  # palette size; keeps each PNG well under 600 KB

INK = (236, 240, 244)
MUTED = (150, 162, 178)
ACCENT = (255, 107, 53)
GROK, CLAUDE, OPENAI = "grok", "claude", "openai"


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), round(size * K * SS))


def sans(size, weight="SemiBold"):
    return font(f"Inter-{weight}.ttf", size)


def mono(size, weight="Medium"):
    return font(f"JetBrainsMono-{weight}.ttf", size)


def hexcol(c):
    return "#%02x%02x%02x" % c


class Fig:
    """Scene + type layer. All public coordinates are scene coordinates."""

    def __init__(self, scene):
        im = Image.open(SRC / "scenes" / f"{scene}.webp").convert("RGB")
        self.name = scene
        self.scene = im
        self.h = round(im.height * K)
        self.base = im.resize((W, self.h), Image.LANCZOS).convert("RGBA")
        self.ov = Image.new("RGBA", (W * SS, self.h * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.ov)

    def P(self, x, y):
        return (x * K * SS, y * K * SS)

    def bg_at(self, x, y):
        return self.scene.getpixel((int(x), int(y)))

    def width(self, s, f, track=0):
        return (f.getlength(s) + track * K * SS * max(0, len(s) - 1)) / (K * SS)

    def text(self, xy, s, f, color=INK, anchor="la", track=0):
        x, y = xy
        if not track:
            self.d.text(self.P(x, y), s, font=f, fill=color, anchor=anchor)
            return
        total = self.width(s, f, track)
        if anchor[0] == "m":
            x -= total / 2
        elif anchor[0] == "r":
            x -= total
        cx, cy = self.P(x, y)
        for ch in s:
            self.d.text((cx, cy), ch, font=f, fill=color, anchor="l" + anchor[1])
            cx += f.getlength(ch) + track * K * SS

    def clear(self, box, inset=4):
        """Blank a tag's interior with its own fill color, so only our type shows."""
        x0, y0, x1, y1 = box
        c = self.bg_at((x0 + x1) / 2, y0 + inset + 2)
        self.d.rectangle([self.P(x0 + inset, y0 + inset), self.P(x1 - inset, y1 - inset)], fill=c + (255,))

    def tag(self, box, s, size=34, kind="sans", color=INK, pad=16, track=0, logos=None):
        """Fit text into an empty tag. Optional logos sit right-aligned inside the tag."""
        x0, y0, x1, y1 = box
        self.clear(box)
        avail = x1 - x0 - 2 * pad
        px = (y1 - y0) * 0.52
        lw = len(logos) * px + (len(logos) - 1) * 14 + 20 if logos else 0
        avail -= lw
        while True:
            f = sans(size) if kind == "sans" else mono(size)
            if self.width(s, f, track) <= avail or size <= 12:
                break
            size -= 1
        cy = (y0 + y1) / 2
        if logos:
            self.text((x0 + pad + 4, cy), s, f, color, "lm", track)
            x = x1 - pad - (lw - 20)
            for n in logos:
                self.logo(n, (x, cy - px / 2), px)
                x += px + 14
        else:
            self.text(((x0 + x1) / 2, cy), s, f, color, "mm", track)

    def logo(self, name, xy, px, color=INK):
        svg = (SRC / "logos" / f"{name}.svg").read_text().replace("currentColor", hexcol(color))
        if "fill=" not in svg.split(">")[0]:
            svg = svg.replace("<svg ", f'<svg fill="{hexcol(color)}" ', 1)
        size = round(px * K * SS)
        png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        x, y = self.P(*xy)
        self.ov.alpha_composite(im, (round(x), round(y)))

    def logo_in(self, box, name, frac=0.66):
        """Center a logo in an empty square socket."""
        x0, y0, x1, y1 = box
        self.clear(box, 3)
        px = min(x1 - x0, y1 - y0) * frac
        self.logo(name, ((x0 + x1 - px) / 2, (y0 + y1 - px) / 2), px)

    def badge(self, c, r, s):
        """Number inside an empty circular badge."""
        x, y = c
        col = self.bg_at(x, y)
        self.d.ellipse([self.P(x - r + 4, y - r + 4), self.P(x + r - 4, y + r - 4)], fill=col + (255,))
        self.text((x, y), s, mono(24), ACCENT, "mm")

    def pill(self, c, s, size=17, color=ACCENT):
        """Small mono note on its own plate, used over an arrow or empty background."""
        x, y = c
        f = mono(size)
        w = self.width(s, f, 1.5)
        col = self.bg_at(x - w / 2 - 20, y - size)
        self.d.rectangle([self.P(x - w / 2 - 10, y - size * 0.85), self.P(x + w / 2 + 10, y + size * 0.85)], fill=col + (255,))
        self.text((x, y), s, f, color, "mm", 1.5)

    def note(self, xy, s, size=17, color=MUTED, anchor="ls", logo=None):
        """Small mono annotation on empty background, optionally led by a logo."""
        x, y = xy
        if logo:
            self.logo(logo, (x, y - size * 1.15), size * 1.4)
            x += size * 1.4 + 10
        self.text((x, y), s, mono(size), color, anchor, 1.5)

    def title(self, n, s, xy=(64, 72)):
        x, y = xy
        self.text((x, y), f"FIG. {n:02d}", mono(19), ACCENT, "ls", 3)
        self.text((x, y + 58), s, sans(52), INK, "ls")

    def save(self):
        ov = self.ov.resize((W, self.h), Image.LANCZOS)
        out = self.base.copy()
        out.alpha_composite(ov)
        out = out.convert("RGB")
        path = OUT / f"{self.name}.png"
        out.quantize(colors=QCOLORS, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(path, optimize=True)
        print(f"docs/images/{path.name}  {out.width}x{out.height}  {path.stat().st_size // 1024} KB")


def who_does_what():
    f = Fig("who-does-what")
    f.title(1, "Who does what")
    f.tag((390, 432, 460, 460), "ASK", 17, "mono", ACCENT, pad=6, track=1.5)
    f.tag((898, 238, 1008, 268), "BIG JOBS", 17, "mono", ACCENT, pad=6, track=1.5)
    f.tag((928, 712, 1035, 741), "RESULTS", 17, "mono", ACCENT, pad=6, track=1.5)
    f.tag((55, 815, 403, 863), "You", 32)
    f.tag((519, 815, 958, 863), "Your Grok Bots", 32)
    f.tag((1089, 815, 1482, 863), "The worker", 32, logos=[CLAUDE, OPENAI])
    f.logo_in((709, 897, 766, 950), GROK)
    f.logo_in((1105, 604, 1140, 636), CLAUDE, 0.7)
    f.logo_in((1151, 604, 1185, 636), OPENAI, 0.7)
    f.note((58, 900), "PHONE OR LAPTOP, PLAIN WORDS")
    f.note((790, 930), "CHIEF OF STAFF + A BOT PER PROJECT")
    f.note((1089, 900), "RENTED SERVER, LONG OR HARD JOBS")
    f.save()


def request_journey():
    f = Fig("request-journey")
    f.title(2, "A request's journey")
    for c, s in (((168, 272), "01"), ((529, 272), "02"), ((927, 209), "03"), ((1372, 272), "04")):
        f.badge(c, 28, s)
    f.tag((53, 788, 312, 842), "You ask", 30)
    f.tag((423, 788, 714, 842), "Bot plans", 30)
    f.tag((847, 788, 1139, 842), "Worker does it", 30)
    f.tag((1249, 788, 1492, 842), "Result comes back", 30)
    f.clear((233, 342, 352, 408), 5)  # the speech bubble carries the example request
    f.text((292, 366), "refresh the", mono(15), INK, "mm")
    f.text((292, 388), "pricing page", mono(15), INK, "mm")
    f.logo(GROK, (472, 436), 50)  # Grok mark inside the bot module
    f.logo_in((913, 497, 951, 539), CLAUDE, 0.68)
    f.logo_in((968, 483, 1007, 525), OPENAI, 0.68)
    f.pill((712, 222), "STUCK? ASKS THE BOT")
    f.pill((770, 910), "BOT CHECKS IT, SENDS YOU ONE LINK")
    f.save()


def decisions_and_goals():
    f = Fig("decisions-and-goals")
    f.title(3, "Where decisions land")
    f.tag((65, 782, 445, 840), "Waiting on you", 32)
    f.tag((545, 782, 947, 840), "You decide", 32)
    f.tag((1057, 782, 1470, 840), "Weekly goals", 32)
    f.tag((681, 901, 853, 947), "DAILY CHECKS", 16, "mono", ACCENT, pad=8, track=1.5)
    f.clear((603, 447, 716, 520), 5)  # the phone shows the morning digest
    f.text((659, 472), "3 ITEMS", mono(15), ACCENT, "mm")
    f.text((659, 496), "TO DECIDE", mono(15), INK, "mm")
    f.note((76, 206), "GROK BOTS", color=INK, logo=GROK)
    f.note((548, 186), "EACH MORNING")
    f.note((1036, 160), "EACH BOT CHECKS ITS GOALS DAILY")
    f.note((735, 1000), "QUIET UNLESS A GOAL IS AT RISK", anchor="rs")
    f.note((805, 1000), "1-3 GOALS PER PROJECT A WEEK")
    f.save()


def which_tool_when():
    f = Fig("which-tool-when")
    f.title(4, "Which tool when", (32, 66))
    panels = [
        # (letter square, text origin, title, sub lines, logos)
        ((30, 517, 60, 547), (44, 200), "Connectors", ["your own", "accounts: mail,", "calendar, files"], [(GROK, "GROK BOT")]),
        ((792, 517, 822, 547), (806, 200), "treg", ["paid data, no", "subscription;", "Search Console,", "Google Ads"], []),
        ((30, 940, 60, 970), (44, 616), "Worker", ["all coding and", "anything long"], [(CLAUDE, "CLAUDE"), (OPENAI, "CODEX")]),
        ((792, 940, 822, 970), (806, 616), "Executor", ["only later, if", "the worker needs", "your connected", "tools"], []),
    ]
    for i, (sq, (x, y), title, sub, logos) in enumerate(panels):
        f.clear(sq, 3)
        f.text(((sq[0] + sq[2]) / 2, (sq[1] + sq[3]) / 2), "ABCD"[i], mono(18), ACCENT, "mm")
        f.text((x, y + 30), title, sans(40), INK, "ls")
        for j, line in enumerate(sub):
            f.text((x, y + 72 + j * 30), line, sans(23, "Regular"), MUTED, "ls")
        for j, (n, label) in enumerate(logos):
            f.note((x, y + 72 + len(sub) * 30 + 30 + j * 38), label, 16, INK, logo=n)
    f.save()


if __name__ == "__main__":
    who_does_what()
    request_journey()
    decisions_and_goals()
    which_tool_when()
