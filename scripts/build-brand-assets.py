#!/usr/bin/env python3
"""Generate the labelito downloadable brand-asset kit.

Reproducible generator (companion to scripts/fetch-icons.sh). It produces, under
``site/assets/brand/``:

  svg/  labelito-mark.svg                    - the mark, verbatim from the canonical source
        labelito-wordmark-{light,dark}.svg   - "labelito" (Inter 800) outlined to vector paths
        labelito-lockup-horizontal-{light,dark}.svg
        labelito-lockup-stacked-{light,dark}.svg
  png/  transparent-background rasters of the mark and every lockup

The wordmark is outlined to paths so the assets render identically without the Inter font
installed. Geometry is instantiated from the variable font at wght=800 (opsz left at its
default 14, matching the site's @font-face which pins only weight).

Dependencies (install into a throwaway venv):

    python3 -m venv .venv && .venv/bin/pip install fonttools brotli cairosvg

`brotli` is required for fonttools to read the .woff2 source. PNG rasterization uses cairosvg;
`inkscape --export-type=png -w <W> <in.svg> -o <out.png>` is an equivalent fallback.

Usage:

    .venv/bin/python scripts/build-brand-assets.py

Re-running overwrites site/assets/brand/** deterministically.
"""
from __future__ import annotations

import re
from pathlib import Path

import cairosvg
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
SITE_ASSETS = REPO / "site" / "assets"
FONT_SRC = SITE_ASSETS / "fonts" / "InterVariable.woff2"
MARK_SRC = SITE_ASSETS / "labelito-logo.svg"
OUT_SVG = SITE_ASSETS / "brand" / "svg"
OUT_PNG = SITE_ASSETS / "brand" / "png"

WORD = "labelito"
WEIGHT = 800
TRACKING_EM = -0.035  # matches the on-page wordmark's letter-spacing

# Brand ink colors (from brand.html tokens): --ink for dark backgrounds, near-black for light.
INK_DARK_BG = "#F4F7FB"   # white ink, sits on dark backgrounds
INK_LIGHT_BG = "#0F1621"  # dark ink, sits on light backgrounds
VARIANTS = {"dark": INK_DARK_BG, "light": INK_LIGHT_BG}

# The mark's native canvas (see labelito-logo.svg viewBox). Used as the unit system for lockups.
MARK_W, MARK_H = 100.0, 80.0

# Lockup composition, expressed as fractions of the mark height so it scales cleanly.
CAP_TARGET = 0.50 * MARK_H   # wordmark cap-height in lockup units
GAP_H = 0.28 * MARK_H        # mark->wordmark gap (horizontal lockup)
GAP_V = 0.34 * MARK_H        # mark->wordmark gap (stacked lockup)
PAD = 0.06 * MARK_H          # viewBox padding around composed lockups

# PNG raster widths (px). Height derives from each SVG's aspect ratio.
PNG_SIZES_MARK = (256, 512, 1024)
PNG_SIZES_LOCKUP = (512, 1024)


# ---------------------------------------------------------------------------
# Mark: reuse the canonical source verbatim
# ---------------------------------------------------------------------------
def read_mark_inner() -> str:
    """Return the inner markup (all <path> elements) of the canonical mark SVG."""
    text = MARK_SRC.read_text(encoding="utf-8")
    open_end = text.index(">", text.index("<svg")) + 1
    close = text.index("</svg>")
    return text[open_end:close].strip()


# ---------------------------------------------------------------------------
# Wordmark: outline "labelito" (Inter 800) to a single path in y-down SVG space
# ---------------------------------------------------------------------------
class Wordmark:
    """The outlined wordmark plus the metrics needed to compose lockups.

    ``d`` is a path drawn in font units, y-down (already flipped from the font's y-up),
    with the baseline at y=0 and the left side bearing at x=0. Bounds are in the same space.
    """

    def __init__(self, d: str, x0: float, x1: float, y_top: float, y_bot: float, cap: float):
        self.d = d
        self.x0, self.x1 = x0, x1
        self.y_top, self.y_bot = y_top, y_bot  # y-down: top < 0 (above baseline), bottom >= 0
        self.cap = cap

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def build_wordmark() -> Wordmark:
    font = TTFont(FONT_SRC)
    upem = font["head"].unitsPerEm
    cap = font["OS/2"].sCapHeight
    inst = instantiateVariableFont(font, {"wght": WEIGHT}, inplace=False)
    cmap = inst.getBestCmap()
    glyphset = inst.getGlyphSet()
    hmtx = inst["hmtx"]

    # Flip the font's y-up outlines into SVG y-down as we draw.
    flip = (1, 0, 0, -1, 0, 0)
    svg_pen = SVGPathPen(glyphset)
    bounds_pen = BoundsPen(glyphset)
    tracking = TRACKING_EM * upem

    x = 0.0
    for ch in WORD:
        gname = cmap[ord(ch)]
        offset = (1, 0, 0, -1, x, 0)  # translate by x, then flip y
        glyphset[gname].draw(TransformPen(svg_pen, offset))
        glyphset[gname].draw(TransformPen(bounds_pen, offset))
        x += hmtx[gname][0] + tracking

    xmin, ymin, xmax, ymax = bounds_pen.bounds  # y-down space
    # Normalize so the visual left edge sits at x=0.
    d = svg_pen.getCommands()
    return Wordmark(d=d, x0=xmin, x1=xmax, y_top=ymin, y_bot=ymax, cap=cap)


