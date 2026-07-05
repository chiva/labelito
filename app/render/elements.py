# SPDX-License-Identifier: GPL-3.0-or-later
"""Element definitions and renderers for the label layout engine."""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# truetype() yields a FreeTypeFont; the load_default() fallback yields a bitmap ImageFont.
_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

# ── Constants ──────────────────────────────────────────────────────────────────
FONT_SIZES = {"title": 60, "subtitle": 40, "text": 32}
# Wrapped-line ceiling applied to a `text` element that declares no explicit `max_lines`. Body text
# previously defaulted to None ⇒ NO clamp, so a long static literal could wrap into an arbitrarily
# tall strip and OOM the worker. A thermal label is short, so 10 lines is generous headroom for every
# shipped template while keeping the strip bounded. The loader's strip-area guard assumes this same
# default for an uncapped text element (loader.DEFAULT_TEXT_MAX_LINES re-exports this value).
DEFAULT_TEXT_MAX_LINES = 10
ALIGN_DEFAULT = "left"
QR_DEFAULT_SIZE = 160
# Horizontal inset a left/right-aligned QR is pasted at; a column must hold the QR *plus* this inset
# or the code clips. Shared by QRElement.render and the row too-narrow guard so they stay in sync.
QR_ALIGN_INSET = 8
BARCODE_DEFAULT_HEIGHT = 60
LINE_DEFAULT_THICKNESS = 2
SPACER_DEFAULT_PX = 16
ICON_DEFAULT_SIZE = 80

# Two-color (red/black) printing. An element's `color` selects which layer it draws in. Only
# "red" is special; any other value (incl. the default) is black. The actual red rendering happens
# only when the engine activates two-color mode (red_active=True on the element); with two-color
# off, a `color: red` element draws black so a non-red print is unaffected (and byte-identical).
COLOR_DEFAULT = "black"
COLOR_RED = "red"
COLOR_CHOICES = frozenset({COLOR_DEFAULT, COLOR_RED})
# Pure-red ink brother_ql's convert(red=True) separates into the red layer (its HSV filter treats
# saturated, bright, red-hued pixels as red). RGB triples; black is the monochrome default.
RGB_RED = (255, 0, 0)
RGB_BLACK = (0, 0, 0)
RGB_WHITE = (255, 255, 255)

# Row container: gap between columns and default vertical alignment of columns.
ROW_DEFAULT_SPACING = 8
ROW_ALIGN_ITEMS_DEFAULT = "center"
VALIGN_CHOICES = frozenset({"top", "center", "bottom"})
# Height of the crossed-box marker drawn when a row column is too narrow to draw an image child
# (QR/barcode fall back to their own intended height, which is known; an image's is not).
ROW_FAILURE_PLACEHOLDER_HEIGHT = 64
# Minimum width reserved for each flexible column when fixed columns overflow the row, so required
# flexible content (e.g. a title) clips visibly instead of collapsing to a zero-width, silent gap.
ROW_MIN_FLEX_WIDTH = 24

# Bundled icon collections (rasterized from SVG) and the per-collection style variants we expose.
# Shared with the loader, which validates a template's `collection`/`style` against these.
KNOWN_COLLECTIONS = frozenset({"fontawesome", "material", "octicons"})
FA_STYLES = frozenset({"solid", "regular", "brands"})
ICON_DEFAULT_STYLE = "solid"
# Custom-asset (no collection) lookup precedence: prefer the crisper vector when both exist.
ICON_ASSET_EXTS = (".svg", ".png")


