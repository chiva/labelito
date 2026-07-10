# SPDX-License-Identifier: GPL-3.0-or-later
"""Label render engine: template + fields → PIL Image (grayscale, label dimensions)."""

from __future__ import annotations

import calendar
import io
import re
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from app.render.elements import (
    ElementBase,
    _apply_padding,
    build_element,
    resolve_custom_icon_path,
)
from app.render.i18n import Translator


# Minimum upper bound on raster rows across ALL Brother QL models supported by brother_ql.
# This is the conservative fallback used when the configured model cannot be resolved: it equals
# the most restrictive (smallest) per-model limit, so no unknown model can ever be exceeded.
# At 300 dpi this is 11811 rows ≈ 1000 mm; at 600 dpi the same row count prints only ~500 mm
# (each row advances 1/600" rather than 1/300"), so the physical max length is halved but the
# raster row limit stays fixed.  BrotherQLRaster.add_raster_data raises BrotherQLRasterError if
# image.size[1] > model.min_max_length_dots[1], so render_max in high_res ENDLESS mode must
# never exceed the configured model's limit regardless of what max_length_px * scale evaluates to.
def _brother_ql_max_rows() -> int:
    try:
        from brother_ql.models import ModelsManager

        mm = ModelsManager()
        return int(min(mm[ident].min_max_length_dots[1] for ident in mm.iter_identifiers()))
    except Exception:  # pragma: no cover - library unavailable in stripped test environments
        return 11811  # known value for all sub-1050 QL models; safe fallback


_BROTHER_QL_MAX_RASTER_ROWS: int = _brother_ql_max_rows()


def _brother_ql_model_max_rows(model_identifier: str) -> int:
    """Return the raster-row ceiling for a specific Brother QL model identifier.

    Uses ``ModelsManager()[identifier].min_max_length_dots[1]`` — the per-model upper bound
    that ``BrotherQLRaster.add_raster_data`` enforces at conversion time.  Wide/long-format
    models (QL-1050/1060/1100-class) accept up to 35433-35434 rows, roughly 3x the sub-1050
    limit.  Using the global minimum for those models silently clips continuous labels longer
    than ~500 mm at 600 dpi even though the printer would accept them.

    Falls back to ``_BROTHER_QL_MAX_RASTER_ROWS`` (the conservative global minimum, 11811) if
    the identifier is unknown or the library is unavailable, so an unresolvable model never
    exceeds a safe limit.
    """
    try:
        from brother_ql.models import ModelsManager

        mm = ModelsManager()
        return int(mm[model_identifier].min_max_length_dots[1])
    except Exception:
        # Unknown model or library issue: fall back to the most restrictive known limit so we
        # never exceed an unknown model's ceiling.
        return _BROTHER_QL_MAX_RASTER_ROWS


# Computed field pattern, e.g. {{date}}, {{date+6m}}, {{now:%H:%M}}.
#   group 1 = key (field name or date/now)
#   group 2 = optional date offset, ±N followed by d(ays)/w(eeks)/m(onths)/y(ears)
#   group 3 = optional strftime format
_FIELD_RE = re.compile(r"\{\{(\w+)([+-]\d+[dwmy])?(?::([^}]*))?\}\}")

# Tokens the engine resolves automatically (not from request fields); always available.
# ``date`` and ``now`` resolve from the clock; ``seq`` resolves per-item in sequence batches
# and to "" in single-item renders. All are excluded from the required-field contract computed
# by the loader (referenced_field_tokens → unresolved_tokens) so templates using them never
# demand a user-supplied "seq" field.
COMPUTED_TOKENS = frozenset({"date", "now", "seq"})

# Layout element attributes that carry substitutable {{token}} strings (see _resolve_all).
_TEMPLATED_ATTRS = ("text", "data", "name")

_DATE_FORMAT = "%d/%m/%Y"
_DATETIME_FORMAT = "%d/%m/%Y %H:%M"

