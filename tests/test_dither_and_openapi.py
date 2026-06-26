# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Floyd–Steinberg dithering (env default / API override / persist / replay)
and the OpenAPI/Swagger polish (security scheme, tags, documented error responses)."""

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


# ── OpenAPI / Swagger polish ─────────────────────────────────────────────────────
def test_openapi_has_bearer_security_scheme(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert "HTTPBearer" in schemes
    assert schemes["HTTPBearer"]["scheme"] == "bearer"


def test_protected_route_carries_security_requirement(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    print_op = spec["paths"]["/print"]["post"]
    assert any("HTTPBearer" in req for req in print_op.get("security", []))


def test_operations_are_tagged(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["paths"]["/print"]["post"]["tags"] == ["Printing"]
    assert spec["paths"]["/health"]["get"]["tags"] == ["System"]
    assert spec["paths"]["/templates"]["get"]["tags"] == ["Templates"]
    assert spec["paths"]["/history/list"]["get"]["tags"] == ["History"]


def test_reprint_documents_404_and_409(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/reprint/{job_id}"]["post"]["responses"]
    assert "404" in responses
    assert "409" in responses
    assert "401" in responses


def test_print_request_schema_documents_dither(client: TestClient) -> None:
    """`dither` must surface in the request schema, now nested under the RenderOptions group."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    # PrintRequest exposes the grouped `options` sub-model, not a flat `dither`.
    assert "options" in schemas["PrintRequest"]["properties"]
    assert "dither" not in schemas["PrintRequest"]["properties"]
    assert "dither" in schemas["RenderOptions"]["properties"]


# ── Threshold: request reaches the driver ────────────────────────────────────────
def _last_threshold_opt(main_mod: object) -> float:
    """The ``threshold`` value the driver's render_payload most recently received."""
    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    return opts["threshold"]


def test_threshold_explicit_reaches_driver(client: TestClient) -> None:
    """An explicit threshold value must arrive at the driver unchanged."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"threshold": 50.0},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_threshold_opt(main_mod) == 50.0


def test_threshold_omitted_uses_default(client: TestClient) -> None:
    """Omitting threshold inherits the env default (70.0 out of the box)."""
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_threshold_opt(main_mod) == 70.0


def test_threshold_omitted_uses_custom_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting threshold with DEFAULT_THRESHOLD=30.0 inherits the custom default."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_threshold", 30.0)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_threshold_opt(main_mod) == 30.0


def test_threshold_explicit_overrides_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit threshold overrides a different env default."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_threshold", 90.0)
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"threshold": 40.0},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_threshold_opt(main_mod) == 40.0


# ── Threshold: persisted effective value ─────────────────────────────────────────
def test_threshold_resolved_value_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RESOLVED effective threshold (env default applied) is stored, not the nullable request."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_threshold", 55.0)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.options.threshold == 55.0


# ── Threshold: reprint replays the saved value ───────────────────────────────────
def test_reprint_replays_saved_threshold(client: TestClient) -> None:
    """A job saved with threshold=25.0 must replay threshold=25.0 to the driver on reprint."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"threshold": 25.0},
            "dry_run": False,
        },
    )
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.options.threshold == 25.0

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 200
    assert _last_threshold_opt(main_mod) == 25.0


# ── Threshold: reprint with None threshold (legacy record) does not crash ────────
def test_reprint_none_threshold_defaults_to_70(client: TestClient) -> None:
    """A PrintJobRecord whose options.threshold is None (default-constructed RenderOptions)
    must NOT crash on /reprint with a 500 (float(None) -> TypeError).  It must succeed and pass
    the DEFAULT_THRESHOLD (70.0) to the driver — exactly as bool(None) yields False for dither."""
    import app.main as main_mod
    from app.models import PrintJobRecord, RenderOptions

    # Construct a legacy record: options has no threshold (None, the model default).
    record = PrintJobRecord(
        job_id="pre-r4-no-threshold",
        template="simple",
        fields={"title": "Legacy"},
        copies=1,
        dry_run=False,
        timestamp="2025-01-01T00:00:00",
        language="en",
        cut=True,
        options=RenderOptions(),  # threshold=None, dither=None
        render_now="2025-01-01T00:00:00",
        status="printed",
    )
    assert record.options.threshold is None  # confirm the pre-condition

    main_mod._history.save(record)

    reprint = client.post("/reprint/pre-r4-no-threshold")
    assert reprint.status_code == 200, f"Expected 200, got {reprint.status_code}: {reprint.text}"

    # The driver must have received the default threshold (70.0), not None.
    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    assert opts["threshold"] == 70.0


