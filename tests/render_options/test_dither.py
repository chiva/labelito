# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Floyd-Steinberg dithering: env default / API override / persist / replay / idempotency fingerprint / web-UI checkbox default / OpenAPI schema."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _last_dither_opt(main_mod: object) -> bool:
    """The ``dither`` value the driver's render_payload most recently received."""
    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    return opts["dither"]


# ── Dithering: request reaches the driver ────────────────────────────────────────
def test_dither_true_reaches_driver(client: TestClient) -> None:
    """An explicit dither:true must arrive at the driver as dither=True."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"dither": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_dither_opt(main_mod) is True


def test_dither_omitted_uses_default_false(client: TestClient) -> None:
    """Omitting the field inherits the env default, which is False out of the box."""
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_dither_opt(main_mod) is False


def test_dither_omitted_uses_default_true(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the field with DEFAULT_DITHER=true inherits the True env default."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", True)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_dither_opt(main_mod) is True


def test_dither_false_overrides_true_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit dither:false must turn a True env default back off (the `or`-trap case)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", True)
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"dither": False},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_dither_opt(main_mod) is False


def test_dither_true_overrides_false_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit dither:true must turn a False env default on."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", False)
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"dither": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_dither_opt(main_mod) is True


# ── Dithering: persisted effective value ─────────────────────────────────────────
def test_dither_resolved_value_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RESOLVED effective value (env default applied) is stored, not the nullable request."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", True)
    # Omit dither → resolves to the True default → persisted as True.
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.options.dither is True


# ── Dithering: reprint replays the saved value ───────────────────────────────────
def test_reprint_replays_saved_dither(client: TestClient) -> None:
    """A job saved with dither=True must replay dither=True to the driver on reprint."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"dither": True},
            "dry_run": False,
        },
    )
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.options.dither is True

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 200
    assert _last_dither_opt(main_mod) is True


# ── Dithering: idempotency fingerprint includes effective dither ─────────────────
def test_idempotency_key_reused_with_different_dither_is_rejected(client: TestClient) -> None:
    """Reusing a key with a different dither is a different print, not a retry → 409.

    Without dither in the fingerprint the second request would be deduped to the first job and
    return 200, silently printing nothing with the requested rasterization.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post("/print", json={**body, "idempotency_key": "d1", "options": {"dither": False}})
    assert r1.status_code == 200
    resp = client.post(
        "/print", json={**body, "idempotency_key": "d1", "options": {"dither": True}}
    )
    assert resp.status_code == 409


def test_idempotency_null_dither_matches_resolved_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null request and an explicit request that resolve to the same effective value are one print.

    With DEFAULT_DITHER=true, ``dither:null`` resolves to True; a follow-up ``dither:true`` under
    the same key must dedupe to the original job (200, same job_id), not 409 — they produce
    identical output, so the fingerprint must match.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", True)
    body = {
        "template": "simple",
        "fields": {"title": "X"},
        "dry_run": False,
        "idempotency_key": "d2",
    }
    r1 = client.post("/print", json=body)  # dither omitted → resolves to True
    assert r1.status_code == 200
    # explicit True → same effective value → same fingerprint
    r2 = client.post("/print", json={**body, "options": {"dither": True}})
    assert r2.status_code == 200
    assert r2.json()["job_id"] == r1.json()["job_id"]


# ── Dithering: the web UI checkbox inherits the env default ──────────────────────
def test_print_page_checkbox_inherits_default_dither(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With DEFAULT_DITHER=true the print page renders the dither box pre-checked (and unchecked
    when false), so the first-party UI honours the configured default instead of always sending
    dither:false."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", True)
    assert 'id="dither" checked' in client.get("/").text

    monkeypatch.setattr(main_mod.settings, "default_dither", False)
    html = client.get("/").text
    assert 'id="dither"' in html and 'id="dither" checked' not in html


def test_print_request_schema_documents_dither(client: TestClient) -> None:
    """`dither` must surface in the request schema, now nested under the RenderOptions group."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    # PrintRequest exposes the grouped `options` sub-model, not a flat `dither`.
    assert "options" in schemas["PrintRequest"]["properties"]
    assert "dither" not in schemas["PrintRequest"]["properties"]
    assert "dither" in schemas["RenderOptions"]["properties"]
