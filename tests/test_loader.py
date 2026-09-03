# SPDX-License-Identifier: GPL-3.0-or-later
import textwrap
from pathlib import Path

import pytest

from app.loader import (
    TemplateLoadError,
    TemplateRegistry,
    load_template,
    validate_template_from_string,
)


def write_yaml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content))
    return path


def test_load_valid_template(sample_template_yaml: Path) -> None:
    t = load_template(sample_template_yaml)
    assert t.name == "test-simple"
    assert t.label == "62"
    assert t.rotate == 90
    assert "title" in t.required_fields
    assert "subtitle" in t.optional_fields
    assert len(t.layout) == 2


def test_load_minimal_template(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "min.yaml",
        """\
        name: min
        description: minimal
        label: "29"
        layout:
          - {type: text, text: hello}
    """,
    )
    t = load_template(path)
    assert t.rotate == 0
    assert t.required_fields == []


def test_missing_required_key_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "bad.yaml",
        """\
        name: bad
        description: no label
        layout:
          - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="label"):
        load_template(path)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("name: [unclosed")
    with pytest.raises(TemplateLoadError, match="YAML parse error"):
        load_template(path)


def test_empty_layout_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "empty.yaml",
        """\
        name: empty
        description: no elements
        label: "62"
        layout: []
    """,
    )
    with pytest.raises(TemplateLoadError, match="non-empty"):
        load_template(path)


def test_unknown_media_label_raises_at_load(tmp_path: Path) -> None:
    """A typo'd media size must fail at LOAD time, naming the bad id and the valid identifiers.

    The preview/print guards already reject an unknown ``label`` — but only when someone tries to
    USE the template, so a typo (``62x28`` for ``62x29``) previously loaded fine and surfaced as a
    request-time error on first print. The loader must reject it up front with the same
    brother_ql-registry message the media guard uses.
    """
    path = write_yaml(
        tmp_path / "typo.yaml",
        """\
        name: typo
        description: typo'd media size
        label: "62x28"
        layout:
          - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"typo\.yaml: Unknown brother_ql label '62x28'"):
        load_template(path)
    # The message must be actionable: it lists the valid identifiers to pick from.
    with pytest.raises(TemplateLoadError, match=r"Known:.*62x29"):
        load_template(path)


def test_unknown_media_label_raises_for_draft_too() -> None:
    """The draft studio shares the loader's schema validation, so a draft with a typo'd media size
    must be rejected by exactly the same load-time check (mapped to a 422 by the caller)."""
    draft = textwrap.dedent("""\
        name: draft-typo
        description: typo'd media size in a draft
        label: "sixtytwo"
        layout:
          - {type: text, text: x}
    """)
    with pytest.raises(TemplateLoadError, match="<draft>: Unknown brother_ql label 'sixtytwo'"):
        validate_template_from_string(draft)


@pytest.mark.parametrize("label", ["62", "62x29", "29x90", "17x54", "62red", "23x23"])
def test_known_media_labels_still_load(tmp_path: Path, label: str) -> None:
    """Every real brother_ql media id — continuous, die-cut, and two-color — keeps loading."""
    path = write_yaml(
        tmp_path / "known.yaml",
        f"""\
        name: known
        description: real media id
        label: "{label}"
        layout:
          - {{type: text, text: x}}
    """,
    )
    assert load_template(path).label == label


def test_unknown_element_type_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "unk.yaml",
        """\
        name: unk
        description: unknown type
        label: "62"
        layout:
          - {type: galaxy_brain, text: wat}
    """,
    )
    with pytest.raises(TemplateLoadError, match="galaxy_brain"):
        load_template(path)


def test_unknown_icon_collection_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "coll.yaml",
        """\
        name: coll
        description: bad icon collection
        label: "62"
        layout:
          - {type: icon, collection: bogus, name: coffee}
    """,
    )
    with pytest.raises(TemplateLoadError, match="collection"):
        load_template(path)


def test_unknown_fontawesome_style_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "style.yaml",
        """\
        name: style
        description: bad fontawesome style
        label: "62"
        layout:
          - {type: icon, collection: fontawesome, style: neon, name: coffee}
    """,
    )
    with pytest.raises(TemplateLoadError, match="style"):
        load_template(path)


def test_unsafe_icon_name_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "trav.yaml",
        """\
        name: trav
        description: traversal icon name
        label: "62"
        layout:
          - {type: icon, name: "../../etc/passwd"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="icon name"):
        load_template(path)


def test_valid_icon_collection_loads(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "ok.yaml",
        """\
        name: ok
        description: valid collection icon
        label: "62"
        layout:
          - {type: icon, collection: fontawesome, style: brands, name: github}
    """,
    )
    t = load_template(path)
    assert t.layout[0]["collection"] == "fontawesome"


def test_invalid_rotate_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rot.yaml",
        """\
        name: rot
        description: bad rotate
        label: "62"
        rotate: ninety
        layout:
          - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="rotate"):
        load_template(path)


def test_out_of_range_rotate_raises(tmp_path: Path) -> None:
    """A syntactically-valid but non-quarter-turn rotate (e.g. 99) is rejected up front.

    `rotate: 99` parses as a clean int, so the old `int()` coercion accepted it and the value
    reached PIL's `Image.rotate` — a tilt that mis-renders, or for a huge int an OverflowError at
    render (a 500 after a reported-clean load). The loader now restricts to {0, 90, 180, 270}.
    """
    for bad in (99, 360, 99999999999):
        path = write_yaml(
            tmp_path / "rot-range.yaml",
            f"""\
            name: rot-range
            description: out-of-range rotate
            label: "62"
            rotate: {bad}
            layout:
              - {{type: text, text: x}}
        """,
        )
        with pytest.raises(TemplateLoadError, match="rotate"):
            load_template(path)


def test_valid_rotations_load(tmp_path: Path) -> None:
    """Each of the four quarter-turns loads and round-trips to the parsed value."""
    for good in (0, 90, 180, 270):
        path = write_yaml(
            tmp_path / "rot-ok.yaml",
            f"""\
            name: rot-ok
            description: valid rotate
            label: "62"
            rotate: {good}
            layout:
              - {{type: text, text: x}}
        """,
        )
        assert load_template(path).rotate == good


def test_oversized_qr_size_raises(tmp_path: Path) -> None:
    """A `qr.size` of 10000 (above MAX_SQUARE_DIMENSION) is rejected — it renders as a sizexsize
    square (PIL resize((size, size))), so the allocation is quadratic and can OOM the worker."""
    path = write_yaml(
        tmp_path / "qr-big.yaml",
        """\
        name: qr-big
        description: oversized qr
        label: "62"
        layout:
          - {type: qr, data: x, size: 10000}
    """,
    )
    with pytest.raises(TemplateLoadError, match="size"):
        load_template(path)


def test_oversized_icon_size_raises(tmp_path: Path) -> None:
    """A `icon.size` of 10000 (above MAX_SQUARE_DIMENSION) is rejected for the same square reason."""
    path = write_yaml(
        tmp_path / "icon-big.yaml",
        """\
        name: icon-big
        description: oversized icon
        label: "62"
        layout:
          - {type: icon, name: snowflake, size: 10000}
    """,
    )
    with pytest.raises(TemplateLoadError, match="size"):
        load_template(path)


def test_oversized_text_font_size_raises(tmp_path: Path) -> None:
    """A `text.size` of 10000 (above MAX_FONT_SIZE) is rejected — a font point size that absurd
    drives a multi-thousand-px-tall strip."""
    path = write_yaml(
        tmp_path / "txt-big.yaml",
        """\
        name: txt-big
        description: oversized text font
        label: "62"
        layout:
          - {type: text, text: hi, size: 10000}
    """,
    )
    with pytest.raises(TemplateLoadError, match="size"):
        load_template(path)