# Fallback weekday/month names mirroring app.render.i18n's module defaults, used when no
# language-specific lists are supplied — plain C-locale English, matching un-localized strftime.
_WEEKDAYS_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAYS_FULL = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_FULL = (
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


def format_seq(start: int, index: int, step: int, padding: int) -> str:
    """Format the ``{{seq}}`` value for item ``index`` of a sequence batch.

    The value is ``start + index * step``, zero-padded to ``padding`` digits when ``padding > 0``
    (negative values keep the sign in front of the digits, matching ``str.zfill``). This is the
    single source of truth for how a sequence item is numbered, shared by :meth:`RenderEngine.
    render_sequence` (lazy generator) and the per-label print loop in ``app.main`` so both number
    items identically.
    """
    value = start + index * step
    return str(value).zfill(padding) if padding > 0 else str(value)


def referenced_field_tokens(layout: list[dict[str, Any]]) -> set[str]:
    """Field names referenced by ``{{tokens}}`` in text/data/name attrs (recursing into children).

    Excludes :data:`COMPUTED_TOKENS` (clock-derived, always available). These are the fields whose
    values are substituted into *rendered text*, so they are the fields that must obey the
    text-size cap regardless of whatever else they may also feed (e.g. an image element).
    """
    refs: set[str] = set()

    def scan(element: dict[str, Any]) -> None:
        for attr in _TEMPLATED_ATTRS:
            value = element.get(attr)
            if isinstance(value, str):
                for match in _FIELD_RE.finditer(value):
                    key = match.group(1)
                    if key not in COMPUTED_TOKENS:
                        refs.add(key)
        children = element.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    scan(child)

    for element in layout:
        scan(element)
    return refs


def uses_seq(layout: list[dict[str, Any]]) -> bool:
    """Return True if *layout* references the ``{{seq}}`` auto-numbering token anywhere.

    Unlike :func:`referenced_field_tokens` — which deliberately *excludes* :data:`COMPUTED_TOKENS`
    so ``seq`` never appears in the required-field contract — this walker specifically looks for
    ``{{seq}}`` (with optional ``±offset``/``:fmt`` groups, matching :data:`_FIELD_RE`).  It exists
    so the print path can reject a ``{{seq}}`` template submitted *without* a ``sequence`` spec:
    such a render would resolve ``{{seq}}`` to "" and silently print a blank-numbered label.

    Scans the same templated attrs (``text``/``data``/``name``) the renderer substitutes, recursing
    into ``row`` children, so detection tracks exactly what is drawn.
    """

    def scan(element: dict[str, Any]) -> bool:
        for attr in _TEMPLATED_ATTRS:
            value = element.get(attr)
            if isinstance(value, str):
                if any(match.group(1) == "seq" for match in _FIELD_RE.finditer(value)):
                    return True
        children = element.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict) and scan(child):
                    return True
        return False

    return any(isinstance(el, dict) and scan(el) for el in layout)


def image_field_names(layout: list[dict[str, Any]]) -> set[str]:
    """Field names read by ``image`` elements (default ``image``), recursing into container children.

    Containers (``row``/``column``) render their children, so the walk descends into any ``children``
    list — exactly the subtree the renderer builds — keeping image-field discovery, request
    validation, and history blob-stripping in lockstep with what is actually drawn.
    """
    names: set[str] = set()
    for el in layout:
        if not isinstance(el, dict):
            continue
        if el.get("type") == "image":
            # Use the field identity verbatim — never str()-coerce. The renderer keeps the raw
            # dataclass value, so coercing a malformed (e.g. list) field here would make the guards
            # track a different identity than ImageElement.render reads. A non-string field is left
            # out (the loader rejects it up front); default to the canonical ``image`` when absent.
            field = el.get("field", "image")
            if isinstance(field, str) and field:
                names.add(field)
        children = el.get("children")
        if isinstance(children, list):
            names |= image_field_names(children)
    return names


