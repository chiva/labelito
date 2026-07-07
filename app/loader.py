# SPDX-License-Identifier: GPL-3.0-or-later
"""Load and validate label templates from YAML files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from app.render.elements import (
    COLOR_CHOICES,
    DEFAULT_TEXT_MAX_LINES,
    FA_STYLES,
    FONT_SIZES,
    KNOWN_COLLECTIONS,
    LIST_DEFAULT_MAX_ITEMS,
    LIST_MARKER_CHOICES,
    TEXT_BACKGROUND_CHOICES,
    VALIGN_CHOICES,
    _resolve_padding,
    _safe_icon_name,
)
from app.render.engine import (
    COMPUTED_TOKENS,
    image_field_names,
    malformed_placeholders,
    referenced_field_tokens,
    unresolved_tokens,
)

log = logging.getLogger(__name__)

REQUIRED_TOP_KEYS = {"name", "description", "label", "layout"}

# A field name is interpolated into request payloads, history, and (pre-fix) the editor DOM via
# innerHTML. Constrain it to a conservative charset at load time so a name like
# ``<img src=x onerror=...>`` can never reach a consumer of /templates/parse — defence in depth
# behind the editor's textContent rendering.
#
# The charset MUST stay a subset of the render token grammar (engine._FIELD_RE matches ``{{(\w+)}}``,
# i.e. ``[A-Za-z0-9_]``). A wider charset would let a template declare ``required: [asset-id]`` and
# reference ``{{asset-id}}``: validation sees no unresolved token, /templates/parse reports the field
# as valid, yet the renderer's ``\w+`` token regex never matches it, so the literal ``{{asset-id}}``
# is printed on the label — a wrong-label failure, not cosmetic. Keeping the two in lockstep means a
# declarable field name is always a substitutable one. Every shipped template already uses only
# ``[A-Za-z0-9_]`` names; widen BOTH this charset and the token grammar together if that ever changes.
FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# Upper bound (in 300 dpi / scale=1 template units) for any single render-affecting pixel
# dimension. A YAML int is unbounded, so a tiny template can declare ``size: 99999999999`` and
# either crash the renderer (OverflowError in PIL) or allocate a multi-gigapixel image and exhaust
# process memory — the body/field caps bound payload size, not a small body's huge numbers. The
# longest continuous label any supported model prints is ~35434 dots (ModelsManager
# min_max_length_dots), but no SINGLE element needs anywhere near that; 10000 px is a generous cap
# that comfortably exceeds every real label element (the largest shipped value is a 180 px QR) while
# keeping the worst-case allocation (about 10000x1296 px, ~13M px) well inside PIL's bomb threshold and
# far from the OverflowError range. Applied BEFORE render/save in build_template_from_mapping, so it
# hardens the draft endpoint AND saved-template reloading.
MAX_ELEMENT_DIMENSION = 10000

# max_lines bounds the number of wrapped text lines; an enormous value would let a single text
# element allocate an arbitrarily tall strip. A label is short — 200 lines is far past any real use.
MAX_TEXT_LINES = 200

# Effective wrapped-line ceiling assumed for a `text` element that OMITS `max_lines` entirely. Most
# shipped templates do NOT set max_lines on body text, so we cannot REJECT an absent value (that would
# break them); instead the renderer clamps an absent max_lines to this default (TextElement.max_lines
# == DEFAULT_TEXT_MAX_LINES) and the strip-area guard below assumes the same value. An explicit
# ``max_lines: null`` is a DIFFERENT case: it overrides the dataclass default with None at the
# renderer (build_element copies every present key, so the None reaches the element and the
# ``if max_lines:`` clamp becomes a no-op → unbounded strip). The loader therefore REJECTS an explicit
# null for any render-affecting numeric (see _validate_element_numerics); only an absent key gets the
# bounded default. Without that, a long static literal (no per-field char cap on a YAML literal) would
# wrap into an arbitrarily tall strip and OOM the worker BEFORE the final compose clamp. Imported from
# elements.py so the guard and the renderer can never drift apart.

# A few attributes are NOT 1-D pixel counts and so need a tighter cap than MAX_ELEMENT_DIMENSION.
# `qr.size` and `icon.size` both render as a sizexsize SQUARE (PIL `resize((size, size))`), so the
# allocation is quadratic, not linear: at the linear cap (10000) that is 100M px, and xscale² (4 at
# 600 dpi high_res) ≈ 400M px — a multi-hundred-MB image that can OOM the worker straight off
# /preview/draft or a saved template that otherwise validates. 2000 px is already far larger than any
# thermal label QR/icon (the largest shipped value is a 180 px QR), and bounds the worst case at
# 2000²x4 ≈ 16M px ≈ 16 MB — generous yet safe on a home box.
MAX_SQUARE_DIMENSION = 2000

# `text.size` is a font POINT size, not a pixel count: it scales the rendered glyph height AND
# (multiplied by max_lines) the strip height, so sizexmax_lines drives a quadratic allocation. A
# 512 pt font is already absurd for a label; cap it well below the linear ceiling.
MAX_FONT_SIZE = 512

# Guard the text strip's AREA, not just the two scalars: even within MAX_FONT_SIZE and MAX_TEXT_LINES
# the product sizexmax_lines can compose a multi-thousand-px-tall strip (line height ≈ 1.3xsize).
# Bound the product so 1.3xsizexlines stays a few-thousand-px strip. Only the `text` element carries
# an author-controlled `size`; title/subtitle have fixed default sizes (60/40 pt) and only max_lines
# is bounded, so at 200 lines their strip is already bounded and needs no extra product guard.
MAX_TEXT_STRIP_PRODUCT = 4000

# The only meaningful top-level rotations: the renderer applies right-angle orientation only
# (engine.py img.rotate(rotate, expand=True)), and the printer feed axis is quarter-turn oriented.
# Any other value is either a no-op tilt that mis-renders or — for an unbounded int — an OverflowError
# in PIL at render time, so the loader rejects anything outside this set.
# Per-element caps bound ONE element's allocation, but not the WHOLE layout: hundreds of individually
# valid elements (e.g. {spacer, size: 10000} xN, or box height: 10000 xN) compose into hundreds of MB
# of strips BEFORE the engine's final raster-row clamp ever runs (engine clamps the composed canvas,
# but each strip is allocated full-size first). Two cheap validation guards bound the layout as a
# whole without a compose refactor:
#
#  • MAX_LAYOUT_ELEMENTS caps the element COUNT (row children counted individually). A real label has
#    well under 64 elements; this stops a "thousand tiny spacers" DoS outright.
#  • MAX_TOTAL_STRIP_HEIGHT caps the SUMMED conservative per-element height contribution. It sits a
#    bit above the largest model raster ceiling (~35434 dots, ModelsManager min_max_length_dots) —
#    a label declared taller than its printer's maximum raster cannot print anyway, so anything above
#    this is purely an allocation hazard, not a real label.
MAX_LAYOUT_ELEMENTS = 64
MAX_TOTAL_STRIP_HEIGHT = 40000

VALID_ROTATIONS = {0, 90, 180, 270}
VALID_ELEMENT_TYPES = {
    "title",
    "subtitle",
    "text",
    "qr",
    "barcode",
    "image",
    "icon",
    "line",
    "box",
    "spacer",
    "row",
    "column",
    "list",
}
# Container elements — the only types that carry a ``children`` list. The layout is a single-level
# grid: a ``row`` lays children side-by-side and may contain ``column``s; a ``column`` stacks
# children vertically and may contain only leaf elements. No deeper nesting (row-in-row,
# column-in-column, row-in-column) is permitted — see :func:`_validate_element`.
CONTAINER_TYPES = frozenset({"row", "column"})
# Text-family elements that accept the shared `background`/`border`/`border_color` decorations.
TEXT_FAMILY_TYPES = frozenset({"title", "subtitle", "text"})
# Upper bound on a list `separator` string length — a delimiter is one or a few chars, never a blob.
MAX_LIST_SEPARATOR_LEN = 8


class TemplateLoadError(ValueError):
    pass


class Template:
    """A loaded, validated label template."""

    __slots__ = (
        "description",
        "is_example",
        "label",
        "layout",
        "name",
        "optional_fields",
        "required_fields",
        "rotate",
        "source_path",
        "valign",
    )

    def __init__(
        self,
        name: str,
        description: str,
        label: str,
        rotate: int,
        required_fields: list[str],
        optional_fields: list[str],
        layout: list[dict[str, Any]],
        source_path: Path,
        is_example: bool = False,
        valign: str = "top",
    ) -> None:
        self.name = name
        self.description = description
        self.label = label
        self.rotate = rotate
        self.valign = valign
        self.required_fields = required_fields
        self.optional_fields = optional_fields
        self.layout = layout
        self.source_path = source_path
        # True when this template came from the bundled example dir (not the user's templates_dir).
        # Set by TemplateRegistry._load_dir after construction; used to visually mark example cards.
        self.is_example = is_example

    @property
    def all_fields(self) -> list[str]:
        return self.required_fields + self.optional_fields


def _validate_icon(file_name: str, label: str, el: dict[str, Any]) -> None:
    """Validate an ``icon`` layout element's collection/style/name.

    File existence is intentionally *not* checked: bundled collections may be absent in dev/test
    environments, so loading must not depend on the baked assets being present. A ``{{token}}``-driven
    name is skipped here — it is sanitized at render time by :func:`_safe_icon_name`.

    ``label`` locates the element in error messages (e.g. ``layout[2]`` or
    ``layout[2].children[1]`` for a row child).
    """
    collection = el.get("collection")
    if collection is not None and str(collection) and str(collection) not in KNOWN_COLLECTIONS:
        raise TemplateLoadError(
            f"{file_name}: {label} unknown icon collection {collection!r}; "
            f"valid: {sorted(KNOWN_COLLECTIONS)}"
        )
    style = el.get("style")
    if str(collection) == "fontawesome" and style is not None and str(style) not in FA_STYLES:
        raise TemplateLoadError(
            f"{file_name}: {label} unknown fontawesome style {style!r}; valid: {sorted(FA_STYLES)}"
        )
    name = el.get("name")
    if isinstance(name, str) and "{{" not in name and _safe_icon_name(name) is None:
        raise TemplateLoadError(
            f"{file_name}: {label} invalid icon name {name!r}; "
            f"must be a single path component without separators or '..'"
        )


def _require_int(
    file_name: str,
    label: str,
    key: str,
    value: Any,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    """Reject a sizing control that is not an integer in ``[minimum, maximum]``.

    Row column maths (``RowElement._column_widths``) and every renderer do direct arithmetic on
    these values, so a YAML typo such as ``width: "80"`` (a string) would load cleanly here yet
    raise ``TypeError`` at render time — a 500 *after* ``/reload`` reported success. ``bool`` is
    excluded explicitly because it is an ``int`` subclass but ``width: true`` is meaningless as a
    pixel count.

    When ``maximum`` is given, a value above it is rejected too: an unbounded YAML int (e.g. a
    300-digit ``size``) would otherwise drive a multi-gigapixel allocation or an OverflowError in
    PIL. Rejecting it here bounds the allocation BEFORE any render/save.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TemplateLoadError(f"{file_name}: {label} '{key}' must be an integer, got {value!r}")
    if value < minimum:
        raise TemplateLoadError(f"{file_name}: {label} '{key}' must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise TemplateLoadError(f"{file_name}: {label} '{key}' must be <= {maximum}, got {value}")


# Per-element render-affecting numeric attributes: (key, minimum, maximum). Every value here feeds a
# pixel dimension (or a wrapped-line count) a renderer multiplies/allocates, so each is type-checked
# (int, not "32") and bounded so a tiny YAML cannot drive an unbounded allocation. Cosmetic enums
# (align/valign/color/symbology/style) are validated elsewhere and intentionally omitted.
_ELEMENT_NUMERIC_BOUNDS: dict[str, tuple[tuple[str, int, int], ...]] = {
    "title": (("max_lines", 1, MAX_TEXT_LINES), ("border", 0, MAX_ELEMENT_DIMENSION)),
    "subtitle": (("max_lines", 1, MAX_TEXT_LINES), ("border", 0, MAX_ELEMENT_DIMENSION)),
    # `text` size is a font point size (MAX_FONT_SIZE), additionally area-guarded against max_lines
    # in _validate_element_numerics; qr/icon size render as a sizexsize square (MAX_SQUARE_DIMENSION).
    "text": (
        ("size", 1, MAX_FONT_SIZE),
        ("max_lines", 1, MAX_TEXT_LINES),
        ("border", 0, MAX_ELEMENT_DIMENSION),
    ),
    # `list` mirrors `text`: a font point size plus a max item count (the wrapped-line ceiling), with
    # the size x max_items product area-guarded in _validate_element_numerics.
    "list": (("size", 1, MAX_FONT_SIZE), ("max_items", 1, MAX_TEXT_LINES)),
    "qr": (("size", 1, MAX_SQUARE_DIMENSION),),
    "barcode": (("height", 1, MAX_ELEMENT_DIMENSION),),
    "image": (("max_height", 1, MAX_ELEMENT_DIMENSION),),
    "icon": (("size", 1, MAX_SQUARE_DIMENSION),),
    "line": (("thickness", 1, MAX_ELEMENT_DIMENSION), ("margin", 0, MAX_ELEMENT_DIMENSION)),
    "box": (("height", 1, MAX_ELEMENT_DIMENSION), ("border", 0, MAX_ELEMENT_DIMENSION)),
    "spacer": (("size", 0, MAX_ELEMENT_DIMENSION),),
}

# Paddings exist on EVERY element (ElementBase). They are pixel insets the render wrapper adds
# (top/bottom grow the strip; left/right inset the content), so an unbounded value inflates the strip;
# bound all four uniformly. The `padding` shorthand is validated separately (_validate_padding).
_COMMON_NUMERIC_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("padding_top", 0, MAX_ELEMENT_DIMENSION),
    ("padding_right", 0, MAX_ELEMENT_DIMENSION),
    ("padding_bottom", 0, MAX_ELEMENT_DIMENSION),
    ("padding_left", 0, MAX_ELEMENT_DIMENSION),
)
# Every padding key (longhand sides + the shorthand). Padding is honoured only on top-level elements,
# so these are rejected on row/column children rather than silently ignored.
PADDING_KEYS: tuple[str, ...] = (
    "padding",
    "padding_top",
    "padding_right",
    "padding_bottom",
    "padding_left",
)