def test_text_strip_product_cap_raises(tmp_path: Path) -> None:
    """`text` size x max_lines over MAX_TEXT_STRIP_PRODUCT is rejected even when each scalar is in
    bounds (size 500 ≤ 512, max_lines 100 ≤ 200, but 500x100 = 50000 ≫ 4000)."""
    path = write_yaml(
        tmp_path / "txt-strip.yaml",
        """\
        name: txt-strip
        description: text strip area too large
        label: "62"
        layout:
          - {type: text, text: hi, size: 500, max_lines: 100}
    """,
    )
    with pytest.raises(TemplateLoadError, match="max_lines"):
        load_template(path)


def test_padding_shorthand_and_longhand_load(tmp_path: Path) -> None:
    """Scalar and 1-4-value list `padding`, plus longhand sides, all load."""
    path = write_yaml(
        tmp_path / "pad-ok.yaml",
        """\
        name: pad-ok
        description: valid padding
        label: "62"
        layout:
          - {type: text, text: a, padding: 8}
          - {type: text, text: b, padding: [8, 12]}
          - {type: text, text: c, padding: [8, 12, 4, 16]}
          - {type: text, text: d, padding_left: 24, padding_top: 6}
    """,
    )
    assert len(load_template(path).layout) == 4


def test_padding_shorthand_too_many_values_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "pad-5.yaml",
        """\
        name: pad-5
        description: padding list too long
        label: "62"
        layout:
          - {type: text, text: a, padding: [1, 2, 3, 4, 5]}
    """,
    )
    with pytest.raises(TemplateLoadError, match="padding"):
        load_template(path)


def test_padding_shorthand_non_int_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "pad-str.yaml",
        """\
        name: pad-str
        description: padding not an int
        label: "62"
        layout:
          - {type: text, text: a, padding: huge}
    """,
    )
    with pytest.raises(TemplateLoadError, match="padding"):
        load_template(path)


def test_padding_on_row_and_column_children_loads(tmp_path: Path) -> None:
    """Padding is supported on row/column children (applied per-cell at render), so it loads there —
    longhand on a row child and the shorthand on a column child."""
    path = write_yaml(
        tmp_path / "pad-children.yaml",
        """\
        name: pad-children
        description: padding on container children
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: a, padding_left: 20}
              - type: column
                children:
                  - {type: text, text: b, padding: 8}
    """,
    )
    assert len(load_template(path).layout) == 1


def test_padding_left_out_of_range_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "pad-big.yaml",
        """\
        name: pad-big
        description: padding_left over the dimension cap
        label: "62"
        layout:
          - {type: text, text: a, padding_left: 99999}
    """,
    )
    with pytest.raises(TemplateLoadError, match="padding_left"):
        load_template(path)


def test_in_bounds_render_dimensions_load(tmp_path: Path) -> None:
    """Ordinary in-bounds qr/text/rotate values still load (the tightened caps reject nothing real:
    qr.size 600, text size 48 with max_lines 4, rotate 90)."""
    path = write_yaml(
        tmp_path / "ok-dims.yaml",
        """\
        name: ok-dims
        description: in-bounds dimensions
        label: "62"
        rotate: 90
        layout:
          - {type: qr, data: x, size: 600}
          - {type: text, text: hi, size: 48, max_lines: 4}
          - {type: icon, name: snowflake, size: 180}
    """,
    )
    t = load_template(path)
    assert t.rotate == 90
    assert len(t.layout) == 3


def test_text_strip_product_cap_applies_without_max_lines(tmp_path: Path) -> None:
    """A large `text.size` with NO `max_lines` is bounded by the product guard against the
    implicit DEFAULT_TEXT_MAX_LINES, not waved through. size 512 x 10 (default) = 5120 > 4000."""
    path = write_yaml(
        tmp_path / "txt-nolines.yaml",
        """\
        name: txt-nolines
        description: big font, no max_lines
        label: "62"
        layout:
          - {type: text, text: hi, size: 512}
    """,
    )
    with pytest.raises(TemplateLoadError, match="max_lines"):
        load_template(path)


def test_text_strip_product_cap_applies_with_null_max_lines(tmp_path: Path) -> None:
    """`max_lines: null` no longer hits a fast-path bypass — it is treated as the implicit
    default, so the same large font is still rejected by the product guard."""
    path = write_yaml(
        tmp_path / "txt-nulllines.yaml",
        """\
        name: txt-nulllines
        description: big font, null max_lines
        label: "62"
        layout:
          - {type: text, text: hi, size: 512, max_lines: null}
    """,
    )
    with pytest.raises(TemplateLoadError, match="max_lines"):
        load_template(path)


def test_text_without_max_lines_within_product_cap_loads(tmp_path: Path) -> None:
    """An ordinary uncapped body text (size 48, no max_lines) stays well under the product cap
    (48 x 10 default = 480 ≤ 4000) and loads — the shipped templates rely on this."""
    path = write_yaml(
        tmp_path / "txt-ok.yaml",
        """\
        name: txt-ok
        description: ordinary body text, no max_lines
        label: "62"
        layout:
          - {type: text, text: "a long body line of text", size: 48}
    """,
    )
    t = load_template(path)
    assert len(t.layout) == 1


# ── an explicit `key: null` is rejected, an absent key uses the default ──────────
def test_text_explicit_null_max_lines_rejected(tmp_path: Path) -> None:
    """`max_lines: null` is NOT the same as omitting it. An explicit null would be copied into
    the element as None, disabling the renderer's `if max_lines:` clamp → unbounded strip. The loader
    rejects it (the message names the key, so the strip-product test's `match` also stays valid)."""
    path = write_yaml(
        tmp_path / "txt-null.yaml",
        """\
        name: txt-null
        description: explicit null max_lines
        label: "62"
        layout:
          - {type: text, text: hi, max_lines: null}
    """,
    )
    with pytest.raises(TemplateLoadError, match="max_lines"):
        load_template(path)


def test_spacer_explicit_null_size_rejected(tmp_path: Path) -> None:
    """`size: null` on a spacer would reach the renderer as None and crash `self._px(None)`. An
    explicit null for a render-affecting numeric is rejected, unlike an absent key (which defaults)."""
    path = write_yaml(
        tmp_path / "spc-null.yaml",
        """\
        name: spc-null
        description: explicit null spacer size
        label: "62"
        layout:
          - {type: spacer, size: null}
    """,
    )
    with pytest.raises(TemplateLoadError, match="must not be null"):
        load_template(path)


def test_text_absent_max_lines_loads_with_default(tmp_path: Path) -> None:
    """Omitting `max_lines` entirely is fine — the dataclass default (DEFAULT_TEXT_MAX_LINES=10)
    applies. This is the case the shipped templates rely on; only the explicit null is rejected."""
    path = write_yaml(
        tmp_path / "txt-absent.yaml",
        """\
        name: txt-absent
        description: no max_lines key at all
        label: "62"
        layout:
          - {type: text, text: hello}
    """,
    )
    t = load_template(path)
    assert len(t.layout) == 1


def test_row_child_explicit_null_width_still_loads(tmp_path: Path) -> None:
    """`width: null` on a row child is the DOCUMENTED flexible-column sentinel (None ⇒ the column
    shares leftover space), so it must stay allowed even though null is rejected for other numerics."""
    path = write_yaml(
        tmp_path / "row-null-width.yaml",
        """\
        name: row-null-width
        description: explicit null width = flexible column
        label: "62"
        layout:
          - type: row
            children:
              - {type: title, text: hi, width: null}
              - {type: icon, name: snowflake, width: 80}
    """,
    )
    t = load_template(path)
    assert len(t.layout) == 1


def test_layout_element_count_cap_raises(tmp_path: Path) -> None:
    """A layout of 100 spacers exceeds MAX_LAYOUT_ELEMENTS (64) and is rejected before any
    render — hundreds of valid elements would otherwise compose into hundreds of MB."""
    spacers = "\n".join("          - {type: spacer, size: 10}" for _ in range(100))
    path = write_yaml(
        tmp_path / "many.yaml",
        f"""\
        name: many
        description: too many elements
        label: "62"
        layout:
{spacers}
    """,
    )
    with pytest.raises(TemplateLoadError, match="elements"):
        load_template(path)


