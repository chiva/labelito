# SPDX-License-Identifier: GPL-3.0-or-later
"""The template studio's hand-maintained vocabularies must match the renderer's.

`builder.js` cannot import Python, so its select lists are literals. A value the studio offers that
the loader rejects makes the studio produce templates it cannot save (the old list carried
`code93`, which python-barcode does not provide); a value the loader accepts that the studio omits is
merely a missing option. Both directions are pinned here, by reading the JS source.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.render.elements import ELEMENT_REGISTRY
from app.render.symbols import (
    AZTEC_KIND_CHOICES,
    DATAMATRIX_SHAPE_CHOICES,
    QR_ECL_CHOICES,
    SUPPORTED_SYMBOLOGIES,
)

BUILDER_JS = Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "js" / "builder.js"


def _js_string_list(name: str) -> set[str]:
    source = BUILDER_JS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = \[(.*?)\];", source, re.DOTALL)
    assert match is not None, f"{name} not found in builder.js"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_builder_symbologies_match_the_renderer() -> None:
    assert _js_string_list("SYMBOLOGIES") == SUPPORTED_SYMBOLOGIES


def test_builder_qr_levels_match_the_renderer() -> None:
    assert _js_string_list("QR_ECL") == QR_ECL_CHOICES


def test_builder_datamatrix_shapes_and_aztec_kinds_match_the_renderer() -> None:
    assert _js_string_list("DM_SHAPES") == DATAMATRIX_SHAPE_CHOICES
    assert _js_string_list("AZTEC_KINDS") == AZTEC_KIND_CHOICES


def test_builder_palette_offers_every_registered_element() -> None:
    assert _js_string_list("PALETTE") == set(ELEMENT_REGISTRY)
