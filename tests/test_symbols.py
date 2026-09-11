# SPDX-License-Identifier: GPL-3.0-or-later
"""Symbol encoding and dot-exact drawing (app.render.symbols)."""

from __future__ import annotations

import pytest
from PIL import Image

from app.render.symbols import (
    QR_ECL_CHOICES,
    QR_QUIET_MODULES,
    Symbol2D,
    SymbolEncodeError,
    draw_marks,
    encode_qr,
    runs_from_matrix,
)


def _pixel_values(img: Image.Image) -> set[int]:
    return set(img.getdata())


# ── runs_from_matrix ────────────────────────────────────────────────────────────
def test_runs_from_matrix_collapses_horizontal_runs() -> None:
    matrix = [
        [1, 1, 0, 1],
        [0, 0, 0, 0],
        [1, 0, 1, 1],
    ]
    sym = runs_from_matrix(matrix, quiet=2)
    assert (sym.cols, sym.rows, sym.quiet) == (4, 3, 2)
    assert sym.marks == ((0, 0, 2, 1), (3, 0, 1, 1), (0, 2, 1, 1), (2, 2, 2, 1))
    assert (sym.units_wide, sym.units_tall) == (8, 7)


def test_runs_from_matrix_treats_any_truthy_cell_as_dark() -> None:
    sym = runs_from_matrix([[True, None, 7]], quiet=0)
    assert sym.marks == ((0, 0, 1, 1), (2, 0, 1, 1))


def test_runs_from_matrix_empty() -> None:
    sym = runs_from_matrix([], quiet=1)
    assert sym.marks == () and (sym.cols, sym.rows) == (0, 0)


# ── draw_marks ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("module_px", [1, 3, 8])
def test_draw_marks_is_dot_exact_and_binary(module_px: int) -> None:
    """Every module is exactly module_px dots square, on integer boundaries, in pure black/white."""
    sym = Symbol2D(marks=((1, 0, 2, 1), (0, 1, 1, 1)), cols=3, rows=2, quiet=1)
    img = draw_marks(sym, module_px)
    assert img.mode == "L"
    assert img.size == (5 * module_px, 4 * module_px)
    assert _pixel_values(img) <= {0, 255}
    q = module_px  # quiet zone offset
    # (1,0,w=2): dark from x=q+1*m .. q+3*m-1 on rows q .. q+m-1
    for y in range(q, q + module_px):
        for x in range(q + module_px, q + 3 * module_px):
            assert img.getpixel((x, y)) == 0
        assert img.getpixel((q + module_px - 1, y)) == 255  # the module before it is light
        assert img.getpixel((q + 3 * module_px, y)) == 255  # and the one after
    # The quiet zone ring is white.
    assert img.getpixel((0, 0)) == 255 and img.getpixel((img.width - 1, img.height - 1)) == 255


def test_draw_marks_rejects_sub_dot_modules() -> None:
    sym = Symbol2D(marks=(), cols=1, rows=1, quiet=0)
    with pytest.raises(ValueError, match="at least one dot"):
        draw_marks(sym, 0)


# ── encode_qr ───────────────────────────────────────────────────────────────────
def test_encode_qr_default_level_is_a_square_symbol_with_spec_quiet_zone() -> None:
    sym = encode_qr("https://example.com")
    assert sym.cols == sym.rows == 25  # version 2 at level M
    assert sym.quiet == QR_QUIET_MODULES == 4
    assert sym.units_wide == 33
    # Finder pattern: the top-left 7x7 outer ring is dark, so the first run spans 7 modules.
    assert sym.marks[0] == (0, 0, 7, 1)


@pytest.mark.parametrize("level", sorted(QR_ECL_CHOICES))
def test_encode_qr_every_level_encodes(level: str) -> None:
    assert encode_qr("labelito", level).cols >= 21


def test_encode_qr_higher_level_needs_more_modules() -> None:
    assert encode_qr("https://example.com", "H").cols > encode_qr("https://example.com", "L").cols


def test_encode_qr_rejects_unknown_level() -> None:
    with pytest.raises(SymbolEncodeError, match="error_correction must be one of"):
        encode_qr("x", "X")


def test_encode_qr_overflow_names_the_level() -> None:
    with pytest.raises(
        SymbolEncodeError, match=r"cannot encode 5000 characters at error_correction H"
    ):
        encode_qr("x" * 5000, "H")
