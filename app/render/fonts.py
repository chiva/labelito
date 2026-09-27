# SPDX-License-Identifier: GPL-3.0-or-later
"""The selectable label font families, read from the pinned manifest.

``font_manifest.json`` is the single source of truth: ``scripts/fetch_label_fonts.py`` downloads and
SHA-256-verifies every file it lists into ``label_fonts_dir`` (baked into the Docker image at build
time, never fetched at runtime), and this module exposes the same entries to the renderer, the
loader, the MCP schema and the studio. Each fetched family lives in ``<label_fonts_dir>/<key>/`` with
its font files and its ``LICENSE.txt``. The default family, DejaVu Sans, is ``builtin``: it keeps the
existing resolution in :func:`app.render.elements._load_font` so the default output never changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = Path(__file__).with_name("font_manifest.json")
LICENSE_FILE_NAME = "LICENSE.txt"
STYLE_REGULAR = "regular"
STYLE_BOLD = "bold"


@dataclass(frozen=True)
class FontStyle:
    """One style of a family: its file name, and the ``wght`` value to set when it is variable."""

    file: str
    wght: int | None = None


@dataclass(frozen=True)
class LabelFont:
    key: str
    name: str
    category: str
    license: str
    builtin: bool
    regular: FontStyle | None
    bold: FontStyle | None
    # Text the studio shows in this font next to its name, for faces that cannot legibly spell
    # their own name (seven/fourteen-segment displays).
    preview_sample: str | None = None

    @property
    def has_bold(self) -> bool:
        return self.builtin or self.bold is not None

    def style(self, bold: bool) -> FontStyle | None:
        """The style to draw with: bold when asked and available, otherwise regular."""
        return self.bold if bold and self.bold is not None else self.regular

    def path(self, label_fonts_dir: Path, bold: bool) -> Path | None:
        """Where the fetched file for *bold*/regular lives, or None for the builtin family."""
        chosen = self.style(bold)
        return None if chosen is None else label_fonts_dir / self.key / chosen.file

    def license_path(self, label_fonts_dir: Path) -> Path:
        return label_fonts_dir / self.key / LICENSE_FILE_NAME


def _style(raw: dict[str, object] | None) -> FontStyle | None:
    if not raw or "file" not in raw:
        return None
    wght = raw.get("wght")
    return FontStyle(file=str(raw["file"]), wght=int(wght) if isinstance(wght, int) else None)


def _load_manifest() -> tuple[str, tuple[str, ...], dict[str, LabelFont]]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    registry: dict[str, LabelFont] = {}
    for family in raw["families"]:
        styles = family.get("styles", {})
        registry[family["key"]] = LabelFont(
            key=family["key"],
            name=family["name"],
            category=family["category"],
            license=family["license"],
            builtin=bool(family.get("builtin", False)),
            regular=_style(styles.get(STYLE_REGULAR)),
            bold=_style(styles.get(STYLE_BOLD)),
            preview_sample=family.get("preview_sample"),
        )
    return raw["default"], tuple(raw["categories"]), registry


DEFAULT_FONT, FONT_CATEGORIES, FONT_REGISTRY = _load_manifest()
FONT_KEYS = frozenset(FONT_REGISTRY)
# Cache-buster for font URLs served to the browser: the manifest pins every file's SHA-256, so its
# content hash changes exactly when any served font does.
MANIFEST_VERSION = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()[:10]
