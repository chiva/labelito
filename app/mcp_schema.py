# SPDX-License-Identifier: GPL-3.0-or-later
"""The template-authoring reference served as an MCP resource (``docs://template-schema``).

An MCP client that can render, print and save templates still has to *write* the YAML, and nothing
in the tool surface describes the schema: ``list_templates`` gives field contracts, ``get_template``
gives one example body. Discovering the element vocabulary, the ``{{token}}`` grammar or the two
icon-resolution modes otherwise means reading several bundled examples and guessing from what
renders — a blank icon and a silently clipped line look identical to a correct label.

**Why not just serve docs/template-format.md?** That 500-line human reference covers the same
language more thoroughly, and duplicating it would be the wrong instinct — except ``.dockerignore``
deliberately keeps ``docs/`` out of the image ("no runtime benefit"), so the file simply is not
there at runtime. What the container does have is the renderer itself, which is a better source
anyway: generated from the code, this cannot drift the way a second prose document would.
``tests/test_mcp_schema.py`` asserts the two never diverge on which element types they cover.

**Generated, not written.** The element tables come from :data:`ELEMENT_REGISTRY` and each element's
dataclass fields, and the vocabularies from the engine's own constants, so the document cannot drift
from the renderer: a new element type, field or default appears here the moment it is added. Only
the prose — the parts that live in behaviour rather than in a declaration — is authored, and
``tests/test_mcp_schema.py`` pins it against the code it describes.

This module deliberately reads ``app.render.engine``'s canonical private patterns
(``_FIELD_RE``/``_TEMPLATED_ATTRS``): being the documentation *of* that engine, restating them here
would create the very second source of truth the generation is meant to avoid.
"""

from __future__ import annotations

import dataclasses

from app.loader import ALIAS_CHARSET, MAX_TEMPLATE_ALIASES, MAX_TEXT_LINES
from app.render.elements import (
    ELEMENT_REGISTRY,
    FA_STYLES,
    ICON_ASSET_EXTS,
    ICON_DEFAULT_STYLE,
    KNOWN_COLLECTIONS,
    VALIGN_CHOICES,
    ElementBase,
)
from app.render.engine import _FIELD_RE, _TEMPLATED_ATTRS, COMPUTED_TOKENS

# Element dataclass fields that a template must never set: the engine owns them (see
# app.render.elements.build_element, which filters both out of any incoming spec).
ENGINE_OWNED_FIELDS = frozenset({"scale"})

# Documented once as "shared by every element" instead of repeated in all 13 tables.
_SHARED_FIELDS = frozenset(f.name for f in dataclasses.fields(ElementBase))


def _default_cell(field: dataclasses.Field[object]) -> str:
    """Render a dataclass field's default for a markdown table cell.

    A field with neither a default nor a factory is required in the YAML — surfacing dataclasses'
    ``MISSING`` sentinel repr instead would be actively misleading.
    """
    if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
        return "**required**"
    if field.default_factory is not dataclasses.MISSING:
        return f"`{field.default_factory()!r}`"
    return f"`{field.default!r}`"


def _summary(cls: type[ElementBase]) -> str:
    """First paragraph of *cls*'s docstring, or "" when it has none.

    ``@dataclass`` synthesizes ``__doc__`` from the generated ``__init__`` signature for a class
    that declares none, which would put a wall of constructor noise in the table header. Detect that
    synthetic form by its ``ClassName(`` prefix and treat it as absent.
    """
    doc = (cls.__doc__ or "").strip()
    if not doc or doc.startswith(f"{cls.__name__}("):
        return ""
    return doc.split("\n\n")[0].replace("\n", " ").strip()


def _field_rows(cls: type[ElementBase], *, shared: bool) -> list[str]:
    """Markdown table rows for *cls*'s own fields (or, with ``shared``, the inherited ones)."""
    rows: list[str] = []
    for field in dataclasses.fields(cls):
        if field.name.startswith("_") or field.name in ENGINE_OWNED_FIELDS:
            continue
        if (field.name in _SHARED_FIELDS) is not shared:
            continue
        if field.name == "type":
            continue
        rows.append(f"| `{field.name}` | `{field.type}` | {_default_cell(field)} |")
    return rows


def _offset_units() -> str:
    """The date-offset units the engine accepts, read out of its own pattern."""
    # Group 2 of _FIELD_RE is the optional offset; its unit set is the bracket expression at the end.
    pattern = _FIELD_RE.pattern
    units = pattern[pattern.index("[dwmy") : pattern.index("]", pattern.index("[dwmy")) + 1]
    return units