def _validate_element_numerics(file_name: str, label: str, el: dict[str, Any]) -> None:
    """Type-check and bound every render-affecting numeric attribute on one element.

    The renderer multiplies these by ``scale`` and hands them to PIL (image dimensions, font size,
    wrapped-line counts). An unbounded YAML int — or a non-int like ``size: "32"`` — would crash the
    render or allocate a giant image; rejecting it here (→ 422 on draft/save, and a malformed saved
    template is skipped on reload) keeps any element's allocation within what a real label needs.
    Row container sizing (``spacing``/``width``/``weight``) and ``image.field`` are validated by
    their own dedicated checks; this covers the per-type pixel/line attributes.
    """
    el_type = el.get("type")
    bounds = _COMMON_NUMERIC_BOUNDS + _ELEMENT_NUMERIC_BOUNDS.get(str(el_type), ())
    for key, minimum, maximum in bounds:
        # Distinguish an ABSENT key from an EXPLICIT ``key: null``. ``el.get(key)`` returns None for
        # both, but they mean opposite things to build_element: an absent key lets the dataclass
        # DEFAULT apply (a sane in-bounds value), while a present ``key: null`` is copied into the
        # element as None — overriding the default. For a render-affecting numeric that None either
        # disables a safety clamp (``max_lines: null`` ⇒ the renderer's ``if max_lines:`` is a no-op ⇒
        # unbounded text strip) or crashes render (``size: null`` ⇒ ``self._px(None)`` raises). So an
        # absent key is skipped (default applies) but an explicit null is REJECTED up front.
        if key not in el:
            continue
        if el[key] is None:
            raise TemplateLoadError(
                f"{file_name}: {label} '{key}' must not be null; omit the key to use the default "
                f"or give an integer in [{minimum}, "
                f"{maximum if maximum is not None else '∞'}]"
            )
        _require_int(file_name, label, key, el[key], minimum=minimum, maximum=maximum)

    # Strip-area guard: `text` is the one element whose font `size` is author-controlled, so even
    # with size ≤ MAX_FONT_SIZE and max_lines ≤ MAX_TEXT_LINES their PRODUCT can compose a giant
    # strip (height ≈ 1.3xsizexlines). Bound the product so the worst-case strip stays a few-thousand
    # px tall. After the absent-vs-null check above, `size`/`max_lines` are each either ABSENT (→ the
    # in-bounds dataclass default applies) or a finite in-range int — an explicit null was already
    # rejected. So the effective value is the present int when present, else the default: `size`
    # falls back to FONT_SIZES["text"] and an omitted `max_lines` is clamped by the renderer to
    # DEFAULT_TEXT_MAX_LINES (so the guard uses that same effective line count, never "unbounded").
    if str(el_type) == "text":
        size = el.get("size")
        max_lines = el.get("max_lines")
        effective_size = (
            size if isinstance(size, int) and not isinstance(size, bool) else FONT_SIZES["text"]
        )
        effective_lines = (
            max_lines
            if isinstance(max_lines, int) and not isinstance(max_lines, bool)
            else DEFAULT_TEXT_MAX_LINES
        )
        if effective_size * effective_lines > MAX_TEXT_STRIP_PRODUCT:
            shown_lines = (
                max_lines if isinstance(max_lines, int) else f"{DEFAULT_TEXT_MAX_LINES} (default)"
            )
            raise TemplateLoadError(
                f"{file_name}: {label} text 'size' x 'max_lines' ({effective_size} x {shown_lines} "
                f"= {effective_size * effective_lines}) must be <= {MAX_TEXT_STRIP_PRODUCT}; reduce "
                f"the font size or set a smaller max_lines so the rendered text strip stays bounded"
            )

    # `list` has the same quadratic exposure as `text`: font `size` x the item count drives the strip
    # height (one line per item, plus wrapping). Bound the product the same way. Both scalars are, by
    # the absent-vs-null check above, either ABSENT (dataclass default applies) or a finite in-range
    # int, so the effective value is the present int else the default (FONT_SIZES["text"] /
    # LIST_DEFAULT_MAX_ITEMS).
    if str(el_type) == "list":
        size = el.get("size")
        max_items = el.get("max_items")
        effective_size = (
            size if isinstance(size, int) and not isinstance(size, bool) else FONT_SIZES["text"]
        )
        effective_items = (
            max_items
            if isinstance(max_items, int) and not isinstance(max_items, bool)
            else LIST_DEFAULT_MAX_ITEMS
        )
        if effective_size * effective_items > MAX_TEXT_STRIP_PRODUCT:
            shown_items = (
                max_items if isinstance(max_items, int) else f"{LIST_DEFAULT_MAX_ITEMS} (default)"
            )
            raise TemplateLoadError(
                f"{file_name}: {label} list 'size' x 'max_items' ({effective_size} x {shown_items} "
                f"= {effective_size * effective_items}) must be <= {MAX_TEXT_STRIP_PRODUCT}; reduce "
                f"the font size or set a smaller max_items so the rendered list strip stays bounded"
            )


