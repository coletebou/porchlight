#!/usr/bin/env python3
"""Render ANSI terminal output as an SVG "screenshot" (for the README).

    porchlight demo --width 160 --color truecolor | scripts/ansi2svg.py --title "atlas" > docs/img/demo.svg

Every glyph is placed by terminal column, so alignment does not depend on the
viewer's font metrics; block elements (▀ ▄ █ ▌) are drawn as rectangles so
banners render without the hairline gaps fonts leave between rows.
"""
import argparse
import html
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from porchlight.theme import _cw  # noqa: E402 — the same width rules the renderer uses

SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
BASE16 = ["#1d1f21", "#cc6666", "#b5bd68", "#f0c674", "#81a2be", "#b294bb", "#8abeb7", "#c5c8c6",
          "#666666", "#d54e53", "#b9ca4a", "#e7c547", "#7aa6da", "#c397d8", "#70c0b1", "#eaeaea"]
FG, BG = "#d8d8d8", "#101217"


def xterm256(n: int) -> str:
    if n < 16:
        return BASE16[n]
    if n >= 232:
        v = 8 + (n - 232) * 10
        return "#%02x%02x%02x" % (v, v, v)
    n -= 16
    lv = [0, 95, 135, 175, 215, 255]
    return "#%02x%02x%02x" % (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])


def parse(text: str):
    """Rows of cells: [(char, color, bold, span)] with span 2 for wide glyphs."""
    rows = []
    color, bold = FG, False
    for raw in text.splitlines():
        line = OTHER.sub(lambda m: m.group(0) if m.group(0).endswith("m") else "", raw)
        cells, i = [], 0
        while i < len(line):
            m = SGR.match(line, i)
            if m:
                codes = [int(c) if c.isdigit() else 0 for c in (m.group(1) or "0").split(";")]
                j = 0
                while j < len(codes):
                    c = codes[j]
                    if c == 0:
                        color, bold = FG, False
                    elif c == 1:
                        bold = True
                    elif c == 22:
                        bold = False
                    elif c == 39:
                        color = FG
                    elif 30 <= c <= 37:
                        color = BASE16[c - 30]
                    elif 90 <= c <= 97:
                        color = BASE16[c - 90 + 8]
                    elif c == 38 and j + 1 < len(codes):
                        if codes[j + 1] == 2 and j + 4 < len(codes):
                            color = "#%02x%02x%02x" % tuple(codes[j + 2:j + 5])
                            j += 4
                        elif codes[j + 1] == 5 and j + 2 < len(codes):
                            color = xterm256(codes[j + 2])
                            j += 2
                    j += 1
                i = m.end()
                continue
            ch = line[i]
            w = _cw(ch, line[i + 1] if i + 1 < len(line) else "")
            if w == 0 and cells:  # a combining mark or variation selector joins the previous glyph
                c0 = cells[-1]
                cells[-1] = (c0[0] + ch, c0[1], c0[2], max(c0[3], 2 if ch == "️" else c0[3]))
            elif w:
                cells.append((ch, color, bold, w))
            i += 1
        rows.append(cells)
    return rows


BLOCKS = {"█": (0.0, 1.0), "▀": (0.0, 0.5), "▄": (0.5, 1.0), "▌": None}
# Meter and rule strokes as (y offset, thickness, width share) of a cell: fonts draw them thin and broken.
LINES = {"━": (0.40, 0.22, 1.0), "╸": (0.40, 0.22, 0.5), "─": (0.48, 0.06, 1.0)}


def svg(rows, title="", font=14.0, pad=18, cols=None):
    cw, lh = font * 0.6, font * 1.32
    width_cols = cols or max((sum(c[3] for c in r) for r in rows), default=80)
    bar = 34 if title is not None else 0
    W = int(pad * 2 + width_cols * cw)
    H = int(pad * 2 + bar + len(rows) * lh)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H),
           '<rect width="100%%" height="100%%" rx="10" fill="%s"/>' % BG]
    if bar:
        for k, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            out.append('<circle cx="%d" cy="%d" r="6" fill="%s"/>' % (pad + 6 + k * 20, pad + 4, col))
        if title:
            out.append('<text x="%d" y="%d" fill="#8b8f98" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
                       'font-size="12" text-anchor="middle">%s</text>' % (W // 2, pad + 8, html.escape(title)))
    out.append('<g font-family="\'JetBrains Mono\',\'SF Mono\',ui-monospace,Menlo,Consolas,\'DejaVu Sans Mono\',monospace" '
               'font-size="%.1f" xml:space="preserve">' % font)
    top = pad + bar
    for r, cells in enumerate(rows):
        y = top + r * lh
        base = y + lh * 0.78
        col = 0
        run, run_x, run_style = [], 0, None

        def flush():
            if run:
                color, bold = run_style
                out.append('<text x="%.1f" y="%.1f" fill="%s"%s>%s</text>' % (
                    pad + run_x * cw, base, color, ' font-weight="bold"' if bold else "", html.escape("".join(run))))
        for ch, color, bold, w in cells:
            if ch in BLOCKS:
                flush()
                run, run_style = [], None
                x0 = pad + col * cw
                if ch == "▌":
                    out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (x0, y, cw / 2, lh, color))
                else:
                    a, b = BLOCKS[ch]
                    out.append('<rect x="%.1f" y="%.1f" width="%.2f" height="%.2f" fill="%s"/>' % (
                        x0, y + a * lh, cw + 0.4, (b - a) * lh + 0.4, color))
            elif ch in LINES:
                flush()
                run, run_style = [], None
                off, thick, share = LINES[ch]
                out.append('<rect x="%.1f" y="%.2f" width="%.2f" height="%.2f" fill="%s"/>' % (
                    pad + col * cw, y + off * lh, cw * share + (0.3 if share == 1.0 else 0), thick * lh, color))
            elif ch == " ":
                flush()
                run, run_style = [], None
            else:
                style = (color, bold)
                if style != run_style or w == 2 or (run and run_x + len(run) != col):
                    flush()
                    run, run_x, run_style = [], col, style
                run.append(ch)
                if w == 2:  # an emoji: give it its own element so it cannot shift what follows
                    flush()
                    run, run_style = [], None
            col += w
        flush()
    out.append("</g></svg>")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", nargs="?", help="ANSI text (default: stdin)")
    ap.add_argument("--title", default="", help="window title")
    ap.add_argument("--font", type=float, default=14.0)
    ap.add_argument("--cols", type=int, default=0, help="pad the canvas to this many columns")
    a = ap.parse_args()
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    rows = parse(text)
    while rows and not rows[-1]:
        rows.pop()
    sys.stdout.write(svg(rows, a.title, a.font, cols=a.cols or None) + "\n")


if __name__ == "__main__":
    main()