# ── Base ───────────────────────────────────────────────────────────────────────
@dataclass
class ElementBase:
    type: str
    padding_top: int = 4
    padding_bottom: int = 4
    # Layout hints honoured only when the element is a child of a `row`; inert otherwise
    # (consistent with the "unknown keys are ignored" contract for stand-alone elements).
    width: int | None = None  # fixed column width in px; None ⇒ flexible (shares leftover space)
    weight: int = 1  # flexible column's share of the row's leftover width
    valign: str = ""  # "" ⇒ inherit the row's align_items; else one of VALIGN_CHOICES
    # Linear scale factor for the whole coordinate system (1 = 300 dpi, 2 = 600 dpi high_res).
    # Threaded by build_element so EVERY pixel dimension a renderer computes — including dataclass
    # DEFAULTS that never appear in the template spec (Title/Subtitle font size, QR/icon size, spacer
    # size, line/box geometry, image max_height, row spacing) — is multiplied uniformly. This makes
    # 600 dpi a uniform 2x of the entire label geometry rather than a leak-prone "scale the fields
    # that happen to be present" patch, so defaults can never be left at 300 dpi size.
    # scale=1 is an exact no-op (every `* self.scale` is identity), preserving byte-identical 300 dpi
    # output. `weight` (a dimensionless ratio) is the sole pixel-unrelated field and is never scaled.
    scale: int = 1
    # Two-color (red/black) printing. `color` is the template-author hint: "red" draws this
    # element in the red layer, anything else in black. `_red_active` is engine-controlled (set by
    # build_element when the resolved render options enable two-color): it gates whether red is
    # honoured at all. When False, every element renders on an "L" (grayscale) canvas in black —
    # byte-identical to the monochrome pipeline — even if `color` is "red". When True, every element
    # renders on an "RGB" canvas (white bg) so a red element can paste pure-red ink for brother_ql's
    # convert(red=True) to separate into the red layer; non-red elements draw pure black on the same
    # RGB canvas. Threaded uniformly (incl. row children) so a whole label is one coherent mode.
    color: str = COLOR_DEFAULT
    _red_active: bool = False

    def _px(self, value: int) -> int:
        """Scale a pixel dimension by the element's coordinate-system scale factor."""
        return value * self.scale

    # ── Two-color helpers ───────────────────────────────────────────────────────
    @property
    def _canvas_mode(self) -> str:
        """PIL image mode for this element's strips: "RGB" in two-color mode, else "L"."""
        return "RGB" if self._red_active else "L"

    @property
    def _bg(self) -> int | tuple[int, int, int]:
        """White background fill matching :attr:`_canvas_mode` (255 for L, (255,255,255) for RGB)."""
        return RGB_WHITE if self._red_active else 255

    @property
    def _ink(self) -> int | tuple[int, int, int]:
        """Foreground fill for this element: pure red when active and color=red, else black.

        In the monochrome pipeline (``_red_active`` False) this is the integer ``0`` the original
        renderers used, so output is byte-identical. In two-color mode it is an RGB triple — red for
        a ``color: red`` element, black otherwise — drawn on the RGB canvas.
        """
        if not self._red_active:
            return 0
        return RGB_RED if self.color == COLOR_RED else RGB_BLACK

    def _new_canvas(self, width: int, height: int) -> Image.Image:
        """A blank white strip in the element's active canvas mode (L or RGB)."""
        return Image.new(self._canvas_mode, (max(0, width), max(0, height)), self._bg)

    def _tint(self, graphic: Image.Image) -> Image.Image:
        """Map an "L" graphic (0=ink, 255=white) to the element's active canvas mode.

        Monochrome pipeline (``_red_active`` False): returned unchanged ("L"), so a graphical
        element's bytes are identical to the monochrome render. Two-color mode: the dark (ink) pixels
        become the element's ``_ink`` colour (pure red for ``color: red``, else black) and the light
        pixels become white, on an RGB image — so brother_ql's convert(red=True) separates the red
        ink into the red layer and everything else into black. The graphic is expected pre-thresholded
        (the QR/barcode/icon/image renderers already 1-bit it), so a simple <128 split is exact.
        """
        if not self._red_active:
            return graphic
        ink = self._ink
        mask = graphic.point(lambda p: 255 if p < 128 else 0).convert("1")
        out = Image.new("RGB", graphic.size, RGB_WHITE)
        out.paste(Image.new("RGB", graphic.size, ink), (0, 0), mask)
        return out

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        raise NotImplementedError


