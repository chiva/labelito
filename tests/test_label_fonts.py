# SPDX-License-Identifier: GPL-3.0-or-later
"""Selectable label fonts: manifest integrity, the pinned fetcher, loading, per-character fallback.

Nothing here needs the fetched fonts. Fallback is exercised with fonts built at test time from
DejaVu by fontTools.subset and registered as label fonts: a digits-only subset forces a mixed-font
line, and a full copy renders the same text in one font. Because the glyphs are identical, the two
renders must match pixel for pixel — proving the runs share a baseline and advance correctly. Tests
against the real fetched families carry the ``label_fonts`` marker (CI fetches them in their own job).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops

from app.loader import TemplateLoadError, validate_template_from_string
from app.render import elements, fonts
from app.render.elements import (
    _GlyphFallback,
    _load_font,
    _load_label_font,
    _runs,
    _wrap_text,
    build_element,
)
from app.render.engine import missing_label_fonts
from app.render.fonts import DEFAULT_FONT, FONT_CATEGORIES, FONT_REGISTRY, FontStyle, LabelFont

REPO = Path(__file__).resolve().parent.parent
MANIFEST = json.loads(fonts.MANIFEST_PATH.read_text(encoding="utf-8"))
ALLOWED_LICENSES = {"OFL-1.1", "Apache-2.0", "Bitstream-Vera"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANVAS_W = 696
DIGITS_KEY = "test-digits"
FULL_KEY = "test-full"
MIXED_TEXT = "Café 12:30"


def _dejavu_dir() -> Path:
    for candidate in (REPO / "fonts", Path("/usr/share/fonts/truetype/dejavu")):
        if (candidate / "DejaVuSans.ttf").is_file() and (
            candidate / "DejaVuSans-Bold.ttf"
        ).is_file():
            return candidate
    pytest.skip("DejaVu Sans is not installed (run scripts/fetch-fonts.sh)")


def _subset(source: Path, target: Path, text: str | None) -> None:
    from fontTools import subset

    options = subset.Options()
    options.layout_features = ["*"]
    font = subset.load_font(str(source), options)
    subsetter = subset.Subsetter(options)
    if text is None:
        subsetter.populate(unicodes=font.getBestCmap().keys())
    else:
        subsetter.populate(text=text)
    subsetter.subset(font)
    target.parent.mkdir(parents=True, exist_ok=True)
    subset.save_font(font, str(target), options)
    font.close()


@pytest.fixture
def label_fonts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(fonts_dir with DejaVu, label_fonts_dir holding the two test families, registered)."""
    dejavu = _dejavu_dir()
    label_dir = tmp_path / "label-fonts"
    for key, text in ((DIGITS_KEY, "0123456789: "), (FULL_KEY, None)):
        for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
            _subset(dejavu / name, label_dir / key / name, text)
        monkeypatch.setitem(
            FONT_REGISTRY,
            key,
            LabelFont(
                key=key,
                name=key,
                category="sans",
                license="Bitstream-Vera",
                builtin=False,
                regular=FontStyle("DejaVuSans.ttf"),
                bold=FontStyle("DejaVuSans-Bold.ttf"),
            ),
        )
    return dejavu, label_dir


def _render(spec: dict[str, Any], dirs: tuple[Path, Path], **kwargs: Any) -> Image.Image:
    fonts_dir, label_dir = dirs
    el = build_element(spec, label_fonts_dir=label_dir, **kwargs)
    return el.render(CANVAS_W, {"__text__": spec["text"]}, fonts_dir, fonts_dir, fonts_dir)


# ── Manifest integrity ─────────────────────────────────────────────────────────────
def test_manifest_has_one_builtin_default_and_known_categories() -> None:
    builtins = [f for f in MANIFEST["families"] if f.get("builtin")]
    assert [f["key"] for f in builtins] == [MANIFEST["default"]] == [DEFAULT_FONT]
    assert {f["category"] for f in MANIFEST["families"]} <= set(FONT_CATEGORIES)
    keys = [f["key"] for f in MANIFEST["families"]]
    assert len(keys) == len(set(keys)), "duplicate font keys"
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", key) for key in keys)


