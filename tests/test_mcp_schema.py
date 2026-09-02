# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the generated template-authoring reference (``app.mcp_schema``).

The document is served to MCP clients as ``docs://template-schema`` and they are told to read it
before writing a template, so a wrong instruction in it is worse than no instruction: it sends
every client down the same dead end, and the failure surfaces as a rejected save rather than as
anything pointing back here.

Three classes of guard, matching the three ways the document can be wrong:

* **Drift from the renderer** — the generated half falls behind the code. Asserted by checking every
  registered element type and every engine vocabulary appears.
* **Drift from the human reference** — ``docs/template-format.md`` documents the same language for
  people (the two coexist because ``.dockerignore`` keeps ``docs/`` out of the image, so the
  runtime has only the code to generate from). Asserted by requiring both to cover the same element
  types.
* **Falsehood** — the authored prose claims something the loader does not accept. Asserted two ways,
  because one is not enough: every yaml block the document recommends goes through the real
  validator, and the claims made only in a sentence are pinned individually. The ``max_lines``
  falsehood that prompted this file lived in prose, so the block check alone would have missed it.

The yaml checking deliberately mirrors ``tests/test_docs.py``, which does the same job for
``docs/template-format.md`` — same validator, same fragment-wrapping rule — so the two documents are
held to one standard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.loader import MAX_TEXT_LINES, validate_template_from_string
from app.mcp_schema import template_schema_markdown
from app.render.elements import ELEMENT_REGISTRY, KNOWN_COLLECTIONS
from app.render.engine import COMPUTED_TOKENS

HUMAN_DOC = Path(__file__).resolve().parent.parent / "docs" / "template-format.md"

_FENCE_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
_FIELD_TOKEN_RE = re.compile(r"\{\{(\w+)")


def _yaml_blocks() -> list[str]:
    blocks = _FENCE_RE.findall(template_schema_markdown())
    assert blocks, "the schema document has no yaml examples to validate"
    return blocks


def _as_template(block: str) -> str:
    """Return *block* as a complete template body, wrapping a layout fragment in a skeleton.

    Declares exactly the field tokens the fragment references (computed tokens are always available
    and must not be declared), which is what satisfies the loader's declared-vs-referenced check in
    both directions.
    """
    if re.search(r"^\s*name\s*:", block, re.MULTILINE):
        return block
    fields = sorted({t for t in _FIELD_TOKEN_RE.findall(block) if t not in COMPUTED_TOKENS})
    skeleton = 'name: schema-example\ndescription: schema example\nlabel: "62"\n'
    if fields:
        skeleton += "fields:\n  optional: [" + ", ".join(fields) + "]\n"
    return skeleton + "layout:\n" + block


@pytest.mark.parametrize("block", _yaml_blocks())
def test_every_documented_yaml_example_actually_loads(block: str) -> None:
    """Every yaml the document recommends must pass the real validator.

    Guards the copy-pasteable half: a client following an example must end up with something
    saveable. Uses ``validate_template_from_string`` — the same path a save and an inline print take
    — so an example cannot pass here and be rejected there.
    """
    validate_template_from_string(_as_template(block), source_name="<schema-doc>")


def test_documents_every_registered_element_type() -> None:
    """Every registered element type gets a section.

    Near-tautological while the tables are generated from the registry — which is the point: it is
    what fails if someone ever replaces the generation with a hand-maintained list.
    """
    markdown = template_schema_markdown()
    for name in ELEMENT_REGISTRY:
        assert f"### `{name}`" in markdown


def test_agrees_with_the_human_reference_on_element_coverage() -> None:
    """The MCP resource and docs/template-format.md must not diverge on what exists.

    They are two renderings of one language for two audiences. In practice this catches ONE
    direction: the generated side picks up a new element type by construction, so the assertion
    that bites is the human reference falling behind the registry. Kept symmetric anyway, because
    the cheap half is what proves the generation is still generating rather than hardcoding.
    """
    human = HUMAN_DOC.read_text(encoding="utf-8")
    generated = template_schema_markdown()
    for name in ELEMENT_REGISTRY:
        assert f"`{name}`" in human, f"{name} missing from the human reference"
        assert f"`{name}`" in generated, f"{name} missing from the MCP resource"


def test_documents_the_engine_vocabularies() -> None:
    """The collections and the max_lines bound are read from the code, not restated in prose."""
    markdown = template_schema_markdown()
    for collection in KNOWN_COLLECTIONS:
        assert f"`{collection}`" in markdown
    assert f"[1, {MAX_TEXT_LINES}]" in markdown


def test_max_lines_guidance_matches_the_loader() -> None:
    """Pin the specific claim the document makes: null and 0 are rejected, the bound loads.

    Without this, correcting the prose is a one-off; with it, a loader change that made ``null``
    legal (or moved the bound) fails here instead of silently making the reference wrong again.
    """
    base = """\
name: probe
description: probe
label: "62"
fields:
  required: [title]
layout:
  - {{type: title, text: "{{{{title}}}}", max_lines: {value}}}
"""

    def rejects(value: str) -> bool:
        try:
            validate_template_from_string(base.format(value=value), source_name="<probe>")
        except Exception:
            return True
        return False

    assert rejects("null"), "documented as rejected, but the validator accepted max_lines: null"
    assert rejects("0"), "documented as rejected, but the validator accepted max_lines: 0"
    assert not rejects(str(MAX_TEXT_LINES)), "the documented upper bound must itself validate"

    # And pin the prose itself, not just the validator: the document must not go back to offering a
    # way to remove the cap, since there is none.
    markdown = template_schema_markdown()
    assert 'no "unlimited"' in markdown
    assert "null` for no cap" not in markdown
