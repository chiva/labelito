"""Landscape (canvas-swap) rotation for die-cut address labels and continuous tape.

Covers the `_compose_canvas` decision, the engine→driver raster contract it protects (a die-cut
right-angle rotation must be composed on a SWAPPED canvas or brother_ql rejects it; a landscape
continuous layout must be turned by the ENGINE or brother_ql rescales it), and the preview path
returning a readable landscape image.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

import pytest
from brother_ql.labels import ALL_LABELS
from PIL import Image, ImageChops

import app.main as main_mod
from app.drivers.brother_ql import BrotherQLDriver
from app.loader import load_template, validate_template_from_string
from app.media import mm_to_dots
from app.render.engine import RenderEngine
from app.render.i18n import Translator

REPO = Path(__file__).resolve().parent.parent
_LABELS = {lbl.identifier: lbl for lbl in ALL_LABELS}

_ADDRESS_FIELDS = {
    "name": "Ada Lovelace",
    "line1": "1234 Example Avenue, Apt 5B",
    "line2": "Brooklyn, NY 11201",
    "line3": "United States",
}


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


# ── _compose_canvas unit ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("rotate", [90, 270])
def test_compose_canvas_die_cut_right_angle_swaps(rotate: int) -> None:
    assert main_mod._compose_canvas(306, 991, rotate) == (991, 306, True)


@pytest.mark.parametrize("rotate", [0, 180])
def test_compose_canvas_die_cut_straight_no_swap(rotate: int) -> None:
    assert main_mod._compose_canvas(306, 991, rotate) == (306, 991, False)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_compose_canvas_square_die_cut_never_swaps(rotate: int) -> None:
    # Square/round die-cut (width == height, e.g. 23x23, d12): a swap is a dimensional no-op and the
    # driver rotates the raster in place, so it must take the non-swapped (full-rotation) preview path.
    assert main_mod._compose_canvas(202, 202, rotate) == (202, 202, False)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_compose_canvas_continuous_without_length_never_swaps(rotate: int) -> None:
    # Continuous media without a declared length has no fixed second dimension to clash; height
    # stays None, no swap. (The loader lets only 0/180 reach this path.)
    assert main_mod._compose_canvas(696, None, rotate) == (696, None, False)


@pytest.mark.parametrize("rotate", [90, 270])
def test_compose_canvas_continuous_landscape_composes_on_length_by_tape_width(rotate: int) -> None:
    # Landscape: the declared length is the compose WIDTH, the tape's printable width the fixed
    # HEIGHT, and the image is already in its readable orientation (swapped=True).
    assert main_mod._compose_canvas(696, None, rotate, length_px=1181) == (1181, 696, True)


@pytest.mark.parametrize("rotate", [0, 180])
def test_compose_canvas_length_is_inert_for_upright_continuous(rotate: int) -> None:
    assert main_mod._compose_canvas(696, None, rotate, length_px=1181) == (696, None, False)


def test_compose_canvas_length_is_inert_for_die_cut() -> None:
    # A die-cut label's length is fixed by its id; a stray length_px must not change the swap.
    assert main_mod._compose_canvas(306, 991, 90, length_px=1181) == (991, 306, True)
    assert main_mod._compose_canvas(306, 991, 0, length_px=1181) == (306, 991, False)


def test_landscape_length_px_uses_configured_driver_dpi() -> None:
    tmpl = validate_template_from_string(_LANDSCAPE_YAML.format(rotate=90, length=100))
    dpi = main_mod._driver_cls.CAPABILITY.dpi
    assert main_mod._landscape_length_px(tmpl) == mm_to_dots(100, dpi) == round(100 / 25.4 * dpi)
    upright = validate_template_from_string(_ASYMMETRIC_SQUARE_YAML.format(rotate=0))
    assert main_mod._landscape_length_px(upright) is None


def test_mm_to_dots_rounds_to_whole_dots() -> None:
    assert mm_to_dots(100, 300) == 1181
    assert mm_to_dots(62, 300) == 732
    assert mm_to_dots(25.4, 300) == 300


# ── landscape continuous: engine turns, driver receives rotate 0, no rescale ─────────
_LANDSCAPE_YAML = """\
name: landscape-probe
description: Landscape layout along 62 mm continuous tape
label: "62"
rotate: {rotate}
length: {length}
valign: center
fields:
  required: [name]
  optional: [line1, line2]
