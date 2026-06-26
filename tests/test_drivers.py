# SPDX-License-Identifier: GPL-3.0-or-later
"""Driver tests — fixture-based byte-level checks, no hardware required."""

from __future__ import annotations

import io
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from brother_ql.labels import ALL_LABELS
from brother_ql.models import ALL_MODELS
from PIL import Image

from app.drivers.base import DRIVERS, get_driver
from app.drivers.brother_ql import BrotherQLDriver

_FORK_MODELS = {m.identifier: m for m in ALL_MODELS}
_FORK_LABELS = {lbl.identifier: lbl for lbl in ALL_LABELS}


def minimal_png(width: int = 696, height: int = 200) -> bytes:
    img = Image.new("RGB", (width, height), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Registry ───────────────────────────────────────────────────────────────────
def test_brother_ql_registered() -> None:
    assert "brother_ql" in DRIVERS


def test_get_driver_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown driver"):
        get_driver("nonexistent_driver")


# ── Capability derivation (sourced from brother_ql_next, never hand-typed) ───────
@pytest.mark.parametrize("model_id", sorted(_FORK_MODELS))
def test_capability_derived_from_fork(model_id: str) -> None:
    """cut and dpi come from the library — they can't drift from it."""
    cap = BrotherQLDriver.for_model(model_id).CAPABILITY
    fork = _FORK_MODELS[model_id]
    assert cap.cut == bool(fork.cutting)
    assert cap.dpi == 300
    assert cap.supported_labels, f"{model_id} resolved no labels"


@pytest.mark.parametrize("model_id", sorted(_FORK_MODELS))
def test_supported_labels_respect_restrictions(model_id: str) -> None:
    """A model only exposes labels the fork actually permits for it."""
    cap = BrotherQLDriver.for_model(model_id).CAPABILITY
    for label_id in cap.supported_labels:
        label = _FORK_LABELS[label_id]
        if label.restricted_to_models:
            assert model_id in label.restricted_to_models, (
                f"{label_id!r} is restricted to {label.restricted_to_models}, not {model_id!r}"
            )


@pytest.mark.parametrize("model_id", sorted(_FORK_MODELS))
def test_label_geometry_matches_fork_dots(model_id: str) -> None:
    cap = BrotherQLDriver.for_model(model_id).CAPABILITY
    for label_id, geo in cap.label_geometries.items():
        label = _FORK_LABELS[label_id]
        assert geo.width_px == label.dots_printable[0]
        assert geo.media_type in ("continuous", "die_cut")
        if geo.media_type == "continuous":
            assert geo.height_px is None
        else:
            assert geo.height_px == label.dots_printable[1]


def test_ql_1110nwb_excludes_102mm_labels() -> None:
    """The 102mm labels are restricted away from the QL-1110NWB by the fork."""
    cap = BrotherQLDriver.for_model("QL-1110NWB").CAPABILITY
    assert "102x152" not in cap.supported_labels
    assert "39x90" in cap.supported_labels


def test_for_model_returns_configured_driver() -> None:
    cls = BrotherQLDriver.for_model("QL-810W")
    assert "62" in cls.CAPABILITY.supported_labels


def test_for_model_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown Brother QL model"):
        BrotherQLDriver.for_model("QL-9999X")


# ── render_payload (mocked brother_ql) ────────────────────────────────────────
def test_render_payload_calls_convert(png_62mm: bytes) -> None:
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    fake_instructions = b"\x1b@" + b"\x00" * 64
    mock_qlr = MagicMock()
    mock_qlr_cls = MagicMock(return_value=mock_qlr)
    mock_convert = MagicMock(return_value=fake_instructions)

    with (
        patch.dict(
            "sys.modules",
            {
                "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
                "brother_ql.conversion": MagicMock(convert=mock_convert),
            },
        ),
    ):
        result = driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
            },
        )

    assert result == fake_instructions


def test_render_payload_copies_replicates_images(png_62mm: bytes) -> None:
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 3,
            },
        )

    _, kwargs = mock_convert.call_args
    assert len(kwargs["images"]) == 3


def test_render_payload_sequence_label_is_single_image_convert(png_62mm: bytes) -> None:
    """A sequence label is converted as ONE image via the copies=1 single-image path.

    The driver no longer accepts a batched ``opts['pngs']`` stream: the print path renders & sends
    each sequence label individually (so each gets its own per-label status confirmation), and each
    of those individual sends reaches the driver as a single PNG with copies=1.  This asserts that
    contract — one decoded image handed to convert(), not a multiplied batch — so the per-label send
    design and the driver agree on the unit of conversion.
    """
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    captured: dict[str, object] = {}

    def _capture_convert(**kwargs: object) -> bytes:
        captured["images"] = kwargs["images"]
        return b"\x00" * 32

    mock_qlr_cls = MagicMock(return_value=MagicMock())

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=_capture_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,  # one printer job per sequence label
            },
        )

    images = captured["images"]
    assert isinstance(images, list), "A single sequence label is converted as one image"
    assert len(images) == 1, "copies=1 must hand convert() exactly one decoded image"
    assert images[0].mode == "RGB", "The single label is decoded to RGB"


