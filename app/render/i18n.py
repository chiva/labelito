# SPDX-License-Identifier: GPL-3.0-or-later
"""Translation catalogs for label chrome words and locale date formats.

Templates may embed ``[[key]]`` tokens in their text; the active language's catalog
maps each key to a word. A distinct delimiter is used so these never collide with the
``{{field}}`` substitution grammar (where ``:`` already means a strftime format).

Catalogs are flat ``translations/<lang>.yaml`` files of ``key: value`` strings, plus the
reserved keys ``_date_format`` / ``_datetime_format`` that localize ``{{date}}``/``{{now}}``,
``_weekdays_abbr`` / ``_weekdays_full`` (7-item Monday-first lists) that localize the ``%a``/``%A``
strftime directives, and ``_months_abbr`` / ``_months_full`` (12-item January-first lists) that
localize ``%b``/``%B`` inside those formats. This format is intentionally trivial so a translation
platform (Weblate/Crowdin) can sync to it later without code changes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Translation token, e.g. [[frozen]] — word characters only, mirroring the field regex.
_TOKEN_RE = re.compile(r"\[\[(\w+)\]\]")

# Reserved keys carry locale metadata, not translatable vocabulary.
_DATE_FORMAT_KEY = "_date_format"
_DATETIME_FORMAT_KEY = "_datetime_format"
_WEEKDAYS_ABBR_KEY = "_weekdays_abbr"
_WEEKDAYS_FULL_KEY = "_weekdays_full"
_MONTHS_ABBR_KEY = "_months_abbr"
_MONTHS_FULL_KEY = "_months_full"

# Reserved keys whose value is an ordered list of strings rather than a plain string — everything
# else in a catalog (including _date_format/_datetime_format) must be a string. Each maps to the
# exact (length, ordering) it requires: weekdays are 7 Monday-first (index 0 = Monday, matching
# datetime.weekday()); months are 12 January-first (index 0 = January, matching datetime.month - 1).
_RESERVED_LIST_SPECS: dict[str, tuple[int, str]] = {
    _WEEKDAYS_ABBR_KEY: (7, "Monday-first"),
    _WEEKDAYS_FULL_KEY: (7, "Monday-first"),
    _MONTHS_ABBR_KEY: (12, "January-first"),
    _MONTHS_FULL_KEY: (12, "January-first"),
}
_RESERVED_LIST_KEYS = frozenset(_RESERVED_LIST_SPECS)

# Fallback date formats when a catalog omits the reserved keys (European day-first).
DEFAULT_DATE_FORMAT = "%d/%m/%Y"
DEFAULT_DATETIME_FORMAT = "%d/%m/%Y %H:%M"

# Fallback weekday names (Monday-first) when a catalog omits the reserved keys — plain C-locale
# English, matching the strftime %a/%A output a catalog-less render already produced before
# localized weekday substitution existed.
DEFAULT_WEEKDAYS_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_WEEKDAYS_FULL = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Fallback month names (January-first) when a catalog omits the reserved keys — plain C-locale
# English, matching the strftime %b/%B output a catalog-less render produced before localized
# month substitution existed.
DEFAULT_MONTHS_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
DEFAULT_MONTHS_FULL = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class TranslationLoadError(ValueError):
    pass


def load_catalog(path: Path) -> dict[str, str | list[str]]:
    """Load and validate a single ``<lang>.yaml`` catalog into a flat mapping.

    Every key is a plain string value except :data:`_RESERVED_LIST_KEYS`: the weekday lists
    (``_weekdays_abbr``/``_weekdays_full``) must be 7-item Monday-first lists of strings and the
    month lists (``_months_abbr``/``_months_full``) must be 12-item January-first lists of strings.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TranslationLoadError(f"{path.name}: YAML parse error: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TranslationLoadError(f"{path.name}: top-level must be a mapping")

    catalog: dict[str, str | list[str]] = {}
    for key, value in raw.items():
        if key in _RESERVED_LIST_KEYS:
            expected_len, ordering = _RESERVED_LIST_SPECS[key]
            if (
                not isinstance(value, list)
                or len(value) != expected_len
                or not all(isinstance(v, str) for v in value)
            ):
                raise TranslationLoadError(
                    f"{path.name}: value for {key!r} must be a {expected_len}-item list of strings "
                    f"({ordering})"
                )
            catalog[str(key)] = list(value)
            continue
        if not isinstance(value, str):
            raise TranslationLoadError(
                f"{path.name}: value for {key!r} must be a string, got {type(value).__name__}"
            )
        # Catalog values are pure vocabulary; forbidding {{…}} keeps the translation pass
        # fully decoupled from field resolution (a translator cannot inject substitutions).
        if "{{" in value or "}}" in value:
            raise TranslationLoadError(
                f"{path.name}: value for {key!r} must not contain '{{{{' or '}}}}'"
            )
        catalog[str(key)] = value
    return catalog


class Translator:
    """Hot-reloadable registry of translation catalogs keyed by lowercased language code."""

    def __init__(
        self,
        translations_dir: Path,
        default_language: str,
        example_dir: Path | None = None,
    ) -> None:
        self.translations_dir = translations_dir
        self.default_language = default_language.lower()
        # Bundled catalogs baked outside the translations_dir volume (config.example_translations_dir).
        # Loaded UNDER translations_dir so a user catalog overrides the bundled one KEY BY KEY for the
        # same language (omitted keys inherit the bundled values) and user-only languages add to it —
        # and, while bundled examples are enabled (example_dir set), the DEFAULT_LANGUAGE catalog
        # exists even against an empty translations mount (no boot hard-fail). With examples off
        # (example_dir=None) and an empty user dir there is no default catalog (softened-boot
        # contract). ``None`` / equal-to-primary means "single dir" (dev/bare-metal).
        self.example_dir = example_dir
        self._catalogs: dict[str, dict[str, str | list[str]]] = {}
        self._errors: list[str] = []

    def load_all(self) -> list[str]:
        """(Re)load all ``*.yaml`` catalogs from the bundled-example dir and the user dir.

        Catalogs that fail to parse/validate are skipped and USER-dir errors retained in
        :attr:`errors`, so a reload can report the failure instead of silently dropping a language.

        Bundled examples load FIRST and user catalogs load on top, MERGED key by key: a user catalog
        for language ``xx`` overrides the bundled ``xx`` per-key while inheriting any key it omits;
        a user-only language adds to the set. A malformed/failed bundled catalog is logged but NOT
        recorded in :attr:`errors` (shipped content must not fail ``/reload``).
        """
        loaded: dict[str, dict[str, str | list[str]]] = {}
        errors: list[str] = []

        # Bundled examples first (lower precedence). Skip when there's no separate dir (dev/bare-metal).
        if self.example_dir is not None and self.example_dir != self.translations_dir:
            self._load_dir(self.example_dir, loaded, errors, is_example=True)
        # User catalogs override the bundled ones for the same language code.
        self._load_dir(self.translations_dir, loaded, errors, is_example=False)

        if errors:
            log.warning("%d translation catalog(s) failed to load", len(errors))

        self._catalogs = loaded
        self._errors = errors
        return list(loaded.keys())

    def _load_dir(
        self,
        directory: Path,
        loaded: dict[str, dict[str, str | list[str]]],
        errors: list[str],
        *,
        is_example: bool,
    ) -> None:
        """Load ``directory/<lang>.yaml`` into ``loaded``, keyed by lowercased stem. A later call for
        the same language MERGES over the earlier one KEY BY KEY (that is how user dirs override
        bundled ones): a user catalog's keys win, and any key it omits is inherited from the bundled
        catalog. Bundled-dir failures are logged but never appended to ``errors``."""
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.yaml")):
            lang = path.stem.lower()
            try:
                # Key-level merge, not whole-file replace: a stale user catalog missing newer
                # reserved keys (e.g. _weekdays_*/_months_* added in a later release) still inherits
                # them from the bundled catalog instead of shadowing it into English fallbacks.
                loaded[lang] = {**loaded.get(lang, {}), **load_catalog(path)}
                log.debug("Loaded translation catalog %r from %s", lang, path.name)
            except TranslationLoadError as exc:
                log.error("Failed to load translation catalog %s: %s", path.name, exc)
                if not is_example:
                    errors.append(str(exc))

    @property
    def errors(self) -> list[str]:
        """Per-file errors from the most recent :meth:`load_all` (empty if all loaded)."""
        return self._errors

    def has(self, language: str) -> bool:
        return language.lower() in self._catalogs

    def available(self) -> list[str]:
        return sorted(self._catalogs.keys())

    def translate(self, text: str, language: str) -> str:
        """Replace every ``[[key]]`` token using the requested language's catalog.

        Per-token fallback chain: requested language → default language → the raw key
        (logged). Never raises, so a missing catalog or key degrades gracefully.
        """
        lang = language.lower()

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            for candidate in (lang, self.default_language):
                catalog = self._catalogs.get(candidate)
                if catalog is not None and key in catalog:
                    value = catalog[key]
                    if isinstance(value, str):
                        return value
                    # A reserved list-valued key (_weekdays_abbr/_weekdays_full) is locale
                    # metadata, not translatable vocabulary — fall through to the missing-key
                    # warning below rather than substituting a list into the rendered text.
                    break
            log.warning(
                "Translation key %r missing for language %r (and default %r); using raw key",
                key,
                lang,
                self.default_language,
            )
            return key

        return _TOKEN_RE.sub(replace, text)

    def date_formats(self, language: str) -> tuple[str, str]:
        """Return ``(date_format, datetime_format)`` for a language.

        Falls back to the default language's catalog, then to the module defaults.
        """
        lang = language.lower()
        date_fmt = DEFAULT_DATE_FORMAT
        datetime_fmt = DEFAULT_DATETIME_FORMAT
        for candidate in (self.default_language, lang):  # requested wins (applied last)
            catalog = self._catalogs.get(candidate)
            if catalog is None:
                continue
            raw_date_fmt = catalog.get(_DATE_FORMAT_KEY, date_fmt)
            if isinstance(raw_date_fmt, str):
                date_fmt = raw_date_fmt
            raw_datetime_fmt = catalog.get(_DATETIME_FORMAT_KEY, datetime_fmt)
            if isinstance(raw_datetime_fmt, str):
                datetime_fmt = raw_datetime_fmt
        return date_fmt, datetime_fmt

    def weekday_names(self, language: str) -> tuple[list[str], list[str]]:
        """Return ``(weekdays_abbr, weekdays_full)`` for a language, both Monday-first 7-item lists.

        Same fallback chain as :meth:`date_formats`: requested language → default language →
        module defaults (plain C-locale English, matching un-localized ``%a``/``%A`` output).
        """
        lang = language.lower()
        abbr: list[str] = list(DEFAULT_WEEKDAYS_ABBR)
        full: list[str] = list(DEFAULT_WEEKDAYS_FULL)
        for candidate in (self.default_language, lang):  # requested wins (applied last)
            catalog = self._catalogs.get(candidate)
            if catalog is None:
                continue
            raw_abbr = catalog.get(_WEEKDAYS_ABBR_KEY)
            if isinstance(raw_abbr, list):
                abbr = list(raw_abbr)  # copy, don't alias the shared catalog's internal list
            raw_full = catalog.get(_WEEKDAYS_FULL_KEY)
            if isinstance(raw_full, list):
                full = list(raw_full)  # same: the singleton translator is shared across requests
        return abbr, full

    def month_names(self, language: str) -> tuple[list[str], list[str]]:
        """Return ``(months_abbr, months_full)`` for a language, both January-first 12-item lists.

        Same fallback chain as :meth:`weekday_names`: requested language → default language →
        module defaults (plain C-locale English, matching un-localized ``%b``/``%B`` output).
        """
        lang = language.lower()
        abbr: list[str] = list(DEFAULT_MONTHS_ABBR)
        full: list[str] = list(DEFAULT_MONTHS_FULL)
        for candidate in (self.default_language, lang):  # requested wins (applied last)
            catalog = self._catalogs.get(candidate)
            if catalog is None:
                continue
            raw_abbr = catalog.get(_MONTHS_ABBR_KEY)
            if isinstance(raw_abbr, list):
                abbr = list(raw_abbr)  # copy, don't alias the shared catalog's internal list
            raw_full = catalog.get(_MONTHS_FULL_KEY)
            if isinstance(raw_full, list):
                full = list(raw_full)  # same: the singleton translator is shared across requests
        return abbr, full

    def __len__(self) -> int:
        return len(self._catalogs)