# ---------------------------------------------------------------------------
# SVG assembly
# ---------------------------------------------------------------------------
def svg_doc(view: tuple[float, float, float, float], body: str, label: str) -> str:
    vx, vy, vw, vh = view
    vb = f"{_n(vx)} {_n(vy)} {_n(vw)} {_n(vh)}"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'role="img" aria-label="{label}">\n{body}\n</svg>\n'
    )


def _n(v: float) -> str:
    """Compact number formatting for SVG output."""
    return f"{v:.3f}".rstrip("0").rstrip(".")


def wordmark_svg(wm: Wordmark, ink: str) -> str:
    view = (wm.x0, wm.y_top, wm.width, wm.y_bot - wm.y_top)
    body = f'  <path d="{wm.d}" fill="{ink}"/>'
    return svg_doc(view, body, WORD)


def _wordmark_group(wm: Wordmark, scale: float, tx: float, ty: float, ink: str) -> str:
    # Place the (baseline-at-0) wordmark: scale, then translate its origin to (tx, ty).
    return (
        f'  <g transform="translate({_n(tx)} {_n(ty)}) scale({_n(scale)})">'
        f'<path d="{wm.d}" fill="{ink}"/></g>'
    )


def lockup_horizontal(mark_inner: str, wm: Wordmark, ink: str) -> str:
    scale = CAP_TARGET / wm.cap
    # Vertically center the cap box (baseline..cap above it) on the mark's center.
    ty = MARK_H / 2 + (scale * wm.cap) / 2
    tx = MARK_W + GAP_H - scale * wm.x0

    word_top = ty + scale * wm.y_top
    word_bot = ty + scale * wm.y_bot
    word_right = tx + scale * wm.x1

    top = min(0.0, word_top) - PAD
    bot = max(MARK_H, word_bot) + PAD
    left = -PAD
    right = word_right + PAD

    body = f"  <g>{mark_inner}</g>\n" + _wordmark_group(wm, scale, tx, ty, ink)
    return svg_doc((left, top, right - left, bot - top), body, WORD)


def lockup_stacked(mark_inner: str, wm: Wordmark, ink: str) -> str:
    scale = CAP_TARGET / wm.cap
    word_w = scale * wm.width
    total_w = max(MARK_W, word_w)

    mark_x = (total_w - MARK_W) / 2
    # Wordmark baseline y so its cap box top sits GAP_V below the mark.
    ty = MARK_H + GAP_V + scale * wm.cap
    tx = (total_w - word_w) / 2 - scale * wm.x0

    word_top = ty + scale * wm.y_top
    word_bot = ty + scale * wm.y_bot

    top = min(0.0, word_top) - PAD
    bot = word_bot + PAD
    left = -PAD
    right = total_w + PAD

    body = (
        f'  <g transform="translate({_n(mark_x)} 0)">{mark_inner}</g>\n'
        + _wordmark_group(wm, scale, tx, ty, ink)
    )
    return svg_doc((left, top, right - left, bot - top), body, WORD)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def rasterize(svg_path: Path, sizes: tuple[int, ...]) -> None:
    stem = svg_path.stem
    for w in sizes:
        out = OUT_PNG / f"{stem}-{w}.png"
        cairosvg.svg2png(url=str(svg_path), write_to=str(out), output_width=w)
        print(f"  png  {out.relative_to(REPO)}")


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    print(f"  svg  {path.relative_to(REPO)}")


def main() -> None:
    OUT_SVG.mkdir(parents=True, exist_ok=True)
    OUT_PNG.mkdir(parents=True, exist_ok=True)

    mark_full = MARK_SRC.read_text(encoding="utf-8")
    mark_inner = read_mark_inner()
    wm = build_wordmark()

    print("SVG assets:")
    # Mark (verbatim copy under a stable kit name)
    mark_out = OUT_SVG / "labelito-mark.svg"
    write(mark_out, mark_full)

    for name, ink in VARIANTS.items():
        write(OUT_SVG / f"labelito-wordmark-{name}.svg", wordmark_svg(wm, ink))
        write(OUT_SVG / f"labelito-lockup-horizontal-{name}.svg",
              lockup_horizontal(mark_inner, wm, ink))
        write(OUT_SVG / f"labelito-lockup-stacked-{name}.svg",
              lockup_stacked(mark_inner, wm, ink))

    print("PNG assets:")
    rasterize(mark_out, PNG_SIZES_MARK)
    for name in VARIANTS:
        for kind in ("horizontal", "stacked"):
            rasterize(OUT_SVG / f"labelito-lockup-{kind}-{name}.svg", PNG_SIZES_LOCKUP)

    print("Done.")


if __name__ == "__main__":
    main()
