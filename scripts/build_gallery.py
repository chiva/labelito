#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render a showcase preview for every shipped template and emit a gallery manifest.

The static site (``site/``) is served by GitHub Pages, which cannot run Python — so the label
previews must be pre-rendered. This script drives the SAME render path the live ``/preview``
endpoint uses (:func:`app.main._render_template_preview`), so a gallery image is byte-identical to
what the printer would actually put on the label. It writes:

    site/assets/samples/<template>.png   one preview per registered template
    site/assets/samples/manifest.json    the data ``site/gallery.html`` renders cards from

Curated example field values live in :data:`SAMPLES` below (one entry per template ``name``). A
template with no entry falls back to a humanized placeholder per declared field, so a newly added
template still appears in the gallery — just with generic sample text until it earns a real example.

Usage (from the repo root):

    uv run python scripts/build_gallery.py            # render into ./site/assets/samples
    uv run python scripts/build_gallery.py --out DIR  # render into DIR/assets/samples

Run in CI by ``.github/workflows/pages.yml`` before the site artifact is uploaded, so the gallery
can never drift from the templates. The generated ``samples/`` dir is git-ignored (regenerated on
every deploy), not committed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# The app refuses to construct without an auth posture chosen (see app/main.py). This tool renders
# offline and never serves a request, so the unauthenticated LAN posture is the correct, side-effect
# -free choice. setdefault so an explicit env still wins.
os.environ.setdefault("ALLOW_UNAUTHENTICATED", "true")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import main as app_main  # noqa: E402  (env must be set before app import)
from app.loader import Template  # noqa: E402
from app.media import MEDIA_TYPE_CONTINUOUS, required_media_for  # noqa: E402

# Default output root: the tracked static site. Previews land under ``<OUT>/assets/samples``.
DEFAULT_OUT_DIR = _REPO_ROOT / "site"
SAMPLES_SUBDIR = Path("assets") / "samples"
MANIFEST_NAME = "manifest.json"

# The example language: ``en`` has the full [[frozen]]/[[stored]]/[[expires]] catalog, so dated
# labels render real words rather than raw keys.
SAMPLE_LANGUAGE = "en"

# A FIXED render moment so re-runs are byte-reproducible (tests can assert; CI deploys don't churn).
# Auto-dated templates ({{date}}, {{date+6m}}) resolve against this instant.
SAMPLE_NOW = datetime(2026, 1, 15, 9, 30, 0)

# A small bundled PNG, base64-encoded on demand, for the API-only ``image`` template's ``image``
# field (the element base64-decodes fields[field] — see app/render/elements.py ImageElement).
_SAMPLE_IMAGE_PATH = _REPO_ROOT / "assets" / "icons" / "snowflake.png"


def _sample_image_b64() -> str:
    return base64.b64encode(_SAMPLE_IMAGE_PATH.read_bytes()).decode("ascii")


# Curated example fields per template ``name`` — realistic content that shows each template at its
# best. Keys are the template ``name`` (not the file stem). ``;``-separated values feed ``list``
# elements (their configured separator); see e.g. templates/62-two-column.yaml.
SAMPLES: dict[str, dict[str, Any]] = {
    "simple-text-12": {"text": "SHELF A-3"},
    "simple-text-29": {"text": "FRAGILE"},
    "simple-text": {"text": "Hello, labelito"},
    "title-subtitle": {"title": "Meeting Room", "subtitle": "3rd Floor · West Wing"},
    "title-subtitle-qr": {
        "title": "Guest Wi-Fi",
        "subtitle": "Scan to connect",
        "qr": "WIFI:S:GuestNet;T:WPA;P:welcome123;;",
    },
    "freezer-icon": {"title": "Bolognese sauce", "subtitle": "2 portions"},
    "freezer-dated": {"title": "Chicken stock", "subtitle": "Homemade"},
    "fridge-dated": {"title": "Cooked rice", "subtitle": "Jasmine"},
    "pantry": {"title": "Basmati rice", "subtitle": "Organic", "quantity": "2 kg"},
    "custom-icon": {"title": "Bolognese sauce", "subtitle": "2 portions"},
    "icon-collection": {"title": "Morning blend", "subtitle": "Whole bean · medium roast"},
    "row-demo": {"title": "Nightly backup", "status": "Completed 02:14"},
    "cable-label": {
        "name": "UPLINK-01",
        "endpoint_a": "Switch-A p24",
        "endpoint_b": "Rack-B p12",
    },
    "asset-tag": {
        "title": "MacBook Pro 16",
        "asset_id": "QK-2024-0421",
        "location": "Desk 14 · Floor 3",
    },
    "shipping-badge": {
        "handling": "FRAGILE",
        "reference": "ORD-88213",
        "qr": "https://track.example/ORD-88213",
    },
    "storage-box-qr": {
        "title": "Garage · Box 7",
        "contents": "Cables; Adapters; Chargers; Batteries",
        "qr": "https://inv.example/box/7",
    },
    "two-column": {
        "left_title": "Groceries",
        "left_items": "Milk; Eggs; Bread; Butter",
        "right_title": "Errands",
        "right_items": "Call bank; Renew pass; Book flight",
    },
    "address": {
        "name": "Alan Turing",
        "line1": "Bletchley Park",
        "line2": "Milton Keynes",
        "line3": "MK3 6EB",
    },
    "address-17x54": {
        "name": "Ada Lovelace",
        "line1": "12 Analytical Ave",
        "line2": "London EC1",
    },
    "address-29x90": {
        "name": "Grace Hopper",
        "line1": "Compiler Lane 1952",
        "line2": "Arlington, VA",
        "line3": "USA",
    },
    # image is populated at build time (needs the base64 payload); see _fields_for.
    "image": {"title": "Company logo"},
}


