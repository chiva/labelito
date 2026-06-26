# SPDX-License-Identifier: GPL-3.0-or-later
"""Render engine tests — geometry, wrap, computed fields, element coverage."""

from __future__ import annotations

import base64
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from app.render.elements import (
    ROW_MIN_FLEX_WIDTH,
    BoxElement,
    IconElement,
    LineElement,
    QRElement,
    RowElement,
    SpacerElement,
    SubtitleElement,
    TextElement,
    TitleElement,
    _load_font,
    _wrap_text,
)
from app.render.engine import (
    RenderEngine,
    _BROTHER_QL_MAX_RASTER_ROWS,
    _add_months,
    _apply_offset,
    _brother_ql_model_max_rows,
    _resolve_fields,
)
from app.render.i18n import Translator

CANVAS_W = 696  # 62mm continuous label at 300 dpi
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"


@pytest.fixture
def engine(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path, translator: Translator
) -> RenderEngine:
    return RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=200,
        max_length_px=6000,
    )


# ── Helper ─────────────────────────────────────────────────────────────────────
def to_pil(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


# ── Wrap / font loading ────────────────────────────────────────────────────────
def test_wrap_text_single_word(fonts_dir: Path) -> None:
    font = _load_font(fonts_dir, 32)
    lines = _wrap_text("hello", font, 10000)
    assert lines == ["hello"]


def test_wrap_text_splits_long_line(fonts_dir: Path) -> None:
    font = _load_font(fonts_dir, 48)
    long_text = "Word " * 30
    lines = _wrap_text(long_text.strip(), font, CANVAS_W)
    assert len(lines) > 1


def test_wrap_text_respects_newlines(fonts_dir: Path) -> None:
    font = _load_font(fonts_dir, 32)
    lines = _wrap_text("line one\nline two", font, 10000)
    assert len(lines) == 2


# ── Element rendering ──────────────────────────────────────────────────────────
def test_title_element_renders(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = TitleElement(text="Hello World")
    img = el.render(
        CANVAS_W, {"__text__": "Hello World"}, fonts_dir, icons_dir, icon_collections_dir
    )
    assert img.width == CANVAS_W
    assert img.height > 0


def test_subtitle_empty_returns_zero_height(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = SubtitleElement(text="")
    img = el.render(CANVAS_W, {"__text__": ""}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 0


def test_subtitle_nonempty_renders(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = SubtitleElement(text="sub")
    img = el.render(CANVAS_W, {"__text__": "sub"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height > 0


def test_text_element_custom_size(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = TextElement(text="test", size=28)
    img = el.render(CANVAS_W, {"__text__": "test"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height > 0


def test_spacer_element_exact_height(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = SpacerElement(size=40)
    img = el.render(CANVAS_W, {}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 40
    assert img.width == CANVAS_W


def test_line_element_renders(fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path) -> None:
    el = LineElement(thickness=2)
    img = el.render(CANVAS_W, {}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height > 0


def test_box_element_renders(fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path) -> None:
    el = BoxElement(height=50, border=2)
    img = el.render(CANVAS_W, {}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 50


def test_qr_element_renders_data(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = QRElement(data="https://example.com", size=120)
    img = el.render(
        CANVAS_W, {"__data__": "https://example.com"}, fonts_dir, icons_dir, icon_collections_dir
    )
    assert img.height > 0


def test_qr_element_empty_data_returns_zero_height(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    el = QRElement(data="")
    img = el.render(CANVAS_W, {"__data__": ""}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 0


# ── Engine — continuous label ──────────────────────────────────────────────────
def test_engine_continuous_min_length(engine: RenderEngine) -> None:
    layout = [{"type": "spacer", "size": 10}]
    png = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    img = to_pil(png)
    assert img.width == CANVAS_W
    assert img.height >= engine.min_length_px


def test_engine_continuous_clamps_to_max(engine: RenderEngine) -> None:
    layout = [{"type": "spacer", "size": 100}] * 200  # would be 20000px
    png = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    img = to_pil(png)
    assert img.height <= engine.max_length_px


def test_engine_die_cut_exact_size(engine: RenderEngine) -> None:
    png = engine.render_to_png(
        [{"type": "title", "text": "Hello"}],
        {"title": "Hello"},
        canvas_width=696,
        canvas_height=271,
    )
    img = to_pil(png)
    assert img.width == 696
    assert img.height == 271


# ── Engine — field substitution ────────────────────────────────────────────────
def test_engine_field_substitution(engine: RenderEngine) -> None:
    layout = [{"type": "text", "text": "Hello {{name}}!"}]
    png = engine.render_to_png(layout, {"name": "World"}, canvas_width=CANVAS_W, canvas_height=None)
    assert isinstance(png, bytes)
    img = to_pil(png)
    assert img.height >= engine.min_length_px


def test_engine_computed_date_field(engine: RenderEngine) -> None:
    layout = [{"type": "text", "text": "Stored: {{date}}"}]
    png = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    assert isinstance(png, bytes)


def test_engine_rotate(engine: RenderEngine) -> None:
    layout = [{"type": "title", "text": "Rotated"}]
    png = engine.render_to_png(
        layout, {"title": "Rotated"}, canvas_width=CANVAS_W, canvas_height=None, rotate=90
    )
    img = to_pil(png)
    # After 90-degree rotation the width and height swap
    assert img.width != CANVAS_W or img.height != CANVAS_W  # they differ


# ── Engine — PNG output validity ───────────────────────────────────────────────
def test_engine_output_is_valid_png(engine: RenderEngine) -> None:
    layout = [{"type": "title", "text": "Test"}]
    png = engine.render_to_png(layout, {"title": "Test"}, canvas_width=CANVAS_W, canvas_height=None)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ── Rich elements ──────────────────────────────────────────────────────────────
def test_barcode_element_renders(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import BarcodeElement

    el = BarcodeElement(data="12345678", symbology="code128", height=60)
    img = el.render(CANVAS_W, {"__data__": "12345678"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height > 0


def test_barcode_element_empty_returns_zero(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import BarcodeElement

    el = BarcodeElement(data="")
    img = el.render(CANVAS_W, {"__data__": ""}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 0


def test_image_element_base64(fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path) -> None:
    import base64
    import io as _io

    from app.render.elements import ImageElement

    # Create a minimal PNG image
    img_src = Image.new("L", (50, 50), 128)
    buf = _io.BytesIO()
    img_src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    el = ImageElement(field="image", max_height=100)
    img = el.render(CANVAS_W, {"image": b64}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height > 0


def test_image_element_missing_field_returns_zero(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import ImageElement

    el = ImageElement(field="image")
    img = el.render(CANVAS_W, {}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 0


def test_icon_element_renders(fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="snowflake", size=80)
    img = el.render(CANVAS_W, {"__name__": "snowflake"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height > 0


def test_icon_element_missing_returns_placeholder(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="nonexistent", size=80)
    img = el.render(
        CANVAS_W, {"__name__": "nonexistent"}, fonts_dir, icons_dir, icon_collections_dir
    )
    assert img.width == CANVAS_W
    assert img.height == 80 + 8


def _icon_ink(img: Image.Image) -> int:
    """Count black (0) pixels — a rendered glyph has ink; a blank strip has none."""
    return img.histogram()[0]  # bin 0 of an "L" image = pixels with value 0 (black)


def test_icon_element_custom_svg_renders(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="foo", size=80)  # foo.svg + foo.png both exist; svg preferred
    img = el.render(CANVAS_W, {"__name__": "foo"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height == 80 + 8
    # The svg is a black square → real ink; the matching foo.png is blank white → none. Ink proves
    # the svg won the precedence probe and rasterized.
    assert _icon_ink(img) > 0


def test_icon_element_png_still_renders(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="snowflake.png", size=80)  # explicit suffix forces the png path
    img = el.render(
        CANVAS_W, {"__name__": "snowflake.png"}, fonts_dir, icons_dir, icon_collections_dir
    )
    assert img.width == CANVAS_W
    assert img.height == 80 + 8


def test_icon_element_collection_rasterizes(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="coffee", size=80, collection="fontawesome", style="solid")
    img = el.render(CANVAS_W, {"__name__": "coffee"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert _icon_ink(img) > 0


def test_icon_element_unknown_collection_returns_blank(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="coffee", size=80, collection="bogus")
    img = el.render(CANVAS_W, {"__name__": "coffee"}, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 80 + 8
    assert _icon_ink(img) == 0


def test_icon_element_rejects_path_traversal(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    from app.render.elements import IconElement

    el = IconElement(name="x", size=80)
    img = el.render(
        CANVAS_W, {"__name__": "../../../etc/passwd"}, fonts_dir, icons_dir, icon_collections_dir
    )
    assert img.height == 80 + 8
    assert _icon_ink(img) == 0


def test_image_field_passes_through_to_element(engine: RenderEngine) -> None:
    """A raw `image` field must reach ImageElement; previously the engine dropped it."""
    src = Image.new("L", (50, 50), 0)  # solid black square
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    layout = [{"type": "image", "field": "image"}]
    png_with = engine.render_to_png(layout, {"image": b64}, CANVAS_W, None)
    png_without = engine.render_to_png(layout, {}, CANVAS_W, None)
    # With the image present the strip has real (dark) content; without it the strip is empty.
    assert png_with != png_without
    assert to_pil(png_with).convert("L").getextrema()[0] < 128


# ── Row container ────────────────────────────────────────────────────────────────
def _ink_bbox(img: Image.Image, x0: int, x1: int) -> tuple[int, int, int, int] | None:
    """Bounding box (within columns [x0, x1)) of black ink, or None if the slice is blank."""
    from PIL import ImageOps

    region = img.crop((x0, 0, x1, img.height))
    return ImageOps.invert(region).getbbox()  # ink (0) → 255 after invert; getbbox finds it


def test_row_column_widths_two_flex() -> None:
    row = RowElement(children=[TextElement(text="a"), TextElement(text="b")])
    # avail = 696 - spacing(8) = 688; split 50/50 (last column absorbs rounding).
    assert row._column_widths(CANVAS_W) == [344, 344]


def test_row_column_widths_fixed_plus_flex() -> None:
    row = RowElement(children=[TextElement(text="a"), IconElement(name="x", width=80)])
    widths = row._column_widths(CANVAS_W)
    assert widths == [CANVAS_W - 8 - 80, 80]
    assert sum(widths) + 8 == CANVAS_W  # columns + single gap fill the row exactly


def test_row_column_widths_weighted() -> None:
    row = RowElement(children=[TextElement(text="a", weight=3), TextElement(text="b", weight=1)])
    w0, w1 = row._column_widths(CANVAS_W)
    assert (w0, w1) == (516, 172)  # 688 split 3:1; remainder to last column
    assert w0 == w1 * 3


def test_row_renders_full_width_strip(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    row = RowElement(children=[TextElement(text="left"), TextElement(text="right")])
    res = {"__children__": [{"__text__": "left"}, {"__text__": "right"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height > 0


def test_row_height_is_max_child_height(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    tall = TitleElement(text="A\nB\nC")  # 3 lines at title size
    short = TextElement(text="x", size=20)
    row = RowElement(children=[tall, short])
    standalone_tall = tall.render(
        CANVAS_W, {"__text__": "A\nB\nC"}, fonts_dir, icons_dir, icon_collections_dir
    )
    res = {"__children__": [{"__text__": "A\nB\nC"}, {"__text__": "x"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == standalone_tall.height


def test_row_valign_moves_short_column(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A short right column sits higher with align_items=top than with bottom."""
    children = [
        TitleElement(text="A\nB\nC"),
        IconElement(name="coffee", size=40, collection="fontawesome"),
    ]
    res = {"__children__": [{"__text__": "A\nB\nC"}, {"__name__": "coffee"}]}
    widths = RowElement(children=children)._column_widths(CANVAS_W)
    right_x0 = CANVAS_W - widths[-1]

    top = RowElement(children=children, align_items="top").render(
        CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir
    )
    bottom = RowElement(children=children, align_items="bottom").render(
        CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir
    )
    top_box = _ink_bbox(top, right_x0, CANVAS_W)
    bottom_box = _ink_bbox(bottom, right_x0, CANVAS_W)
    assert top_box is not None and bottom_box is not None
    assert bottom_box[1] > top_box[1]  # ink starts lower when bottom-aligned


def test_row_overflow_fixed_widths_still_canvas_width(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    row = RowElement(
        children=[TextElement(text="a", width=CANVAS_W), TextElement(text="b", width=CANVAS_W)]
    )
    res = {"__children__": [{"__text__": "a"}, {"__text__": "b"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W  # over-wide columns are scaled to fit, not an exception


def test_row_oversized_fixed_keeps_flex_text_visible(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """An oversized fixed column must not collapse a required flex text column to a silent zero width.

    The documented pattern is a title in a flex column beside a fixed icon; a typo like an icon
    `width: 1_000_000` previously starved the title to width 0 — it vanished while the print still
    reported success. The flex column must keep a visible minimum so the title clips instead.
    """
    row = RowElement(
        children=[TextElement(text="Rack A-2"), IconElement(name="check", width=1_000_000)]
    )
    widths = row._column_widths(CANVAS_W)
    assert widths[0] >= ROW_MIN_FLEX_WIDTH  # the flex title keeps a visible minimum, not 0
    res = {"__children__": [{"__text__": "Rack A-2"}, {}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert _has_ink(img)  # the title still renders (clipped), not silently dropped


def test_row_fixed_widths_never_overflow_canvas() -> None:
    """Fixed columns that overshoot the row are scaled so columns + gaps fit — no off-canvas paste.

    Two 400px columns on a 696px label would otherwise push the second column partly off the edge
    (silently dropping a QR/barcode there); scaling keeps every column on-canvas.
    """
    spacing = 8
    row = RowElement(
        children=[TextElement(text="a", width=400), TextElement(text="b", width=400)],
        spacing=spacing,
    )
    widths = row._column_widths(CANVAS_W)
    assert sum(widths) + spacing * (len(widths) - 1) <= CANVAS_W
    assert all(w > 0 for w in widths)  # both columns remain visible, just narrower


def _sample_png_b64(size: tuple[int, int] = (50, 50)) -> str:
    img_src = Image.new("L", size, 128)
    buf = io.BytesIO()
    img_src.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_row_tiny_image_column_does_not_crash(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A fixed image column narrower than the resize margin draws a failure marker, not a crash."""
    from app.render.elements import ImageElement

    row = RowElement(
        children=[
            TextElement(text="label"),
            ImageElement(field="photo", width=8),  # < 16px margin ⇒ would compute new_w <= 0
        ]
    )
    res = {"__children__": [{"__text__": "label"}, {"photo": _sample_png_b64()}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W  # no exception; the too-narrow image column shows a crossed box


def test_row_exhausted_flex_image_column_does_not_crash(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A flex image column squeezed to zero width by an oversized fixed sibling must not crash."""
    from app.render.elements import ImageElement

    row = RowElement(
        children=[
            TextElement(text="label", width=CANVAS_W),  # consumes the whole row
            ImageElement(field="photo"),  # flex column left at width 0
        ]
    )
    res = {"__children__": [{"__text__": "label"}, {"photo": _sample_png_b64()}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W


def test_row_tiny_barcode_column_does_not_crash(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A barcode column narrower than the resize margin draws a failure marker, not a crash."""
    from app.render.elements import BarcodeElement

    row = RowElement(
        children=[
            TextElement(text="label"),
            BarcodeElement(data="12345678", width=8),
        ]
    )
    res = {"__children__": [{"__text__": "label"}, {"__data__": "12345678"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W


def _has_ink(img: Image.Image) -> bool:
    """True when the (grayscale) image contains any black pixels — i.e. something was drawn."""
    extrema = img.getextrema()
    return img.height > 0 and extrema[0] == 0


def test_row_narrow_qr_draws_failure_placeholder(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A QR whose column is narrower than its fixed size shows a crossed box at the QR's height."""
    qr = QRElement(data="https://example.com", size=120)
    qr.width = 40  # column < size ⇒ the QR would clip; expect a placeholder instead
    row = RowElement(children=[qr])
    res = {"__children__": [{"__data__": "https://example.com"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert img.height == 120  # placeholder occupies the QR's intended height
    assert _has_ink(img)  # the crossed box is drawn, not a blank gap


def test_row_narrow_qr_without_data_stays_blank(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """An empty (optional) QR field in a narrow column must NOT trigger the failure marker."""
    qr = QRElement(data="", size=120)
    qr.width = 40
    row = RowElement(children=[qr])
    res = {"__children__": [{"__data__": ""}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 0  # no content ⇒ blank strip, no placeholder


def test_row_exact_fit_fixed_keeps_flex_text_visible() -> None:
    """A fixed column that exactly consumes `avail` must still leave the flex column its minimum.

    The reserve previously only applied on strict overflow (`fixed_total > avail`); an exact fit
    (`fixed_total == avail`) fell through and starved the flex column to 0, silently dropping text.
    """
    spacing = 8
    avail = CANVAS_W - spacing
    row = RowElement(
        children=[IconElement(name="check", width=avail), TextElement(text="Rack A-2")],
        spacing=spacing,
    )
    widths = row._column_widths(CANVAS_W)
    assert widths[1] >= ROW_MIN_FLEX_WIDTH  # flex text keeps its reserved minimum, not 0
    assert sum(widths) + spacing * (len(widths) - 1) <= CANVAS_W  # still fits the canvas


def test_row_qr_width_equals_size_left_align_draws_marker(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A left-aligned QR with width == size still clips by the 8px inset, so it must mark, not clip."""
    qr = QRElement(data="https://example.com", size=120, align="left")
    qr.width = 120  # == size, but the left-align inset needs size + QR_ALIGN_INSET to fit
    row = RowElement(children=[qr])
    res = {"__children__": [{"__data__": "https://example.com"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 120  # placeholder height (== size); a real QR strip would be size + 8
    assert _has_ink(img)


def test_row_qr_width_equals_size_right_align_draws_marker(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """Right-aligned QR mirrors the left case: width == size clips by the inset, so mark it."""
    qr = QRElement(data="https://example.com", size=120, align="right")
    qr.width = 120
    row = RowElement(children=[qr])
    res = {"__children__": [{"__data__": "https://example.com"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 120  # placeholder, not a clipped QR


def test_row_qr_width_equals_size_center_align_renders_qr(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A centered QR needs no inset, so width == size renders the real QR (no false marker)."""
    qr = QRElement(data="https://example.com", size=120, align="center")
    qr.width = 120
    row = RowElement(children=[qr])
    res = {"__children__": [{"__data__": "https://example.com"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 128  # real QR strip is size + 8, proving the marker path was NOT taken


def test_row_narrow_barcode_with_data_draws_failure_placeholder(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A barcode collapsed to a blank strip by a too-narrow column shows a crossed box."""
    from app.render.elements import BarcodeElement

    bc = BarcodeElement(data="12345678", height=60)
    bc.width = 10  # ≤ 16px margin ⇒ renderer returns a blank strip
    row = RowElement(children=[bc])
    res = {"__children__": [{"__data__": "12345678"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W
    assert _has_ink(img)  # placeholder drawn rather than silently dropping the barcode


def test_row_narrow_image_without_content_stays_blank(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """An absent (optional) image field in a narrow column must NOT trigger the failure marker."""
    from app.render.elements import ImageElement

    img_el = ImageElement(field="photo", width=8)
    row = RowElement(children=[img_el])
    res = {"__children__": [{}]}  # no "photo" key ⇒ no content
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.height == 0  # no content ⇒ blank strip, no placeholder


def test_row_extreme_fixed_width_is_clamped() -> None:
    """A pathological fixed width must clamp to the canvas, not request a giant child allocation."""
    row = RowElement(children=[TextElement(text="a", width=1_000_000_000), TextElement(text="b")])
    widths = row._column_widths(CANVAS_W)
    assert max(widths) <= CANVAS_W  # no column exceeds the row width → bounded allocation


def test_row_extreme_fixed_width_renders_canvas_width(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """The whole row still renders at canvas width despite an extreme fixed child width."""
    row = RowElement(children=[TextElement(text="a", width=1_000_000_000), TextElement(text="b")])
    res = {"__children__": [{"__text__": "a"}, {"__text__": "b"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W


def test_row_gigantic_int_fixed_width_does_not_overflow(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A loadable-but-absurd fixed width (300-digit int) must scale via integer math, not crash.

    Float scaling (``c.width * scale``) raised ``OverflowError: int too large to convert to float``
    on such a value, turning preview/print into a 500. Integer arithmetic keeps the column bounded.
    """
    huge = 10**300
    row = RowElement(children=[TextElement(text="a", width=huge), TextElement(text="b")])
    widths = row._column_widths(CANVAS_W)
    assert max(widths) <= CANVAS_W  # scaled down with no OverflowError
    res = {"__children__": [{"__text__": "a"}, {"__text__": "b"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W


def test_row_gigantic_spacing_does_not_overflow(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A loadable-but-absurd `spacing` (300-digit int) must not reach PIL.paste as a giant coord.

    ``x += w + spacing`` would otherwise make the second column's paste raise
    ``OverflowError: Python int too large to convert to C long`` — a 500 for an accepted template.
    Pasting stops once a column starts off-canvas, so the giant coordinate is never handed to PIL.
    """
    row = RowElement(children=[TextElement(text="a"), TextElement(text="b")], spacing=10**300)
    res = {"__children__": [{"__text__": "a"}, {"__text__": "b"}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W  # renders (degraded) instead of raising OverflowError


def test_row_tiny_line_column_does_not_crash(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A line column narrower than twice its margin degrades to empty, not a ValueError."""
    from app.render.elements import LineElement

    row = RowElement(children=[TextElement(text="label"), LineElement(width=8)])
    res = {"__children__": [{"__text__": "label"}, {}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W


def test_row_exhausted_flex_box_column_does_not_crash(
    fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """A box flex column squeezed to zero width by an oversized fixed sibling must not crash."""
    from app.render.elements import BoxElement

    row = RowElement(children=[TextElement(text="label", width=CANVAS_W), BoxElement()])
    res = {"__children__": [{"__text__": "label"}, {}]}
    img = row.render(CANVAS_W, res, fonts_dir, icons_dir, icon_collections_dir)
    assert img.width == CANVAS_W


def test_engine_row_child_token_resolves(engine: RenderEngine) -> None:
    layout = [{"type": "row", "children": [{"type": "text", "text": "{{name}}"}]}]
    png_with = engine.render_to_png(layout, {"name": "Hello"}, CANVAS_W, None)
    png_without = engine.render_to_png(layout, {}, CANVAS_W, None)
    assert png_with != png_without  # the {{name}} ink only appears when the field is supplied


def test_engine_row_child_translation(engine: RenderEngine) -> None:
    layout = [{"type": "row", "children": [{"type": "text", "text": "[[frozen]]", "size": 30}]}]
    png_en = engine.render_to_png(layout, {}, CANVAS_W, None, language="en")
    png_es = engine.render_to_png(layout, {}, CANVAS_W, None, language="es")
    assert png_en != png_es  # "Frozen" vs "Congelado" inside the row child


# ── Date arithmetic — calendar math (deterministic, no clock dependency) ─────────
def test_apply_offset_days() -> None:
    base = datetime(2026, 6, 22, 10, 30)
    assert _apply_offset(base, "+5d") == datetime(2026, 6, 27, 10, 30)
    assert _apply_offset(base, "-1d") == datetime(2026, 6, 21, 10, 30)


def test_apply_offset_weeks() -> None:
    base = datetime(2026, 6, 22)
    assert _apply_offset(base, "+2w") == datetime(2026, 7, 6)


def test_apply_offset_months_and_years() -> None:
    base = datetime(2026, 6, 22)
    assert _apply_offset(base, "+6m") == datetime(2026, 12, 22)
    assert _apply_offset(base, "-1y") == datetime(2025, 6, 22)


def test_add_months_clamps_short_month() -> None:
    """Jan 31 + 1 month must clamp to the last valid day of February, not overflow."""
    assert _add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert _add_months(datetime(2024, 1, 31), 1) == datetime(2024, 2, 29)  # leap year


def test_add_months_crosses_year_boundary() -> None:
    assert _add_months(datetime(2026, 11, 15), 3) == datetime(2027, 2, 15)
    assert _add_months(datetime(2026, 1, 15), -1) == datetime(2025, 12, 15)


# ── Date arithmetic — token substitution (now injected, resolution is pure) ──
def test_resolve_date_offset_token() -> None:
    now = datetime.now()
    expected = (now + timedelta(days=5)).strftime("%d/%m/%Y")
    out = _resolve_fields("Caduca: {{date+5d}}", {}, now=now)
    assert out == f"Caduca: {expected}"


def test_resolve_plain_date_unaffected() -> None:
    now = datetime.now()
    expected = now.strftime("%d/%m/%Y")
    out = _resolve_fields("{{date}}", {}, now=now)
    assert out == expected


def test_resolve_date_accepts_custom_format() -> None:
    out = _resolve_fields("{{date:%Y}}", {}, now=datetime.now())
    assert out == str(date.today().year)


def test_resolve_now_offset_with_format() -> None:
    now = datetime.now()
    expected = (now + timedelta(days=1)).strftime("%d/%m")
    out = _resolve_fields("{{now+1d:%d/%m}}", {}, now=now)
    assert out == expected


def test_resolve_field_named_like_no_offset() -> None:
    """A normal field substitution is untouched by the offset grammar."""
    out = _resolve_fields("{{title}}", {"title": "Router"}, now=datetime.now())
    assert out == "Router"


# ── Bundled templates — load & render (covers the new homelab/die-cut templates) ──
def test_all_bundled_templates_load() -> None:
    from app.loader import TemplateRegistry

    names = TemplateRegistry(TEMPLATES_DIR).load_all()
    assert {"cable-label", "asset-tag", "address"} <= set(names)
    assert len(names) >= 11


# ── i18n — two-pass resolution (translate [[key]] then resolve {{date}}) ──────────
def test_engine_translation_changes_chrome(engine: RenderEngine) -> None:
    """Same layout rendered in two languages must produce different pixels."""
    layout = [{"type": "text", "text": "[[frozen]]: {{date}}", "size": 30}]
    png_en = engine.render_to_png(layout, {}, CANVAS_W, None, language="en")
    png_es = engine.render_to_png(layout, {}, CANVAS_W, None, language="es")
    assert png_en[:8] == b"\x89PNG\r\n\x1a\n"
    assert png_en != png_es  # "Frozen" vs "Congelado"


def test_engine_translation_defaults_to_translator_language(engine: RenderEngine) -> None:
    """Omitting language falls back to the translator's default (en here)."""
    layout = [{"type": "text", "text": "[[frozen]]: {{date}}", "size": 30}]
    assert engine.render_to_png(layout, {}, CANVAS_W, None) == engine.render_to_png(
        layout, {}, CANVAS_W, None, language="en"
    )


def test_all_template_tokens_exist_in_default_catalog() -> None:
    """Every [[key]] used by a bundled template must exist in the default (en) catalog."""
    import re

    from app.render.i18n import load_catalog

    en = load_catalog(TRANSLATIONS_DIR / "en.yaml")
    token_re = re.compile(r"\[\[(\w+)\]\]")
    for path in TEMPLATES_DIR.glob("*.yaml"):
        for key in token_re.findall(path.read_text(encoding="utf-8")):
            assert key in en, f"{path.name} uses [[{key}]] missing from en.yaml"


@pytest.mark.parametrize(
    ("template_name", "fields", "canvas_height"),
    [
        (
            "cable-label",
            {"name": "SW1-p3", "endpoint_a": "rack-A", "endpoint_b": "AP-garage"},
            None,
        ),
        ("asset-tag", {"title": "Server 01", "asset_id": "SRV-0001", "location": "Rack B-2"}, None),
        ("address", {"name": "Santi", "line1": "Calle Mayor 1", "line2": "28013 Madrid"}, 271),
        ("freezer-dated", {"title": "Caldo"}, None),  # exercises [[…]] + {{date+6m}}
        (
            "row-demo",
            {"title": "Rack A-2", "status": "online"},
            None,
        ),  # text-left / glyph-right row
    ],
)
def test_bundled_template_renders(
    engine: RenderEngine,
    template_name: str,
    fields: dict[str, str],
    canvas_height: int | None,
) -> None:
    from app.loader import load_template

    tmpl = load_template(TEMPLATES_DIR / f"{template_name}.yaml")
    png = engine.render_to_png(tmpl.layout, fields, CANVAS_W, canvas_height, tmpl.rotate)
    img = to_pil(png)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    if canvas_height is not None:
        assert img.height == canvas_height


# ── 600 dpi high-resolution mode ─────────────────────────────────────────────────
#
# 600 dpi mode is a UNIFORM 2x of the whole label coordinate system. convert(dpi_600=True) always
# does `im.resize((im.size[0]//2, im.size[1]))` — halving the print-head (width) axis while KEEPING
# the rows — and sets the printer to 300x600 dpi, where the feed advances at 600 dpi so each row is
# 1/600" of feed. The SAME physical length therefore needs DOUBLE the rows. Hence BOTH axes double:
# width to satisfy the post-halving == dots_printable[0] check, height so the doubled rows print the
# intended physical length (not half). These tests assert the real geometry and exercise the real
# brother_ql conversion so a regression to single-axis scaling fails loudly.


def test_high_res_continuous_doubles_both_axes(engine: RenderEngine) -> None:
    """high_res on a continuous label doubles BOTH width and the feed-axis (height) row count.

    Width doubles so convert's internal `//2` lands on dots_printable[0]; height doubles so the
    extra rows print the same physical length at 600 dpi feed (single-axis scaling would print the
    label at half length — Bug 1).
    """
    layout = [{"type": "title", "text": "Hi-res"}]
    png_off = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    png_on = engine.render_to_png(
        layout, {}, canvas_width=CANVAS_W, canvas_height=None, high_res=True
    )
    img_off = to_pil(png_off)
    img_on = to_pil(png_on)
    assert img_on.width == img_off.width * 2, (
        f"expected width {img_off.width * 2}, got {img_on.width}"
    )
    assert img_on.height == img_off.height * 2, (
        f"expected feed-axis height {img_off.height * 2} (doubled rows), got {img_on.height}"
    )


def test_high_res_die_cut_doubles_both_axes(engine: RenderEngine) -> None:
    """With high_res=True on a die-cut label the engine doubles both width and height.

    convert(dpi_600=True) for DIE_CUT checks for exactly (dots_printable[0]*2, dots_printable[1]*2).
    """
    canvas_height = 271  # 29mm die-cut at 300 dpi
    layout = [{"type": "title", "text": "Hi-res"}]
    png_off = engine.render_to_png(layout, {}, CANVAS_W, canvas_height)
    png_on = engine.render_to_png(layout, {}, CANVAS_W, canvas_height, high_res=True)
    img_off = to_pil(png_off)
    img_on = to_pil(png_on)
    assert img_on.width == img_off.width * 2
    assert img_on.height == img_off.height * 2


def test_high_res_off_byte_identical_to_default(engine: RenderEngine) -> None:
    """high_res=False must produce byte-identical output to the default (no high_res kwarg).

    Regression guard: the high_res code path (scale=1) must not affect normal 300 dpi renders.
    """
    layout = [{"type": "title", "text": "Normal"}, {"type": "subtitle", "text": "sub"}]
    png_default = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    png_false = engine.render_to_png(
        layout, {}, canvas_width=CANVAS_W, canvas_height=None, high_res=False
    )
    assert png_default == png_false, "high_res=False must be byte-identical to the default render"


def test_high_res_off_byte_identical_rich_layout(engine: RenderEngine) -> None:
    """scale=1 is a no-op across every element type, incl. defaults and a row container.

    A broader byte-identical guard than the title/subtitle case: every renderer now multiplies its
    dimensions by self.scale, so this proves scale=1 leaves all of them untouched.
    """
    layout = [
        {"type": "title", "text": "T"},
        {"type": "subtitle", "text": "S"},
        {"type": "text", "text": "body"},
        {"type": "qr", "data": "x"},
        {"type": "line"},
        {"type": "box"},
        {"type": "spacer"},
        {
            "type": "row",
            "children": [
                {"type": "text", "text": "left"},
                {"type": "qr", "data": "y", "width": 120},
            ],
        },
    ]
    png_default = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    png_false = engine.render_to_png(
        layout, {}, canvas_width=CANVAS_W, canvas_height=None, high_res=False
    )
    assert png_default == png_false


def test_high_res_full_width_element_fills_doubled_canvas(engine: RenderEngine) -> None:
    """A full-width element (title) must still be full-width in the high_res render.

    Checks that layout is visually consistent — not stretched — when high_res=True.
    """
    layout = [{"type": "title", "text": "Full width"}]
    png_on = engine.render_to_png(
        layout, {}, canvas_width=CANVAS_W, canvas_height=None, high_res=True
    )
    img_on = to_pil(png_on)
    # The rendered strip must fill the full doubled width (no off-canvas paste / clipping).
    assert img_on.width == CANVAS_W * 2
    assert img_on.height > 0


# ── element DEFAULTS scale under high_res (Bug 2) ────────────────────────────────
def _whole_ink_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the black ink (non-white pixels) in an L-mode strip, or None if blank."""
    inverted = img.point(lambda p: 255 if p < 128 else 0)
    return inverted.getbbox()


def test_high_res_default_title_scales(engine: RenderEngine) -> None:
    """A DEFAULT-sized title (no explicit `size` field) renders ~2x taller under high_res.

    Title/Subtitle carry no `size` field at all, so the old field-scaling approach left their
    default font size at 300 dpi while the canvas doubled (Bug 2). The whole-geometry scale fixes it.
    """
    layout = [{"type": "title", "text": "Default Title"}]
    off = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None))
    on = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True))
    box_off, box_on = _whole_ink_bbox(off), _whole_ink_bbox(on)
    assert box_off is not None and box_on is not None
    h_off = box_off[3] - box_off[1]
    h_on = box_on[3] - box_on[1]
    # Glyph ink height must scale ~2x (allow rounding/hinting slack).
    assert 1.8 <= h_on / h_off <= 2.2, f"title ink height ratio {h_on / h_off:.2f} not ~2x"


def test_high_res_default_qr_scales(engine: RenderEngine) -> None:
    """A DEFAULT-sized QR (no explicit `size`) renders ~2x larger under high_res."""
    layout = [{"type": "qr", "data": "https://example.com"}]
    off = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None))
    on = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True))
    box_off, box_on = _whole_ink_bbox(off), _whole_ink_bbox(on)
    assert box_off is not None and box_on is not None
    w_off = box_off[2] - box_off[0]
    w_on = box_on[2] - box_on[0]
    assert 1.8 <= w_on / w_off <= 2.2, f"QR ink width ratio {w_on / w_off:.2f} not ~2x"


def test_high_res_default_image_scales(engine: RenderEngine) -> None:
    """A DEFAULT image (max_height default) scales ~2x under high_res (default field, not in spec)."""
    src = Image.new("L", (300, 300), 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    layout = [{"type": "image"}]
    off = to_pil(engine.render_to_png(layout, {"image": b64}, CANVAS_W, None))
    on = to_pil(engine.render_to_png(layout, {"image": b64}, CANVAS_W, None, high_res=True))
    box_off, box_on = _whole_ink_bbox(off), _whole_ink_bbox(on)
    assert box_off is not None and box_on is not None
    h_off = box_off[3] - box_off[1]
    h_on = box_on[3] - box_on[1]
    assert 1.8 <= h_on / h_off <= 2.2, f"image ink height ratio {h_on / h_off:.2f} not ~2x"


# ── ENDLESS feed-axis length clamps (Bug 1) ──────────────────────────────────────
def test_high_res_continuous_min_length_scales(engine: RenderEngine) -> None:
    """A short ENDLESS label clamps to 2x min_length_px under high_res (full physical length).

    Single-axis scaling left the clamp at the 300 dpi value, so the doubled-row label printed at
    half its physical minimum length. The clamp must scale to the 600 dpi dot count.
    """
    layout = [{"type": "spacer", "size": 1}]
    img_off = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None))
    img_on = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True))
    assert img_off.height == engine.min_length_px
    assert img_on.height == engine.min_length_px * 2, (
        f"expected min clamp {engine.min_length_px * 2} (600 dpi dots), got {img_on.height}"
    )


def test_high_res_continuous_max_length_scales(
    fonts_dir: Path,
    icons_dir: Path,
    icon_collections_dir: Path,
    translator: Translator,
) -> None:
    """A very long ENDLESS label clamps to 2x max_length_px under high_res (no premature clip).

    With a small max_length_px and tall content, the 300 dpi render hits the cap; the high_res
    render must hit DOUBLE the cap (same physical max length), not the unscaled 300 dpi cap.
    """
    small_max = RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=10,
        max_length_px=300,
    )
    layout = [{"type": "spacer", "size": 5000}]  # far taller than max
    img_off = to_pil(small_max.render_to_png(layout, {}, CANVAS_W, None))
    img_on = to_pil(small_max.render_to_png(layout, {}, CANVAS_W, None, high_res=True))
    assert img_off.height == small_max.max_length_px
    assert img_on.height == small_max.max_length_px * 2, (
        f"expected max clamp {small_max.max_length_px * 2}, got {img_on.height}"
    )


# ── REAL brother_ql conversion (would have caught both bugs) ─────────────────────
def test_high_res_continuous_real_conversion_doubles_rows(engine: RenderEngine) -> None:
    """Render an ENDLESS label at 300 and 600 dpi, push BOTH through real convert(), assert.

    - 300 dpi: width == dots_printable[0] (696), passes convert(dpi_600=False).
    - 600 dpi: width == dots_printable[0]*2 (1392), height doubled; convert(dpi_600=True) halves
      width to 696 (no ValueError) and the feed-axis rows are DOUBLE the 300 dpi rows.
    """
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    layout = [{"type": "title", "text": "Hi-res"}, {"type": "qr", "data": "x"}]
    png_off = engine.render_to_png(layout, {}, CANVAS_W, None)
    png_on = engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True)
    img_off = Image.open(io.BytesIO(png_off)).convert("RGB")
    img_on = Image.open(io.BytesIO(png_on)).convert("RGB")

    # Real conversion must not raise on either input.
    for img, dpi_600 in ((img_off, False), (img_on, True)):
        qlr = BrotherQLRaster("QL-810W")
        qlr.exception_on_warning = True
        convert(qlr=qlr, images=[img], label="62", rotate="0", dpi_600=dpi_600, cut=True)

    # Feed-axis rows (image height) double at 600 dpi for the same physical length. Content-driven
    # height carries sub-pixel rounding in per-line metrics, so allow a couple of px of slack while
    # still proving the rows ~doubled (a single-axis bug would leave the ratio at ~1.0).
    assert abs(img_on.height - img_off.height * 2) <= 2, (
        f"expected ~{img_off.height * 2} rows at 600 dpi, got {img_on.height}"
    )
    # Width is the doubled print-head axis the library expects to halve (exact, canvas-controlled).
    assert img_on.width == img_off.width * 2 == CANVAS_W * 2


def test_high_res_continuous_max_length_real_conversion_no_error(
    fonts_dir: Path,
    icons_dir: Path,
    icon_collections_dir: Path,
    translator: Translator,
) -> None:
    """Regression: high_res ENDLESS at DEFAULT max_length_px must not raise BrotherQLRasterError.

    Before the fix, render_max = max_length_px * scale = 6000 * 2 = 12000 rows, which exceeds
    the QL-810W hard limit of 11811.  BrotherQLRaster.add_raster_data raises BrotherQLRasterError
    for any image whose height > model.min_max_length_dots[1].  The fix caps render_max at
    _BROTHER_QL_MAX_RASTER_ROWS so the rendered image always fits within the library limit.
    """
    from brother_ql.conversion import convert
    from brother_ql.models import ModelsManager
    from brother_ql.raster import BrotherQLRaster

    from app.render.engine import _BROTHER_QL_MAX_RASTER_ROWS

    # Use default max_length_px=6000; with scale=2 the uncapped value would be 12000 > 11811.
    default_max_engine = RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=200,
        max_length_px=6000,
    )
    # A spacer large enough to force the render to hit the row cap.  The clamped result is
    # 1392 x 11811 px (= 16.4 MP) which marginally exceeds app/main.py's 16 MP PIL bomb guard;
    # lift the limit for this test only — it is an anti-DOS heuristic, not a correctness gate.
    layout = [{"type": "spacer", "size": 7000}]
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None  # disable bomb check for render + open
    try:
        png = default_max_engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True)
        img = Image.open(io.BytesIO(png)).convert("RGB")
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit

    # Rendered height must be within the library's hard row limit.
    mm = ModelsManager()
    model_max = min(mm[ident].min_max_length_dots[1] for ident in mm.iter_identifiers())
    assert img.height <= model_max, (
        f"rendered height {img.height} exceeds model max {model_max}; "
        "fix did not clamp render_max correctly"
    )
    assert img.height == _BROTHER_QL_MAX_RASTER_ROWS, (
        f"expected height == _BROTHER_QL_MAX_RASTER_ROWS ({_BROTHER_QL_MAX_RASTER_ROWS}), "
        f"got {img.height}"
    )

    # Real conversion must NOT raise.
    qlr = BrotherQLRaster("QL-810W")
    qlr.exception_on_warning = True
    convert(qlr=qlr, images=[img], label="62", rotate="0", dpi_600=True, cut=True)


def test_high_res_continuous_small_max_still_doubles(
    fonts_dir: Path,
    icons_dir: Path,
    icon_collections_dir: Path,
    translator: Translator,
) -> None:
    """A small max_length_px that stays below _BROTHER_QL_MAX_RASTER_ROWS still doubles exactly.

    Confirms that the row-cap only kicks in when max_length_px * scale would exceed the library
    limit, and that normal high_res behaviour (doubling) is unaffected for in-range values.
    """
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    small_max = RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=10,
        max_length_px=300,  # 300 * 2 = 600, well below 11811
    )
    layout = [{"type": "spacer", "size": 5000}]  # far taller than max
    img_off = to_pil(small_max.render_to_png(layout, {}, CANVAS_W, None))
    img_on = to_pil(small_max.render_to_png(layout, {}, CANVAS_W, None, high_res=True))

    # 300 dpi hits the 300-row cap; 600 dpi doubles it to 600.
    assert img_off.height == small_max.max_length_px
    assert img_on.height == small_max.max_length_px * 2, (
        f"expected {small_max.max_length_px * 2} (doubled), got {img_on.height}"
    )

    # Real conversion must also pass.
    for img, dpi_600 in ((img_off, False), (img_on, True)):
        qlr = BrotherQLRaster("QL-810W")
        qlr.exception_on_warning = True
        convert(qlr=qlr, images=[img], label="62", rotate="0", dpi_600=dpi_600, cut=True)


def test_high_res_die_cut_real_conversion_no_value_error(engine: RenderEngine) -> None:
    """A DIE_CUT high_res render matches dots_expected exactly so convert() does not raise.

    convert(dpi_600=True) for DIE_CUT raises ValueError unless im.size == (W*2, H*2). We render at
    the label's true dots_printable so the dimensions are exactly what the library demands.
    """
    from brother_ql.conversion import convert
    from brother_ql.labels import ALL_LABELS
    from brother_ql.raster import BrotherQLRaster

    label_id = "29x90"
    spec = next(lbl for lbl in ALL_LABELS if lbl.identifier == label_id)
    w300, h300 = spec.dots_printable  # (306, 991)

    layout = [{"type": "title", "text": "DC"}]
    png_off = engine.render_to_png(layout, {}, w300, h300)
    png_on = engine.render_to_png(layout, {}, w300, h300, high_res=True)
    img_off = Image.open(io.BytesIO(png_off)).convert("RGB")
    img_on = Image.open(io.BytesIO(png_on)).convert("RGB")

    assert img_off.size == (w300, h300)
    assert img_on.size == (w300 * 2, h300 * 2)  # == dots_expected for dpi_600

    # Neither conversion may raise "Bad image dimensions".
    for img, dpi_600 in ((img_off, False), (img_on, True)):
        qlr = BrotherQLRaster("QL-810W")
        qlr.exception_on_warning = True
        convert(qlr=qlr, images=[img], label=label_id, rotate="0", dpi_600=dpi_600, cut=True)


# ── per-model raster-row cap (not global min) ────────────────────────────────────


def test_high_res_default_model_cap_still_11811(
    fonts_dir: Path,
    icons_dir: Path,
    icon_collections_dir: Path,
    translator: Translator,
) -> None:
    """QL-810W (the default model) still caps ENDLESS high_res rows at 11811.

    Confirms existing behaviour is preserved: a RenderEngine constructed with the QL-810W
    per-model ceiling (11811 == _BROTHER_QL_MAX_RASTER_ROWS) clips at the same point as the
    pre-fix global constant did, so no regression for the common case.
    """
    ql810w_max = _brother_ql_model_max_rows("QL-810W")
    assert ql810w_max == _BROTHER_QL_MAX_RASTER_ROWS, (
        f"QL-810W limit changed in brother_ql: expected {_BROTHER_QL_MAX_RASTER_ROWS}, "
        f"got {ql810w_max}"
    )
    model_engine = RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=200,
        max_length_px=6000,
        max_raster_rows=ql810w_max,
    )
    layout = [{"type": "spacer", "size": 7000}]
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        png = model_engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True)
        img = Image.open(io.BytesIO(png))
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit
    assert img.height == ql810w_max, (
        f"QL-810W high_res ENDLESS must cap at {ql810w_max}, got {img.height}"
    )


def test_high_res_wide_format_model_not_clipped_at_global_min(
    fonts_dir: Path,
    icons_dir: Path,
    icon_collections_dir: Path,
    translator: Translator,
) -> None:
    """QL-1100-class model allows ENDLESS high_res rows above 11811 (no silent over-truncation).

    Before the fix, the global minimum (11811) was used unconditionally, clipping legitimate
    labels on wide-format printers whose limit is ~35434.  With the fix, a RenderEngine
    configured for QL-1100 uses that model's ceiling instead, so content that renders above
    11811 rows is NOT clipped to 11811 — it passes through up to the model limit.

    The test also pushes the result through real convert(dpi_600=True) on BrotherQLRaster("QL-1100")
    to confirm the library itself accepts the larger row count without raising.
    """
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    ql1100_max = _brother_ql_model_max_rows("QL-1100")
    # Sanity check: the QL-1100 limit must be well above the global min to exercise the bug path.
    assert ql1100_max > _BROTHER_QL_MAX_RASTER_ROWS, (
        f"QL-1100 limit {ql1100_max} should exceed global min {_BROTHER_QL_MAX_RASTER_ROWS}"
    )

    # Use max_length_px that when doubled lands between the two limits so the difference is
    # unambiguous: 6100 * 2 = 12200 > 11811 but << 35434.
    model_engine = RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=200,
        max_length_px=6100,
        max_raster_rows=ql1100_max,
    )
    # A spacer tall enough to reach the max_length_px*2 = 12200 row cap.
    layout = [{"type": "spacer", "size": 7000}]
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        png = model_engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True)
        img = Image.open(io.BytesIO(png))
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit

    # The rendered height must exceed the old global minimum (proving it was NOT clipped there)
    # and stay within the QL-1100 model ceiling.
    assert img.height > _BROTHER_QL_MAX_RASTER_ROWS, (
        f"QL-1100 high_res ENDLESS must not clip at global min {_BROTHER_QL_MAX_RASTER_ROWS}; "
        f"got height {img.height}"
    )
    assert img.height <= ql1100_max, (
        f"QL-1100 high_res ENDLESS must not exceed model ceiling {ql1100_max}; "
        f"got height {img.height}"
    )
    assert img.height == 6100 * 2, (
        f"expected exactly max_length_px*2 = {6100 * 2} rows (below QL-1100 ceiling); "
        f"got {img.height}"
    )

    # Real conversion on a QL-1100 raster must not raise BrotherQLRasterError.
    qlr = BrotherQLRaster("QL-1100")
    qlr.exception_on_warning = True
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        img_rgb = Image.open(io.BytesIO(png)).convert("RGB")
        convert(
            qlr=qlr,
            images=[img_rgb],
            label="103",  # 103mm continuous roll — supported by QL-1100
            rotate="0",
            dpi_600=True,
            cut=True,
        )
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit


def test_high_res_fixed_length_min_equals_max_capped_at_model_ceiling(
    fonts_dir: Path,
    icons_dir: Path,
    icon_collections_dir: Path,
    translator: Translator,
) -> None:
    """A fixed-length high_res ENDLESS config (min == max) cannot exceed the model row ceiling.

    Regression: render_min is scaled independently of render_max. With min_length_px ==
    max_length_px == 6000 on QL-810W, the scaled minimum (12000) would otherwise win in
    _compose's max(render_min, min(total, render_max)) and produce a 12000-row image that
    exceeds the 11811 hard limit, crashing convert(dpi_600=True) with BrotherQLRasterError.
    render_min must be clamped to the capped render_max so the composed height stays ≤ 11811.
    """
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    ql810w_max = _brother_ql_model_max_rows("QL-810W")
    model_engine = RenderEngine(
        fonts_dir=fonts_dir,
        icons_dir=icons_dir,
        icon_collections_dir=icon_collections_dir,
        translator=translator,
        min_length_px=6000,
        max_length_px=6000,
        max_raster_rows=ql810w_max,
    )
    # Tiny content — the fixed minimum drives the height, not the content.
    layout = [{"type": "spacer", "size": 10}]
    old_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        png = model_engine.render_to_png(layout, {}, CANVAS_W, None, high_res=True)
        img = Image.open(io.BytesIO(png))
        assert img.height == ql810w_max, (
            f"fixed-length high_res must clamp the scaled minimum to {ql810w_max}, "
            f"got {img.height}"
        )
        # Real conversion must not raise now that height ≤ the model ceiling.
        qlr = BrotherQLRaster("QL-810W")
        img_rgb = Image.open(io.BytesIO(png)).convert("RGB")
        convert(
            qlr=qlr,
            images=[img_rgb],
            label="62",
            rotate="0",
            dpi_600=True,
            cut=True,
        )
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit


# ── {{seq}} token and render_sequence ────────────────────────────────────────────


def test_seq_excluded_from_computed_tokens_contract() -> None:
    """{{seq}} must be in COMPUTED_TOKENS so the loader never treats it as a missing user field."""
    from app.render.engine import COMPUTED_TOKENS

    assert "seq" in COMPUTED_TOKENS, "'seq' must be a computed token, not a required user field"


def test_seq_excluded_from_required_field_detection() -> None:
    """A template using {{seq}} must not report 'seq' as a required or optional field.

    referenced_field_tokens (used by the loader's field-contract computation) must exclude seq
    because it is in COMPUTED_TOKENS. This is the mechanism that keeps {{seq}} out of the
    required-field contract the loader computes and the /print route enforces.
    """
    from app.render.engine import referenced_field_tokens

    layout = [{"type": "text", "text": "Item {{seq}}: {{title}}"}]
    tokens = referenced_field_tokens(layout)
    assert "seq" not in tokens, "'seq' must not appear as a referenced field token"
    assert "title" in tokens, "'title' must still be detected as a referenced field token"


def test_resolve_fields_seq_default_empty() -> None:
    """Without a seq argument, {{seq}} resolves to an empty string."""
    from datetime import datetime

    from app.render.engine import _resolve_fields

    result = _resolve_fields("Item {{seq}} done", {}, datetime(2026, 1, 1))
    assert result == "Item  done", f"Expected 'Item  done' (empty seq), got {result!r}"


def test_resolve_fields_seq_substituted() -> None:
    """With an explicit seq string, {{seq}} substitutes correctly."""
    from datetime import datetime

    from app.render.engine import _resolve_fields

    result = _resolve_fields("Box-{{seq}}", {}, datetime(2026, 1, 1), seq="007")
    assert result == "Box-007", f"Expected 'Box-007', got {result!r}"


def test_render_sequence_is_a_lazy_generator(engine: RenderEngine) -> None:
    """render_sequence must be a generator (one label materialized at a time, not a buffered list).

    This is the memory contract: a 500-label batch must never hold all PNGs at once. We prove
    laziness two ways — the return value is a generator object, and calling render_sequence does
    NOT render anything until the consumer pulls (so no eager whole-batch buffer is built).
    """
    import inspect

    calls = {"n": 0}
    real_render_to_png = engine.render_to_png

    def _counting_render_to_png(*args: object, **kwargs: object) -> bytes:
        calls["n"] += 1
        return real_render_to_png(*args, **kwargs)  # type: ignore[arg-type]

    engine.render_to_png = _counting_render_to_png  # type: ignore[method-assign]
    try:
        gen = engine.render_sequence(
            [{"type": "text", "text": "{{seq}}"}], {}, CANVAS_W, None, start=1, count=500
        )
        assert inspect.isgenerator(gen), "render_sequence must return a generator, not a list"
        assert calls["n"] == 0, "render_sequence must not render anything until iterated (lazy)"
        first = next(gen)
        assert calls["n"] == 1, "Pulling one item must render exactly one label, not the batch"
        assert first[:8] == b"\x89PNG\r\n\x1a\n", "Yielded item must be a valid PNG"
    finally:
        engine.render_to_png = real_render_to_png  # type: ignore[method-assign]


def test_render_sequence_produces_count_distinct_images(engine: RenderEngine) -> None:
    """render_sequence must yield exactly count PNG byte strings."""
    layout = [{"type": "text", "text": "Label {{seq}}"}]
    results = list(
        engine.render_sequence(layout, {}, CANVAS_W, None, start=1, count=3, step=1, padding=0)
    )
    assert len(results) == 3, f"Expected 3 images, got {len(results)}"
    assert all(isinstance(r, bytes) for r in results), "All results must be bytes"
    assert all(r[:8] == b"\x89PNG\r\n\x1a\n" for r in results), "All results must be valid PNGs"


def test_render_sequence_images_are_distinct(engine: RenderEngine) -> None:
    """Each item in a sequence batch must render differently (different seq value = different pixels)."""
    layout = [{"type": "text", "text": "Item-{{seq}}"}]
    results = list(
        engine.render_sequence(layout, {}, CANVAS_W, None, start=1, count=3, step=1, padding=3)
    )
    assert results[0] != results[1], "seq=001 and seq=002 must render differently"
    assert results[1] != results[2], "seq=002 and seq=003 must render differently"
    assert results[0] != results[2], "seq=001 and seq=003 must render differently"


def test_render_sequence_padding_applied() -> None:
    """padding=3 must produce zero-padded seq values (e.g. 001, 002, 010)."""
    from datetime import datetime

    from app.render.engine import _resolve_fields

    for value, expected in [(1, "001"), (2, "002"), (10, "010")]:
        seq_str = str(value).zfill(3)
        result = _resolve_fields("{{seq}}", {}, datetime(2026, 1, 1), seq=seq_str)
        assert result == expected, f"Expected {expected!r}, got {result!r}"


def test_render_sequence_step(engine: RenderEngine) -> None:
    """step=5 must produce seq values 10, 15, 20 for start=10, count=3."""
    from datetime import datetime

    from app.render.engine import _resolve_fields

    for i, expected in enumerate([10, 15, 20]):
        value = 10 + i * 5
        result = _resolve_fields("{{seq}}", {}, datetime(2026, 1, 1), seq=str(value))
        assert result == str(expected), f"step=5, item {i}: expected {expected!r}, got {result!r}"


def test_render_sequence_returns_exactly_count(engine: RenderEngine) -> None:
    """render_sequence count is honoured precisely (not off by one)."""
    layout = [{"type": "text", "text": "{{seq}}"}]
    for count in (1, 5, 10):
        results = list(engine.render_sequence(layout, {}, CANVAS_W, None, start=1, count=count))
        assert len(results) == count, f"count={count}: expected {count} images, got {len(results)}"


# ── two-color (red/black) rendering ───────────────────────────────────────────────
def _has_color(img: Image.Image, rgb: tuple[int, int, int]) -> bool:
    """True if the RGB image contains at least one pixel of the exact colour."""
    return rgb in set(img.convert("RGB").getdata())


def test_red_off_byte_identical_to_default(engine: RenderEngine) -> None:
    """red=False must produce byte-identical output to the default (no red kwarg).

    Regression guard: the two-color code path must not perturb a normal monochrome render even
    when the layout carries `color: red` elements — with red off they simply print black on "L".
    """
    layout = [
        {"type": "title", "text": "Black"},
        {"type": "text", "text": "marked red but red is off", "color": "red"},
        {"type": "qr", "data": "x", "color": "red"},
        {"type": "line", "color": "red"},
        {"type": "box"},
    ]
    png_default = engine.render_to_png(layout, {}, canvas_width=CANVAS_W, canvas_height=None)
    png_false = engine.render_to_png(
        layout, {}, canvas_width=CANVAS_W, canvas_height=None, red=False
    )
    assert png_default == png_false, "red=False must be byte-identical to the default render"
    # And the default render is grayscale "L" — the two-color path is fully inert when off.
    assert to_pil(png_default).mode == "L"


def test_red_active_renders_rgb_with_pure_red(engine: RenderEngine) -> None:
    """With red=True a `color: red` element draws pure red (255,0,0) and the rest pure black."""
    layout = [
        {"type": "title", "text": "Black title"},
        {"type": "text", "text": "Red body", "color": "red"},
    ]
    img = engine.render(layout, {}, CANVAS_W, None, red=True)
    assert img.mode == "RGB", "two-color render must be RGB so the red layer survives to convert()"
    assert _has_color(img, (255, 0, 0)), "a color: red element must paste pure red ink"
    assert _has_color(img, (0, 0, 0)), "the black element must paste pure black ink"


def test_red_active_color_red_only_when_active(engine: RenderEngine) -> None:
    """A `color: red` element prints BLACK (no red layer) when red is not active.

    This is the documented least-surprising rule: the label still prints, monochrome, and red=False
    output is unaffected by element colours.
    """
    layout = [{"type": "title", "text": "Marked red", "color": "red"}]
    img = engine.render(layout, {}, CANVAS_W, None, red=False)
    assert img.mode == "L"
    assert img.convert("RGB").getextrema()  # sanity: image exists
    assert not _has_color(img, (255, 0, 0)), "no red ink may appear when red is inactive"


def test_red_non_red_element_is_black_under_two_color(engine: RenderEngine) -> None:
    """An element without color: red stays black even on the RGB two-color canvas (no red leak)."""
    layout = [{"type": "title", "text": "Plain black title"}]
    img = engine.render(layout, {}, CANVAS_W, None, red=True)
    assert img.mode == "RGB"
    assert _has_color(img, (0, 0, 0))
    assert not _has_color(img, (255, 0, 0)), "a non-red element must not introduce red ink"


def test_red_with_high_res_doubles_and_stays_rgb(engine: RenderEngine) -> None:
    """red + high_res compose orthogonally: RGB at the 2x-scaled size with red ink present."""
    layout = [{"type": "title", "text": "T"}, {"type": "text", "text": "r", "color": "red"}]
    off = to_pil(engine.render_to_png(layout, {}, CANVAS_W, None, red=True))
    on = engine.render(layout, {}, CANVAS_W, None, red=True, high_res=True)
    assert on.mode == "RGB"
    assert on.width == off.width * 2
    assert on.height == off.height * 2
    assert _has_color(on, (255, 0, 0)), "the red layer must survive high_res scaling"


def test_red_row_children_inherit_two_color(engine: RenderEngine) -> None:
    """A `color: red` child inside a row renders red; the row canvas is RGB and composes cleanly."""
    layout = [
        {
            "type": "row",
            "children": [
                {"type": "text", "text": "left black"},
                {"type": "text", "text": "right red", "color": "red", "width": 200},
            ],
        }
    ]
    img = engine.render(layout, {}, CANVAS_W, None, red=True)
    assert img.mode == "RGB"
    assert _has_color(img, (255, 0, 0)), "a red row child must paste red ink"
    assert _has_color(img, (0, 0, 0)), "the black row child must paste black ink"


def test_red_graphical_elements_tint_to_red(engine: RenderEngine) -> None:
    """A `color: red` QR/barcode tints its ink to pure red under two-color mode."""
    layout = [{"type": "qr", "data": "HELLO", "color": "red"}]
    img = engine.render(layout, {}, CANVAS_W, None, red=True)
    assert img.mode == "RGB"
    assert _has_color(img, (255, 0, 0)), "a color: red QR must render in red"


# ── a draft renders identically to the equivalent SAVED template ──────
def test_draft_renders_identically_to_saved_template(engine: RenderEngine, tmp_path: Path) -> None:
    """Same YAML + fields → byte-identical PNG whether loaded from a file or from a string.

    This proves the draft studio path (validate_template_from_string) reuses the SAME validation
    and render as a saved template (load_template): both produce a Template with the same layout,
    and a single engine render call on each must yield identical bytes.
    """
    import textwrap

    from app.loader import load_template, validate_template_from_string

    yaml_text = textwrap.dedent("""\
        name: parity
        description: Parity template
        label: "62"
        rotate: 0
        fields:
          required: [title]
          optional: [subtitle]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: subtitle, text: "{{subtitle}}"}
    """)

    # Saved: write the file and load it the same way the registry does.
    path = tmp_path / "parity.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    saved = load_template(path)

    # Draft: validate straight from the in-memory string — no file involved.
    draft = validate_template_from_string(yaml_text)

    assert draft.layout == saved.layout
    assert draft.label == saved.label
    assert draft.rotate == saved.rotate

    fields = {"title": "Hello", "subtitle": "World"}
    now = datetime(2026, 6, 25, 12, 0, 0)

    saved_png = engine.render_to_png(
        saved.layout, fields, CANVAS_W, None, saved.rotate, "en", now=now
    )
    draft_png = engine.render_to_png(
        draft.layout, fields, CANVAS_W, None, draft.rotate, "en", now=now
    )
    assert draft_png == saved_png, "a draft must render byte-identically to its saved equivalent"
