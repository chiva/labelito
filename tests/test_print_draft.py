"""Tests for POST /print/draft — printing the studio's in-memory draft YAML.

The route is the studio-gated twin of /print: it accepts a raw ``yaml`` body (like
``/preview/draft``) and funnels into the same print machinery (``_handle_print``). Crucially it is
gated by EDITOR_ENABLED, NOT by INLINE_TEMPLATES_ENABLED — the shared ``client`` fixture keeps
inline templates at their default (off), which is exactly the configuration this route must work
under.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

# A valid draft body: requires `title`, optional `subtitle` — mirrors the studio's starter seed.
DRAFT_YAML = textwrap.dedent("""\
    name: studio-draft
    description: A draft printed straight from the studio
    label: "62"
    fields:
      required: [title]
      optional: [subtitle]
    layout:
      - {type: title, text: "{{title}}"}
      - {type: subtitle, text: "{{subtitle}}"}
""")

SEQ_DRAFT_YAML = textwrap.dedent("""\
    name: seq-draft
    description: A numbered draft
    label: "62"
    layout:
      - {type: title, text: "No. {{seq}}"}
""")


def test_print_draft_works_with_inline_templates_disabled(client: TestClient) -> None:
    """The whole point of the route: a draft prints with INLINE_TEMPLATES_ENABLED at its default
    (off) — EDITOR_ENABLED alone authorizes it, exactly like /preview/draft."""
    import app.main as main_mod

    assert (
        main_mod.settings.inline_templates_enabled is False
    )  # fixture default, the interesting case

    resp = client.post(
        "/print/draft", json={"yaml": DRAFT_YAML, "fields": {"title": "Hi"}, "dry_run": True}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["template"] == "studio-draft"  # the parsed internal name
    assert data["dry_run"] is True

    # The body is frozen into history verbatim, so /reprint reproduces it with no registry entry.
    record = main_mod._load_job(data["job_id"])
    assert record is not None
    assert record.template_source == DRAFT_YAML


def test_print_draft_real_print_calls_driver(client: TestClient) -> None:
    import app.main as main_mod

    resp = client.post(
        "/print/draft", json={"yaml": DRAFT_YAML, "fields": {"title": "Real"}, "dry_run": False}
    )
    assert resp.status_code == 200, resp.text
    main_mod._driver.render_payload.assert_called()


def test_print_draft_options_are_honored(client: TestClient) -> None:
    """The request's rasterization options flow through to the resolved record — the draft route
    must not silently drop them on the way into _handle_print."""
    import app.main as main_mod

    resp = client.post(
        "/print/draft",
        json={
            "yaml": DRAFT_YAML,
            "fields": {"title": "x"},
            "dry_run": True,
            "options": {"dither": True},
        },
    )
    assert resp.status_code == 200, resp.text
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None and record.options.dither is True


def test_print_draft_missing_required_field_is_422_with_names(client: TestClient) -> None:
    """Same 422 detail shape as /preview/draft ({msg, missing_required}) so the studio's error
    flattener renders the field names."""
    resp = client.post("/print/draft", json={"yaml": DRAFT_YAML, "fields": {}, "dry_run": True})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["missing_required"] == ["title"]
    assert detail["template"] == "studio-draft"


def test_print_draft_malformed_yaml_is_422(client: TestClient) -> None:
    resp = client.post(
        "/print/draft", json={"yaml": "name: [unclosed", "fields": {}, "dry_run": True}
    )
    assert resp.status_code == 422


def test_print_draft_oversized_yaml_is_422(client: TestClient) -> None:
    from app.models import MAX_TEMPLATE_YAML_CHARS

    oversized = "#" * (MAX_TEMPLATE_YAML_CHARS + 1)
    resp = client.post("/print/draft", json={"yaml": oversized, "fields": {}, "dry_run": True})
    assert resp.status_code == 422


def test_print_draft_seq_without_sequence_is_422(client: TestClient) -> None:
    """A {{seq}} draft with no sequence spec would print blank numbers — rejected like /print."""
    resp = client.post("/print/draft", json={"yaml": SEQ_DRAFT_YAML, "fields": {}, "dry_run": True})
    assert resp.status_code == 422


def test_print_draft_sequence_batch_prints(client: TestClient) -> None:
    resp = client.post(
        "/print/draft",
        json={
            "yaml": SEQ_DRAFT_YAML,
            "fields": {},
            "dry_run": True,
            "sequence": {"start": 1, "count": 3},
        },
    )
    assert resp.status_code == 200, resp.text


def test_print_draft_sequence_and_copies_is_422(client: TestClient) -> None:
    """sequence and copies>1 are mutually exclusive — the internal PrintRequest's validator must
    surface as a 422, not a 500 (the handler builds that model by hand)."""
    resp = client.post(
        "/print/draft",
        json={
            "yaml": SEQ_DRAFT_YAML,
            "fields": {},
            "dry_run": True,
            "copies": 2,
            "sequence": {"start": 1, "count": 3},
        },
    )
    assert resp.status_code == 422


def test_print_draft_unknown_key_is_422(client: TestClient) -> None:
    """extra='forbid': a misspelled knob is an error, never silently ignored on a physical print."""
    resp = client.post(
        "/print/draft",
        json={"yaml": DRAFT_YAML, "fields": {"title": "x"}, "dry_run": True, "copise": 3},
    )
    assert resp.status_code == 422


def test_reprint_draft_job_reproduces_without_registry_entry(client: TestClient) -> None:
    import app.main as main_mod

    assert main_mod.registry.get("studio-draft") is None  # never stored on disk

    resp = client.post(
        "/print/draft", json={"yaml": DRAFT_YAML, "fields": {"title": "Once"}, "dry_run": True}
    )
    job_id = resp.json()["job_id"]

    reprinted = client.post(f"/reprint/{job_id}")
    assert reprinted.status_code == 200, reprinted.text
    assert reprinted.json()["template"] == "studio-draft"


def test_print_draft_idempotency_same_body_dedupes(client: TestClient) -> None:
    payload = {
        "yaml": DRAFT_YAML,
        "fields": {"title": "x"},
        "dry_run": True,
        "idempotency_key": "draft-retry-key",
    }
    first = client.post("/print/draft", json=payload)
    second = client.post("/print/draft", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]


def test_print_draft_editor_disabled_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EDITOR_ENABLED=false hides the route entirely (404, not 401/403), like the other studio
    routes — see also the shared gate tests in test_api.py."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "editor_enabled", False)
    resp = client.post(
        "/print/draft", json={"yaml": DRAFT_YAML, "fields": {"title": "x"}, "dry_run": True}
    )
    assert resp.status_code == 404


def test_print_draft_metrics_use_inline_sentinel(client: TestClient) -> None:
    """A draft print labels labels_printed_total with the fixed <inline> sentinel, not the
    unbounded user-controlled draft name — same cardinality guard as inline prints."""
    resp = client.post(
        "/print/draft", json={"yaml": DRAFT_YAML, "fields": {"title": "x"}, "dry_run": False}
    )
    assert resp.status_code == 200
    metrics = client.get("/metrics").text
    assert 'template="studio-draft"' not in metrics