def _require_choice(
    file_name: str, label: str, key: str, value: Any, choices: frozenset[str]
) -> None:
    """Reject an alignment control whose value is not one of ``choices``.

    An out-of-range value would not crash (render falls back to centring) but silently ignores the
    template author's intent, so it is rejected up front like every other layout typo."""
    if str(value) not in choices:
        raise TemplateLoadError(
            f"{file_name}: {label} '{key}' must be one of {sorted(choices)}, got {value!r}"
        )


def _require_bool(file_name: str, label: str, key: str, value: Any) -> None:
    """Reject a toggle control that is not a real YAML boolean.

    The renderer gates on plain truthiness (``if self.divider``/``if self.fill``), so the common
    quoting typo ``divider: "false"`` would load as a non-empty *string* — truthy — and print the
    feature the author meant to disable. A wrong-label failure, so reject any non-bool up front."""
    if not isinstance(value, bool):
        raise TemplateLoadError(
            f"{file_name}: {label} '{key}' must be a boolean (true/false), got {value!r}"
        )


def _validate_padding(file_name: str, label: str, value: Any) -> None:
    """Validate the CSS-style ``padding`` shorthand: a scalar int or a list of 1-4 ints.

    Mirrors CSS's 1-4-value clockwise forms (all / v h / t h b / t r b l). Each value is a pixel inset
    bounded like the longhand fields, so a huge/negative/non-int (or a >4-value list) is rejected up
    front rather than silently mis-expanded by :func:`app.render.elements._resolve_padding`."""
    if isinstance(value, bool) or not isinstance(value, (int, list)):
        raise TemplateLoadError(
            f"{file_name}: {label} 'padding' must be an int or a list of 1-4 ints, got {value!r}"
        )
    if isinstance(value, int):
        _require_int(file_name, label, "padding", value, minimum=0, maximum=MAX_ELEMENT_DIMENSION)
        return
    if not 1 <= len(value) <= 4:
        raise TemplateLoadError(
            f"{file_name}: {label} 'padding' list must have 1-4 values (CSS clockwise: all / v h / "
            f"t h b / t r b l), got {len(value)}"
        )
    for i, item in enumerate(value):
        _require_int(
            file_name, label, f"padding[{i}]", item, minimum=0, maximum=MAX_ELEMENT_DIMENSION
        )


