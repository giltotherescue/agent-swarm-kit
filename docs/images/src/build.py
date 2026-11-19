"""Builds the eight README diagrams in docs/images/.

For each topic there are two images that share one look:
  <topic>.png        the short version: a small illustration strip over four plain-language columns
  <topic>-steps.png  step by step: a light swimlane, one row per part of the system

The illustration objects come from codex-image strips in strips/ (see prompts/). Everything else is
drawn here: real Inter and JetBrains Mono type, vector arrows, and the single-color marks in logos/.
Each page is set as SVG in an HTML file, screenshotted at 2x with agent-browser, then given
transparent rounded corners so the white card sits cleanly on GitHub light and dark.

Run: see README.md in this folder.
"""
import base64, html, io, os, re, subprocess, sys, tempfile

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)
FONTS = os.environ.get("DIAGRAM_FONTS", os.path.join(HERE, "fonts"))
SCALE = 2

# palette (shared by both styles)
BG, ALT, LINE, INK, DIM, AC = "#FFFFFF", "#F6F8FA", "#D0D7DE", "#1F2328", "#59636E", "#C2410C"
MUTE = "#8C959F"
STATE = {"queued": "#6E7781", "running": "#0969DA", "done": "#1A7F37"}

# ---------------------------------------------------------------- text measuring and wrapping
_fonts = {}


def font(size, weight=400, mono=False):
    name = ("JetBrainsMono-Regular.ttf" if weight < 500 else "JetBrainsMono-Medium.ttf") if mono else {
        400: "Inter-Regular.ttf", 500: "Inter-Medium.ttf", 600: "Inter-SemiBold.ttf", 700: "Inter-Bold.ttf"}[weight]
    key = (name, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(os.path.join(FONTS, name), size * 4)
    return _fonts[key]


def runs(s, weight=400):
    """Split **bold** and `code` markup into (text, weight, mono) runs."""
    out = []
    for part in re.split(r"(\*\*.+?\*\*|`.+?`)", s):
        if not part:
            continue
        if part.startswith("**"):
            out.append((part[2:-2], 600, False))
        elif part.startswith("`"):
            out.append((part[1:-1], 400, True))
        else:
            out.append((part, weight, False))
    return out


def width(s, size, weight=400):
    w = 0
    for t, wt, mono in runs(s, weight):
        w += font(size * 0.92 if mono else size, wt, mono).getlength(t) / 4
    return w


def wrap(s, size, maxw, weight=400):
    # keep markup spans and "20 s" style units together
    s = re.sub(r"(`[^`]+`|\*\*.+?\*\*)", lambda m: m.group(0).replace(" ", "\u00a0"), s)
    s = re.sub(r"(\d) (s|h|min)\b", "\\1\u00a0\\2", s)
    lines, cur = [], ""
    for word in s.split(" "):
        cand = (cur + " " + word).strip()
        if cur and width(cand.replace("\u00a0", " "), size, weight) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    lines.append(cur)
    return [ln.replace("\u00a0", " ") for ln in lines]


def esc(s):
    return html.escape(s, quote=True)


def logo(name, x, y, size, fill=INK):
    src = open(os.path.join(HERE, "logos", name + ".svg")).read()
    body = "".join(re.findall(r"<path[^>]*?(?:/>|>.*?</path>)", src, re.S))
    return (f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 24 24">'
            f'<g fill="{fill}" fill-rule="evenodd">{body}</g></svg>')


# ---------------------------------------------------------------- SVG page
class Doc:
    def __init__(self, w):
        self.w, self.h, self.parts, self.defs = w, 0, [], []
        for mid, col in [("a", INK), ("m", MUTE), ("o", AC)]:
            self.defs.append(
                f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" '
                f'orient="auto"><path d="M0,0.5 L10,5 L0,9.5 z" fill="{col}"/></marker>')

    def add(self, s):
        self.parts.append(s)

    def rect(self, x, y, w, h, fill="none", stroke=None, sw=1, r=8, dash=None):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        if dash:
            a += f' stroke-dasharray="{dash}"'
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" fill="{fill}"{a}/>')

    def text(self, x, y, lines, size, weight=400, fill=INK, lh=None, anchor="start", mono=False, spacing=None):
        if isinstance(lines, str):
            lines = [lines]
        lh = lh or round(size * 1.4)
        fam = "JetBrains Mono" if mono else "Inter"
        ls = f' letter-spacing="{spacing}"' if spacing else ""
        out = [f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}" font-size="{size}" font-weight="{weight}" '
               f'fill="{fill}" text-anchor="{anchor}"{ls}>']
        for i, ln in enumerate(lines):
            body = ""
            for t, wt, m in runs(ln, weight):
                t = esc(t)
                if m:
                    body += f'<tspan font-family="JetBrains Mono" font-size="{size * 0.92:.2f}">{t}</tspan>'
                elif wt != weight:
                    body += f'<tspan font-weight="{wt}">{t}</tspan>'
                else:
                    body += t
            out.append(f'<tspan x="{x:.1f}" dy="{0 if i == 0 else lh}">{body}</tspan>')
        out.append("</text>")
        self.add("".join(out))

    def path(self, d, color=INK, sw=1.5, mid="a", dash=None):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{mid})"' if mid else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round"{da}{m}/>')

    def num(self, cx, cy, n, r=10, fill=INK, color=BG, size=11.5):
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{fill}"/>')
        self.add(f'<text x="{cx:.1f}" y="{cy + size * 0.36:.1f}" font-family="Inter" font-size="{size}" '
                 f'font-weight="700" fill="{color}" text-anchor="middle">{esc(str(n))}</text>')

    def image(self, data, x, y, w, h):
        self.add(f'<image href="{data}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"/>')

    def header(self, kicker, title, subtitle, x=28, sub_w=None):
        self.text(x, 40, kicker, 12, weight=500, fill=AC, mono=True, spacing="1.2")
        self.text(x, 74, title, 27, weight=600)
        lines = wrap(subtitle, 15.5, sub_w or self.w - 2 * x)
        self.text(x, 102, lines, 15.5, fill=DIM, lh=22)
        return 102 + 22 * (len(lines) - 1)

    def html(self):
        faces = "".join(
            f"@font-face{{font-family:'{fam}';src:url('file://{FONTS}/{f}');font-weight:{wt}}}"
            for fam, f, wt in [("Inter", "Inter-Regular.ttf", 400), ("Inter", "Inter-Medium.ttf", 500),
                               ("Inter", "Inter-SemiBold.ttf", 600), ("Inter", "Inter-Bold.ttf", 700),
                               ("JetBrains Mono", "JetBrainsMono-Regular.ttf", 400),
                               ("JetBrains Mono", "JetBrainsMono-Medium.ttf", 500)])
        frame = (f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="14" fill="{BG}" '
                 f'stroke="{LINE}" stroke-width="1"/>')
        svg = (f'<svg id="frame" xmlns="http://www.w3.org/2000/svg" width="{self.w * SCALE}" '
               f'height="{self.h * SCALE}" viewBox="0 0 {self.w} {self.h}"><defs>{"".join(self.defs)}</defs>'
               f'{frame}{"".join(self.parts)}</svg>')
        return (f'<!doctype html><html><head><meta charset="utf-8"><style>{faces}'
                f'html,body{{margin:0;background:#fff}} svg{{display:block}}</style></head><body>{svg}</body></html>')


