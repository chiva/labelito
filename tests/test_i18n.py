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