# ── Threshold: idempotency fingerprint distinguishes different thresholds ─────────
def test_idempotency_key_reused_with_different_threshold_is_rejected(client: TestClient) -> None:
    """Reusing a key with a different threshold is a different print, not a retry -> 409.

    threshold is automatically folded into the fingerprint
    via options.model_dump() without any per-field fingerprint edit.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print", json={**body, "idempotency_key": "t1", "options": {"threshold": 70.0}}
    )
    assert r1.status_code == 200
    resp = client.post(
        "/print", json={**body, "idempotency_key": "t1", "options": {"threshold": 50.0}}
    )
    assert resp.status_code == 409


def test_idempotency_null_threshold_matches_resolved_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null threshold and an explicit one that resolve to the same value are one print.

    With DEFAULT_THRESHOLD=80.0, threshold:null resolves to 80.0; a follow-up threshold:80.0
    under the same key must dedupe to the original job (200, same job_id), not 409.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_threshold", 80.0)
    body = {
        "template": "simple",
        "fields": {"title": "X"},
        "dry_run": False,
        "idempotency_key": "t2",
    }
    r1 = client.post("/print", json=body)  # threshold omitted -> resolves to 80.0
    assert r1.status_code == 200
    r2 = client.post("/print", json={**body, "options": {"threshold": 80.0}})
    assert r2.status_code == 200
    assert r2.json()["job_id"] == r1.json()["job_id"]


# ── threshold canonicalization under dither ───────────────────────────────────────
def test_dither_on_canonicalizes_threshold_in_record(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When effective dither is True, the frozen record.options.threshold must equal
    settings.default_threshold regardless of what threshold the request supplied.

    threshold is a no-op in brother_ql under Floyd-Steinberg dither, so freezing the
    caller-supplied value would imply a precision that does not exist and create spurious
    fingerprint differences between otherwise-identical prints.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_threshold", 70.0)
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"dither": True, "threshold": 30.0},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    # threshold must be collapsed to the canonical default, not the caller-supplied 30.0
    assert record.options.threshold == 70.0, (
        f"expected canonical threshold 70.0 (default_threshold), got {record.options.threshold}"
    )


def test_dither_on_threshold_does_not_split_idempotency_fingerprint(
    client: TestClient,
) -> None:
    """Under dither=True, two requests differing ONLY in threshold must produce the same
    idempotency fingerprint — the second call with the same key must dedupe (200, same job_id),
    not 409.

    The fix: threshold is canonicalized to settings.default_threshold before fingerprinting
    when effective_dither is True, so distinct caller-supplied thresholds hash identically.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={**body, "idempotency_key": "r4-a", "options": {"dither": True, "threshold": 40.0}},
    )
    assert r1.status_code == 200
    # Same key, same dither=True, but different threshold — must dedupe because threshold
    # is a no-op under dither and is canonicalized to the same value in both fingerprints.
    r2 = client.post(
        "/print",
        json={**body, "idempotency_key": "r4-a", "options": {"dither": True, "threshold": 90.0}},
    )
    assert r2.status_code == 200, f"expected 200 (dedupe), got {r2.status_code}: {r2.text}"
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_default_dither_true_canonicalizes_threshold(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When DEFAULT_DITHER=true and dither is omitted from the request, the effective dither is
    True (inherited from the server default), so the threshold must still be canonicalized.

    Ensures the canonicalization applies to the server-default-dither path, not only when the
    request explicitly sets dither:true.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_dither", True)
    monkeypatch.setattr(main_mod.settings, "default_threshold", 70.0)

    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    # First request: dither inherited from server default (True), threshold=25.0
    r1 = client.post(
        "/print",
        json={**body, "idempotency_key": "r4-b", "options": {"threshold": 25.0}},
    )
    assert r1.status_code == 200
    record1 = main_mod._load_job(r1.json()["job_id"])
    assert record1 is not None
    assert record1.options.threshold == 70.0, (
        f"expected canonical 70.0, got {record1.options.threshold}"
    )

    # Second request: same key, different threshold — must dedupe (threshold is no-op under dither)
    r2 = client.post(
        "/print",
        json={**body, "idempotency_key": "r4-b", "options": {"threshold": 85.0}},
    )
    assert r2.status_code == 200, (
        f"expected 200 (dedupe under default_dither=True), got {r2.status_code}: {r2.text}"
    )
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_dither_off_keeps_distinct_threshold_fingerprints(client: TestClient) -> None:
    """Regression: with dither=False, two requests with different thresholds must still produce
    DISTINCT fingerprints — the existing behavior must be preserved when dither is off.

    threshold IS meaningful under no-dither (it drives the B/W cutoff), so collapsing it would
    cause identical-key retries with intentionally different thresholds to silently dedupe.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={**body, "idempotency_key": "r4-c", "options": {"dither": False, "threshold": 40.0}},
    )
    assert r1.status_code == 200
    # Different threshold, same key, dither=False: must still be treated as a different print → 409
    r2 = client.post(
        "/print",
        json={**body, "idempotency_key": "r4-c", "options": {"dither": False, "threshold": 90.0}},
    )
    assert r2.status_code == 409, (
        f"expected 409 (distinct prints, dither=False), got {r2.status_code}: {r2.text}"
    )


# ── Threshold: out-of-range values are rejected ───────────────────────────────────
def test_threshold_zero_is_rejected(client: TestClient) -> None:
    """threshold=0 is out of range (gt=0) and must be rejected with 422."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"threshold": 0},
            "dry_run": False,
        },
    )
    assert resp.status_code == 422


