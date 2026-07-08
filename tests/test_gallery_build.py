# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the label-gallery build (scripts/build_gallery.py).

The gallery previews are generated at deploy time, so the build must stay honest: one valid preview
per shipped template, a manifest whose records match the templates, curated sample data that never
rots, and byte-reproducible output (the render is pinned to a fixed date/language).
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app import main as app_main

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "build_gallery.py"


def _load_build_module():
    """Import scripts/build_gallery.py as a module (it lives outside the app package)."""
    spec = importlib.util.spec_from_file_location("build_gallery", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_mod():
    return _load_build_module()


@pytest.fixture(scope="module")
def registered_names() -> set[str]:
    return set(app_main.registry.load_all())


@pytest.fixture(scope="module")
def _gallery_build(build_mod, tmp_path_factory) -> tuple[list[dict], Path]:
    """Build the gallery once per module — the consuming tests only read it, never mutate.

    ``strict_icons=False``: this environment has not run ``scripts/fetch-icons.sh`` (the ``icons``
    CI job fetches them; the plain unit job does not), so the shipped collection-icon templates
    legitimately render blank here. The strict default — what the CI Pages deploy runs — has its
    own dedicated tests below against an isolated registry.
    """
    out_dir = tmp_path_factory.mktemp("gallery")
    return build_mod.build(out_dir, strict_icons=False), out_dir


@pytest.fixture(scope="module")
def manifest(_gallery_build) -> list[dict]:
    return _gallery_build[0]


@pytest.fixture(scope="module")
def manifest_out(_gallery_build) -> Path:
    return _gallery_build[1]


def test_one_preview_png_per_registered_template(
    build_mod, manifest_out: Path, manifest: list[dict], registered_names: set[str]
) -> None:
    samples_dir = manifest_out / build_mod.SAMPLES_SUBDIR
    png_stems = {p.stem for p in samples_dir.glob("*.png")}
    manifest_names = {e["name"] for e in manifest}

    assert manifest_names == registered_names, (
        "manifest must cover exactly the registered templates"
    )
    assert png_stems == registered_names, "one PNG per template, no orphans, none missing"


def test_manifest_written_and_matches_return(
    build_mod, manifest_out: Path, manifest: list[dict]
) -> None:
    manifest_path = manifest_out / build_mod.SAMPLES_SUBDIR / build_mod.MANIFEST_NAME
    assert manifest_path.is_file()
    on_disk = json.loads(manifest_path.read_text())
    assert on_disk == manifest


def test_every_preview_is_a_valid_png_at_label_width(
    build_mod, manifest_out: Path, manifest: list[dict]
) -> None:
    samples_dir = manifest_out / build_mod.SAMPLES_SUBDIR
    for entry in manifest:
        png_path = samples_dir / f"{entry['name']}.png"
        img = Image.open(io.BytesIO(png_path.read_bytes()))
        img.load()
        assert img.width > 0 and img.height > 0
        # rotate: 0 templates render at the label's printable width; a rotated one swaps the axes,
        # so only assert the width match when no rotation is applied.
        tmpl = app_main.registry.get(entry["name"])
        if tmpl is not None and tmpl.rotate == 0:
            assert img.width == entry["width_px"]


def test_manifest_entries_have_the_fields_gallery_needs(manifest: list[dict]) -> None:
    required_keys = {
        "name",
        "description",
        "label",
        "size",
        "required",
        "optional",
        "fields",
        "image",
        "width_px",
        "curl",
    }
    for entry in manifest:
        assert required_keys <= entry.keys(), f"{entry.get('name')} missing keys"
        assert entry["image"].endswith(f"{entry['name']}.png")
        assert entry["curl"].startswith("curl -X POST")
        assert entry["name"] in entry["curl"]


def test_all_declared_fields_are_populated_in_the_example(manifest: list[dict]) -> None:
    """Each preview exercises the full layout: every required + optional field has a value."""
    for entry in manifest:
        for field in [*entry["required"], *entry["optional"]]:
            assert field in entry["fields"], f"{entry['name']}: field {field!r} not populated"


def test_no_stale_sample_entries(build_mod, registered_names: set[str]) -> None:
    """Every curated SAMPLES key maps to a real template — a guard against the dict rotting."""
    stale = set(build_mod.SAMPLES) - registered_names
    assert not stale, f"SAMPLES has entries for non-existent templates: {sorted(stale)}"


def test_image_template_gets_a_base64_payload_but_redacts_it_in_output(
    build_mod, manifest: list[dict]
) -> None:
    image_entry = next((e for e in manifest if e["name"] == "image"), None)
    if image_entry is None:  # the image template is optional to the suite's fixtures
        pytest.skip("no 'image' template registered")
    # The manifest (public) must not carry the bulky base64 blob, and the curl must show a placeholder.
    assert image_entry["fields"].get("image") == "<base64-png>"
    assert "<base64-png>" in image_entry["curl"]


def test_build_is_byte_reproducible(build_mod, tmp_path: Path) -> None:
    """A fixed render date + language means two builds produce identical bytes (no CI churn).

    Non-strict for the same reason as the module fixture: the icon collections are not fetched in
    this environment, and reproducibility is what is under test here.
    """
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    build_mod.build(out_a, strict_icons=False)
    build_mod.build(out_b, strict_icons=False)
    samples = build_mod.SAMPLES_SUBDIR
    for png in (out_a / samples).glob("*.png"):
        twin = out_b / samples / png.name
        assert twin.read_bytes() == png.read_bytes(), f"{png.name} differs between builds"
    assert (out_a / samples / build_mod.MANIFEST_NAME).read_bytes() == (
        out_b / samples / build_mod.MANIFEST_NAME
    ).read_bytes()


# ── Strict icon check: a blank showcase icon must FAIL the build, not warn ─────────
# The runtime render path degrades a missing icon to a blank strip + warning (correct at print
# time), and collection icons are excluded from the boot scan — so before the strict check, a
# template whose icon never resolved deployed a silently broken gallery image. These tests drive
# build() against an isolated registry/engine (mirroring the `client` fixture in conftest.py) so
# the icon's presence is fully controlled.

_ICON_TEMPLATE = """\
name: {name}
description: gallery strict-icon fixture
label: "62"
layout:
  - {{type: icon, name: {icon}}}
  - {{type: text, text: static}}
"""


def _isolate_gallery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    template_yaml: str,
    icon_files: dict[str, bytes],
) -> None:
    """Point app.main's registry + engine at temp template/icon dirs for one build() call.

    The loader accepts a custom-asset icon name without checking a file exists (the file is a
    deploy-time asset, not template schema), so a template naming a ghost icon loads fine — the
    blank strip only materializes at render time, which is exactly the seam build() must police.
    """
    from app.loader import TemplateRegistry
    from app.render.engine import RenderEngine

    templates_d = tmp_path / "templates"
    templates_d.mkdir()
    (templates_d / "fixture.yaml").write_text(template_yaml)
    icons_d = tmp_path / "icons"
    icons_d.mkdir()
    for filename, payload in icon_files.items():
        (icons_d / filename).write_bytes(payload)
    fonts_d = tmp_path / "fonts"
    fonts_d.mkdir()
    collections_d = tmp_path / "icon-collections"
    collections_d.mkdir()

    monkeypatch.setattr(app_main, "registry", TemplateRegistry(templates_d))
    monkeypatch.setattr(
        app_main,
        "engine",
        RenderEngine(
            fonts_dir=fonts_d,
            icons_dir=icons_d,
            icon_collections_dir=collections_d,
            translator=app_main.translator,
        ),
    )


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("L", (90, 90), 255).save(buf, format="PNG")
    return buf.getvalue()


