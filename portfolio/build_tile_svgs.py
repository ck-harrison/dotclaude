#!/usr/bin/env python3
"""Build the DotClaude portfolio tile SVGs (resting + hover).

All text is converted to outlined <path> shapes with fontTools, so the SVGs
need no fonts. Fonts are open-licence (SIL OFL): Inter 700/500 and JetBrains
Mono 500, from @fontsource on jsdelivr.

Usage: python build_tile_svgs.py <fonts_dir> <out_dir>
Needs: pip install fonttools brotli
"""
import os
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

SLUG = "dotclaude"

# Brand colours, taken from dotclaude.py (CSS variables) and icon.svg.
BG_TOP = "#2A211C"      # warm near-black, a step lighter than the app's dark bg
BG_BOT = "#1B1A18"      # the app's dark-mode --bg
TERRACOTTA = "#C2410C"  # the app's --accent
TERRA_LIGHT = "#F08A5D" # the app's dark-mode --accent
CREAM = "#FBF6F1"       # icon sheet
BLUSH = "#E9C9B5"       # icon fold + config lines
INK = "#1F1E1C"         # the app's --ink
MUTED = "#6B6862"       # the app's --muted

FONTS = {}


def font(path):
    if path not in FONTS:
        FONTS[path] = TTFont(path)
    return FONTS[path]


def r(v):
    return f"{round(v, 2):g}"


def text_path(fpath, s, size, x, baseline, anchor="start", tracking=0.0):
    """Return (d, width) for string s, outlined, at font size `size` units."""
    f = font(fpath)
    upm = f["head"].unitsPerEm
    gs = f.getGlyphSet()
    cmap = f.getBestCmap()
    hmtx = f["hmtx"]
    scale = size / upm
    names = [cmap[ord(c)] for c in s]
    adv = [hmtx[n][0] * scale + tracking * size for n in names]
    width = sum(adv) - tracking * size
    if anchor == "middle":
        x -= width / 2
    elif anchor == "end":
        x -= width
    pen = SVGPathPen(gs, ntos=r)
    cx = x
    for n, a in zip(names, adv):
        tp = TransformPen(pen, (scale, 0, 0, -scale, cx, baseline))
        gs[n].draw(tp)
        cx += a
    return pen.getCommands(), width


def text_width(fpath, s, size, tracking=0.0):
    return text_path(fpath, s, size, 0, 0, tracking=tracking)[1]


def fits(a, b, lo, hi, what):
    """Self-test: fail the build if a text run spills out of its box."""
    if a < lo or b > hi:
        raise SystemExit(f"LAYOUT FAIL: {what} spans {a:.1f}..{b:.1f}, box is {lo}..{hi}")


def svg_open(bg_id):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">'
        f'<rect width="800" height="600" fill="url(#{bg_id})"/>'
        f'<defs><linearGradient id="{bg_id}" x1="0" y1="0" x2="0.35" y2="1">'
        f'<stop offset="0" stop-color="{BG_TOP}"/><stop offset="1" stop-color="{BG_BOT}"/>'
        "</linearGradient></defs>"
    )


def sheet_mark(x, y, h):
    """DotClaude mark (sheet with folded corner, two config lines, a dot),
    flattened from icon.svg. Source box is 430x524 (x 290..720, y 250..774)."""
    s = h / 524
    t = f'translate({r(x)} {r(y)}) scale({r(s)})'
    # coordinates below are icon.svg's, shifted so the sheet starts at 0,0
    return (
        f'<g transform="{t}">'
        f'<path d="M40 0H310L430 120V484Q430 524 390 524H40Q0 524 0 484V40Q0 0 40 0Z" fill="{CREAM}"/>'
        f'<path d="M310 0V80Q310 120 350 120H430Z" fill="{BLUSH}"/>'
        f'<rect x="72" y="170" width="220" height="34" rx="17" fill="{BLUSH}"/>'
        f'<rect x="72" y="240" width="290" height="34" rx="17" fill="{BLUSH}"/>'
        f'<circle cx="140" cy="390" r="70" fill="{TERRACOTTA}"/>'
        "</g>"
    )


def build_tile(fd):
    bold = os.path.join(fd, "inter-latin-700-normal.woff2")
    size = 122
    tr = -0.025
    w_dot = text_width(bold, "Dot", size, tr)
    w_all = text_width(bold, "DotClaude", size, tr)
    x0 = 400 - w_all / 2
    baseline = 478
    d_dot, _ = text_path(bold, "Dot", size, x0, baseline, tracking=tr)
    gap = w_dot + tr * size  # advance after "Dot" including tracking
    d_cl, _ = text_path(bold, "Claude", size, x0 + gap, baseline, tracking=tr)
    mark_h = 250
    mark_w = mark_h * 430 / 524
    out = [svg_open(f"{SLUG}-tile-bg")]
    out.append(sheet_mark(400 - mark_w / 2, 72, mark_h))
    out.append(f'<path d="{d_dot}" fill="{TERRA_LIGHT}"/>')
    out.append(f'<path d="{d_cl}" fill="{CREAM}"/>')
    fits(x0, x0 + w_all, 72, 728, "wordmark")
    out.append("</svg>")
    return "".join(out), {"wordmark_size": size, "wordmark_width": round(w_all, 1),
                          "wordmark_x": (round(x0, 1), round(x0 + w_all, 1))}


