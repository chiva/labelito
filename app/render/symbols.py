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

import barcode as python_barcode
from PIL import Image, ImageDraw

# One dark run: x, y, width, height — all in MODULE units, relative to the symbol's top-left
# module (the quiet zone is added at draw time).
Mark = tuple[int, int, int, int]

# Spec quiet zones, in modules per side. ISO/IEC 18004 §9.3 asks for 4 around a QR symbol;
# ISO/IEC 16022 asks for 1 around a Data Matrix; Aztec (ISO/IEC 24778) needs none because its
# bull's-eye finder is in the centre; ISO/IEC 15438 asks for 2 around a PDF417.
QR_QUIET_MODULES = 4
DATAMATRIX_QUIET_MODULES = 1
AZTEC_QUIET_MODULES = 0
PDF417_QUIET_MODULES = 2

QR_ECL_CHOICES = frozenset({"L", "M", "Q", "H"})
# Matches the level the qr element has always used, so a template that says nothing keeps its
# symbol density (a higher level adds modules for the same payload).
QR_ECL_DEFAULT = "M"

# Data Matrix ECC200 symbol shapes: the 24 square sizes, the 6 rectangular ones, or whichever of
# the two families fits the payload in fewer modules.
DATAMATRIX_SHAPE_CHOICES = frozenset({"square", "rectangular", "auto"})
DATAMATRIX_SHAPE_DEFAULT = "square"
# ASCII 29 (GS) separates variable-length GS1 application identifiers in a GS1 payload; the
# encoder turns each into the FNC1 codeword the standard requires.
GS1_SEPARATOR = "\x1d"

# Aztec: error-correction is a percentage of the symbol reserved for correction codewords (23 is
# the ISO default); `compact` symbols have 1..4 layers, `full` ones 1..32.
AZTEC_ECC_DEFAULT = 23
AZTEC_ECC_MIN = 5
AZTEC_ECC_MAX = 95
AZTEC_KIND_CHOICES = frozenset({"auto", "compact", "full"})
AZTEC_KIND_DEFAULT = "auto"
AZTEC_LAYERS_MAX = 32
AZTEC_COMPACT_LAYERS_MAX = 4

# PDF417: 1..30 data columns, error-correction level 0..8 (each level doubles the correction
# codewords), and each codeword row is drawn `row_height` modules tall (3 is the ISO recommendation).
PDF417_COLUMNS_MIN = 1
PDF417_COLUMNS_MAX = 30
PDF417_ECL_MIN = 0
PDF417_ECL_MAX = 8
PDF417_ROW_HEIGHT_DEFAULT = 3
PDF417_ROW_HEIGHT_MIN = 1
PDF417_ROW_HEIGHT_MAX = 10
# A PDF417 is wide and shallow (a default symbol is 120+ modules across), so at the qr default of
# 160 px it would draw 1 dot per module — unreadable. Default it near the 62 mm tape width instead.
PDF417_DEFAULT_SIZE = 600


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


def encode_datamatrix(
    data: str, symbol_shape: str = DATAMATRIX_SHAPE_DEFAULT, *, gs1: bool = False
) -> Symbol2D:
    """Encode *data* as a Data Matrix ECC200 (ISO/IEC 16022).

    pyStrich's ``.matrix`` is only the data mapping region (the L-shaped finder pattern is added by
    its renderer), so the symbol is read back from ``get_rect_marks()`` with the encoder's own quiet
    zone disabled; the spec 1-module quiet zone is re-applied at draw time like every other symbol.

    ``gs1`` encodes a GS1 payload: a leading FNC1 marks the symbol as GS1, and every
    :data:`GS1_SEPARATOR` (ASCII 29) in *data* becomes the FNC1 that terminates a variable-length
    application identifier — so a template writes ``01{{gtin}}\\u001d10{{batch}}``. GS1 data is
    ASCII by definition; anything else is rejected rather than silently re-encoded.
    """
    from pystrich.datamatrix import FNC1, DataMatrixData, DataMatrixEncoder
    from pystrich.exceptions import PyStrichError

    if symbol_shape not in DATAMATRIX_SHAPE_CHOICES:
        raise SymbolEncodeError(
            f"datamatrix: symbol_shape must be one of {sorted(DATAMATRIX_SHAPE_CHOICES)}, "
            f"got {symbol_shape!r}"
        )
    try:
        if gs1:
            if not data.isascii():
                raise SymbolEncodeError("datamatrix: gs1 data must be ASCII")
            segments: list[object] = [FNC1]
            for i, part in enumerate(data.split(GS1_SEPARATOR)):
                if i:
                    segments.append(FNC1)
                if part:
                    segments.append(part)
            payload = DataMatrixData(*segments, encoding="ascii")  # type: ignore[arg-type]
        else:
            payload = DataMatrixData(data, auto_encoding=True)
        encoder = DataMatrixEncoder(
            payload,
            quiet_zone=0,
            symbol_shape=symbol_shape,  # type: ignore[arg-type]
        )
        marks = encoder.get_rect_marks()
    except PyStrichError as exc:
        raise SymbolEncodeError(
            f"datamatrix: cannot encode {len(data)} characters with symbol_shape {symbol_shape}: "
            f"{exc}"
        ) from exc
    return Symbol2D(
        marks=tuple(marks.marks),
        cols=marks.width,
        rows=marks.height,
        quiet=DATAMATRIX_QUIET_MODULES,
    )


