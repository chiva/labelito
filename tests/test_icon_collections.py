# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify the bundled icon collections produced by ``scripts/fetch-icons.sh``.

Unlike the rest of the suite — which renders against a *synthetic* icon tree built in
``tests/conftest.py`` — these tests exercise the REAL collections normalized into
``settings.icon_collections_dir`` by the fetch script (and by the Docker ``icons`` build stage).
They confirm two things a dependency bump (e.g. FontAwesome v6 → v7) could silently break:

1. the copy produced non-empty, licensed directories for every collection we bundle, and
2. every icon referenced by the templates in ``templates/`` — plus a representative glyph per
   collection — resolves to a real file and actually loads/rasterizes through the production
   :class:`~app.render.elements.IconElement` code path.

The collections are a build artifact, absent from a plain checkout, so the whole module is marked
``icons`` and skips with a clear hint when they haven't been fetched. CI runs ``fetch-icons.sh``
first in a dedicated job, making this a real gate rather than a silently-skipped test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.config import settings
from app.loader import TemplateRegistry
from app.render.elements import (
    FA_STYLES,
    KNOWN_COLLECTIONS,
    IconElement,
    build_element,
)

pytestmark = pytest.mark.icons

TEMPLATES_DIR = Path("templates")

# The on-disk layout scripts/fetch-icons.sh produces: FontAwesome keeps its style subdirectories,
# Material and Octicons are flattened (style is None ⇒ no subdirectory).
_COLLECTION_DIRS: tuple[tuple[str, str | None], ...] = (
    ("fontawesome", "solid"),
    ("fontawesome", "regular"),
    ("fontawesome", "brands"),
    ("material", None),
    ("octicons", None),
)

# The exact license filename the script copies into each collection root.
_LICENSE_FILENAME: dict[str, str] = {
    "fontawesome": "LICENSE.txt",
    "material": "LICENSE",
    "octicons": "LICENSE",
}

# A partial or failed copy leaves a directory near-empty; every collection ships far more than this.
# Kept low so ordinary upstream glyph churn can never turn this into a false failure.
_MIN_GLYPHS = 10

# Size (px) used when loading a glyph to confirm it rasterizes; arbitrary but must round-trip exactly.
_SMOKE_SIZE = 64


def _collection_dir(collections_dir: Path, collection: str, style: str | None) -> Path:
    """Path to a collection's glyph directory, honouring FontAwesome's style subdirectory."""
    base = collections_dir / collection
    return base / style if style else base


def _glyph_files(directory: Path) -> list[Path]:
    """The ``*.svg`` glyphs in *directory*, sorted for determinism (excludes the LICENSE file)."""
    return sorted(directory.glob("*.svg"))


