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


# ── encode_datamatrix ───────────────────────────────────────────────────────────
def test_encode_datamatrix_square_symbol_with_finder_and_spec_quiet_zone() -> None:
    from app.render.symbols import DATAMATRIX_QUIET_MODULES, encode_datamatrix

    sym = encode_datamatrix("Hello labelito")
    assert sym.cols == sym.rows == 16
    assert sym.quiet == DATAMATRIX_QUIET_MODULES == 1
    # The L-shaped finder: the whole left column and bottom row are dark.
    dark = {(x + dx, y) for x, y, w, _h in sym.marks for dx in range(w)}
    assert all((0, y) in dark for y in range(sym.rows))
    assert all((x, sym.rows - 1) in dark for x in range(sym.cols))


def test_encode_datamatrix_rectangular_is_wider_than_tall() -> None:
    from app.render.symbols import encode_datamatrix

    sym = encode_datamatrix("Hello", "rectangular")
    assert sym.cols > sym.rows  # one of the six ECC200 rectangular sizes (here 18 x 8)


def test_encode_datamatrix_gs1_changes_the_symbol_and_requires_ascii() -> None:
    from app.render.symbols import GS1_SEPARATOR, SymbolEncodeError, encode_datamatrix

    payload = f"0109501101020917{GS1_SEPARATOR}10ABC123"
    plain = encode_datamatrix(payload)
    gs1 = encode_datamatrix(payload, gs1=True)
    assert gs1.marks != plain.marks  # the leading FNC1 and the separator codeword are encoded
    with pytest.raises(SymbolEncodeError, match="gs1 data must be ASCII"):
        encode_datamatrix("01éé", gs1=True)


def test_encode_datamatrix_rejects_unknown_shape() -> None:
    from app.render.symbols import SymbolEncodeError, encode_datamatrix

    with pytest.raises(SymbolEncodeError, match="symbol_shape must be one of"):
        encode_datamatrix("x", "round")


# ── encode_aztec ────────────────────────────────────────────────────────────────
def test_encode_aztec_compact_symbol_has_no_quiet_zone() -> None:
    from app.render.symbols import AZTEC_QUIET_MODULES, encode_aztec

    sym = encode_aztec("Hello labelito")
    assert sym.cols == sym.rows == 19  # smallest compact symbol
    assert sym.quiet == AZTEC_QUIET_MODULES == 0
    assert sym.units_wide == 19


def test_encode_aztec_full_symbol_size_follows_layers() -> None:
    from app.render.symbols import encode_aztec

    assert encode_aztec("Hi", symbol_kind="full", layers=4).cols == 31
    assert encode_aztec("Hi", symbol_kind="compact", layers=2).cols == 19


def test_encode_aztec_layers_without_kind_and_bad_kind_are_errors() -> None:
    from app.render.symbols import SymbolEncodeError, encode_aztec

    with pytest.raises(SymbolEncodeError, match="symbol_kind"):
        encode_aztec("x", layers=3)
    with pytest.raises(SymbolEncodeError, match="symbol_kind must be one of"):
        encode_aztec("x", symbol_kind="huge")


# ── encode_pdf417 ───────────────────────────────────────────────────────────────
def test_encode_pdf417_is_wide_and_stretched_by_row_height() -> None:
    from app.render.symbols import PDF417_QUIET_MODULES, encode_pdf417

    sym = encode_pdf417("Hello labelito")
    assert sym.quiet == PDF417_QUIET_MODULES == 2
    assert sym.cols > sym.rows  # stacked linear: wide and shallow
    assert sym.rows % 3 == 0  # default row_height 3 stretches every codeword row
    one = encode_pdf417("Hello labelito", row_height=1)
    assert one.rows * 3 == sym.rows and one.cols == sym.cols


def test_encode_pdf417_columns_and_ecl_change_the_geometry() -> None:
    from app.render.symbols import encode_pdf417

    narrow = encode_pdf417("Hello labelito, hello world", columns=2)
    wide = encode_pdf417("Hello labelito, hello world", columns=8)
    assert narrow.cols < wide.cols and narrow.rows > wide.rows
    assert encode_pdf417("x", ecl=8).rows > encode_pdf417("x", ecl=0).rows


def test_encode_pdf417_rejects_encoder_option_errors_as_symbol_errors() -> None:
    from app.render.symbols import SymbolEncodeError, encode_pdf417

    with pytest.raises(SymbolEncodeError, match="pdf417"):
        encode_pdf417("x", columns=99)