def test_threshold_above_100_is_rejected(client: TestClient) -> None:
    """threshold=100.1 is out of range (le=100) and must be rejected with 422."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"threshold": 100.1},
            "dry_run": False,
        },
    )
    assert resp.status_code == 422


def test_threshold_100_is_accepted(client: TestClient) -> None:
    """threshold=100 is on the boundary (le=100) and must be accepted."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"threshold": 100},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200


# ── Threshold: OpenAPI schema documents threshold ─────────────────────────────────
def test_print_request_schema_documents_threshold(client: TestClient) -> None:
    """`threshold` must surface in the RenderOptions schema nested under PrintRequest."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "threshold" not in schemas["PrintRequest"]["properties"]
    assert "threshold" in schemas["RenderOptions"]["properties"]


# ── Settings validation: DEFAULT_THRESHOLD bounds enforced at config load ─────────
def test_settings_default_threshold_zero_rejected() -> None:
    """DEFAULT_THRESHOLD=0 is out of range (gt=0) and must fail at Settings construction."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(default_threshold=0)


def test_settings_default_threshold_above_100_rejected() -> None:
    """DEFAULT_THRESHOLD=101 is out of range (le=100) and must fail at Settings construction."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(default_threshold=101)


def test_settings_default_threshold_nan_rejected() -> None:
    """DEFAULT_THRESHOLD=nan is non-finite and must fail at Settings construction."""
    import math

    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(default_threshold=math.nan)


def test_settings_default_threshold_inf_rejected() -> None:
    """DEFAULT_THRESHOLD=inf is non-finite and must fail at Settings construction."""
    import math

    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(default_threshold=math.inf)


def test_settings_default_threshold_valid_loads() -> None:
    """A valid DEFAULT_THRESHOLD (e.g. 80) loads successfully and is stored as-is."""
    from app.config import Settings

    s = Settings(default_threshold=80.0)
    assert s.default_threshold == 80.0


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


# ── two-color (red/black) — env default / API override / persist / replay ─────────
def _last_red_opt(main_mod: object) -> bool:
    """The ``red`` value the driver's render_payload most recently received."""
    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    return opts["red"]