def _iter_icon_specs(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``icon`` element in a template layout, recursing into ``row`` children."""
    icons: list[dict[str, Any]] = []

    def scan(element: dict[str, Any]) -> None:
        if element.get("type") == "icon":
            icons.append(element)
        children = element.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    scan(child)

    for element in layout:
        if isinstance(element, dict):
            scan(element)
    return icons


def _discover_template_icons() -> list[tuple[str, dict[str, Any]]]:
    """(template name, icon spec) for every icon referenced across ``templates/``."""
    registry = TemplateRegistry(TEMPLATES_DIR)
    registry.load_all()
    return [
        (template.name, spec)
        for template in registry.all()
        for spec in _iter_icon_specs(template.layout)
    ]


# Discovered at import so each icon becomes its own parametrized case with a readable id. Independent
# of the fetched collections (templates always exist), so it is safe to evaluate at collection time.
_TEMPLATE_ICONS = _discover_template_icons()


def _icon_id(item: tuple[str, dict[str, Any]]) -> str:
    template_name, spec = item
    source = spec.get("collection") or "asset"
    return f"{template_name}:{source}/{spec.get('name')}"


@pytest.fixture(scope="module")
def collections_dir() -> Path:
    """The real fetched icon-collections directory, or skip if it hasn't been built."""
    directory = settings.icon_collections_dir
    if not directory.exists():
        pytest.skip(
            f"icon collections not fetched at {directory}; run scripts/fetch-icons.sh first"
        )
    return directory


@pytest.mark.parametrize("collection, style", _COLLECTION_DIRS)
def test_collection_directory_populated(
    collections_dir: Path, collection: str, style: str | None
) -> None:
    """Each bundled collection directory exists and holds a plausible number of glyphs."""
    directory = _collection_dir(collections_dir, collection, style)
    assert directory.is_dir(), f"missing collection directory: {directory}"
    glyphs = _glyph_files(directory)
    assert len(glyphs) >= _MIN_GLYPHS, (
        f"{directory} has only {len(glyphs)} svg(s) (< {_MIN_GLYPHS}); "
        "fetch-icons.sh likely produced a partial copy"
    )


@pytest.mark.parametrize("collection", sorted(_LICENSE_FILENAME))
def test_collection_license_copied(collections_dir: Path, collection: str) -> None:
    """The upstream LICENSE the script copies is present at each collection root (GPL compliance)."""
    license_path = collections_dir / collection / _LICENSE_FILENAME[collection]
    assert license_path.is_file(), f"missing license: {license_path}"
    assert license_path.stat().st_size > 0, f"empty license: {license_path}"


def test_octicons_size_suffix_normalized(collections_dir: Path) -> None:
    """Octicons are normalized to ``<name>.svg`` — no ``-24``/``-16`` size suffix survives."""
    octicons = collections_dir / "octicons"
    leftover = sorted(octicons.glob("*-24.svg")) + sorted(octicons.glob("*-16.svg"))
    assert not leftover, f"un-normalized octicons still carry a size suffix: {leftover}"


@pytest.mark.parametrize("collection, style", _COLLECTION_DIRS)
def test_collection_sample_glyph_renders(
    collections_dir: Path, collection: str, style: str | None
) -> None:
    """A representative glyph from each collection rasterizes through the real load path."""
    directory = _collection_dir(collections_dir, collection, style)
    glyphs = _glyph_files(directory)
    assert glyphs, f"no glyphs to sample in {directory}"

    spec: dict[str, Any] = {
        "type": "icon",
        "name": glyphs[0].stem,
        "collection": collection,
        "size": _SMOKE_SIZE,
    }
    if style:
        spec["style"] = style
    element = build_element(spec)
    assert isinstance(element, IconElement)

    image = element._load_icon(element.name, settings.icons_dir, collections_dir)
    assert image is not None, f"failed to load {glyphs[0]}"
    expected = element._px(element.size)
    assert image.size == (expected, expected)


def test_templates_reference_icons() -> None:
    """Guard against a template refactor silently dropping all icon coverage from this suite."""
    assert _TEMPLATE_ICONS, "no icons discovered in templates/ — icon coverage would be empty"


def test_all_referenced_collections_are_known() -> None:
    """Every collection a template references is one we actually bundle (no typos / stale names)."""
    referenced = {spec["collection"] for _, spec in _TEMPLATE_ICONS if spec.get("collection")}
    unknown = referenced - KNOWN_COLLECTIONS
    assert not unknown, f"templates reference unknown collections: {sorted(unknown)}"


@pytest.mark.parametrize("template_name, spec", _TEMPLATE_ICONS, ids=map(_icon_id, _TEMPLATE_ICONS))
def test_template_icon_resolves_and_loads(
    collections_dir: Path, template_name: str, spec: dict[str, Any]
) -> None:
    """Every icon a template uses resolves to a real file and loads via the production path.

    Covers bundled-collection icons (FontAwesome ``mug-hot``/``check``) and custom assets
    (``snowflake`` PNG from ``settings.icons_dir``) alike, exactly as :class:`IconElement` would at
    render time — including the FontAwesome default-style fallback and the svg→png asset probe.
    """
    element = build_element(spec)
    assert isinstance(element, IconElement)

    # FA_STYLES is imported so a future style addition is a single-source change here too.
    if element.collection == "fontawesome":
        assert element.style in FA_STYLES

    path = element._resolve_path(element.name, settings.icons_dir, collections_dir)
    assert path is not None, f"{template_name}: {element.name!r} did not resolve to a path"
    assert path.exists(), f"{template_name}: resolved path missing on disk: {path}"

    image = element._load_icon(element.name, settings.icons_dir, collections_dir)
    assert isinstance(image, Image.Image), f"{template_name}: {element.name!r} failed to load"
    expected = element._px(element.size)
    assert image.size == (expected, expected)