# Last-resort fonts to try when DejaVu (the font the printed label uses) is absent — typically a
# bare macOS/Windows dev host that has not run scripts/fetch-fonts.sh. These are NOT the printer's
# font: their metrics and glyph coverage differ from DejaVu, so a preview rendered with one may wrap
# differently or cover different symbols than the printed label. We warn and still prefer real text
# over ImageFont.load_default()'s ASCII-only bitmap. (regular, bold) per family, tried in order.
_FALLBACK_FONTS: tuple[tuple[str, str], ...] = (
    # macOS
    (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc"),
    # Windows
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    # Linux distros that place DejaVu / a common sans outside the Debian path probed above
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    (
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ),
)

# Warn at most once per resolved fallback path (and once for the bitmap default): _load_font runs
# per text element per render, so an un-deduped warning would flood the logs.
_WARNED_FONT_FALLBACKS: set[str] = set()


def _warn_font_fallback(key: str, message: str, *args: object) -> None:
    if key not in _WARNED_FONT_FALLBACKS:
        _WARNED_FONT_FALLBACKS.add(key)
        log.warning(message, *args)


def _load_font(fonts_dir: Path, size: int, bold: bool = False) -> _Font:
    """Load DejaVu (the font the printed label uses) by name, with graceful fallbacks.

    Search order:
      1. ``fonts_dir`` — operator/dev copy (scripts/fetch-fonts.sh writes DejaVu here for local dev).
      2. ``/usr/share/fonts/truetype/dejavu`` — the Debian ``fonts-dejavu-core`` path baked into the
         Docker image, so the container stays faithful even when ``fonts_dir`` is an empty volume.
      3. A common OS sans (``_FALLBACK_FONTS``) — keeps a fontless dev host (macOS/Windows that has
         not fetched DejaVu) rendering real text instead of tofu; warned, since it is off-font.
      4. ``ImageFont.load_default()`` — PIL's ASCII-only bitmap, the final resort.
    """
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for directory in (fonts_dir, Path("/usr/share/fonts/truetype/dejavu")):
        path = directory / name
        if path.exists():
            return ImageFont.truetype(str(path), size)

    for regular, bold_path in _FALLBACK_FONTS:
        candidate = Path(bold_path if bold else regular)
        if candidate.exists():
            _warn_font_fallback(
                str(candidate),
                "DejaVu font not found; falling back to %s. Preview wrapping and glyph coverage may "
                "not match the printed label — run scripts/fetch-fonts.sh for a faithful preview.",
                candidate.name,
            )
            return ImageFont.truetype(str(candidate), size)

    _warn_font_fallback(
        "__bitmap__",
        "No scalable font found; using PIL's bitmap default. Non-ASCII glyphs (arrows, accents) will "
        "render as boxes and text metrics will not match the printer — run scripts/fetch-fonts.sh.",
    )
    return ImageFont.load_default()


def _safe_icon_name(name: str) -> str | None:
    """Return *name* if it is a single safe path component, else None.

    Icon names map to one file in a known directory. They can be ``{{field}}``-driven (i.e.
    request-controlled), so reject anything that could traverse the filesystem — separators, a
    leading dot (hidden/relative), or a parent reference. An explicit ``.svg``/``.png`` suffix is
    fine; only the embedded ``..`` sequence is forbidden.
    """
    name = name.strip()
    if not name or name.startswith(".") or "/" in name or "\\" in name or ".." in name:
        return None
    return name


def resolve_custom_icon_path(name: str, icons_dir: Path) -> Path | None:
    """Resolve a custom-asset icon *name* to an EXISTING file in *icons_dir*, or None.

    A custom asset (an ``icon`` element with no ``collection``) loads from ``icons_dir``: an explicit
    ``.svg``/``.png`` suffix is taken verbatim, otherwise ``<name>.svg`` is probed before
    ``<name>.png`` (vector preferred). Returns None when no matching file exists, so the same call
    both loads an icon (:meth:`IconElement._load_icon`) and detects a missing one at boot
    (:func:`app.render.engine.missing_custom_icons`). *name* must already be sanitized by
    :func:`_safe_icon_name`.

    ``OSError`` from :meth:`Path.exists` is treated as "missing": ``_safe_icon_name`` bounds the
    charset but not the length, and an overlong path component makes ``exists()`` raise
    ``ENAMETOOLONG`` (rather than return False) on some platforms/Python versions. Swallowing it here
    keeps a malformed-but-loadable icon name from crashing the render loop OR the startup/reload/save
    scan that calls this — it degrades to a blank strip plus a warning instead.
    """
    try:
        if Path(name).suffix.lower() in ICON_ASSET_EXTS:
            candidate = icons_dir / name
            return candidate if candidate.exists() else None
        for ext in ICON_ASSET_EXTS:
            candidate = icons_dir / f"{name}{ext}"
            if candidate.exists():
                return candidate
    except OSError:
        return None
    return None


def _rasterize_svg(path: Path, size: int) -> Image.Image:
    """Rasterize a (trusted, server-side) SVG to a 1-bit-thresholded grayscale square.

    Only ever called on bundled-collection or operator-placed asset files — never on
    request-supplied image data, which flows through :class:`ImageElement` instead. cairosvg parses
    with defusedxml, so external-entity expansion is disabled.
    """
    import cairosvg

    # Pass the SVG bytes directly rather than url=: cairosvg's url path opens an internal temp file
    # whose finalizer can emit an unraisable warning, and reading here keeps the trusted-input
    # boundary explicit (no cairosvg-side filesystem access).
    png_bytes = cairosvg.svg2png(
        bytestring=path.read_bytes(),
        output_width=size,
        output_height=size,
        background_color="white",
    )
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    return img.point(lambda p: 0 if p < 128 else 255)


def _wrap_text(text: str, font: _Font, max_width: int) -> list[str]:
    """Wrap text to fit within max_width pixels."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            test = current + " " + word
            bbox = font.getbbox(test)
            if bbox[2] - bbox[0] <= max_width:
                current = test
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _render_text_block(
    text: str,
    font: _Font,
    canvas_width: int,
    align: str,
    max_lines: int | None = None,
    padding_h: int = 8,
    scale: int = 1,
    mode: str = "L",
    bg: int | tuple[int, int, int] = 255,
    fill: int | tuple[int, int, int] = 0,
) -> Image.Image:
    """Render wrapped text into a new image of the correct height.

    ``scale`` multiplies the layout paddings/line-spacing so they grow with the (already
    scale-sized) font, keeping the whole text block uniformly 2x under 600 dpi high_res.
    ``padding_h`` is given pre-scaled by the caller (it derives from ``self._px``-scaled insets).

    ``mode``/``bg``/``fill`` carry the two-color canvas mode and ink: in the monochrome
    pipeline they default to the original ``"L"``/255/0 so output is byte-identical; in two-color
    mode the caller passes ``"RGB"`` with a white-tuple background and a red/black ink tuple.
    """
    line_gap = 8 * scale
    block_pad = 8 * scale
    top_pad = 4 * scale
    effective_width = canvas_width - 2 * padding_h
    lines = _wrap_text(text, font, effective_width)
    if max_lines:
        lines = lines[:max_lines]

    sample_bbox = font.getbbox("Ay")
    line_height = (sample_bbox[3] - sample_bbox[1]) + line_gap

    total_height = line_height * len(lines) + block_pad
    img = Image.new(mode, (canvas_width, total_height), bg)
    draw = ImageDraw.Draw(img)

    y = top_pad
    for line in lines:
        bbox = font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        if align == "center":
            x = (canvas_width - text_w) // 2
        elif align == "right":
            x = canvas_width - text_w - padding_h
        else:
            x = padding_h
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height

    return img


# ── Text element types ─────────────────────────────────────────────────────────
@dataclass
class TitleElement(ElementBase):
    type: str = "title"
    text: str = ""
    align: str = ALIGN_DEFAULT
    max_lines: int | None = 2
    bold: bool = True

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        text = str(resolved_fields.get("__text__", self.text))
        font = _load_font(fonts_dir, self._px(FONT_SIZES["title"]), self.bold)
        return _render_text_block(
            text,
            font,
            canvas_width,
            self.align,
            self.max_lines,
            self._px(8),
            self.scale,
            self._canvas_mode,
            self._bg,
            self._ink,
        )


@dataclass
class SubtitleElement(ElementBase):
    type: str = "subtitle"
    text: str = ""
    align: str = ALIGN_DEFAULT
    max_lines: int | None = 2
    bold: bool = False

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        text = str(resolved_fields.get("__text__", self.text))
        if not text.strip():
            return self._new_canvas(canvas_width, 0)
        font = _load_font(fonts_dir, self._px(FONT_SIZES["subtitle"]), self.bold)
        return _render_text_block(
            text,
            font,
            canvas_width,
            self.align,
            self.max_lines,
            self._px(8),
            self.scale,
            self._canvas_mode,
            self._bg,
            self._ink,
        )


@dataclass
class TextElement(ElementBase):
    type: str = "text"
    text: str = ""
    size: int = FONT_SIZES["text"]
    align: str = ALIGN_DEFAULT
    bold: bool = False
    # Finite default (was None ⇒ no clamp): an uncapped text element could wrap a long static
    # literal into an unbounded strip and OOM. The loader's strip-area guard assumes this same
    # ceiling for text that omits max_lines, so the guard cannot be bypassed by simply leaving it off.
    max_lines: int | None = DEFAULT_TEXT_MAX_LINES

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        text = str(resolved_fields.get("__text__", self.text))
        font = _load_font(fonts_dir, self._px(self.size), self.bold)
        return _render_text_block(
            text,
            font,
            canvas_width,
            self.align,
            self.max_lines,
            self._px(8),
            self.scale,
            self._canvas_mode,
            self._bg,
            self._ink,
        )


# ── QR element ─────────────────────────────────────────────────────────────────
@dataclass
class QRElement(ElementBase):
    type: str = "qr"
    data: str = ""
    size: int = QR_DEFAULT_SIZE
    align: str = "center"

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        import qrcode

        data = str(resolved_fields.get("__data__", self.data))
        if not data.strip():
            return self._new_canvas(canvas_width, 0)

        size = self._px(self.size)
        inset = self._px(QR_ALIGN_INSET)
        pad = self._px(4)
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("L")
        qr_img = self._tint(qr_img.resize((size, size), Image.LANCZOS))

        canvas = self._new_canvas(canvas_width, size + 2 * pad)
        if self.align == "center":
            x = (canvas_width - size) // 2
        elif self.align == "right":
            x = canvas_width - size - inset
        else:
            x = inset
        canvas.paste(qr_img, (x, pad))
        return canvas


# ── Barcode element ────────────────────────────────────────────────────────────
@dataclass
class BarcodeElement(ElementBase):
    type: str = "barcode"
    data: str = ""
    symbology: str = "code128"
    height: int = BARCODE_DEFAULT_HEIGHT
    align: str = "center"
    show_value: bool = False

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        import barcode as python_barcode
        from barcode.writer import ImageWriter

        data = str(resolved_fields.get("__data__", self.data))
        if not data.strip():
            return self._new_canvas(canvas_width, 0)

        inset = self._px(8)
        pad = self._px(4)
        bc_class = python_barcode.get_barcode_class(self.symbology)
        buf = io.BytesIO()
        writer = ImageWriter()
        # The generator's built-in value text is tiny/unstyled and outside labelito's font
        # control, so bars-only is the default — value display belongs to the template's own
        # `text` elements. `show_value: true` re-enables it for quick templates.
        bc_class(data, writer=writer).write(buf, options={"write_text": bool(self.show_value)})
        buf.seek(0)
        bc_img = Image.open(buf).convert("L")

        new_w = canvas_width - 2 * inset
        bc_scale = new_w / bc_img.width
        new_h = int(bc_img.height * bc_scale)
        if new_w <= 0 or new_h <= 0:
            # Column too narrow to draw into (e.g. a tiny fixed `width` or a flex column
            # squeezed to zero inside a row). Degrade to an empty strip rather than letting
            # Image.resize raise ValueError and turn the request into a 500.
            return self._new_canvas(canvas_width, 0)
        bc_img = self._tint(bc_img.resize((new_w, new_h), Image.LANCZOS))

        canvas = self._new_canvas(canvas_width, new_h + 2 * pad)
        if self.align == "center":
            x = (canvas_width - new_w) // 2
        elif self.align == "right":
            x = canvas_width - new_w - inset
        else:
            x = inset
        canvas.paste(bc_img, (x, pad))
        return canvas


# ── Image element ──────────────────────────────────────────────────────────────
@dataclass
class ImageElement(ElementBase):
    type: str = "image"
    field: str = "image"
    max_height: int = 200
    align: str = "center"

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        raw = resolved_fields.get(self.field)
        if not raw:
            return self._new_canvas(canvas_width, 0)

        if isinstance(raw, bytes):
            img_bytes = raw
        else:
            img_bytes = base64.b64decode(raw)

        inset = self._px(8)
        pad = self._px(4)
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        fit = min((canvas_width - 2 * inset) / img.width, self._px(self.max_height) / img.height)
        new_w, new_h = int(img.width * fit), int(img.height * fit)
        if new_w <= 0 or new_h <= 0:
            # Column too narrow to draw into (e.g. a tiny fixed `width` or a flex column
            # squeezed to zero inside a row). Degrade to an empty strip rather than letting
            # Image.resize raise ValueError and turn the request into a 500.
            return self._new_canvas(canvas_width, 0)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        img = img.point(lambda p: 0 if p < 128 else 255)  # 1-bit dither
        img = self._tint(img)

        canvas = self._new_canvas(canvas_width, new_h + 2 * pad)
        if self.align == "center":
            x = (canvas_width - new_w) // 2
        elif self.align == "right":
            x = canvas_width - new_w - inset
        else:
            x = inset
        canvas.paste(img, (x, pad))
        return canvas


# ── Icon element ───────────────────────────────────────────────────────────────
@dataclass
class IconElement(ElementBase):
    """A named, server-side graphic rendered at a fixed size.

    Two sources, selected by :attr:`collection`:

    * **collection unset** — a custom asset in ``icons_dir``. The name resolves to ``<name>.svg``
      then ``<name>.png`` (vector preferred); an explicit suffix in the name forces that file.
    * **collection set** — a bundled SVG from ``icon_collections_dir/<collection>/``. FontAwesome
      additionally selects a :attr:`style` subdirectory (``solid``/``regular``/``brands``).

    SVG sources are rasterized via cairosvg; PNG sources keep the original open/resize/threshold
    path. A missing, unknown, or unsafe reference renders a blank strip (the label still prints).
    """

    type: str = "icon"
    name: str = ""
    size: int = ICON_DEFAULT_SIZE
    align: str = "center"
    collection: str = ""
    style: str = ICON_DEFAULT_STYLE

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        icon_name = str(resolved_fields.get("__name__", self.name))
        size = self._px(self.size)
        inset = self._px(8)
        pad = self._px(4)
        blank = self._new_canvas(canvas_width, size + 2 * pad)

        icon = self._load_icon(icon_name, icons_dir, icon_collections_dir)
        if icon is None:
            return blank

        icon = self._tint(icon)
        canvas = blank
        if self.align == "center":
            x = (canvas_width - size) // 2
        elif self.align == "right":
            x = canvas_width - size - inset
        else:
            x = inset
        canvas.paste(icon, (x, pad))
        return canvas

    def _resolve_path(self, name: str, icons_dir: Path, icon_collections_dir: Path) -> Path | None:
        """Map a (sanitized) icon name to a file path per the collection/asset resolution rules."""
        if self.collection:
            if self.collection not in KNOWN_COLLECTIONS:
                return None
            base = icon_collections_dir / self.collection
            if self.collection == "fontawesome":
                style = self.style if self.style in FA_STYLES else ICON_DEFAULT_STYLE
                base = base / style
            return base / f"{name}.svg"

        # Custom asset: existence-checked resolution shared with the boot warning (single source of
        # truth for the svg→png probe and explicit-suffix rule).
        return resolve_custom_icon_path(name, icons_dir)

    def _load_icon(
        self, name: str, icons_dir: Path, icon_collections_dir: Path
    ) -> Image.Image | None:
        """Resolve, load, and 1-bit-threshold an icon to a ``size`` by ``size`` image, or None.

        Returns None for a missing, unknown, unsafe, OR corrupt/undecodable reference so the caller
        renders a blank strip and the label still prints (see :meth:`render`). A present-but-broken
        file (e.g. a truncated PNG or malformed SVG) must not raise past here into the render loop.
        """
        safe = _safe_icon_name(name)
        if safe is None:
            log.warning("icon: rejected unsafe icon name %r", name)
            return None

        path = self._resolve_path(safe, icons_dir, icon_collections_dir)
        if path is None or not path.exists():
            log.warning(
                "icon: no file for name=%r collection=%r style=%r",
                name,
                self.collection,
                self.style,
            )
            return None

        size = self._px(self.size)
        try:
            if path.suffix.lower() == ".svg":
                return _rasterize_svg(path, size)
            icon = Image.open(path).convert("L").resize((size, size), Image.LANCZOS)
            return icon.point(lambda p: 0 if p < 128 else 255)
        except Exception as exc:
            # Trusted server-side file, but corrupt/unreadable (bad bytes, truncation, malformed
            # SVG): degrade to a blank strip rather than 500 the whole render.
            log.warning("icon: failed to load name=%r path=%s: %s", name, path, exc)
            return None


# ── Structural elements ────────────────────────────────────────────────────────
@dataclass
class LineElement(ElementBase):
    type: str = "line"
    thickness: int = LINE_DEFAULT_THICKNESS
    margin: int = 8

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        thickness = self._px(self.thickness)
        margin = self._px(self.margin)
        h = thickness + margin * 2
        img = self._new_canvas(canvas_width, h)
        if canvas_width - margin < margin or thickness < 1:
            # Column too narrow for the rule between its margins (e.g. a tiny fixed `width` or a
            # flex column squeezed inside a row). Drawing here would pass ImageDraw.rectangle an
            # inverted x-range and raise ValueError; return the blank strip instead.
            return img
        draw = ImageDraw.Draw(img)
        draw.rectangle(
            (
                margin,
                margin,
                canvas_width - margin,
                margin + thickness - 1,
            ),
            fill=self._ink,
        )
        return img


@dataclass
class BoxElement(ElementBase):
    type: str = "box"
    height: int = 40
    border: int = 2

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        height = self._px(self.height)
        border = self._px(self.border)
        img = self._new_canvas(canvas_width, height)
        if canvas_width < 1 or height < 1:
            # Column too narrow/short to outline (e.g. a flex column squeezed to zero inside a row);
            # ImageDraw.rectangle would get an inverted range and raise. Return the blank strip.
            return img
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, canvas_width - 1, height - 1), outline=self._ink, width=border)
        return img


@dataclass
class SpacerElement(ElementBase):
    type: str = "spacer"
    size: int = SPACER_DEFAULT_PX

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        return self._new_canvas(canvas_width, self._px(self.size))


def _failure_placeholder(
    width: int,
    height: int,
    mode: str = "L",
    bg: int | tuple[int, int, int] = 255,
    ink: int | tuple[int, int, int] = 0,
) -> Image.Image:
    """A bordered box with a diagonal cross, marking a row column too narrow to draw its content.

    Drawn in place of a QR/barcode/image whose column would otherwise clip or blank the content, so
    the failed render is visible on the printed label instead of silently missing.

    ``mode``/``bg``/``ink`` default to the original monochrome ``"L"``/255/0 (byte-identical), and
    are set to the child element's two-color canvas mode/ink in red mode so the placeholder pastes
    cleanly onto an RGB row canvas and inherits the failed child's colour.
    """
    w, h = max(1, width), max(1, height)
    img = Image.new(mode, (w, h), bg)
    draw = ImageDraw.Draw(img)
    stroke = max(1, min(w, h) // 20)
    draw.rectangle((0, 0, w - 1, h - 1), outline=ink, width=stroke)
    draw.line((0, 0, w - 1, h - 1), fill=ink, width=stroke)
    draw.line((0, h - 1, w - 1, 0), fill=ink, width=stroke)
    return img


# ── Row container ────────────────────────────────────────────────────────────────
@dataclass
class RowElement(ElementBase):
    """A horizontal band that lays its child elements out in side-by-side columns.

    Each child is rendered into a column-width sub-strip *by its own renderer*, so every
    per-element behaviour (alignment, padding, fonts, empty-handling) is reused verbatim; the
    columns are then composited left-to-right into a single full-width strip that slots into the
    engine's unchanged vertical stack.

    Column widths use a fixed-then-flex model: children with an explicit :attr:`~ElementBase.width`
    reserve that many pixels first, then the leftover (after inter-column ``spacing``) is divided
    among the rest in proportion to their :attr:`~ElementBase.weight`. Each child is placed
    vertically per its :attr:`~ElementBase.valign`, falling back to the row's :attr:`align_items`.
    """

    type: str = "row"
    children: list[ElementBase] = field(default_factory=list)
    align_items: str = ROW_ALIGN_ITEMS_DEFAULT
    spacing: int = ROW_DEFAULT_SPACING

    def render(
        self,
        canvas_width: int,
        resolved_fields: dict[str, Any],
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
    ) -> Image.Image:
        child_res = resolved_fields.get("__children__") or [{} for _ in self.children]
        widths = self._column_widths(canvas_width)
        strips: list[Image.Image] = []
        for child, w, res in zip(self.children, widths, child_res, strict=True):
            # A data-bearing child (QR/barcode/image) given a column too narrow to draw its content
            # would otherwise vanish silently — a QR clips, a barcode/image collapses to a blank
            # strip — while the API still reports a successful print (and for image jobs the blob is
            # then stripped from history, so the loss is unrecoverable). Replace that silent gap with
            # a visible crossed box so the failure is unmistakable on the printed label. QR clipping
            # is predicted from its fixed size (it never blanks); barcode/image are detected by the
            # blank strip their own renderers return when the column collapses.
            if (
                isinstance(child, QRElement)
                and self._child_has_content(child, res)
                and w
                < child._px(child.size)
                + (0 if child.align == "center" else child._px(QR_ALIGN_INSET))
            ):
                strips.append(
                    _failure_placeholder(
                        w, child._px(child.size), child._canvas_mode, child._bg, child._ink
                    )
                )
                continue
            strip = child.render(w, res, fonts_dir, icons_dir, icon_collections_dir)
            if (
                isinstance(child, BarcodeElement | ImageElement)
                and self._child_has_content(child, res)
                and strip.height == 0
            ):
                marker_h = (
                    child._px(child.height)
                    if isinstance(child, BarcodeElement)
                    else child._px(ROW_FAILURE_PLACEHOLDER_HEIGHT)
                )
                strip = _failure_placeholder(w, marker_h, child._canvas_mode, child._bg, child._ink)
            strips.append(strip)
        row_h = max((s.height for s in strips), default=0)
        canvas = self._new_canvas(canvas_width, row_h)
        if row_h == 0:
            return canvas

        x = 0
        for child, w, strip in zip(self.children, widths, strips, strict=True):
            # Once a column starts at or past the right edge it is fully off-canvas, and so is every
            # later column (x only grows). Stop before handing PIL a coordinate it can't take: an
            # absurd `spacing` (e.g. a 300-digit YAML int) survives load — the loader only checks
            # int >= 0 — and would otherwise reach Image.paste as a giant int and raise OverflowError.
            if x >= canvas_width:
                break
            valign = child.valign or self.align_items
            if valign == "top":
                y = 0
            elif valign == "bottom":
                y = row_h - strip.height
            else:  # "center"
                y = (row_h - strip.height) // 2
            canvas.paste(strip, (x, y))
            x += w + self._px(self.spacing)
        return canvas

    @staticmethod
    def _child_has_content(child: ElementBase, resolved: dict[str, Any]) -> bool:
        """Whether a data-bearing child actually has content to draw for this render.

        Used to scope the too-narrow-column guard to columns that would *drop real content*, so a
        genuinely empty optional field (which legitimately renders a blank strip) is never rejected.
        """
        if isinstance(child, QRElement | BarcodeElement):
            return bool(str(resolved.get("__data__", child.data)).strip())
        if isinstance(child, ImageElement):
            return bool(resolved.get(child.field))
        return False

    def _column_widths(self, canvas_width: int) -> list[int]:
        """Allocate per-column pixel widths: fixed-width children first, leftover split by weight.

        The last flexible column absorbs integer-division rounding so the columns plus gaps sum to
        the available width exactly.

        When the fixed widths alone overflow the available width (after gaps), every fixed column is
        scaled down proportionally to fit instead of overflowing the canvas. This serves two ends:
        it bounds the sub-strip each child renderer allocates (a typo like ``width: 1_000_000_000``
        scales down to the row width rather than allocating a billion-pixel image), and it keeps
        every column on-canvas so an over-wide layout can't silently push a later column (a QR or
        barcode) off the edge while the API still reports success. Whenever flexible children exist
        and the fixed/spacing budget would leave them less than ``ROW_MIN_FLEX_WIDTH`` each, that
        minimum is reserved first (capped at ``avail``) and the fixed columns are scaled into the
        remainder, so required flexible content (e.g. a title) clips visibly rather than collapsing
        to a zero-width, silent gap.
        """
        n = len(self.children)
        if n == 0:
            return []
        # Every layout quantity lives in the same (possibly 2x high_res) coordinate system as
        # ``canvas_width``: spacing, fixed-column widths, and the flex minimum are all scaled by the
        # row's ``scale`` so the column allocation is a uniform 2x under 600 dpi (children share the
        # same scale, so their rendered content matches the columns they are handed).
        avail = max(0, canvas_width - self._px(self.spacing) * (n - 1))
        fixed_total = sum(self._px(c.width) for c in self.children if c.width is not None)
        flex = [c for c in self.children if c.width is None]
        # Scale fixed widths down only when they overshoot the row; the common (fitting) case keeps
        # exact requested widths. Integer arithmetic throughout: an absurdly large ``width`` (e.g. a
        # 300-digit YAML int) stays an exact Python int instead of being coerced to float, which
        # would raise OverflowError on a loadable-but-malicious template.
        min_flex = min(self._px(ROW_MIN_FLEX_WIDTH) * len(flex), avail) if flex else 0
        # Scale fixed columns whenever they can't fit alongside the reserved flex minimum — not only
        # on strict overflow. Exact-fit fixed widths (fixed_total == avail) or a spacing that eats the
        # row would otherwise leave flex children 0 px and silently drop their content. Reserving the
        # minimum first keeps flexible content visible (clipped) rather than vanishing.
        scale_fixed = fixed_total > avail - min_flex
        if scale_fixed:
            fixed_budget = max(0, avail - min_flex)
            flex_avail = min_flex
        else:
            fixed_budget = avail
            flex_avail = max(0, avail - fixed_total)
        weight_total = sum(max(0, c.weight) for c in flex) or 1
        last_flex = flex[-1] if flex else None

        widths: list[int] = []
        used = 0
        for c in self.children:
            if c.width is not None:
                # Integer floor scaling keeps the scaled total within ``fixed_budget`` without float
                # math: an absurd ``width`` (e.g. a 300-digit YAML int) stays an exact Python int
                # instead of being coerced to float, which would raise OverflowError. The column
                # width is read in the row's scaled coordinate system (``_px``) so high_res columns
                # are a uniform 2x — matching the scaled ``fixed_total``/``avail`` above.
                cw = self._px(c.width)
                w = cw * fixed_budget // fixed_total if scale_fixed else cw
            elif c is last_flex:
                w = flex_avail - used  # absorb rounding so columns + gaps fill the row exactly
            else:
                w = flex_avail * max(0, c.weight) // weight_total
                used += w
            widths.append(max(0, w))
        return widths


# ── Factory ────────────────────────────────────────────────────────────────────
ELEMENT_REGISTRY: dict[str, type[ElementBase]] = {
    "title": TitleElement,
    "subtitle": SubtitleElement,
    "text": TextElement,
    "qr": QRElement,
    "barcode": BarcodeElement,
    "image": ImageElement,
    "icon": IconElement,
    "line": LineElement,
    "box": BoxElement,
    "spacer": SpacerElement,
    "row": RowElement,
}


def build_element(spec: dict[str, Any], scale: int = 1, red_active: bool = False) -> ElementBase:
    """Instantiate an element from a template spec dict.

    ``scale`` (default 1) is the linear scale factor of the whole label coordinate system: 1 at
    300 dpi, 2 for 600 dpi high-resolution mode. Rather than multiplying the pixel-valued
    fields that *happen to appear* in the spec — which silently left dataclass DEFAULTS (e.g. the
    Title/Subtitle font size, QR/icon size, spacer/line/image/row defaults) at 300 dpi size and
    shrank them on the doubled canvas — the factory simply records ``scale`` on the element
    (and threads it into row children). Every renderer then multiplies EACH of its pixel dimensions
    by ``self.scale`` via ``self._px`` at draw time, so defaults and explicit values scale together,
    uniformly. The template author still supplies field values in 300 dpi units; high_res is a pure
    presentation-time magnification. ``scale=1`` is an exact no-op, preserving byte-identical 300 dpi
    output. ``weight`` (a dimensionless ratio) is the only pixel-unrelated field and is never scaled.

    ``red_active`` (default False) is engine-controlled two-color state: when True, every
    element renders on an RGB canvas (so a ``color: red`` element can draw pure red) and the driver
    is told to separate the red layer; when False, every element renders monochrome ("L") —
    a ``color: red`` element simply prints black. It is threaded into row children so the
    whole label shares one coherent mode. Like ``scale`` it can never be set from a template spec; it
    is set on the constructed element after key-filtering. ``color`` IS a template key (recorded as a
    normal field) but is only honoured when ``red_active`` is True.
    """
    el_type = spec.get("type", "text")
    cls = ELEMENT_REGISTRY.get(el_type)
    if cls is None:
        raise ValueError(f"Unknown element type: {el_type!r}")
    import dataclasses

    known = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in spec.items() if k in known}
    # A template must never override the scale factor or the engine-controlled red flag.
    filtered.pop("scale", None)
    filtered.pop("_red_active", None)
    if scale != 1:
        filtered["scale"] = scale
    # Container elements (row) carry a list of child specs; build them recursively at the same scale
    # AND the same two-color mode so children share the row's canvas mode/ink semantics.
    if isinstance(filtered.get("children"), list):
        filtered["children"] = [
            build_element(c, scale=scale, red_active=red_active)
            for c in filtered["children"]
            if isinstance(c, dict)
        ]
    el = cls(**filtered)
    el._red_active = red_active
    return el
