# SPDX-License-Identifier: GPL-3.0-or-later
"""Documentation tests — keep docs/template-format.md examples valid against the real loader.

The plan's contract for the template-format doc is "every documented example must parse via the
existing template validation". This extracts every ```yaml fenced block from the doc and runs it
through ``validate_template_from_string`` (the exact path a saved template and the studio draft
preview use). Full templates (name + layout) are validated as-is; standalone layout-element snippets
are wrapped in a minimal skeleton that declares the fields they reference, so a drifted example or a
renamed/removed element attribute fails CI instead of silently shipping a doc that lies.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.loader import validate_template_from_string
from app.render.engine import COMPUTED_TOKENS

DOC = Path(__file__).resolve().parent.parent / "docs" / "template-format.md"

_FENCE_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
_FIELD_TOKEN_RE = re.compile(r"\{\{(\w+)")  # the field-name start of a {{token}}


def _yaml_blocks() -> list[str]:
    return _FENCE_RE.findall(DOC.read_text(encoding="utf-8"))


def _is_full_template(block: str) -> bool:
    return bool(
        re.search(r"^\s*name\s*:", block, re.MULTILINE)
        and re.search(r"^\s*layout\s*:", block, re.MULTILINE)
    )


def _is_layout_fragment(block: str) -> bool:
    """A standalone layout snippet — the first non-blank line is a sequence item (`- ...`)."""
    for line in block.splitlines():
        if line.strip():
            return line.lstrip().startswith("-")
    return False


def _wrap_layout_fragment(block: str) -> str:
    """Wrap a layout snippet in a minimal template, declaring every referenced field as optional.

    Computed tokens (date/now/seq) are always available and never declared. Declaring exactly the
    referenced field tokens keeps the loader's declared⇔referenced check satisfied in both directions.
    """
    fields = sorted({t for t in _FIELD_TOKEN_RE.findall(block) if t not in COMPUTED_TOKENS})
    skeleton = 'name: doc-example\ndescription: doc example\nlabel: "62"\n'
    if fields:
        skeleton += "fields:\n  optional: [" + ", ".join(fields) + "]\n"
    skeleton += "layout:\n" + block
    return skeleton


def test_template_format_doc_has_examples() -> None:
    """Guard against the doc losing its examples entirely (which would make the parse test vacuous)."""
    blocks = _yaml_blocks()
    assert len(blocks) >= 10, (
        f"expected the template-format doc to carry its examples, got {len(blocks)}"
    )
    assert any(_is_full_template(b) for b in blocks), "the doc must contain a full template example"


def test_template_format_examples_parse() -> None:
    """Every ```yaml example parses through the real validator — full templates and layout snippets."""
    for block in _yaml_blocks():
        if _is_full_template(block):
            validate_template_from_string(block)
        elif _is_layout_fragment(block):
            validate_template_from_string(_wrap_layout_fragment(block))
        # Other fragments (e.g. the bare `fields:` mapping) are illustrative sub-snippets, not
        # standalone templates, and are intentionally not validated in isolation.