@pytest.mark.parametrize(
    "family", [f for f in MANIFEST["families"] if not f.get("builtin")], ids=lambda f: f["key"]
)
def test_every_fetched_family_is_pinned_and_licensed(family: dict[str, Any]) -> None:
    """Every file is pinned by SHA-256 to an immutable source, and the licence ships with the font."""
    assert family["license"] in ALLOWED_LICENSES
    entries = [*family["styles"].values(), family["license_file"]]
    for entry in entries:
        assert SHA256_RE.match(entry["sha256"]), entry
        assert entry["url"].startswith("https://"), entry
        pinned = (
            "/google/fonts/23e54b51ddffbc7713c583748e3bd86f62b1fa4a/" in entry["url"]
            or "/v0.46/" in entry["url"]
        )
        assert pinned, f"{entry['url']} is not pinned to a commit or release tag"
        if "member" in entry:
            assert SHA256_RE.match(entry["member_sha256"]), entry
    assert "regular" in family["styles"]
    weights = [style.get("wght") for style in family["styles"].values()]
    if any(w is not None for w in weights):  # a variable font: both styles are the same file
        assert weights == [400, 700] and len({s["file"] for s in family["styles"].values()}) == 1


def test_registry_mirrors_the_manifest() -> None:
    assert set(FONT_REGISTRY) == {f["key"] for f in MANIFEST["families"]}
    patrick = FONT_REGISTRY["patrick-hand"]
    assert not patrick.has_bold and patrick.style(bold=True) == patrick.regular
    montserrat = FONT_REGISTRY["montserrat"]
    assert montserrat.style(bold=True) == FontStyle("Montserrat[wght].ttf", 700)
    assert montserrat.path(Path("/x"), bold=False) == Path("/x/montserrat/Montserrat[wght].ttf")
    assert FONT_REGISTRY[DEFAULT_FONT].path(Path("/x"), bold=False) is None


# ── Fetcher ────────────────────────────────────────────────────────────────────────
def _fetcher() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fetch_label_fonts", REPO / "scripts" / "fetch_label_fonts.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(tmp_path: Path, name: str, data: bytes) -> dict[str, str]:
    path = tmp_path / "src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"file": name, "url": path.as_uri(), "sha256": hashlib.sha256(data).hexdigest()}


def _mini_manifest(tmp_path: Path) -> dict[str, Any]:
    archive = tmp_path / "src" / "pack.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("pack/Seg-Regular.ttf", b"seg-font")
    zip_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {
        "default": "dejavu-sans",
        "categories": ["sans", "lcd"],
        "families": [
            {"key": "dejavu-sans", "builtin": True, "styles": {"regular": {}, "bold": {}}},
            {
                "key": "plain",
                "styles": {"regular": _source(tmp_path, "Plain-Regular.ttf", b"regular-bytes")},
                "license_file": _source(tmp_path, "OFL.txt", b"licence text"),
            },
            {
                "key": "seg",
                "styles": {
                    "regular": {
                        "file": "Seg-Regular.ttf",
                        "url": archive.as_uri(),
                        "sha256": zip_sha,
                        "member": "pack/Seg-Regular.ttf",
                        "member_sha256": hashlib.sha256(b"seg-font").hexdigest(),
                    }
                },
                "license_file": _source(tmp_path, "SEG.txt", b"seg licence"),
            },
        ],
    }


def test_fetcher_writes_each_family_with_its_licence_and_verifies(tmp_path: Path) -> None:
    fetcher = _fetcher()
    manifest = _mini_manifest(tmp_path)
    dest = tmp_path / "out"
    assert fetcher.fetch(dest, manifest) == fetcher.EXIT_OK
    assert (dest / "plain" / "Plain-Regular.ttf").read_bytes() == b"regular-bytes"
    assert (dest / "plain" / "LICENSE.txt").read_bytes() == b"licence text"
    assert (dest / "seg" / "Seg-Regular.ttf").read_bytes() == b"seg-font"
    assert fetcher.verify(dest, manifest) == fetcher.EXIT_OK
    assert not (dest / "dejavu-sans").exists(), "the builtin family is never fetched"