def missing_custom_icons(layout: list[dict[str, Any]], icons_dir: Path) -> set[str]:
    """Static custom-asset icon names in *layout* with no matching file in *icons_dir*.

    A custom-asset icon is an ``icon`` element with no ``collection``: it loads ``<name>.svg`` then
    ``<name>.png`` from ``icons_dir`` (see :class:`~app.render.elements.IconElement`). Only STATIC
    names are reported — a ``{{token}}``-driven name is request-controlled and unknowable at boot,
    and collection icons are skipped (their files are baked image content, absent in dev/test by
    design). Recurses into container (``row``/``column``) children, mirroring the subtree the renderer
    actually draws.

    Powers the boot warning that surfaces a bind-mounted ``assets/icons`` which omits a file a
    template names — the silently-blank case (see ``app.main.startup``). Loaded templates have
    already had their static icon names sanitized by the loader, so no re-validation is needed here.
    """
    missing: set[str] = set()
    for el in layout:
        if not isinstance(el, dict):
            continue
        if el.get("type") == "icon" and not el.get("collection"):
            name = el.get("name")
            if isinstance(name, str) and name and "{{" not in name:
                if resolve_custom_icon_path(name, icons_dir) is None:
                    missing.add(name)
        children = el.get("children")
        if isinstance(children, list):
            missing |= missing_custom_icons(children, icons_dir)
    return missing


def unresolved_tokens(layout: list[dict[str, Any]], declared_fields: list[str]) -> list[str]:
    """Return the sorted ``{{token}}`` keys in *layout* that nothing can ever fill.

    A token resolves only if it is a :data:`COMPUTED_TOKENS` member (clock-derived) or names a
    declared field. Anything else — a typo (``{{titel}}``) or a removed feature (``{{counter}}``)
    — would otherwise substitute to an empty string at render time and print a silently blank
    label. Used by the loader to reject such templates loudly instead.
    """
    declared = set(declared_fields)
    return sorted(key for key in referenced_field_tokens(layout) if key not in declared)


# Loose detector for a ``{{ ... }}``-shaped span — anything that LOOKS like a placeholder. Used only
# to find MALFORMED placeholders: a span that does not fully match _FIELD_RE is one the substitution
# pass (_FIELD_RE.finditer) silently skips, so it would print on the label verbatim. Non-greedy so
# ``{{a}}{{b}}`` yields two spans, not one. ``[\s\S]`` (not ``.``) so the span can cross a YAML
# literal-block newline: ``{{asset-\nid}}`` is just as unsubstitutable as ``{{asset-id}}`` and must
# be caught too. A valid newline-bearing token (e.g. ``{{date:%Y\n%m}}``) still fullmatches _FIELD_RE
# — whose ``:fmt`` group is ``[^}]*`` and already spans newlines — so it is correctly NOT flagged.
_PLACEHOLDER_SPAN_RE = re.compile(r"\{\{[\s\S]*?\}\}")


def malformed_placeholders(layout: list[dict[str, Any]]) -> list[str]:
    """Return the sorted ``{{...}}`` spans in *layout* the engine could never substitute.

    The substitution pass matches only :data:`_FIELD_RE` (``{{\\w+}}`` with optional ±offset/:fmt),
    so a placeholder-shaped span with a hyphen/dot/space in the name (``{{asset-id}}``), stray inner
    spaces (``{{ title }}``), or other typo produces NO match and is left on the printed label
    verbatim — a wrong-label failure, not cosmetic. The loader rejects these up front. Walks the same
    templated attrs (text/data/name) the renderer substitutes, recursing into row children, so
    detection tracks exactly what is drawn.
    """
    bad: set[str] = set()

    def scan(element: dict[str, Any]) -> None:
        for attr in _TEMPLATED_ATTRS:
            value = element.get(attr)
            if isinstance(value, str):
                for span in _PLACEHOLDER_SPAN_RE.finditer(value):
                    if not _FIELD_RE.fullmatch(span.group(0)):
                        bad.add(span.group(0))
        children = element.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    scan(child)

    for element in layout:
        if isinstance(element, dict):
            scan(element)
    return sorted(bad)