layout:
  - {{type: text, text: "{{{{name}}}}", size: 64, bold: true, max_lines: 1, align: left}}
  - {{type: text, text: "{{{{line1}}}}", size: 44, max_lines: 1, align: left}}
  - {{type: text, text: "{{{{line2}}}}", size: 44, max_lines: 1, align: left}}
"""

_LANDSCAPE_FIELDS = {"name": "Ada Lovelace", "line1": "1234 Example Avenue", "line2": "Brooklyn"}


def _driver_opts(label: str, *, high_res: bool = False, red: bool = False) -> dict[str, Any]:
    return {
        "model": "QL-810W",
        "label": label,
        "rotate": 0,
        "cut": True,
        "copies": 1,
        "dither": False,
        "threshold": 70.0,
        "high_res": high_res,
        "red": red,
    }


@pytest.mark.parametrize("rotate", [90, 270])
@pytest.mark.parametrize("high_res", [False, True])
def test_landscape_continuous_raster_is_tape_wide_and_not_resized(
    engine: RenderEngine, caplog: Any, rotate: int, high_res: bool
) -> None:
    """Compose on (length x tape width), turn in the engine, hand the driver rotate=0: the raster is
    exactly tape-width wide and `length` long (x2 in high_res), and brother_ql's fallback resize —
    the silent distortion this mode exists to avoid — never fires."""
    tmpl = validate_template_from_string(_LANDSCAPE_YAML.format(rotate=rotate, length=100))
    width_px, _endless = _LABELS["62"].dots_printable
    assert width_px == 696
    length_px = mm_to_dots(100, 300)
    canvas_w, canvas_h, swapped = main_mod._compose_canvas(
        width_px, None, rotate, length_px=length_px
    )
    assert swapped and (canvas_w, canvas_h) == (length_px, width_px)

    png = engine.render_to_png(
        tmpl.layout,
        _LANDSCAPE_FIELDS,
        canvas_w,
        canvas_h,
        rotate=rotate,
        valign=tmpl.valign,
        high_res=high_res,
    )
    scale = 2 if high_res else 1
    composed = Image.open(io.BytesIO(png))
    assert composed.size == (width_px * scale, length_px * scale)

    driver = BrotherQLDriver.for_model("QL-810W")()
    with caplog.at_level(logging.WARNING, logger="brother_ql.conversion"):
        payload = driver.render_payload(png, _driver_opts("62", high_res=high_res))
    assert isinstance(payload, bytes) and len(payload) > 0
    assert not any("resize" in r.getMessage().lower() for r in caplog.records)


def test_landscape_continuous_red_raster_keeps_rgb_and_is_not_resized(
    engine: RenderEngine, caplog: Any
) -> None:
    """A quarter turn is a lossless transpose, so the two-colour RGB canvas survives it and the
    red-capable driver path accepts the tape-wide raster without a resize."""
    tmpl = validate_template_from_string(
        _LANDSCAPE_YAML.format(rotate=90, length=100).replace('label: "62"', 'label: "62red"')
    )
    length_px = mm_to_dots(100, 300)
    png = engine.render_to_png(
        tmpl.layout, _LANDSCAPE_FIELDS, length_px, 696, rotate=90, valign="center", red=True
    )
    composed = Image.open(io.BytesIO(png))
    assert composed.mode == "RGB" and composed.size == (696, length_px)
    driver = BrotherQLDriver.for_model("QL-810W")()
    with caplog.at_level(logging.WARNING, logger="brother_ql.conversion"):
        payload = driver.render_payload(png, _driver_opts("62red", red=True))
    assert isinstance(payload, bytes) and len(payload) > 0
    assert not any("resize" in r.getMessage().lower() for r in caplog.records)


def test_landscape_continuous_turned_by_driver_would_be_resized(
    engine: RenderEngine, caplog: Any
) -> None:
    """Regression for the failure this mode replaces: the old continuous path composed at the TAPE
    width (696 x content height) and let the driver turn it; the turned raster is then
    content-height wide, which brother_ql rescales to the tape (and logs), distorting the print.
    The print path must never hand the driver a quarter turn on continuous media."""
    png = engine.render_to_png([{"type": "text", "text": "x", "size": 20}], {}, 696, None, rotate=0)
    assert Image.open(io.BytesIO(png)).size == (696, 200)  # tape-wide, min_length_px tall
    driver = BrotherQLDriver.for_model("QL-810W")()
    opts = _driver_opts("62")
    opts["rotate"] = 90
    with caplog.at_level(logging.WARNING, logger="brother_ql.conversion"):
        driver.render_payload(png, opts)
    assert any("resize" in r.getMessage().lower() for r in caplog.records)


def test_landscape_preview_is_readable_landscape_and_270_is_the_180_turn_of_90() -> None:
    """Preview parity mirrors die-cut: both turns preview as the same landscape strip, and the 270°
    preview is the 90° preview turned 180°, so an approved preview matches its print."""
    fields = {"name": "TOP-LEFT-EDGE"}
    tmpl90 = validate_template_from_string(_LANDSCAPE_YAML.format(rotate=90, length=100))
    tmpl270 = validate_template_from_string(_LANDSCAPE_YAML.format(rotate=270, length=100))
    png90 = main_mod._render_template_preview(tmpl90, fields, "en", dither=False)
    png270 = main_mod._render_template_preview(tmpl270, fields, "en", dither=False)
    img90 = Image.open(io.BytesIO(png90))
    img270 = Image.open(io.BytesIO(png270))
    assert img90.size == img270.size == (mm_to_dots(100, main_mod._driver_cls.CAPABILITY.dpi), 696)
    assert img90.width > img90.height
    assert png90 != png270
    assert img270.rotate(180).tobytes() == img90.tobytes()


def _ink_rows(img: Image.Image) -> tuple[int, int]:
    bbox = ImageChops.invert(img.convert("L")).getbbox()
    assert bbox is not None
    return bbox[1], bbox[3]


def test_landscape_valign_center_and_overflow_clip_behave_like_die_cut(
    engine: RenderEngine,
) -> None:
    """The tape width is the fixed axis: a fitting block is centred within it by valign, and a block
    taller than it is clipped to exactly the tape width — never grown, never rescaled."""
    length_px = mm_to_dots(100, 300)
    small = [{"type": "text", "text": "one line", "size": 40, "max_lines": 1}]
    centred = engine.render(small, {}, length_px, 696, rotate=0, valign="center")
    top, bottom = _ink_rows(centred)
    assert centred.size == (length_px, 696)
    assert abs(top - (696 - bottom)) <= 4  # equal slack above and below, within glyph metrics

    tall = [{"type": "title", "text": f"line {i}", "max_lines": 1} for i in range(12)]
    clipped = engine.render(tall, {}, length_px, 696, rotate=90, valign="center")
    assert clipped.size == (696, length_px)  # 12 x 73 px > 696: clipped, not grown


# ── engine → driver raster contract ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "template_name,label_id",
    [("29x90-address", "29x90"), ("17x54-address", "17x54"), ("62-shipping-landscape", "62")],
)
def test_address_templates_are_landscape(template_name: str, label_id: str) -> None:
    tmpl = load_template(REPO / "templates" / f"{template_name}.yaml")
    assert tmpl.rotate == 90, "address template must opt into the landscape rotation"
    assert tmpl.label == label_id


@pytest.mark.parametrize(
    "template_name,label_id",
    [("29x90-address", "29x90"), ("17x54-address", "17x54")],
)
def test_die_cut_rotated_raster_is_accepted_by_driver(
    engine: RenderEngine, template_name: str, label_id: str
) -> None:
    """The swapped-canvas raster + driver rotate=90 lands on dots_printable — no ValueError."""
    tmpl = load_template(REPO / "templates" / f"{template_name}.yaml")
    width_px, height_px = _LABELS[label_id].dots_printable
    canvas_w, canvas_h, swapped = main_mod._compose_canvas(width_px, height_px, tmpl.rotate)
    assert swapped and (canvas_w, canvas_h) == (height_px, width_px)

    png = engine.render_to_png(tmpl.layout, _ADDRESS_FIELDS, canvas_w, canvas_h, rotate=0)
    composed = Image.open(io.BytesIO(png))
    assert composed.size == (height_px, width_px)  # landscape: long edge is the width

    driver = BrotherQLDriver.for_model("QL-810W")()
    payload = driver.render_payload(
        png,
        {
            "model": "QL-810W",
            "label": label_id,
            "rotate": tmpl.rotate,
            "cut": True,
            "copies": 1,
            "dither": False,
            "threshold": 70.0,
            "high_res": False,
            "red": False,
        },
    )
    assert isinstance(payload, bytes) and len(payload) > 0


@pytest.mark.parametrize("label_id", ["29x90", "17x54"])
def test_naive_field_flip_without_swap_is_rejected(engine: RenderEngine, label_id: str) -> None:
    """Regression: composing at the printable size (no swap) then rotating 90 is the failure the
    canvas swap avoids — brother_ql rejects the mismatched dimensions."""
    width_px, height_px = _LABELS[label_id].dots_printable
    layout = [{"type": "text", "text": "x", "size": 20}]
    png = engine.render_to_png(layout, {}, width_px, height_px, rotate=0)  # portrait, NOT swapped
    driver = BrotherQLDriver.for_model("QL-810W")()
    with pytest.raises(ValueError, match="Bad image dimensions"):
        driver.render_payload(
            png,
            {"model": "QL-810W", "label": label_id, "rotate": 90, "cut": True, "copies": 1},
        )


# ── preview path ─────────────────────────────────────────────────────────────────
def test_preview_die_cut_rotate90_is_landscape(engine: RenderEngine) -> None:
    """`_render_template_preview` returns a readable landscape PNG (width > height), not the
    portrait/sideways image a double rotation would produce."""
    tmpl = load_template(REPO / "templates" / "29x90-address.yaml")
    width_px, height_px = _LABELS[tmpl.label].dots_printable
    assert height_px is not None and height_px > width_px  # 29x90 is portrait as printable

    canvas_w, canvas_h, swapped = main_mod._compose_canvas(width_px, height_px, tmpl.rotate)
    preview_rotate = (tmpl.rotate - 90) if swapped else tmpl.rotate
    img = engine.render(tmpl.layout, _ADDRESS_FIELDS, canvas_w, canvas_h, preview_rotate)
    assert img.width > img.height  # readable landscape
    assert img.size == (height_px, width_px)


_ASYMMETRIC_DIE_CUT_YAML = """\
name: rotate-parity-probe
description: Asymmetric die-cut layout to distinguish 90 from 270 previews
label: "29x90"
rotate: {rotate}
fields:
  required: [name]
