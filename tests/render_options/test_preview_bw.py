# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for /preview mirroring the print's black/white conversion.

Covers the ``_preview_bw_convert`` helper directly (dither / threshold, matching brother_ql's exact
0-100->0-255 mapping) and the ``/preview`` route wiring (options reach the render, red is unchanged).
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from app.main import _preview_bw_convert


# ── _preview_bw_convert: direct unit tests of the conversion helper ──────────────
def test_preview_bw_convert_dither_produces_pure_binary() -> None:
    """Floyd-Steinberg dithering a gradient must yield only two output gray levels (0/255) — not
    the old many-shades-of-grey anti-aliased render."""
    # A horizontal gradient exercises the full 0-255 range, unlike a flat image.
    img = Image.new("L", (256, 10))
    for x in range(256):
        for y in range(10):
            img.putpixel((x, y), x)
    out = _preview_bw_convert(img, dither=True, threshold=70.0)
    assert out.mode == "L"
    colors = out.getcolors(maxcolors=1000)
    assert colors is not None
    levels = {pixel for _count, pixel in colors}
    assert levels <= {0, 255}
    assert len(levels) == 2, f"expected pure two-level dithering, got {levels}"


def test_preview_bw_convert_threshold_extremes_flip_mid_gray() -> None:
    """A mid-gray pixel must flip from white to black as threshold sweeps from a low to a high
    cutoff percentage — the same 0-100 knob brother_ql's convert() consumes."""
    img = Image.new("L", (4, 4), 128)  # flat mid-gray
    low = _preview_bw_convert(img, dither=False, threshold=1.0)
    high = _preview_bw_convert(img, dither=False, threshold=99.0)
    assert low.getpixel((0, 0)) == 255, "threshold=1 (near-white cutoff) must keep mid-gray white"
    assert high.getpixel((0, 0)) == 0, "threshold=99 (near-black cutoff) must turn mid-gray black"


def test_preview_bw_convert_matches_brother_ql_threshold_mapping() -> None:
    """The cutoff must match brother_ql.conversion.convert's exact formula:
    ``cutoff = min(255, max(0, int((100 - threshold) / 100 * 255)))`` applied to the inverted image.
    At threshold=70.0 the cutoff is 76, placing the black/white boundary between luminance 179
    (black) and 180 (white) in the ORIGINAL (non-inverted) image."""
    darker = Image.new("L", (2, 2), 179)
    lighter = Image.new("L", (2, 2), 180)
    assert _preview_bw_convert(darker, dither=False, threshold=70.0).getpixel((0, 0)) == 0
    assert _preview_bw_convert(lighter, dither=False, threshold=70.0).getpixel((0, 0)) == 255


def test_preview_bw_convert_dither_ignores_threshold() -> None:
    """Under dither, the threshold value must have no bearing (brother_ql ignores it too)."""
    img = Image.new("L", (4, 4), 128)
    with_low = _preview_bw_convert(img, dither=True, threshold=1.0)
    with_high = _preview_bw_convert(img, dither=True, threshold=99.0)
    assert list(with_low.getdata()) == list(with_high.getdata())


# ── /preview route: options actually reach the conversion ────────────────────────
def _colors_of(png: bytes) -> set[int]:
    img = Image.open(io.BytesIO(png)).convert("L")
    colors = img.getcolors(maxcolors=1_000_000)
    assert colors is not None
    return {pixel for _count, pixel in colors}


def test_preview_dither_true_returns_two_gray_levels(client: TestClient) -> None:
    """A dithered /preview must be pure two-level B/W, matching what /print would produce."""
    resp = client.post(
        "/preview",
        json={"template": "simple", "fields": {"title": "Hello"}, "options": {"dither": True}},
    )
    assert resp.status_code == 200
    levels = _colors_of(resp.content)
    assert levels <= {0, 255}
    assert len(levels) == 2, f"expected exactly two gray levels under dither, got {levels}"


def test_preview_dither_false_default_returns_two_gray_levels(client: TestClient) -> None:
    """Even without dither, the threshold cutoff still collapses the preview to pure B/W (no more
    many-shades-of-grey anti-aliasing) — mirroring the print raster's hard cutoff."""
    resp = client.post("/preview", json={"template": "simple", "fields": {"title": "Hello"}})
    assert resp.status_code == 200
    levels = _colors_of(resp.content)
    assert levels <= {0, 255}


def test_preview_threshold_option_changes_output(client: TestClient) -> None:
    """A different threshold option must actually reach the conversion (not be silently dropped)."""
    body = {"template": "simple", "fields": {"title": "Hello"}}
    low = client.post("/preview", json={**body, "options": {"threshold": 1.0}}).content
    high = client.post("/preview", json={**body, "options": {"threshold": 99.0}}).content
    assert low != high, "threshold=1 and threshold=99 must produce different preview pixels"


def test_preview_red_template_stays_monochrome(client: TestClient) -> None:
    """/preview never goes two-color regardless of a template's `color: red` elements — unchanged
    this round. The red-colored element still renders plain black/white, never RGB with red ink."""
    resp = client.post(
        "/preview",
        json={
            "template": "red-label",
            "fields": {"title": "Hi"},
            "options": {"dither": True},
        },
    )
    assert resp.status_code == 200
    img = Image.open(io.BytesIO(resp.content))
    assert img.mode != "RGB"
    assert (255, 0, 0) not in set(img.convert("RGB").getdata()), (
        "a color: red element must not paste red ink in a preview"
    )
    levels = _colors_of(resp.content)
    assert levels <= {0, 255}
