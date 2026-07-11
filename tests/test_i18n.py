# SPDX-License-Identifier: GPL-3.0-or-later
"""Translator unit tests — catalog loading, fallback chain, locale date formats."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from app.render.i18n import (
    DEFAULT_DATETIME_FORMAT,
    DEFAULT_MONTHS_ABBR,
    DEFAULT_MONTHS_FULL,
    DEFAULT_WEEKDAYS_ABBR,
    DEFAULT_WEEKDAYS_FULL,
    TranslationLoadError,
    Translator,
    load_catalog,
)

# A full 12-item January-first Spanish month pair, reused across the month-localization tests.
_ES_MONTHS_ABBR = "[ene, feb, mar, abr, may, jun, jul, ago, sep, oct, nov, dic]"
_ES_MONTHS_FULL = (
    "[enero, febrero, marzo, abril, mayo, junio, "
    "julio, agosto, septiembre, octubre, noviembre, diciembre]"
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


# ── Bundled-example catalog merge (translations_dir + example_dir) ────────────────
def test_translator_provides_default_language_when_user_dir_empty(tmp_path: Path) -> None:
    """An empty (bind-mounted) translations dir must not drop the DEFAULT_LANGUAGE catalog: the
    bundled example supplies it, so has(default) stays true and the service no longer crashes on boot."""
    user = tmp_path / "translations"
    user.mkdir()
    examples = tmp_path / "examples"
    examples.mkdir()
    _write(examples, "en", 'frozen: "Frozen"\n')

    t = Translator(user, "en", examples)
    assert t.load_all() == ["en"]
    assert t.has("en")


def test_translator_user_overrides_example_for_same_language(tmp_path: Path) -> None:
    """A user catalog overrides the bundled one KEY BY KEY (loaded on top): a user-provided key wins,
    a key present only in the bundled catalog is inherited, and a user-only language is added
    alongside."""
    user = tmp_path / "translations"
    user.mkdir()
    examples = tmp_path / "examples"
    examples.mkdir()
    _write(examples, "en", 'frozen: "Frozen (shipped)"\nexpires: "Expires (shipped)"\n')
    _write(examples, "de", 'frozen: "Gefroren"\n')
    _write(user, "en", 'frozen: "Frozen (mine)"\n')

    t = Translator(user, "en", examples)
    assert sorted(t.load_all()) == ["de", "en"]
    assert t.translate("[[frozen]]", "en") == "Frozen (mine)"  # user key wins
    assert t.translate("[[expires]]", "en") == "Expires (shipped)"  # omitted key inherited
    assert t.translate("[[frozen]]", "de") == "Gefroren"  # bundled-only language kept


def test_translator_stale_user_catalog_inherits_bundled_reserved_lists(tmp_path: Path) -> None:
    """Regression: a stale user catalog that predates the %a/%b feature (only ``frozen``, no
    ``_weekdays_*``/``_months_*``) must inherit the bundled locale lists per-key instead of shadowing
    them into English fallbacks — the "Sat, 11 Jul" bug. Vocabulary from the user file still wins."""
    user = tmp_path / "translations"
    user.mkdir()
    examples = tmp_path / "examples"
    examples.mkdir()
    _write(
        examples,
        "es",
        'frozen: "Congelado (shipped)"\n'
        "_weekdays_abbr: [lun, mar, mié, jue, vie, sáb, dom]\n"
        "_weekdays_full: [lunes, martes, miércoles, jueves, viernes, sábado, domingo]\n"
        f"_months_abbr: {_ES_MONTHS_ABBR}\n"
        f"_months_full: {_ES_MONTHS_FULL}\n",
    )
    _write(user, "es", 'frozen: "Congelado"\n')  # stale: vocabulary only, no reserved lists

    t = Translator(user, "es", examples)
    assert t.load_all() == ["es"]
    assert t.translate("[[frozen]]", "es") == "Congelado"  # user vocabulary still wins
    abbr, full = t.weekday_names("es")
    assert abbr[5] == "sáb" and full[5] == "sábado"  # inherited, not English "Sat"/"Saturday"
    months_abbr, months_full = t.month_names("es")
    assert months_abbr[6] == "jul" and months_full[6] == "julio"  # inherited, not English "Jul"


def test_translator_example_dir_equal_to_user_loads_once(catalogs: Path) -> None:
    t = Translator(catalogs, "en", catalogs)
    assert sorted(t.load_all()) == ["en", "es"]


def test_translator_example_dir_none_loads_only_user(tmp_path: Path) -> None:
    """LOAD_EXAMPLES=false is wired as example_dir=None: bundled catalogs on disk are never scanned."""
    user = tmp_path / "translations"
    user.mkdir()
    examples = tmp_path / "examples"
    examples.mkdir()
    _write(examples, "de", 'frozen: "Gefroren"\n')
    _write(user, "en", 'frozen: "Frozen"\n')

    t = Translator(user, "en", None)
    assert t.load_all() == ["en"]  # user only; the bundled 'de' is skipped
    assert not t.has("de")


def test_translator_no_default_catalog_degrades_to_raw_key(tmp_path: Path) -> None:
    """With examples off and an empty translations dir there is no default catalog: load_all must not
    raise, has(default) is False, and translate() renders the raw key (the softened-boot contract)."""
    user = tmp_path / "translations"
    user.mkdir()

    t = Translator(user, "en", None)
    assert t.load_all() == []
    assert not t.has("en")
    assert t.translate("[[frozen]]: today", "en") == "frozen: today"


def test_translator_malformed_example_not_in_errors(tmp_path: Path) -> None:
    """A malformed bundled catalog is logged but not recorded in errors (shipped content must not fail
    /reload); a malformed USER catalog still is."""
    user = tmp_path / "translations"
    user.mkdir()
    examples = tmp_path / "examples"
    examples.mkdir()
    _write(examples, "en", 'frozen: "Frozen"\n')
    (examples / "de.yaml").write_text(": : not a mapping :")  # malformed bundled

    t = Translator(user, "en", examples)
    t.load_all()
    assert t.has("en")  # the valid bundled catalog still loads
    assert t.errors == []  # the malformed bundled 'de' failure is not user-actionable
    assert not t.has("de")  # and it did not register


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


# ── Month name lists (_months_abbr / _months_full) ───────────────────────────────
def test_load_accepts_month_lists(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "en",
        "_months_abbr: [Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec]\n"
        "_months_full: [January, February, March, April, May, June, July, August, "
        "September, October, November, December]\n",
    )
    catalog = load_catalog(path)
    assert catalog["_months_abbr"][0] == "Jan"
    assert catalog["_months_full"][11] == "December"


def test_load_rejects_month_list_wrong_length(tmp_path: Path) -> None:
    # 7 items is a valid weekday length but wrong for months — the per-key spec must reject it.
    path = _write(tmp_path, "bad", "_months_abbr: [Jan, Feb, Mar, Apr, May, Jun, Jul]\n")
    with pytest.raises(TranslationLoadError, match="12-item list"):
        load_catalog(path)


def test_load_rejects_month_list_non_string_items(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad", "_months_full: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]\n")
    with pytest.raises(TranslationLoadError, match="12-item list"):
        load_catalog(path)


def test_load_rejects_month_key_as_plain_string(tmp_path: Path) -> None:
    """The reserved month list keys must be lists, not a scalar string."""
    path = _write(tmp_path, "bad", '_months_abbr: "Jan"\n')
    with pytest.raises(TranslationLoadError, match="12-item list"):
        load_catalog(path)


def test_month_names_per_language_and_fallback(tmp_path: Path) -> None:
    d = tmp_path / "translations"
    d.mkdir()
    _write(
        d,
        "en",
        'frozen: "Frozen"\n'
        "_months_abbr: [Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec]\n"
        "_months_full: [January, February, March, April, May, June, July, August, "
        "September, October, November, December]\n",
    )
    # es supplies only the abbreviated list — the full list must fall back to the default
    # language's (en) catalog, mirroring weekday_names()'s per-field fallback chain.
    _write(d, "es", f'frozen: "Congelado"\n_months_abbr: {_ES_MONTHS_ABBR}\n')
    t = Translator(d, "en")
    t.load_all()

    assert t.month_names("en") == (
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        list(DEFAULT_MONTHS_FULL),
    )
    abbr, full = t.month_names("es")
    assert abbr == [
        "ene",
        "feb",
        "mar",
        "abr",
        "may",
        "jun",
        "jul",
        "ago",
        "sep",
        "oct",
        "nov",
        "dic",
    ]
    assert full == list(DEFAULT_MONTHS_FULL)  # es omitted _months_full → en fallback
    # Unknown language falls back entirely to the default language's (en) lists.
    assert t.month_names("zz")[0][6] == "Jul"


def test_month_names_falls_back_to_module_default_when_catalog_omits_keys(
    catalogs: Path,
) -> None:
    """The shared `catalogs` fixture carries no _months_* keys — month_names() must degrade to the
    plain-English module defaults rather than raising or returning an empty list."""
    t = Translator(catalogs, "en")
    t.load_all()
    assert t.month_names("en") == (list(DEFAULT_MONTHS_ABBR), list(DEFAULT_MONTHS_FULL))
    assert t.month_names("es") == (list(DEFAULT_MONTHS_ABBR), list(DEFAULT_MONTHS_FULL))


def test_all_bundled_catalogs_have_complete_month_lists() -> None:
    """Every shipped translations/<lang>.yaml must carry both month reserved keys as valid 12-item
    January-first lists — the month counterpart to the weekday-completeness guard. A missing list
    degrades silently to English, so this pins the shipped catalogs explicitly."""
    translations_dir = Path(__file__).resolve().parent.parent / "translations"
    catalog_paths = sorted(translations_dir.glob("*.yaml"))
    assert len(catalog_paths) == 8, f"expected 8 shipped catalogs, found {len(catalog_paths)}"
    for path in catalog_paths:
        catalog = load_catalog(path)
        abbr = catalog.get("_months_abbr")
        full = catalog.get("_months_full")
        assert isinstance(abbr, list) and len(abbr) == 12, f"{path.name}: _months_abbr incomplete"
        assert isinstance(full, list) and len(full) == 12, f"{path.name}: _months_full incomplete"