def test_strict_build_fails_when_a_template_icon_is_missing(
    build_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A template whose icon resolves to no file must FAIL the strict (default) build, naming the
    template and the icon — never deploy a showcase image with a silently blank strip."""
    _isolate_gallery(
        monkeypatch,
        tmp_path,
        _ICON_TEMPLATE.format(name="ghost-icon", icon="ghost-glyph"),
        icon_files={},
    )
    with pytest.raises(build_mod.GalleryBuildError, match="blank icon") as excinfo:
        build_mod.build(tmp_path / "out")
    message = str(excinfo.value)
    assert "ghost-icon" in message, "the failing template must be named"
    assert "ghost-glyph" in message, "the unresolvable icon must be named"
    assert "fetch-icons" in message, "the failure must point at the fix"


def test_strict_build_fails_before_writing_the_manifest(
    build_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A strict failure must not leave a manifest behind that references blank showcase images."""
    _isolate_gallery(
        monkeypatch,
        tmp_path,
        _ICON_TEMPLATE.format(name="ghost-icon", icon="ghost-glyph"),
        icon_files={},
    )
    out_dir = tmp_path / "out"
    with pytest.raises(build_mod.GalleryBuildError, match="blank icon"):
        build_mod.build(out_dir)
    manifest_path = out_dir / build_mod.SAMPLES_SUBDIR / build_mod.MANIFEST_NAME
    assert not manifest_path.exists(), "a failed strict build must not write manifest.json"


def test_strict_build_passes_when_the_icon_resolves(
    build_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The strict default is not trigger-happy: a template whose icon file exists builds cleanly."""
    _isolate_gallery(
        monkeypatch,
        tmp_path,
        _ICON_TEMPLATE.format(name="icon-ok", icon="snowflake"),
        icon_files={"snowflake.png": _png_bytes()},
    )
    out_dir = tmp_path / "out"
    entries = build_mod.build(out_dir)
    assert [e["name"] for e in entries] == ["icon-ok"]
    assert (out_dir / build_mod.SAMPLES_SUBDIR / "icon-ok.png").is_file()


def test_strict_build_catches_blank_icons_even_when_logging_is_quieter(
    build_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The strict check must not depend on the ambient logging config: with the elements logger
    effectively above WARNING (e.g. LOG_LEVEL=ERROR reaching root via app.main's basicConfig),
    ``Logger.warning`` drops the record before any handler sees it — build() pins the logger to
    WARNING for the capture window and restores it afterward."""
    import logging

    _isolate_gallery(
        monkeypatch,
        tmp_path,
        _ICON_TEMPLATE.format(name="ghost-icon", icon="ghost-glyph"),
        icon_files={},
    )
    icon_logger = logging.getLogger("app.render.elements")
    previous = icon_logger.level
    icon_logger.setLevel(logging.ERROR)
    try:
        with pytest.raises(build_mod.GalleryBuildError, match="blank icon"):
            build_mod.build(tmp_path / "out")
        assert icon_logger.level == logging.ERROR, "build() must restore the pre-build level"
    finally:
        icon_logger.setLevel(previous)


def test_non_strict_build_keeps_the_graceful_blank_icon_behavior(
    build_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """strict_icons=False preserves the pre-strict behavior (blank strip, build succeeds) for
    environments where the icon collections are legitimately absent."""
    _isolate_gallery(
        monkeypatch,
        tmp_path,
        _ICON_TEMPLATE.format(name="ghost-icon", icon="ghost-glyph"),
        icon_files={},
    )
    out_dir = tmp_path / "out"
    entries = build_mod.build(out_dir, strict_icons=False)
    assert [e["name"] for e in entries] == ["ghost-icon"]
    assert (out_dir / build_mod.SAMPLES_SUBDIR / build_mod.MANIFEST_NAME).is_file()