def test_layout_total_height_budget_raises(tmp_path: Path) -> None:
    """A handful of valid {spacer, size: 10000} elements stays under the count cap but their
    summed declared height exceeds MAX_TOTAL_STRIP_HEIGHT (40000) and is rejected."""
    spacers = "\n".join("          - {type: spacer, size: 10000}" for _ in range(8))
    path = write_yaml(
        tmp_path / "tall.yaml",
        f"""\
        name: tall
        description: cumulative height too large
        label: "62"
        layout:
{spacers}
    """,
    )
    with pytest.raises(TemplateLoadError, match="height"):
        load_template(path)


def test_normal_layout_within_budget_loads(tmp_path: Path) -> None:
    """A normal bundled-size layout (a title, two body lines, a QR) is far under both budgets."""
    path = write_yaml(
        tmp_path / "normal.yaml",
        """\
        name: normal
        description: ordinary label
        label: "62"
        layout:
          - {type: title, text: hi, max_lines: 2}
          - {type: text, text: line one, size: 28}
          - {type: text, text: line two, size: 28}
          - {type: qr, data: x, size: 160}
    """,
    )
    t = load_template(path)
    assert len(t.layout) == 4


def test_row_height_uses_tallest_child_not_sum(tmp_path: Path) -> None:
    """A row's height contribution is the TALLEST child (side-by-side render), not the sum, so a
    row of several tall-but-individually-bounded children stays within the budget."""
    children = ", ".join("{type: spacer, size: 5000}" for _ in range(6))
    path = write_yaml(
        tmp_path / "row-tall.yaml",
        f"""\
        name: row-tall
        description: wide row of tall children
        label: "62"
        layout:
          - {{type: row, children: [{children}]}}
    """,
    )
    # 6 children x 5000 = 30000 if summed (under budget either way), but the row contributes only the
    # tallest child (~5000), so this comfortably loads — proving children are not summed.
    t = load_template(path)
    assert len(t.layout) == 1


def test_non_mapping_fields_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "fl.yaml",
        """\
        name: fl
        description: fields is a list
        label: "62"
        fields: []
        layout:
          - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="fields"):
        load_template(path)


def test_non_list_required_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rq.yaml",
        """\
        name: rq
        description: required is a scalar
        label: "62"
        fields:
          required: title
        layout:
          - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="lists"):
        load_template(path)


def test_undeclared_field_token_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "tok.yaml",
        """\
        name: tok
        description: token with no matching field
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: text, text: "{{counter}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="counter"):
        load_template(path)


def test_computed_tokens_need_no_declaration(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "dt.yaml",
        """\
        name: dt
        description: date/now resolve without a field
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: text, text: "{{date+6m}} {{now:%H:%M}}"}
    """,
    )
    t = load_template(path)
    assert t.name == "dt"


def test_registry_load_all(templates_dir: Path, sample_template_yaml: Path) -> None:
    reg = TemplateRegistry(templates_dir)
    names = reg.load_all()
    assert "test-simple" in names
    assert len(reg) == 1


def test_registry_get(registry: TemplateRegistry) -> None:
    t = registry.get("test-simple")
    assert t is not None
    assert t.name == "test-simple"


def test_registry_get_missing(registry: TemplateRegistry) -> None:
    assert registry.get("nonexistent") is None


def test_registry_all(registry: TemplateRegistry) -> None:
    all_templates = registry.all()
    assert any(t.name == "test-simple" for t in all_templates)


def test_registry_skips_invalid_file(templates_dir: Path, sample_template_yaml: Path) -> None:
    (templates_dir / "broken.yaml").write_text("name: [")
    reg = TemplateRegistry(templates_dir)
    names = reg.load_all()
    assert "test-simple" in names
    assert "broken" not in names
    assert len(names) == 1
    # The skipped file's error is retained so a caller (e.g. /reload) can report it.
    assert any("broken.yaml" in err for err in reg.errors)


def test_registry_skips_symlinked_template(
    tmp_path: Path, templates_dir: Path, sample_template_yaml: Path
) -> None:
    """A symlinked *.yaml — even one whose target is valid YAML — is never loaded.

    glob follows symlinks, so without this guard a link to a valid template OUTSIDE templates_dir
    would enter the registry and expose that external file to render/preview/source-load. It must be
    skipped-and-reported, and never returned by get().
    """
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        'name: sneaky\ndescription: external\nlabel: "62"\n'
        'fields:\n  required: [title]\nlayout:\n  - {type: title, text: "{{title}}"}\n'
    )
    link = templates_dir / "sneaky.yaml"
    link.symlink_to(outside)

    reg = TemplateRegistry(templates_dir)
    names = reg.load_all()
    assert "test-simple" in names
    assert "sneaky" not in names
    assert reg.get("sneaky") is None
    assert any("sneaky.yaml" in err and "symlink" in err for err in reg.errors)


def test_registry_errors_empty_on_clean_load(
    templates_dir: Path, sample_template_yaml: Path
) -> None:
    reg = TemplateRegistry(templates_dir)
    reg.load_all()
    assert reg.errors == []


def test_registry_rejects_duplicate_internal_name(templates_dir: Path) -> None:
    """Two files declaring the same internal `name` must not silently merge. The FIRST file in
    sort order keeps the name; the later duplicate is recorded as an error naming both files. This
    makes the registry deterministic regardless of which file sorts after the other."""
    # `aaa.yaml` sorts before `zzz.yaml`, so aaa wins and zzz is the rejected duplicate.
    write_yaml(
        templates_dir / "aaa.yaml",
        """\
        name: shared
        description: first by sort order
        label: "62"
        layout:
          - {type: text, text: first}
    """,
    )
    write_yaml(
        templates_dir / "zzz.yaml",
        """\
        name: shared
        description: later duplicate
        label: "62"
        layout:
          - {type: text, text: second}
    """,
    )
    reg = TemplateRegistry(templates_dir)
    names = reg.load_all()
    # Exactly one "shared" is registered, and it is the first file's (aaa.yaml).
    assert names.count("shared") == 1
    assert reg.get("shared") is not None
    assert reg.get("shared").source_path.name == "aaa.yaml"
    # The duplicate is reported, with both filenames and the shared name in the message.
    assert any("zzz.yaml" in err and "aaa.yaml" in err and "shared" in err for err in reg.errors)


# ── Bundled-example dir merge (templates_dir + example_dir) ───────────────────────
def _example_template(directory: Path, filename: str, name: str, text: str) -> Path:
    return write_yaml(
        directory / filename,
        f"""\
        name: {name}
        description: example
        label: "62"
        layout:
          - {{type: text, text: {text}}}
    """,
    )


def test_registry_loads_examples_when_user_dir_empty(tmp_path: Path, templates_dir: Path) -> None:
    """A bind-mounted (empty) user dir must not hide the bundled examples: with templates_dir empty,
    the examples still load. This is the core anti-shadowing guarantee."""
    examples = tmp_path / "examples"
    examples.mkdir()
    _example_template(examples, "pantry.yaml", "pantry", "shipped")

    reg = TemplateRegistry(templates_dir, examples)
    names = reg.load_all()
    assert names == ["pantry"]
    assert reg.get("pantry").source_path.parent == examples
    assert reg.errors == []


def test_registry_user_overrides_example_of_same_name(tmp_path: Path, templates_dir: Path) -> None:
    """A user template with the same internal `name` as a bundled example silently shadows it — the
    intended override, NOT a duplicate-name error (which is reserved for two *user* files)."""
    examples = tmp_path / "examples"
    examples.mkdir()
    _example_template(examples, "pantry.yaml", "pantry", "shipped")
    _example_template(templates_dir, "my-pantry.yaml", "pantry", "mine")

    reg = TemplateRegistry(templates_dir, examples)
    names = reg.load_all()
    assert names.count("pantry") == 1
    # The USER file wins, and no error is recorded for the shadowed example.
    assert reg.get("pantry").source_path.parent == templates_dir
    assert reg.errors == []


def test_registry_merges_distinct_user_and_examples(tmp_path: Path, templates_dir: Path) -> None:
    examples = tmp_path / "examples"
    examples.mkdir()
    _example_template(examples, "shipped.yaml", "shipped", "a")
    _example_template(templates_dir, "mine.yaml", "mine", "b")

    reg = TemplateRegistry(templates_dir, examples)
    assert sorted(reg.load_all()) == ["mine", "shipped"]
    assert reg.errors == []


def test_registry_example_dir_equal_to_user_loads_once(
    templates_dir: Path, sample_template_yaml: Path
) -> None:
    """When example_dir resolves to templates_dir (the bare-metal/dev default) the dir is scanned
    once — the same file must not register twice and raise a spurious duplicate-name error."""
    reg = TemplateRegistry(templates_dir, templates_dir)
    names = reg.load_all()
    assert names == ["test-simple"]
    assert reg.errors == []


def test_registry_malformed_example_does_not_pollute_errors(
    tmp_path: Path, templates_dir: Path, sample_template_yaml: Path
) -> None:
    """A malformed BUNDLED example is logged but never added to `errors` — shipped content must not
    gate a user's server-save (whose rollback keys off a non-empty errors list) or fail /reload."""
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "broken.yaml").write_text("name: [")

    reg = TemplateRegistry(templates_dir, examples)
    names = reg.load_all()
    assert "test-simple" in names  # the user template still loads
    assert reg.errors == []  # the bundled failure is not user-actionable