layout:
  - {{type: title, text: "{{{{name}}}}", align: left, max_lines: 1}}
"""


def test_preview_die_cut_90_and_270_differ() -> None:
    """Regression (Codex): a die-cut 270° preview must NOT be byte-identical to the 90° preview —
    the driver rotates 90° and 270° into rasters that differ by 180°, so their previews must too,
    or a 270° label prints upside-down relative to an approved preview."""
    from app.loader import validate_template_from_string

    fields = {"name": "TOP-LEFT-EDGE"}
    tmpl90 = validate_template_from_string(_ASYMMETRIC_DIE_CUT_YAML.format(rotate=90))
    tmpl270 = validate_template_from_string(_ASYMMETRIC_DIE_CUT_YAML.format(rotate=270))
    assert tmpl90.rotate == 90 and tmpl270.rotate == 270

    png90 = main_mod._render_template_preview(tmpl90, fields, "en", dither=False)
    png270 = main_mod._render_template_preview(tmpl270, fields, "en", dither=False)
    assert png90 != png270, "90° and 270° die-cut previews must be distinguishable"

    img90 = Image.open(io.BytesIO(png90))
    img270 = Image.open(io.BytesIO(png270))
    assert img90.size == img270.size  # same landscape canvas, opposite orientation
    assert img90.width > img90.height  # both readable landscape
    # 270 preview is the 90 preview turned 180° (net display rotation tmpl.rotate - 90).
    assert img270.rotate(180).tobytes() == img90.tobytes()


_ASYMMETRIC_SQUARE_YAML = """\
name: square-rotate-parity-probe
description: Asymmetric square die-cut layout to check rotation is not suppressed
label: "23x23"
rotate: {rotate}
fields:
  required: [name]