def _validate_row_child_sizing(file_name: str, label: str, child: dict[str, Any]) -> None:
    """Validate the per-child column hints (``width``/``weight``/``valign``) of a row child."""
    width = child.get("width")
    # ``width`` is the documented EXCEPTION to the explicit-null rejection in
    # _validate_element_numerics: None IS the legitimate sentinel for a flexible column (it is the
    # dataclass default and the renderer keys "flex vs fixed" on ``width is None``), so an explicit
    # ``width: null`` is intentionally allowed — it means the same as omitting the key. Only a PRESENT
    # non-null width is a fixed pixel count that must be a bounded int.
    if width is not None:
        _require_int(file_name, label, "width", width, minimum=1, maximum=MAX_ELEMENT_DIMENSION)
    if "weight" in child:
        # `weight` is a dimensionless ratio, not a pixel count, but an unbounded value is still
        # nonsensical and pointlessly large; cap it at the same generous ceiling. _require_int also
        # rejects a ``weight: null`` (None is not an int) — the renderer does ``max(0, c.weight)``,
        # which would raise TypeError on None, so null is correctly refused here, unlike ``width``.
        _require_int(
            file_name, label, "weight", child["weight"], minimum=0, maximum=MAX_ELEMENT_DIMENSION
        )
    valign = child.get("valign")
    if valign is not None and str(valign) != "":  # "" ⇒ inherit the row's align_items
        _require_choice(file_name, label, "valign", valign, VALIGN_CHOICES)


