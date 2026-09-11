# SPDX-License-Identifier: GPL-3.0-or-later
"""Symbol encoding and dot-exact drawing for the matrix (2D) code elements.

The element renderers in :mod:`app.render.elements` describe *where* a symbol sits on the label;
this module owns *what* the symbol is. Every encoder here returns a :class:`Symbol2D` — the symbol's
dark modules as horizontal runs in module units plus its spec quiet zone — and :func:`draw_marks`
turns that into a Pillow image whose modules are an INTEGER number of device dots. A thermal head
prints dots, not fractions of dots: a symbol scaled to an arbitrary pixel size lands module edges
between dots and the resampling filter smears them into grey, which the driver then thresholds
back into ragged black. Drawing on whole dots is what keeps small symbols scannable.

pyStrich is the single encoding backend. It is imported lazily inside each encoder so the app's
import graph (and the loader, which validates templates without rendering) stays light.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw

# One dark run: x, y, width, height — all in MODULE units, relative to the symbol's top-left
# module (the quiet zone is added at draw time).
Mark = tuple[int, int, int, int]

# Spec quiet zones, in modules per side. ISO/IEC 18004 §9.3 asks for 4 around a QR symbol.
QR_QUIET_MODULES = 4

QR_ECL_CHOICES = frozenset({"L", "M", "Q", "H"})
# Matches the level the qr element has always used, so a template that says nothing keeps its
# symbol density (a higher level adds modules for the same payload).
QR_ECL_DEFAULT = "M"


class SymbolEncodeError(ValueError):
    """The payload or options cannot be encoded as the requested symbol.

    A ``ValueError`` so the print path's uniform "Render error" mapping surfaces the message to
    the client, exactly as a malformed EAN payload does for the 1D barcode element.
    """


@dataclass(frozen=True)
class Symbol2D:
    """A matrix symbol as dark runs in module units, plus the quiet zone its spec requires."""

    marks: tuple[Mark, ...]
    cols: int
    rows: int
    quiet: int

    @property
    def units_wide(self) -> int:
        return self.cols + 2 * self.quiet

    @property
    def units_tall(self) -> int:
        return self.rows + 2 * self.quiet


def runs_from_matrix(matrix: Sequence[Sequence[object]], quiet: int) -> Symbol2D:
    """Collapse a row-major module matrix (truthy = dark) into horizontal dark runs.

    Runs rather than single modules keep the draw loop small: a version-40 QR has ~31k modules but
    only a few thousand runs. Every row is taken at the width of the first, which is what every
    pyStrich encoder produces.
    """
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0
    marks: list[Mark] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < cols:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < cols and row[x]:
                x += 1
            marks.append((start, y, x - start, 1))
    return Symbol2D(marks=tuple(marks), cols=cols, rows=rows, quiet=quiet)


def draw_marks(symbol: Symbol2D, module_px: int) -> Image.Image:
    """Draw *symbol* with every module exactly ``module_px`` device dots square.

    The result is an ``"L"`` image holding only 0 (dark) and 255 (light) — no intermediate greys —
    which is the pre-thresholded form :meth:`ElementBase._tint` relies on for two-colour labels. The
    quiet zone is part of the image so a caller that centres the symbol in a box never has to
    re-derive it.
    """
    if module_px < 1:
        raise ValueError(f"module_px must be at least one dot, got {module_px}")
    width = symbol.units_wide * module_px
    height = symbol.units_tall * module_px
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    q = symbol.quiet
    for x, y, w, h in symbol.marks:
        left = (q + x) * module_px
        top = (q + y) * module_px
        draw.rectangle((left, top, left + w * module_px - 1, top + h * module_px - 1), fill=0)
    return img


def encode_qr(data: str, error_correction: str = QR_ECL_DEFAULT) -> Symbol2D:
    """Encode *data* as a QR Code (ISO/IEC 18004) at the given error-correction level.

    The character set is chosen by the encoder (ASCII, then Latin-1, then UTF-8 with an ECI
    header), so any string encodes; only a payload too long for the largest symbol at this level
    fails, as a :class:`SymbolEncodeError` naming the level so the author knows which knob to turn.
    """
    from pystrich.exceptions import PyStrichError
    from pystrich.qrcode import QRCodeEncoder

    if error_correction not in QR_ECL_CHOICES:
        raise SymbolEncodeError(
            f"qr: error_correction must be one of {sorted(QR_ECL_CHOICES)}, got {error_correction!r}"
        )
    try:
        encoder = QRCodeEncoder(data, ecl=error_correction)  # type: ignore[arg-type]
    except PyStrichError as exc:
        raise SymbolEncodeError(
            f"qr: cannot encode {len(data)} characters at error_correction {error_correction}: {exc}"
        ) from exc
    return runs_from_matrix(encoder.matrix, QR_QUIET_MODULES)
