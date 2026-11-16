"""Small SVG diagram builder used for the architecture diagrams in docs/architecture.

Design rules (Azure Architecture Center conventions): white canvas, official product icons with
a label underneath, light grouping boundaries with a caption, numbered steps that match the
"Dataflow" list in the article, plain arrows, restrained palette, no decoration.
"""
from __future__ import annotations

import os
import re
from xml.sax.saxutils import escape

from PIL import ImageFont

FONT_STACK = "'Segoe UI','Segoe UI Variable',Selawik,'Liberation Sans','Helvetica Neue',Arial,sans-serif"
_fonts: dict = {}

# Text is measured with an Arial-metric font so that wrapping is the same on every platform.
# Override with DIAGRAM_FONT_REGULAR / DIAGRAM_FONT_BOLD if none of these exist on your machine.
_CANDIDATES = {
    False: [
        os.environ.get("DIAGRAM_FONT_REGULAR", ""),
        "/usr/share/fonts/metric-compat/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ],
    True: [
        os.environ.get("DIAGRAM_FONT_BOLD", ""),
        "/usr/share/fonts/metric-compat/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ],
}


def _font_path(bold: bool) -> str:
    for p in _CANDIDATES[bold]:
        if p and os.path.exists(p):
            return p
    raise SystemExit("No Arial-metric font found. Install Liberation Sans or Arial, or set DIAGRAM_FONT_REGULAR and DIAGRAM_FONT_BOLD.")


INK = "#201F1E"       # primary text
MUTED = "#605E5C"     # secondary text
LINE = "#484644"      # arrows
EDGE = "#C8C6C4"      # box strokes
ACCENT = "#0F6CBD"    # step markers
PANEL = "#FAF9F8"     # neutral group fill
WHITE = "#FFFFFF"


def _font(size: float, bold: bool = False):
    key = (round(size * 4), bold)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(_font_path(bold), int(round(size * 4)), layout_engine=ImageFont.Layout.BASIC)
    return _fonts[key]


def text_width(s: str, size: float, bold: bool = False) -> float:
    return _font(size, bold).getlength(s) / 4.0


def wrap(s: str, width: float, size: float, bold: bool = False) -> list[str]:
    out: list[str] = []
    for para in s.split("\n"):
        words, cur = para.split(" "), ""
        for w in words:
            t = (cur + " " + w).strip()
            if cur and text_width(t, size, bold) > width:
                out.append(cur)
                cur = w
            else:
                cur = t
        out.append(cur)
    return out


class Diagram:
    def __init__(self, w: int, h: int, icon_dir: str):
        self.w, self.h, self.icon_dir = w, h, icon_dir
        self.body: list[str] = []
        self.symbols: dict[str, str] = {}
        self.sym_box: dict[str, tuple] = {}
        self.markers: set[str] = set()

    # ---------- icons -------------------------------------------------
    def _symbol(self, key: str) -> None:
        if key in self.symbols:
            return
        t = open(os.path.join(self.icon_dir, key + ".svg"), encoding="utf-8").read()
        t = re.sub(r"<\?xml[^>]*\?>", "", t).strip()
        m = re.search(r"<svg\b[^>]*>", t)
        root = m.group(0)
        inner = t[m.end(): t.rfind("</svg>")]
        vb = re.search(r'viewBox="([^"]+)"', root)
        if vb:
            box = tuple(float(x) for x in re.split(r"[ ,]+", vb.group(1).strip()))
        else:
            w = float(re.search(r'\swidth="([\d.]+)', root).group(1))
            h = float(re.search(r'\sheight="([\d.]+)', root).group(1))
            box = (0.0, 0.0, w, h)
        pre = "i-" + key + "-"
        ids = set(re.findall(r'\bid="([^"]+)"', inner))
        for i in sorted(ids, key=len, reverse=True):
            inner = re.sub(r'\bid="%s"' % re.escape(i), 'id="%s%s"' % (pre, i), inner)
            inner = inner.replace("url(#%s)" % i, "url(#%s%s)" % (pre, i))
            inner = re.sub(r'href="#%s"' % re.escape(i), 'href="#%s%s"' % (pre, i), inner)
        root_fill = re.search(r'\sfill="([^"]+)"', root)
        g_attr = ' fill="%s"' % root_fill.group(1) if root_fill else ""
        self.symbols[key] = '<symbol id="ic-%s" viewBox="%g %g %g %g"><g%s>%s</g></symbol>' % (key, *box, g_attr, inner)
        self.sym_box[key] = box

    def icon(self, key: str, x: float, y: float, size: float) -> None:
        """Place an icon with its top-left at x,y inside a size x size cell, keeping aspect ratio."""
        self._symbol(key)
        _, _, bw, bh = self.sym_box[key]
        if bw >= bh:
            w, h = size, size * bh / bw
        else:
            w, h = size * bw / bh, size
        self.body.append('<use href="#ic-%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>' % (key, x + (size - w) / 2, y + (size - h) / 2, w, h))

    # ---------- primitives ---------------------------------------------
    def rect(self, x, y, w, h, fill=WHITE, stroke=EDGE, r=8, sw=1.0, dash=None) -> None:
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        self.body.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%g" fill="%s" stroke="%s" stroke-width="%g"%s/>' % (x, y, w, h, r, fill, stroke, sw, d))

    def text(self, x, y, s, size=12, bold=False, fill=INK, anchor="start", italic=False) -> None:
        st = ' font-style="italic"' if italic else ""
        self.body.append('<text x="%.1f" y="%.1f" font-size="%g" font-weight="%s" fill="%s" text-anchor="%s"%s>%s</text>' % (x, y, size, 600 if bold else 400, fill, anchor, st, escape(s)))

    def para(self, x, y, s, width, size=12, bold=False, fill=INK, anchor="start", lh=None) -> float:
        lh = lh or round(size * 1.32, 1)
        for i, line in enumerate(wrap(s, width, size, bold)):
            self.text(x, y + i * lh, line, size, bold, fill, anchor)
        return y + (len(wrap(s, width, size, bold)) - 1) * lh

    def group(self, x, y, w, h, title, sub=None, icon=None, fill=PANEL, stroke=EDGE, title_size=14.5, dash=None) -> None:
        self.rect(x, y, w, h, fill=fill, stroke=stroke, r=10, dash=dash)
        tx = x + 16
        if icon:
            self.icon(icon, x + 14, y + 11, 26)
            tx = x + 48
        self.text(tx, y + 29, title, title_size, bold=True)
        if sub:
            self.text(tx, y + 46, sub, 11.5, fill=MUTED)

    def node(self, cx, top, icon, label, sub=None, size=52, width=132, label_size=12.5, sub_size=11) -> float:
        self.icon(icon, cx - size / 2, top, size)
        y = top + size + 17
        y = self.para(cx, y, label, width, label_size, bold=True, anchor="middle")
        if sub:
            y = self.para(cx, y + 15, sub, width, sub_size, fill=MUTED, anchor="middle")
        return y

    def chip(self, x, y, w, h, title, sub=None, icon=None, fill=WHITE, stroke=EDGE, title_size=12.5, sub_size=11, dash=None) -> None:
        self.rect(x, y, w, h, fill=fill, stroke=stroke, r=6, dash=dash)
        tx, tw = x + 12, w - 24
        if icon:
            s = min(h - 16, 36)
            self.icon(icon, x + 10, y + (h - s) / 2, s)
            tx, tw = x + 18 + s, w - 30 - s
        t_lines = wrap(title, tw, title_size, True)
        s_lines = wrap(sub, tw, sub_size) if sub else []
        total = len(t_lines) * title_size * 1.3 + (len(s_lines) * sub_size * 1.32 + 3 if s_lines else 0)
        cy = y + (h - total) / 2 + title_size
        for i, line in enumerate(t_lines):
            self.text(tx, cy + i * title_size * 1.3, line, title_size, bold=True)
        cy += len(t_lines) * title_size * 1.3 + 2
        for i, line in enumerate(s_lines):
            self.text(tx, cy + i * sub_size * 1.32, line, sub_size, fill=MUTED)

    def arrow(self, pts, dashed=False, both=False, color=LINE, sw=1.5, head=True) -> None:
        d = "M" + " L".join("%.1f %.1f" % p for p in pts)
        extra = ' stroke-dasharray="5 4"' if dashed else ""
        mk = ""
        if head:
            mid = "ah-" + color.strip("#").lower()
            self.markers.add(color)
            mk = ' marker-end="url(#%s)"' % mid
            if both:
                mk += ' marker-start="url(#%s)"' % mid
        self.body.append('<path d="%s" fill="none" stroke="%s" stroke-width="%g" stroke-linejoin="round"%s%s/>' % (d, color, sw, extra, mk))

    def step(self, cx, cy, n, r=11) -> None:
        self.body.append('<circle cx="%.1f" cy="%.1f" r="%g" fill="%s" stroke="#FFFFFF" stroke-width="1.5"/>' % (cx, cy, r, ACCENT))
        self.text(cx, cy + 4.3, str(n), 12, bold=True, fill="#FFFFFF", anchor="middle")

    def label(self, x, y, s, size=11, fill=MUTED, anchor="middle", bg=True, bold=False) -> None:
        if bg:
            w = text_width(s, size, bold) + 8
            x0 = x - w / 2 if anchor == "middle" else (x - 4 if anchor == "start" else x - w + 4)
            self.body.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#FFFFFF" opacity="0.92"/>' % (x0, y - size + 0.5, w, size + 4))
        self.text(x, y, s, size, bold=bold, fill=fill, anchor=anchor)

    def line(self, x1, y1, x2, y2, color=EDGE, sw=1.0, dash=None) -> None:
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        self.body.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%g"%s/>' % (x1, y1, x2, y2, color, sw, d))

    def raw(self, s: str) -> None:
        self.body.append(s)

    # ---------- output --------------------------------------------------
    def svg(self, title: str, desc: str) -> str:
        defs = "".join(
            '<marker id="ah-%s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
            '<path d="M0 0.6 L10 5 L0 9.4 z" fill="%s"/></marker>' % (c.strip("#").lower(), c) for c in sorted(self.markers)
        )
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 %d %d" width="%d" height="%d" '
            'font-family="%s" role="img" aria-labelledby="t d">\n<title id="t">%s</title>\n<desc id="d">%s</desc>\n<defs>%s%s</defs>\n'
            '<rect width="%d" height="%d" fill="#FFFFFF"/>\n%s\n</svg>\n'
        ) % (self.w, self.h, self.w, self.h, FONT_STACK, escape(title), escape(desc), defs, "".join(self.symbols.values()), self.w, self.h, "\n".join(self.body))