def test_render_payload_no_longer_accepts_pngs_batch(png_62mm: bytes) -> None:
    """The driver's batched ``opts['pngs']`` path is removed (per-label send design).

    A stray ``pngs`` key in opts must be ignored — the driver converts the single ``png`` argument
    only — so no caller can accidentally revive a single-atomic-batch send that would bypass the
    per-label status confirmation.
    """
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    captured: dict[str, object] = {}

    def _capture_convert(**kwargs: object) -> bytes:
        captured["images"] = kwargs["images"]
        return b"\x00" * 32

    mock_qlr_cls = MagicMock(return_value=MagicMock())

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=_capture_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
                "pngs": [png_62mm, png_62mm, png_62mm],  # legacy key — must be ignored
            },
        )

    images = captured["images"]
    assert isinstance(images, list) and len(images) == 1, (
        "opts['pngs'] must be ignored; only the single png arg is converted"
    )


# ── cut kwarg regression test ─────────────────────────────────────────────────
def test_render_payload_uses_cut_not_cut_now(png_62mm: bytes) -> None:
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": False,
                "copies": 1,
            },
        )

    _, kwargs = mock_convert.call_args
    assert "cut" in kwargs, "convert() must be called with 'cut' kwarg"
    assert kwargs["cut"] is False, "cut=False must be forwarded"
    assert "cut_now" not in kwargs, "cut_now must not be passed (old broken kwarg)"


# ── Real brother_ql conversion: printable-width raster + rotate=90 (no mock) ──────
def test_render_payload_rotate90_continuous_real_conversion() -> None:
    """A printable-width (696px) raster + rotate=90 must rasterize through the REAL convert.

    Guards the print path's contract: the engine hands the driver an unrotated, printable-width
    image and the driver rotates it. A mocked convert can't catch a wrong-shape regression here.
    """
    driver = BrotherQLDriver.for_model("QL-810W")()
    png = minimal_png(696, 300)  # 62mm continuous printable width, unrotated
    out = driver.render_payload(
        png,
        {
            "model": "QL-810W",
            "label": "62",
            "rotate": 90,
            "cut": True,
            "copies": 1,
        },
    )
    assert isinstance(out, bytes | bytearray)
    assert len(out) > 0


def test_render_payload_rotate0_continuous_no_resize(caplog: Any) -> None:
    """A printable-width (696px) raster + rotate=0 must rasterize without brother_ql's resize.

    The continuous templates print upright at rotate=0, so the engine's 696px-wide raster already
    matches the roll's printable width. brother_ql therefore neither rotates nor resizes it — the
    fallback resize (which logs ``Need to resize the image...`` and rescales content) must not fire.
    """
    driver = BrotherQLDriver.for_model("QL-810W")()
    png = minimal_png(696, 300)  # 62mm continuous printable width, unrotated
    with caplog.at_level(logging.WARNING, logger="brother_ql.conversion"):
        out = driver.render_payload(
            png,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
            },
        )
    assert isinstance(out, bytes | bytearray)
    assert len(out) > 0
    assert not any("resize" in r.getMessage().lower() for r in caplog.records)


# ── 600 dpi high-resolution mode ────────────────────────────────────────────────
def test_render_payload_high_res_passes_dpi_600_true(png_62mm: bytes) -> None:
    """When high_res=True the driver must call convert() with dpi_600=True."""
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
                "high_res": True,
            },
        )

    _, kwargs = mock_convert.call_args
    assert kwargs.get("dpi_600") is True, "convert() must receive dpi_600=True when high_res=True"


def test_render_payload_high_res_false_passes_dpi_600_false(png_62mm: bytes) -> None:
    """When high_res=False (or absent) the driver must call convert() with dpi_600=False."""
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
                "high_res": False,
            },
        )

    _, kwargs = mock_convert.call_args
    assert kwargs.get("dpi_600") is False, (
        "convert() must receive dpi_600=False when high_res=False"
    )


def test_render_payload_high_res_absent_defaults_to_false(png_62mm: bytes) -> None:
    """When high_res is not in opts (absent), dpi_600 must default to False."""
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
                # high_res intentionally absent
            },
        )

    _, kwargs = mock_convert.call_args
    assert kwargs.get("dpi_600") is False, (
        "convert() must default to dpi_600=False when high_res absent"
    )