def test_fetcher_refuses_a_tampered_download_and_keeps_the_previous_tree(tmp_path: Path) -> None:
    fetcher = _fetcher()
    manifest = _mini_manifest(tmp_path)
    dest = tmp_path / "out"
    fetcher.fetch(dest, manifest)
    (tmp_path / "src" / "Plain-Regular.ttf").write_bytes(b"substituted")
    with pytest.raises(fetcher.FontFetchError, match="does not match pinned"):
        fetcher.fetch(dest, manifest)
    assert (dest / "plain" / "Plain-Regular.ttf").read_bytes() == b"regular-bytes"


def test_fetcher_verify_reports_missing_and_modified_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = _fetcher()
    manifest = _mini_manifest(tmp_path)
    dest = tmp_path / "out"
    fetcher.fetch(dest, manifest)
    (dest / "plain" / "LICENSE.txt").unlink()
    (dest / "seg" / "Seg-Regular.ttf").write_bytes(b"edited")
    assert fetcher.verify(dest, manifest) == fetcher.EXIT_MISMATCH
    err = capsys.readouterr().err
    assert "missing plain/LICENSE.txt" in err and "SHA-256 mismatch seg/Seg-Regular.ttf" in err


# ── Loading ────────────────────────────────────────────────────────────────────────
def test_builtin_family_is_the_legacy_font_with_no_fallback(tmp_path: Path) -> None:
    dejavu = _dejavu_dir()
    font, fallback = _load_label_font(dejavu, tmp_path, DEFAULT_FONT, 32, bold=False)
    assert fallback is None
    assert font.getbbox("Hello") == _load_font(dejavu, 32).getbbox("Hello")


def test_uninstalled_family_renders_in_dejavu_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dejavu = _dejavu_dir()
    elements._WARNED_FONT_FALLBACKS.discard("label-font:caveat")
    with caplog.at_level(logging.WARNING, logger=elements.log.name):
        for _ in range(3):
            font, fallback = _load_label_font(dejavu, tmp_path / "empty", "caveat", 32, bold=False)
    assert fallback is None
    assert font.getbbox("Hello") == _load_font(dejavu, 32).getbbox("Hello")
    assert sum("'caveat' is not installed" in r.getMessage() for r in caplog.records) == 1


