// SPDX-License-Identifier: GPL-3.0-or-later
//
// Visual label builder for the Template Studio — an alternative to hand-writing the YAML DSL.
//
// It maintains an in-memory layout MODEL and serialises it back to YAML, so the whole existing
// Studio machinery (field detection, the pixel-accurate server preview, draft print, save) is reused
// verbatim: every edit re-emits the YAML into the hidden #yaml textarea and calls the page's own
// doPreview(). The reverse direction (open an existing YAML template in the builder) is served by the
// server via POST /templates/parse-layout, since the browser has no YAML parser.
//
// This file is a STATIC asset (not Jinja-rendered), so the {{token}} / [[key]] placeholders below are
// literal text, never template variables. It reuses labelito.js helpers (api/authHeaders/debounce)
// and editor.html globals (doPreview/syncYamlHighlight/showStatus/gatherFields/loadByName/useLabel).
//
// Security discipline mirrors the rest of the page: all user/template-supplied strings are rendered
// with textContent / DOM nodes, never innerHTML — this page holds the API token in localStorage.

(function () {
  'use strict';

  // ── Element schema ────────────────────────────────────────────────────────────
  // One descriptor per element type, mirroring app/render/elements.py defaults and app/loader.py
  // bounds. This single table drives BOTH the palette and the inspector, so "full parity" is data,
  // not thirteen hand-built panels. `control` picks the inspector widget; `text` marks the templated
  // attribute a block edits inline on the canvas. Only real, renderer-honoured knobs appear here
  // (there is no font-family in the DSL, and title/subtitle sizes are fixed — so no `size` on them).
  const ALIGN = ['left', 'center', 'right'];
  const VALIGN = ['top', 'center', 'bottom'];
  const COLORS = ['black', 'red'];
  const BADGE = ['none', 'black', 'red'];
  const MARKERS = ['bullet', 'number', 'none'];
  const COLLECTIONS = ['', 'fontawesome', 'material', 'octicons'];
  const FA_STYLES = ['solid', 'regular', 'brands'];
  const SYMBOLOGIES = [
    'code128', 'code39', 'code93', 'ean13', 'ean8', 'ean14',
    'gs1_128', 'isbn13', 'issn', 'itf', 'jan', 'pzn', 'upca',
  ];

  // Attributes shared by every element (ElementBase). Paddings live in an "Advanced" group so the
  // common panel stays short. `color` is only meaningful on two-color models but is harmless elsewhere.
  const COMMON_ADVANCED = [
    { key: 'padding_top', label: 'Padding top', control: 'number', min: 0, max: 10000 },
    { key: 'padding_right', label: 'Padding right', control: 'number', min: 0, max: 10000 },
    { key: 'padding_bottom', label: 'Padding bottom', control: 'number', min: 0, max: 10000 },
    { key: 'padding_left', label: 'Padding left', control: 'number', min: 0, max: 10000 },
    { key: 'color', label: 'Color', control: 'select', choices: COLORS, default: 'black' },
  ];

  // Per-child column hints, honoured only when the element sits inside a `row` (inert otherwise).
  const ROW_CHILD_ATTRS = [
    { key: 'width', label: 'Column width (px)', control: 'number', min: 1, max: 10000, placeholder: 'flex' },
    { key: 'weight', label: 'Flex weight', control: 'number', min: 0, max: 10000, default: 1 },
    { key: 'valign', label: 'Cell v-align', control: 'select', choices: ['', ...VALIGN], default: '' },
  ];

  const SCHEMA = {
    title: {
      label: 'Title', badge: 'T', text: 'text',
      attrs: [
        { key: 'text', label: 'Text', control: 'text', default: '' },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'left' },
        { key: 'max_lines', label: 'Max lines', control: 'number', min: 1, max: 200, default: 2 },
        { key: 'bold', label: 'Bold', control: 'toggle', default: true },
        { key: 'background', label: 'Badge fill', control: 'select', choices: BADGE, default: 'none' },
        { key: 'border', label: 'Border (px)', control: 'number', min: 0, max: 10000, default: 0 },
        { key: 'border_color', label: 'Border color', control: 'select', choices: COLORS, default: 'black' },
      ],
    },
    subtitle: {
      label: 'Subtitle', badge: 't', text: 'text',
      attrs: [
        { key: 'text', label: 'Text', control: 'text', default: '' },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'left' },
        { key: 'max_lines', label: 'Max lines', control: 'number', min: 1, max: 200, default: 2 },
        { key: 'bold', label: 'Bold', control: 'toggle', default: false },
        { key: 'background', label: 'Badge fill', control: 'select', choices: BADGE, default: 'none' },
        { key: 'border', label: 'Border (px)', control: 'number', min: 0, max: 10000, default: 0 },
        { key: 'border_color', label: 'Border color', control: 'select', choices: COLORS, default: 'black' },
      ],
    },
    text: {
      label: 'Text', badge: '¶', text: 'text',
      attrs: [
        { key: 'text', label: 'Text', control: 'text', default: '' },
        { key: 'size', label: 'Font size (pt)', control: 'number', min: 1, max: 512, default: 32 },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'left' },
        { key: 'bold', label: 'Bold', control: 'toggle', default: false },
        { key: 'max_lines', label: 'Max lines', control: 'number', min: 1, max: 200, default: 10 },
        { key: 'background', label: 'Badge fill', control: 'select', choices: BADGE, default: 'none' },
        { key: 'border', label: 'Border (px)', control: 'number', min: 0, max: 10000, default: 0 },
        { key: 'border_color', label: 'Border color', control: 'select', choices: COLORS, default: 'black' },
      ],
    },
    list: {
      label: 'List', badge: '≡', text: 'text',
      attrs: [
        { key: 'text', label: 'Items (one string, split by separator)', control: 'text', default: '' },
        { key: 'separator', label: 'Separator', control: 'text', default: '', placeholder: '\\n' },
        { key: 'marker', label: 'Marker', control: 'select', choices: MARKERS, default: 'bullet' },
        { key: 'size', label: 'Font size (pt)', control: 'number', min: 1, max: 512, default: 32 },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'left' },
        { key: 'bold', label: 'Bold', control: 'toggle', default: false },
        { key: 'max_items', label: 'Max items', control: 'number', min: 1, max: 200, default: 20 },
      ],
    },
    qr: {
      label: 'QR code', badge: '▦', text: 'data',
      attrs: [
        { key: 'data', label: 'Data', control: 'text', default: '' },
        { key: 'size', label: 'Size (px)', control: 'number', min: 1, max: 2000, default: 160 },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'center' },
      ],
    },
    barcode: {
      label: 'Barcode', badge: '‖', text: 'data',
      attrs: [
        { key: 'data', label: 'Data', control: 'text', default: '' },
        { key: 'symbology', label: 'Symbology', control: 'select', choices: SYMBOLOGIES, default: 'code128' },
        { key: 'height', label: 'Height (px)', control: 'number', min: 1, max: 10000, default: 60 },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'center' },
        { key: 'show_value', label: 'Show value', control: 'toggle', default: false },
      ],
    },
    image: {
      label: 'Image', badge: '▣',
      attrs: [
        { key: 'field', label: 'Field name', control: 'text', default: 'image' },
        { key: 'max_height', label: 'Max height (px)', control: 'number', min: 1, max: 10000, default: 200 },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'center' },
      ],
    },
    icon: {
      label: 'Icon', badge: '★', text: 'name',
      attrs: [
        { key: 'name', label: 'Name', control: 'text', default: '' },
        { key: 'collection', label: 'Collection', control: 'select', choices: COLLECTIONS, default: '' },
        { key: 'style', label: 'FA style', control: 'select', choices: FA_STYLES, default: 'solid', when: (el) => el.collection === 'fontawesome' },
        { key: 'size', label: 'Size (px)', control: 'number', min: 1, max: 2000, default: 80 },
        { key: 'align', label: 'Align', control: 'align', choices: ALIGN, default: 'center' },
      ],
    },
    line: {
      label: 'Line', badge: '─',
      attrs: [
        { key: 'thickness', label: 'Thickness (px)', control: 'number', min: 1, max: 10000, default: 2 },
        { key: 'margin', label: 'Margin (px)', control: 'number', min: 0, max: 10000, default: 8 },
      ],
    },
    box: {
      label: 'Box', badge: '▭',
      attrs: [
        { key: 'height', label: 'Height (px)', control: 'number', min: 1, max: 10000, default: 40 },
        { key: 'border', label: 'Border (px)', control: 'number', min: 0, max: 10000, default: 2 },
        { key: 'fill', label: 'Fill solid', control: 'toggle', default: false },
      ],
    },
    spacer: {
      label: 'Spacer', badge: '␣',
      attrs: [
        { key: 'size', label: 'Height (px)', control: 'number', min: 0, max: 10000, default: 16 },
      ],
    },
    row: {
      label: 'Row', badge: '⇆', container: true,
      attrs: [
        { key: 'align_items', label: 'Align items', control: 'select', choices: VALIGN, default: 'center' },
        { key: 'spacing', label: 'Spacing (px)', control: 'number', min: 0, max: 10000, default: 8 },
        { key: 'divider', label: 'Dividers', control: 'toggle', default: false },
        { key: 'divider_thickness', label: 'Divider (px)', control: 'number', min: 1, max: 10000, default: 2 },
        { key: 'divider_color', label: 'Divider color', control: 'select', choices: COLORS, default: 'black' },
      ],
    },
    column: {
      // ⇅ (vertical) pairs with row's ⇆ (horizontal) and is clearly distinct from barcode's ‖ bars.
      label: 'Column', badge: '⇅', container: true,
      attrs: [
        { key: 'spacing', label: 'Spacing (px)', control: 'number', min: 0, max: 10000, default: 0 },
      ],
    },
  };

  // Palette order (grouped visually by kind).
  const PALETTE = ['title', 'subtitle', 'text', 'list', 'qr', 'barcode', 'image', 'icon', 'line', 'box', 'spacer', 'row', 'column'];
  const CONTAINER_TYPES = new Set(['row', 'column']);
  const COMPUTED_TOKENS = new Set(['date', 'now', 'seq']);

  const isContainer = (type) => CONTAINER_TYPES.has(type);
  const schemaOf = (type) => SCHEMA[type] || null;

  // Which container types a given list accepts (single-level grid, enforced like the loader):
  //  top level  → any element; row.children → leaf + column; column.children → leaf only.
  function allowedInList(listKind, type) {
    if (listKind === 'root') return !!SCHEMA[type];
    if (listKind === 'row') return type === 'column' || !isContainer(type);
    if (listKind === 'column') return !isContainer(type);
    return false;
  }

  // ── State ───────────────────────────────────────────────────────────────────
  const model = { name: 'my-label', description: 'A new label', label: '62', rotate: 0, valign: 'top', layout: [] };
  const fieldOptional = new Set();   // field names the user marked optional (else required)
  let selectedEl = null;             // the selected element OBJECT (survives re-render / DnD)
  let designMode = true;             // true → show {{token}} chips; false → substitute sample values
  let visualActive = false;
  let sortables = [];
  let uidCounter = 0;
  const uidMap = new Map();          // uid → element object (rebuilt each render)

  const els = {};                    // cached DOM handles, filled in init()
  let debouncedPreview = () => {};

  const nextUid = () => 'lb' + (++uidCounter);

  function createDefaultElement(type) {
    switch (type) {
      case 'title': return { type, text: 'Title' };
      case 'subtitle': return { type, text: 'Subtitle' };
      case 'text': return { type, text: 'Text' };
      case 'list': return { type, text: 'item 1; item 2', separator: ';' };
      case 'qr': return { type, data: 'https://example.com' };
      case 'barcode': return { type, data: '12345678' };
      case 'image': return { type, field: 'image' };
      case 'icon': return { type, name: 'star', collection: 'fontawesome' };
      case 'line': return { type };
      case 'box': return { type };
      case 'spacer': return { type };
      case 'row': return { type, children: [{ type: 'text', text: 'left' }, { type: 'text', text: 'right', align: 'right' }] };
      case 'column': return { type, children: [{ type: 'text', text: 'line 1' }] };
      default: return { type };
    }
  }

  // ── Token scanning ───────────────────────────────────────────────────────────
  const TOKEN_RE = /\{\{([^}]*)\}\}/g;
  // Mirror the engine's _FIELD_RE ({{(\w+)([+-]\d+[dwmy])?(?::([^}]*))?}}): a token is a field
  // reference to its leading name, optionally followed by a date offset and/or a :format. So
  // {{title}}, {{title:fmt}} and {{title+3d}} all reference the field `title`; date/now/seq are
  // computed tokens and never a user field. Extracting the name (not requiring a bare name) keeps
  // referencedFields consistent with what the loader treats as referenced — otherwise a template
  // using {{name:fmt}} on a user field would lose its declaration on re-emit and be rejected.
  const FIELD_TOKEN_RE = /^([A-Za-z0-9_]+)(?:[+-]\d+[dwmy])?(?::[^}]*)?$/;
  function fieldNameOf(inner) {
    const m = inner.match(FIELD_TOKEN_RE);
    if (!m) return null;                                      // malformed span — not a field token
    return COMPUTED_TOKENS.has(m[1]) ? null : m[1];           // date/now/seq are computed, not fields
  }
  function scanTokens(text, out) {
    if (typeof text !== 'string') return;
    TOKEN_RE.lastIndex = 0;
    let m;
    while ((m = TOKEN_RE.exec(text))) {
      const name = fieldNameOf(m[1]);
      if (name && !out.includes(name)) out.push(name);
    }
  }
  // The exact attributes the engine substitutes tokens in (app/render/engine.py `_TEMPLATED_ATTRS`).
  // Scanning these — not just the schema's single canonical `text` pointer — keeps referencedFields
  // in lockstep with the loader for ANY loadable template, e.g. one that (unusually) carries a token
  // in an off-type attr; scanning only the canonical attr would drop that field on re-emit while
  // emitLeaf still emits the token, desyncing declared⇔referenced.
  const TEMPLATED_ATTRS = ['text', 'data', 'name'];
  // Field names referenced by {{tokens}} across the whole layout, in first-seen order. The emitted
  // `fields` block is derived from exactly these, so the loader's declared⇔referenced rule always holds.
  function referencedFields(layout) {
    const out = [];
    const walk = (el) => {
      for (const k of TEMPLATED_ATTRS) scanTokens(el[k], out);
      if (Array.isArray(el.children)) el.children.forEach(walk);
    };
    (layout || []).forEach(walk);
    return out;
  }

  // ── YAML emitter (model → YAML) ──────────────────────────────────────────────
  function qstr(s) {
    return '"' + String(s)
      .replace(/\\/g, '\\\\').replace(/"/g, '\\"')
      .replace(/\n/g, '\\n').replace(/\t/g, '\\t') + '"';
  }
  function emitScalar(v) {
    if (typeof v === 'number') return String(v);
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (v === null || v === undefined) return 'null';
    // A list value (only the CSS-style `padding: [t, r, b, l]` shorthand, preserved verbatim from a
    // loaded template) must emit as a real flow sequence — NOT qstr(String(v)) = "8,4", which the
    // loader rejects as neither an int nor a list.
    if (Array.isArray(v)) return '[' + v.map(emitScalar).join(', ') + ']';
    return qstr(v);
  }
  // Ordered own keys of an element for stable, readable output: schema order first, then any extras
  // (row-child hints width/weight/valign, or unknown keys preserved from a loaded template).
  function orderedKeys(el) {
    const sc = schemaOf(el.type);
    const order = sc ? sc.attrs.map((a) => a.key) : [];
    const extra = ['width', 'weight', 'valign', 'padding_top', 'padding_right', 'padding_bottom', 'padding_left', 'padding', 'color'];
    const seen = new Set(['type', 'children']);
    const keys = [];
    for (const k of [...order, ...extra]) {
      if (k in el && !seen.has(k)) { keys.push(k); seen.add(k); }
    }
    for (const k of Object.keys(el)) {
      if (!seen.has(k)) { keys.push(k); seen.add(k); }
    }
    return keys;
  }
  function emitLeaf(el) {
    const parts = ['type: ' + el.type];
    for (const k of orderedKeys(el)) parts.push(k + ': ' + emitScalar(el[k]));
    return '{' + parts.join(', ') + '}';
  }
  // A container is only worth emitting if it will contribute at least one child. A leaf always
  // contributes; a container contributes only if it (recursively) holds an emittable descendant. This
  // must be recursive: a row whose ONLY child is an emptied column would otherwise emit a `- type:
  // row / children:` header with nothing under it (a bare `children:` the loader rejects), one level
  // up from the leaf-level skip.
  function isEmittable(el) {
    if (!el || !el.type) return false;
    if (!isContainer(el.type)) return true;
    const kids = Array.isArray(el.children) ? el.children : [];
    return kids.some(isEmittable);
  }
  function emitElement(el, indent, out) {
    if (!isContainer(el.type)) {
      out.push(indent + '- ' + emitLeaf(el));
      return;
    }
    // A container's `children` must be non-empty (loader rejects `children: []` / a bare `children:`).
    // A container can legitimately be emptied mid-build (delete its last child, or drag every child
    // out), so skip emitting an empty one rather than producing YAML that fails the whole preview —
    // the canvas still shows it as a "Drop elements here" work-in-progress until it's filled.
    const kids = (Array.isArray(el.children) ? el.children : []).filter(isEmittable);
    if (!kids.length) return;
    out.push(indent + '- type: ' + el.type);
    for (const k of orderedKeys(el)) out.push(indent + '  ' + k + ': ' + emitScalar(el[k]));
    out.push(indent + '  children:');
    for (const c of kids) emitElement(c, indent + '    ', out);
  }
  function emitYaml() {
    const out = [];
    out.push('name: ' + qstr(model.name));
    out.push('description: ' + qstr(model.description));
    out.push('label: ' + qstr(model.label));
    out.push('rotate: ' + String(model.rotate || 0));
    if (model.valign && model.valign !== 'top') out.push('valign: ' + model.valign);

    const refs = referencedFields(model.layout);
    const required = refs.filter((f) => !fieldOptional.has(f));
    const optional = refs.filter((f) => fieldOptional.has(f));
    if (required.length || optional.length) {
      out.push('fields:');
      // Quote every field name: a bare name that is a YAML 1.1 keyword (no/yes/on/off/true/false/null)
      // or looks numeric (010, 0x1f) would otherwise parse to a bool/int, so the declared field would
      // no longer string-match its {{token}} and the loader would reject a template the user just
      // built. Field names are [A-Za-z0-9_] so quoting needs no escaping, but qstr is safe regardless.
      if (required.length) out.push('  required: [' + required.map(qstr).join(', ') + ']');
      if (optional.length) out.push('  optional: [' + optional.map(qstr).join(', ') + ']');
    }
    // Collect the layout body first so a layout of only empty containers (each skipped by
    // emitElement) still falls back to the explicit empty-list marker instead of a bare `layout:`.
    const layoutLines = [];
    for (const el of model.layout) emitElement(el, '  ', layoutLines);
    out.push('layout:');
    if (!layoutLines.length) {
      // An empty layout is invalid; the explicit `[]` makes the preview report the empty label
      // cleanly rather than emitting a bare `layout:` that parses to null.
      out.push('  []');
    } else {
      out.push(...layoutLines);
    }
    return out.join('\n') + '\n';
  }

  // ── Commit: re-emit YAML → repaint highlight → server preview ─────────────────
  function commit() {
    // Only the builder owns #yaml while Visual mode is active. In YAML mode the textarea is authored
    // by hand, so a trailing debounced commit (e.g. an inline edit's timer firing just after the user
    // switched to YAML and started typing) must not clobber those manual edits.
    if (!visualActive) return;
    const yaml = emitYaml();
    els.yaml.value = yaml;
    if (typeof window.syncYamlHighlight === 'function') window.syncYamlHighlight();
    debouncedPreview();
  }

  // ── Canvas rendering ─────────────────────────────────────────────────────────
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Append `text` to `parent`, styling {{field}} / [[key]] placeholders as chips. In Preview mode a
  // {{field}} is replaced by the operator's sample value (from the shared field inputs) when present.
  function renderText(parent, text) {
    const RE = /(\{\{[^}]*\}\}|\[\[[^\]]*\]\])/g;
    const samples = (!designMode && typeof window.gatherFields === 'function') ? window.gatherFields() : {};
    let last = 0;
    let m;
    RE.lastIndex = 0;
    while ((m = RE.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      const isField = tok.startsWith('{{');
      const inner = tok.slice(2, -2).trim();
      if (!designMode && isField && Object.prototype.hasOwnProperty.call(samples, inner)) {
        parent.appendChild(document.createTextNode(samples[inner]));
      } else {
        const chip = document.createElement('span');
        chip.className = 'lb-chip-token' + (isField ? '' : ' lb-chip-i18n');
        chip.textContent = tok;
        parent.appendChild(chip);
      }
      last = m.index + tok.length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  // A one-line schematic summary for a non-text element (line/box/spacer/qr/barcode/image/icon).
  function schematicSummary(el) {
    switch (el.type) {
      case 'qr': return 'QR · ' + (el.size || 160) + 'px';
      case 'barcode': return 'Barcode · ' + (el.symbology || 'code128');
      case 'image': return 'Image · field "' + (el.field || 'image') + '"';
      case 'line': return 'Horizontal rule';
      case 'box': return 'Box · ' + (el.height || 40) + 'px' + (el.fill ? ' (filled)' : '');
      case 'spacer': return 'Spacer · ' + (el.size ?? 16) + 'px';
      default: return el.type;
    }
  }

  // Canvas font preview. The block editor is schematic, but a flat 14px for every text element hides
  // the real size hierarchy (title 60pt ≫ subtitle 40pt ≫ text's author-set size), so title/subtitle/
  // text/list all looked identically tiny and the "Font size (pt)" control appeared inert. Mirror the
  // renderer's fixed sizes (app/render/elements.py FONT_SIZES) and the author-set `size` for text/list,
  // then map points → canvas px with a scale + clamp so a preview stays legible without dwarfing a block.
  const PREVIEW_FONT_PT = { title: 60, subtitle: 40, text: 32, list: 32 };
  const PREVIEW_BOLD_DEFAULT = { title: true, subtitle: false, text: false, list: false };
  function previewFontPt(el) {
    if (el.type === 'text' || el.type === 'list') {
      const s = el.size;
      if (typeof s === 'number' && isFinite(s) && s > 0) return s;
    }
    return PREVIEW_FONT_PT[el.type] || 32;
  }
  // pt → px: 0.5 scale reads well (title 30px / subtitle 20px / text@32pt 16px), clamped so tiny or
  // huge author sizes still render a sane, non-overflowing preview line.
  function applyTextPreviewStyle(content, el) {
    if (!Object.prototype.hasOwnProperty.call(PREVIEW_FONT_PT, el.type)) return;
    const px = Math.max(12, Math.min(34, Math.round(previewFontPt(el) * 0.5)));
    content.style.fontSize = px + 'px';
    const bold = (typeof el.bold === 'boolean') ? el.bold : PREVIEW_BOLD_DEFAULT[el.type];
    content.style.fontWeight = bold ? '700' : '400';
    content.style.textAlign = (el.align === 'center' || el.align === 'right') ? el.align : 'left';
  }

  function renderBlock(el, listKind) {
    const uid = nextUid();
    uidMap.set(uid, el);
    const block = document.createElement('div');
    block.className = 'lb-block';
    block.dataset.uid = uid;
    block.dataset.type = el.type;
    if (isContainer(el.type)) block.classList.add('lb-block-container');

    // Head: drag handle + type badge + label + actions
    const head = document.createElement('div');
    head.className = 'lb-block-head';
    const handle = document.createElement('span');
    handle.className = 'lb-handle';
    handle.textContent = '☰';
    handle.title = 'Drag to reorder';
    const badge = document.createElement('span');
    badge.className = 'lb-badge';
    badge.textContent = (schemaOf(el.type) || {}).badge || '?';
    const name = document.createElement('span');
    name.className = 'lb-block-name';
    name.textContent = (schemaOf(el.type) || {}).label || el.type;
    const spacer = document.createElement('span');
    spacer.style.flex = '1';
    const dup = actionBtn('⧉', 'Duplicate', (e) => { e.stopPropagation(); duplicateBlock(el); });
    const del = actionBtn('✕', 'Delete', (e) => { e.stopPropagation(); deleteBlock(el); });
    head.append(handle, badge, name, spacer, dup, del);
    block.appendChild(head);

    // Body
    const sc = schemaOf(el.type);
    if (sc && sc.text) {
      const content = document.createElement('div');
      content.className = 'lb-content';
      content.dataset.textkey = sc.text;
      renderText(content, el[sc.text] || '');
      applyTextPreviewStyle(content, el);
      makeInlineEditable(content, el, sc.text);
      block.appendChild(content);
    } else if (isContainer(el.type)) {
      const kidsWrap = document.createElement('div');
      kidsWrap.className = 'lb-children' + (el.type === 'row' ? ' lb-children-row' : ' lb-children-col');
      kidsWrap.dataset.list = el.type;
      const kids = Array.isArray(el.children) ? el.children : [];
      for (const c of kids) kidsWrap.appendChild(renderBlock(c, el.type));
      if (!kids.length) {
        const empty = document.createElement('div');
        empty.className = 'lb-empty-hint';
        empty.textContent = 'Drop elements here';
        kidsWrap.appendChild(empty);
      }
      block.appendChild(kidsWrap);
    } else {
      const content = document.createElement('div');
      content.className = 'lb-content lb-content-schematic';
      content.textContent = schematicSummary(el);
      block.appendChild(content);
    }

    block.addEventListener('mousedown', (e) => {
      // Select on click, but don't steal the drag handle's or an action button's interaction.
      if (e.target.closest('.lb-action') || e.target.closest('[contenteditable="true"]')) return;
      e.stopPropagation();
      selectBlockByEl(el);
    });
    return block;
  }

  function actionBtn(glyph, title, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lb-action';
    b.textContent = glyph;
    b.title = title;
    b.addEventListener('click', onClick);
    return b;
  }

  // Inline text editing: focus → plain text for a clean caret; input → update model (debounced
  // commit); blur → re-render chips. The floating toolbar's "Insert field" writes into this element.
  function makeInlineEditable(content, el, key) {
    content.setAttribute('contenteditable', 'true');
    content.spellcheck = false;
    content.addEventListener('focus', () => {
      content.textContent = el[key] || '';
      selectBlockByEl(el);
    });
    content.addEventListener('input', () => {
      el[key] = content.textContent;
      debouncedInlineCommit();
    });
    content.addEventListener('blur', () => {
      el[key] = content.textContent;
      clear(content);
      renderText(content, el[key] || '');
      commit();
      renderInspector();   // field list may have changed
    });
    content.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); content.blur(); }
    });
  }
  let debouncedInlineCommit = () => {}; // reassigned in init() once debounce is available

  function renderCanvas() {
    uidMap.clear();
    clear(els.canvas);
    const root = document.createElement('div');
    root.className = 'lb-list';
    root.dataset.list = 'root';
    if (!model.layout.length) {
      const hint = document.createElement('div');
      hint.className = 'lb-empty-hint lb-empty-root';
      hint.textContent = 'Drag elements from the palette to start building your label';
      root.appendChild(hint);
    } else {
      for (const el of model.layout) root.appendChild(renderBlock(el, 'root'));
    }
    els.canvas.appendChild(root);
    applySelectionHighlight();
    initSortables();
    positionToolbar();
  }

  // ── Selection + floating quick-toolbar ───────────────────────────────────────
  function uidByEl(target) {
    for (const [uid, el] of uidMap) if (el === target) return uid;
    return null;
  }
  function nodeForSelected() {
    const uid = uidByEl(selectedEl);
    return uid ? els.canvas.querySelector('[data-uid="' + uid + '"]') : null;
  }
  function selectBlockByEl(el) {
    selectedEl = el;
    applySelectionHighlight();
    renderInspector();
    positionToolbar();
  }
  // Clear the selection, returning the inspector to the Template settings panel. Reachable three ways:
  // the inspector's "‹ Template settings" back link, a click on empty canvas background, and Escape.
  function deselect() {
    if (!selectedEl) return;
    selectedEl = null;
    applySelectionHighlight();
    renderInspector();
    positionToolbar();
  }
  function applySelectionHighlight() {
    els.canvas.querySelectorAll('.lb-block-selected').forEach((n) => n.classList.remove('lb-block-selected'));
    if (!selectedEl) return;
    const node = nodeForSelected();
    if (node) node.classList.add('lb-block-selected');
  }
  function positionToolbar() {
    const tb = els.toolbar;
    const el = selectedEl;
    const node = nodeForSelected();
    if (!el || !node) { tb.style.display = 'none'; return; }
    clear(tb);
    const sc = schemaOf(el.type);
    const alignAttr = sc && sc.attrs.find((x) => x.key === 'align');
    if (alignAttr) {
      for (const a of ALIGN) {
        // Route through setAttr so toggling back to the default (left) drops the key rather than
        // leaving redundant `align: left` noise in the emitted YAML.
        const b = toolbarBtn('', a, () => { setAttr(el, alignAttr, a); renderInspector(); positionToolbar(); });
        b.textContent = { left: '⇤', center: '≡', right: '⇥' }[a];
        b.classList.toggle('active', (el.align ?? alignAttr.default) === a);
        tb.appendChild(b);
      }
    }
    const boldAttr = sc && sc.attrs.find((x) => x.key === 'bold');
    if (boldAttr) {
      const def = boldAttr.default || false;
      const b = toolbarBtn('B', 'Bold', () => {
        setAttr(el, boldAttr, !(el.bold ?? def)); renderInspector(); positionToolbar();
      });
      b.style.fontWeight = '700';
      b.classList.toggle('active', !!(el.bold ?? def));
      tb.appendChild(b);
    }
    if (sc && sc.text) {
      const b = toolbarBtn('{ }', 'Insert field token', () => insertFieldToken(el, sc.text));
      tb.appendChild(b);
    }
    tb.style.display = 'flex';
    // Anchor above the selected block, within the canvas.
    const cRect = els.canvas.getBoundingClientRect();
    const nRect = node.getBoundingClientRect();
    tb.style.left = Math.max(4, nRect.left - cRect.left + els.canvas.scrollLeft) + 'px';
    tb.style.top = Math.max(0, nRect.top - cRect.top + els.canvas.scrollTop - tb.offsetHeight - 4) + 'px';
  }
  function toolbarBtn(txt, title, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lb-tb-btn';
    b.textContent = txt;
    b.title = title;
    b.addEventListener('mousedown', (e) => e.preventDefault()); // keep canvas focus/selection
    b.addEventListener('click', onClick);
    return b;
  }
  function insertFieldToken(el, key) {
    const fieldName = window.prompt('Field name (letters, digits, _):', 'field');
    if (!fieldName) return;
    if (!/^[A-Za-z0-9_]{1,64}$/.test(fieldName)) {
      if (typeof window.showStatus === 'function') window.showStatus('Invalid field name — use letters, digits, _.', 'err');
      return;
    }
    el[key] = (el[key] || '') + '{{' + fieldName + '}}';
    renderCanvas();
    commit();
    renderInspector();
  }

  // ── Block mutation ───────────────────────────────────────────────────────────
  function findParentList(target) {
    // Returns the array containing `target` (either model.layout or some container's children).
    if (model.layout.includes(target)) return model.layout;
    let found = null;
    const walk = (el) => {
      if (Array.isArray(el.children)) {
        if (el.children.includes(target)) { found = el.children; return; }
        el.children.forEach(walk);
      }
    };
    model.layout.forEach(walk);
    return found;
  }
  function duplicateBlock(el) {
    const list = findParentList(el);
    if (!list) return;
    const copy = JSON.parse(JSON.stringify(el));
    list.splice(list.indexOf(el) + 1, 0, copy);
    renderCanvas();
    commit();
  }
  function deleteBlock(el) {
    const list = findParentList(el);
    if (!list) return;
    list.splice(list.indexOf(el), 1);
    // Clear the selection if the selected element is no longer reachable in the model — this covers
    // both deleting the selected element itself AND deleting a container whose selected DESCENDANT
    // went with it (else the inspector shows a ghost whose edits silently do nothing).
    if (selectedEl && findParentList(selectedEl) === null) selectedEl = null;
    renderCanvas();
    commit();
    renderInspector();
  }

  // ── SortableJS wiring ────────────────────────────────────────────────────────
  function destroySortables() {
    for (const s of sortables) { try { s.destroy(); } catch (e) { /* already gone */ } }
    sortables = [];
  }
  function initSortables() {
    destroySortables();
    if (typeof window.Sortable === 'undefined') return;
    const lists = els.canvas.querySelectorAll('[data-list]');
    lists.forEach((listEl) => {
      const kind = listEl.dataset.list;
      const s = window.Sortable.create(listEl, {
        group: {
          name: 'lb',
          pull: true,
          put: (to, from, dragEl) => allowedInList(to.el.dataset.list, dragEl.dataset.type),
        },
        handle: '.lb-handle',
        draggable: '.lb-block',
        animation: 140,
        fallbackOnBody: true,
        swapThreshold: 0.65,
        ghostClass: 'lb-ghost',
        chosenClass: 'lb-chosen',
        dragClass: 'lb-drag',
        emptyInsertThreshold: 12,
        onEnd: onDndEnd,
      });
      sortables.push(s);
    });
    // Palette is a clone-source that never accepts drops and never reorders.
    if (els.palette && typeof window.Sortable !== 'undefined') {
      const ps = window.Sortable.create(els.palette, {
        group: { name: 'lb', pull: 'clone', put: false },
        sort: false,
        draggable: '.lb-chip',
        onEnd: onDndEnd,
      });
      sortables.push(ps);
    }
  }
  // After any drag settles, rebuild the model from the DOM order, then fully re-render (which mints
  // fresh nodes + Sortables). This keeps the model authoritative and avoids drifting DOM/model state.
  function onDndEnd() {
    // Defer past this callback: renderCanvas() calls initSortables(), which destroys every Sortable —
    // including the one whose onEnd is running right now. Tearing that down mid-callback throws in
    // some SortableJS builds, so let it finish unwinding its drag state first.
    setTimeout(() => {
      rebuildModelFromDom();
      renderCanvas();
      commit();
      renderInspector();
    }, 0);
  }
  function rebuildModelFromDom() {
    const readList = (listEl) => {
      const items = [];
      for (const node of listEl.children) {
        const type = node.dataset && node.dataset.type;
        if (!type) continue;                                          // skip empty-hints / stray nodes
        // An existing block carries a uid that still maps to its element object (uidMap is only
        // cleared on the next full render, which happens AFTER this rebuild). A node without a live
        // uid is a fresh clone dropped from the palette → make a default element of that type.
        let el = (node.classList.contains('lb-block') && node.dataset.uid)
          ? uidMap.get(node.dataset.uid) : null;
        if (!el) el = createDefaultElement(type);
        if (isContainer(type)) {
          const childListEl = node.querySelector('.lb-children');
          el.children = childListEl ? readList(childListEl) : (el.children || []);
        }
        items.push(el);
      }
      return items;
    };
    const rootEl = els.canvas.querySelector('[data-list="root"]');
    model.layout = rootEl ? readList(rootEl) : [];
  }

  // ── Inspector ────────────────────────────────────────────────────────────────
  function renderInspector() {
    const insp = els.inspector;
    clear(insp);
    const el = selectedEl;
    if (!el) { renderTemplateSettings(insp); return; }

    const sc = schemaOf(el.type);
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'lb-insp-back';
    back.textContent = '‹ Template settings';
    back.addEventListener('click', deselect);
    insp.appendChild(back);
    const title = document.createElement('div');
    title.className = 'lb-insp-title';
    title.textContent = (sc ? sc.label : el.type) + ' settings';
    insp.appendChild(title);

    const list = findParentList(el);
    const inRow = !!(list && model.layout !== list && parentTypeOf(el) === 'row');

    for (const attr of (sc ? sc.attrs : [])) {
      if (attr.when && !attr.when(el)) continue;
      insp.appendChild(buildControl(el, attr));
    }
    if (inRow) {
      insp.appendChild(groupHeader('Column hints (in row)'));
      for (const attr of ROW_CHILD_ATTRS) insp.appendChild(buildControl(el, attr));
    }
    const adv = document.createElement('details');
    adv.className = 'lb-insp-advanced';
    const sum = document.createElement('summary');
    sum.textContent = 'Advanced (padding, color)';
    adv.appendChild(sum);
    for (const attr of COMMON_ADVANCED) adv.appendChild(buildControl(el, attr));
    insp.appendChild(adv);
  }
  function parentTypeOf(target) {
    let t = null;
    const walk = (el) => {
      if (Array.isArray(el.children)) {
        if (el.children.includes(target)) { t = el.type; return; }
        el.children.forEach(walk);
      }
    };
    model.layout.forEach(walk);
    return t;
  }
  function groupHeader(text) {
    const h = document.createElement('div');
    h.className = 'lb-insp-group';
    h.textContent = text;
    return h;
  }
  // Client-side numeric validation that mirrors app/loader.py bounds, so the inspector rejects a value
  // the server would reject (with inline feedback) instead of letting it through to a post-submit
  // "Invalid template YAML" error. The text/list strip is AREA-guarded server-side: size × max_lines
  // (text) or size × max_items (list) must stay ≤ MAX_TEXT_STRIP_PRODUCT. The per-key defaults mirror
  // render/elements.py FONT_SIZES and the SCHEMA defaults so the product check matches when the sibling
  // attr is absent (omitted == default).
  const MAX_TEXT_STRIP_PRODUCT = 4000;
  const PRODUCT_CONSTRAINTS = {
    text: { keys: ['size', 'max_lines'], defaults: { size: 32, max_lines: 10 } },
    list: { keys: ['size', 'max_items'], defaults: { size: 32, max_items: 20 } },
  };
  // Message if setting `changingKey` to `proposed` would push the strip area over the cap, else null.
  function productError(el, changingKey, proposed) {
    const c = PRODUCT_CONSTRAINTS[el.type];
    if (!c || !c.keys.includes(changingKey)) return null;
    const val = (k) => (k === changingKey ? proposed
      : (typeof el[k] === 'number' && isFinite(el[k]) ? el[k] : c.defaults[k]));
    const a = val(c.keys[0]);
    const b = val(c.keys[1]);
    if (a * b <= MAX_TEXT_STRIP_PRODUCT) return null;
    const other = changingKey === c.keys[0] ? c.keys[1] : c.keys[0];
    return c.keys[0] + ' × ' + c.keys[1] + ' (' + a + ' × ' + b + ' = ' + (a * b) +
      ') exceeds ' + MAX_TEXT_STRIP_PRODUCT + '. Lower this or reduce ' + other + '.';
  }
  // Toggle a field's invalid state: red border + an inline message (empty msg clears it).
  function setFieldError(wrap, inp, msg) {
    let err = wrap.querySelector('.lb-field-error');
    if (msg) {
      inp.classList.add('lb-invalid');
      inp.setAttribute('aria-invalid', 'true');
      if (!err) {
        err = document.createElement('span');
        err.className = 'lb-field-error';
        err.setAttribute('role', 'alert');
        wrap.appendChild(err);
      }
      err.textContent = msg;
    } else {
      inp.classList.remove('lb-invalid');
      inp.removeAttribute('aria-invalid');
      if (err) err.remove();
    }
  }

  function buildControl(el, attr) {
    const wrap = document.createElement('label');
    wrap.className = 'lb-field';
    const lab = document.createElement('span');
    lab.className = 'lb-field-label';
    lab.textContent = attr.label;
    wrap.appendChild(lab);
    const cur = el[attr.key];

    if (attr.control === 'toggle') {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!(cur ?? attr.default);
      cb.addEventListener('change', () => { setAttr(el, attr, cb.checked); });
      wrap.classList.add('lb-field-inline');
      wrap.insertBefore(cb, lab);
      return wrap;
    }
    if (attr.control === 'select' || attr.control === 'align') {
      const sel = document.createElement('select');
      sel.className = 'input';
      for (const choice of attr.choices) {
        const opt = document.createElement('option');
        opt.value = choice;
        opt.textContent = choice === '' ? '(inherit)' : choice;
        sel.appendChild(opt);
      }
      sel.value = (cur ?? attr.default ?? attr.choices[0]);
      sel.addEventListener('change', () => { setAttr(el, attr, sel.value); renderInspector(); positionToolbar(); });
      wrap.appendChild(sel);
      return wrap;
    }
    const inp = document.createElement('input');
    inp.className = 'input';
    if (attr.control === 'number') {
      inp.type = 'number';
      if (attr.min !== undefined) inp.min = String(attr.min);
      if (attr.max !== undefined) inp.max = String(attr.max);
      // Prefill the effective default (e.g. text font size 32) when the attr is unset, so the field
      // shows the value actually in force rather than a blank — the model stays clean (an untouched
      // default is never written to YAML; setAttr still drops it if re-entered).
      inp.value = (cur ?? attr.default ?? '');
      if (attr.placeholder) inp.placeholder = attr.placeholder;
      inp.addEventListener('input', () => {
        const raw = inp.value.trim();
        // Cleared field → drop the attr (revert to default). But a reverted default can itself blow the
        // strip-area cap (e.g. clear max_lines while size=512: default 10 → 512×10=5120), so re-check the
        // product against the DEFAULT the deletion would restore; if it fails, keep the attr and flag the
        // field rather than emitting YAML the server would 422. This bypasses setAttr, so refresh here too.
        if (raw === '') {
          const pc = PRODUCT_CONSTRAINTS[el.type];
          const reverted = pc && pc.keys.includes(attr.key) ? pc.defaults[attr.key] : undefined;
          const cerr = reverted !== undefined ? productError(el, attr.key, reverted) : null;
          if (cerr) { setFieldError(wrap, inp, cerr); return; }
          setFieldError(wrap, inp, ''); deleteAttr(el, attr.key); commit(); refreshBlockPreview(el); return;
        }
        // type=number still admits 'e'/'+'/'-'/'.', so validate explicitly: whole non-negative integers
        // only, within [min,max], and within the server's strip-area cap. On any failure, flag the field
        // and DON'T commit — the model keeps its last valid value so the emitted YAML never goes invalid.
        if (!/^\d+$/.test(raw)) { setFieldError(wrap, inp, 'Enter a whole number.'); return; }
        const n = parseInt(raw, 10);
        if (attr.min !== undefined && n < attr.min) { setFieldError(wrap, inp, 'Minimum is ' + attr.min + '.'); return; }
        if (attr.max !== undefined && n > attr.max) { setFieldError(wrap, inp, 'Maximum is ' + attr.max + '.'); return; }
        const perr = productError(el, attr.key, n);
        if (perr) { setFieldError(wrap, inp, perr); return; }
        setFieldError(wrap, inp, '');
        setAttr(el, attr, n);
      });
    } else {
      inp.type = 'text';
      inp.value = (cur ?? '');
      if (attr.placeholder) inp.placeholder = attr.placeholder;
      inp.addEventListener('input', () => {
        setAttr(el, attr, inp.value);
        if (schemaOf(el.type) && schemaOf(el.type).text === attr.key) { renderCanvasTextOnly(el); }
      });
    }
    wrap.appendChild(inp);
    return wrap;
  }
  // Update just the text content of one block on the canvas (cheap live echo while typing in the
  // inspector's text field), without a full canvas re-render that would blur the input.
  function renderCanvasTextOnly(el) {
    const uid = uidByEl(el);
    if (!uid) return;
    const node = els.canvas.querySelector('[data-uid="' + uid + '"] .lb-content[contenteditable]');
    if (node && document.activeElement !== node) {
      clear(node);
      renderText(node, el[schemaOf(el.type).text] || '');
    }
  }
  // Re-apply the canvas font preview for one block after a size/bold/align change. The inspector and
  // toolbar commit the model but don't re-render the canvas, so without this the schematic block
  // wouldn't echo the new size/weight/align until the next full render. Cheap and safe to call for any
  // element: it no-ops when the block isn't on the canvas and applyTextPreviewStyle ignores non-text
  // types, so it lives inside setAttr() — the single choke point every attribute edit flows through
  // (inspector toggle/select/number, toolbar bold/align) — rather than at each scattered call site.
  function refreshBlockPreview(el) {
    const uid = uidByEl(el);
    if (!uid) return;
    const node = els.canvas.querySelector('[data-uid="' + uid + '"] > .lb-content');
    if (node) applyTextPreviewStyle(node, el);
  }
  // Apply an attribute, dropping it when it equals its schema default so the emitted YAML stays clean.
  function setAttr(el, attr, value) {
    const isDefault = attr.default !== undefined && value === attr.default && attr.key !== 'field';
    const isEmptyStr = value === '' && attr.control !== 'select';
    if ((isDefault || isEmptyStr) && attr.key !== 'text' && attr.key !== 'data' && attr.key !== 'name') {
      deleteAttr(el, attr.key);
    } else {
      el[attr.key] = value;
    }
    commit();
    refreshBlockPreview(el);
  }
  function deleteAttr(el, key) { delete el[key]; }

  function renderTemplateSettings(insp) {
    const title = document.createElement('div');
    title.className = 'lb-insp-title';
    title.textContent = 'Template settings';
    insp.appendChild(title);
    const hint = document.createElement('p');
    hint.className = 'lb-insp-hint';
    hint.textContent = 'Select an element on the canvas to edit it, or drag one from the palette.';
    insp.appendChild(hint);

    insp.appendChild(textSetting('Name', model.name, (v) => { model.name = v; commit(); }));
    insp.appendChild(textSetting('Description', model.description, (v) => { model.description = v; commit(); }));

    // Label size — a select over the printer's supported ids (embedded as LABELS by editor.html).
    // LABELS is a `const` (lexical) global on the page, so it is reachable by bare name from this
    // later-running classic script but is NOT a `window.` property — read it via a typeof guard.
    // eslint-disable-next-line no-undef
    const labels = (typeof LABELS !== 'undefined' && Array.isArray(LABELS)) ? LABELS : [];
    if (labels.length) {
      const wrap = document.createElement('label');
      wrap.className = 'lb-field';
      const lab = document.createElement('span');
      lab.className = 'lb-field-label';
      lab.textContent = 'Label size';
      wrap.appendChild(lab);
      const sel = document.createElement('select');
      sel.className = 'input';
      for (const l of labels) {
        const opt = document.createElement('option');
        opt.value = l.id;
        opt.textContent = l.id;
        sel.appendChild(opt);
      }
      sel.value = model.label;
      sel.addEventListener('change', () => { model.label = sel.value; commit(); });
      wrap.appendChild(sel);
      insp.appendChild(wrap);
    } else {
      insp.appendChild(textSetting('Label', model.label, (v) => { model.label = v; commit(); }));
    }

    insp.appendChild(selectSetting('Rotate', ['0', '90', '180', '270'], String(model.rotate || 0),
      (v) => { model.rotate = parseInt(v, 10) || 0; commit(); }));
    insp.appendChild(selectSetting('Vertical align', VALIGN, model.valign || 'top',
      (v) => { model.valign = v; commit(); }));

    // Fields: every {{token}} referenced by the layout, with a required/optional toggle each.
    const refs = referencedFields(model.layout);
    insp.appendChild(groupHeader('Fields'));
    if (!refs.length) {
      const none = document.createElement('p');
      none.className = 'lb-insp-hint';
      none.textContent = 'No fields yet. Insert a {{token}} in a text/data element to add one.';
      insp.appendChild(none);
    } else {
      for (const f of refs) {
        const row = document.createElement('label');
        row.className = 'lb-field lb-field-inline';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = fieldOptional.has(f);
        cb.addEventListener('change', () => {
          if (cb.checked) fieldOptional.add(f); else fieldOptional.delete(f);
          commit();
        });
        const lab = document.createElement('span');
        lab.className = 'lb-field-label';
        lab.append(document.createTextNode(f + ' '));
        const tag = document.createElement('span');
        tag.className = 'lb-optional-tag';
        tag.textContent = 'optional';
        lab.appendChild(tag);
        row.append(cb, lab);
        insp.appendChild(row);
      }
    }
  }
  function textSetting(label, value, onInput) {
    const wrap = document.createElement('label');
    wrap.className = 'lb-field';
    const lab = document.createElement('span');
    lab.className = 'lb-field-label';
    lab.textContent = label;
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'input';
    inp.value = value || '';
    inp.addEventListener('input', () => onInput(inp.value));
    wrap.append(lab, inp);
    return wrap;
  }
  function selectSetting(label, choices, value, onChange) {
    const wrap = document.createElement('label');
    wrap.className = 'lb-field';
    const lab = document.createElement('span');
    lab.className = 'lb-field-label';
    lab.textContent = label;
    const sel = document.createElement('select');
    sel.className = 'input';
    for (const c of choices) {
      const opt = document.createElement('option');
      opt.value = c;
      opt.textContent = c;
      sel.appendChild(opt);
    }
    sel.value = value;
    sel.addEventListener('change', () => onChange(sel.value));
    wrap.append(lab, sel);
    return wrap;
  }

  // ── Palette ──────────────────────────────────────────────────────────────────
  function renderPalette() {
    clear(els.palette);
    for (const type of PALETTE) {
      const sc = SCHEMA[type];
      const chip = document.createElement('div');
      chip.className = 'lb-chip';
      chip.dataset.type = type;
      const badge = document.createElement('span');
      badge.className = 'lb-badge';
      badge.textContent = sc.badge;
      const name = document.createElement('span');
      name.textContent = sc.label;
      chip.append(badge, name);
      // Click also adds the element (accessibility / non-drag fallback).
      chip.addEventListener('click', () => {
        model.layout.push(createDefaultElement(type));
        renderCanvas();
        commit();
      });
      els.palette.appendChild(chip);
    }
  }

  // ── Two-way sync ─────────────────────────────────────────────────────────────
  // Build the model from the current #yaml via the server (the only YAML parser available). On success
  // enter Visual mode; on failure stay in YAML mode and surface the reason.
  async function syncFromYaml() {
    const yaml = els.yaml.value;
    if (!yaml.trim()) { model.layout = []; return true; }
    try {
      const res = await fetch(window.api('/templates/parse-layout'), {
        method: 'POST', headers: window.authHeaders(), body: JSON.stringify({ yaml }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const detail = err.detail || err;
        const msg = (detail && detail.error) ? detail.error
          : (typeof window.previewErrorMessage === 'function' ? window.previewErrorMessage(res.status, detail) : 'Invalid YAML');
        if (typeof window.showStatus === 'function') window.showStatus('Can’t open in the visual builder: ' + msg, 'err');
        return false;
      }
      const data = await res.json();
      model.name = data.name || 'my-label';
      model.description = data.description || '';
      model.label = data.label || '62';
      model.rotate = data.rotate || 0;
      model.valign = data.valign || 'top';
      model.layout = Array.isArray(data.layout) ? data.layout : [];
      fieldOptional.clear();
      ((data.fields && data.fields.optional) || []).forEach((f) => fieldOptional.add(f));
      selectedEl = null;
      return true;
    } catch (e) {
      if (typeof window.showStatus === 'function') window.showStatus('Network error opening the builder: ' + e.message, 'err');
      return false;
    }
  }

  // Monotonic mode-switch token. enterVisual awaits a network parse, so a YAML click (or a second
  // enterVisual) that lands mid-flight must win — otherwise the stale fetch resolves and snaps the
  // page back to Visual. Each switch bumps the token; enterVisual bails if it's no longer current.
  let modeSeq = 0;
  // The mode the user/config wants to be in — distinct from `visualActive`, which only flips true
  // AFTER enterVisual's async parse resolves. An out-of-band #yaml change (loadByName/useLabel) must
  // re-sync the builder whenever visual is INTENDED, even while the first parse is still in flight.
  let intendedMode = 'yaml';
  async function enterVisual() {
    intendedMode = 'visual';
    const my = ++modeSeq;
    const ok = await syncFromYaml();
    if (my !== modeSeq) return;            // superseded by a later mode switch / reload while parsing
    // Parse failed (bad YAML, or a transient network/auth error on /templates/parse-layout): fall back
    // to the raw editor. Reset visualActive too — enterVisual can now be re-entered FROM an active
    // visual session (loadByName/useLabel), so a stale `true` would leave the code panel showing while
    // commit()'s `!visualActive` guard is defeated, letting a pending debounced commit clobber #yaml.
    if (!ok) { visualActive = false; setMode('yaml'); return; }
    visualActive = true;
    setMode('visual');
    renderPalette();
    renderCanvas();
    renderInspector();
  }
  function enterYaml() {
    intendedMode = 'yaml';
    modeSeq++;
    visualActive = false;
    setMode('yaml');
    // Model already emitted into #yaml on every edit; nothing else to sync.
  }
  function setMode(mode) {
    const visual = mode === 'visual';
    els.root.style.display = visual ? 'flex' : 'none';
    els.codePanel.style.display = visual ? 'none' : '';
    els.btnVisual.classList.toggle('active', visual);
    els.btnYaml.classList.toggle('active', !visual);
    if (visual) positionToolbar();
  }

  // ── Init ─────────────────────────────────────────────────────────────────────
  function buildDom() {
    const grid = document.querySelector('.grid2');
    const codePanel = grid.querySelector('.code-panel');
    const draftPreview = document.getElementById('draft-preview');

    const root = document.createElement('div');
    root.className = 'lb-root';
    root.id = 'lb-root';
    root.style.display = 'none';

    const palette = document.createElement('div');
    palette.className = 'lb-palette';
    const palTitle = document.createElement('div');
    palTitle.className = 'lb-section-title';
    palTitle.textContent = 'Elements';
    root.appendChild(palTitle);
    root.appendChild(palette);

    const canvasHead = document.createElement('div');
    canvasHead.className = 'lb-canvas-head';
    const canvasTitle = document.createElement('div');
    canvasTitle.className = 'lb-section-title';
    canvasTitle.textContent = 'Canvas';
    // Design ⇄ Preview toggle
    const dp = document.createElement('div');
    dp.className = 'lb-dp-toggle';
    const bDesign = segBtn('Design', () => { designMode = true; syncDpToggle(); renderCanvas(); });
    const bPreview = segBtn('Preview', () => { designMode = false; syncDpToggle(); renderCanvas(); });
    dp.append(bDesign, bPreview);
    canvasHead.append(canvasTitle, dp);
    root.appendChild(canvasHead);

    const canvasWrap = document.createElement('div');
    canvasWrap.className = 'lb-canvas-wrap';
    const canvas = document.createElement('div');
    canvas.className = 'lb-canvas';
    // Click on empty canvas background (not on a block) clears the selection → back to Template settings.
    // Block clicks stopPropagation (renderBlock), so a mousedown reaching here with no ancestor block is
    // genuinely a background click — guard on .lb-block so clicking into a block's text never deselects.
    canvas.addEventListener('mousedown', (e) => { if (!e.target.closest('.lb-block')) deselect(); });
    const toolbar = document.createElement('div');
    toolbar.className = 'lb-toolbar';
    toolbar.style.display = 'none';
    canvasWrap.append(toolbar, canvas);
    root.appendChild(canvasWrap);

    const inspTitle = document.createElement('div');
    inspTitle.className = 'lb-section-title';
    inspTitle.textContent = 'Inspector';
    root.appendChild(inspTitle);
    const inspector = document.createElement('div');
    inspector.className = 'lb-inspector';
    root.appendChild(inspector);

    grid.insertBefore(root, draftPreview);

    els.root = root;
    els.palette = palette;
    els.canvas = canvas;
    els.inspector = inspector;
    els.toolbar = toolbar;
    els.codePanel = codePanel;
    els.yaml = document.getElementById('yaml');
    els.bDesign = bDesign;
    els.bPreview = bPreview;
    syncDpToggle();
  }
  function segBtn(txt, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lb-seg';
    b.textContent = txt;
    b.addEventListener('click', onClick);
    return b;
  }
  function syncDpToggle() {
    if (els.bDesign) els.bDesign.classList.toggle('active', designMode);
    if (els.bPreview) els.bPreview.classList.toggle('active', !designMode);
  }
  function buildModeToggle() {
    const toolbar = document.querySelector('.studio-toolbar');
    const seg = document.createElement('div');
    seg.className = 'lb-mode-toggle';
    els.btnVisual = segBtn('Visual', () => { enterVisual(); });
    els.btnYaml = segBtn('YAML', () => { enterYaml(); });
    const startVisual = editorDefaultMode() === 'visual';
    els.btnVisual.classList.toggle('active', startVisual);
    els.btnYaml.classList.toggle('active', !startVisual);
    seg.append(els.btnVisual, els.btnYaml);
    toolbar.insertBefore(seg, toolbar.firstChild);
  }

  function init() {
    if (!document.querySelector('.grid2') || !document.getElementById('yaml')) return;
    // debounce comes from labelito.js; fall back to a trivial timer if it isn't present.
    const deb = (typeof window.debounce === 'function')
      ? window.debounce
      : (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
    debouncedPreview = deb(() => { if (typeof window.doPreview === 'function') window.doPreview(); }, 500);
    debouncedInlineCommit = deb(() => commit(), 400);

    buildModeToggle();
    buildDom();

    // Escape clears the selection (blurring an active inline edit first) → back to Template settings.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !visualActive || !selectedEl) return;
      if (document.activeElement && document.activeElement.isContentEditable) document.activeElement.blur();
      deselect();
    });

    // Rebuild the model when the YAML is replaced out-of-band while Visual mode is open (Load a
    // template, or "Use" a label id from the reference). Monkey-patch the page globals so no edit to
    // the inline editor.html script is needed.
    wrapGlobal('loadByName');
    wrapGlobal('useLabel');

    // Open in the configured default authoring surface (EDITOR_DEFAULT_MODE, default "visual"). The
    // raw editor is the DOM's resting state, so "yaml" needs nothing; "visual" auto-enters the builder
    // (enterVisual re-parses the seed and falls back to YAML on a parse error).
    if (editorDefaultMode() === 'visual') enterVisual();
  }
  // The Studio's default authoring surface, from the EDITOR_DEFAULT_MODE page global (a lexical const
  // set by editor.html, so it's reachable by bare name but not a window property — guard with typeof).
  function editorDefaultMode() {
    // eslint-disable-next-line no-undef
    return (typeof EDITOR_DEFAULT_MODE !== 'undefined' && EDITOR_DEFAULT_MODE === 'yaml') ? 'yaml' : 'visual';
  }
  function wrapGlobal(name) {
    const orig = window[name];
    if (typeof orig !== 'function') return;
    window[name] = async function (...args) {
      const before = els.yaml.value;
      const r = await orig.apply(this, args);
      // Re-enter Visual only when orig actually replaced #yaml (loaded a template / used a label) AND
      // visual is the intended surface — NOT merely when `visualActive` is already true, so a load that
      // lands while the initial auto-enter parse is still in flight isn't lost (enterVisual bumps
      // modeSeq so the stale seed parse bails and re-parses the new #yaml). The `before` guard skips a
      // cancelled/no-op load (unsaved-edits confirm declined), which would otherwise clear the
      // selection and drop in-progress empty containers via a pointless re-parse.
      if (intendedMode === 'visual' && els.yaml.value !== before) await enterVisual();
      return r;
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.LabelBuilder = {
    enterVisual, enterYaml, syncFromYaml, _model: model,
    // Side-effect-free test hook: emit YAML for an explicit layout + optional-field set without
    // touching builder state, so the emitter can be validated against the real loader in e2e.
    _emitForTest(layout, optional) {
      const savedLayout = model.layout;
      const savedOpt = new Set(fieldOptional);
      model.layout = layout || [];
      fieldOptional.clear();
      (optional || []).forEach((f) => fieldOptional.add(f));
      try { return emitYaml(); } finally {
        model.layout = savedLayout;
        fieldOptional.clear();
        savedOpt.forEach((f) => fieldOptional.add(f));
      }
    },
  };
})();
