"""Tests for inline (on-the-fly) templates on /print and /preview.

An inline request carries a full template YAML body (`template_inline`) instead of a stored
template name, gated by INLINE_TEMPLATES_ENABLED. The body is validated by the same path a saved
file gets, and on /print it is frozen into history so /reprint reproduces it exactly.
"""

import io
import textwrap

import pytest
from fastapi.testclient import TestClient
from PIL import Image

# A valid inline template body: requires `title`, optional `subtitle`.
INLINE_YAML = textwrap.dedent("""\
    name: inline-demo
    description: An inline template body
    label: "62"
    fields:
      required: [title]
      optional: [subtitle]
    layout:
      - {type: title, text: "{{title}}"}
      - {type: subtitle, text: "{{subtitle}}"}
""")


@pytest.fixture
def inline_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The shared client with INLINE_TEMPLATES_ENABLED flipped on (default is off)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "inline_templates_enabled", True)
    return client


def test_inline_print_dry_run_freezes_source(inline_client: TestClient) -> None:
    """An inline dry-run print renders, reports the parsed name, and freezes the body in history."""
    import app.main as main_mod

    resp = inline_client.post(
        "/print",
        json={"template_inline": INLINE_YAML, "fields": {"title": "Hi"}, "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_run"] is True
    assert data["template"] == "inline-demo"  # the parsed internal name

    record = main_mod._load_job(data["job_id"])
    assert record is not None
    assert record.template_source == INLINE_YAML  # frozen verbatim for /reprint


def test_inline_print_calls_driver(inline_client: TestClient) -> None:
    import app.main as main_mod

    resp = inline_client.post(
        "/print",
        json={"template_inline": INLINE_YAML, "fields": {"title": "Real"}, "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    main_mod._driver.render_payload.assert_called()


def test_inline_and_name_both_is_422(inline_client: TestClient) -> None:
    """Supplying both a stored name and an inline body is ambiguous → 422 (model validator)."""
    resp = inline_client.post(
        "/print",
        json={"template": "simple", "template_inline": INLINE_YAML, "fields": {"title": "x"}},
    )
    assert resp.status_code == 422


def test_neither_template_nor_inline_is_422(inline_client: TestClient) -> None:
    """Omitting both a name and a body is a 422 — there is nothing to render."""
    resp = inline_client.post("/print", json={"fields": {"title": "x"}})
    assert resp.status_code == 422


def test_inline_disabled_is_403(client: TestClient) -> None:
    """With INLINE_TEMPLATES_ENABLED off (the shared client default), an inline print is refused."""
    resp = client.post(
        "/print",
        json={"template_inline": INLINE_YAML, "fields": {"title": "x"}, "dry_run": True},
    )
    assert resp.status_code == 403


def test_inline_missing_required_field_is_422(inline_client: TestClient) -> None:
    """The inline body's required-field contract is enforced exactly like a stored template's."""
    resp = inline_client.post(
        "/print", json={"template_inline": INLINE_YAML, "fields": {}, "dry_run": True}
    )
    assert resp.status_code == 422


def test_inline_malformed_yaml_is_422(inline_client: TestClient) -> None:
    resp = inline_client.post(
        "/print",
        json={"template_inline": "name: [unclosed", "fields": {"title": "x"}, "dry_run": True},
    )
    assert resp.status_code == 422


def test_inline_oversized_yaml_is_422(inline_client: TestClient) -> None:
    """A body beyond MAX_TEMPLATE_YAML_CHARS (64 KiB) is rejected by the model before parsing."""
    from app.models import MAX_TEMPLATE_YAML_CHARS

    oversized = "#" * (MAX_TEMPLATE_YAML_CHARS + 1)
    resp = inline_client.post(
        "/print",
        json={"template_inline": oversized, "fields": {"title": "x"}, "dry_run": True},
    )
    assert resp.status_code == 422


def test_inline_preview_returns_png(inline_client: TestClient) -> None:
    resp = inline_client.post(
        "/preview", json={"template_inline": INLINE_YAML, "fields": {"title": "Hello"}}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.width > 0 and img.height > 0


def test_inline_preview_disabled_is_403(client: TestClient) -> None:
    resp = client.post(
        "/preview", json={"template_inline": INLINE_YAML, "fields": {"title": "Hello"}}
    )
    assert resp.status_code == 403


def test_reprint_inline_job_reproduces_without_registry_entry(inline_client: TestClient) -> None:
    """An inline job reprints from its frozen source even though 'inline-demo' is not in the registry."""
    import app.main as main_mod

    assert main_mod.registry.get("inline-demo") is None  # never stored on disk

    resp = inline_client.post(
        "/print",
        json={"template_inline": INLINE_YAML, "fields": {"title": "Original"}, "dry_run": True},
    )
    job_id = resp.json()["job_id"]

    reprinted = inline_client.post(f"/reprint/{job_id}")
    assert reprinted.status_code == 200, reprinted.text
    assert reprinted.json()["template"] == "inline-demo"


def test_reprint_inline_uses_frozen_source_not_shadowing_stored_template(
    inline_client: TestClient,
) -> None:
    """If a DIFFERENT stored template later claims the same name, reprint still uses the frozen body.

    The stored 'inline-demo' requires a field the original job never supplied; if reprint resolved
    the registry version it would 409 on the missing field. Reproducing from the frozen source
    (which only needs `title`) succeeds — proving the body, not the name, drives reprint.
    """
    import app.main as main_mod

    resp = inline_client.post(
        "/print",
        json={"template_inline": INLINE_YAML, "fields": {"title": "Original"}, "dry_run": True},
    )
    job_id = resp.json()["job_id"]

    # Now register a conflicting stored template with the same internal name.
    (main_mod.registry.templates_dir / "inline-demo.yaml").write_text(
        textwrap.dedent("""\
        name: inline-demo
        description: A DIFFERENT stored template that shares the inline name
        label: "62"
        fields:
          required: [other_field]
          optional: []
        layout:
          - {type: title, text: "{{other_field}}"}
    """)
    )
    assert inline_client.post("/reload").status_code == 200
    assert main_mod.registry.get("inline-demo") is not None

    reprinted = inline_client.post(f"/reprint/{job_id}")
    assert reprinted.status_code == 200, reprinted.text


def test_inline_idempotency_distinct_bodies_collide_409(inline_client: TestClient) -> None:
    """Two different inline bodies sharing one idempotency_key are different prints → 409."""
    body_a = INLINE_YAML
    body_b = INLINE_YAML.replace("An inline template body", "A different inline body")

    first = inline_client.post(
        "/print",
        json={
            "template_inline": body_a,
            "fields": {"title": "x"},
            "dry_run": True,
            "idempotency_key": "shared-key",
        },
    )
    assert first.status_code == 200, first.text

    second = inline_client.post(
        "/print",
        json={
            "template_inline": body_b,
            "fields": {"title": "x"},
            "dry_run": True,
            "idempotency_key": "shared-key",
        },
    )
    assert second.status_code == 409


def test_inline_idempotency_same_body_dedupes(inline_client: TestClient) -> None:
    """The same inline body + key is a retry: the second call returns the first job, no reprint."""
    payload = {
        "template_inline": INLINE_YAML,
        "fields": {"title": "x"},
        "dry_run": True,
        "idempotency_key": "retry-key",
    }
    first = inline_client.post("/print", json=payload)
    second = inline_client.post("/print", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]


def test_inline_deeply_nested_yaml_is_422_not_500(inline_client: TestClient) -> None:
    """A deeply nested but within-cap body overflows PyYAML's recursion (RecursionError, not a
    YAMLError). It must map to 422 (bad client input), never a 500."""
    from app.models import MAX_TEMPLATE_YAML_CHARS

    depth = 20_000
    nested = "[" * depth + "0" + "]" * depth  # ~40 KiB of balanced nested flow sequences
    assert len(nested) < MAX_TEMPLATE_YAML_CHARS  # passes the length cap, reaches the parser

    resp = inline_client.post("/preview", json={"template_inline": nested, "fields": {}})
    assert resp.status_code == 422


def test_inline_preview_download_name_is_header_safe(inline_client: TestClient) -> None:
    """An inline template name is user-controlled and unconstrained by _SAFE_TEMPLATE_NAME, so the
    Content-Disposition filename must be sanitized — a CR/LF in the name must not inject headers."""
    crlf_yaml = (
        'name: "a\\r\\nX-Injected: pwned"\n'
        "description: header injection probe\n"
        'label: "62"\n'
        "fields:\n  required: [title]\n"
        'layout:\n  - {type: title, text: "{{title}}"}\n'
    )
    resp = inline_client.post(
        "/preview?download=true", json={"template_inline": crlf_yaml, "fields": {"title": "x"}}
    )
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert "x-injected" not in {k.lower() for k in resp.headers}


def test_inline_metrics_use_sentinel_label(inline_client: TestClient) -> None:
    """The labels_printed_total counter labels inline jobs with the fixed <inline> sentinel, not the
    (unbounded, user-controlled) inline template name."""
    resp = inline_client.post(
        "/print",
        json={"template_inline": INLINE_YAML, "fields": {"title": "x"}, "dry_run": False},
    )
    assert resp.status_code == 200
    metrics = inline_client.get("/metrics").text
    assert 'template="<inline>"' in metrics
    assert 'template="inline-demo"' not in metrics