def test_variable_font_weight_axis_is_set_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several variable families default to Thin or Light, so Regular must be set to 400 and Bold
    to 700 on the `wght` axis, leaving every other axis at its own default."""
    calls: list[list[float]] = []

    class _Font:
        def set_variation_by_axes(self, values: list[float]) -> None:
            calls.append(values)

    font_file = tmp_path / "montserrat" / "Montserrat[wght].ttf"
    font_file.parent.mkdir(parents=True)
    font_file.write_bytes(b"")
    monkeypatch.setattr(elements.ImageFont, "truetype", lambda *_a, **_k: _Font())
    monkeypatch.setattr(elements, "_variation_axes", lambda _p: (("opsz", 14.0), ("wght", 100.0)))
    monkeypatch.setattr(elements, "_glyph_coverage", lambda _p: frozenset())
    monkeypatch.setattr(elements, "_load_font", lambda *_a, **_k: _Font())
    _load_label_font(tmp_path, tmp_path, "montserrat", 32, bold=False)
    _load_label_font(tmp_path, tmp_path, "montserrat", 32, bold=True)
    assert calls == [[14.0, 400.0], [14.0, 700.0]]


# ── Per-character fallback ─────────────────────────────────────────────────────────
def test_runs_switch_fonts_per_cluster_and_keep_spaces_in_the_current_run(
    label_fonts: tuple[Path, Path],
) -> None:
    fonts_dir, label_dir = label_fonts
    font, fallback = _load_label_font(fonts_dir, label_dir, DIGITS_KEY, 40, bold=False)
    assert fallback is not None
    runs = [
        (run_font is font, "".join(clusters))
        for run_font, clusters in _runs(MIXED_TEXT, font, fallback)
    ]
    assert runs == [(False, "Café "), (True, "12:30")]


def test_a_combining_accent_never_splits_from_its_base_letter(
    label_fonts: tuple[Path, Path],
) -> None:
    fonts_dir, label_dir = label_fonts
    font, fallback = _load_label_font(fonts_dir, label_dir, DIGITS_KEY, 40, bold=False)
    assert fallback is not None
    decomposed = "5é"  # 5, then e + combining acute: the digit font has neither e nor U+0301
    runs = ["".join(c) for _f, c in _runs(decomposed, font, fallback)]
    assert runs == ["5", "é"]
    only_mark_missing = _GlyphFallback(fallback.font, fallback.coverage | {ord("e")})
    runs = ["".join(c) for _f, c in _runs(decomposed, font, only_mark_missing)]
    assert runs == ["5", "é"], "a base the font has still falls back when its mark is missing"


@pytest.mark.parametrize("letter_spacing", [0.0, 0.2])
@pytest.mark.parametrize("bold", [False, True])
def test_mixed_font_line_matches_a_single_font_render_pixel_for_pixel(
    label_fonts: tuple[Path, Path], letter_spacing: float, bold: bool
) -> None:
    """The digit subset and the full copy share DejaVu's glyphs, so a line split into a fallback run
    and a primary run must be indistinguishable from the same line in one font."""
    base = {
        "type": "text",
        "text": MIXED_TEXT,
        "size": 40,
        "letter_spacing": letter_spacing,
        "bold": bold,
    }
    mixed = _render({**base, "font": DIGITS_KEY}, label_fonts)
    single = _render({**base, "font": FULL_KEY}, label_fonts)
    assert mixed.size == single.size
    assert ImageChops.difference(mixed, single).getbbox() is None


def _tall_digits_font(label_dir: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a digit-only font whose ascent is 60% taller than DejaVu's. With identical metrics a
    baseline bug is invisible; with this one, runs aligned by their ascender instead of their
    baseline land at visibly different heights."""
    from fontTools.ttLib import TTFont

    key = "test-tall-digits"
    source = label_dir / DIGITS_KEY / "DejaVuSans.ttf"
    target = label_dir / key / "DejaVuSans.ttf"
    target.parent.mkdir(parents=True, exist_ok=True)
    with TTFont(source) as font:
        grow = int(font["hhea"].ascent * 0.6)
        font["hhea"].ascent += grow
        font["OS/2"].sTypoAscender += grow
        font["OS/2"].usWinAscent += grow
        font.save(target)
    monkeypatch.setitem(
        FONT_REGISTRY,
        key,
        LabelFont(key, key, "sans", "Bitstream-Vera", False, FontStyle("DejaVuSans.ttf"), None),
    )
    return key