def test_registry_marks_example_provenance(tmp_path: Path, templates_dir: Path) -> None:
    """Templates loaded from the example dir carry is_example=True; the user's own carry False —
    the flag the web UI uses to mute example cards."""
    examples = tmp_path / "examples"
    examples.mkdir()
    _example_template(examples, "shipped.yaml", "shipped", "a")
    _example_template(templates_dir, "mine.yaml", "mine", "b")

    reg = TemplateRegistry(templates_dir, examples)
    reg.load_all()
    assert reg.get("mine").is_example is False
    assert reg.get("shipped").is_example is True


def test_registry_example_dir_none_loads_only_user(
    tmp_path: Path, templates_dir: Path, sample_template_yaml: Path
) -> None:
    """LOAD_EXAMPLES=false is wired as example_dir=None: the shipped examples exist on disk but are
    never scanned, so only the user's templates_dir loads."""
    examples = tmp_path / "examples"
    examples.mkdir()
    _example_template(examples, "pantry.yaml", "pantry", "shipped")

    reg = TemplateRegistry(templates_dir, None)
    names = reg.load_all()
    assert names == ["test-simple"]  # user only; the bundled 'pantry' is absent
    assert "pantry" not in names
    assert reg.errors == []


# ── Row container validation ─────────────────────────────────────────────────────
def test_valid_row_template_loads(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "row.yaml",
        """\
        name: row
        description: text left, glyph right
        label: "62"
        fields:
          required: [title]
        layout:
          - type: row
            children:
              - {type: title, text: "{{title}}", align: left}
              - {type: icon, name: check, collection: fontawesome, width: 80, align: right}
    """,
    )
    t = load_template(path)
    assert t.layout[0]["type"] == "row"
    assert len(t.layout[0]["children"]) == 2


def test_nested_row_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "nested.yaml",
        """\
        name: nested
        description: row inside a row
        label: "62"
        layout:
          - type: row
            children:
              - type: row
                children:
                  - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="cannot be nested here"):
        load_template(path)


def test_row_missing_children_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "nochild.yaml",
        """\
        name: nochild
        description: row with no children
        label: "62"
        layout:
          - {type: row}
    """,
    )
    with pytest.raises(TemplateLoadError, match="non-empty 'children'"):
        load_template(path)


def test_row_empty_children_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "emptychild.yaml",
        """\
        name: emptychild
        description: row with empty children list
        label: "62"
        layout:
          - {type: row, children: []}
    """,
    )
    with pytest.raises(TemplateLoadError, match="non-empty 'children'"):
        load_template(path)


def test_row_child_undeclared_token_raises(tmp_path: Path) -> None:
    """An undeclared {{token}} nested inside a row child must still be rejected."""
    path = write_yaml(
        tmp_path / "rowtok.yaml",
        """\
        name: rowtok
        description: undeclared token inside a row child
        label: "62"
        fields:
          required: [title]
        layout:
          - type: row
            children:
              - {type: title, text: "{{title}}"}
              - {type: text, text: "{{counter}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="counter"):
        load_template(path)


def test_row_child_bad_icon_raises(tmp_path: Path) -> None:
    """Icon validation must recurse into row children (labelled layout[i].children[j])."""
    path = write_yaml(
        tmp_path / "rowicon.yaml",
        """\
        name: rowicon
        description: bad icon collection inside a row child
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: hi}
              - {type: icon, collection: bogus, name: coffee}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"children\[1\] unknown icon collection"):
        load_template(path)


def test_row_child_quoted_width_raises(tmp_path: Path) -> None:
    """A quoted (string) width is a common YAML typo; it must be rejected, not 500 at render."""
    path = write_yaml(
        tmp_path / "rowwidth.yaml",
        """\
        name: rowwidth
        description: string width inside a row child
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: a}
              - {type: text, text: b, width: "80"}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"children\[1\] 'width' must be an integer"):
        load_template(path)


def test_row_child_nonpositive_width_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rowwidth0.yaml",
        """\
        name: rowwidth0
        description: zero width inside a row child
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: a}
              - {type: text, text: b, width: 0}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'width' must be >= 1"):
        load_template(path)


def test_row_child_string_weight_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rowweight.yaml",
        """\
        name: rowweight
        description: string weight inside a row child
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: a, weight: "2"}
              - {type: text, text: b}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'weight' must be an integer"):
        load_template(path)


def test_row_string_spacing_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rowspacing.yaml",
        """\
        name: rowspacing
        description: string spacing on a row
        label: "62"
        layout:
          - type: row
            spacing: "8"
            children:
              - {type: text, text: a}
              - {type: text, text: b}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'spacing' must be an integer"):
        load_template(path)


def test_row_invalid_align_items_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rowalign.yaml",
        """\
        name: rowalign
        description: bad align_items on a row
        label: "62"
        layout:
          - type: row
            align_items: middle
            children:
              - {type: text, text: a}
              - {type: text, text: b}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'align_items' must be one of"):
        load_template(path)


def test_row_child_invalid_valign_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rowvalign.yaml",
        """\
        name: rowvalign
        description: bad valign on a row child
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: a}
              - {type: text, text: b, valign: middle}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"children\[1\] 'valign' must be one of"):
        load_template(path)


def test_image_field_must_be_string(tmp_path: Path) -> None:
    """A non-string image 'field' (here a list) must be rejected, not crash the renderer."""
    path = write_yaml(
        tmp_path / "badfield.yaml",
        """\
        name: badfield
        description: image field is a list
        label: "62"
        layout:
          - type: row
            children:
              - {type: text, text: hi}
              - {type: image, field: [photo]}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"image 'field' must be a non-empty string"):
        load_template(path)


def test_image_field_empty_string_rejected(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "emptyfield.yaml",
        """\
        name: emptyfield
        description: image field is empty
        label: "62"
        layout:
          - {type: image, field: ""}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"image 'field' must be a non-empty string"):
        load_template(path)


def test_image_text_field_collision_raises(tmp_path: Path) -> None:
    """A field used by both an image element and a text template must be rejected at load.

    The image-field exemption from the text-size cap would otherwise let a large value render as
    text unguarded, defeating the render-time allocation cap.
    """
    path = write_yaml(
        tmp_path / "collide.yaml",
        """\
        name: collide
        description: same field feeds an image and a text token
        label: "62"
        fields:
          required: [photo]
        layout:
          - type: row
            children:
              - {type: text, text: "{{photo}}"}
              - {type: image, field: photo}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"both an image element and a text template"):
        load_template(path)