def test_red_true_reaches_driver(client: TestClient) -> None:
    """An explicit red:true on a two-color model + red media must arrive at the driver as red=True."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert _last_red_opt(main_mod) is True


def test_red_omitted_uses_default_false(client: TestClient) -> None:
    """Omitting red inherits the env default, which is False out of the box."""
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200
    assert _last_red_opt(main_mod) is False


def test_red_omitted_uses_default_true(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting red with DEFAULT_RED=true inherits the True env default (on a red-capable template)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_red", True)
    resp = client.post(
        "/print", json={"template": "red-label", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200, resp.text
    assert _last_red_opt(main_mod) is True


def test_red_false_overrides_true_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit red:false must turn a True env default back off."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_red", True)
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"red": False},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200
    assert _last_red_opt(main_mod) is False


def test_red_resolved_value_persisted(client: TestClient) -> None:
    """The RESOLVED effective red is stored on record.options.red, not the nullable request."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.options.red is True


def test_reprint_replays_saved_red(client: TestClient) -> None:
    """A job saved with red=True must replay red=True to the driver on reprint."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.options.red is True

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 200, reprint.text
    assert _last_red_opt(main_mod) is True


def test_idempotency_key_reused_with_different_red_is_rejected(client: TestClient) -> None:
    """Reusing a key with a different red is a different print, not a retry -> 409.

    red folds into the fingerprint via options.model_dump()
    with no per-field fingerprint edit.
    """
    body = {"template": "red-label", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post("/print", json={**body, "idempotency_key": "rd1", "options": {"red": False}})
    assert r1.status_code == 200
    resp = client.post("/print", json={**body, "idempotency_key": "rd1", "options": {"red": True}})
    assert resp.status_code == 409


def test_idempotency_null_red_matches_resolved_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null red and an explicit one that resolve to the same value are one print (dedupe)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_red", True)
    body = {
        "template": "red-label",
        "fields": {"title": "X"},
        "dry_run": False,
        "idempotency_key": "rd2",
    }
    r1 = client.post("/print", json=body)  # red omitted -> resolves to True
    assert r1.status_code == 200
    r2 = client.post("/print", json={**body, "options": {"red": True}})
    assert r2.status_code == 200
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_red_true_on_non_red_media_rejected_422(client: TestClient) -> None:
    """red=true on a template bound to plain (non-red) media is a clean 422, never a 500.

    The `simple` template prints on `62` (black/white) media; red printing there would silently lose
    the red layer, so the capability gate rejects it.
    """
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 422, resp.text


def test_red_true_on_non_two_color_model_rejected_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red=true when the configured model lacks two-color support is a clean 422, not a 500."""
    import app.main as main_mod
    from app.drivers.brother_ql import BrotherQLDriver

    monkeypatch.setattr(main_mod.settings, "model", "QL-700")
    monkeypatch.setattr(main_mod, "_driver_cls", BrotherQLDriver.for_model("QL-700"))
    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 422, resp.text


def test_red_none_record_defaults_false_on_reprint(client: TestClient) -> None:
    """A PrintJobRecord whose options.red is None (legacy record) must not crash on /reprint.

    It must succeed and pass red=False to the driver — mirroring the legacy threshold/high_res tests.
    """
    import app.main as main_mod
    from app.models import PrintJobRecord, RenderOptions

    record = PrintJobRecord(
        job_id="pre-r3-no-red",
        template="simple",
        fields={"title": "Legacy"},
        copies=1,
        dry_run=False,
        timestamp="2025-01-01T00:00:00",
        language="en",
        cut=True,
        options=RenderOptions(),  # red=None (model default)
        render_now="2025-01-01T00:00:00",
        status="printed",
    )
    assert record.options.red is None

    main_mod._history.save(record)
    reprint = client.post("/reprint/pre-r3-no-red")
    assert reprint.status_code == 200, reprint.text
    assert _last_red_opt(main_mod) is False