def render(doc, name):
    """HTML -> 2x PNG via agent-browser, then transparent rounded corners."""
    tmp = tempfile.mkdtemp()
    page = os.path.join(tmp, name + ".html")
    open(page, "w").write(doc.html())
    shot = os.path.join(tmp, name + ".png")
    s = "diagram-build"
    run = lambda *a: subprocess.run(["agent-browser", "--session", s, *a], check=True, capture_output=True)
    run("set", "viewport", str(doc.w * SCALE + 40), "1200")
    run("open", "file://" + page)
    run("wait", "800")
    run("screenshot", "#frame", shot)
    im = Image.open(shot).convert("RGBA")
    im = im.crop((0, 0, doc.w * SCALE, doc.h * SCALE))
    mask = Image.new("L", (im.width * 4, im.height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius=14 * SCALE * 4, fill=255)
    im.putalpha(mask.resize(im.size, Image.LANCZOS))
    dst = os.path.join(OUT, name + ".png")
    im.save(dst, optimize=True)
    if os.environ.get("KEEP_HTML"):
        open(os.path.join(os.environ["KEEP_HTML"], name + ".html"), "w").write(doc.html())
    print(dst, im.size)


# ---------------------------------------------------------------- illustration objects
def strip_objects(name, n=4):
    """Cut the n objects out of a codex-image strip, with white turned into transparency."""
    im = Image.open(os.path.join(HERE, "strips", name + ".webp")).convert("RGB")
    px = im.load()
    a = Image.new("RGBA", im.size)
    ap = a.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b = px[x, y]
            al = 255 - min(r, g, b)
            if al < 10:
                ap[x, y] = (255, 255, 255, 0)
                continue
            un = lambda c: max(0, min(255, round(255 - (255 - c) * 255 / al)))
            ap[x, y] = (un(r), un(g), un(b), al)
    alpha = a.getchannel("A")
    cols = [max(alpha.crop((x, 0, x + 1, im.height)).getextrema()[1], 0) > 60 for x in range(im.width)]
    segs, start = [], None
    for x, on in enumerate(cols + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            segs.append([start, x])
            start = None
    merged = []
    for sgm in segs:  # join pieces of one object split by a thin gap
        if merged and sgm[0] - merged[-1][1] < 40:
            merged[-1][1] = sgm[1]
        else:
            merged.append(sgm)
    merged = sorted(sorted(merged, key=lambda s: s[1] - s[0], reverse=True)[:n])
    objs = []
    for x0, x1 in merged:
        piece = a.crop((x0, 0, x1, im.height))
        bb = piece.getchannel("A").point(lambda v: 255 if v > 60 else 0).getbbox()
        objs.append(piece.crop((0, bb[1], x1 - x0, bb[3])))
    return objs


def data_uri(im, h_px):
    im = im.resize((round(im.width * h_px / im.height), h_px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- D: the short version
D_W = 880


def short(name, kicker, title, subtitle, cols, note=None, arrows=True):
    """cols: [(heading, sub, logos, body, highlight)]"""
    d = Doc(D_W)
    P, GAP = 28, 14
    ytop = d.header(kicker, title, subtitle, P)
    objs = strip_objects(name)
    colw = (D_W - 2 * P - 3 * GAP) / 4
    centers = [P + colw / 2 + i * (colw + GAP) for i in range(4)]
    # one scale for the whole strip so the objects keep their drawn sizes
    MAXH, MAXW = 104, 150
    s = min(min(MAXH / o.height for o in objs), min(MAXW / o.width for o in objs))
    sy0 = ytop + 26
    mid = sy0 + MAXH / 2
    boxes = []
    for o, cx in zip(objs, centers):
        w, h = o.width * s, o.height * s
        d.image(data_uri(o, round(h * SCALE * 1.5)), cx - w / 2, mid - h / 2, w, h)
        boxes.append((cx - w / 2, cx + w / 2))
    for i in range(3 if arrows else 0):
        x1, x2 = boxes[i][1] + 14, boxes[i + 1][0] - 14
        if x2 - x1 > 24:
            d.path(f"M{x1:.1f},{mid:.1f} L{x2:.1f},{mid:.1f}", AC, sw=1.6, mid="o")
    # columns
    top = sy0 + MAXH + 22
    PAD = 16
    inner = colw - 2 * PAD
    BODY, BLH = 15, 21.5
    laid = []
    for head, sub, lg, body, hi in cols:
        hl = wrap(head, 16.5, inner - 30, 600)
        subx = (22 * len(lg) + 2) if lg else 0
        sl = wrap(sub, 13.5, inner - subx, 500)
        bl = []
        for para in body:
            bl.append(wrap(para, BODY, inner))
        h = 26 + 22 * len(hl) + 4 + 19 * len(sl) + 12 + sum(BLH * len(p) + 8 for p in bl) + 6
        laid.append((hl, sl, bl, lg, hi, h))
    ch = max(l[-1] for l in laid)
    for i, (hl, sl, bl, lg, hi, _) in enumerate(laid):
        x = centers[i] - colw / 2
        d.rect(x, top, colw, ch, fill=ALT, stroke=AC if hi else LINE, sw=1.6 if hi else 1, r=10)
        d.num(x + PAD + 11, top + 26, i + 1, r=11, fill=AC, size=12)
        d.text(x + PAD + 30, top + 32, hl, 16.5, weight=600, lh=22)
        y = top + 32 + 22 * (len(hl) - 1) + 26
        sx = x + PAD
        if lg:
            for j, l in enumerate(lg):
                d.add(logo(l, sx + j * 22, y - 13, 16, AC))
            sx += 22 * len(lg) + 2
        d.text(sx, y, sl, 13.5, weight=500, fill=AC, lh=19)
        y += 19 * (len(sl) - 1) + 28
        for k, p in enumerate(bl):
            d.text(x + PAD, y, p, BODY, fill=INK if k == 0 else DIM, lh=BLH)
            y += BLH * len(p) + 8
    d.h = top + ch + 26
    if note:
        nl = wrap(note, 14, D_W - 2 * P)
        d.text(P, d.h + 2, nl, 14, fill=DIM, lh=20)
        d.h += 20 * len(nl) + 14
    return d


# ---------------------------------------------------------------- C: step by step (swimlanes)
C_W = 920


class Lanes:
    def __init__(self, kicker, title, subtitle, lanes, cs=83, cw=146):
        self.d = Doc(C_W)
        self.kicker, self.title, self.subtitle = kicker, title, subtitle
        self.lanes, self.cs, self.cw = lanes, cs, cw
        self.cards, self.links, self.bands, self.notes = {}, [], [], []
        self.P, self.LW = 22, 136
        self.x0 = self.P + self.LW + 8

    def card(self, key, col, lane, title, body, num=None, kind="step"):
        """kind: step | you (orange outline: you act) | stuck (dashed: the stuck loop) | plain"""
        tl = wrap(title, 14, self.cw - (40 if num is not None else 22), 600)
        bl = wrap(body, 13.5, self.cw - 22) if body else []
        h = 18 + 19 * len(tl) + (6 + 18 * len(bl) if bl else 0) + 10
        self.cards[key] = dict(col=col, lane=lane, title=tl, body=bl, num=num, kind=kind, h=h)

    def link(self, a, b, label=None, ax=None, kind="step", lx=None):
        self.links.append((a, b, label, ax, kind, lx))

    def band(self, text, c0, c1, color, style="pill"):
        self.bands.append((text, c0, c1, color, style))

    def colx(self, c):
        return self.x0 + c * self.cs

    def render(self, name):
        d, P = self.d, self.P
        y = d.header(self.kicker, self.title, self.subtitle, P)
        # legend
        y += 26
        lx = P
        d.rect(lx, y - 11, 22, 14, fill=BG, stroke=AC, sw=1.6, r=4)
        d.text(lx + 30, y, "you act here", 12.5, fill=DIM)
        lx += 30 + width("you act here", 12.5) + 22
        if any(c["kind"] == "stuck" for c in self.cards.values()):
            d.rect(lx, y - 11, 22, 14, fill=BG, stroke=MUTE, sw=1.2, r=4, dash="3 3")
            d.text(lx + 30, y, "only if the model gets stuck", 12.5, fill=DIM)
        y += 16
        if self.bands:
            y += 14
            for text, c0, c1, color, style in self.bands:
                bx0 = self.colx(c0)
                bx1 = self.colx(c1) - 6 if c1 <= 7 else self.colx(7) + self.cw
                if style == "pill":
                    d.rect(bx0, y, bx1 - bx0, 24, fill=BG, stroke=color, sw=1.2, r=12)
                    d.text(bx0 + 11, y + 16.5, text, 12.5, weight=500, fill=color, mono=True)
                else:
                    d.text(bx0, y + 12, text, 12, weight=600, fill=INK, spacing="1.3")
                    d.add(f'<line x1="{bx0}" y1="{y + 20}" x2="{bx1}" y2="{y + 20}" stroke="{INK}" stroke-width="1.5"/>')
            y += 34
        # lanes
        top = y + 4
        PADV = 13
        lane_y, lane_h = [], []
        for i, (lname, sub, lg) in enumerate(self.lanes):
            sl = wrap(sub, 13, self.LW - 14)
            need = max([c["h"] for c in self.cards.values() if c["lane"] == i] + [0]) + 2 * PADV
            need = max(need, 30 + 17.5 * len(sl) + 14)
            lane_y.append(top)
            lane_h.append(need)
            d.rect(P, top, C_W - 2 * P, need, fill=ALT if i % 2 == 0 else BG, r=0)
            tx = P + 10
            if lg:
                for j, l in enumerate(lg):
                    d.add(logo(l, tx + j * 21, top + 13, 16))
                tx += 21 * len(lg) + 2
            d.text(tx, top + 26, lname, 14.5, weight=600)
            d.text(P + 10, top + 45, sl, 13, fill=DIM, lh=17.5)
            top += need
        d.add(f'<line x1="{self.x0 - 8}" y1="{lane_y[0]}" x2="{self.x0 - 8}" y2="{top}" stroke="{LINE}"/>')
        # place cards
        for c in self.cards.values():
            c["x"] = self.colx(c["col"])
            c["y"] = lane_y[c["lane"]] + PADV
            c["w"] = self.cw
        # links first (under cards)
        labels = []
        for a, b, label, ax, kind, lxo in self.links:
            A, B = self.cards[a], self.cards[b]
            color, mid, dash = (MUTE, "m", "4 3") if kind == "stuck" else (INK, "a", None)
            if A["lane"] == B["lane"]:
                yy = A["y"] + 22
                x1, x2 = A["x"] + A["w"], B["x"]
                d.path(f"M{x1},{yy} L{x2 - 1},{yy}", color, mid=mid, dash=dash)
                if label:
                    labels.append((x1 + 6, yy - 6, label, "start"))
                continue
            down = B["lane"] > A["lane"]
            ov0, ov1 = max(A["x"], B["x"]), min(A["x"] + A["w"], B["x"] + B["w"])
            ya = A["y"] + A["h"] if down else A["y"]
            yb = B["y"] if down else B["y"] + B["h"]
            if ov1 - ov0 >= 20 or ax is not None:
                x = ax if ax is not None else (ov0 + ov1) / 2
                x = A["x"] + x if ax is not None and ax < 200 else x
                d.path(f"M{x:.1f},{ya:.1f} L{x:.1f},{yb + (-1 if down else 1):.1f}", color, mid=mid, dash=dash)
                lyy = (ya + yb) / 2 + 4
                if label:
                    labels.append((x + 6 if lxo is None else x + lxo, lyy, label, "start" if (lxo or 1) > 0 else "end"))
            else:  # elbow: out of the side, then down/up into the target
                x1, yy = A["x"] + A["w"], A["y"] + 22
                x = B["x"] + 22
                d.path(f"M{x1},{yy} L{x:.1f},{yy} L{x:.1f},{yb + (-1 if down else 1):.1f}", color, mid=mid, dash=dash)
                if label:
                    labels.append((x + 6, (yy + yb) / 2 + 4, label, "start"))
        for c in self.cards.values():
            k = c["kind"]
            stroke, sw, dash = {"you": (AC, 1.8, None), "stuck": (MUTE, 1.2, "4 3"),
                                "plain": (LINE, 1, None)}.get(k, (MUTE, 1, None))
            d.rect(c["x"], c["y"], c["w"], c["h"], fill=BG, stroke=stroke, sw=sw, r=8, dash=dash)
            tx = c["x"] + 11
            if c["num"] is not None:
                d.num(c["x"] + 19, c["y"] + 21, c["num"], r=9.5, fill=AC if k == "you" else INK, size=11.5)
                tx = c["x"] + 34
            tcol = DIM if k == "stuck" else INK
            d.text(tx, c["y"] + 26, c["title"], 14, weight=600, fill=tcol, lh=19)
            if c["body"]:
                d.text(c["x"] + 11, c["y"] + 26 + 19 * (len(c["title"]) - 1) + 24, c["body"], 13.5, fill=DIM, lh=18)
        for x, y_, t, anchor in labels:
            w_ = width(t, 12.5 * 0.92)
            bx = x - 3 if anchor == "start" else x - w_ - 3
            d.rect(bx, y_ - 12, w_ + 6, 16, fill=BG, r=3)
            d.text(x, y_, t, 12.5, fill=DIM, mono=True, anchor=anchor)
        y = top + 26
        if self.notes:
            n = len(self.notes)
            nw = (C_W - 2 * P - 24 * (n - 1)) / n
            hmax = 0
            for i, (h, body, accent) in enumerate(self.notes):
                x = P + i * (nw + 24)
                d.text(x, y, h, 14, weight=600, fill=AC if accent else INK)
                bl = wrap(body, 13.5, nw)
                d.text(x, y + 22, bl, 13.5, fill=DIM, lh=19)
                hmax = max(hmax, 22 + 19 * len(bl))
            y += hmax + 6
        d.h = y
        render(d, name)


# ---------------------------------------------------------------- the four topics
KICK_D, KICK_C = "THE SHORT VERSION", "STEP BY STEP"


def who_does_what():
    render(short(
        "who-does-what", KICK_D, "Who does what",
        "You talk to your bots. The bots plan and hand the heavy work to the worker.",
        [("You", "in Grok chat", None,
          ["You set the goals and make the calls.",
           "You only hear from the swarm when something needs you or a result is ready."], True),
         ("Chief of Staff", "one Grok Bot, all projects", ["grok"],
          ["Coordinates across your projects.",
           "Sends you one list of decisions each morning and runs the weekly goals meeting."], False),
         ("Project bots", "one Grok Bot per project", ["grok"],
          ["Each owns its project: watches, drafts, plans.",
           "Writes a brief for anything long or any code, and gets the result straight back."], False),
         ("The worker", "an always-on server", ["claude", "openai"],
          ["Runs Claude Code or Codex on your own subscriptions.",
           "Code comes back as a draft pull request. Only you merge."], False)], arrows=False), "who-does-what")

    L = Lanes(KICK_C, "Who does what, on a normal day",
              "Each row is one part of the system. Results go straight to the bot that asked, "
              "not through a hub.",
              [("You", "in Grok chat and on GitHub", None),
               ("Chief of Staff", "a Grok Bot for all projects", ["grok"]),
               ("Project bot", "a Grok Bot, one per project", ["grok"]),
               ("Task queue", "the backend: SQLite on the worker server", None),
               ("Worker", "runner + Claude Code or Codex", ["claude", "openai"]),
               ("GitHub", "your repos", None)])
    L.card("list", 0, 1, "Morning list", "every open decision in one message, each with a recommendation", 1)
    L.card("reply", 1, 0, "Reply", "in one line: “1 yes, 2 drop, 3 snooze”", 2, "you")
    L.card("plan", 2, 2, "Plan + act", "quick things it does itself; code or long jobs get a brief", 3)
    L.card("queue", 3, 3, "Hold the task", "brief, owner, state; the bot can only queue for its own projects", 4)
    L.card("work", 4, 4, "Do the work", "in a sealed folder, with no GitHub login; Codex if Claude hits a limit", 5)
    L.card("pr", 5, 5, "Draft PR", "the runner pushes after a secret scan, never to main by default", None, "plain")
    L.card("notice", 5, 3, "Result notice", "report + PR link go straight to the bot that asked", 6)
    L.card("check", 6, 2, "Check + tell you", "compares it with the ask, sends one message, logs it", 7)
    L.card("merge", 7, 0, "Review + merge", "only a human merges", 8, "you")
    L.card("goals", 4, 1, "Weekly goals", "scores last week, proposes 1 to 3 goals per project", None, "plain")
    L.link("list", "reply")
    L.link("reply", "plan")
    L.link("plan", "queue", "enqueue")
    L.link("queue", "work", "claim")
    L.link("work", "pr", "push")
    L.link("work", "notice", "finish")
    L.link("notice", "check", "notice")
    L.link("check", "merge")
    L.render("who-does-what-steps")


def request_journey():
    render(short(
        "request-journey", KICK_D, "A request's journey",
        "One code task, from your message to a draft pull request.",
        [("You ask", "in Grok chat", None,
          ["“Refresh the pricing page.”",
           "Quick things the bot does itself. Code, or anything over about 15 minutes, goes to the worker."],
          True),
         ("The bot plans", "your website bot", ["grok"],
          ["It writes a brief: the goal, what done means, the repo and the limits.",
           "Then it queues the task on the worker."], False),
         ("The worker does it", "Claude Code or Codex", ["claude", "openai"],
          ["Edits, tests and commits in a sealed folder, then writes a report.",
           "The runner scans for secrets and opens a draft PR."], False),
         ("You review", "one message, one link", None,
          ["The result goes straight back to the website bot.",
           "It checks it against your ask and messages you. You merge."], True)]), "request-journey")

    L = Lanes(KICK_C, "A request's journey, step by step",
              "“Refresh the pricing page”, from Grok chat to a draft pull request.",
              [("You", "in Grok chat and on GitHub", None),
               ("Website bot", "a Grok Bot, the project bot", ["grok"]),
               ("Task queue", "the backend: SQLite on the worker server", None),
               ("Runner", "swarm-worker; the only part with the GitHub login", None),
               ("Model", "Claude Code or Codex, in a sealed folder", ["claude", "openai"]),
               ("GitHub", "your repo", None)])
    L.band("queued", 2, 3, STATE["queued"])
    L.band("running · checks in every 30 s", 3, 6, STATE["running"])
    L.band("done", 6, 8, STATE["done"])
    L.card("ask", 0, 0, "Ask", "“Refresh the pricing page”", 1, "you")
    L.card("brief", 1, 1, "Write a brief", "goal, done means, repo, limits; queues it with its own token", 2)
    L.card("save", 2, 2, "Save the task", "checks the bot owns the project; state: queued", 3)
    L.card("claim", 3, 3, "Claim + prepare", "polls every 20 s, clones the repo, fills the inbox", 4)
    L.card("work", 4, 4, "Do the work", "edits, tests, commits; writes REPORT.md and AGENT_STATUS", 5)
    L.card("pub", 5, 3, "Collect + publish", "checks it's still allowed, scans for secrets, pushes", 6)
    L.card("pr", 5, 5, "Draft PR", "branch agent/<task id>; never merged by the worker", None, "plain")
    L.card("fin", 6, 2, "Finish + notify", "state: done; summary + PR link straight to the bot", 7)
    L.card("check", 7, 1, "Check + tell you", "compares it with your ask; one message + link", 8)
    L.card("merge", 7, 0, "Review + merge", "only a human merges", 9, "you")
    L.card("wait", 4, 2, "Waiting", "AGENT_STATUS says blocked, with one question", None, "stuck")
    L.card("ans", 4, 1, "Answer", "the bot answers, or you do if it was queued `--ask-human`", None, "stuck")
    L.link("ask", "brief")
    L.link("brief", "save", "enqueue")
    L.link("save", "claim", "claim")
    L.link("claim", "work", "start", lx=-6)
    L.link("work", "pub", "outbox")
    L.link("pub", "pr", "push + PR", ax=96)
    L.link("pub", "fin", "finish")
    L.link("fin", "check", "notice")
    L.link("check", "merge")
    L.link("work", "wait", ax=73, kind="stuck")
    L.link("wait", "ans", ax=73, kind="stuck")
    L.notes = [("If it gets stuck", "The answer puts the task back in the queue and the same session carries on. "
                                    "If the runner goes quiet for 10 minutes, the task is queued again.", False),
               ("Where you are needed", "Only the orange cards: you ask and you merge. The worker never merges or "
                                        "deploys, and pushes to main only if the project opted in.", True)]
    L.render("request-journey-steps")


def decisions_and_goals():
    render(short(
        "decisions-and-goals", KICK_D, "Where decisions land",
        "Everything that needs you goes on one list. You hear about it once a day.",
        [("Bots file it", "on the Waiting on you list", None,
          ["Anything that needs your yes, or a goal at risk.",
           "A yes is needed to send, spend, delete or give access."], False),
         ("One message", "each morning, from the Chief of Staff", ["grok"],
          ["Every open item, numbered, each with a recommendation.",
           "On a normal day you hear nothing else."], False),
         ("You reply", "in one line, or on a call", None,
          ["“1 yes, 2 drop, 3 snooze”, or talk it through by voice.",
           "Dropping is fine: the bot stops and does not ask again. Only your yes counts."], True),
         ("Weekly goals", "about 10 minutes a week", None,
          ["Last week gets scored, and each project gets 1 to 3 goals.",
           "You confirm. Bots check them daily."], True)]), "decisions-and-goals")

    L = Lanes(KICK_C, "Where decisions land, step by step",
              "Everything that needs you goes on one list. At most one message a day, plus the weekly "
              "goals meeting.",
              [("You", "the only one who can say yes", None),
               ("Chief of Staff", "a Grok Bot", ["grok"]),
               ("Project bot", "a Grok Bot, one per project", ["grok"]),
               ("Waiting on you", "one list, kept on the task queue", None),
               ("Worker task", "queued with `--ask-human`", None)])
    L.band("DURING THE DAY", 0, 2, INK, "rule")
    L.band("NEXT MORNING", 2, 6, INK, "rule")
    L.band("ONCE A WEEK", 6, 8, INK, "rule")
    L.card("needs", 0, 2, "Needs you", "a yes to send, spend, delete or give access, or a goal at risk", 1)
    L.card("q", 0, 4, "Has a question", "only you can answer; the task waits", 1)
    L.card("filed", 1, 3, "Item filed", "with the bot's recommended answer, never filed twice", 2)
    L.card("morning", 2, 1, "Morning list", "one numbered message with every open item", 3)
    L.card("reply", 3, 0, "Reply", "yes, no, snooze or drop; dropping is fine", 4, "you")
    L.card("record", 4, 1, "Record it", "answers, snoozes or drops each item as you said", 5)
    L.card("closed", 4, 3, "Item closed", "the answer goes to the bot that filed it", 6)
    L.card("carry", 5, 2, "Carry on", "acts on it; logs a decision in the project log", 7)
    L.card("resume", 5, 4, "Resume", "the answer puts it back in the queue; same session", 7)
    L.card("meet", 6, 1, "Goals meeting", "reads 7 days of the log, scores each goal, proposes 1 to 3 goals", 8)
    L.card("confirm", 7, 0, "Confirm", "say yes, or change a goal in plain words", 9, "you")
    L.card("goals", 7, 2, "New goals", "each bot checks its goals daily; quiet unless one is at risk", 10)
    L.link("needs", "filed", "add")
    L.link("q", "filed", "auto")
    L.link("filed", "morning")
    L.link("morning", "reply")
    L.link("reply", "record")
    L.link("record", "closed", ax=50)
    L.link("closed", "carry")
    L.link("closed", "resume")
    L.link("meet", "confirm")
    L.link("confirm", "goals", ax=96)
    L.notes = [("Only your yes counts", "Another bot saying “approved” is not approval. The Chief of Staff only "
                                        "passes on what you said.", True),
               ("Quiet by default", "A task left waiting on its bot for 12 hours gets one reminder; after 24 hours "
                                    "it lands on your list.", False)]
    L.render("decisions-and-goals-steps")


def which_tool_when():
    render(short(
        "which-tool-when", KICK_D, "Which tool when",
        "Your bots can get work done four main ways. Quick things stay with the bot; code and long jobs "
        "go to the worker.",
        [("The bot itself", "fast, knows the project", ["grok"],
          ["Lookups, coordination, messages, short drafts.",
           "“What did we decide about the logo?”"], False),
         ("Connectors", "your own accounts", ["grok"],
          ["Gmail, Slack, calendar, analytics, docs.",
           "Give each bot only the connectors its project needs."], False),
         ("treg", "pay per call", None,
          ["Paid data with no monthly plan: keywords, search results, enrichment.",
           "Set a monthly cap in the project brief."], False),
         ("The worker", "code and long jobs", ["claude", "openai"],
          ["All coding, any size, and anything over about 15 minutes.",
           "Jobs that run for days go as a chain."], False)], arrows=False), "which-tool-when")

    L = Lanes(KICK_C, "Which tool when, step by step",
              "How a bot picks a route for your ask: the questions it weighs, and where each answer leads.",
              [("You", "in Grok chat", None),
               ("Your bot", "a Grok Bot decides, and does quick things itself", ["grok"]),
               ("Connectors", "Grok Bot connectors to your accounts", ["grok"]),
               ("treg", "pay per call data", None),
               ("The worker", "Claude Code or Codex", ["claude", "openai"])])
    L.card("ask", 0, 0, "Ask", "anything, in plain words", None, "you")
    L.card("q1", 1, 1, "Code, or over ~15 min?", "also design, deep research, bulk scraping, a set model", None)
    L.card("q2", 3, 1, "Your own account?", "Gmail, Slack, calendar, analytics, docs", None)
    L.card("q3", 5, 1, "Paid data?", "keywords, search results, enrichment", None)
    L.card("self", 7, 1, "Bot does it", "lookups, messages, short drafts", None)
    L.card("worker", 2, 4, "Worker task", "any size of code; a draft PR comes back. Jobs that run for days: a chain", None)
    L.card("exec", 4, 4, "Executor, later", "only if a worker task needs your connected tools too", None, "plain")
    L.card("conn", 4, 2, "Use a connector", "quick reads and drafts; nothing sent without your yes", None)
    L.card("treg", 6, 3, "Use treg", "also your Search Console and Google Ads; ask before a big batch", None)
    L.link("ask", "q1")
    L.link("q1", "q2", "no")
    L.link("q2", "q3", "no")
    L.link("q3", "self", "no")
    L.link("q1", "worker", "yes")
    L.link("q2", "conn", "yes")
    L.link("q3", "treg", "yes")
    L.notes = [("Same account, two ways?", "If a connector and treg both reach it (Search Console, Google Ads), "
                                           "use the connector: it is already logged in and free per call.", False)]
    L.render("which-tool-when-steps")


if __name__ == "__main__":
    which = sys.argv[1:] or ["who", "journey", "decisions", "which"]
    fns = {"who": who_does_what, "journey": request_journey, "decisions": decisions_and_goals,
           "which": which_tool_when}
    for k in which:
        fns[k]()