def template_schema_markdown() -> str:
    """Build the full template-authoring reference."""
    element_sections: list[str] = []
    for name in sorted(ELEMENT_REGISTRY):
        cls = ELEMENT_REGISTRY[name]
        rows = _field_rows(cls, shared=False)
        body = (
            "\n".join(["| field | type | default |", "| --- | --- | --- |", *rows])
            if rows
            else "_No fields of its own — only the shared ones above._"
        )
        summary = _summary(cls)
        heading = f"### `{name}`\n\n{summary}\n\n" if summary else f"### `{name}`\n\n"
        element_sections.append(f"{heading}{body}")

    shared_rows = _field_rows(ElementBase, shared=True)
    computed = ", ".join(f"`{{{{{t}}}}}`" for t in sorted(COMPUTED_TOKENS))
    templated = ", ".join(f"`{a}`" for a in _TEMPLATED_ATTRS)
    collections = ", ".join(f"`{c}`" for c in sorted(KNOWN_COLLECTIONS))
    fa_styles = ", ".join(f"`{s}`" for s in sorted(FA_STYLES))
    exts = " then ".join(f"`{e}`" for e in ICON_ASSET_EXTS)
    valigns = ", ".join(f"`{v}`" for v in sorted(VALIGN_CHOICES))

    return f"""# labelito template schema

A template is one YAML file. Validate a draft with `validate_template`, see it with
`preview_ephemeral_label`, and persist it with `save_template` (when enabled).

## Envelope

```yaml
name: my-label            # required; ALSO the registry key and the saved file name
description: What it is   # required
label: "62"               # required; a label id from get_capabilities (quote it — "62" is a string)
rotate: 0                 # 0, 90, 180 or 270
valign: top               # {valigns} — vertical placement on die-cut media with leftover height
aliases: [my label]       # optional; other ways a PERSON SAYS this name, for voice matching
fields:
  required: [title]       # values a print MUST supply
  optional: [subtitle]    # values a print MAY supply
layout:                   # ordered list of elements, rendered top to bottom
  - {{type: title, text: "{{{{title}}}}"}}
```

`fields` and `layout` must agree, in both directions:

* Declaring a field does not place it — `layout` does, by referencing `{{{{field}}}}`.
* **Every `{{{{field}}}}` a layout references must be declared**, or the template is rejected with
  "layout references undeclared field token(s)". The computed tokens are the exception: they are
  always available and must NOT be declared.

`aliases` is for voice: other ways a person SAYS this name, so an assistant matching speech against
the catalog accepts them and reports back the canonical `name`. Printing is always by `name` — an
alias is never a lookup key. Up to {MAX_TEMPLATE_ALIASES} per template, and {ALIAS_CHARSET}.
Unlike `name`, an alias may carry spaces and accents.

Declaring the template's own name is rejected, and so is the same alias twice — compared after
case AND spacing are normalized, so `"my label"` and `"my  label"` count as one. Aliases are
stored NFC-normalized, so it does not matter whether an accent was typed precomposed or as a
combining mark. And so is an entry YAML already turned into something else: `aliases: [no]` is the
boolean `False` in YAML 1.1, so quote any alias that is a bare
`yes`/`no`/`on`/`off`/`true`/`false`/`null`/`~` or looks numeric.

## Tokens

Substituted in these element attributes only: {templated}.

* `{{{{field}}}}` — a value from the print request.
* {computed} — resolved by the engine, always available, and never part of the field contract.
  `seq` is the auto-numbering counter and renders empty outside a `sequence` batch.
* `{{{{date+6m}}}}` — a date offset: a sign, a count, and a unit from `{_offset_units()}`
  (days, weeks, months, years). **Negative offsets work too**: `{{{{date-7d}}}}`.
* `{{{{date:%d/%m/%Y}}}}` — a strftime format. Weekday and month names are localized to the
  request language, so `%A` gives "martes" for `language: es`.

Both can combine: `{{{{date+6m:%d %b %Y}}}}`.

## Translations

`[[key]]` resolves against the translation catalog for the request language, so one template serves
every language instead of being duplicated per locale.

## Icons

`icon` has **two resolution modes**, and picking the wrong one renders nothing at all rather than
failing:

* **`collection` set** — a bundled SVG from {collections}. FontAwesome also takes a `style`
  ({fa_styles}, default `{ICON_DEFAULT_STYLE}`); the other collections ignore it.
  Use `list_icons` to find a name.
* **`collection` unset** — a file you placed in the icons directory, resolved as {exts}.

```yaml
- {{type: icon, collection: fontawesome, style: solid, name: snowflake, size: 90}}
```

## Shared element fields

Every element accepts these, whatever its `type`:

| field | type | default |
| --- | --- | --- |
{chr(10).join(shared_rows)}

`padding_*` may instead be written with the CSS-style `padding` shorthand: a scalar, `[v, h]`,
`[t, h, b]` or `[t, r, b, l]`. `width`, `weight` and `valign` are layout hints honoured only when
the element is a child of a `row`, and inert elsewhere.

## Element types

{chr(10).join(f"{chr(10)}{section}" for section in element_sections)}

## Gotchas

* **Text is clipped, not shrunk.** `size` is a fixed value and `max_lines` a hard cap; text that
  exceeds either is cut with no ellipsis, no warning and no error. A `title` defaults to
  `max_lines: 2`, so a long value silently loses its tail — raise it to an integer in
  `[1, {MAX_TEXT_LINES}]`. There is **no "unlimited"**: `null` and `0` are both rejected by the
  loader, and omitting the key restores the per-element default rather than removing the cap.

  ```yaml
  - {{type: title, text: "{{{{title}}}}", max_lines: 8}}
  ```

  Always confirm a design with `preview_ephemeral_label` at the longest value you expect, not a
  short one.
* **A tape is only as wide as its label.** Continuous media grows downward for free, so extra
  lines cost nothing; horizontal overflow is what gets cut. Check widths against
  `get_capabilities`.
* **Unknown element keys are ignored silently**, so a typo'd field name is a no-op rather than an
  error. If a property seems to do nothing, check its spelling against the table above.
"""
