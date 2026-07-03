# SPDX-License-Identifier: GPL-3.0-or-later
"""Translator unit tests — catalog loading, fallback chain, locale date formats."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from app.render.i18n import (
    DEFAULT_DATE_FORMAT,
    DEFAULT_DATETIME_FORMAT,
    DEFAULT_WEEKDAYS_ABBR,
    DEFAULT_WEEKDAYS_FULL,
    TranslationLoadError,
    Translator,
    load_catalog,
)


def _write(directory: Path, lang: str, body: str) -> Path:
    path = directory / f"{lang}.yaml"
    path.write_text(textwrap.dedent(body))
    return path


@pytest.fixture
def catalogs(tmp_path: Path) -> Path:
    d = tmp_path / "translations"
    d.mkdir()
    _write(d, "en", 'frozen: "Frozen"\nexpires: "Expires"\n_date_format: "%m/%d/%Y"\n')
    _write(d, "es", 'frozen: "Congelado"\n_date_format: "%d/%m/%Y"\n')
    return d


def test_load_all_and_available(catalogs: Path) -> None:
    t = Translator(catalogs, "en")
    assert sorted(t.load_all()) == ["en", "es"]
    assert t.available() == ["en", "es"]
    assert len(t) == 2
    assert t.has("en") and not t.has("de")


def test_translate_happy_path(catalogs: Path) -> None:
    t = Translator(catalogs, "en")
    t.load_all()
    assert t.translate("[[frozen]]: today", "es") == "Congelado: today"
    assert t.translate("plain text, no tokens", "es") == "plain text, no tokens"


def test_translate_falls_back_to_default_then_raw(
    catalogs: Path, caplog: pytest.LogCaptureFixture
) -> None:
    t = Translator(catalogs, "en")
    t.load_all()
    # 'expires' missing in es → falls back to en default.
    assert t.translate("[[expires]]", "es") == "Expires"
    # 'unknown' missing everywhere → raw key, with a warning.
    with caplog.at_level(logging.WARNING):
        assert t.translate("[[unknown]]", "es") == "unknown"
    assert any("missing" in r.message for r in caplog.records)


def test_translate_language_casing_is_normalized(catalogs: Path) -> None:
    t = Translator(catalogs, "EN")
    t.load_all()
    assert t.translate("[[frozen]]", "ES") == "Congelado"


def test_unknown_language_falls_back_to_default(catalogs: Path) -> None:
    t = Translator(catalogs, "en")
    t.load_all()
    assert t.translate("[[frozen]]", "zz") == "Frozen"


def test_date_formats_per_language_and_fallback(catalogs: Path) -> None:
    t = Translator(catalogs, "en")
    t.load_all()
    assert t.date_formats("es")[0] == "%d/%m/%Y"
    assert t.date_formats("en")[0] == "%m/%d/%Y"
    # es omits _datetime_format → module default.
    assert t.date_formats("es")[1] == DEFAULT_DATETIME_FORMAT
    # Unknown language → the default language's locale (en here), not the module default.
    assert t.date_formats("zz") == ("%m/%d/%Y", DEFAULT_DATETIME_FORMAT)


def test_empty_dir_loads_nothing(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    t = Translator(d, "en")
    assert t.load_all() == []
    assert t.available() == []
    # Degrades gracefully: every token becomes its raw key, dates use module defaults.
    assert t.translate("[[frozen]]", "en") == "frozen"
    assert t.date_formats("en") == (DEFAULT_DATE_FORMAT, DEFAULT_DATETIME_FORMAT)


def test_load_rejects_substitution_tokens(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad", 'frozen: "Frozen {{date}}"\n')
    with pytest.raises(TranslationLoadError, match="must not contain"):
        load_catalog(path)


def test_load_rejects_non_string_value(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad", "frozen: 123\n")
    with pytest.raises(TranslationLoadError, match="must be a string"):
        load_catalog(path)


def test_load_rejects_non_mapping(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad", "- just\n- a\n- list\n")
    with pytest.raises(TranslationLoadError, match="must be a mapping"):
        load_catalog(path)


def test_load_empty_file_is_empty_catalog(tmp_path: Path) -> None:
    path = _write(tmp_path, "empty", "")
    assert load_catalog(path) == {}


def test_load_all_skips_bad_catalog(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    d = tmp_path / "translations"
    d.mkdir()
    _write(d, "en", 'frozen: "Frozen"\n')
    _write(d, "bad", "frozen: 123\n")
    t = Translator(d, "en")
    with caplog.at_level(logging.ERROR):
        loaded = t.load_all()
    assert loaded == ["en"]  # bad catalog skipped, not fatal
    assert any("bad.yaml" in r.message for r in caplog.records)


# ── Weekday name lists (_weekdays_abbr / _weekdays_full) ─────────────────────────
def test_load_accepts_weekday_lists(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "en",
        "_weekdays_abbr: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]\n"
        "_weekdays_full: [Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday]\n",
    )
    catalog = load_catalog(path)
    assert catalog["_weekdays_abbr"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert catalog["_weekdays_full"][0] == "Monday"


def test_load_rejects_weekday_list_wrong_length(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad", "_weekdays_abbr: [Mon, Tue, Wed]\n")
    with pytest.raises(TranslationLoadError, match="7-item list"):
        load_catalog(path)


def test_load_rejects_weekday_list_non_string_items(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad", "_weekdays_abbr: [1, 2, 3, 4, 5, 6, 7]\n")
    with pytest.raises(TranslationLoadError, match="7-item list"):
        load_catalog(path)


def test_load_rejects_weekday_key_as_plain_string(tmp_path: Path) -> None:
    """The reserved list keys must be lists, not a scalar string like every other catalog value."""
    path = _write(tmp_path, "bad", '_weekdays_abbr: "Mon"\n')
    with pytest.raises(TranslationLoadError, match="7-item list"):
        load_catalog(path)


def test_weekday_names_per_language_and_fallback(tmp_path: Path) -> None:
    d = tmp_path / "translations"
    d.mkdir()
    _write(
        d,
        "en",
        'frozen: "Frozen"\n'
        "_weekdays_abbr: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]\n"
        "_weekdays_full: [Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday]\n",
    )
    # es supplies only the abbreviated list — the full list must fall back to the default
    # language's (en) catalog, mirroring date_formats()'s per-field fallback chain.
    _write(d, "es", 'frozen: "Congelado"\n_weekdays_abbr: [lun, mar, mié, jue, vie, sáb, dom]\n')
    t = Translator(d, "en")
    t.load_all()

    assert t.weekday_names("en") == (
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    )
    abbr, full = t.weekday_names("es")
    assert abbr == ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    assert full == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    # Unknown language falls back entirely to the default language's (en) lists.
    assert t.weekday_names("zz") == (
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    )


def test_weekday_names_falls_back_to_module_default_when_catalog_omits_keys(
    catalogs: Path,
) -> None:
    """The shared `catalogs` fixture carries no _weekdays_* keys — weekday_names() must degrade
    to the plain-English module defaults rather than raising or returning an empty list."""
    t = Translator(catalogs, "en")
    t.load_all()
    assert t.weekday_names("en") == (list(DEFAULT_WEEKDAYS_ABBR), list(DEFAULT_WEEKDAYS_FULL))
    assert t.weekday_names("es") == (list(DEFAULT_WEEKDAYS_ABBR), list(DEFAULT_WEEKDAYS_FULL))


def test_all_bundled_catalogs_have_complete_weekday_lists() -> None:
    """Every shipped translations/<lang>.yaml must carry both weekday reserved keys as valid
    7-item Monday-first lists. A missing/incomplete list degrades silently to English rather than
    raising, so this guards the actual shipped catalogs explicitly (a translation-completeness
    regression, not a crash, would otherwise go unnoticed)."""
    translations_dir = Path(__file__).resolve().parent.parent / "translations"
    catalog_paths = sorted(translations_dir.glob("*.yaml"))
    assert len(catalog_paths) == 8, f"expected 8 shipped catalogs, found {len(catalog_paths)}"
    for path in catalog_paths:
        catalog = load_catalog(path)
        abbr = catalog.get("_weekdays_abbr")
        full = catalog.get("_weekdays_full")
        assert isinstance(abbr, list) and len(abbr) == 7, f"{path.name}: _weekdays_abbr incomplete"
        assert isinstance(full, list) and len(full) == 7, f"{path.name}: _weekdays_full incomplete"