def _validate_element(
    file_name: str,
    label: str,
    el: Any,
    *,
    allowed_containers: frozenset[str] = CONTAINER_TYPES,
    nested: bool = False,
) -> None:
    """Validate one layout element's type (and icon/container/decoration specifics), recursing.

    ``allowed_containers`` is the set of container types permitted at THIS position; it shrinks as
    the walk descends so the layout stays a single-level grid: the top level allows both ``row`` and
    ``column``; a row's children allow only ``column`` (a row may hold columns, not another row); a
    column's children allow no container at all (columns hold only leaf elements). A container found
    where it isn't allowed is rejected loudly rather than silently mis-rendering.

    ``nested`` marks a ``row``/``column`` child. Padding is applied only to top-level elements (by
    ``RenderEngine._render_elements``); a child's padding_* keys would have no effect, so they are
    rejected here rather than silently accepted-and-ignored (the same footgun the padding fields were
    added to remove). Pad the container as a whole instead.
    """
    if not isinstance(el, dict):
        raise TemplateLoadError(f"{file_name}: {label} must be a mapping")
    if nested:
        present = [k for k in PADDING_KEYS if k in el]
        if present:
            raise TemplateLoadError(
                f"{file_name}: {label} padding ({', '.join(present)}) is not supported on a "
                f"row/column child — it has no effect there; pad the container element instead"
            )
    el_type = el.get("type")
    if el_type not in VALID_ELEMENT_TYPES:
        raise TemplateLoadError(
            f"{file_name}: {label} unknown element type {el_type!r}; "
            f"valid: {sorted(VALID_ELEMENT_TYPES)}"
        )
    if el_type in CONTAINER_TYPES and el_type not in allowed_containers:
        raise TemplateLoadError(
            f"{file_name}: {label} a {el_type!r} cannot be nested here — the layout is a "
            f"single-level grid: a 'row' may contain 'column's, and a 'column' may contain only "
            f"leaf elements (no row-in-row, column-in-column, or row-in-column)"
        )
    # Bound every render-affecting numeric attribute (sizes, heights, paddings, line counts) BEFORE
    # render/save so a tiny YAML cannot drive an unbounded PIL allocation or an OverflowError.
    _validate_element_numerics(file_name, label, el)
    if "padding" in el:
        _validate_padding(file_name, label, el["padding"])
    # Two-color: an optional `color` selects the red vs black layer. Validate up front like
    # every other layout control so a typo (color: blue) is a clear load error, not a silently
    # ignored value. Only honoured when a print resolves red=true; otherwise the element draws black.
    if "color" in el:
        _require_choice(file_name, label, "color", el["color"], COLOR_CHOICES)
    # Text-family decorations: `background` (badge/banner fill) and `border`+`border_color` (boxed
    # text). Validate the enums up front like `color`; `border` (a pixel count) is bounded by the
    # numeric guard above. Only meaningful on text/title/subtitle — a stray value elsewhere is a typo.
    if el_type in TEXT_FAMILY_TYPES:
        if "background" in el:
            _require_choice(
                file_name, label, "background", el["background"], TEXT_BACKGROUND_CHOICES
            )
        if "border_color" in el:
            _require_choice(file_name, label, "border_color", el["border_color"], COLOR_CHOICES)
    if el_type == "icon":
        _validate_icon(file_name, label, el)
    if el_type == "image" and "field" in el:
        # The renderer does resolved_fields.get(el.field); a non-string (e.g. a list) is unhashable
        # and raises TypeError at render. Worse, the image guards would track a coerced str() of it
        # while the renderer reads the raw value — different identities. Require a non-empty string.
        image_field = el["field"]
        if not isinstance(image_field, str) or not image_field:
            raise TemplateLoadError(
                f"{file_name}: {label} image 'field' must be a non-empty string, got {image_field!r}"
            )
    if el_type == "box" and "fill" in el:
        _require_bool(file_name, label, "fill", el["fill"])
    if el_type == "list":
        if "bold" in el:
            _require_bool(file_name, label, "bold", el["bold"])
        if "marker" in el:
            _require_choice(file_name, label, "marker", el["marker"], LIST_MARKER_CHOICES)
        if "separator" in el:
            sep = el["separator"]
            if not isinstance(sep, str) or not 1 <= len(sep) <= MAX_LIST_SEPARATOR_LEN:
                raise TemplateLoadError(
                    f"{file_name}: {label} list 'separator' must be a string of 1.."
                    f"{MAX_LIST_SEPARATOR_LEN} chars, got {sep!r}"
                )
    if el_type == "row":
        if "spacing" in el:
            _require_int(
                file_name, label, "spacing", el["spacing"], minimum=0, maximum=MAX_ELEMENT_DIMENSION
            )
        if "align_items" in el:
            _require_choice(file_name, label, "align_items", el["align_items"], VALIGN_CHOICES)
        # Optional vertical dividers between columns.
        if "divider" in el:
            _require_bool(file_name, label, "divider", el["divider"])
        if "divider_thickness" in el:
            _require_int(
                file_name,
                label,
                "divider_thickness",
                el["divider_thickness"],
                minimum=1,
                maximum=MAX_ELEMENT_DIMENSION,
            )
        if "divider_color" in el:
            _require_choice(file_name, label, "divider_color", el["divider_color"], COLOR_CHOICES)
        children = el.get("children")
        if not isinstance(children, list) or not children:
            raise TemplateLoadError(
                f"{file_name}: {label} 'row' requires a non-empty 'children' list"
            )
        for j, child in enumerate(children):
            child_label = f"{label}.children[{j}]"
            # A row may hold columns but not another row (single-level grid).
            _validate_element(
                file_name, child_label, child, allowed_containers=frozenset({"column"}), nested=True
            )
            _validate_row_child_sizing(file_name, child_label, child)
    elif el_type == "column":
        if "spacing" in el:
            _require_int(
                file_name, label, "spacing", el["spacing"], minimum=0, maximum=MAX_ELEMENT_DIMENSION
            )
        children = el.get("children")
        if not isinstance(children, list) or not children:
            raise TemplateLoadError(
                f"{file_name}: {label} 'column' requires a non-empty 'children' list"
            )
        for j, child in enumerate(children):
            child_label = f"{label}.children[{j}]"
            # A column holds only leaf elements — no container may nest inside it.
            _validate_element(
                file_name, child_label, child, allowed_containers=frozenset(), nested=True
            )
    elif "children" in el:
        # Only a container ('row'/'column') renders children (the sole elements with that dataclass
        # field). A 'children' list on any other element is silently ignored at render time, yet the
        # recursive image/token walkers would still descend into it — so an ignored child could mark
        # a text field as an image, bypassing the text-size cap and corrupting history. Reject it so
        # validation, rendering, and history all traverse exactly the same element tree.
        raise TemplateLoadError(
            f"{file_name}: {label} only a 'row' or 'column' element may have 'children'"
        )


# Conservative per-type fallback defaults for the cumulative height estimate. These mirror the
# dataclass defaults in render/elements.py for the height-driving attribute of each element, used
# only when the YAML omits that attribute. They are upper-bound estimates for a SAFETY ceiling, not
# pixel-exact render maths — the engine still produces the true strip. Kept generous so the budget
# only ever rejects pathological layouts, never a real label.
_HEIGHT_DEFAULTS: dict[str, int] = {
    "spacer": 16,  # SpacerElement.size
    "box": 40,  # BoxElement.height
    "line": 2,  # LineElement.thickness
    "image": 200,  # ImageElement.max_height
    "qr": 160,  # QRElement.size
    "icon": 80,  # IconElement.size
    "barcode": 60,  # BarcodeElement.height
}
# Text line height is ≈1.3xfont size (an 8 px line gap atop the glyph height); round up to 2x so the
# per-element estimate is a comfortable upper bound on the rendered text strip.
_TEXT_LINE_HEIGHT_FACTOR = 2
# Default wrapped-line counts for title/subtitle (TitleElement/SubtitleElement.max_lines == 2).
_TITLE_DEFAULT_MAX_LINES = 2


