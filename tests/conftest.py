# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures for all tests."""

from __future__ import annotations

import io
import textwrap
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.history import build_history_store
from app.loader import Template, TemplateRegistry
from app.render.engine import RenderEngine
from app.render.i18n import Translator

_EN_CATALOG = textwrap.dedent("""\
    frozen: "Frozen"
    stored: "Stored"
    expires: "Expires"
    _date_format: "%m/%d/%Y"
    _datetime_format: "%m/%d/%Y %H:%M"
""")
_ES_CATALOG = textwrap.dedent("""\
    frozen: "Congelado"
    stored: "Guardado"
    expires: "Caduca"
    _date_format: "%d/%m/%Y"
    _datetime_format: "%d/%m/%Y %H:%M"
""")


def _write_catalogs(directory: Path) -> None:
    """Seed a translations dir with the en + es catalogs used across the test suite."""
    (directory / "en.yaml").write_text(_EN_CATALOG)
    (directory / "es.yaml").write_text(_ES_CATALOG)


@pytest.fixture(scope="session")
def fonts_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fonts")


# A minimal black-square SVG used to exercise the cairosvg rasterization path in tests.
_SQUARE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect x="2" y="2" width="20" height="20"/></svg>'
)


@pytest.fixture(scope="session")
def icons_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("icons")
    # Create a minimal 1-bit snowflake icon PNG
    img = Image.new("L", (90, 90), 255)
    img.save(d / "snowflake.png")
    # A custom svg+png pair: the element must prefer the svg when both exist by bare name.
    (d / "foo.svg").write_text(_SQUARE_SVG, encoding="utf-8")
    Image.new("L", (90, 90), 255).save(d / "foo.png")
    return d


@pytest.fixture(scope="session")
def icon_collections_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("icon-collections")
    fa_solid = d / "fontawesome" / "solid"
    fa_solid.mkdir(parents=True)
    (fa_solid / "coffee.svg").write_text(_SQUARE_SVG, encoding="utf-8")
    return d


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("data")


@pytest.fixture(scope="session")
def translations_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("translations")
    _write_catalogs(d)
    return d


@pytest.fixture
def translator(translations_dir: Path) -> Translator:
    t = Translator(translations_dir, "en")
    t.load_all()
    return t