def _add_months(base: datetime, months: int) -> datetime:
    """Add (or subtract) calendar months, clamping the day to the target month's length."""
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return base.replace(year=year, month=month, day=min(base.day, last_day))


def _apply_offset(base: datetime, offset: str) -> datetime:
    """Shift a datetime by an offset like '+6m' or '-14d' (d=days, w=weeks, m=months, y=years)."""
    amount = int(offset[:-1])  # leading sign kept by int()
    unit = offset[-1]
    if unit == "d":
        return base + timedelta(days=amount)
    if unit == "w":
        return base + timedelta(weeks=amount)
    if unit == "m":
        return _add_months(base, amount)
    return _add_months(base, amount * 12)  # unit == "y" (regex permits only dwmy)


def _resolve_fields(
    template_str: str,
    fields: dict[str, Any],
    now: datetime,
    date_fmt: str = _DATE_FORMAT,
    datetime_fmt: str = _DATETIME_FORMAT,
    seq: str = "",
    weekday_abbr: Sequence[str] = _WEEKDAYS_ABBR,
    weekday_full: Sequence[str] = _WEEKDAYS_FULL,
    month_abbr: Sequence[str] = _MONTHS_ABBR,
    month_full: Sequence[str] = _MONTHS_FULL,
) -> str:
    """Substitute {{field}}, {{date[±Nunit]}}, {{now[±Nunit][:fmt]}}, {{seq}} in a string.

    Resolution is pure: ``now`` is the reference instant for ``{{date}}``/``{{now}}``, injected
    by the caller so the same string renders identically on preview, print, and reprint (no clock
    reads happen here).  ``seq`` is the pre-formatted sequence string for the current item (empty
    for non-sequence renders).

    ``date_fmt``/``datetime_fmt`` are the active locale's default formats for ``{{date}}``
    and ``{{now}}``; an explicit ``{{date:%fmt}}`` in the template still overrides them.

    ``weekday_abbr``/``weekday_full`` are the active locale's Monday-first weekday name lists
    (index 0 = Monday, matching ``datetime.weekday()``) and ``month_abbr``/``month_full`` are its
    January-first month name lists (index 0 = January, matching ``datetime.month - 1``), all
    defaulting to plain English. When the effective strftime format contains ``%a``/``%A`` (or
    ``%b``/``%B``) the localized name is substituted into the format string BEFORE calling
    ``strftime`` — Python's ``strftime`` always emits the C-locale (English) weekday/month name
    regardless of the process locale, so leaving it to ``strftime`` would silently ignore the
    label's language.
    """

    def replace(match: re.Match[str]) -> str:
        key, offset, fmt = match.group(1), match.group(2), match.group(3)
        if key in ("date", "now"):
            moment = now
            if offset:
                moment = _apply_offset(moment, offset)
            effective_fmt = fmt or (date_fmt if key == "date" else datetime_fmt)
            if "%a" in effective_fmt or "%A" in effective_fmt:
                weekday = moment.weekday()  # Monday=0, matching the Monday-first name lists
                effective_fmt = effective_fmt.replace("%A", weekday_full[weekday])
                effective_fmt = effective_fmt.replace("%a", weekday_abbr[weekday])
            if "%b" in effective_fmt or "%B" in effective_fmt:
                month = moment.month - 1  # January=0, matching the January-first name lists
                effective_fmt = effective_fmt.replace("%B", month_full[month])
                effective_fmt = effective_fmt.replace("%b", month_abbr[month])
            return moment.strftime(effective_fmt)
        if key == "seq":
            return seq
        value = fields.get(key, "")
        return str(value) if value is not None else ""

    return _FIELD_RE.sub(replace, template_str)


