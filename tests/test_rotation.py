"""Landscape (canvas-swap) rotation for die-cut address labels.

Covers the `_compose_canvas` decision, the engine→driver raster contract it protects (a die-cut
right-angle rotation must be composed on a SWAPPED canvas or brother_ql rejects it), and the preview
path returning a readable landscape image.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from brother_ql.labels import ALL_LABELS
from PIL import Image

import app.main as main_mod
from app.drivers.brother_ql import BrotherQLDriver
from app.loader import load_template
from app.render.engine import RenderEngine
from app.render.i18n import Translator

REPO = Path(__file__).resolve().parent.parent
_LABELS = {lbl.identifier: lbl for lbl in ALL_LABELS}

_ADDRESS_FIELDS = {
    "name": "Santiago Fernandez",
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
def test_compose_canvas_continuous_never_swaps(rotate: int) -> None:
    # Continuous media has no fixed second dimension to clash; height stays None, no swap.
    assert main_mod._compose_canvas(696, None, rotate) == (696, None, False)


# ── engine → driver raster contract ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "template_name,label_id",
    [("29x90-address", "29x90"), ("17x54-address", "17x54")],
)
def test_die_cut_address_template_is_landscape(template_name: str, label_id: str) -> None:
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
    img = engine.render(
        tmpl.layout,
        _ADDRESS_FIELDS,
        canvas_w,
        canvas_h,
        0 if swapped else tmpl.rotate,
    )
    assert img.width > img.height  # readable landscape
    assert img.size == (height_px, width_px)