layout:
  - {{type: title, text: "{{{{name}}}}", align: left, max_lines: 1}}
"""


def test_preview_square_die_cut_applies_full_rotation() -> None:
    """Regression (Codex): a square/round die-cut label rotates in place, so its preview must apply
    the full ``tmpl.rotate`` (not the swapped net-rotation) — otherwise a rotate:90 label previews
    upright while the driver prints it sideways."""
    from app.loader import validate_template_from_string

    fields = {"name": "TOP-LEFT-EDGE"}
    tmpl0 = validate_template_from_string(_ASYMMETRIC_SQUARE_YAML.format(rotate=0))
    tmpl90 = validate_template_from_string(_ASYMMETRIC_SQUARE_YAML.format(rotate=90))

    png0 = main_mod._render_template_preview(tmpl0, fields, "en", dither=False)
    png90 = main_mod._render_template_preview(tmpl90, fields, "en", dither=False)
    assert png0 != png90, "square rotate:90 preview must show the rotation, not suppress it"

    img0 = Image.open(io.BytesIO(png0))
    img90 = Image.open(io.BytesIO(png90))
    assert img0.size == img90.size  # square, so dims unchanged
    # The driver rotates the rotate:0 raster by the full 90°; the preview must match that orientation.
    assert img0.rotate(90, expand=True).tobytes() == img90.tobytes()


# ── shipped landscape shipping template ─────────────────────────────────────────
_SHIPPING = REPO / "templates" / "62-shipping-landscape.yaml"
_SHIPPING_FULL = {
    "name": "Margaret Hamilton",
    "address1": "Apollo Guidance Way 1969",
    "address2": "Building 4, Suite 11",
    "zip": "02139",
    "city": "Cambridge",
    "region": "Massachusetts",
    "country": "United States",
}


def test_shipping_template_is_a_100mm_landscape_on_62mm_tape() -> None:
    tmpl = load_template(_SHIPPING)
    assert tmpl.name == "shipping-62"
    assert (tmpl.label, tmpl.rotate, tmpl.length_mm, tmpl.valign) == ("62", 90, 100.0, "center")
    assert tmpl.required_fields == ["name"]
    assert tmpl.optional_fields == ["address1", "address2", "city", "zip", "region", "country"]


def test_shipping_template_prints_tape_wide_without_resize(
    engine: RenderEngine, caplog: Any
) -> None:
    tmpl = load_template(_SHIPPING)
    length_px = mm_to_dots(100, 300)
    canvas_w, canvas_h, swapped = main_mod._compose_canvas(
        696, None, tmpl.rotate, length_px=length_px
    )
    assert swapped
    png = engine.render_to_png(
        tmpl.layout, _SHIPPING_FULL, canvas_w, canvas_h, rotate=tmpl.rotate, valign=tmpl.valign
    )
    assert Image.open(io.BytesIO(png)).size == (696, length_px)
    driver = BrotherQLDriver.for_model("QL-810W")()
    with caplog.at_level(logging.WARNING, logger="brother_ql.conversion"):
        payload = driver.render_payload(png, _driver_opts("62"))
    assert isinstance(payload, bytes) and len(payload) > 0
    assert not any("resize" in r.getMessage().lower() for r in caplog.records)


def test_shipping_template_unused_lines_vanish(
    engine: RenderEngine, fonts_dir: Path, icons_dir: Path, icon_collections_dir: Path
) -> None:
    """Only `name` set: the composed block IS the name strip (every optional line collapsed,
    including the space-joined "zip city" line). Adding `country` appends exactly that strip, and
    the full sample fits within the tape width with slack for valign to centre. Ink rows are
    compared against the same elements rendered alone, so glyph metrics cancel out exactly."""
    from app.render.elements import TextElement

    tmpl = load_template(_SHIPPING)
    length_px = mm_to_dots(100, 300)

    def strip(text: str, size: int, bold: bool) -> Image.Image:
        el = TextElement(text=text, size=size, bold=bold, align="left", max_lines=1)
        return el.render(length_px, {"__text__": text}, fonts_dir, icons_dir, icon_collections_dir)

    def composed(fields: dict[str, str]) -> Image.Image:
        return engine.render(tmpl.layout, fields, length_px, 696, rotate=0, valign="top")

    name_strip = strip("Margaret Hamilton", 64, True)
    country_strip = strip("United States", 44, True)

    assert _ink_rows(composed({"name": "Margaret Hamilton"})) == _ink_rows(name_strip)

    _c_top, c_bottom = _ink_rows(country_strip)
    with_country = composed({"name": "Margaret Hamilton", "country": "United States"})
    assert _ink_rows(with_country) == (_ink_rows(name_strip)[0], name_strip.height + c_bottom)

    _top, bottom = _ink_rows(composed(_SHIPPING_FULL))
    assert bottom < 696  # the full address fits the tape width, so valign: center has room