def test_distinct_image_and_text_fields_load(tmp_path: Path) -> None:
    """The collision check must not reject the normal case of distinct image and text fields."""
    path = write_yaml(
        tmp_path / "nocollide.yaml",
        """\
        name: nocollide
        description: distinct image and text fields
        label: "62"
        fields:
          required: [title, photo]
        layout:
          - type: row
            children:
              - {type: text, text: "{{title}}"}
              - {type: image, field: photo}
    """,
    )
    t = load_template(path)
    assert t.name == "nocollide"


def test_children_on_non_row_element_raises(tmp_path: Path) -> None:
    """Only a 'row' renders children; a 'children' list elsewhere must be rejected, not ignored.

    Otherwise the recursive image/token walkers would descend into a subtree the renderer ignores —
    e.g. marking a text field as an image and bypassing the text-size cap.
    """
    path = write_yaml(
        tmp_path / "strandchildren.yaml",
        """\
        name: strandchildren
        description: children on a non-row element
        label: "62"
        fields:
          required: [title]
        layout:
          - type: text
            text: "{{title}}"
            children:
              - {type: image, field: title}
    """,
    )
    with pytest.raises(
        TemplateLoadError, match=r"only a 'row' or 'column' element may have 'children'"
    ):
        load_template(path)


def test_row_valid_sizing_loads(tmp_path: Path) -> None:
    """Well-formed integer sizing and valid alignment values load without error."""
    path = write_yaml(
        tmp_path / "rowok.yaml",
        """\
        name: rowok
        description: valid row sizing controls
        label: "62"
        fields:
          required: [title]
        layout:
          - type: row
            align_items: top
            spacing: 12
            children:
              - {type: title, text: "{{title}}", weight: 3, valign: bottom}
              - {type: icon, name: snowflake, width: 90, valign: center}
    """,
    )
    t = load_template(path)
    assert t.layout[0]["children"][0]["weight"] == 3
    assert t.layout[0]["children"][1]["width"] == 90


def test_template_all_fields(sample_template: object) -> None:
    from app.loader import Template

    assert isinstance(sample_template, Template)
    all_f = sample_template.all_fields
    assert "title" in all_f
    assert "subtitle" in all_f