def build_hover(fd):
    mono = os.path.join(fd, "jetbrains-mono-latin-500-normal.woff2")
    bold = os.path.join(fd, "inter-latin-700-normal.woff2")
    med = os.path.join(fd, "inter-latin-500-normal.woff2")
    out = [svg_open(f"{SLUG}-hover-bg")]

    # 1. Selected file row, as in the app's sidebar: mini mark + "CLAUDE.md"
    row_x, row_y, row_w, row_h = 48, 72, 704, 156
    out.append(f'<rect x="{row_x}" y="{row_y}" width="{row_w}" height="{row_h}" rx="30" fill="{TERRACOTTA}"/>')
    mh = 96
    out.append(sheet_mark(row_x + 40, row_y + (row_h - mh) / 2, mh))
    name_size = 92
    name_x = row_x + 40 + mh * 430 / 524 + 32
    d, w_name = text_path(mono, "CLAUDE.md", name_size, name_x, row_y + row_h / 2 + 35)
    fits(name_x, name_x + w_name, row_x + 24, row_x + row_w - 24, "CLAUDE.md")
    out.append(f'<path d="{d}" fill="{CREAM}"/>')

    # pointer from the row to the explanation card
    out.append(f'<path d="M150 228L186 264H114Z" fill="{TERRACOTTA}"/>')

    # 2. Action card: one click on "Optimise" (the app's best-practice
    #    review), and its result.
    cx, cy, cw, ch = 48, 264, 704, 264
    out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="30" fill="{CREAM}"/>')

    # button: sparkle + "Optimise"
    b_size = 64
    w_b = text_width(bold, "Optimise", b_size, -0.01)
    bx, by, bh = cx + 48, cy + 36, 112
    spark_r = 24
    bw = 44 + 2 * spark_r + 22 + w_b + 150  # room on the right for the cursor
    out.append(f'<rect x="{r(bx)}" y="{by}" width="{r(bw)}" height="{bh}" rx="{bh // 2}" fill="{TERRACOTTA}"/>')
    sx, sy = bx + 44 + spark_r, by + bh / 2
    out.append(
        f'<path d="M{r(sx)} {r(sy - spark_r)}Q{r(sx)} {r(sy)} {r(sx + spark_r)} {r(sy)}'
        f'Q{r(sx)} {r(sy)} {r(sx)} {r(sy + spark_r)}Q{r(sx)} {r(sy)} {r(sx - spark_r)} {r(sy)}'
        f'Q{r(sx)} {r(sy)} {r(sx)} {r(sy - spark_r)}Z" fill="{CREAM}"/>')
    tx = sx + spark_r + 22
    d, _ = text_path(bold, "Optimise", b_size, tx, by + bh / 2 + 23, tracking=-0.01)
    out.append(f'<path d="{d}" fill="{CREAM}"/>')
    fits(bx, bx + bw, cx + 32, cx + cw - 32, "Optimise button")

    # cursor clicking the button's right end, with a press ripple
    tipx, tipy = bx + bw - 54, by + bh / 2 + 4
    out.append(f'<circle cx="{r(tipx)}" cy="{r(tipy)}" r="30" fill="none" stroke="{CREAM}" '
               f'stroke-opacity="0.55" stroke-width="6"/>')
    out.append(f'<path transform="translate({r(tipx)} {r(tipy)}) scale(1.5)" '
               f'd="M0 0V62L15 48L26 72L38 67L27 43H48Z" fill="{INK}" stroke="{CREAM}" '
               f'stroke-width="4" stroke-linejoin="round"/>')

    # result: check + "3 fixes ready"
    ry = cy + 200
    out.append(f'<circle cx="{cx + 48 + 30}" cy="{ry}" r="30" fill="{TERRACOTTA}"/>')
    out.append(f'<path d="M{cx + 64} {ry + 1}L{cx + 74} {ry + 12}L{cx + 94} {ry - 11}" fill="none" '
               f'stroke="{CREAM}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>')
    r_size = 58
    d, w_line = text_path(bold, "3 fixes ready", r_size, cx + 128, ry + 21, tracking=-0.01)
    fits(cx + 128, cx + 128 + w_line, cx + 32, tipx - 24, "3 fixes ready (clear of cursor)")
    fits(tx, tx + w_b, bx, tipx - 34, "Optimise (clear of ripple)")
    out.append(f'<path d="{d}" fill="{INK}"/>')
    out.append("</svg>")
    return "".join(out), {"filename_width": round(w_name, 1),
                          "filename_right_edge": round(name_x + w_name, 1),
                          "line_right_edge": round(cx + 48 + w_line, 1)}


def main():
    fd, od = sys.argv[1], sys.argv[2]
    os.makedirs(od, exist_ok=True)
    for name, fn in (("tile", build_tile), ("hover", build_hover)):
        svg, info = fn(fd)
        p = os.path.join(od, f"{SLUG}-{name}.svg")
        with open(p, "w") as fh:
            fh.write(svg)
        with open(p + ".txt", "w") as fh:
            fh.write(svg)
        print(p, len(svg.encode()), "bytes", info)


if __name__ == "__main__":
    main()