def _int_attr(el: dict[str, Any], key: str, default: int) -> int:
    """Read an already-validated non-negative int attribute, falling back to ``default``.

    Every value here has passed :func:`_validate_element_numerics` (an int in range, or absent), so a
    present value is a safe int; an absent/None value uses the element's dataclass default."""
    value = el.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _estimate_element_height(el: dict[str, Any]) -> int:
    """Conservatively estimate one element's contributed strip height (in template px).

    Maps each element type to its height-driving attribute (reusing the dataclass defaults when the
    attribute is absent) and adds the common vertical padding. For a ``row`` the children render
    side-by-side, so the row's height is the TALLEST child; for a ``column`` they stack, so its
    height is the SUM (plus inter-child spacing). Used only by the cumulative budget guard
    (:data:`MAX_TOTAL_STRIP_HEIGHT`); it is intentionally an upper bound, not exact.
    """
    el_type = str(el.get("type"))
    # Only top/bottom padding adds height (left/right inset the content width). Resolve via the same
    # shorthand+longhand logic the renderer uses so the budget matches what actually renders.
    _pt, _pr, _pb, _pl = _resolve_padding(el)
    padding = _pt + _pb

    if el_type == "row":
        children = el.get("children")
        child_max = 0
        if isinstance(children, list):
            child_max = max(
                (_estimate_element_height(c) for c in children if isinstance(c, dict)), default=0
            )
        return padding + child_max

    if el_type == "column":
        # Children stack vertically, so the column's height is their SUM plus the spacing gaps.
        children = el.get("children")
        child_dicts = (
            [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []
        )
        child_sum = sum(_estimate_element_height(c) for c in child_dicts)
        spacing = _int_attr(el, "spacing", 0) * max(0, len(child_dicts) - 1)
        return padding + child_sum + spacing

    if el_type == "list":
        # size x max_items (the finite default when omitted) x the line-height factor — the same
        # product the strip-area guard bounds, turned into pixels.
        size = _int_attr(el, "size", FONT_SIZES["text"])
        max_items = _int_attr(el, "max_items", LIST_DEFAULT_MAX_ITEMS)
        return padding + size * max_items * _TEXT_LINE_HEIGHT_FACTOR

    if el_type == "text":
        # size x effective max_lines (the finite default when omitted) x the line-height
        # factor. This is the same product the strip-area guard bounds, here turned into pixels.
        size = _int_attr(el, "size", FONT_SIZES["text"])
        max_lines = _int_attr(el, "max_lines", DEFAULT_TEXT_MAX_LINES)
        return padding + size * max_lines * _TEXT_LINE_HEIGHT_FACTOR

    if el_type in ("title", "subtitle"):
        # Fixed font size (FONT_SIZES) x max_lines (default 2). Author cannot enlarge the font.
        max_lines = _int_attr(el, "max_lines", _TITLE_DEFAULT_MAX_LINES)
        return padding + FONT_SIZES[el_type] * max_lines * _TEXT_LINE_HEIGHT_FACTOR

    if el_type == "box":
        # height + the border drawn on top and bottom edges.
        return (
            padding
            + _int_attr(el, "height", _HEIGHT_DEFAULTS["box"])
            + 2 * _int_attr(el, "border", 2)
        )

    if el_type == "line":
        # thickness + the margin above and below the rule.
        return (
            padding
            + _int_attr(el, "thickness", _HEIGHT_DEFAULTS["line"])
            + 2 * _int_attr(el, "margin", 8)
        )

    if el_type in _HEIGHT_DEFAULTS:
        # spacer/image/qr/icon/barcode: a single height-driving attribute.
        attr = {
            "spacer": "size",
            "image": "max_height",
            "qr": "size",
            "icon": "size",
            "barcode": "height",
        }[el_type]
        return padding + _int_attr(el, attr, _HEIGHT_DEFAULTS[el_type])

    # Unknown type (already rejected by _validate_element, so unreachable in practice): fall back to
    # the linear dimension cap so the budget still bounds it defensively.
    return padding + MAX_ELEMENT_DIMENSION


def _validate_layout_budget(file_name: str, layout: list[Any]) -> None:
    """Reject a layout whose element count or cumulative declared height is pathological.

    Per-element caps don't bound the WHOLE layout: hundreds of individually valid elements compose
    into hundreds of MB of strips before the engine's final raster clamp. Two cheap guards — an
    element-count cap (:data:`MAX_LAYOUT_ELEMENTS`, counting container children) and a summed conservative
    height budget (:data:`MAX_TOTAL_STRIP_HEIGHT`) — bound the worst-case allocation without a
    compose refactor. Called after per-element validation, so every numeric value is already an
    in-range int.
    """

    def count_elements(el: Any) -> int:
        """The element plus every (transitively) nested container child — a row/column of N leaves
        is N+1 elements, and a row holding a column of M leaves is 1 + 1 + M."""
        if not isinstance(el, dict):  # already rejected by _validate_element; defensive
            return 0
        n = 1
        children = el.get("children")
        if isinstance(children, list):
            n += sum(count_elements(c) for c in children)
        return n

    total_count = 0
    total_height = 0
    for el in layout:
        if not isinstance(el, dict):  # already rejected by _validate_element; defensive
            continue
        total_count += count_elements(el)
        total_height += _estimate_element_height(el)

    if total_count > MAX_LAYOUT_ELEMENTS:
        raise TemplateLoadError(
            f"{file_name}: layout has {total_count} elements (counting container children); the maximum is "
            f"{MAX_LAYOUT_ELEMENTS}. A real label needs far fewer — split into multiple templates"
        )
    if total_height > MAX_TOTAL_STRIP_HEIGHT:
        raise TemplateLoadError(
            f"{file_name}: the layout's combined declared height (~{total_height} px) exceeds the "
            f"{MAX_TOTAL_STRIP_HEIGHT} px budget; a label taller than its printer's maximum raster "
            f"cannot print — reduce element sizes or the element count"
        )


# Sentinel ``source_path`` for a Template built from an in-memory YAML string (the draft studio)
# rather than a file on disk. It is never read/written — a draft preview renders straight
# from the validated layout — but Template requires a Path, so an unambiguous, non-filesystem marker
# makes a stray read fail loudly instead of touching a real file.
DRAFT_SOURCE_PATH = Path("<draft>")


def build_template_from_mapping(raw: Any, source_name: str, source_path: Path) -> Template:
    """Validate a parsed YAML mapping into a :class:`Template`.

    This is the single source of truth for template schema validation, shared by the file loader
    (:func:`load_template`) and the in-memory draft validator (:func:`validate_template_from_string`)
    so a draft preview is gated by EXACTLY the same checks a saved template is — reserved-name
    collisions, ``{{seq}}``/computed-token rules, undeclared-token rejection, image/text field
    collisions, and per-element layout validation.

    ``source_name`` prefixes every error message (a filename for the loader, ``<draft>`` for a
    draft) and ``source_path`` is stored on the returned Template (a real path for the loader, the
    :data:`DRAFT_SOURCE_PATH` sentinel for a draft, which is never read back).
    """
    if not isinstance(raw, dict):
        raise TemplateLoadError(f"{source_name}: top-level must be a mapping")

    missing = REQUIRED_TOP_KEYS - raw.keys()
    if missing:
        raise TemplateLoadError(f"{source_name}: missing required keys: {missing}")

    name = str(raw["name"])
    description = str(raw.get("description", ""))
    label = str(raw["label"])

    # Coercions below can raise on a plausible typo (rotate: ninety, fields: []); wrap them as
    # TemplateLoadError so one malformed file is skipped-and-reported, never an uncaught 500 in
    # /reload or a crash at startup (load_all only isolates TemplateLoadError).
    try:
        rotate = int(raw.get("rotate", 0))
    except (TypeError, ValueError) as exc:
        raise TemplateLoadError(f"{source_name}: 'rotate' must be an integer") from exc
    # The renderer only does right-angle orientation (engine.py: img.rotate(rotate, expand=True)),
    # so only the 4 quarter-turns are meaningful. An unbounded int (e.g. rotate: 99999999) reaches
    # PIL's Image.rotate and raises OverflowError at render — a 500 after /reload reported success.
    # Restrict to the valid orientation enum up front.
    if rotate not in VALID_ROTATIONS:
        raise TemplateLoadError(
            f"{source_name}: 'rotate' must be one of {sorted(VALID_ROTATIONS)}, got {rotate}"
        )

    # Top-level vertical placement of the whole composed block within a fixed die-cut canvas.
    # Defaults to "top" (the historical top-anchored stack); "center"/"bottom" only take effect on
    # die-cut media with leftover height. Validated against the same VALIGN_CHOICES the row/column
    # cross-axis alignment uses, so an out-of-range value is a load error, not a silent fallback.
    valign = str(raw.get("valign", "top"))
    _require_choice(source_name, "template", "valign", valign, VALIGN_CHOICES)

    fields_spec = raw.get("fields", {})
    if not isinstance(fields_spec, dict):
        raise TemplateLoadError(f"{source_name}: 'fields' must be a mapping")
    required_raw = fields_spec.get("required", [])
    optional_raw = fields_spec.get("optional", [])
    if not isinstance(required_raw, list) or not isinstance(optional_raw, list):
        raise TemplateLoadError(f"{source_name}: 'fields.required'/'fields.optional' must be lists")
    required_fields: list[str] = [str(f) for f in required_raw]
    optional_fields: list[str] = [str(f) for f in optional_raw]

    # Defence in depth behind the editor's textContent rendering: a field name flows to
    # /templates/parse and is interpolated into the studio's generated form. Reject any name outside
    # a conservative charset (which is also a subset of the {{token}} grammar, so a declarable name is
    # always substitutable — see FIELD_NAME_RE) so a name like `<img src=x onerror=...>` can never
    # reach a consumer and a hyphen/dot/space name can never render as a literal placeholder. The
    # server enforces this regardless of which UI (or none) calls the endpoint.
    bad_names = sorted(
        f for f in required_fields + optional_fields if not FIELD_NAME_RE.fullmatch(f)
    )
    if bad_names:
        raise TemplateLoadError(
            f"{source_name}: invalid field name(s) {bad_names}; field names must match "
            f"{FIELD_NAME_RE.pattern} (letters, digits, '_'; 1-64 chars)"
        )

    # Reject any user field whose name collides with a computed token.  The resolver returns
    # the computed value before consulting request fields, so a field named "seq" (or "date"/"now")
    # would silently shadow the supplied value — the user's data is ignored without any warning.
    reserved_collision = sorted(
        f for f in required_fields + optional_fields if f in COMPUTED_TOKENS
    )
    if reserved_collision:
        raise TemplateLoadError(
            f"{source_name}: field name(s) {reserved_collision} are reserved for computed tokens "
            f"({sorted(COMPUTED_TOKENS)}); rename the field(s) — e.g. 'seq' is used for "
            f"auto-numbering and cannot be declared as a user field"
        )

    layout = raw.get("layout", [])
    if not isinstance(layout, list) or not layout:
        raise TemplateLoadError(f"{source_name}: 'layout' must be a non-empty list")

    for i, el in enumerate(layout):
        _validate_element(source_name, f"layout[{i}]", el)

    # Per-element caps bound one element; this bounds the WHOLE layout (count + cumulative height) so
    # a layout of hundreds of individually valid elements cannot OOM the worker before compose.
    _validate_layout_budget(source_name, layout)

    # Reject {{...}} spans the engine could never substitute (a hyphen/dot/space name, stray inner
    # spaces) BEFORE the undeclared-token check: such a span matches no token (engine._FIELD_RE), so
    # unresolved_tokens would see nothing to reject while the renderer prints the literal placeholder
    # on the label — a wrong-label failure. This keeps a declarable token always a substitutable one,
    # the inline-text counterpart to the FIELD_NAME_RE charset on declared fields.
    bad_placeholders = malformed_placeholders(layout)
    if bad_placeholders:
        raise TemplateLoadError(
            f"{source_name}: layout contains malformed placeholder(s) {bad_placeholders} that the "
            f"renderer cannot substitute (a token must match {{{{name}}}} with name in letters, "
            f"digits, '_'); fix the token name or remove the braces"
        )

    # Reject {{tokens}} that nothing can fill: an undeclared field or a removed feature would
    # otherwise substitute to "" at print time and emit a silently blank label.
    unknown_tokens = unresolved_tokens(layout, required_fields + optional_fields)
    if unknown_tokens:
        raise TemplateLoadError(
            f"{source_name}: layout references undeclared field token(s) {unknown_tokens}; "
            f"declare them under fields.required/optional or remove the token "
            f"(computed tokens {sorted(COMPUTED_TOKENS)} are always available)"
        )

    # Reject a field used by BOTH an image element and a text template. An image field carries a
    # base64 blob and is exempt from the text-size cap (MAX_TEXT_FIELD_CHARS); if the same field
    # also feeds a {{token}} in text/data/name, a large value would render as text unguarded and
    # defeat the render-time allocation cap. Distinct names keep the image and text caps separate.
    image_text_collision = sorted(image_field_names(layout) & referenced_field_tokens(layout))
    if image_text_collision:
        raise TemplateLoadError(
            f"{source_name}: field(s) {image_text_collision} are read by both an image element and "
            f"a text template; an image field is exempt from the text-size cap, so rendering it as "
            f"text would bypass that guard — use distinct field names for the image and the text"
        )

    return Template(
        name=name,
        description=description,
        label=label,
        rotate=rotate,
        required_fields=required_fields,
        optional_fields=optional_fields,
        layout=layout,
        source_path=source_path,
        valign=valign,
    )


def load_template(path: Path) -> Template:
    """Load and validate a single template YAML file.

    Parses the file, then delegates ALL schema validation to :func:`build_template_from_mapping`
    so the file loader and the in-memory draft validator share one validation path verbatim.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateLoadError(f"{path.name}: YAML parse error: {exc}") from exc
    return build_template_from_mapping(raw, path.name, path)


def validate_template_from_string(yaml_text: str, source_name: str = "<draft>") -> Template:
    """Validate raw template YAML text into a :class:`Template` WITHOUT touching the filesystem.

    Backs the draft studio: a user-supplied YAML body is parsed and run through exactly the
    same schema validation as a saved file (:func:`build_template_from_mapping`), but no file is
    read or written and the returned Template carries the :data:`DRAFT_SOURCE_PATH` sentinel. A
    malformed YAML body raises :class:`TemplateLoadError` (the caller maps it to a 422) rather than
    surfacing an uncaught ``yaml.YAMLError``.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise TemplateLoadError(f"{source_name}: YAML parse error: {exc}") from exc
    return build_template_from_mapping(raw, source_name, DRAFT_SOURCE_PATH)


class TemplateRegistry:
    """Hot-reloadable registry of all templates in a directory."""

    def __init__(self, templates_dir: Path, example_dir: Path | None = None) -> None:
        self.templates_dir = templates_dir
        # Bundled examples baked outside the templates_dir volume (config.example_templates_dir).
        # Loaded IN ADDITION to templates_dir so a bind-mount can't shadow them and upgrades ship new
        # examples. ``None`` (or a path equal to templates_dir) means "single dir" — the common
        # bare-metal/dev case where templates_dir already holds the shipped examples.
        self.example_dir = example_dir
        self._templates: dict[str, Template] = {}
        self._errors: list[str] = []

    def load_all(self) -> list[str]:
        """(Re)load all .yaml files from the user dir and the bundled-example dir; return loaded names.

        Files that fail to parse/validate are skipped (so the rest stay available) and their
        errors are retained in :attr:`errors` for the caller to surface — a reload must not report
        success while a malformed file has silently dropped a template.

        Two distinct USER files declaring the same internal ``name`` are NOT silently merged: the
        registry indexes by internal name, so a later file would otherwise overwrite an earlier one and
        the winner would depend on filename sort order. Instead the FIRST file in sort order keeps the
        name (deterministic, independent of which file was edited last) and every later duplicate is
        rejected with an error naming BOTH files and the shared name. Because the error lands in
        :attr:`errors`, the server-save route's ``if errors:`` branch rolls back a save that introduces
        a duplicate, and ``/reload`` surfaces it as a 422.

        The bundled-example dir is loaded AFTER the user dir and only fills names the user has not
        already defined: a user template with the same internal ``name`` as a bundled example silently
        shadows it (the intended override — NOT a duplicate error). A malformed or symlinked bundled
        example is logged but never added to :attr:`errors`, so shipped-content problems can't block a
        user's save or fail ``/reload``.
        """
        loaded: dict[str, Template] = {}
        errors: list[str] = []

        # User dir first — its files take precedence and are the only source of user-actionable errors.
        self._load_dir(self.templates_dir, loaded, errors, is_example=False)
        # Bundled examples fill in the rest. Skip when there is no separate example dir (dev/bare-metal
        # where it resolves to templates_dir), else the same files would be scanned twice. Compare
        # RESOLVED paths so equivalent-but-differently-spelled dirs (relative vs absolute, ``.``/``..``
        # components, symlinks) for the same physical directory still collapse to a single pass.
        if (
            self.example_dir is not None
            and self.example_dir.resolve() != self.templates_dir.resolve()
        ):
            self._load_dir(self.example_dir, loaded, errors, is_example=True)

        if errors:
            log.warning("%d template(s) failed to load", len(errors))

        self._templates = loaded
        self._errors = errors
        return list(loaded.keys())

    def _load_dir(
        self,
        directory: Path,
        loaded: dict[str, Template],
        errors: list[str],
        *,
        is_example: bool,
    ) -> None:
        """Load ``directory/*.yaml`` into ``loaded``/``errors`` in filename-sort order.

        ``is_example`` flips two behaviours: a name that already exists is treated as an intended
        override (silent skip) rather than a duplicate error, and per-file failures (symlink, parse)
        are logged but NOT appended to ``errors`` — bundled content must never gate user saves.
        """
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.yaml")):
            # Never load a symlinked template file. ``glob`` follows symlinks, so a link such as
            # ``templates/x.yaml -> /elsewhere/valid.yaml`` whose target is valid YAML would otherwise
            # enter the registry and expose a file OUTSIDE the directory to every consumer (render,
            # preview, and the studio's source-load endpoint). Rejecting it here — at the layer that
            # owns ingestion — keeps the registry's contents confined to real files under a known
            # directory, so "load only what's under this dir" holds for all downstream paths.
            if path.is_symlink():
                msg = f"{path.name}: skipped — symlinked template files are not loaded (security)"
                log.warning("%s", msg)
                if not is_example:
                    errors.append(msg)
                continue
            try:
                t = load_template(path)
            except TemplateLoadError as exc:
                log.error("Failed to load template %s: %s", path.name, exc)
                if not is_example:
                    errors.append(str(exc))
                continue
            existing = loaded.get(t.name)
            if existing is not None:
                if is_example:
                    # A user template (or an earlier example) already claimed this internal name; the
                    # bundled example is intentionally shadowed — skip it silently, NOT an error.
                    log.debug(
                        "Bundled example %r (%s) shadowed by %s",
                        t.name,
                        path.name,
                        existing.source_path.name,
                    )
                    continue
                # Two USER files share an internal name. Keep the first (sort order) so the registry
                # stays deterministic, and reject this duplicate with an error naming BOTH files.
                msg = (
                    f"{path.name}: internal template name {t.name!r} is already declared by "
                    f"{existing.source_path.name}; template names must be unique — rename one file's "
                    f"'name'. Keeping {existing.source_path.name} (first in sort order)."
                )
                log.error("%s", msg)
                errors.append(msg)
                continue
            t.is_example = is_example
            loaded[t.name] = t
            log.debug("Loaded template %r from %s", t.name, path.name)

    @property
    def errors(self) -> list[str]:
        """Per-file errors from the most recent :meth:`load_all` (empty if all loaded)."""
        return self._errors

    def get(self, name: str) -> Template | None:
        return self._templates.get(name)

    def all(self) -> list[Template]:
        return list(self._templates.values())

    def __len__(self) -> int:
        return len(self._templates)
