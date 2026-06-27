# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the B/W threshold render option: override / persist / replay / canonicalization under dither / out-of-range rejection / OpenAPI schema / DEFAULT_THRESHOLD settings validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