def encode_aztec(
    data: str,
    *,
    ecc: int = AZTEC_ECC_DEFAULT,
    symbol_kind: str = AZTEC_KIND_DEFAULT,
    layers: int | None = None,
) -> Symbol2D:
    """Encode *data* as an Aztec Code (ISO/IEC 24778).

    ``ecc`` is the percentage of the symbol given to error correction, ``symbol_kind`` picks the
    compact (1-4 layers) or full (1-32 layers) family or lets the encoder choose, and ``layers``
    pins the symbol size — the encoder needs an explicit family for that, so the loader requires
    ``symbol_kind`` alongside ``layers``.
    """
    from pystrich.aztec import AztecEncoder
    from pystrich.exceptions import PyStrichError

    if symbol_kind not in AZTEC_KIND_CHOICES:
        raise SymbolEncodeError(
            f"aztec: symbol_kind must be one of {sorted(AZTEC_KIND_CHOICES)}, got {symbol_kind!r}"
        )
    try:
        encoder = AztecEncoder(
            data,
            ecc=ecc,
            symbol_kind=symbol_kind,  # type: ignore[arg-type]
            layers=layers,
            quiet_zone=0,
        )
    except PyStrichError as exc:
        raise SymbolEncodeError(f"aztec: cannot encode {len(data)} characters: {exc}") from exc
    return runs_from_matrix(encoder.matrix, AZTEC_QUIET_MODULES)


def encode_pdf417(
    data: str,
    *,
    columns: int | None = None,
    ecl: int | None = None,
    row_height: int = PDF417_ROW_HEIGHT_DEFAULT,
) -> Symbol2D:
    """Encode *data* as a PDF417 (ISO/IEC 15438).

    ``columns`` fixes the number of data columns (the encoder picks one otherwise), ``ecl`` the
    error-correction level 0-8 (the encoder scales it with the payload otherwise), and
    ``row_height`` how many modules tall each codeword row is drawn — the matrix pyStrich returns is
    already stretched by it, so a taller row is more rows of the same runs.
    """
    from pystrich.exceptions import PyStrichError
    from pystrich.pdf417 import PDF417Encoder

    try:
        encoder = PDF417Encoder(
            data,
            ecl=ecl,  # type: ignore[arg-type]
            columns=columns,
            quiet_zone=0,
            row_height=row_height,
        )
    except PyStrichError as exc:
        raise SymbolEncodeError(f"pdf417: cannot encode {len(data)} characters: {exc}") from exc
    return runs_from_matrix(encoder.matrix, PDF417_QUIET_MODULES)


# ── Linear (1D) barcodes ─────────────────────────────────────────────────────────

# ISO/IEC 15417 and friends ask for a quiet zone of at least 10 narrow modules on each side of a
# linear symbol; it is what lets a scanner find the first and last bar.
BARCODE_QUIET_MODULES = 10
# ITF-14 (GS1 carton codes) is Interleaved 2 of 5 inside a bearer bar frame; ISO/IEC 16390 puts
# the frame at 4.8 mm minimum, which at typical module widths is about 4 narrow modules — the
# default pyStrich also uses.
ITF14_SYMBOLOGY = "itf14"
ITF14_BEARER_MODULES = 4
# The 1D symbologies a template may name: python-barcode's registry (code128, ean13, upca, code39,
# itf, codabar, gs1_128, ...) plus ITF-14 from pyStrich, which python-barcode lacks (its `itf` is
# plain Interleaved 2 of 5 with no bearer bars). One set drives the renderer, the loader's error
# message, the schema document and the studio's drift test, so they cannot disagree.
SUPPORTED_SYMBOLOGIES: frozenset[str] = frozenset(python_barcode.PROVIDED_BARCODES) | {
    ITF14_SYMBOLOGY
}