def test_print_request_schema_documents_red(client: TestClient) -> None:
    """`red` must surface in the RenderOptions schema nested under PrintRequest."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "red" not in schemas["PrintRequest"]["properties"]
    assert "red" in schemas["RenderOptions"]["properties"]


def test_capabilities_reports_two_color(client: TestClient) -> None:
    """/capabilities surfaces two_color + red_labels so clients can discover the feature."""
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["two_color"] is True  # QL-810W (the test default model)
    assert "62red" in body["red_labels"]


def test_print_page_red_toggle_inherits_default_red(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a two-color model the print page renders a red box, pre-checked iff DEFAULT_RED=true."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_red", True)
    assert 'id="red" checked' in client.get("/").text

    monkeypatch.setattr(main_mod.settings, "default_red", False)
    html = client.get("/").text
    assert 'id="red"' in html and 'id="red" checked' not in html


# ── /reprint two-color media/capability drift guard ──────────────────────────────
def test_reprint_red_job_against_non_red_media_template_is_409(client: TestClient) -> None:
    """A saved red=True job whose current template is now bound to non-red media must 409 on reprint.

    Mirrors the {{seq}} reprint-drift guard: the original /print gate checked model+media at submit
    time, but the template may since have been rebound to plain (62) media. convert(red=True) would
    silently lose the red layer; /reprint must catch this statically and return 409.

    Uses the legacy-record seeding pattern to inject a pre-existing record directly into history,
    simulating a job that was printed with red=True against the (now-changed) template.
    """
    import app.main as main_mod
    from app.models import PrintJobRecord, RenderOptions

    # Seed a history record as if it was printed with red=True against "simple" (which uses
    # label="62", plain black/white media — NOT a red-capable label).
    record = PrintJobRecord(
        job_id="reprint-red-drift-1",
        template="simple",
        fields={"title": "Legacy Red"},
        copies=1,
        dry_run=False,
        timestamp="2026-01-01T00:00:00",
        language="en",
        cut=True,
        options=RenderOptions(dither=False, threshold=70.0, high_res=False, red=True),
        render_now="2026-01-01T00:00:00",
        status="printed",
    )
    main_mod._history.save(record)

    resp = client.post("/reprint/reprint-red-drift-1")
    assert resp.status_code == 409, (
        f"expected 409 (red media drift), got {resp.status_code}: {resp.text}"
    )
    assert "two-color" in resp.text.lower() or "red" in resp.text.lower(), (
        "409 detail must mention the two-color/red reason"
    )


def test_reprint_red_job_with_valid_red_media_succeeds(client: TestClient) -> None:
    """A saved red=True job against the red-label template (62red media) must still reprint 200.

    Regression guard: the drift check must only fire when the model/media is INCOMPATIBLE, not
    when everything is fine. The red-label template in the test fixture uses label=62red which is
    in CAPABILITY.red_labels for the QL-810W (the test default model).
    """
    # Print via the normal path first so the record is properly saved.
    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 200, reprint.text


def test_print_red_true_on_non_red_media_still_422(client: TestClient) -> None:
    """/print with red=True on plain (non-red) media is still a 422, not changed by this fix."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "options": {"red": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 422, resp.text


# ── dither and threshold are inert under red — canonicalize in fingerprint ────────
def test_red_canonicalizes_dither_in_record(client: TestClient) -> None:
    """When effective red=True, the frozen record.options.dither must be False regardless of the
    request value — dither is inert under brother_ql's HSV-separation two-color path.
    """
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True, "dither": True},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.options.dither is False, (
        f"expected canonical dither=False under red, got {record.options.dither}"
    )


