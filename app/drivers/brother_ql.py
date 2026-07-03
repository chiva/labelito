# SPDX-License-Identifier: GPL-3.0-or-later
"""Brother QL printer driver.

Capabilities are derived directly from the imported ``brother_ql_next`` registries —
``ALL_MODELS`` for auto-cut support and ``ALL_LABELS`` for label identifiers,
geometry, and per-model restrictions. Nothing is hand-maintained, so the driver can
never disagree with the library that actually rasterizes the labels (which is exactly
how the old hard-coded table drifted into the QL-800 and ``38x90`` bugs).
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from typing import Any

from brother_ql.labels import ALL_LABELS, Color, FormFactor
from brother_ql.models import ALL_MODELS
from PIL import Image

from app.drivers.base import Capability, register_driver
from app.models import LabelGeometry

# Standard rasterisation DPI (always used for the print-head axis geometry lookup).
_DPI = 300
# High-resolution DPI used when high_res=True; the driver passes dpi_600=True to convert().
_DPI_HIGH = 600

_MODELS = {m.identifier: m for m in ALL_MODELS}
_LABELS = {lbl.identifier: lbl for lbl in ALL_LABELS}

# Models whose Brother raster command reference documents the "high resolution printing" bit
# (expanded-mode 600x300 dpi, brother_ql's convert(dpi_600=True)). Unlike two_color/cutting, the
# installed brother_ql/brother_ql_next library carries NO such flag on ALL_MODELS, so this cannot be
# derived from the library the way the rest of this driver is — it is hand-curated from Brother's
# per-series Raster Command Reference PDFs (Expanded mode / ESC i K, bit 7):
#   - QL-570/580N/700 doc explicitly scopes the bit to "(QL-570/580N/700)" among that whole series.
#   - QL-710W/720NW doc documents the identical bit with no exclusion.
#   - QL-800/810W/820NWB doc documents it with no exclusion.
#   - QL-500/550/560/650TD/1050/1060N (same combined doc as QL-570/580N/700) are excluded by that
#     same scoping note.
#   - QL-1100/1110NWB/1115NWB's own resolution table (§2.3.1) lists only "300 dpi high, 300 dpi
#     wide" — no 600 dpi row — so the whole series is excluded.
#   - QL-600 is undocumented at the command-bit level in any Brother reference found; excluded
#     conservatively rather than guessed from third-party retailer spec pages.
_HIGH_RES_MODELS = frozenset(
    {"QL-570", "QL-580N", "QL-700", "QL-710W", "QL-720NW", "QL-800", "QL-810W", "QL-820NWB"}
)


def _is_red_label(label: Any) -> bool:
    """True when a brother_ql label is black/red two-color media (Color.BLACK_RED_WHITE).

    The library tags DK-22251-class media with ``color == Color.BLACK_RED_WHITE`` (value 1); plain
    black/white media is ``Color.BLACK_WHITE`` (0). A red print needs one of these loaded.
    """
    return bool(getattr(label, "color", None) == Color.BLACK_RED_WHITE)


def _geometry(label: Any) -> LabelGeometry:
    """Map a brother_ql label to our LabelGeometry (dots @ 300 dpi)."""
    width_px, height_px = label.dots_printable
    continuous = label.form_factor == FormFactor.ENDLESS
    return LabelGeometry(
        width_px=width_px,
        height_px=None if continuous else height_px,
        media_type="continuous" if continuous else "die_cut",
    )


def _capability_for(model_id: str) -> Capability:
    """Build a Capability for a model straight from the brother_ql registries.

    ``two_color`` and ``red_labels`` are read from the library too (``model.two_color`` and each
    label's ``color``), so two-color capability can never drift from what convert(red=True) actually
    accepts — exactly the no-hand-maintained-table principle the rest of this driver follows.
    """
    model = _MODELS[model_id]
    supported = {
        identifier: label
        for identifier, label in _LABELS.items()
        if not label.restricted_to_models or model_id in label.restricted_to_models
    }
    geometries = {identifier: _geometry(label) for identifier, label in supported.items()}
    red_labels = [identifier for identifier, label in supported.items() if _is_red_label(label)]
    return Capability(
        name="brother_ql",
        dpi=_DPI,
        cut=bool(model.cutting),
        two_color=bool(getattr(model, "two_color", False)),
        high_res=model_id in _HIGH_RES_MODELS,
        supported_labels=list(geometries),
        red_labels=red_labels,
        label_geometries=geometries,
    )


@register_driver("brother_ql")
class BrotherQLDriver:
    """Driver for Brother QL-series label printers using the brother_ql library."""

    _model: str = "QL-810W"
    CAPABILITY: Capability = _capability_for("QL-810W")

    @classmethod
    def for_model(cls, model: str) -> type[BrotherQLDriver]:
        """Return a driver subclass configured for the given model."""
        if model not in _MODELS:
            raise ValueError(f"Unknown Brother QL model {model!r}. Supported: {sorted(_MODELS)}")
        capability = _capability_for(model)

        class _Configured(BrotherQLDriver):
            _model = model
            CAPABILITY = capability

        _Configured.__name__ = f"BrotherQLDriver[{model}]"
        return _Configured

    def render_payload(self, png: bytes, opts: dict[str, Any]) -> bytes:
        """Convert a single PNG to QL raster bytes via brother_ql.

        ``png`` is one rendered label.  It is opened once and multiplied by ``copies`` so that
        identical labels print back-to-back; the ``copies`` path uses this with ``copies > 1``.

        Sequence batches do NOT pass through here multiplied: the print path renders and sends
        each sequence label individually (``copies=1`` per call) so every label gets its own
        per-label status confirmation within the printer's status-read window. Each label is one
        independent ``convert`` → ``transport.send``, so a single decoded RGB image is resident at a
        time and a mid-batch printer error is caught right after the offending label rather than
        being masked by a single long batch send. See ``app.main._execute_print``.

        When ``high_res`` is True the caller must supply a PNG whose width is already doubled
        (2x dots_printable[0]) so that convert(dpi_600=True) receives the expected 2x input
        and internally halves the width to pack print-head dots at 600 dpi. See engine.py for
        the scaling contract.
        """
        from brother_ql.conversion import convert
        from brother_ql.raster import BrotherQLRaster

        model = opts.get("model", self._model)
        label = opts.get("label", "62")
        rotate = str(opts.get("rotate", 0))
        cut = bool(opts.get("cut", True))
        copies = int(opts.get("copies", 1))
        dither = bool(opts.get("dither", False))
        threshold = float(opts.get("threshold", 70.0))
        high_res = bool(opts.get("high_res", False))
        dpi_600 = high_res  # named alias; True ↔ _DPI_HIGH, False ↔ _DPI
        # Two-color (red/black) printing. When True the engine has rendered an RGB image with
        # pure-red + black content; brother_ql's convert(red=True) separates the red and black layers
        # for two-color media. convert() raises BrotherQLUnsupportedCmd when the model lacks two-color
        # support (qlr.two_color_support False); the print path maps that to a clean 4xx.
        red = bool(opts.get("red", False))

        # Open once and multiply for identical back-to-back labels (copies path; copies=1 for a
        # single sequence label). RGB is the mode convert() wants for two-color (it derives the red
        # layer from the RGB channels) and is harmless for monochrome (it converts to "L" internally).
        img = Image.open(io.BytesIO(png)).convert("RGB")
        images: Iterable[Image.Image] = [img] * copies

        qlr = BrotherQLRaster(model)
        qlr.exception_on_warning = True

        instructions = convert(
            qlr=qlr,
            images=images,
            label=label,
            rotate=rotate,
            threshold=threshold,
            dither=dither,
            compress=False,
            red=red,
            dpi_600=dpi_600,
            cut=cut,
        )
        return instructions  # type: ignore[no-any-return]