@dataclass(frozen=True)
class Bars1D:
    """A linear barcode as a module string plus the frame its spec puts around it."""

    pattern: (
        str  # one char per narrow module: '1'/'G' dark (G = guard bar, drawn the same), '0' light
    )
    quiet: int  # light modules on each side
    bearer: int  # bearer-bar thickness in modules (top/bottom and both ends); 0 = none
    text: str  # the human-readable value (with any computed check digit) for `show_value`

    @property
    def units_wide(self) -> int:
        return len(self.pattern) + 2 * (self.quiet + self.bearer)


def encode_1d(symbology: str, data: str) -> Bars1D:
    """Encode *data* in a linear *symbology* as its module string.

    python-barcode's ``build()`` yields exactly one module string per symbol and validates the
    payload for the fixed-format codes (EAN/UPC digits and length, ISBN prefixes, ...); its errors
    become :class:`SymbolEncodeError` so a bad value is a clear "Render error", as before.
    """
    from barcode.errors import BarcodeError

    if symbology not in SUPPORTED_SYMBOLOGIES:
        raise SymbolEncodeError(
            f"barcode: unknown symbology {symbology!r}; valid: {sorted(SUPPORTED_SYMBOLOGIES)}"
        )
    if symbology == ITF14_SYMBOLOGY:
        return _encode_itf14(data)
    try:
        code = python_barcode.get_barcode_class(symbology)(data)
        pattern = code.build()[0]
        text = code.get_fullcode()
    except BarcodeError as exc:
        raise SymbolEncodeError(f"barcode: cannot encode {data!r} as {symbology}: {exc}") from exc
    return Bars1D(pattern=pattern, quiet=BARCODE_QUIET_MODULES, bearer=0, text=str(text))


def _encode_itf14(data: str) -> Bars1D:
    """ITF-14 via pyStrich: 13 digits (the check digit is computed) or 14 (it is verified)."""
    from pystrich.exceptions import PyStrichError
    from pystrich.itf import ITF14Encoder

    try:
        encoder = ITF14Encoder(data)
    except PyStrichError as exc:
        raise SymbolEncodeError(
            f"barcode: cannot encode {data!r} as itf14 (needs 13 or 14 digits): {exc}"
        ) from exc
    return Bars1D(
        pattern=encoder.bars,
        quiet=BARCODE_QUIET_MODULES,
        bearer=ITF14_BEARER_MODULES,
        text=encoder.full_code,
    )


def draw_bars(bars: Bars1D, module_px: int, bar_height_px: int) -> Image.Image:
    """Draw *bars* with every module exactly ``module_px`` dots wide and ``bar_height_px`` tall.

    Quiet zones are part of the image; bearer bars (ITF-14) are drawn ``bearer`` modules thick
    above and below the bars and at both ends, outside the quiet zones per ISO/IEC 16390. Output is
    a binary ``"L"`` image, like :func:`draw_marks`.
    """
    if module_px < 1:
        raise ValueError(f"module_px must be at least one dot, got {module_px}")
    if bar_height_px < 1:
        raise ValueError(f"bar_height_px must be at least one dot, got {bar_height_px}")
    bearer_px = bars.bearer * module_px
    width = bars.units_wide * module_px
    height = bar_height_px + 2 * bearer_px
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    left = (bars.bearer + bars.quiet) * module_px
    x = 0
    while x < len(bars.pattern):
        if bars.pattern[x] == "0":
            x += 1
            continue
        start = x
        while x < len(bars.pattern) and bars.pattern[x] != "0":
            x += 1
        x0 = left + start * module_px
        draw.rectangle(
            (x0, bearer_px, left + x * module_px - 1, bearer_px + bar_height_px - 1), fill=0
        )
    if bearer_px:
        draw.rectangle((0, 0, width - 1, bearer_px - 1), fill=0)
        draw.rectangle((0, height - bearer_px, width - 1, height - 1), fill=0)
        draw.rectangle((0, 0, bearer_px - 1, height - 1), fill=0)
        draw.rectangle((width - bearer_px, 0, width - 1, height - 1), fill=0)
    return img
