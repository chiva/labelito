# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the 600 dpi high_res render option: env default / API override / persist / replay / idempotency fingerprint / web-UI checkbox default / OpenAPI schema."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ── high_res — env default / API override / persist / replay ──────────────────────
def _last_high_res_opt(main_mod: object) -> bool:
    """The ``high_res`` value the driver's render_payload most recently received."""
    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    return opts["high_res"]


def test_high_res_true_reaches_driver(client: TestClient) -> None:
    """An explicit high_res:true must arrive at the driver as high_res=True."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"high_res": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_high_res_opt(main_mod) is True


def test_high_res_omitted_uses_default_false(client: TestClient) -> None:
    """Omitting high_res inherits the env default, which is False out of the box."""
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_high_res_opt(main_mod) is False


def test_high_res_omitted_uses_default_true(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting high_res with DEFAULT_HIGH_RES=true inherits the True env default."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_high_res", True)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_high_res_opt(main_mod) is True


def test_high_res_false_overrides_true_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit high_res:false must turn a True env default back off."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_high_res", True)
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"high_res": False},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_high_res_opt(main_mod) is False


def test_high_res_resolved_value_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RESOLVED effective high_res (env default applied) is stored, not the nullable request."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_high_res", True)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.options.high_res is True


def test_reprint_replays_saved_high_res(client: TestClient) -> None:
    """A job saved with high_res=True must replay high_res=True to the driver on reprint."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"high_res": True},
            "dry_run": False,
        },
    )
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.options.high_res is True

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 200
    assert _last_high_res_opt(main_mod) is True


def test_idempotency_key_reused_with_different_high_res_is_rejected(client: TestClient) -> None:
    """Reusing a key with a different high_res is a different print, not a retry -> 409.

    high_res is automatically folded into the fingerprint
    via options.model_dump() without any per-field fingerprint edit.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print", json={**body, "idempotency_key": "hr1", "options": {"high_res": False}}
    )
    assert r1.status_code == 200
    resp = client.post(
        "/print", json={**body, "idempotency_key": "hr1", "options": {"high_res": True}}
    )
    assert resp.status_code == 409


def test_idempotency_null_high_res_matches_resolved_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null high_res and an explicit one that resolve to the same value are one print.

    With DEFAULT_HIGH_RES=true, high_res:null resolves to True; a follow-up high_res:true
    under the same key must dedupe to the original job (200, same job_id), not 409.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_high_res", True)
    body = {
        "template": "simple",
        "fields": {"title": "X"},
        "dry_run": False,
        "idempotency_key": "hr2",
    }
    r1 = client.post("/print", json=body)  # high_res omitted -> resolves to True
    assert r1.status_code == 200
    r2 = client.post("/print", json={**body, "options": {"high_res": True}})
    assert r2.status_code == 200
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_print_page_checkbox_inherits_default_high_res(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With DEFAULT_HIGH_RES=true the print page renders the high-res box pre-checked."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_high_res", True)
    assert 'id="high-res" checked' in client.get("/").text

    monkeypatch.setattr(main_mod.settings, "default_high_res", False)
    html = client.get("/").text
    assert 'id="high-res"' in html and 'id="high-res" checked' not in html


def test_print_request_schema_documents_high_res(client: TestClient) -> None:
    """`high_res` must surface in the RenderOptions schema nested under PrintRequest."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "high_res" not in schemas["PrintRequest"]["properties"]
    assert "high_res" in schemas["RenderOptions"]["properties"]


def test_high_res_none_record_defaults_to_false_on_reprint(client: TestClient) -> None:
    """A PrintJobRecord whose options.high_res is None (legacy record) must not crash on /reprint.

    It must succeed and pass high_res=False to the driver — mirroring the legacy threshold test.
    """
    import app.main as main_mod
    from app.models import PrintJobRecord, RenderOptions

    record = PrintJobRecord(
        job_id="pre-r5-no-high-res",
        template="simple",
        fields={"title": "Legacy"},
        copies=1,
        dry_run=False,
        timestamp="2025-01-01T00:00:00",
        language="en",
        cut=True,
        options=RenderOptions(),  # high_res=None (model default)
        render_now="2025-01-01T00:00:00",
        status="printed",
    )
    assert record.options.high_res is None

    main_mod._history.save(record)

    reprint = client.post("/reprint/pre-r5-no-high-res")
    assert reprint.status_code == 200, f"Expected 200, got {reprint.status_code}: {reprint.text}"

    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    assert opts["high_res"] is False