def test_red_preserves_threshold_in_record(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When effective red=True, the frozen record.options.threshold must PRESERVE the requested
    non-default threshold — threshold IS applied by convert() on both the red and black layers
    after HSV separation, so it materially changes output and must be honored in history.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "default_threshold", 70.0)
    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True, "threshold": 40.0},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.options.threshold == 40.0, (
        f"expected threshold=40.0 preserved under red, got {record.options.threshold}"
    )


def test_red_dither_does_not_split_idempotency_fingerprint(client: TestClient) -> None:
    """Under red=True, two requests differing ONLY in dither must produce the same fingerprint.

    The second call with the same key must dedupe (200, same job_id), not 409 — dither is inert
    under the two-color HSV-separation path and must not cause spurious idempotency splits.
    """
    body = {"template": "red-label", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={**body, "idempotency_key": "r3-f2-a", "options": {"red": True, "dither": False}},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/print",
        json={**body, "idempotency_key": "r3-f2-a", "options": {"red": True, "dither": True}},
    )
    assert r2.status_code == 200, (
        f"expected 200 (dedupe: dither is inert under red), got {r2.status_code}: {r2.text}"
    )
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_red_threshold_does_split_idempotency_fingerprint(client: TestClient) -> None:
    """Under red=True, two requests differing ONLY in threshold must produce DISTINCT fingerprints.

    threshold IS applied by convert() on both the red and black layers after HSV separation —
    it materially changes output under red. A reused idempotency_key with a different threshold
    must be rejected with 409, not silently deduped.
    """
    body = {"template": "red-label", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={**body, "idempotency_key": "r3-f2-b", "options": {"red": True, "threshold": 40.0}},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/print",
        json={**body, "idempotency_key": "r3-f2-b", "options": {"red": True, "threshold": 90.0}},
    )
    assert r2.status_code == 409, (
        f"expected 409 (distinct fingerprints: threshold is honored under red), "
        f"got {r2.status_code}: {r2.text}"
    )


def test_red_threshold_reaches_driver(client: TestClient) -> None:
    """Under red=True, the requested threshold must reach the driver unchanged.

    Proves that threshold is forwarded to convert() under the two-color path, not silently
    replaced with the default — it IS applied to both the red and black layers after HSV filtering.
    """
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "red-label",
            "fields": {"title": "X"},
            "options": {"red": True, "threshold": 35.0},
            "dry_run": False,
        },
    )
    assert resp.status_code == 200, resp.text
    args, _ = main_mod._driver.render_payload.call_args
    _png, opts = args
    assert opts["threshold"] == 35.0, (
        f"expected threshold=35.0 forwarded to driver under red, got {opts['threshold']}"
    )


def test_non_red_dither_fingerprint_distinction_preserved(client: TestClient) -> None:
    """Regression: with red=False, two requests with different dither must still produce
    DISTINCT fingerprints — the existing behavior must be preserved when red is off.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={**body, "idempotency_key": "r3-f2-c", "options": {"red": False, "dither": False}},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/print",
        json={**body, "idempotency_key": "r3-f2-c", "options": {"red": False, "dither": True}},
    )
    assert r2.status_code == 409, (
        f"expected 409 (distinct prints: dither differs, red=False), got {r2.status_code}"
    )


def test_non_red_threshold_fingerprint_distinction_preserved(client: TestClient) -> None:
    """Regression: with red=False and dither=False, two requests with different thresholds must
    still produce DISTINCT fingerprints — the existing behavior must be preserved when red is off.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={
            **body,
            "idempotency_key": "r3-f2-d",
            "options": {"red": False, "dither": False, "threshold": 40.0},
        },
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/print",
        json={
            **body,
            "idempotency_key": "r3-f2-d",
            "options": {"red": False, "dither": False, "threshold": 90.0},
        },
    )
    assert r2.status_code == 409, (
        f"expected 409 (distinct prints: threshold differs, red=False dither=False), "
        f"got {r2.status_code}"
    )