class RenderEngine:
    def __init__(
        self,
        fonts_dir: Path,
        icons_dir: Path,
        icon_collections_dir: Path,
        translator: Translator,
        min_length_px: int = 200,
        max_length_px: int = 6000,
        max_raster_rows: int = _BROTHER_QL_MAX_RASTER_ROWS,
    ) -> None:
        self.fonts_dir = fonts_dir
        self.icons_dir = icons_dir
        self.icon_collections_dir = icon_collections_dir
        self.translator = translator
        self.min_length_px = min_length_px
        self.max_length_px = max_length_px
        # Per-model raster-row ceiling for high_res ENDLESS mode.  Derived from the configured
        # printer model's min_max_length_dots[1]; wide-format models (QL-1100-class) allow up to
        # ~35434 rows vs the sub-1050 default of 11811.  Falls back to the global minimum when the
        # model cannot be resolved so we never exceed an unknown model's limit.
        self.max_raster_rows = max_raster_rows

    # ── Public API ──────────────────────────────────────────────────────────────
    def render(
        self,
        layout: list[dict[str, Any]],
        fields: dict[str, Any],
        canvas_width: int,
        canvas_height: int | None,  # None = continuous
        rotate: int = 0,
        language: str | None = None,
        *,
        now: datetime | None = None,
        high_res: bool = False,
        red: bool = False,
        seq: str = "",
        valign: str = "top",
    ) -> Image.Image:
        # Two-color (red/black) two-layer rendering.
        #
        # When ``red`` is True the WHOLE canvas (every element strip and the composed image) is
        # rendered in "RGB" with a white background: elements marked ``color: red`` paste pure-red
        # (255,0,0) ink, everything else pure black, so brother_ql's convert(red=True) separates the
        # red ink into its own raster layer for QL-800/810W/820NWB + DK-22251 black/red media. When
        # ``red`` is False the canvas is "L" exactly as before — a ``color: red`` element prints
        # black, and the output is byte-identical to a monochrome render. The mode is threaded
        # uniformly through build_element → each element's ``_red_active`` (incl. row children) and
        # into ``_compose`` so the composed image matches the strips it pastes. ``red`` composes
        # orthogonally with high_res (RGB at the 2x-scaled size) and with dither/threshold, which
        # brother_ql applies to the separated black layer.
        #
        # 600 dpi high-resolution scaling.
        #
        # Approach — UNIFORM GEOMETRY (not field-by-field patching). When high_res is on we render
        # the ENTIRE label coordinate system at 2x linear scale: canvas width, feed-axis height,
        # every element dimension (incl. dataclass DEFAULTS like Title/Subtitle font size), AND the
        # length clamps are all computed at the doubled scale. The scale factor is threaded into
        # build_element → each element's `scale` field → `self._px(...)` in every renderer, so no
        # dimension can be left behind at 300 dpi. scale=1 is an exact no-op, so high_res=False
        # output stays byte-identical to the 300 dpi render.
        #
        # Why both axes double — confirmed against brother_ql.conversion.convert(dpi_600=True):
        #
        #   For ANY form factor the library first computes the per-axis dot count for the chosen
        #   physical label, then for dpi_600 it does `im.resize((im.size[0]//2, im.size[1]))` —
        #   HALVING the width (print-head axis) while KEEPING the height (feed axis = raster rows).
        #   It then feeds `im.size[1]` rows to add_media_and_quality and sets the dpi_600 flag (bit 6
        #   of expanded mode), which puts the printer in 300x600 dpi mode: the feed advances at
        #   600 dpi, so each raster ROW is 1/600" of feed instead of 1/300". To print the SAME
        #   physical length you therefore need DOUBLE the rows. Hence the feed/height axis must be
        #   doubled too — the previous code left it single and printed every continuous label at HALF
        #   its intended length with the wrong length clamps (Bug 1).
        #
        #   ENDLESS (continuous, canvas_height=None):
        #     Input must be (dots_printable[0]*2, H). Width is checked == dots_printable[0] after the
        #     internal halving, so width doubles. H is unconstrained by label geometry but IS bounded
        #     by the model's raster-row limit (BrotherQLRaster.add_raster_data raises
        #     BrotherQLRasterError if image.size[1] > model.min_max_length_dots[1]).  feed-length
        #     clamps are scaled to 600 dpi dot counts (min*2), but the max is further capped at
        #     self.max_raster_rows (derived from the configured model, e.g. 11811 for QL-810W,
        #     ~35434 for QL-1100-class) so that e.g. max_length_px=6000 * 2 = 12000 never exceeds
        #     the sub-1050 hard limit, while wide-format models are not needlessly clipped.
        #
        #   DIE_CUT (canvas_height set):
        #     dots_expected = [el*2 for el in dots_printable]; the library raises ValueError unless
        #     im.size == dots_expected. So input must be exactly (dots_printable[0]*2,
        #     dots_printable[1]*2) — both axes doubled, content at 2x. The library then halves width;
        #     height stays doubled (the doubled rows print the unchanged physical length at 600 dpi
        #     feed). Length clamps are inert for die-cut (exact canvas height).
        #
        # Rotation: the driver rotates (rotate != 0 is forwarded to convert()); the engine itself
        # does NOT apply PIL rotation when high_res is active (rotate=0 in _execute_print always).
        # For preview (where PIL rotation IS applied here), high_res is never set, so the rotate
        # branch and the high_res branch never interact.
        if high_res:
            scale = 2
            render_width = canvas_width * scale
            # Feed axis doubles in both cases: ENDLESS doubles rows for the same physical length;
            # DIE_CUT doubles height to match dots_expected.
            render_height = canvas_height * scale if canvas_height is not None else None
            # Length clamps are feed-axis dot counts; scale them to 600 dpi so a short label keeps
            # its full physical minimum length (not half) and a long label is not clipped at the
            # 300 dpi cap. Inert for die-cut (exact canvas height), correct for ENDLESS.
            render_min = self.min_length_px * scale
            # Cap the ENDLESS max at the model-specific raster-row ceiling (self.max_raster_rows).
            # 600 dpi mode does NOT raise the row ceiling — the printer accepts at most
            # model.min_max_length_dots[1] rows regardless of dpi_600.  Without this cap,
            # max_length_px=6000 (default) would produce a 12000-row image that exceeds the
            # sub-1050 limit of 11811 and BrotherQLRaster.add_raster_data raises BrotherQLRasterError.
            # Wide-format models (QL-1100-class, limit ~35434) may allow more rows; using the
            # global minimum for those silently clips labels that the printer would accept.
            # self.max_raster_rows is set from the configured model at construction (via
            # _brother_ql_model_max_rows) and falls back to the conservative global minimum (11811)
            # for unknown models.  Truncation is the existing behaviour, so clamping is consistent.
            render_max = min(self.max_length_px * scale, self.max_raster_rows)
            # The scaled MINIMUM must also respect the model row ceiling. A fixed-length
            # config (e.g. min_length_px == max_length_px) would otherwise compose to
            # min_length_px * 2 rows — _compose returns max(render_min, min(total, render_max)) —
            # which can exceed the limit and crash convert(dpi_600=True) with BrotherQLRasterError.
            # Clamping render_min to the capped render_max keeps the composed height ≤ the ceiling.
            render_min = min(render_min, render_max)
        else:
            scale = 1
            render_width = canvas_width
            render_height = canvas_height
            render_min = self.min_length_px
            render_max = self.max_length_px

        lang = language or self.translator.default_language
        moment = now if now is not None else datetime.now()
        elements = [build_element(spec, scale=scale, red_active=red) for spec in layout]
        resolved = self._resolve_all(elements, fields, lang, moment, seq)
        strips = self._render_elements(elements, resolved, render_width)
        img = self._compose(
            strips, render_width, render_height, render_min, render_max, red=red, valign=valign
        )
        if rotate:
            img = img.rotate(rotate, expand=True)
        return img

    def render_to_png(
        self,
        layout: list[dict[str, Any]],
        fields: dict[str, Any],
        canvas_width: int,
        canvas_height: int | None,
        rotate: int = 0,
        language: str | None = None,
        *,
        now: datetime | None = None,
        high_res: bool = False,
        red: bool = False,
        seq: str = "",
        valign: str = "top",
    ) -> bytes:
        img = self.render(
            layout,
            fields,
            canvas_width,
            canvas_height,
            rotate,
            language,
            now=now,
            high_res=high_res,
            red=red,
            seq=seq,
            valign=valign,
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def render_sequence(
        self,
        layout: list[dict[str, Any]],
        fields: dict[str, Any],
        canvas_width: int,
        canvas_height: int | None,
        *,
        start: int,
        count: int,
        step: int = 1,
        padding: int = 0,
        rotate: int = 0,
        language: str | None = None,
        now: datetime | None = None,
        high_res: bool = False,
        red: bool = False,
        valign: str = "top",
    ) -> Iterator[bytes]:
        """Lazily render ``count`` labels, yielding one PNG byte string per item.

        This is a **generator**, not a list builder: each item is rendered only when the consumer
        pulls it, and the previously-yielded PNG is eligible for garbage collection before the next
        is produced.  ``{{seq}}`` is substituted per item as::

            str(start + i * step).zfill(padding)

        for item ``i`` in ``range(count)``.  All other computed tokens (``{{date}}``, ``{{now}}``)
        resolve to the same ``now`` for every item, so the batch timestamp is consistent.

        The lazy contract is load-bearing for memory: the consumer pulls one PNG, uses it, and lets
        it fall out of scope before the next is produced, so only ONE encoded label is ever held by
        this generator.  The live print path no longer routes a batch through here — it renders
        and sends each label individually so each gets its own per-label status confirmation (see
        ``app.main._execute_print``); this generator remains the lazy validator used by the dry-run
        path, which pulls every item to surface per-item render errors without buffering the batch.
        """
        for i in range(count):
            seq_str = format_seq(start, i, step, padding)
            yield self.render_to_png(
                layout,
                fields,
                canvas_width,
                canvas_height,
                rotate,
                language,
                now=now,
                high_res=high_res,
                red=red,
                seq=seq_str,
                valign=valign,
            )

    # ── Private helpers ─────────────────────────────────────────────────────────
    def _resolve_all(
        self,
        elements: list[ElementBase],
        fields: dict[str, Any],
        language: str,
        now: datetime,
        seq: str = "",
    ) -> list[dict[str, Any]]:
        """Resolve translation tokens then {{field}} substitutions for each element.

        Two passes per string: (1) translate ``[[key]]`` chrome words for ``language``,
        (2) substitute ``{{field}}``/``{{date}}``/``{{seq}}``. Translating first means
        user-supplied field values (which only enter in pass 2) are never mistranslated.

        Binary-valued elements (e.g. ``ImageElement``) read a raw request field by name rather
        than a ``{{token}}`` string, so the named field is passed through verbatim.

        Container elements (``RowElement``) carry child elements; their resolved dicts are nested
        under ``__children__`` (parallel to ``el.children``) so the container can hand each child
        its own resolution at render time.

        ``seq`` is the pre-formatted sequence string for the current item ("" for non-sequence
        renders); it is substituted wherever ``{{seq}}`` appears.
        """
        date_fmt, datetime_fmt = self.translator.date_formats(language)
        weekday_abbr, weekday_full = self.translator.weekday_names(language)
        month_abbr, month_full = self.translator.month_names(language)
        return [
            self._resolve_element(
                el,
                fields,
                language,
                now,
                date_fmt,
                datetime_fmt,
                seq,
                weekday_abbr,
                weekday_full,
                month_abbr,
                month_full,
            )
            for el in elements
        ]

    def _resolve_element(
        self,
        el: ElementBase,
        fields: dict[str, Any],
        language: str,
        now: datetime,
        date_fmt: str,
        datetime_fmt: str,
        seq: str = "",
        weekday_abbr: Sequence[str] = _WEEKDAYS_ABBR,
        weekday_full: Sequence[str] = _WEEKDAYS_FULL,
        month_abbr: Sequence[str] = _MONTHS_ABBR,
        month_full: Sequence[str] = _MONTHS_FULL,
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for attr in _TEMPLATED_ATTRS:
            raw = getattr(el, attr, None)
            if isinstance(raw, str):
                translated = self.translator.translate(raw, language)
                resolved[f"__{attr}__"] = _resolve_fields(
                    translated,
                    fields,
                    now,
                    date_fmt,
                    datetime_fmt,
                    seq,
                    weekday_abbr,
                    weekday_full,
                    month_abbr,
                    month_full,
                )
        field_name = getattr(el, "field", None)
        if isinstance(field_name, str):
            resolved[field_name] = fields.get(field_name)
        children = getattr(el, "children", None)
        if children:
            resolved["__children__"] = [
                self._resolve_element(
                    child,
                    fields,
                    language,
                    now,
                    date_fmt,
                    datetime_fmt,
                    seq,
                    weekday_abbr,
                    weekday_full,
                    month_abbr,
                    month_full,
                )
                for child in children
            ]
        return resolved

    def _render_elements(
        self,
        elements: list[ElementBase],
        resolved: list[dict[str, Any]],
        canvas_width: int,
    ) -> list[Image.Image]:
        strips: list[Image.Image] = []
        for el, res in zip(elements, resolved, strict=True):
            # Author padding is applied UNIFORMLY via _apply_padding — the same helper the row/column
            # child renderers use — so padding behaves identically on top-level and nested elements.
            # A blank element (absent optional field) renders zero-height and contributes nothing.
            def _render(
                content_width: int, el: ElementBase = el, res: dict[str, Any] = res
            ) -> Image.Image:
                return el.render(
                    content_width, res, self.fonts_dir, self.icons_dir, self.icon_collections_dir
                )

            strip = _apply_padding(el, canvas_width, _render)
            if strip.height > 0:
                strips.append(strip)
        return strips

    def _compose(
        self,
        strips: list[Image.Image],
        canvas_width: int,
        canvas_height: int | None,
        min_length_px: int | None = None,
        max_length_px: int | None = None,
        *,
        red: bool = False,
        valign: str = "top",
    ) -> Image.Image:
        _min = min_length_px if min_length_px is not None else self.min_length_px
        _max = max_length_px if max_length_px is not None else self.max_length_px
        total_height = sum(s.height for s in strips)

        if canvas_height is None:
            # Continuous: clamp to [min, max] (caller scales limits for high_res)
            height = max(_min, min(total_height, _max))
        else:
            # Die-cut: exact canvas
            height = canvas_height

        # RGB white canvas in two-color mode (matching the RGB strips), else the original "L" canvas
        # so a non-red compose is byte-identical to the monochrome path.
        if red:
            canvas = Image.new("RGB", (canvas_width, height), (255, 255, 255))
        else:
            canvas = Image.new("L", (canvas_width, height), 255)
        # Vertical placement of the stacked block. ``top`` (the default) keeps the historical
        # top-anchored layout, so every template that does not opt in composes byte-identically.
        # Only die-cut media (canvas_height set → fixed face) is shifted: continuous tape has no
        # fixed frame to centre within (its length is elastic, merely clamped to [min, max]), so
        # valign is a no-op there. ``center``/``bottom`` shift only when the content actually fits
        # (slack > 0); on overflow we fall back to y=0 so the per-strip crop below still trims the
        # tail from the top exactly as before, rather than pushing the head off-canvas.
        slack = height - total_height
        if canvas_height is not None and slack > 0 and valign in ("center", "bottom"):
            y = slack // 2 if valign == "center" else slack
        else:
            y = 0
        for strip in strips:
            if y + strip.height > height:
                # Clip to canvas height — prefer autosize on text elements before reaching this
                remaining = height - y
                if remaining <= 0:
                    break
                strip = strip.crop((0, 0, canvas_width, remaining))
            canvas.paste(strip, (0, y))
            y += strip.height
        return canvas