@pytest.fixture
def templates_dir(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    return d


@pytest.fixture
def sample_template_yaml(templates_dir: Path) -> Path:
    path = templates_dir / "test-simple.yaml"
    path.write_text(
        textwrap.dedent("""\
        name: test-simple
        description: Test template
        label: "62"
        rotate: 90
        fields:
          required: [title]
          optional: [subtitle]
        layout:
          - {type: title, text: "{{title}}", max_lines: 2}
          - {type: subtitle, text: "{{subtitle}}"}
    """)
    )
    return path


@pytest.fixture
def sample_template(sample_template_yaml: Path) -> Template:
    from app.loader import load_template

    return load_template(sample_template_yaml)


@pytest.fixture
def registry(templates_dir: Path, sample_template_yaml: Path) -> TemplateRegistry:
    r = TemplateRegistry(templates_dir)
    r.load_all()
    return r


@pytest.fixture
def mock_driver() -> MagicMock:
    from app.drivers.base import Capability
    from app.models import LabelGeometry

    driver = MagicMock()
    driver.CAPABILITY = Capability(
        name="mock",
        dpi=300,
        cut=True,
        two_color=True,
        supported_labels=["62", "62red"],
        red_labels=["62red"],
        label_geometries={
            "62": LabelGeometry(width_px=696, height_px=None, media_type="continuous"),
            "62red": LabelGeometry(width_px=696, height_px=None, media_type="continuous"),
        },
    )
    driver.render_payload.return_value = b"\x1b@" + b"\x00" * 32  # dummy QL payload
    return driver


@pytest.fixture
def png_62mm() -> bytes:
    """Minimal valid PNG for a 696px-wide label."""
    img = Image.new("L", (696, 300), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with mocked printer (file transport)."""
    import app.main as main_mod

    # Point settings at temp dirs
    templates_d = tmp_path / "templates"
    templates_d.mkdir()
    fonts_d = tmp_path / "fonts"
    fonts_d.mkdir()
    icons_d = tmp_path / "icons"
    icons_d.mkdir()
    img = Image.new("L", (90, 90), 255)
    img.save(icons_d / "snowflake.png")
    icon_collections_d = tmp_path / "icon-collections"
    (icon_collections_d / "fontawesome" / "solid").mkdir(parents=True)
    (icon_collections_d / "fontawesome" / "solid" / "coffee.svg").write_text(
        _SQUARE_SVG, encoding="utf-8"
    )
    data_d = tmp_path / "data"
    data_d.mkdir()
    translations_d = tmp_path / "translations"
    translations_d.mkdir()
    _write_catalogs(translations_d)

    # Write a minimal template
    (templates_d / "simple.yaml").write_text(
        textwrap.dedent("""\
        name: simple
        description: Simple test template
        label: "62"
        rotate: 0
        fields:
          required: [title]
          optional: [subtitle]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: subtitle, text: "{{subtitle}}"}
    """)
    )

    # A template carrying translatable chrome, for language-override tests
    (templates_d / "chrome-test.yaml").write_text(
        textwrap.dedent("""\
        name: chrome-test
        description: Template with translatable chrome words
        label: "62"
        rotate: 0
        fields:
          required: [contents]
          optional: []
        layout:
          - {type: text, text: "[[frozen]]: {{date}}"}
    """)
    )

    # A rotate: 90 continuous template (rotation regression test)
    (templates_d / "rotated.yaml").write_text(
        textwrap.dedent("""\
        name: rotated
        description: Rotated continuous template
        label: "62"
        rotate: 90
        fields:
          required: [title]
          optional: []
        layout:
          - {type: title, text: "{{title}}"}
    """)
    )

    # A two-color template on black/red media with a color: red element.
    (templates_d / "red-label.yaml").write_text(
        textwrap.dedent("""\
        name: red-label
        description: Two-color red/black template
        label: "62red"
        rotate: 0
        fields:
          required: [title]
          optional: []
        layout:
          - {type: title, text: "{{title}}", color: red}
          - {type: subtitle, text: black subtitle}
    """)
    )

    # An image template (image-field passthrough / multipart-preview test)
    (templates_d / "image-test.yaml").write_text(
        textwrap.dedent("""\
        name: image-test
        description: Image template
        label: "62"
        rotate: 0
        fields:
          required: [image]
          optional: []
        layout:
          - {type: image, field: image}
    """)
    )

    # An image template reading a non-default field name (custom-field cap-bypass test)
    (templates_d / "custom-image.yaml").write_text(
        textwrap.dedent("""\
        name: custom-image
        description: Image template with a non-default field name
        label: "62"
        rotate: 0
        fields:
          required: [photo]
          optional: []
        layout:
          - {type: image, field: photo}
    """)
    )

    # An image nested inside a row container (recursive image-field discovery test)
    (templates_d / "row-image.yaml").write_text(
        textwrap.dedent("""\
        name: row-image
        description: Image element nested one level down inside a row
        label: "62"
        rotate: 0
        fields:
          required: [photo]
          optional: [title]
        layout:
          - type: row
            children:
              - {type: title, text: "{{title}}", align: left}
              - {type: image, field: photo, width: 120, align: right}
    """)
    )

    monkeypatch.setattr(main_mod.settings, "templates_dir", templates_d)
    monkeypatch.setattr(main_mod.settings, "fonts_dir", fonts_d)
    monkeypatch.setattr(main_mod.settings, "icons_dir", icons_d)
    monkeypatch.setattr(main_mod.settings, "icon_collections_dir", icon_collections_d)
    monkeypatch.setattr(main_mod.settings, "data_dir", data_d)
    monkeypatch.setattr(main_mod.settings, "translations_dir", translations_d)
    monkeypatch.setattr(main_mod.settings, "default_language", "en")
    # No transport setting: it is inferred from the printer_uri scheme. file:// selects the sink.
    monkeypatch.setattr(main_mod.settings, "printer_uri", f"file://{tmp_path / 'output.bin'}")
    monkeypatch.setattr(main_mod.settings, "api_token", None)
    monkeypatch.setattr(main_mod.settings, "allow_unauthenticated", True)
    monkeypatch.setattr(main_mod.settings, "history_mode", "memory")
    monkeypatch.setattr(main_mod.settings, "editor_enabled", True)
    # Metrics are opt-in (default off); enable them in the harness so metrics-behaviour tests exercise
    # the live endpoint. The dedicated disabled-gate test overrides this back to False.
    monkeypatch.setattr(main_mod.settings, "metrics_enabled", True)

    # Fresh in-memory history per test (TestClient is not entered as a context manager, so the
    # startup() rebuild does not fire — build it here, mirroring registry/translator/engine).
    # Close the prior store first so its sqlite connection is not left for the GC to finalize
    # (filterwarnings=error would turn that finalization warning into a failure).
    main_mod._history.close()
    main_mod._history = build_history_store(main_mod.settings)

    # Reload registry
    main_mod.registry = TemplateRegistry(templates_d)
    main_mod.registry.load_all()

    # Rebuild the translator + engine against the temp dirs (engine holds the translator)
    main_mod.translator = Translator(translations_d, "en")
    main_mod.translator.load_all()
    main_mod.engine = RenderEngine(
        fonts_dir=fonts_d,
        icons_dir=icons_d,
        icon_collections_dir=icon_collections_d,
        translator=main_mod.translator,
    )

    # Patch driver to avoid actual QL raster generation
    mock_driver = MagicMock()
    mock_driver.render_payload.return_value = b"\x00" * 64
    monkeypatch.setattr(main_mod, "_driver", mock_driver)

    yield TestClient(main_mod.app)

    # Close the history connection so it is not left for the GC (see note above).
    main_mod._history.close()


@pytest.fixture(autouse=True)
def _reset_usb_module_state() -> Iterator[None]:
    """Reset the USB module's device-busy flag and lock before every test.

    ``_usb_busy`` is module-level state that persists across tests.  When a timeout test spawns
    a daemon worker that sleeps for several seconds, ``_usb_busy`` stays True for the lifetime of
    that sleep, causing the next test to see a falsely-busy device.  Resetting here gives every
    test a clean slate without requiring each one to manage teardown.
    """
    import threading

    import app.transports.usb as usb_mod

    # Replace the lock so any orphaned worker from a prior test still holds the OLD lock object
    # and cannot interfere with the fresh one.  _usb_busy must also be cleared so the fast-path
    # check in send() does not fire prematurely.
    usb_mod._USB_DEVICE_LOCK = threading.Lock()
    usb_mod._usb_busy = False
    yield
    # No teardown needed: the next iteration of this fixture resets state again.
