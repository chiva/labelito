# Template format

A labelito template is a small YAML file in the templates directory (one template per file,
`*.yaml`). It declares which **label media** to print on, which **fields** a caller supplies, and a
**layout** of elements stacked top-to-bottom. The same file is validated identically whether it is
loaded from disk at startup or typed into the Template Studio (`/editor`) — the studio's live preview
is gated by exactly the checks below.

This document is the authoritative reference for every parameter. It is sourced from the validator
(`app/loader.py`), the element renderers (`app/render/elements.py`), and the token/i18n engine
(`app/render/engine.py`); every example here parses through that validator.

---

## Top-level keys

| Key | Required | Type | Notes |
|---|---|---|---|
| `name` | **yes** | string | Internal id and the registry key. Must be unique across all template files; two files with the same `name` is a load error. Also the default download/save filename in the studio. |
| `description` | **yes** | string | Human-readable summary shown in the template picker. The picker groups templates by the `label` size denomination and, on an SNMP printer, focuses the group matching the loaded roll. |
| `label` | **yes** | string | The brother_ql label id the template prints on (e.g. `"62"`, `"62x29"`). Quote it so `62` is not parsed as an integer. See [Choosing a label](#choosing-a-label). |
| `layout` | **yes** | list | Non-empty list of [layout elements](#layout-elements), rendered top-to-bottom. |
| `rotate` | no | int | Quarter-turn orientation applied to the whole label. One of `0`, `90`, `180`, `270` (default `0`). Any other value is rejected. |
| `fields` | no | mapping | Declares the [fields](#fields) a caller supplies. Omit it for a fully static label. |

A template missing any of `name`, `description`, `label`, `layout` is rejected. The top-level node
must be a mapping; a uniformly-indented root mapping is accepted as long as it is consistent.

### Choosing a label

`label` must be a label id the configured printer model supports. The Template Studio's **Label
reference** panel (bottom of `/editor`) lists every supported id with the media it requires, and —
when the printer answers SNMP — flags the loaded roll and the matching id(s). Picking a `label`
whose media does not match the loaded roll is rejected at print time with `409 Conflict` (the
media-compatibility guard), so it is worth matching it to your roll up front.

---

## Fields

`fields` declares the substitutable values a caller passes at print time:

```yaml
fields:
  required: [title]
  optional: [subtitle, quantity]
```

- **`required`** — fields a caller must supply. A `/print` without one is rejected.
- **`optional`** — fields a caller may supply; absent ones substitute to an empty string (the
  element renders blank and contributes no height).

Rules enforced by the loader:

- **Field names** match `[A-Za-z0-9_]` and are 1–64 characters. Hyphens, dots, and spaces are
  rejected — the name must also be a valid `{{token}}`, so a name that could never be substituted is
  refused up front.
- **Declared ⇔ referenced.** Every declared field must be referenced by a `{{token}}` somewhere in
  the layout, and every `{{token}}` must resolve to a declared field or a [computed token](#computed-tokens).
  A `{{token}}` with no matching field is rejected (it would otherwise print a blank label); a
  malformed placeholder like `{{asset-id}}` is rejected too.
- **Reserved names.** A field may not be named `date`, `now`, or `seq` — those are computed tokens
  and would shadow the caller's value.

### Per-field input caps (enforced at print/preview time)

| Cap | Value |
|---|---|
| Max fields per request | 50 |
| Max field-name length | 100 chars |
| Max rendered text length per field | 1000 chars |
| Max image field size | 5 MiB decoded (≈6.7 MB base64), 16 MP |

---

## Tokens

### Field tokens

`{{fieldname}}` is replaced by the caller-supplied value. Tokens appear inside the `text`, `data`,
and `name` attributes of elements (the only templated attributes). A token may be surrounded by
literal text: `"{{endpoint_a}}  →  {{endpoint_b}}"`.

### Computed tokens

Always available without being declared as fields:

| Token | Resolves to | Options |
|---|---|---|
| `{{date}}` | Current date | Offset `±N` with unit `d`/`w`/`m`/`y` (e.g. `{{date+6m}}` = six months ahead). strftime format via `:` (e.g. `{{date:%Y-%m-%d}}`). Default format is locale-driven (`%d/%m/%Y`). |
| `{{now}}` | Current date+time | Same offset/format options (e.g. `{{now:%H:%M}}`). |
| `{{seq}}` | Per-item sequence number | Only meaningful in a sequence batch (a `/print` with a `sequence` spec). Numbered `start + index*step`, optionally zero-padded. A template that uses `{{seq}}` printed **without** a sequence spec is rejected (it would print a blank number). |

A sequence spec on the `/print` request controls `{{seq}}`:

| Field | Default | Range |
|---|---|---|
| `start` | `1` | `-1e9 … 1e9` |
| `count` | — (required) | `1 … 500` |
| `step` | `1` | `1 … 1e6` |
| `padding` | `0` (no padding) | `0 … 32` |

### Translation tokens (i18n)

`[[key]]` is replaced by the active language's catalog word (from `translations/<lang>.yaml`). A
distinct delimiter keeps these from colliding with `{{field}}` substitution. Translation runs first,
then field/computed substitution — so a translated word is never re-interpreted as a field.

```yaml
- {type: text, text: "[[frozen]]: {{date}}", size: 28, align: center}
```

---

## Layout elements

Each layout entry is a mapping with a `type` and per-type attributes. Elements render top-to-bottom
into a single vertical stack at the label's printable width.

### Attributes common to every element

| Attribute | Type | Default | Notes |
|---|---|---|---|
| `padding_top` | int ≥ 0 | `4` | Vertical inset above the element (template px). |
| `padding_bottom` | int ≥ 0 | `4` | Vertical inset below. |
| `color` | enum | `black` | `red` draws this element in the red layer — honoured only on two-color models when the print resolves `red=true`; otherwise it draws black (output is byte-identical to a plain label). |
| `width`, `weight`, `valign` | — | — | Column hints honoured **only** inside a [`row`](#row); inert on a stand-alone element. |

Unknown attributes on an element are ignored (except `children`, which is allowed only on a `row`).

### Element types

#### `title` / `subtitle`

Bold (title) or regular (subtitle) heading text. Fixed font sizes (60 pt / 40 pt) — the author
cannot change the size, only `max_lines`.

| Attribute | Type | Default |
|---|---|---|
| `text` | string (templated) | `""` |
| `align` | `left`/`center`/`right` | `left` |
| `max_lines` | int 1–200 | `2` |
| `bold` | bool | `true` (title) / `false` (subtitle) |

```yaml
- {type: title, text: "{{title}}", max_lines: 2, align: center}
- {type: subtitle, text: "{{subtitle}}", max_lines: 2, align: center}
```

#### `text`

Body text with an author-controlled font size.

| Attribute | Type | Default |
|---|---|---|
| `text` | string (templated) | `""` |
| `size` | int 1–512 (pt) | `32` |
| `align` | `left`/`center`/`right` | `left` |
| `bold` | bool | `false` |
| `max_lines` | int 1–200 | `10` |

`size × max_lines` is additionally bounded (≤ 4000) so a large font and many lines cannot compose an
unbounded strip.

```yaml
- {type: text, text: "{{line1}}", size: 26, align: left}
```

#### `qr`

A QR code rendered from the `data` attribute.

| Attribute | Type | Default |
|---|---|---|
| `data` | string (templated) | `""` |
| `size` | int 1–2000 (px square) | `160` |
| `align` | `left`/`center`/`right` | `center` |

```yaml
- {type: qr, data: "{{qr}}", size: 140, align: right}
```

#### `barcode`

A 1-D barcode rendered from the `data` attribute.

| Attribute | Type | Default |
|---|---|---|
| `data` | string (templated) | `""` |
| `symbology` | string | `code128` |
| `height` | int 1–10000 (px) | `60` |
| `align` | `left`/`center`/`right` | `center` |

```yaml
- {type: barcode, data: "{{asset_id}}", symbology: code128, height: 70, align: center}
```

#### `image`

A caller-supplied image, drawn from a **field** (base64 in the request, or a multipart upload). The
field named here is exempt from the text-size cap, so it must not also feed a text `{{token}}`.

| Attribute | Type | Default |
|---|---|---|
| `field` | non-empty string (field name) | `image` |
| `max_height` | int 1–10000 (px) | `200` |
| `align` | `left`/`center`/`right` | `center` |

#### `icon`

A named server-side graphic. Two sources, selected by `collection`:

- **No `collection`** — a custom asset in the icons directory (`<name>.svg` preferred, then `<name>.png`).
- **`collection` set** — a bundled glyph. Valid collections: `fontawesome`, `material`, `octicons`.
  FontAwesome also takes a `style`: `solid` (default), `regular`, `brands`.

| Attribute | Type | Default |
|---|---|---|
| `name` | string (templated) | `""` |
| `size` | int 1–2000 (px square) | `80` |
| `align` | `left`/`center`/`right` | `center` |
| `collection` | enum or unset | `""` (custom asset) |
| `style` | enum (FontAwesome only) | `solid` |

```yaml
- {type: icon, collection: fontawesome, style: solid, name: mug-hot, size: 90, align: right}
```

A missing, unknown, or unsafe icon reference renders a blank strip — the label still prints.

#### `line`

A horizontal rule.

| Attribute | Type | Default |
|---|---|---|
| `thickness` | int 1–10000 (px) | `2` |
| `margin` | int 0–10000 (px) | `8` |

```yaml
- {type: line}
```

#### `box`

A rectangular outline.

| Attribute | Type | Default |
|---|---|---|
| `height` | int 1–10000 (px) | `40` |
| `border` | int 0–10000 (px) | `2` |

#### `spacer`

Vertical whitespace.

| Attribute | Type | Default |
|---|---|---|
| `size` | int 0–10000 (px) | `16` |

```yaml
- {type: spacer, size: 12}
```

#### `row`

A horizontal band that lays its children out side-by-side in columns. One level of nesting only — a
`row` may not contain another `row`.

| Attribute | Type | Default |
|---|---|---|
| `children` | non-empty list of elements | — (required) |
| `align_items` | `top`/`center`/`bottom` | `center` |
| `spacing` | int 0–10000 (px) | `8` |

Each child may carry column hints (inert outside a row):

| Child attribute | Type | Default | Notes |
|---|---|---|---|
| `width` | int ≥ 1 (px) or `null` | `null` | Fixed column width. `null`/absent ⇒ a flexible column sharing leftover space. |
| `weight` | int ≥ 0 | `1` | A flexible column's share of the leftover width. |
| `valign` | `top`/`center`/`bottom` or `""` | `""` | Per-child vertical alignment; `""` inherits the row's `align_items`. |

```yaml
- type: row
  align_items: center
  children:
    - {type: title, text: "{{title}}", align: left, max_lines: 2}
    - {type: icon, name: check, collection: fontawesome, size: 64, width: 80, align: right}
```

---

## Layout-wide limits

| Limit | Value | Why |
|---|---|---|
| Max elements per layout | 64 (counting row children) | Bounds a "thousand tiny elements" allocation. |
| Max combined declared height | ~40000 px | A label taller than the printer's maximum raster cannot print anyway. |
| Max single pixel dimension | 10000 px | Bounds any one element's allocation. |
| Max QR/icon square dimension | 2000 px | Square allocation is quadratic. |
| Max font size | 512 pt | Quadratic with `max_lines`. |
| Max template YAML size | 64 KiB | A real template is tiny. |

---

## A complete example

```yaml
name: freezer-dated
description: Freezer label — always stamps storage date automatically
label: "62"
rotate: 0
fields:
  required: [title]
  optional: [subtitle]
layout:
  - {type: spacer, size: 8}
  - {type: title, text: "{{title}}", max_lines: 2, align: center, bold: true}
  - {type: subtitle, text: "{{subtitle}}", max_lines: 1, align: center}
  - {type: line}
  - {type: text, text: "[[frozen]]: {{date}}", size: 28, align: center}
  - {type: text, text: "[[expires]]: {{date+6m}}", size: 28, align: center}
  - {type: spacer, size: 8}
```

This declares one required field (`title`) and one optional (`subtitle`), prints on a 62 mm
continuous roll, and stamps the current date plus a six-month expiry using computed tokens and the
`[[frozen]]`/`[[expires]]` translation keys. Every shipped template under `templates/` is a working
reference for these parameters.