def test_runs_from_fonts_with_different_ascents_share_one_baseline(
    label_fonts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """'H' (DejaVu fallback) and '0' (the tall-ascent primary) have no descenders: on a shared
    baseline their ink ends on the same row, and neither is clipped at the strip's edges."""
    key = _tall_digits_font(label_fonts[1], monkeypatch)
    img = _render({"type": "text", "text": "HHH000", "size": 60, "font": key}, label_fonts)
    ink = ImageChops.invert(img.convert("L"))
    box = ink.getbbox()
    assert box is not None
    split = (box[0] + box[2]) // 2
    letters = ink.crop((0, 0, split, img.height)).getbbox()
    digits = ink.crop((split, 0, img.width, img.height)).getbbox()
    assert letters is not None and digits is not None
    assert abs(letters[3] - digits[3]) <= 1, (
        f"baselines differ: letters end {letters[3]}, digits {digits[3]}"
    )
    assert box[1] > 0 and box[3] < img.height, "a run was clipped at the strip edge"


def test_mixed_font_wrapping_matches_single_font_wrapping(label_fonts: tuple[Path, Path]) -> None:
    fonts_dir, label_dir = label_fonts
    digits, digits_fallback = _load_label_font(fonts_dir, label_dir, DIGITS_KEY, 48, bold=False)
    full, full_fallback = _load_label_font(fonts_dir, label_dir, FULL_KEY, 48, bold=False)
    text = "Pedido 2026 entregado el 14:30 en Café Central 12"
    for tracking in (0.0, 9.6):
        assert _wrap_text(text, digits, 420, tracking, digits_fallback) == _wrap_text(
            text, full, 420, tracking, full_fallback
        )


def test_template_font_reaches_row_and_column_children(label_fonts: tuple[Path, Path]) -> None:
    _fonts_dir, label_dir = label_fonts
    row = build_element(
        {
            "type": "row",
            "children": [
                {"type": "text", "text": "a"},
                {"type": "column", "children": [{"type": "title", "text": "b", "font": FULL_KEY}]},
            ],
        },
        label_fonts_dir=label_dir,
        default_font=DIGITS_KEY,
    )
    text, column = row.children  # type: ignore[attr-defined]
    assert (text._default_font, text.font, text._label_fonts_dir) == (DIGITS_KEY, "", label_dir)
    title = column.children[0]
    assert (title._default_font, title.font) == (DIGITS_KEY, FULL_KEY)


def test_element_font_overrides_the_template_default(label_fonts: tuple[Path, Path]) -> None:
    spec = {"type": "text", "text": MIXED_TEXT, "size": 40}
    inherited = _render(spec, label_fonts, default_font=FULL_KEY)
    own = _render({**spec, "font": FULL_KEY}, label_fonts, default_font=DIGITS_KEY)
    assert ImageChops.difference(inherited, own).getbbox() is None


def test_missing_label_fonts_names_uninstalled_families_only(tmp_path: Path) -> None:
    (tmp_path / "caveat").mkdir()
    (tmp_path / "caveat" / "Caveat[wght].ttf").write_bytes(b"")
    layout = [
        {"type": "text", "text": "x", "font": "caveat"},
        {"type": "row", "children": [{"type": "title", "text": "y", "font": "vt323"}]},
    ]
    assert missing_label_fonts(layout, "inter", tmp_path) == {"vt323", "inter"}
    assert missing_label_fonts([], DEFAULT_FONT, tmp_path) == set()


# ── Loader ─────────────────────────────────────────────────────────────────────────
def _draft(top: str = "", element: str = "") -> str:
    return (
        f'name: fonts\ndescription: d\nlabel: "62"\n{top}layout:\n'
        f'  - {{type: text, text: "Hello"{element}}}\n'
    )


def test_template_and_element_fonts_load() -> None:
    tmpl = validate_template_from_string(_draft("font: courier-prime\n", ", font: dseg7-classic"))
    assert tmpl.font == "courier-prime"
    assert tmpl.layout[0]["font"] == "dseg7-classic"
    assert validate_template_from_string(_draft()).font == DEFAULT_FONT


@pytest.mark.parametrize(
    ("top", "element", "where"),
    [
        ("font: comic-sans\n", "", "template 'font'"),
        ("font: null\n", "", "template 'font'"),
        ("", ", font: Montserrat", "layout[0] 'font'"),
        ("", ", font: null", "layout[0] 'font'"),
        ("", ", font: [inter]", "layout[0] 'font'"),
    ],
)
def test_unknown_or_malformed_fonts_are_rejected(top: str, element: str, where: str) -> None:
    with pytest.raises(TemplateLoadError, match=rf"{re.escape(where)} must be one of"):
        validate_template_from_string(_draft(top, element))


# ── Fetched families (CI `Label fonts` job; run scripts/fetch_label_fonts.py locally) ──
FETCHED = REPO / "label-fonts"
LATIN1_SAMPLE = "Ñandú Café Crème €12,50 · «ÄÖÜß» 21°"


@pytest.mark.label_fonts
def test_fetched_tree_matches_the_manifest() -> None:
    assert _fetcher().verify(FETCHED, MANIFEST) == 0


@pytest.mark.label_fonts
@pytest.mark.parametrize("key", [k for k, f in FONT_REGISTRY.items() if not f.builtin])
@pytest.mark.parametrize("bold", [False, True])
def test_every_fetched_family_renders(key: str, bold: bool) -> None:
    dejavu = _dejavu_dir()
    img = _render(
        {"type": "text", "text": LATIN1_SAMPLE, "font": key, "bold": bold, "size": 36},
        (dejavu, FETCHED),
    )
    assert ImageChops.invert(img.convert("L")).getbbox() is not None


@pytest.mark.label_fonts
@pytest.mark.parametrize(
    "key", [k for k, f in FONT_REGISTRY.items() if not f.builtin and f.category != "lcd"]
)
def test_text_families_cover_latin1_themselves(key: str) -> None:
    """Only the segment-display fonts are expected to lean on the DejaVu fallback for Latin-1."""
    coverage = elements._glyph_coverage(str(FONT_REGISTRY[key].path(FETCHED, bold=False)))
    missing = [c for c in LATIN1_SAMPLE if ord(c) not in coverage]
    assert len(missing) <= 1, f"{key} lacks {missing}"


@pytest.mark.label_fonts
def test_variable_bold_is_heavier_than_regular() -> None:
    dejavu = _dejavu_dir()

    def ink(bold: bool) -> int:
        img = _render(
            {"type": "text", "text": "Montserrat", "font": "montserrat", "bold": bold, "size": 48},
            (dejavu, FETCHED),
        )
        return sum(1 for p in img.convert("L").getdata() if p < 128)

    assert ink(True) > ink(False) * 1.3


# ── Remaining branches: boot warning, alignment, bitmap metrics ────────────────────
def _registry_with(tmp_path: Path, yaml: str) -> Any:
    from app.loader import TemplateRegistry

    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text(yaml)
    reg = TemplateRegistry(tdir)
    reg.load_all()
    assert not reg.errors, reg.errors
    return reg


def test_boot_warning_names_the_template_and_its_uninstalled_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import app.main as main_mod

    reg = _registry_with(
        tmp_path,
        'name: lcd-temp\ndescription: d\nlabel: "62"\nfont: vt323\n'
        "layout:\n  - {type: text, text: hi, font: dseg7-classic}\n",
    )
    monkeypatch.setattr(main_mod, "registry", reg)
    monkeypatch.setattr(main_mod.settings, "label_fonts_dir", tmp_path / "no-fonts")
    with caplog.at_level(logging.WARNING):
        main_mod._warn_missing_label_fonts()
    assert "lcd-temp" in caplog.text
    assert "['dseg7-classic', 'vt323']" in caplog.text


def test_boot_warning_survives_a_failing_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Advisory only: one template that breaks the scan must not abort startup, reload or save."""
    import app.main as main_mod

    reg = _registry_with(
        tmp_path, 'name: t\ndescription: d\nlabel: "62"\nlayout:\n  - {type: text, text: hi}\n'
    )
    monkeypatch.setattr(main_mod, "registry", reg)

    def boom(*_args: Any) -> set[str]:
        raise OSError("unreadable")

    monkeypatch.setattr(main_mod, "missing_label_fonts", boom)
    with caplog.at_level(logging.ERROR):
        main_mod._warn_missing_label_fonts()
    assert "Missing-font scan failed for template 't'" in caplog.text


def test_missing_label_fonts_skips_non_mapping_entries(tmp_path: Path) -> None:
    layout: list[Any] = ["not an element", {"type": "text", "text": "x", "font": "bungee"}]
    assert missing_label_fonts(layout, DEFAULT_FONT, tmp_path) == {"bungee"}


@pytest.mark.parametrize("align", ["center", "right"])
def test_mixed_font_lines_align_like_single_font_lines(
    label_fonts: tuple[Path, Path], align: str
) -> None:
    base = {"type": "text", "text": MIXED_TEXT, "size": 40, "align": align}
    mixed = _render({**base, "font": DIGITS_KEY}, label_fonts)
    single = _render({**base, "font": FULL_KEY}, label_fonts)
    assert ImageChops.difference(mixed, single).getbbox() is None
    box = ImageChops.invert(mixed.convert("L")).getbbox()
    assert box is not None
    if align == "right":
        assert CANVAS_W - 8 - 2 <= box[2] <= CANVAS_W - 8 + 2
    else:
        assert abs((box[0] + box[2]) / 2 - CANVAS_W / 2) <= 2


def test_bitmap_last_resort_font_reports_its_glyph_height() -> None:
    from PIL import ImageFont

    bitmap = ImageFont.load_default_imagefont()
    ascent, descent = elements._metrics(bitmap)
    assert ascent == bitmap.getbbox("Ay")[3] and descent == 0