# ── two-color (red/black) printing ────────────────────────────────────────────────
def test_render_payload_red_true_passes_red_to_convert(png_62mm: bytes) -> None:
    """When red=True the driver must call convert() with red=True."""
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62red",
                "rotate": 0,
                "cut": True,
                "copies": 1,
                "red": True,
            },
        )

    _, kwargs = mock_convert.call_args
    assert kwargs.get("red") is True, "convert() must receive red=True when red=True"


def test_render_payload_red_false_passes_red_false(png_62mm: bytes) -> None:
    """When red=False (or absent) the driver must call convert() with red=False."""
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {
                "model": "QL-810W",
                "label": "62",
                "rotate": 0,
                "cut": True,
                "copies": 1,
                "red": False,
            },
        )

    _, kwargs = mock_convert.call_args
    assert kwargs.get("red") is False


def test_render_payload_red_absent_defaults_false(png_62mm: bytes) -> None:
    """When red is not in opts the driver must default convert(red=False)."""
    cls = BrotherQLDriver.for_model("QL-810W")
    driver = cls()

    mock_qlr_cls = MagicMock(return_value=MagicMock())
    mock_convert = MagicMock(return_value=b"\x00" * 32)

    with patch.dict(
        "sys.modules",
        {
            "brother_ql.raster": MagicMock(BrotherQLRaster=mock_qlr_cls),
            "brother_ql.conversion": MagicMock(convert=mock_convert),
        },
    ):
        driver.render_payload(
            png_62mm,
            {"model": "QL-810W", "label": "62", "rotate": 0, "cut": True, "copies": 1},
        )

    _, kwargs = mock_convert.call_args
    assert kwargs.get("red") is False


def test_render_payload_red_true_real_conversion_differs_from_mono() -> None:
    """A red=True print on a two-color model reaches the REAL convert(red=True) and emits DIFFERENT
    raster bytes than the monochrome path — proving the red command bytes are actually generated."""
    driver = BrotherQLDriver.for_model("QL-810W")()
    # Build an RGB image carrying both black and pure-red content (what the engine produces).
    img = Image.new("RGB", (696, 200), (255, 255, 255))
    img.paste((0, 0, 0), (0, 0, 348, 100))  # black block
    img.paste((255, 0, 0), (348, 0, 696, 100))  # red block
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    base = {"model": "QL-810W", "label": "62", "rotate": 0, "cut": True, "copies": 1}
    out_mono = driver.render_payload(png, {**base, "red": False})
    out_red = driver.render_payload(png, {**base, "red": True})
    assert isinstance(out_red, bytes | bytearray) and len(out_red) > 0
    assert out_mono != out_red, "red=True must produce different raster command bytes than mono"


def test_render_payload_red_on_unsupported_model_raises() -> None:
    """A red=True print on a non-two-color model raises BrotherQLUnsupportedCmd (mapped to 4xx
    by the print path), not a generic error."""
    from brother_ql.exceptions import BrotherQLUnsupportedCmd

    driver = BrotherQLDriver.for_model("QL-700")()  # QL-700: two_color False
    png = minimal_png(696, 200)
    with pytest.raises(BrotherQLUnsupportedCmd):
        driver.render_payload(
            png,
            {"model": "QL-700", "label": "62", "rotate": 0, "cut": True, "copies": 1, "red": True},
        )


# ── capability surfaces two-color + red media ─────────────────────────────────────
def test_capability_two_color_flag_matches_fork() -> None:
    """two_color on the Capability mirrors the fork's model.two_color for every model."""
    for model_id, fork in _FORK_MODELS.items():
        cap = BrotherQLDriver.for_model(model_id).CAPABILITY
        assert cap.two_color == bool(getattr(fork, "two_color", False)), model_id


def test_capability_two_color_models_are_the_red_capable_three() -> None:
    """Exactly QL-800/810W/820NWB report two_color=True."""
    two_color_models = {
        m for m in _FORK_MODELS if BrotherQLDriver.for_model(m).CAPABILITY.two_color
    }
    assert two_color_models == {"QL-800", "QL-810W", "QL-820NWB"}


def test_capability_red_labels_include_62red() -> None:
    """The black/red media identifier (62red) is surfaced in red_labels for a two-color model."""
    cap = BrotherQLDriver.for_model("QL-810W").CAPABILITY
    assert "62red" in cap.red_labels
    # Every red label must also be a supported label.
    assert set(cap.red_labels) <= set(cap.supported_labels)