def _humanize(field: str) -> str:
    """A generic placeholder for a field with no curated sample (keeps new templates visible)."""
    return field.replace("_", " ").strip().title() or "Sample"


def _fields_for(tmpl: Template) -> dict[str, Any]:
    """Resolve the example fields for a template: curated where available, humanized otherwise.

    Every declared field (required first, then optional) is populated so the preview exercises the
    full layout. The API-only ``image`` template gets the bundled sample PNG injected as base64.
    """
    curated = dict(SAMPLES.get(tmpl.name, {}))
    if tmpl.name == "image":
        curated["image"] = _sample_image_b64()
    fields: dict[str, Any] = {}
    for field in tmpl.all_fields:
        if field in curated:
            fields[field] = curated[field]
        elif field == "image":
            fields[field] = _sample_image_b64()
        else:
            fields[field] = _humanize(field)
    return fields


def _size_label(label_id: str) -> str:
    """A human-readable media descriptor, e.g. ``62 mm · continuous`` or ``62 x 29 mm · die-cut``."""
    media = required_media_for(label_id)
    width = f"{media.width_mm:g}"
    if media.media_type == MEDIA_TYPE_CONTINUOUS:
        return f"{width} mm · continuous"
    return f"{width} x {media.length_mm:g} mm · die-cut"


def _curl_example(name: str, fields: dict[str, Any]) -> str:
    """A copy-pasteable POST /print snippet. Bulky base64 image payloads are elided for readability."""
    display = {k: ("<base64-png>" if k == "image" else v) for k, v in fields.items()}
    body = json.dumps({"template": name, "fields": display}, ensure_ascii=False)
    return (
        "curl -X POST http://localhost:8765/print \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        f"  -d {shlex.quote(body)}"
    )


def _entry(tmpl: Template, image_rel: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Assemble one manifest record for a template (everything gallery.html needs to draw a card)."""
    width_px, height_px = app_main._get_geometry(tmpl.label)
    display_fields = {k: ("<base64-png>" if k == "image" else v) for k, v in fields.items()}
    return {
        "name": tmpl.name,
        "description": tmpl.description or "",
        "label": tmpl.label,
        "size": _size_label(tmpl.label),
        "is_example": tmpl.is_example,
        "required": list(tmpl.required_fields),
        "optional": list(tmpl.optional_fields),
        "fields": display_fields,
        "image": image_rel,
        "width_px": width_px,
        "height_px": height_px,
        "curl": _curl_example(tmpl.name, fields),
    }


class GalleryBuildError(RuntimeError):
    """A shipped template or translation catalog failed to load — the build must fail closed.

    ``TemplateRegistry``/``Translator`` skip malformed files and record the reason in ``.errors``
    rather than raising, so an unchecked build would deploy a gallery that silently omits the broken
    template (or renders raw ``[[token]]`` chrome) while the Pages job still reports success — hiding
    exactly the breakage this generated showcase exists to surface.
    """


def build(out_dir: Path) -> list[dict[str, Any]]:
    """Render every registered template into ``out_dir/assets/samples`` and write ``manifest.json``.

    Returns the manifest (list of per-template records), sorted by media size then name for a stable
    gallery order. Raises :class:`GalleryBuildError` if any shipped template or translation catalog
    fails to load (fail closed, so a broken template fails the deploy instead of vanishing from the
    showcase), and propagates any render exception for the same reason.
    """
    names = sorted(app_main.registry.load_all())
    if app_main.registry.errors:
        raise GalleryBuildError(
            "template(s) failed to load:\n  " + "\n  ".join(app_main.registry.errors)
        )
    app_main.translator.load_all()
    if app_main.translator.errors:
        raise GalleryBuildError(
            "translation catalog(s) failed to load:\n  " + "\n  ".join(app_main.translator.errors)
        )

    samples_dir = out_dir / SAMPLES_SUBDIR
    samples_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    for name in names:
        tmpl = app_main.registry.get(name)
        if tmpl is None:  # pragma: no cover - names() and get() come from the same registry
            continue
        fields = _fields_for(tmpl)
        png = app_main._render_template_preview(tmpl, fields, SAMPLE_LANGUAGE, now=SAMPLE_NOW)
        (samples_dir / f"{name}.png").write_bytes(png)
        image_rel = f"{SAMPLES_SUBDIR.as_posix()}/{name}.png"
        entries.append(_entry(tmpl, image_rel, fields))

    # height_px is None for continuous labels (no fixed length); treat as 0 so those sort ahead of
    # die-cut labels of the same width, keeping the order total and deterministic.
    entries.sort(key=lambda e: (e["width_px"], e["height_px"] or 0, e["name"]))
    manifest_path = samples_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="site root to write assets/samples into (default: ./site)",
    )
    args = parser.parse_args(argv)

    entries = build(args.out)
    dest = (args.out / SAMPLES_SUBDIR).resolve()
    print(f"Rendered {len(entries)} template previews → {dest}")
    print(f"Manifest → {(dest / MANIFEST_NAME)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
