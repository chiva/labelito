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


@pytest.fixture
def manifest(build_mod, tmp_path: Path) -> list[dict]:
    return build_mod.build(tmp_path)


def test_one_preview_png_per_registered_template(
    build_mod, tmp_path: Path, manifest: list[dict], registered_names: set[str]
) -> None:
    samples_dir = tmp_path / build_mod.SAMPLES_SUBDIR
    png_stems = {p.stem for p in samples_dir.glob("*.png")}
    manifest_names = {e["name"] for e in manifest}

    assert manifest_names == registered_names, (
        "manifest must cover exactly the registered templates"
    )
    assert png_stems == registered_names, "one PNG per template, no orphans, none missing"


def test_manifest_written_and_matches_return(
    build_mod, tmp_path: Path, manifest: list[dict]
) -> None:
    manifest_path = tmp_path / build_mod.SAMPLES_SUBDIR / build_mod.MANIFEST_NAME
    assert manifest_path.is_file()
    on_disk = json.loads(manifest_path.read_text())
    assert on_disk == manifest


def test_every_preview_is_a_valid_png_at_label_width(
    build_mod, tmp_path: Path, manifest: list[dict]
) -> None:
    samples_dir = tmp_path / build_mod.SAMPLES_SUBDIR
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
    """A fixed render date + language means two builds produce identical bytes (no CI churn)."""
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    build_mod.build(out_a)
    build_mod.build(out_b)
    samples = build_mod.SAMPLES_SUBDIR
    for png in (out_a / samples).glob("*.png"):
        twin = out_b / samples / png.name
        assert twin.read_bytes() == png.read_bytes(), f"{png.name} differs between builds"
    assert (out_a / samples / build_mod.MANIFEST_NAME).read_bytes() == (
        out_b / samples / build_mod.MANIFEST_NAME
    ).read_bytes()
