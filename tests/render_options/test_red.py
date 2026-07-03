# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for two-color (red/black) printing: env default / API override / persist / replay / media & capability drift guards / dither+threshold canonicalization under red."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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


def test_print_page_templates_json_carries_per_template_red_flag(client: TestClient) -> None:
    """The embedded TEMPLATES JSON flags which templates are bound to black/red media, so the print
    page can gate/pill the #red toggle per-template (a red-capable model can still hold non-red
    templates — the physical roll's colour is what actually matters, and that's unknowable from
    SNMP, so this is a per-template authoring signal, not a live-roll guarantee)."""
    import json
    import re

    page = client.get("/").text
    m = re.search(r"const TEMPLATES = (\[.*?\]);", page, re.DOTALL)
    assert m, "index page must embed a TEMPLATES JSON array"
    templates = json.loads(m.group(1))
    by_name = {t["name"]: t["red"] for t in templates}
    assert by_name["red-label"] is True, "the 62red-bound template must be flagged red"
    assert by_name["simple"] is False, "a plain 62 template must not be flagged red"


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