# ── Reserved / computed-token field name checks ─────────────────────────────────
def test_seq_as_required_field_raises(tmp_path: Path) -> None:
    """A template that declares 'seq' as a required user field must be rejected.

    The resolver substitutes {{seq}} from the computed sequence value before consulting request
    fields, so a declared 'seq' field would be silently ignored — the user's value never reaches
    the label.  The loader must fail loudly instead.
    """
    path = write_yaml(
        tmp_path / "seqfield.yaml",
        """\
        name: seqfield
        description: seq declared as user field
        label: "62"
        fields:
          required: [seq]
        layout:
          - {type: text, text: "{{seq}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"reserved for computed tokens"):
        load_template(path)


def test_seq_as_optional_field_raises(tmp_path: Path) -> None:
    """The reservation check must apply to optional fields too, not just required."""
    path = write_yaml(
        tmp_path / "seqopt.yaml",
        """\
        name: seqopt
        description: seq declared as optional user field
        label: "62"
        fields:
          optional: [seq]
        layout:
          - {type: text, text: "{{seq}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"reserved for computed tokens"):
        load_template(path)


def test_seq_token_without_field_declaration_loads(tmp_path: Path) -> None:
    """Using {{seq}} in a layout without declaring it as a field must load successfully.

    {{seq}} is a COMPUTED_TOKEN; it is resolved per-item by the engine and must never require a
    user-supplied field declaration.
    """
    path = write_yaml(
        tmp_path / "seqtoken.yaml",
        """\
        name: seqtoken
        description: uses {{seq}} as a computed token
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: text, text: "Item {{seq}}"}
    """,
    )
    t = load_template(path)
    assert t.name == "seqtoken"
    assert "seq" not in t.required_fields
    assert "seq" not in t.optional_fields


def test_date_as_field_raises(tmp_path: Path) -> None:
    """'date' is also a reserved computed token and must be rejected as a user field name."""
    path = write_yaml(
        tmp_path / "datefield.yaml",
        """\
        name: datefield
        description: date declared as user field
        label: "62"
        fields:
          required: [date]
        layout:
          - {type: text, text: "{{date}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"reserved for computed tokens"):
        load_template(path)


def test_now_as_field_raises(tmp_path: Path) -> None:
    """'now' is also a reserved computed token and must be rejected as a user field name."""
    path = write_yaml(
        tmp_path / "nowfield.yaml",
        """\
        name: nowfield
        description: now declared as user field
        label: "62"
        fields:
          required: [now]
        layout:
          - {type: text, text: "{{now}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"reserved for computed tokens"):
        load_template(path)


# ── field-name charset + render-affecting numeric bounds ────────────────
def test_shipped_templates_all_load() -> None:
    """Every template shipped under templates/ still validates with the current bounds in place.

    Guards against a chosen bound or the field-name charset accidentally rejecting a real template.
    """
    shipped = Path(__file__).resolve().parent.parent / "templates"
    yamls = sorted(shipped.glob("*.yaml"))
    assert yamls, "expected shipped templates to exist"
    for path in yamls:
        load_template(path)  # raises TemplateLoadError on regression


def test_html_field_name_rejected(tmp_path: Path) -> None:
    """A field name carrying HTML markup is rejected at load (defence in depth behind editor DOM)."""
    path = write_yaml(
        tmp_path / "xss.yaml",
        """\
        name: xss
        description: html field name
        label: "62"
        fields:
          required: ["<img src=x onerror=fetch(1)>"]
        layout:
          - {type: title, text: hi}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"invalid field name"):
        load_template(path)


def test_field_name_charset_matches_token_grammar(tmp_path: Path) -> None:
    """Field names must stay within the {{token}} grammar ([A-Za-z0-9_]).

    A name with a dot/dash/space would validate but the renderer's ``\\w+`` token regex would never
    substitute it, printing the literal ``{{lot-id}}`` on the label — a wrong-label failure. So such
    names are rejected at load time, keeping every declarable name substitutable.
    """
    for bad in ("line.1", "lot-id", "Box 1"):
        path = write_yaml(
            tmp_path / "bad.yaml",
            f"""\
            name: bad
            description: unsubstitutable name
            label: "62"
            fields:
              required: ["{bad}"]
            layout:
              - {{type: title, text: "{{{{title}}}}"}}
        """,
        )
        with pytest.raises(TemplateLoadError, match="invalid field name"):
            load_template(path)


def test_malformed_inline_placeholder_rejected(tmp_path: Path) -> None:
    """A {{...}} span the engine can't substitute is rejected, not printed literally.

    Even when the name is never DECLARED as a field, a hyphen/dot/space placeholder in layout text
    matches no token (engine._FIELD_RE), so the renderer would leave the literal ``{{asset-id}}`` on
    the label. The loader rejects it up front — the inline-text counterpart to the field-name charset.
    """
    for bad in ("{{asset-id}}", "{{line.1}}", "{{ title }}"):
        path = write_yaml(
            tmp_path / "bad.yaml",
            f"""\
            name: bad
            description: malformed placeholder
            label: "62"
            fields:
              required: [title]
            layout:
              - {{type: title, text: "hello {bad}"}}
        """,
        )
        with pytest.raises(TemplateLoadError, match="malformed placeholder"):
            load_template(path)


def test_malformed_placeholder_spanning_newline_rejected(tmp_path: Path) -> None:
    """A malformed {{...}} span crossing a YAML literal-block newline is still rejected.

    The loose span detector uses ``[\\s\\S]`` (not ``.``) so a placeholder broken across a newline —
    ``{{asset-\\nid}}`` — which the renderer also cannot substitute, does not slip past validation and
    print literally.
    """
    path = write_yaml(
        tmp_path / "nl.yaml",
        """\
        name: nl
        description: newline-spanning malformed placeholder
        label: "62"
        fields:
          required: [title]
        layout:
          - type: title
            text: |
              hello {{asset-
              id}}
    """,
    )
    with pytest.raises(TemplateLoadError, match="malformed placeholder"):
        load_template(path)


def test_field_name_charset_accepts_underscore_names(tmp_path: Path) -> None:
    """Letters, digits, and underscore — every name the shipped templates use — load and resolve."""
    path = write_yaml(
        tmp_path / "ok.yaml",
        """\
        name: ok
        description: substitutable names
        label: "62"
        fields:
          required: ["first_name", "asset_id", "box1"]
        layout:
          - {type: title, text: "{{first_name}} {{asset_id}} {{box1}}"}
    """,
    )
    t = load_template(path)
    assert t.required_fields == ["first_name", "asset_id", "box1"]


def test_negative_spacer_size_rejected(tmp_path: Path) -> None:
    """A negative spacer.size is a load error, not a render-time crash."""
    path = write_yaml(
        tmp_path / "neg.yaml",
        """\
        name: neg
        description: negative size
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: spacer, size: -5}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'size' must be >= 0"):
        load_template(path)


def test_enormous_dimension_rejected(tmp_path: Path) -> None:
    """A dimension above the per-element cap is rejected before any allocation."""
    path = write_yaml(
        tmp_path / "huge.yaml",
        """\
        name: huge
        description: enormous size
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: qr, data: x, size: 99999999999}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'size' must be <="):
        load_template(path)


def test_non_int_dimension_rejected(tmp_path: Path) -> None:
    """A string where an integer dimension is expected is a clear type error at load."""
    path = write_yaml(
        tmp_path / "strsize.yaml",
        """\
        name: strsize
        description: string size
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}"}
          - {type: text, text: hi, size: "32"}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"'size' must be an integer"):
        load_template(path)


# ── Column container ─────────────────────────────────────────────────────────────
def test_valid_column_in_row_loads(tmp_path: Path) -> None:
    """A row holding a column of leaf elements is the intended single-level grid."""
    path = write_yaml(
        tmp_path / "col.yaml",
        """\
        name: col
        description: column inside a row
        label: "62"
        fields:
          required: [title]
          optional: [subtitle, qr]
        layout:
          - type: row
            children:
              - type: column
                children:
                  - {type: title, text: "{{title}}"}
                  - {type: subtitle, text: "{{subtitle}}"}
              - {type: qr, data: "{{qr}}", width: 140}
    """,
    )
    t = load_template(path)
    assert t.layout[0]["type"] == "row"
    assert t.layout[0]["children"][0]["type"] == "column"


def test_top_level_column_loads(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "topcol.yaml",
        """\
        name: topcol
        description: top-level column
        label: "62"
        fields:
          required: [title]
        layout:
          - type: column
            children:
              - {type: title, text: "{{title}}"}
    """,
    )
    assert load_template(path).layout[0]["type"] == "column"


def test_column_in_column_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "colcol.yaml",
        """\
        name: colcol
        description: column inside a column
        label: "62"
        layout:
          - type: column
            children:
              - type: column
                children:
                  - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="cannot be nested here"):
        load_template(path)


def test_row_in_column_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "rowcol.yaml",
        """\
        name: rowcol
        description: row inside a column
        label: "62"
        layout:
          - type: column
            children:
              - type: row
                children:
                  - {type: text, text: x}
    """,
    )
    with pytest.raises(TemplateLoadError, match="cannot be nested here"):
        load_template(path)


def test_column_empty_children_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "emptycol.yaml",
        """\
        name: emptycol
        description: column with no children
        label: "62"
        layout:
          - {type: column, children: []}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'column' requires a non-empty 'children'"):
        load_template(path)


def test_column_bad_spacing_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "colspace.yaml",
        """\
        name: colspace
        description: column with negative spacing
        label: "62"
        fields:
          required: [title]
        layout:
          - type: column
            spacing: -1
            children:
              - {type: title, text: "{{title}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'spacing'"):
        load_template(path)


def test_column_height_budget_uses_sum(tmp_path: Path) -> None:
    """A column of many tall children sums their heights, so it can exceed the budget where a row
    (which takes the tallest) would not."""
    tall = ", ".join(["{type: spacer, size: 9000}"] * 6)  # 6 x 9000 summed = 54000 > 40000
    path = write_yaml(
        tmp_path / "tallcol.yaml",
        f"""\
        name: tallcol
        description: tall column
        label: "62"
        layout:
          - {{type: column, children: [{tall}]}}
    """,
    )
    with pytest.raises(TemplateLoadError, match="combined declared height"):
        load_template(path)


# ── List element ─────────────────────────────────────────────────────────────────
def test_valid_list_loads(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "list.yaml",
        """\
        name: listtmpl
        description: bulleted list
        label: "62"
        fields:
          required: [items]
        layout:
          - {type: list, text: "{{items}}", marker: bullet, size: 26, max_items: 5}
    """,
    )
    assert load_template(path).layout[0]["type"] == "list"


def test_list_bad_marker_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "badmarker.yaml",
        """\
        name: badmarker
        description: bad list marker
        label: "62"
        fields:
          required: [items]
        layout:
          - {type: list, text: "{{items}}", marker: stars}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'marker'"):
        load_template(path)


def test_list_bad_separator_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "badsep.yaml",
        """\
        name: badsep
        description: empty list separator
        label: "62"
        fields:
          required: [items]
        layout:
          - {type: list, text: "{{items}}", separator: ""}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'separator'"):
        load_template(path)


def test_list_size_times_max_items_bounded(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "biglist.yaml",
        """\
        name: biglist
        description: list strip too large
        label: "62"
        fields:
          required: [items]
        layout:
          - {type: list, text: "{{items}}", size: 200, max_items: 100}
    """,
    )
    with pytest.raises(TemplateLoadError, match=r"list 'size' x 'max_items'"):
        load_template(path)


# ── Badge / boxed text & filled box ──────────────────────────────────────────────
def test_text_background_and_border_load(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "badge.yaml",
        """\
        name: badge
        description: badge and boxed text
        label: "62"
        fields:
          required: [handling, ref]
        layout:
          - {type: text, text: "{{handling}}", background: black}
          - {type: text, text: "{{ref}}", border: 3, border_color: red}
    """,
    )
    assert load_template(path).layout[0]["background"] == "black"


def test_text_bad_background_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "badbg.yaml",
        """\
        name: badbg
        description: bad background
        label: "62"
        fields:
          required: [title]
        layout:
          - {type: title, text: "{{title}}", background: blue}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'background'"):
        load_template(path)


def test_box_fill_loads(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "fillbox.yaml",
        """\
        name: fillbox
        description: filled box bar
        label: "62"
        layout:
          - {type: box, height: 20, fill: true, color: red}
    """,
    )
    assert load_template(path).layout[0]["fill"] is True


# ── Row vertical divider ─────────────────────────────────────────────────────────
def test_row_divider_loads(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "divider.yaml",
        """\
        name: divtmpl
        description: row with divider
        label: "62"
        fields:
          required: [a, b]
        layout:
          - type: row
            divider: true
            divider_thickness: 3
            divider_color: red
            children:
              - {type: text, text: "{{a}}"}
              - {type: text, text: "{{b}}"}
    """,
    )
    assert load_template(path).layout[0]["divider"] is True


def test_row_bad_divider_color_raises(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "baddiv.yaml",
        """\
        name: baddiv
        description: bad divider color
        label: "62"
        fields:
          required: [a, b]
        layout:
          - type: row
            divider: true
            divider_color: green
            children:
              - {type: text, text: "{{a}}"}
              - {type: text, text: "{{b}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'divider_color'"):
        load_template(path)


def test_row_quoted_false_divider_raises(tmp_path: Path) -> None:
    """`divider: "false"` (a quoted string) is truthy at render — reject it as a non-bool."""
    path = write_yaml(
        tmp_path / "quoteddiv.yaml",
        """\
        name: quoteddiv
        description: quoted-false divider
        label: "62"
        fields:
          required: [a, b]
        layout:
          - type: row
            divider: "false"
            children:
              - {type: text, text: "{{a}}"}
              - {type: text, text: "{{b}}"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'divider' must be a boolean"):
        load_template(path)


def test_box_quoted_false_fill_raises(tmp_path: Path) -> None:
    """`fill: "false"` on a box is a truthy string — reject it so the bar isn't silently filled."""
    path = write_yaml(
        tmp_path / "quotedfill.yaml",
        """\
        name: quotedfill
        description: quoted-false box fill
        label: "62"
        layout:
          - {type: box, height: 20, fill: "false"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'fill' must be a boolean"):
        load_template(path)


def test_list_quoted_false_bold_raises(tmp_path: Path) -> None:
    """`bold: "false"` on a list is a truthy string — reject it so the text isn't silently bold."""
    path = write_yaml(
        tmp_path / "quotedbold.yaml",
        """\
        name: quotedbold
        description: quoted-false list bold
        label: "62"
        fields:
          required: [items]
        layout:
          - {type: list, text: "{{items}}", bold: "false"}
    """,
    )
    with pytest.raises(TemplateLoadError, match="'bold' must be a boolean"):
        load_template(path)


# --- aliases (alternative spoken names) ------------------------------------------------------
#
# An alias widens what a SPEECH matcher accepts for a template; it is never a lookup key. These
# lock down the two halves that matter: what a valid alias is allowed to look like (spaces and
# accents, which a template `name` may not carry), and which mistakes are rejected loudly instead
# of being dropped where the author would never notice.

ALIASED_TEMPLATE = """\
    name: meal-prep
    description: batch cooking
    label: "62"
    aliases: {aliases}
    layout:
      - {{type: text, text: hello}}
"""


def _load_with_aliases(tmp_path: Path, aliases: str) -> "object":
    path = write_yaml(tmp_path / "aliased.yaml", ALIASED_TEMPLATE.format(aliases=aliases))
    return load_template(path)


def test_aliases_default_to_empty(tmp_path: Path) -> None:
    """A template without `aliases` exposes [], not None — consumers iterate it unconditionally."""
    path = write_yaml(
        tmp_path / "plain.yaml",
        """\
        name: plain
        description: no aliases
        label: "62"
        layout:
          - {type: text, text: hello}
    """,
    )
    assert load_template(path).aliases == []


def test_aliases_keep_declared_order_and_casing(tmp_path: Path) -> None:
    """Order and casing survive: the value is shown to humans, and the matcher folds case itself."""
    t = _load_with_aliases(tmp_path, '["Comida Preparada", "batch cooking"]')
    assert t.aliases == ["Comida Preparada", "batch cooking"]


@pytest.mark.parametrize(
    "alias",
    [
        "comida preparada",  # the point of aliases: a spoken phrase, with a space
        "lasaña",  # accents — a name charset would reject this
        "café con leche",
        "prep 2",  # digits
        "l'étiquette",  # apostrophe
        "meal.prep",
        "meal_prep",
        "meal-prep-2",
        "a",  # single character is a legal (if unwise) spoken name
    ],
)
def test_valid_alias_shapes(tmp_path: Path, alias: str) -> None:
    assert _load_with_aliases(tmp_path, f'["{alias}"]').aliases == [alias]


@pytest.mark.parametrize(
    ("alias", "description"),
    [
        ("\u0928\u092e\u0938\u094d\u0924\u0947", "devanagari: letters plus two Mn marks"),
        ("\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35", "thai with vowel marks"),
        ("\u05e9\u05b8\u05dc\u05d5\u05b9\u05dd", "hebrew with niqqud"),
    ],
)
def test_a_script_that_needs_combining_marks_is_accepted(
    tmp_path: Path, alias: str, description: str
) -> None:
    """The charset promised "any script" and delivered only scripts with no combining marks.

    ``नमस्ते`` is letters plus two ``Mn`` marks that have NO precomposed form, so normalization
    cannot remove them — the whole writing system was unusable, and so were Thai tone marks and
    Hebrew niqqud, while the documentation said otherwise.
    """
    assert _load_with_aliases(tmp_path, f'["{alias}"]').aliases == [alias], description


def test_a_decomposed_accent_is_accepted_and_stored_precomposed(tmp_path: Path) -> None:
    """The likeliest case, and the one with a silent consequence.

    ``cafe\u0301`` (e + combining acute) was rejected outright, even though "café" is the doc's own
    example — and macOS has historically handed out decomposed text, so an author pastes it without
    knowing. Accepting it is only half: the two forms are DIFFERENT strings, unequal even after
    lower-casing, so an alias stored decomposed would validate and then never match anything a
    voice assistant folded the usual way. Storing NFC is what makes it actually work.
    """
    decomposed = "cafe\u0301"
    precomposed = "caf\u00e9"
    assert decomposed != precomposed
    assert decomposed.lower() != precomposed.lower()

    assert _load_with_aliases(tmp_path, f'["{decomposed}"]').aliases == [precomposed]


def test_two_spellings_of_one_accent_are_the_same_alias(tmp_path: Path) -> None:
    """Because both are stored NFC, they collide — which is the point, not a surprise."""
    with pytest.raises(TemplateLoadError, match="duplicate alias"):
        _load_with_aliases(tmp_path, '["cafe\u0301", "caf\u00e9"]')


def test_a_leading_combining_mark_is_still_rejected(tmp_path: Path) -> None:
    """Marks are allowed everywhere but first: a word cannot begin with one."""
    with pytest.raises(TemplateLoadError, match="invalid alias"):
        _load_with_aliases(tmp_path, '["\u0301cafe"]')


def test_alias_whitespace_is_collapsed(tmp_path: Path) -> None:
    """Runs of whitespace collapse, so two aliases cannot differ invisibly by spacing."""
    t = _load_with_aliases(tmp_path, '["  comida   preparada  "]')
    assert t.aliases == ["comida preparada"]


@pytest.mark.parametrize(
    "alias",
    [
        "",  # empty
        "  ",  # whitespace only
        "-prep",  # leading punctuation: a stray list dash or a typo
        ".prep",
        "_prep",  # underscore start is an identifier habit, not speech
        "'prep",
        "meal (prep)",  # every one of these is a sentence-grammar metacharacter downstream
        "meal [prep]",
        "meal {prep}",
        "meal <prep>",
        "meal|prep",
        "meal;prep",
        "meal\\\\prep",
        "meal/prep",  # a path separator has no business in a spoken name
        "meal@prep",
        "x" * 65,  # one past the 64-char cap
    ],
)
def test_invalid_alias_is_rejected(tmp_path: Path, alias: str) -> None:
    with pytest.raises(TemplateLoadError, match="invalid alias"):
        _load_with_aliases(tmp_path, f'["{alias}"]')


@pytest.mark.parametrize(
    ("literal", "loaded_as"),
    [
        ("yes", "True"),
        ("no", "False"),
        ("on", "True"),
        ("off", "False"),
        ("true", "True"),
        ("null", "None"),
        ("~", "None"),
        ("12", "12"),
        ("1.5", "1.5"),
    ],
)
def test_a_yaml_keyword_alias_is_rejected_not_coerced(
    tmp_path: Path, literal: str, loaded_as: str
) -> None:
    """PyYAML implements YAML 1.1, where ordinary spoken words are keywords.

    ``aliases: [no]`` parses to ``False`` before any validation runs, so coercing with ``str()``
    stored the alias {loaded_as!r} — silent corruption of exactly the short, common words an alias
    is most likely to be. The author has to be told to quote it, because a wrong alias is
    invisible: it simply never matches anything anyone says.
    """
    with pytest.raises(TemplateLoadError, match="not a string"):
        _load_with_aliases(tmp_path, f"[{literal}]")


@pytest.mark.parametrize("quoted", ['["no"]', "['off']", '["12"]'])
def test_a_quoted_keyword_alias_is_accepted_verbatim(tmp_path: Path, quoted: str) -> None:
    """The fix the error message tells the author about has to actually work."""
    alias = quoted.strip("[]\"'")
    assert _load_with_aliases(tmp_path, quoted).aliases == [alias]


def test_alias_list_must_be_a_list(tmp_path: Path) -> None:
    """A bare string is the likely mistake: iterating it would declare one alias per character."""
    with pytest.raises(TemplateLoadError, match="'aliases' must be a list"):
        _load_with_aliases(tmp_path, "comida preparada")


def test_too_many_aliases_is_rejected(tmp_path: Path) -> None:
    from app.loader import MAX_TEMPLATE_ALIASES

    too_many = "[" + ", ".join(f'"alias {i}"' for i in range(MAX_TEMPLATE_ALIASES + 1)) + "]"
    with pytest.raises(TemplateLoadError, match="too many aliases"):
        _load_with_aliases(tmp_path, too_many)

    at_cap = "[" + ", ".join(f'"alias {i}"' for i in range(MAX_TEMPLATE_ALIASES)) + "]"
    assert len(_load_with_aliases(tmp_path, at_cap).aliases) == MAX_TEMPLATE_ALIASES


@pytest.mark.parametrize("second", ["comida preparada", "Comida Preparada", "comida   preparada"])
def test_duplicate_alias_is_rejected(tmp_path: Path, second: str) -> None:
    """Case and spacing are folded before comparing, so a 'different' duplicate is still one."""
    with pytest.raises(TemplateLoadError, match="duplicate alias"):
        _load_with_aliases(tmp_path, f'["comida preparada", "{second}"]')


@pytest.mark.parametrize("alias", ["meal-prep", "MEAL-PREP"])
def test_alias_equal_to_the_template_name_is_rejected(tmp_path: Path, alias: str) -> None:
    """The name always matches on its own, so aliasing it is a no-op the author should see."""
    with pytest.raises(TemplateLoadError, match="own name"):
        _load_with_aliases(tmp_path, f'["{alias}"]')


def test_alias_may_be_the_despaced_name(tmp_path: Path) -> None:
    """`meal prep` for `meal-prep` is redundant but NOT rejected.

    A consumer can derive the de-hyphenated form itself, so this alias adds nothing — but the
    author is being explicit, not making a mistake, and the consumer de-duplicates spoken forms
    anyway. Only an exact (case-folded) match with the name is refused.
    """
    assert _load_with_aliases(tmp_path, '["meal prep"]').aliases == ["meal prep"]


def test_aliases_are_validated_for_drafts_too(tmp_path: Path) -> None:
    """The draft path shares build_template_from_mapping, so an MCP/studio draft is gated equally."""
    with pytest.raises(TemplateLoadError, match="invalid alias"):
        validate_template_from_string(
            textwrap.dedent(ALIASED_TEMPLATE.format(aliases='["meal (prep)"]'))
        )


def test_aliases_are_not_lookup_keys(tmp_path: Path) -> None:
    """The registry indexes by `name` only: an alias must not resolve a template.

    This is the invariant that keeps aliases a voice concern. If the registry ever indexed them,
    every API path (print, preview, source) would silently gain a second namespace whose collisions
    nothing validates — precisely what _validate_aliases declines to check for.
    """
    write_yaml(tmp_path / "aliased.yaml", ALIASED_TEMPLATE.format(aliases='["comida preparada"]'))
    registry = TemplateRegistry(tmp_path)
    registry.load_all()
    assert registry.get("meal-prep") is not None
    assert registry.get("comida preparada") is None


# --- contested spoken forms across the catalog ------------------------------------------------
#
# Reported, never rejected. The distinction is the whole design: see spoken_form_collisions.

COLLIDING_TEMPLATE = """\
    name: {name}
    description: x
    label: "62"
    aliases: {aliases}
    layout:
      - {{type: text, text: hello}}
"""


def _registry_with(tmp_path: Path, *specs: tuple[str, str]) -> TemplateRegistry:
    for name, aliases in specs:
        write_yaml(tmp_path / f"{name}.yaml", COLLIDING_TEMPLATE.format(name=name, aliases=aliases))
    registry = TemplateRegistry(tmp_path)
    registry.load_all()
    return registry


def test_two_templates_claiming_one_alias_is_warned(tmp_path: Path) -> None:
    registry = _registry_with(tmp_path, ("nevera", '["frio"]'), ("congelador", '["frio"]'))

    assert registry.errors == []
    assert len(registry.warnings) == 1
    assert "'frio'" in registry.warnings[0]
    assert "nevera" in registry.warnings[0] and "congelador" in registry.warnings[0]


def test_an_alias_claiming_another_templates_name_is_warned(tmp_path: Path) -> None:
    """The worse half of the collision: the other template's NAME wins, so the alias is dead."""
    registry = _registry_with(tmp_path, ("nevera", "[]"), ("congelador", '["nevera"]'))

    assert registry.errors == []
    assert len(registry.warnings) == 1
    assert "'nevera'" in registry.warnings[0]


def test_separator_and_case_differences_still_collide(tmp_path: Path) -> None:
    """The warning has to fold the way a speech matcher does, or it misses the common case."""
    registry = _registry_with(tmp_path, ("meal-prep", "[]"), ("batch", '["Meal Prep"]'))

    assert len(registry.warnings) == 1
    assert "meal prep" in registry.warnings[0]


def test_a_collision_never_becomes_an_error(tmp_path: Path) -> None:
    """The property that makes this a warning and not a rejection.

    ``errors`` gates catalog-wide — the save route rolls back on ANY error, not just one about the
    file being saved — so a colliding alias becoming an error would let one bad pair of voice hints
    make every later save fail. Both templates here still load, list and print.
    """
    registry = _registry_with(tmp_path, ("nevera", '["frio"]'), ("congelador", '["frio"]'))

    assert registry.errors == []
    assert sorted(t.name for t in registry.all()) == ["congelador", "nevera"]
    assert registry.get("nevera") is not None
    assert registry.get("congelador") is not None


def test_a_clean_catalog_warns_about_nothing(tmp_path: Path) -> None:
    registry = _registry_with(tmp_path, ("nevera", '["frio"]'), ("congelador", '["congelado"]'))
    assert registry.warnings == []


def test_one_template_claiming_a_form_twice_is_not_a_collision(tmp_path: Path) -> None:
    """`meal prep` as an alias of `meal-prep` folds onto its own name — redundant, not contested."""
    registry = _registry_with(tmp_path, ("meal-prep", '["meal prep"]'))
    assert registry.warnings == []


def test_a_user_alias_colliding_with_a_bundled_example_is_warned(tmp_path: Path) -> None:
    """The user has to hear it even though the other side is not theirs: their alias is the dead one.

    The message says which side is shipped, because renaming their own alias is the only fix
    available to them.
    """
    example_dir = tmp_path / "examples"
    example_dir.mkdir()
    write_yaml(
        example_dir / "shipped.yaml", COLLIDING_TEMPLATE.format(name="shipped", aliases="[]")
    )
    write_yaml(
        tmp_path / "mine.yaml", COLLIDING_TEMPLATE.format(name="mine", aliases='["shipped"]')
    )
    registry = TemplateRegistry(tmp_path, example_dir)
    registry.load_all()

    assert registry.errors == []
    assert len(registry.warnings) == 1
    assert "bundled example" in registry.warnings[0]


def test_two_bundled_examples_colliding_is_not_the_users_problem(tmp_path: Path) -> None:
    """Shipped-content noise the user cannot act on, kept out of anything user-facing."""
    example_dir = tmp_path / "examples"
    example_dir.mkdir()
    write_yaml(example_dir / "a.yaml", COLLIDING_TEMPLATE.format(name="a", aliases='["frio"]'))
    write_yaml(example_dir / "b.yaml", COLLIDING_TEMPLATE.format(name="b", aliases='["frio"]'))
    registry = TemplateRegistry(tmp_path, example_dir)
    registry.load_all()

    assert registry.warnings == []
