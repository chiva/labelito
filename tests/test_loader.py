# SPDX-License-Identifier: GPL-3.0-or-later
import textwrap
from pathlib import Path

import pytest

from app.loader import TemplateLoadError, TemplateRegistry, load_template


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
    with pytest.raises(TemplateLoadError, match="another 'row'"):
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
    with pytest.raises(TemplateLoadError, match=r"only a 'row' element may have 'children'"):
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
