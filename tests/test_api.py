# SPDX-License-Identifier: GPL-3.0-or-later
"""API integration tests — FastAPI TestClient, mocked printer."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "template_count" in data
    assert data["template_count"] >= 1


def test_capabilities_response(client: TestClient) -> None:
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert "supported_labels" in data
    assert "dpi" in data
    assert isinstance(data["supported_labels"], list)


def test_list_templates(client: TestClient) -> None:
    resp = client.get("/templates")
    assert resp.status_code == 200
    templates = resp.json()
    assert isinstance(templates, list)
    assert len(templates) >= 1
    t = templates[0]
    assert "name" in t
    assert "fields" in t
    assert "required" in t["fields"]


def test_list_templates_includes_continuous_media(client: TestClient) -> None:
    """Each template carries its required media (Step 6) so the UI can badge compatibility.

    The fixture templates use the continuous ``62`` label → 62mm continuous, no discrete length."""
    resp = client.get("/templates")
    assert resp.status_code == 200
    by_name = {t["name"]: t for t in resp.json()}
    media = by_name["simple"]["media"]
    assert media == {"width_mm": 62.0, "media_type": "continuous", "length_mm": None}


def test_list_templates_die_cut_media_carries_length(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A die-cut template (62x29) exposes a die-cut media with the label length."""
    import app.main as main_mod

    _write_label_template(main_mod, "diecut", "62x29")
    resp = client.get("/templates")
    by_name = {t["name"]: t for t in resp.json()}
    assert by_name["diecut"]["media"] == {
        "width_mm": 62.0,
        "media_type": "die_cut",
        "length_mm": 29.0,
    }


def test_index_embeds_template_media(client: TestClient) -> None:
    """The index route serialises each template's media into the inline TEMPLATES JSON (Step 6),
    so the page can compare it against GET /printer/status client-side without another round-trip."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert '"media"' in resp.text
    assert '"media_type": "continuous"' in resp.text or '"media_type":"continuous"' in resp.text


def test_preview_returns_png(client: TestClient) -> None:
    resp = client.post("/preview", json={"template": "simple", "fields": {"title": "Hello"}})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    # Should be a decodable PNG
    img = Image.open(io.BytesIO(resp.content))
    assert img.width > 0
    assert img.height > 0


def test_preview_download_sets_attachment(client: TestClient) -> None:
    resp = client.post(
        "/preview?download=true", json={"template": "simple", "fields": {"title": "Hi"}}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"] == 'attachment; filename="simple.png"'


def test_preview_without_download_has_no_attachment(client: TestClient) -> None:
    resp = client.post("/preview", json={"template": "simple", "fields": {"title": "Hi"}})
    assert resp.status_code == 200
    assert "content-disposition" not in resp.headers


def test_preview_template_not_found(client: TestClient) -> None:
    resp = client.post("/preview", json={"template": "ghost-template", "fields": {}})
    assert resp.status_code == 404


def test_preview_missing_template_422(client: TestClient) -> None:
    """Omitting `template` on /preview is a 422 — the field is required, no discovery fallback."""
    resp = client.post("/preview", json={"fields": {"totally_unknown_field": "x"}})
    assert resp.status_code == 422


def test_print_dry_run(client: TestClient) -> None:
    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "Dry Run Test"}, "dry_run": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["template"] == "simple"
    assert "job_id" in data


def test_print_calls_driver(client: TestClient) -> None:
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "Real Print"}, "dry_run": False},
    )
    assert resp.status_code == 200
    main_mod._driver.render_payload.assert_called()


def test_reprint_existing_job(client: TestClient) -> None:
    # First print to create a job
    resp1 = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "Original"}, "dry_run": True},
    )
    assert resp1.status_code == 200
    job_id = resp1.json()["job_id"]

    resp2 = client.post(f"/reprint/{job_id}")
    assert resp2.status_code == 200
    assert resp2.json()["template"] == "simple"


def test_reprint_missing_job(client: TestClient) -> None:
    resp = client.post("/reprint/nonexistent-job-id-xyz")
    assert resp.status_code == 404


def test_reload_endpoint(client: TestClient) -> None:
    resp = client.post("/reload")
    assert resp.status_code == 200
    data = resp.json()
    assert "loaded" in data


def test_reload_reports_malformed_template_file(client: TestClient) -> None:
    """A broken YAML file must surface as 422 with its error, not a misleading 200 success."""
    import app.main as main_mod

    (main_mod.registry.templates_dir / "broken.yaml").write_text("name: [unclosed")
    resp = client.post("/reload")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("broken.yaml" in err for err in detail["errors"])
    # The valid templates still loaded — a bad sibling file doesn't take them down.
    assert "simple" in detail["loaded"]


def test_reload_reports_missing_default_language(client: TestClient) -> None:
    """If reload would drop the default-language catalog, that is a reported failure."""
    import app.main as main_mod

    (main_mod.translator.translations_dir / "en.yaml").unlink()
    resp = client.post("/reload")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("default language" in err for err in detail["errors"])


def test_metrics_endpoint(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "labels_printed_total" in resp.text


def test_health_reports_languages(client: TestClient) -> None:
    data = client.get("/health").json()
    assert data["default_language"] == "en"
    assert {"en", "es"} <= set(data["languages"])


def test_language_override_changes_render(client: TestClient) -> None:
    body = {"template": "chrome-test", "fields": {"contents": "x"}}
    resp_en = client.post("/preview", json={**body, "language": "en"})
    resp_es = client.post("/preview", json={**body, "language": "es"})
    assert resp_en.status_code == 200
    assert resp_es.status_code == 200
    assert resp_en.content != resp_es.content  # "Frozen" vs "Congelado"


def test_default_language_used_when_omitted(client: TestClient) -> None:
    body = {"template": "chrome-test", "fields": {"contents": "x"}}
    default = client.post("/preview", json=body)
    explicit_en = client.post("/preview", json={**body, "language": "en"})
    assert default.content == explicit_en.content  # default_language is "en"


def test_print_persists_language_and_reprint_reuses_it(client: TestClient) -> None:
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "chrome-test",
            "fields": {"contents": "x"},
            "language": "es",
            "dry_run": True,
        },
    )
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None
    assert record.language == "es"

    resp2 = client.post(f"/reprint/{job_id}")
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_startup_rejects_missing_default_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DEFAULT_LANGUAGE with no catalog must fail fast at startup."""
    import app.main as main_mod

    empty_translations = tmp_path / "translations"
    empty_translations.mkdir()
    monkeypatch.setattr(main_mod.settings, "data_dir", tmp_path)
    monkeypatch.setattr(main_mod.settings, "default_language", "en")
    monkeypatch.setattr(main_mod.translator, "translations_dir", empty_translations)
    monkeypatch.setattr(main_mod.translator, "default_language", "en")
    with pytest.raises(RuntimeError, match="no catalog"):
        await main_mod.startup()


def test_web_ui_renders(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Labelito" in resp.text


def test_api_token_enforced(tmp_path, monkeypatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "secret123")

    resp = client_for(main_mod).post("/print", json={"fields": {}})
    # No token → 401
    assert resp.status_code == 401

    monkeypatch.setattr(main_mod.settings, "api_token", None)


def client_for(main_mod) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(main_mod.app)


def test_api_token_valid_passes(client: TestClient, monkeypatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "valid-token")
    try:
        resp = client.post(
            "/preview",
            json={"template": "simple", "fields": {"title": "Secured"}},
            headers={"Authorization": "Bearer valid-token"},
        )
        assert resp.status_code == 200
    finally:
        monkeypatch.setattr(main_mod.settings, "api_token", None)


@pytest.mark.asyncio
async def test_startup_rejects_unsupported_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsupported LABEL_SIZE for the configured model must fail fast at startup."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "data_dir", tmp_path)
    monkeypatch.setattr(main_mod.settings, "label_size", "999-not-a-real-size")
    with pytest.raises(RuntimeError, match="not supported by model"):
        await main_mod.startup()


@pytest.mark.asyncio
async def test_startup_rejects_invalid_network_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed PRINTER_URI for the network transport must fail at boot, not on first print."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "data_dir", tmp_path)
    monkeypatch.setattr(main_mod.settings, "allow_unauthenticated", True)
    # tcp:// infers the network transport; the missing port is the malformed part.
    monkeypatch.setattr(main_mod.settings, "printer_uri", "tcp://192.168.1.55")  # no port
    with pytest.raises(ValueError, match="Invalid network printer URI"):
        await main_mod.startup()


# ── Auth: fail closed unless explicitly opted out ────────────────────────────────
def test_auth_fails_closed_without_token_or_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", None)
    monkeypatch.setattr(main_mod.settings, "allow_unauthenticated", False)
    with pytest.raises(RuntimeError, match="ALLOW_UNAUTHENTICATED"):
        main_mod._require_auth_or_optout()


def test_auth_allows_explicit_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", None)
    monkeypatch.setattr(main_mod.settings, "allow_unauthenticated", True)
    main_mod._require_auth_or_optout()  # must not raise


def test_auth_allows_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "secret")
    monkeypatch.setattr(main_mod.settings, "allow_unauthenticated", False)
    main_mod._require_auth_or_optout()  # must not raise


def test_auth_rejects_empty_or_blank_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "   ")
    monkeypatch.setattr(main_mod.settings, "allow_unauthenticated", False)
    with pytest.raises(RuntimeError, match="empty"):
        main_mod._require_auth_or_optout()


# ── Reprint: reproduces the original label's frozen instant ──────────────────────
def test_reprint_reproduces_render_instant(client: TestClient) -> None:
    import app.main as main_mod

    resp1 = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    record1 = main_mod._load_job(resp1.json()["job_id"])
    assert record1 is not None
    assert record1.render_now is not None

    resp2 = client.post(f"/reprint/{resp1.json()['job_id']}")
    assert resp2.status_code == 200
    record2 = main_mod._load_job(resp2.json()["job_id"])
    assert record2 is not None
    # Same frozen instant → computed {{date}}/{{now}} tokens reproduce identically.
    assert record2.render_now == record1.render_now


# ── Idempotency: an opt-in key de-duplicates retries ─────────────────────────────
def test_idempotency_key_dedupes_retry(client: TestClient) -> None:
    """A repeated key returns the original job — no second label, nothing re-sent."""
    import app.main as main_mod

    body = {
        "template": "simple",
        "fields": {"title": "X"},
        "dry_run": False,
        "idempotency_key": "k1",
    }
    r1 = client.post("/print", json=body)
    assert r1.status_code == 200
    sends = main_mod._driver.render_payload.call_count

    r2 = client.post("/print", json=body)
    assert r2.status_code == 200
    assert r2.json()["job_id"] == r1.json()["job_id"]  # original job returned
    assert main_mod._driver.render_payload.call_count == sends  # nothing re-sent


def test_concurrent_same_key_prints_once(client: TestClient) -> None:
    """Two same-key requests racing in together must produce exactly one physical send.

    The idempotency check lives inside ``_print_lock``; whichever request loses the race acquires
    the lock only after the winner has appended its history record, so it returns that job rather
    than printing a duplicate. Driven at the coroutine level via ``asyncio.gather`` so the race is
    exercised deterministically (one task parks in the threadpool send while the other reaches the
    lock), without TestClient's single-portal threading.
    """
    import asyncio

    import app.main as main_mod
    from app.models import PrintRequest, PrintResponse

    req = PrintRequest(template="simple", fields={"title": "X"}, idempotency_key="race-1")

    async def _both() -> list[PrintResponse]:
        return await asyncio.gather(main_mod.print_label(req), main_mod.print_label(req))

    r1, r2 = asyncio.run(_both())
    assert r1.job_id == r2.job_id  # the loser returns the winner's job
    assert main_mod._driver.render_payload.call_count == 1  # only one label hit the printer


def test_no_idempotency_key_allows_intentional_duplicate(client: TestClient) -> None:
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post("/print", json=body)
    r2 = client.post("/print", json=body)
    assert r1.json()["job_id"] != r2.json()["job_id"]  # each is a distinct, real print


def test_idempotency_retry_after_failure_still_prints(client: TestClient) -> None:
    """A failed attempt must not be de-duplicated — retrying its key prints for real."""
    import app.main as main_mod

    body = {
        "template": "simple",
        "fields": {"title": "X"},
        "dry_run": False,
        "idempotency_key": "k2",
    }
    main_mod._driver.render_payload.side_effect = RuntimeError("printer offline")
    assert client.post("/print", json=body).status_code == 500

    main_mod._driver.render_payload.side_effect = None
    assert client.post("/print", json=body).status_code == 200


def test_idempotency_key_reused_with_different_request_is_rejected(client: TestClient) -> None:
    """Reusing a key for a different label must 409, not silently return the old job."""
    base = {"fields": {"title": "X"}, "dry_run": False, "idempotency_key": "k3"}
    assert client.post("/print", json={"template": "simple", **base}).status_code == 200

    # Same key, different fields → not a retry. Must be rejected, not deduped to the old job.
    changed = {"template": "simple", "fields": {"title": "Y"}, "dry_run": False}
    resp = client.post("/print", json={**changed, "idempotency_key": "k3"})
    assert resp.status_code == 409


def test_idempotency_dry_run_then_real_print_is_rejected(client: TestClient) -> None:
    """A dry-run keyed job must not satisfy a later real print under the same key.

    Without dry_run in the fingerprint this would 200 with the dry-run job and print nothing —
    silent data loss. The mismatch must surface as a 409 instead.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "idempotency_key": "k4"}
    assert client.post("/print", json={**body, "dry_run": True}).status_code == 200
    assert client.post("/print", json={**body, "dry_run": False}).status_code == 409


# ── RenderOptions group: fingerprint hashes the whole `options` object ────────────
def test_idempotency_key_reused_with_different_option_is_rejected(client: TestClient) -> None:
    """Reusing a key with a different rasterization option is a different print → 409.

    The fingerprint hashes ``options.model_dump()`` wholesale, so any option in the RenderOptions
    group distinguishes two prints automatically — no per-option line to forget. Here the only
    difference is the dither option; the second request must not dedupe to the first.
    """
    body = {"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    r1 = client.post(
        "/print", json={**body, "idempotency_key": "opt1", "options": {"dither": False}}
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/print", json={**body, "idempotency_key": "opt1", "options": {"dither": True}}
    )
    assert r2.status_code == 409


def test_reprint_replays_resolved_options(client: TestClient) -> None:
    """A job stores its resolved RenderOptions, and /reprint replays that exact object.

    With DEFAULT_DITHER=true and an omitted option, the resolved value frozen into history is True;
    the reprint must hand the driver dither=True even though the request never said so.
    """
    import app.main as main_mod

    monkeypatch_default = main_mod.settings.default_dither
    try:
        main_mod.settings.default_dither = True
        resp = client.post(
            "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
        )
        job_id = resp.json()["job_id"]
        record = main_mod._load_job(job_id)
        assert record is not None and record.options.dither is True  # resolved value frozen

        reprint = client.post(f"/reprint/{job_id}")
        assert reprint.status_code == 200
        args, _ = main_mod._driver.render_payload.call_args
        _png, opts = args
        assert opts["dither"] is True  # replayed from the frozen options, not re-resolved
    finally:
        main_mod.settings.default_dither = monkeypatch_default


# ── Rotation applied once, by the driver, on a printable-width raster ─────────────
def test_print_sends_printable_width_raster_and_driver_rotates(client: TestClient) -> None:
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "rotated", "fields": {"title": "Hi"}, "dry_run": False}
    )
    assert resp.status_code == 200

    args, _ = main_mod._driver.render_payload.call_args
    png_bytes, opts = args
    # The print raster is rendered UNrotated at the roll printable width; the driver rotates it
    # (brother_ql needs the printable width to rasterize continuous labels correctly).
    assert opts["rotate"] == 90, "driver applies the template rotation"
    img = Image.open(io.BytesIO(png_bytes))
    assert img.width == 696, "raster handed to driver is at printable width, not pre-rotated"


# ── Print history is status-aware: failed jobs are recorded but not reprintable ───
def test_successful_print_records_printed_status(client: TestClient) -> None:
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None and record.status == "printed"


def test_dry_run_records_dry_run_status(client: TestClient) -> None:
    import app.main as main_mod

    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": True}
    )
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None and record.status == "dry-run"


def test_failed_print_is_recorded_and_not_reprintable(client: TestClient) -> None:
    import app.main as main_mod

    main_mod._driver.render_payload.side_effect = RuntimeError("printer offline")
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 500
    main_mod._driver.render_payload.side_effect = None

    failed = [r for r in main_mod._history.recent(50) if r.status == "failed"]
    assert len(failed) == 1

    reprint = client.post(f"/reprint/{failed[0].job_id}")
    assert reprint.status_code == 409  # a failed job must not be replayable


# ── Image fields reach the renderer (regression: dropped before rendering) ────────
def test_multipart_preview_renders_uploaded_image(client: TestClient) -> None:
    src = Image.new("L", (80, 80), 0)  # solid black square
    buf = io.BytesIO()
    src.save(buf, format="PNG")

    resp = client.post(
        "/preview/multipart",
        data={"template": "image-test", "fields_json": "{}"},
        files={"image": ("upload.png", buf.getvalue(), "image/png")},
    )
    assert resp.status_code == 200
    out = Image.open(io.BytesIO(resp.content)).convert("L")
    assert out.getextrema()[0] < 128, "uploaded image must render as nonblank pixels"


def test_multipart_blank_template_rejected(client: TestClient) -> None:
    """A blank multipart template is a clean 422 (min_length=1), not an unhandled 500."""
    resp = client.post("/preview/multipart", data={"template": "", "fields_json": "{}"})
    assert resp.status_code == 422


def test_multipart_missing_template_rejected(client: TestClient) -> None:
    """`template` is a required form field — omitting it is a 422, not a discovery fallback."""
    resp = client.post("/preview/multipart", data={"fields_json": "{}"})
    assert resp.status_code == 422


def test_multipart_rejects_non_image_content_type(client: TestClient) -> None:
    resp = client.post(
        "/preview/multipart",
        data={"template": "image-test", "fields_json": "{}"},
        files={"image": ("note.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 415


def test_multipart_rejects_oversized_upload(client: TestClient) -> None:
    import app.main as main_mod

    big = b"\x00" * (main_mod.MAX_IMAGE_UPLOAD_BYTES + 1)
    resp = client.post(
        "/preview/multipart",
        data={"template": "image-test", "fields_json": "{}"},
        files={"image": ("big.png", big, "image/png")},
    )
    assert resp.status_code == 413


def test_multipart_rejects_invalid_image(client: TestClient) -> None:
    resp = client.post(
        "/preview/multipart",
        data={"template": "image-test", "fields_json": "{}"},
        files={"image": ("x.png", b"definitely not a PNG", "image/png")},
    )
    assert resp.status_code == 422


def test_multipart_rejects_too_many_pixels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "MAX_IMAGE_PIXELS", 100)  # 80x80 = 6400 px exceeds this
    src = Image.new("L", (80, 80), 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    resp = client.post(
        "/preview/multipart",
        data={"template": "image-test", "fields_json": "{}"},
        files={"image": ("x.png", buf.getvalue(), "image/png")},
    )
    assert resp.status_code == 413


# ── JSON base64 image fields hit the same caps as multipart uploads ──────────────
def test_json_image_renders(client: TestClient) -> None:
    src = Image.new("L", (80, 80), 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    resp = client.post("/preview", json={"template": "image-test", "fields": {"image": b64}})
    assert resp.status_code == 200


def test_json_image_oversized_rejected_before_decode(client: TestClient) -> None:
    """An oversized base64 field is rejected by encoded length, before it is decoded."""
    import app.main as main_mod

    # Comfortably past the char bound so the pre-decode guard fires (not the post-decode check).
    oversized = "A" * (main_mod.MAX_IMAGE_B64_CHARS + 4)
    resp = client.post("/preview", json={"template": "image-test", "fields": {"image": oversized}})
    assert resp.status_code == 413


def test_json_image_invalid_base64_rejected(client: TestClient) -> None:
    resp = client.post(
        "/preview", json={"template": "image-test", "fields": {"image": "@@not-base64@@"}}
    )
    assert resp.status_code == 422


def test_json_image_invalid_bytes_rejected(client: TestClient) -> None:
    not_an_image = base64.b64encode(b"definitely not a PNG").decode()
    resp = client.post(
        "/preview", json={"template": "image-test", "fields": {"image": not_an_image}}
    )
    assert resp.status_code == 422


def test_json_image_too_many_pixels_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "MAX_IMAGE_PIXELS", 100)  # 80x80 = 6400 px exceeds this
    src = Image.new("L", (80, 80), 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    resp = client.post(
        "/print", json={"template": "image-test", "fields": {"image": b64}, "dry_run": True}
    )
    assert resp.status_code == 413


def test_json_custom_image_field_is_capped(client: TestClient) -> None:
    """A template reading its image from a non-default field must still hit the size cap."""
    import app.main as main_mod

    big = base64.b64encode(b"\x00" * (main_mod.MAX_IMAGE_UPLOAD_BYTES + 1)).decode()
    resp = client.post("/preview", json={"template": "custom-image", "fields": {"photo": big}})
    assert resp.status_code == 413


def test_oversized_text_field_rejected_before_render(client: TestClient) -> None:
    """A pathologically long text field must 413 before it allocates a giant render buffer."""
    import app.main as main_mod

    huge = "A" * (main_mod.MAX_TEXT_FIELD_CHARS + 1)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": huge}, "dry_run": True}
    )
    assert resp.status_code == 413


def test_oversized_text_field_rejected_on_preview(client: TestClient) -> None:
    import app.main as main_mod

    huge = "A" * (main_mod.MAX_TEXT_FIELD_CHARS + 1)
    resp = client.post("/preview", json={"template": "simple", "fields": {"title": huge}})
    assert resp.status_code == 413


def test_normal_length_text_field_still_renders(client: TestClient) -> None:
    """The cap is generous — a realistic multi-line label must still print."""
    ok = "Line of label text. " * 10  # ~200 chars, well under the cap
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": ok}, "dry_run": True}
    )
    assert resp.status_code == 200


def test_oversized_nonstring_field_rejected_before_render(client: TestClient) -> None:
    """A huge number stringifies long; the cap must apply to str(value), not just str fields."""
    import app.main as main_mod

    huge_number = int("9" * (main_mod.MAX_TEXT_FIELD_CHARS + 1))
    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": huge_number}, "dry_run": True},
    )
    assert resp.status_code == 413


def test_collection_field_value_rejected(client: TestClient) -> None:
    """A list/object field is nonsensical for a label and must 422, not be stringified."""
    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": ["a", "b", "c"]}, "dry_run": True},
    )
    assert resp.status_code == 422


def test_explicit_template_missing_required_field_rejected(client: TestClient) -> None:
    """Naming a template directly must still 422 on a missing required field, not print blank."""
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"subtitle": "x"}, "dry_run": True}
    )
    assert resp.status_code == 422


def test_explicit_template_blank_required_field_rejected(client: TestClient) -> None:
    """A present-but-blank required field is treated as missing (would print an empty label)."""
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "   "}, "dry_run": True}
    )
    assert resp.status_code == 422


def test_template_is_required(client: TestClient) -> None:
    """Omitting `template` is rejected (422) — there is no field-based auto-discovery fallback."""
    resp = client.post("/print", json={"fields": {"title": "x"}, "dry_run": True})
    assert resp.status_code == 422


def test_blank_template_rejected(client: TestClient) -> None:
    """An empty/blank template name is rejected by the min_length=1 constraint (422)."""
    resp = client.post("/print", json={"template": "", "fields": {"title": "x"}, "dry_run": True})
    assert resp.status_code == 422


def test_unknown_template_not_found(client: TestClient) -> None:
    """A named template that does not exist is a 404, not a silent fallback."""
    resp = client.post(
        "/print", json={"template": "ghost", "fields": {"title": "x"}, "dry_run": True}
    )
    assert resp.status_code == 404


def test_too_many_fields_rejected(client: TestClient) -> None:
    import app.main as main_mod

    fields = {"title": "x"}  # satisfy the required-field check so the count cap is what trips
    fields.update({f"f{i}": "x" for i in range(main_mod.MAX_FIELD_COUNT)})
    assert len(fields) > main_mod.MAX_FIELD_COUNT
    resp = client.post("/print", json={"template": "simple", "fields": fields, "dry_run": True})
    assert resp.status_code == 413


def test_oversized_field_name_rejected(client: TestClient) -> None:
    import app.main as main_mod

    long_name = "n" * (main_mod.MAX_FIELD_NAME_CHARS + 1)
    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "x", long_name: "y"}, "dry_run": True},
    )
    assert resp.status_code == 413


def test_oversized_idempotency_key_rejected(client: TestClient) -> None:
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "x"},
            "dry_run": True,
            "idempotency_key": "k" * 201,
        },
    )
    assert resp.status_code == 422


def test_oversized_request_body_rejected_by_content_length(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body over the cap is rejected by Content-Length before it is parsed."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "MAX_REQUEST_BODY_BYTES", 100)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X" * 300}, "dry_run": True}
    )
    assert resp.status_code == 413


def test_chunked_body_without_content_length_rejected(client: TestClient) -> None:
    """A chunked POST (no Content-Length) must be rejected (411), not slip past the size guard."""

    def _chunks() -> object:
        yield b'{"template": "simple", "fields": {"title": "X"}, "dry_run": true}'

    # Passing an iterator as content makes httpx stream it chunked, with no Content-Length header.
    resp = client.post("/print", content=_chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 411


def test_unknown_top_level_field_rejected(client: TestClient) -> None:
    """A misspelled/unknown top-level option must 422, not be silently ignored."""
    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "X"}, "bogus_field": True, "dry_run": True},
    )
    assert resp.status_code == 422


def test_image_job_strips_blob_from_history(client: TestClient) -> None:
    """An image field must not be persisted in history (it would bloat the file)."""
    import app.main as main_mod

    src = Image.new("L", (80, 80), 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    resp = client.post(
        "/print", json={"template": "image-test", "fields": {"image": b64}, "dry_run": True}
    )
    assert resp.status_code == 200
    record = main_mod._load_job(resp.json()["job_id"])
    assert record is not None
    assert record.image_stripped is True
    assert "image" not in record.fields  # the ~KB+ blob is not retained


def test_reprint_image_job_rejected(client: TestClient) -> None:
    """Reprinting an image job must 409 — the blob was not retained to reproduce it."""
    import app.main as main_mod

    src = Image.new("L", (80, 80), 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    resp = client.post(
        "/print", json={"template": "image-test", "fields": {"image": b64}, "dry_run": True}
    )
    job_id = resp.json()["job_id"]
    assert main_mod._load_job(job_id) is not None

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 409


# ── Image fields nested inside a row container share every image safeguard ────────
def _png_b64(size: tuple[int, int] = (80, 80)) -> str:
    src = Image.new("L", size, 0)
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_row_nested_image_renders(client: TestClient) -> None:
    """An image field declared inside a row child is recognized and rendered.

    The base64 blob is far longer than the plain-text field cap; if the row-nested image were not
    discovered as an image field it would be rejected by the text-field guard before rendering.
    """
    resp = client.post(
        "/preview",
        json={"template": "row-image", "fields": {"title": "Sample", "photo": _png_b64()}},
    )
    assert resp.status_code == 200


def test_row_nested_image_field_is_capped(client: TestClient) -> None:
    """The upload size cap must reach an image field nested inside a row child."""
    import app.main as main_mod

    big = base64.b64encode(b"\x00" * (main_mod.MAX_IMAGE_UPLOAD_BYTES + 1)).decode()
    resp = client.post("/preview", json={"template": "row-image", "fields": {"photo": big}})
    assert resp.status_code == 413


def test_row_nested_image_job_strips_blob_from_history(client: TestClient) -> None:
    """A row-nested image blob must be stripped from history and block reprint, like a top-level one."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={
            "template": "row-image",
            "fields": {"title": "Sample", "photo": _png_b64()},
            "dry_run": True,
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None
    assert record.image_stripped is True
    assert "photo" not in record.fields

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 409


def _save_legacy_record(client: TestClient, template: str, fields: dict) -> str:
    """Persist a history row directly, simulating one written before the field validators existed.

    image_stripped defaults False and the fields skip /print's checks — exactly the durable
    pre-upgrade state a reprint must re-guard against.
    """
    import app.main as main_mod
    from app.models import PrintJobRecord

    job_id = "legacy-job-1"
    main_mod._history.save(
        PrintJobRecord(
            job_id=job_id,
            template=template,
            fields=fields,
            copies=1,
            dry_run=True,
            timestamp="2026-01-01T00:00:00",
            status="printed",
        )
    )
    return job_id


def test_reprint_legacy_image_record_rejected(client: TestClient) -> None:
    """A pre-validator history row still carrying an image value must 409 on reprint, not render."""
    b64 = _png_b64()
    job_id = _save_legacy_record(client, "row-image", {"title": "Sample", "photo": b64})
    resp = client.post(f"/reprint/{job_id}")
    assert resp.status_code == 409


def test_reprint_record_missing_required_field_rejected(client: TestClient) -> None:
    """A saved row that no longer satisfies the template's required fields must 409, not print blank.

    Simulates schema drift (template gained a required field) / a legacy row lacking one: rendering
    would substitute "" and emit a blank required label while reporting success.
    """
    # "simple" requires `title`; persist a record that lacks it.
    job_id = _save_legacy_record(client, "simple", {"subtitle": "only optional"})
    resp = client.post(f"/reprint/{job_id}")
    assert resp.status_code == 409


def test_reprint_legacy_oversized_text_record_rejected(client: TestClient) -> None:
    """A pre-validator history row with oversized text must 413 on reprint, not reach the renderer."""
    import app.main as main_mod

    huge = "A" * (main_mod.MAX_TEXT_FIELD_CHARS + 1)
    job_id = _save_legacy_record(client, "simple", {"title": huge})
    resp = client.post(f"/reprint/{job_id}")
    assert resp.status_code == 413


def test_image_field_list_value_rejected(client: TestClient) -> None:
    """A non-string value for a top-level image field must 422, not crash in base64 decode."""
    resp = client.post("/preview", json={"template": "image-test", "fields": {"image": ["abc"]}})
    assert resp.status_code == 422


def test_row_nested_image_field_list_value_rejected(client: TestClient) -> None:
    """A non-string value for a row-nested image field must 422, not reach the renderer as a 500."""
    resp = client.post("/preview", json={"template": "row-image", "fields": {"photo": ["abc"]}})
    assert resp.status_code == 422


def test_history_write_failure_after_send_still_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed history append must not turn a successful physical print into an error.

    Reporting failure would invite a client retry and a duplicate label; the printer is the
    source of truth, so the request succeeds and the lost record is logged.
    """
    import app.main as main_mod

    def boom(record: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(main_mod._history, "save", boom)
    resp = client.post(
        "/print", json={"template": "simple", "fields": {"title": "X"}, "dry_run": False}
    )
    assert resp.status_code == 200


# ── History browse endpoints ─────────────────────────────────────────────────────


def _seed_jobs(client: TestClient, n: int) -> list[str]:
    """Print n distinct jobs (newest last) and return their job_ids in print order."""
    ids = []
    for i in range(n):
        resp = client.post(
            "/print",
            json={"template": "simple", "fields": {"title": f"Job {i}"}, "dry_run": True},
        )
        assert resp.status_code == 200
        ids.append(resp.json()["job_id"])
    return ids


def test_history_list_pagination(client: TestClient) -> None:
    ids = _seed_jobs(client, 5)

    first = client.get("/history/list?offset=0&limit=2")
    assert first.status_code == 200
    data = first.json()
    assert data["total"] == 5
    assert data["offset"] == 0 and data["limit"] == 2
    assert len(data["entries"]) == 2
    # Newest first: the last two printed lead.
    assert [e["job_id"] for e in data["entries"]] == [ids[4], ids[3]]

    second = client.get("/history/list?offset=2&limit=2")
    assert [e["job_id"] for e in second.json()["entries"]] == [ids[2], ids[1]]


def test_history_list_default_and_limit_bounds(client: TestClient) -> None:
    _seed_jobs(client, 1)
    # Defaults apply when params are omitted.
    assert client.get("/history/list").json()["limit"] == 20
    # limit over the ceiling is rejected by FastAPI validation.
    assert client.get("/history/list?limit=1000").status_code == 422
    assert client.get("/history/list?offset=-1").status_code == 422
    # An offset past SQLite's bindable int64 range is a controlled 422, not a 500.
    assert client.get("/history/list?offset=9223372036854775808").status_code == 422


def test_history_delete_removes_entry(client: TestClient) -> None:
    import app.main as main_mod

    ids = _seed_jobs(client, 2)
    target = ids[0]
    assert main_mod._load_job(target) is not None

    resp = client.delete(f"/history/{target}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert main_mod._load_job(target) is None

    # Second delete of the same id → 404.
    assert client.delete(f"/history/{target}").status_code == 404
    # The other entry is untouched.
    assert main_mod._load_job(ids[1]) is not None


def test_reprint_from_history_appends_new_entry(client: TestClient) -> None:
    [job_id] = _seed_jobs(client, 1)
    before = client.get("/history/list").json()["total"]

    reprint = client.post(f"/reprint/{job_id}")
    assert reprint.status_code == 200

    after = client.get("/history/list").json()
    assert after["total"] == before + 1
    assert after["entries"][0]["job_id"] == reprint.json()["job_id"]  # newest first


def test_history_page_renders(client: TestClient) -> None:
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "History" in resp.text


def test_index_shows_history_link_by_default(client: TestClient) -> None:
    assert 'href="/history"' in client.get("/").text


def test_history_endpoints_require_token(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "secret123")
    try:
        # Data + mutation routes require the token...
        assert client.get("/history/list").status_code == 401
        assert client.delete("/history/whatever").status_code == 401
        # ...but the HTML shell is public (no data; must load so the browser can enter the token),
        # mirroring GET /.
        assert client.get("/history").status_code == 200
    finally:
        monkeypatch.setattr(main_mod.settings, "api_token", None)


def test_history_ui_gate_precedes_auth(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """HISTORY_UI=false must 404 (route appears absent), not 401, even when a token is required."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "secret123")
    monkeypatch.setattr(main_mod.settings, "history_ui", False)
    try:
        for resp in (
            client.get("/history"),
            client.get("/history/list"),
            client.delete("/history/whatever"),
        ):
            assert resp.status_code == 404
            # The 404 must not disclose that a hidden history UI exists — generic body only.
            assert "history" not in resp.text.lower()
    finally:
        monkeypatch.setattr(main_mod.settings, "api_token", None)


def test_history_delete_storage_error_is_500_not_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend failure on delete must surface as 500, never a misleading 404 (which would imply
    the privacy-facing deletion succeeded / the row was absent)."""
    import sqlite3

    import app.main as main_mod

    [job_id] = _seed_jobs(client, 1)

    def boom(_job_id: str) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main_mod._history, "delete", boom)
    assert client.delete(f"/history/{job_id}").status_code == 500
    # The row was not actually removed.
    assert main_mod._load_job(job_id) is not None


def test_history_ui_toggle_off_hides_browse_but_keeps_reprint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_mod

    [job_id] = _seed_jobs(client, 1)  # record exists in the store regardless of the UI flag

    monkeypatch.setattr(main_mod.settings, "history_ui", False)

    assert client.get("/history").status_code == 404
    assert client.get("/history/list").status_code == 404
    assert client.delete(f"/history/{job_id}").status_code == 404
    # Reprint-by-id survives the browse UI being off.
    assert client.post(f"/reprint/{job_id}").status_code == 200
    # The print page drops the History link.
    assert 'href="/history"' not in client.get("/").text


# ── GET /printer/status endpoint ─────────────────────────────────────────────────────────────────


def test_printer_status_happy_path_with_reachable_transport(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport whose query_status returns reachable=True yields a 200 with the full JSON shape:
    reachable, model, media fields, errors, status, phase. Simulates a networked printer that replies."""
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    class _ReachableTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            return None

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus(
                ok=True,
                errors=[],
                raw={},
                model="QL-800",
                media_width_mm=62,
                media_length_mm=0,
                media_type="Continuous length tape",
                status_type="Reply to status request",
                phase_type="Waiting to receive",
                reachable=True,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _ReachableTransport)

    resp = client.get("/printer/status")

    assert resp.status_code == 200, f"expected 200 from reachable transport; got {resp.status_code}"
    data = resp.json()
    assert data["state"] == "idle", "reachable + no errors must derive state=idle"
    assert data["uri"] == main_mod.settings.printer_uri, "response must echo the configured URI"
    assert data["reachable"] is True
    assert data["model"] == "QL-800"
    assert data["media_width_mm"] == 62
    assert data["media_length_mm"] == 0
    assert data["media_type"] == "Continuous length tape"
    assert data["status"] == "Reply to status request"
    assert data["phase"] == "Waiting to receive"
    assert data["errors"] == []


def test_printer_status_file_transport_returns_503_not_a_real_printer(client: TestClient) -> None:
    """With a file:// transport (the client fixture default), /printer/status returns 503 with
    reachable=False at the TOP level — same body shape as the 200 response."""
    resp = client.get("/printer/status")

    assert resp.status_code == 503, (
        f"file:// transport has no printer; expected 503 got {resp.status_code}"
    )
    data = resp.json()
    assert "detail" not in data, f"503 body must not wrap fields under 'detail'; got {data!r}"
    assert data.get("reachable") is False, "file transport 503 must carry top-level reachable=False"
    assert data.get("state") == "off", "an unreachable/no-printer transport must derive state=off"
    assert data.get("uri"), "503 body must still carry the configured URI"


def test_printer_status_uses_check_token_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /printer/status follows the same auth posture as other protected endpoints: a missing
    token returns 401 when API_TOKEN is configured. Uses a reachable transport stub so the 401
    path is not shadowed by a 503 from the file transport."""
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    class _ReachableTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            return None

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus(ok=True, errors=[], raw={}, reachable=True)

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _ReachableTransport)
    monkeypatch.setattr(main_mod.settings, "api_token", "secret-token")

    resp = client.get("/printer/status")
    assert resp.status_code == 401, "a missing/wrong token must return 401 when API_TOKEN is set"

    authed = client.get("/printer/status", headers={"Authorization": "Bearer secret-token"})
    assert authed.status_code == 200, "a correct token must succeed"


def test_printer_status_unreachable_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the transport's query_status returns reachable=False (e.g. network printer
    unreachable), /printer/status returns 503 with reachable=False in the body."""
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    class _UnreachableTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            return None

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus.unreachable("test: simulated unreachable printer")

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _UnreachableTransport)

    resp = client.get("/printer/status")

    assert resp.status_code == 503, f"unreachable printer must return 503; got {resp.status_code}"
    data = resp.json()
    assert "detail" not in data, f"503 body must not wrap fields under 'detail'; got {data!r}"
    assert data.get("reachable") is False, (
        f"503 body must carry top-level reachable=false; got {data!r}"
    )
    assert data.get("state") == "off", "an unreachable printer must derive state=off"
    assert data.get("uri") == main_mod.settings.printer_uri, "503 body must echo the configured URI"


def test_printer_status_503_when_print_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a print is in progress (lock held), /printer/status returns 503 immediately rather
    than blocking the request behind the in-flight print."""
    import asyncio

    import app.main as main_mod

    # Manually acquire the print lock to simulate an in-progress print.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main_mod._print_lock.acquire())
        resp = client.get("/printer/status")
    finally:
        if main_mod._print_lock.locked():
            main_mod._print_lock.release()
        loop.close()

    assert resp.status_code == 503, (
        f"a held print lock must return 503 printer-busy; got {resp.status_code}"
    )
    data = resp.json()
    assert "detail" not in data, f"503 body must not wrap fields under 'detail'; got {data!r}"
    assert data.get("reachable") is False, (
        f"printer-busy 503 must carry top-level reachable=false in the body; got {data!r}"
    )
    assert data.get("state") == "printing", "a held print lock must derive state=printing"


def test_printer_status_reachable_with_errors_returns_error_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable printer reporting errors (e.g. out of media, cover open) returns 200 with
    state=error and the error strings echoed — distinct from the off (unreachable) state."""
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    class _ErrorTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            return None

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus(
                ok=False,
                errors=["Cover open", "No media"],
                raw={},
                model="QL-800",
                reachable=True,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _ErrorTransport)

    resp = client.get("/printer/status")

    assert resp.status_code == 200, (
        f"a reachable printer (even with errors) must return 200; got {resp.status_code}"
    )
    data = resp.json()
    assert data["state"] == "error", "reachable + errors must derive state=error"
    assert data["reachable"] is True
    assert data["uri"] == main_mod.settings.printer_uri
    assert data["errors"] == ["Cover open", "No media"]


# ── sequence spec / auto-numbering ───────────────────────────────────────────────


def test_sequence_basic_dry_run(client: TestClient) -> None:
    """A sequence spec on /print (dry_run) must succeed and record count in the history row."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": 3, "start": 1, "step": 1, "padding": 3},
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None
    assert record.sequence is not None, "Frozen sequence spec must be stored on the history record"
    assert record.sequence.count == 3
    assert record.sequence.start == 1
    assert record.sequence.padding == 3


def test_sequence_sends_one_label_at_a_time(client: TestClient) -> None:
    """A sequence print (non-dry-run) must drive the driver ONCE PER LABEL.

    The batch is no longer one atomic convert/send: each of the ``count`` labels is rendered,
    converted (copies=1), and sent individually so each gets its own per-label status confirmation.
    This asserts ``render_payload`` is called exactly ``count`` times, each with a single PNG and
    copies=1 (never a batched ``opts['pngs']`` stream).
    """
    import app.main as main_mod

    # Use a {{seq}} template so the biconditional guard passes and images are verifiably distinct.
    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 4, "start": 1, "step": 1, "padding": 2},
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    calls = main_mod._driver.render_payload.call_args_list
    assert len(calls) == 4, (
        f"Per-label send must call render_payload count=4 times, got {len(calls)}"
    )
    for png, opts in (c.args for c in calls):
        assert "pngs" not in opts, "Per-label send must NOT use the batched opts['pngs'] path"
        assert opts.get("copies") == 1, "Each sequence label is one printer job (copies=1)"
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "Each label is sent as a single valid PNG"


def test_sequence_each_label_is_distinct(client: TestClient) -> None:
    """Each per-label send must carry a distinct PNG (its own {{seq}} value).

    The seq template embeds {{seq}}, so the per-item render produces different bytes per label.
    """
    import textwrap

    import app.main as main_mod

    # Inject a seq template into the live registry
    seq_template_yaml = textwrap.dedent("""\
        name: seq-label
        description: Sequence numbering test
        label: "62"
        rotate: 0
        fields:
          required: []
          optional: []
        layout:
          - {type: text, text: "Item {{seq}}"}
    """)
    (main_mod.registry.templates_dir / "seq-label.yaml").write_text(seq_template_yaml)
    main_mod.registry.load_all()

    resp = client.post(
        "/print",
        json={
            "template": "seq-label",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 3, "start": 1, "step": 1, "padding": 3},
        },
    )
    assert resp.status_code == 200, f"Expected 200: {resp.text}"
    calls = main_mod._driver.render_payload.call_args_list
    assert len(calls) == 3, "One per-label send per item"
    pngs = [c.args[0] for c in calls]
    # Items differ because {{seq}} resolves to 001/002/003
    assert pngs[0] != pngs[1], "seq=001 and seq=002 must produce different images"
    assert pngs[1] != pngs[2], "seq=002 and seq=003 must produce different images"


def _seq_error_metric() -> float:
    import app.main as main_mod

    return main_mod.LABEL_ERRORS.labels(reason="printer_error")._value.get()


def test_sequence_stops_at_first_printer_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ok PrinterStatus on label k stops the batch at k.

    Simulate a transport that returns a not-ok status on the 2nd of 4 labels. The batch must:
    stop at label 2 (only 2 sends reach the transport), record the job ``failed``, emit
    ``label_errors_total{reason="printer_error"}`` once, and advance ``labels_printed_total`` by
    exactly k-1 = 1 (the one label sent OK before the failing one). The HTTP result is 502.
    """
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    _write_seq_template(main_mod)

    sends: dict[str, int] = {"count": 0}

    class _FailOnSecond:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            sends["count"] += 1
            if sends["count"] == 2:
                return PrinterStatus(ok=False, errors=["out of media"], raw={})
            return PrinterStatus.synthetic_ok()

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus.synthetic_ok()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _FailOnSecond)

    errors_before = _seq_error_metric()
    printed_before = _get_labels_printed("seq-guard", dry_run=False)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 4, "start": 1},
        },
    )
    assert resp.status_code == 502, f"Expected 502 on mid-batch printer error: {resp.text}"
    assert "out of media" in resp.text

    # The loop stopped at label 2: only labels 1 and 2 were sent (3 and 4 never reached).
    assert sends["count"] == 2, f"Batch must STOP at the failing label, got {sends['count']} sends"

    # Job recorded failed (one failed row).
    failed = [r for r in main_mod._history.recent(50) if r.status == "failed"]
    assert len(failed) == 1, "The failed batch must record exactly one failed history row"
    assert failed[0].sequence is not None and failed[0].sequence.count == 4

    # Error metric incremented exactly once.
    assert _seq_error_metric() == errors_before + 1, "printer_error must be emitted exactly once"

    # labels_printed_total advanced by k-1 = 1 (the one label actually sent before the failure).
    printed_after = _get_labels_printed("seq-guard", dry_run=False)
    assert printed_after == printed_before + 1, (
        f"labels_printed_total must advance by k-1=1 (labels actually sent); "
        f"got delta={printed_after - printed_before}"
    )


def test_sequence_clean_batch_advances_metric_by_count(client: TestClient) -> None:
    """A clean count=N non-dry-run batch advances labels_printed_total by N and records printed."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    count = 5
    before = _get_labels_printed("seq-guard", dry_run=False)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": count, "start": 1},
        },
    )
    assert resp.status_code == 200, f"Expected 200: {resp.text}"

    after = _get_labels_printed("seq-guard", dry_run=False)
    assert after == before + count, (
        f"A clean batch must advance labels_printed_total by count={count}; "
        f"got delta={after - before}"
    )
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.status == "printed"
    assert main_mod._driver.render_payload.call_count == count, "One send per label"


def test_sequence_none_status_does_not_fail_batch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None per-label status (state unknown: USB) must NOT fail the batch."""
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    _write_seq_template(main_mod)

    class _NoneStatus:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            return None  # transport cannot read state back

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus.synthetic_ok()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _NoneStatus)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 3, "start": 1},
        },
    )
    assert resp.status_code == 200, f"None status must be treated as no-error: {resp.text}"
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.status == "printed"


def test_sequence_renders_one_label_at_a_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory bound: the engine renders a sequence ONE label at a time, interleaved with sends.

    Proven by interleaving order: render N happens, then send N, then render N+1 — never all N
    renders up front. We spy on engine.render_to_png and the transport send and assert the event
    stream alternates render/send, so no whole-batch buffer of decoded images is built.
    """
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    _write_seq_template(main_mod)

    events: list[str] = []
    real_render_to_png = main_mod.engine.render_to_png

    def _spy_render(*args: object, **kwargs: object) -> bytes:
        events.append("render")
        return real_render_to_png(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(main_mod.engine, "render_to_png", _spy_render)

    class _RecordingTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            events.append("send")
            return PrinterStatus.synthetic_ok()

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus.synthetic_ok()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _RecordingTransport)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 3, "start": 1},
        },
    )
    assert resp.status_code == 200, f"Expected 200: {resp.text}"
    # Exactly one render per label, interleaved render→send→render→send… — never 3 renders then
    # 3 sends (which would mean the whole batch was buffered before any send).
    assert events == ["render", "send", "render", "send", "render", "send"], (
        f"Renders and sends must interleave one label at a time, got {events}"
    )


def test_sequence_dry_run_renders_but_does_not_send(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run renders each label (lazily) for validation but sends nothing (no driver/transport)."""
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    _write_seq_template(main_mod)

    sends: dict[str, int] = {"count": 0}

    class _CountingTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            sends["count"] += 1
            return PrinterStatus.synthetic_ok()

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus.synthetic_ok()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _CountingTransport)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": 4, "start": 1},
        },
    )
    assert resp.status_code == 200, f"Expected 200: {resp.text}"
    assert sends["count"] == 0, "dry_run must not send any label to the transport"
    assert main_mod._driver.render_payload.call_count == 0, (
        "dry_run must not convert via the driver"
    )


def test_sequence_and_copies_conflict_is_422(client: TestClient) -> None:
    """sequence + copies > 1 must be rejected with 422 (mutually exclusive)."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "copies": 3,
            "sequence": {"count": 5, "start": 1},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_sequence_copies_1_with_sequence_ok(client: TestClient) -> None:
    """sequence with copies=1 (the default) must succeed when the template uses {{seq}}."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "copies": 1,
            "dry_run": True,
            "sequence": {"count": 2, "start": 1},
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_sequence_count_zero_is_422(client: TestClient) -> None:
    """count < 1 must be rejected with 422 (ge=1 constraint)."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "dry_run": True,
            "sequence": {"count": 0, "start": 1},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_sequence_count_over_cap_is_422(client: TestClient) -> None:
    """count > 500 must be rejected with 422 (le=500 constraint)."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "dry_run": True,
            "sequence": {"count": 501, "start": 1},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_sequence_step_zero_is_422(client: TestClient) -> None:
    """step < 1 must be rejected with 422 (ge=1 constraint)."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "dry_run": True,
            "sequence": {"count": 5, "start": 1, "step": 0},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_sequence_padding_negative_is_422(client: TestClient) -> None:
    """padding < 0 must be rejected with 422 (ge=0 constraint)."""
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "dry_run": True,
            "sequence": {"count": 5, "start": 1, "padding": -1},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_sequence_fingerprint_differs_from_no_sequence(client: TestClient) -> None:
    """A request with a sequence spec must have a different fingerprint than the same request without.

    Two requests under the same idempotency key that differ in sequence presence must not dedupe.
    r1 = plain non-seq print (simple); r2 = seq print (seq-guard) — different templates and
    sequence presence ensure different fingerprints → 409 on key reuse.
    """
    import app.main as main_mod

    _write_seq_template(main_mod)
    r1 = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "dry_run": False,
            "idempotency_key": "sq-fp-1",
        },
    )
    assert r1.status_code == 200

    # Same key, different request (different template + has sequence) → different fingerprint → 409
    r2 = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "idempotency_key": "sq-fp-1",
            "sequence": {"count": 3, "start": 1},
        },
    )
    assert r2.status_code == 409, (
        "sequence vs no-sequence request must fingerprint differently → 409"
    )


def test_sequence_different_specs_fingerprint_differently(client: TestClient) -> None:
    """Two requests with different sequence specs under the same key must 409."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    base = {"template": "seq-guard", "fields": {}, "dry_run": False}
    r1 = client.post(
        "/print",
        json={**base, "idempotency_key": "sq-fp-2", "sequence": {"count": 3, "start": 1}},
    )
    assert r1.status_code == 200

    # Same key, different sequence (count=5 vs count=3) → different print → 409
    r2 = client.post(
        "/print",
        json={**base, "idempotency_key": "sq-fp-2", "sequence": {"count": 5, "start": 1}},
    )
    assert r2.status_code == 409, "Differing sequence specs must fingerprint differently → 409"


def test_sequence_reprint_replays_batch(client: TestClient) -> None:
    """A reprinted sequence job must replay with the frozen sequence spec.

    This tests that the sequence spec is stored in history and fed back to _execute_print
    on reprint so the batch is reproduced (same count sent to the driver).
    """
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": 5, "start": 10, "step": 2, "padding": 3},
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    record = main_mod._load_job(job_id)
    assert record is not None and record.sequence is not None
    assert record.sequence.count == 5
    assert record.sequence.start == 10
    assert record.sequence.step == 2
    assert record.sequence.padding == 3

    # Reprint should succeed and replay the sequence
    resp2 = client.post(f"/reprint/{job_id}")
    assert resp2.status_code == 200


def test_sequence_history_records_one_row_per_batch(client: TestClient) -> None:
    """One history row per batch — not one per item."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    before = main_mod._history.count()
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": 7, "start": 1},
        },
    )
    assert resp.status_code == 200
    after = main_mod._history.count()
    assert after == before + 1, (
        f"A sequence batch must add exactly 1 history row, not {after - before}"
    )


def test_plain_copies_unchanged_by_r7(client: TestClient) -> None:
    """Plain copies (no sequence) must still work exactly as before: no opts['pngs']."""
    import app.main as main_mod

    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "X"}, "copies": 3, "dry_run": False},
    )
    assert resp.status_code == 200
    _png, opts = main_mod._driver.render_payload.call_args[0]
    assert "pngs" not in opts, "Plain copies path must NOT set opts['pngs']"
    assert opts.get("copies") == 3, "Plain copies must still pass copies=3 to the driver"


# ── padding cap ──────────────────────────────────────────────────────────────────


def test_sequence_padding_above_cap_is_422(client: TestClient) -> None:
    """padding > MAX_SEQUENCE_PADDING (32) must be rejected with 422 at request validation.

    Without this bound, a tiny authenticated request could set an enormous padding and force
    large string allocations in render_sequence while the print lock is held (DoS).
    """
    from app.models import MAX_SEQUENCE_PADDING

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "X"},
            "dry_run": True,
            "sequence": {"count": 2, "start": 1, "padding": MAX_SEQUENCE_PADDING + 1},
        },
    )
    assert resp.status_code == 422, (
        f"padding={MAX_SEQUENCE_PADDING + 1} must be rejected as 422; got {resp.status_code}: {resp.text}"
    )


def test_sequence_padding_at_cap_is_ok(client: TestClient) -> None:
    """padding == MAX_SEQUENCE_PADDING (32) is the boundary and must be accepted."""
    import app.main as main_mod
    from app.models import MAX_SEQUENCE_PADDING

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": 1, "start": 1, "padding": MAX_SEQUENCE_PADDING},
        },
    )
    assert resp.status_code == 200, (
        f"padding={MAX_SEQUENCE_PADDING} (the cap) must be accepted; got {resp.status_code}: {resp.text}"
    )


# ── labels_printed_total batch count ─────────────────────────────────────────────


def _get_labels_printed(template: str, dry_run: bool) -> float:
    import app.main as main_mod

    return main_mod.LABELS_PRINTED.labels(template=template, dry_run=str(dry_run))._value.get()


def test_sequence_batch_increments_metric_by_count(client: TestClient) -> None:
    """A sequence batch of N labels must advance labels_printed_total by N, not 1.

    Without this, _execute_print would call .inc() (default 1) regardless of sequence.count,
    so a 500-label batch would be recorded as 1 printed label.
    """
    import app.main as main_mod

    _write_seq_template(main_mod)
    batch_count = 7
    before = _get_labels_printed("seq-guard", dry_run=True)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": batch_count, "start": 1},
        },
    )
    assert resp.status_code == 200

    after = _get_labels_printed("seq-guard", dry_run=True)
    assert after == before + batch_count, (
        f"labels_printed_total must advance by sequence.count={batch_count}; "
        f"got delta={(after - before)}"
    )


def test_copies_increments_metric_by_copies(client: TestClient) -> None:
    """Plain copies=N must advance labels_printed_total by N (not 1).

    Mirrors the sequence fix: the effective physical count is copies, not 1.
    """
    copies = 3
    before = _get_labels_printed("simple", dry_run=True)

    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "CopiesMetric"},
            "dry_run": True,
            "copies": copies,
        },
    )
    assert resp.status_code == 200

    after = _get_labels_printed("simple", dry_run=True)
    assert after == before + copies, (
        f"labels_printed_total must advance by copies={copies}; got delta={(after - before)}"
    )


# ── {{seq}} template requires a sequence spec ────────────────────────────────────


def _write_seq_template(main_mod: object, name: str = "seq-guard") -> None:
    """Write a template whose layout references {{seq}} into the live registry."""
    import textwrap

    yaml = textwrap.dedent(f"""\
        name: {name}
        description: A template that auto-numbers with the seq token
        label: "62"
        rotate: 0
        fields:
          required: []
          optional: []
        layout:
          - {{type: text, text: "Box {{{{seq}}}}"}}
    """)
    (main_mod.registry.templates_dir / f"{name}.yaml").write_text(yaml)  # type: ignore[attr-defined]
    main_mod.registry.load_all()  # type: ignore[attr-defined]


def test_print_seq_template_without_sequence_is_422(client: TestClient) -> None:
    """A {{seq}} template printed WITHOUT a sequence spec must 422 (not silently print blank)."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={"template": "seq-guard", "fields": {}, "dry_run": True},
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "seq" in resp.text.lower(), "Error must mention the seq token / sequence requirement"


def test_print_seq_template_with_sequence_works(client: TestClient) -> None:
    """A {{seq}} template printed WITH a sequence spec must still succeed and number each item."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 3, "start": 1, "padding": 2},
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    calls = main_mod._driver.render_payload.call_args_list
    pngs = [c.args[0] for c in calls]
    assert len(pngs) == 3, "Sequence print of a {{seq}} template must send count distinct labels"
    assert pngs[0] != pngs[1], "seq=01 and seq=02 must differ"


def test_print_non_seq_template_unaffected_by_guard(client: TestClient) -> None:
    """A template that does NOT use {{seq}} must print without a sequence spec (no regression)."""
    resp = client.post(
        "/print",
        json={"template": "simple", "fields": {"title": "Plain"}, "dry_run": True},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_preview_seq_template_is_422(client: TestClient) -> None:
    """/preview of a {{seq}} template must 422: preview carries no sequence, so {{seq}} would be
    blank and the user could approve a label that prints differently."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post("/preview", json={"template": "seq-guard", "fields": {}})
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    assert "seq" in resp.text.lower(), "Preview error must mention the seq requirement"


def test_preview_non_seq_template_unaffected(client: TestClient) -> None:
    """/preview of a non-{{seq}} template must still render normally."""
    resp = client.post("/preview", json={"template": "simple", "fields": {"title": "Hi"}})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_reprint_seq_template_without_saved_sequence_is_409(client: TestClient) -> None:
    """A saved job with no sequence whose current template now uses {{seq}} must 409 on reprint.

    Mirrors the /print + /preview guard for the reprint path: a non-sequence history row
    has record.sequence is None, but if the live template was edited to use {{seq}} (schema drift),
    _execute_print would take the single-render path and resolve {{seq}} to "" — reprinting a
    silently blank-numbered label. Reprint must reject it like the other schema-drift cases.
    """
    import app.main as main_mod

    _write_seq_template(main_mod)
    # Persist a non-sequence history row (record.sequence is None) for the now-{{seq}} template.
    job_id = _save_legacy_record(client, "seq-guard", {})
    resp = client.post(f"/reprint/{job_id}")
    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    assert "seq" in resp.text.lower(), "Reprint error must mention the seq/sequence requirement"


def test_print_sequence_on_non_seq_template_is_422(client: TestClient) -> None:
    """Reciprocal guard: a sequence spec on a template that does NOT use {{seq}} must 422.

    Without this guard, posting sequence.count=500 against a non-{{seq}} template silently
    renders 500 identical labels and bypasses the copies cap (10). The biconditional requires
    sequence iff the template uses {{seq}}.
    """
    resp = client.post(
        "/print",
        json={
            "template": "simple",
            "fields": {"title": "Bypass"},
            "dry_run": True,
            "sequence": {"count": 5, "start": 1},
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    body = resp.text.lower()
    assert "seq" in body or "sequence" in body, (
        "Error must mention {{seq}} / sequence inapplicability"
    )


def test_reprint_seq_saved_job_against_non_seq_template_is_409(client: TestClient) -> None:
    """Reciprocal reprint guard: a saved sequence job whose current template no longer uses {{seq}}
    must 409 on reprint (schema drift in the reverse direction).

    Scenario: a job was originally printed against a {{seq}} template; the template was later
    edited to remove {{seq}}. Replaying the sequence spec would emit a batch of identical
    unnumbered labels. The reprint guard must reject it.
    """
    import app.main as main_mod
    from app.models import PrintJobRecord, SequenceSpec

    # Seed a history row with a sequence spec against the plain 'simple' template (simulating a
    # template that used to have {{seq}} but was edited to remove it).
    job_id = "legacy-seq-job-1"
    main_mod._history.save(
        PrintJobRecord(
            job_id=job_id,
            template="simple",
            fields={"title": "Old"},
            copies=1,
            dry_run=True,
            timestamp="2026-01-01T00:00:00",
            status="printed",
            sequence=SequenceSpec(count=3, start=1),
        )
    )
    resp = client.post(f"/reprint/{job_id}")
    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    body = resp.text.lower()
    assert "seq" in body or "sequence" in body, (
        "Reprint error must mention {{seq}} / sequence drift"
    )


# ── incremental render — dry_run must not buffer the whole batch ──────────────────


def test_sequence_dry_run_renders_lazily_without_buffering(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry_run sequence of a large count must render lazily (one item at a time), never building
    a whole-batch buffer. We wrap engine.render_to_png and assert it is called exactly count times
    (one per item) and that render_sequence stays a generator the route pulls item-by-item."""
    import app.main as main_mod

    _write_seq_template(main_mod)
    count = 50
    calls = {"n": 0}
    real = main_mod.engine.render_to_png

    def _counting(*args: object, **kwargs: object) -> bytes:
        calls["n"] += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(main_mod.engine, "render_to_png", _counting)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": count, "start": 1},
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    # Exactly one render per item (the dry_run path pulls the generator to completion to validate
    # every item, discarding each PNG — so the renders happen but no whole-batch buffer is built).
    assert calls["n"] == count, f"dry_run must render each of {count} items once; got {calls['n']}"


# ── history browse payload includes the frozen sequence spec ──────────────────


def test_history_browse_includes_sequence_spec(client: TestClient) -> None:
    """A sequence job's browse row must expose the full frozen sequence spec so the UI can
    display the effective batch size and range (xN, first..last) instead of the misleading
    copies=1 value that sequence jobs always carry.

    This also acts as the regression test for the history-UI change: if the spec were
    stripped by the response model or the serialiser, the JS copiesCell() helper would fall
    back to showing '1 copy' for a 500-label batch — the footgun this feature prevents.
    """
    import app.main as main_mod

    _write_seq_template(main_mod)
    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": True,
            "sequence": {"count": 40, "start": 1, "step": 1, "padding": 3},
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    browse = client.get("/history/list?offset=0&limit=1")
    assert browse.status_code == 200
    entry = browse.json()["entries"][0]

    # The sequence spec must be present and complete in the browse payload.
    assert "sequence" in entry, "Browse row must include the 'sequence' key for a sequence job"
    seq = entry["sequence"]
    assert seq is not None, "sequence must not be null for a sequence job"
    assert seq["count"] == 40, f"sequence.count must be 40; got {seq['count']}"
    assert seq["start"] == 1, f"sequence.start must be 1; got {seq['start']}"
    assert seq["step"] == 1, f"sequence.step must be 1; got {seq['step']}"
    assert seq["padding"] == 3, f"sequence.padding must be 3; got {seq['padding']}"

    # Copies is 1 (as always for sequence jobs) — the UI must use sequence.count, not this.
    assert entry["copies"] == 1, "copies must be 1 for a sequence job (sequence drives the count)"


# ── render_error vs print_error classification in sequence loop ──────────────────────────────


def _seq_render_error_metric() -> float:
    """Current value of label_errors_total{reason="render_error"}."""
    import app.main as main_mod

    return main_mod.LABEL_ERRORS.labels(reason="render_error")._value.get()


def _seq_print_error_metric() -> float:
    """Current value of label_errors_total{reason="print_error"}."""
    import app.main as main_mod

    return main_mod.LABEL_ERRORS.labels(reason="print_error")._value.get()


def test_sequence_render_error_before_any_send_is_render_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render failure on the FIRST label of a sequence (printed==0) must:
    - emit label_errors_total{reason="render_error"} (NOT print_error)
    - NOT record any failed print row in history
    - return 500 (same status the plain render-error path uses)

    This mirrors the plain copies path: if the printer was never involved, the failure
    is a render fault, not a print fault — no physical output happened.
    """
    import app.main as main_mod

    _write_seq_template(main_mod)

    render_errors_before = _seq_render_error_metric()
    print_errors_before = _seq_print_error_metric()
    history_before = len(main_mod._history.recent(500))

    # Inject a render failure: monkeypatch engine.render_to_png to raise on the first call.
    real_render = main_mod.engine.render_to_png

    calls: dict[str, int] = {"n": 0}

    def _boom_on_first(*args: object, **kwargs: object) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected render failure")
        return real_render(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(main_mod.engine, "render_to_png", _boom_on_first)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 3, "start": 1},
        },
    )

    # Must fail (500 — render error, no printer involved).
    assert resp.status_code == 500, (
        f"Expected 500 on render error; got {resp.status_code}: {resp.text}"
    )
    assert "render error" in resp.text.lower(), f"Body must mention render error; got {resp.text!r}"

    # render_error metric incremented, print_error must NOT be incremented.
    assert _seq_render_error_metric() == render_errors_before + 1, (
        "render_error metric must increment on a sequence render failure"
    )
    assert _seq_print_error_metric() == print_errors_before, (
        "print_error metric must NOT increment when the failure is a render fault"
    )

    # No failed print row recorded — the printer was never involved.
    history_after = len(main_mod._history.recent(500))
    assert history_after == history_before, (
        "A render failure before any send must NOT record a failed print row "
        f"(history grew from {history_before} to {history_after})"
    )


def test_sequence_render_error_after_some_sends_records_partial_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render failure on a LATER label (printed > 0) must:
    - emit label_errors_total{reason="render_error"} (NOT print_error)
    - record exactly one failed history row (partial batch, physical output happened)
    - advance labels_printed_total by the number already sent before the failure
    - return 500

    NOTE: a render failure before any send (printed==0) is covered by the previous test.
    That case does NOT record a failed print row — only the mid-batch case does,
    because physical output already happened and the job must be visible in history.
    """
    import app.main as main_mod
    from app.transports.base import PrinterStatus

    _write_seq_template(main_mod)

    # Use a custom transport so sends succeed silently (allows measuring labels_printed_total).
    class _OkTransport:
        def __init__(self, uri: str) -> None:
            pass

        def send(self, data: bytes) -> PrinterStatus | None:
            return PrinterStatus.synthetic_ok()

        def query_status(self, request: bytes) -> PrinterStatus:
            return PrinterStatus.synthetic_ok()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _OkTransport)

    render_errors_before = _seq_render_error_metric()
    print_errors_before = _seq_print_error_metric()
    printed_before = _get_labels_printed("seq-guard", dry_run=False)
    history_len_before = len(main_mod._history.recent(500))

    # Fail the render on the 3rd label (2 already sent successfully).
    FAIL_AT = 3
    real_render = main_mod.engine.render_to_png
    calls: dict[str, int] = {"n": 0}

    def _boom_on_nth(*args: object, **kwargs: object) -> bytes:
        calls["n"] += 1
        if calls["n"] == FAIL_AT:
            raise RuntimeError("injected mid-batch render failure")
        return real_render(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(main_mod.engine, "render_to_png", _boom_on_nth)

    resp = client.post(
        "/print",
        json={
            "template": "seq-guard",
            "fields": {},
            "dry_run": False,
            "sequence": {"count": 5, "start": 1},
        },
    )

    assert resp.status_code == 500, (
        f"Expected 500 on mid-batch render error; got {resp.status_code}: {resp.text}"
    )
    assert "render error" in resp.text.lower(), f"Body must mention render error; got {resp.text!r}"

    # render_error metric incremented, print_error must NOT be incremented.
    assert _seq_render_error_metric() == render_errors_before + 1, (
        "render_error metric must increment on a mid-batch render failure"
    )
    assert _seq_print_error_metric() == print_errors_before, (
        "print_error metric must NOT increment when the failure is a render fault"
    )

    # One failed history row recorded (partial batch — physical output happened before the fault).
    history_len_after = len(main_mod._history.recent(500))
    assert history_len_after == history_len_before + 1, (
        "A mid-batch render failure (printed > 0) must record exactly one failed history row"
    )
    failed_rows = [r for r in main_mod._history.recent(500) if r.status == "failed"]
    assert failed_rows, "The history row must be recorded as failed"

    # labels_printed_total advanced by the 2 labels sent before the render failed (FAIL_AT - 1).
    printed_after = _get_labels_printed("seq-guard", dry_run=False)
    expected_delta = FAIL_AT - 1
    assert printed_after == printed_before + expected_delta, (
        f"labels_printed_total must advance by {expected_delta} (labels sent before render failure); "
        f"got delta={printed_after - printed_before}"
    )


# ── EDITOR_ENABLED feature gate ──────────────────────────────────────────────────────────────────


def test_editor_gate_disabled_by_default_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EDITOR_ENABLED=false (the default) must 404 all studio routes — not 401/403."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "editor_enabled", False)
    assert client.get("/editor").status_code == 404
    assert client.post("/preview/draft", json={"yaml": "", "fields": {}}).status_code == 404
    assert client.post("/templates/parse", json={"yaml": ""}).status_code == 404
    assert client.post("/templates", json={"name": "x", "yaml": ""}).status_code == 404


def test_editor_gate_precedes_auth(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """EDITOR_ENABLED=false must 404 (route appears absent), not 401, even when a token is required.

    The visibility gate is listed before ``check_token`` on all studio routes, so a disabled studio
    is indistinguishable from an unrouted path — no detail that discloses the hidden surface.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "secret123")
    monkeypatch.setattr(main_mod.settings, "editor_enabled", False)
    assert client.get("/editor").status_code == 404
    assert client.post("/preview/draft", json={"yaml": "", "fields": {}}).status_code == 404
    assert client.post("/templates/parse", json={"yaml": ""}).status_code == 404
    assert client.post("/templates", json={"name": "x", "yaml": ""}).status_code == 404


def test_editor_disabled_hides_nav_link(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When EDITOR_ENABLED=false, the index page must not render the Editor → nav link."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "editor_enabled", False)
    assert 'href="/editor"' not in client.get("/").text


def test_editor_enabled_shows_nav_link(client: TestClient) -> None:
    """When EDITOR_ENABLED=true (set by the client fixture), the Editor → nav link appears."""
    assert 'href="/editor"' in client.get("/").text


def test_save_template_editor_disabled_is_404_not_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With EDITOR_ENABLED=false, POST /templates is 404 (gate wins) even if TEMPLATES_WRITABLE=true.

    The editor gate precedes auth AND the writable check, so a disabled studio never reveals whether
    server-save is configured — 404 regardless of TEMPLATES_WRITABLE.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "editor_enabled", False)
    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    assert client.post("/templates", json={"name": "x", "yaml": ""}).status_code == 404


# ── In-browser YAML template studio (draft preview + field auto-detection) ──────
_DRAFT_YAML = """\
name: draft-simple
description: A draft template
label: "62"
rotate: 0
fields:
  required: [title]
  optional: [subtitle]
layout:
  - {type: title, text: "{{title}}"}
  - {type: subtitle, text: "{{subtitle}}"}
"""


def test_preview_draft_happy_path_returns_png(client: TestClient) -> None:
    """Valid draft YAML + fields renders a PNG, just like /preview — no file is written."""
    resp = client.post("/preview/draft", json={"yaml": _DRAFT_YAML, "fields": {"title": "Hello"}})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.width > 0
    assert img.height > 0


def test_preview_draft_malformed_yaml_is_422_not_500(client: TestClient) -> None:
    """A YAML parse error must surface as a structured 422, never an unhandled 500."""
    resp = client.post("/preview/draft", json={"yaml": "name: [unclosed\n  : : :", "fields": {}})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["msg"] == "Invalid template YAML"
    assert "error" in detail


def test_preview_draft_schema_invalid_template_is_422(client: TestClient) -> None:
    """A structurally-parseable YAML that violates the template schema → 422 (missing keys)."""
    resp = client.post("/preview/draft", json={"yaml": "name: x\nlabel: '62'\n", "fields": {}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["msg"] == "Invalid template YAML"


def test_preview_draft_malformed_placeholder_is_422(client: TestClient) -> None:
    """A draft with a {{...}} span the engine can't substitute → 422 (never a literal-on-label print).

    Guards the studio parse/preview path: a hyphenated/spaced placeholder in layout text is caught at
    validation so the editor surfaces it instead of rendering the literal token onto a label.
    """
    yaml = _DRAFT_YAML.replace('text: "{{title}}"', 'text: "{{asset-id}}"')
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "Hi"}})
    assert resp.status_code == 422
    assert client.post("/templates/parse", json={"yaml": yaml}).status_code == 422


def test_preview_draft_unsupported_label_is_400(client: TestClient) -> None:
    """A label the configured model does not support → 400 (matches the saved-template path)."""
    yaml = _DRAFT_YAML.replace('label: "62"', 'label: "nonsense-label"')
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "Hi"}})
    assert resp.status_code == 400


def test_templates_parse_detects_real_user_fields(client: TestClient) -> None:
    """Auto-detection returns the declared user fields (required/optional)."""
    resp = client.post("/templates/parse", json={"yaml": _DRAFT_YAML})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "draft-simple"
    assert data["fields"]["required"] == ["title"]
    assert data["fields"]["optional"] == ["subtitle"]


def test_templates_parse_excludes_computed_and_i18n_tokens(client: TestClient) -> None:
    """{{date}}/{{now}}/[[translation]] are NOT surfaced as user fields; only real fields are.

    The template uses {{date}}, {{now}} and a [[frozen]] translation token alongside one real
    user field ({{contents}}). The field contract must contain only the real field.
    """
    yaml = """\
name: draft-computed
description: Uses computed + i18n tokens plus one real field
label: "62"
rotate: 0
fields:
  required: [contents]
  optional: []
layout:
  - {type: text, text: "[[frozen]] {{contents}}"}
  - {type: text, text: "made {{date}} at {{now}}"}
"""
    resp = client.post("/templates/parse", json={"yaml": yaml})
    assert resp.status_code == 200
    fields = resp.json()["fields"]
    assert fields["required"] == ["contents"]
    assert fields["optional"] == []
    # Computed/i18n tokens must never leak into the user-field contract.
    for tok in ("date", "now", "seq", "frozen"):
        assert tok not in fields["required"]
        assert tok not in fields["optional"]


def test_templates_parse_reserved_field_name_is_422(client: TestClient) -> None:
    """Declaring a reserved computed-token name (seq/date/now) as a user field → 422."""
    for reserved in ("seq", "date", "now"):
        yaml = f"""\
name: draft-reserved
description: Reserved field name
label: "62"
rotate: 0
fields:
  required: [{reserved}]
  optional: []
layout:
  - {{type: title, text: "{{{{{reserved}}}}}"}}
"""
        resp = client.post("/templates/parse", json={"yaml": yaml})
        assert resp.status_code == 422, f"reserved name {reserved!r} must 422"
        assert resp.json()["detail"]["msg"] == "Invalid template YAML"


def test_preview_draft_reserved_field_name_is_422(client: TestClient) -> None:
    """The draft preview path rejects a reserved-name template too (same validator)."""
    yaml = """\
name: draft-reserved
description: Reserved field name
label: "62"
rotate: 0
fields:
  required: [seq]
  optional: []
layout:
  - {type: title, text: "{{seq}}"}
"""
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"seq": "1"}})
    assert resp.status_code == 422


def test_preview_draft_seq_template_without_sequence_is_422(client: TestClient) -> None:
    """A {{seq}} draft behaves like /preview of a saved {{seq}} template: 422 (would render blank).

    `seq` is a computed token (not a declared field), so the template loads, but the preview path
    rejects it because no sequence object can accompany a preview — matching the established
    /preview behaviour.
    """
    yaml = """\
name: draft-seq
description: Uses the seq auto-numbering token
label: "62"
rotate: 0
fields:
  required: []
  optional: []
layout:
  - {type: title, text: "Box {{seq}}"}
"""
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {}})
    assert resp.status_code == 422


def test_preview_draft_oversized_text_field_is_413(client: TestClient) -> None:
    """The text-field cap applies to the draft path — a draft does not bypass it."""
    import app.main as main_mod

    huge = "A" * (main_mod.MAX_TEXT_FIELD_CHARS + 1)
    resp = client.post("/preview/draft", json={"yaml": _DRAFT_YAML, "fields": {"title": huge}})
    assert resp.status_code == 413


def test_preview_draft_too_many_fields_is_413(client: TestClient) -> None:
    """The field-count cap applies to the draft path."""
    import app.main as main_mod

    fields = {"title": "Hi"}
    fields.update({f"f{i}": "x" for i in range(main_mod.MAX_FIELD_COUNT)})
    assert len(fields) > main_mod.MAX_FIELD_COUNT
    resp = client.post("/preview/draft", json={"yaml": _DRAFT_YAML, "fields": fields})
    assert resp.status_code == 413


def test_preview_draft_oversized_image_field_is_413(client: TestClient) -> None:
    """The image-field cap applies to the draft path — an oversized base64 image is rejected."""
    import app.main as main_mod

    yaml = """\
name: draft-image
description: Image draft
label: "62"
rotate: 0
fields:
  required: [image]
  optional: []
layout:
  - {type: image, field: image}
"""
    oversized = "A" * (main_mod.MAX_IMAGE_B64_CHARS + 4)
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"image": oversized}})
    assert resp.status_code == 413


def test_preview_draft_unknown_top_level_key_is_422(client: TestClient) -> None:
    """A typo'd request key (extra='forbid') is a 422, not silently ignored."""
    resp = client.post("/preview/draft", json={"yaml": _DRAFT_YAML, "feilds": {"title": "x"}})
    assert resp.status_code == 422


# ── server-save (gated behind TEMPLATES_WRITABLE) ──────
def test_save_template_disabled_by_default_is_403(client: TestClient) -> None:
    """With TEMPLATES_WRITABLE=false (the default), POST /templates is a 403."""
    resp = client.post("/templates", json={"name": "newtmpl", "yaml": _DRAFT_YAML})
    assert resp.status_code == 403


def test_save_template_when_enabled_writes_and_reloads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TEMPLATES_WRITABLE=true, a valid draft is written and the registry reloads it."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    yaml = _DRAFT_YAML.replace("draft-simple", "saved-via-studio")
    resp = client.post("/templates", json={"name": "saved-via-studio", "yaml": yaml})
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] == "saved-via-studio"
    assert (main_mod.settings.templates_dir / "saved-via-studio.yaml").exists()
    assert "saved-via-studio" in main_mod.registry._templates


def test_save_template_path_traversal_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted name with separators / traversal in the YAML's internal name → 422 even if writable.

    The save target is the VALIDATED template's internal `name`, so the path-traversal
    guard is applied to it. A crafted internal name must be rejected before any file is written.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    for bad in ("../evil", "a/b", "..", "with.dot", "/abs"):
        yaml = _DRAFT_YAML.replace("name: draft-simple", f'name: "{bad}"')
        resp = client.post("/templates", json={"name": "ok-name", "yaml": yaml})
        assert resp.status_code == 422, f"internal name {bad!r} must be rejected"


def test_save_template_invalid_yaml_not_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-invalid YAML is rejected (422) and never persisted, even when writable."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    resp = client.post("/templates", json={"name": "broken", "yaml": "name: x\nlabel: '62'\n"})
    assert resp.status_code == 422
    assert not (main_mod.settings.templates_dir / "broken.yaml").exists()


def test_save_template_casefold_collision_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case-only name collision is rejected (422) so a save can't clobber a different template.

    On a case-insensitive filesystem ``Foo.yaml`` and ``foo.yaml`` are the same file, which would let
    saving internal name ``Foo`` silently overwrite an existing ``foo`` template while the
    duplicate-name registry guard (which is case-sensitive) never fires. The save path rejects the
    collision before writing; re-saving the SAME name (same case) is still a legitimate overwrite.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    lower = _DRAFT_YAML.replace("draft-simple", "casecollide")
    assert client.post("/templates", json={"name": "casecollide", "yaml": lower}).status_code == 200

    # A different-cased internal name collides with casecollide.yaml on a case-insensitive volume.
    upper = _DRAFT_YAML.replace("name: draft-simple", "name: CaseCollide")
    resp = client.post("/templates", json={"name": "CaseCollide", "yaml": upper})
    assert resp.status_code == 422
    assert "case-insensitive" in resp.text
    # The original lowercase template is untouched.
    assert (main_mod.settings.templates_dir / "casecollide.yaml").exists()

    # Re-saving the exact same name (same case) is a normal overwrite, not a collision.
    assert client.post("/templates", json={"name": "casecollide", "yaml": lower}).status_code == 200


def test_editor_page_served(client: TestClient) -> None:
    """The /editor studio page is served as HTML and is reachable without a token."""
    resp = client.get("/editor")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Template Studio" in resp.text


# ── Load an existing template's source (GET /templates/{name}/source) ──
def test_template_source_happy_path(client: TestClient) -> None:
    """A known template's raw YAML loads and round-trips back through the draft parser."""
    resp = client.get("/templates/simple/source")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "simple"
    assert "name: simple" in body["yaml"]
    # The returned source is itself a loadable template.
    assert client.post("/templates/parse", json={"yaml": body["yaml"]}).status_code == 200


def test_template_source_unknown_name_is_404(client: TestClient) -> None:
    """An unregistered name is a registry miss → 404, never a filesystem probe."""
    assert client.get("/templates/does-not-exist/source").status_code == 404


def test_template_source_traversal_is_404(client: TestClient) -> None:
    """Traversal-shaped names cannot escape: the name is a registry key, never a path component."""
    for bad in ("..", "..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fsecrets"):
        assert client.get(f"/templates/{bad}/source").status_code == 404


def test_template_source_load_picker_present_when_loadable(client: TestClient) -> None:
    """With TEMPLATES_LOADABLE true (default), the editor renders the load picker."""
    assert 'id="load-select"' in client.get("/editor").text


def test_template_source_loadable_disabled_is_404_and_hides_picker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEMPLATES_LOADABLE=false 404s the source route and removes the picker (editor still served)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_loadable", False)
    assert client.get("/templates/simple/source").status_code == 404
    page = client.get("/editor")
    assert page.status_code == 200  # the editor itself stays available
    assert 'id="load-select"' not in page.text


def test_template_source_editor_disabled_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EDITOR_ENABLED=false hides the source route entirely (editor gate wins)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "editor_enabled", False)
    assert client.get("/templates/simple/source").status_code == 404


def test_template_source_gate_precedes_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled feature 404s even when a token is required; enabled-but-unauthed is 401."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "api_token", "secret123")
    # Feature off → 404 (route appears absent), not 401.
    monkeypatch.setattr(main_mod.settings, "templates_loadable", False)
    assert client.get("/templates/simple/source").status_code == 404
    # Feature on, no token → 401 (auth runs after the gates pass).
    monkeypatch.setattr(main_mod.settings, "templates_loadable", True)
    assert client.get("/templates/simple/source").status_code == 401


# ── server-side field-name charset (defence in depth behind editor textContent) ──────────────────
# The editor now builds the field-form labels and status messages with textContent / text nodes (no
# innerHTML interpolation of server-supplied strings), so a name like `<img src=x onerror=...>` can
# never execute in the studio page. That DOM behaviour cannot be unit-tested here; this asserts the
# server-side guard that backs it — /templates/parse and save reject HTML-ish field names at load.
_XSS_FIELD_YAML = """\
name: draft-xss
description: Field name carrying HTML
label: "62"
rotate: 0
fields:
  required: ["<img src=x onerror=fetch(1)>"]
  optional: []
layout:
  - {type: title, text: hi}
"""


def test_templates_parse_rejects_html_field_name_is_422(client: TestClient) -> None:
    """A draft declaring an HTML-ish field name is rejected at parse with a clear error (not 200)."""
    resp = client.post("/templates/parse", json={"yaml": _XSS_FIELD_YAML})
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["msg"] == "Invalid template YAML"
    assert "invalid field name" in body["detail"]["error"]


def test_preview_draft_rejects_html_field_name_is_422(client: TestClient) -> None:
    """The draft preview path rejects the HTML-ish field name too (same validator)."""
    resp = client.post("/preview/draft", json={"yaml": _XSS_FIELD_YAML, "fields": {}})
    assert resp.status_code == 422


# ── render-affecting numeric attributes are bounded BEFORE render (422, not 500) ──
def _draft_with_layout(
    layout_line: str, *, fields: str = "required: [title]\n  optional: []"
) -> str:
    return (
        f'name: draft-num\ndescription: d\nlabel: "62"\nrotate: 0\n'
        f"fields:\n  {fields}\nlayout:\n  - {layout_line}\n"
    )


def test_preview_draft_negative_spacer_size_is_422(client: TestClient) -> None:
    """A negative spacer.size is rejected at validation (422), never reaching the renderer (500)."""
    yaml = _draft_with_layout("{type: title, text: hi}\n  - {type: spacer, size: -5}")
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "x"}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["msg"] == "Invalid template YAML"


def test_preview_draft_enormous_dimension_is_422(client: TestClient) -> None:
    """An enormous dimension (above the per-element cap) is rejected (422), not a huge allocation."""
    for layout in (
        "{type: spacer, size: 99999999999}",
        "{type: text, text: hi, size: 99999999999}",
        "{type: qr, data: x, size: 99999999999}",
        "{type: box, height: 99999999999}",
        "{type: barcode, data: x, height: 99999999999}",
        "{type: icon, name: snowflake, size: 99999999999}",
    ):
        yaml = _draft_with_layout(layout)
        resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "x"}})
        assert resp.status_code == 422, f"{layout!r} must be rejected, got {resp.status_code}"


def test_preview_draft_non_int_dimension_is_422(client: TestClient) -> None:
    """A non-integer dimension (e.g. size: '32') is a type error caught at load, not a render crash."""
    yaml = _draft_with_layout('{type: text, text: hi, size: "32"}')
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "x"}})
    assert resp.status_code == 422


def test_preview_draft_normal_dimensions_still_200(client: TestClient) -> None:
    """A template with ordinary in-bounds numeric attributes still renders fine."""
    yaml = _draft_with_layout(
        '{type: title, text: "{{title}}", max_lines: 2}\n'
        "  - {type: text, text: hi, size: 28}\n"
        "  - {type: spacer, size: 16}\n"
        "  - {type: box, height: 40, border: 2}"
    )
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "Hello"}})
    assert resp.status_code == 200


# ── quadratic/area render shapes are bounded tighter than the 1-D cap ───────────
def test_preview_draft_square_and_font_caps_are_422(client: TestClient) -> None:
    """qr/icon size render as a sizexsize square and text size is a font point size: each has a
    tighter cap (MAX_SQUARE_DIMENSION / MAX_FONT_SIZE) than the linear MAX_ELEMENT_DIMENSION, so a
    value of 10000 — under the old linear cap — is now rejected before any quadratic allocation."""
    for layout in (
        "{type: qr, data: x, size: 10000}",
        "{type: icon, name: snowflake, size: 10000}",
        "{type: text, text: hi, size: 10000}",
    ):
        yaml = _draft_with_layout(layout)
        resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "x"}})
        assert resp.status_code == 422, f"{layout!r} must be rejected, got {resp.status_code}"
        assert resp.json()["detail"]["msg"] == "Invalid template YAML"


def test_preview_draft_text_strip_product_is_422(client: TestClient) -> None:
    """A text whose sizexmax_lines exceeds the strip-area cap is rejected even though each scalar is
    in bounds (size 500 ≤ MAX_FONT_SIZE, max_lines 100 ≤ MAX_TEXT_LINES, but 500x100 ≫ 4000)."""
    yaml = _draft_with_layout("{type: text, text: hi, size: 500, max_lines: 100}")
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "x"}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["msg"] == "Invalid template YAML"


def test_preview_draft_out_of_range_rotate_is_422(client: TestClient) -> None:
    """A non-quarter-turn rotate (99) and a giant value are rejected at the draft path (422), not a
    PIL OverflowError (500) at render."""
    for bad in (99, 99999999999):
        yaml = f'name: draft-rot\ndescription: d\nlabel: "62"\nrotate: {bad}\nlayout:\n  - {{type: text, text: hi}}\n'
        resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {}})
        assert resp.status_code == 422, f"rotate {bad} must be rejected, got {resp.status_code}"
        assert resp.json()["detail"]["msg"] == "Invalid template YAML"


def test_preview_draft_in_bounds_square_font_rotate_still_200(client: TestClient) -> None:
    """Real values under the tightened caps still render (qr 600, text size 48 max_lines 4, rotate 90)."""
    yaml = (
        'name: draft-ok\ndescription: d\nlabel: "62"\nrotate: 90\nfields:\n'
        "  required: [title]\n  optional: []\nlayout:\n"
        '  - {type: title, text: "{{title}}"}\n'
        "  - {type: text, text: hi, size: 48, max_lines: 4}\n"
        "  - {type: qr, data: x, size: 600}\n"
    )
    resp = client.post("/preview/draft", json={"yaml": yaml, "fields": {"title": "Hello"}})
    assert resp.status_code == 200


# ── server-save filename derives from the validated template's internal name ──────
def test_save_template_uses_internal_name_not_request_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request.name that differs from the YAML's `name` saves under the YAML name and reports it.

    Regression: previously the file was written to `<request.name>.yaml` while the registry indexed
    by the YAML's internal name, so `{name: simple, <yaml name: renamed>}` clobbered simple.yaml and
    registered "renamed". The save target must be the validated template's internal name, the
    response must report it, and the pre-existing simple.yaml must NOT be clobbered.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    simple_path = main_mod.settings.templates_dir / "simple.yaml"
    simple_before = simple_path.read_text(encoding="utf-8")

    yaml = _DRAFT_YAML.replace("draft-simple", "renamed")
    resp = client.post("/templates", json={"name": "simple", "yaml": yaml})
    assert resp.status_code == 200
    body = resp.json()
    # Response reports the real internal name, not the request name.
    assert body["saved"] == "renamed"
    assert body["path"] == "renamed.yaml"
    # The file is written under the internal name; simple.yaml is untouched.
    assert (main_mod.settings.templates_dir / "renamed.yaml").exists()
    assert simple_path.read_text(encoding="utf-8") == simple_before
    assert "renamed" in main_mod.registry._templates


# ── a duplicate internal name in an EARLIER-sorting file rolls the save back ─────
def test_save_template_duplicate_internal_name_earlier_file_rolls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source_path check only caught a duplicate file sorting AFTER the saved file. A
    duplicate declaring the same internal `name` in an EARLIER-sorting file used to silently shadow
    the save. The registry now records that duplicate as an error, so save_template's `if errors:`
    branch rolls the write back and returns 422 — and a plain /reload reports the same duplicate.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    tdir = main_mod.settings.templates_dir
    # Pre-write a file that sorts BEFORE the save target (`mmm.yaml`) but declares the same internal
    # name `mmm`. On reload it wins the name (first in sort order) and the just-saved mmm.yaml is the
    # rejected duplicate.
    (tdir / "aaa-dup.yaml").write_text(
        "name: mmm\ndescription: earlier duplicate\nlabel: '62'\n"
        "layout:\n  - {type: text, text: earlier}\n",
        encoding="utf-8",
    )
    main_mod.registry.load_all()

    yaml = _DRAFT_YAML.replace("draft-simple", "mmm")
    resp = client.post("/templates", json={"name": "mmm", "yaml": yaml})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "rolled back" in detail["detail"]
    # The duplicate-name error names both files and the shared name.
    assert any("aaa-dup.yaml" in err and "mmm" in err for err in detail["errors"])
    # The save was rolled back: mmm.yaml must not be left on disk, and no temp sibling lingers.
    assert not (tdir / "mmm.yaml").exists()
    leftovers = [p.name for p in tdir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"rollback left temp files behind: {leftovers}"


# ── save is atomic and rolls back if the written file fails to reload ─────────────
def test_save_template_atomic_no_temp_left_behind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful save returns 200, writes the final file, and leaves no `.tmp` sibling behind.

    The write goes via a temp file + os.replace (atomic on one filesystem); on success the temp
    name must have been consumed by the replace, never lingering in the templates dir.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    yaml = _DRAFT_YAML.replace("draft-simple", "atomic-save")
    resp = client.post("/templates", json={"name": "atomic-save", "yaml": yaml})
    assert resp.status_code == 200
    tdir = main_mod.settings.templates_dir
    assert (tdir / "atomic-save.yaml").exists()
    leftovers = [p.name for p in tdir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"atomic save left temp files behind: {leftovers}"


def test_save_template_rollback_on_reload_error_is_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the post-write reload reports an error, the save is rolled back and reported as 422.

    The /reload endpoint treats reload errors as 422; save must not be weaker. We inject an error by
    wrapping registry.load_all so its first call (the save's verify) reports a stale error, forcing
    the rollback path. The previously-existing simple.yaml must survive unchanged and no new file or
    temp sibling may be left behind.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    tdir = main_mod.settings.templates_dir
    simple_before = (tdir / "simple.yaml").read_text(encoding="utf-8")

    real_load_all = main_mod.registry.load_all
    calls = {"n": 0}

    def fake_load_all() -> list[str]:
        # First call is the save's post-write verify: report an error so the endpoint rolls back.
        # Later calls (the rollback's restore reload) run for real so the registry ends consistent.
        loaded = real_load_all()
        calls["n"] += 1
        if calls["n"] == 1:
            main_mod.registry._errors = ["injected: simulated reload failure"]
        return loaded

    monkeypatch.setattr(main_mod.registry, "load_all", fake_load_all)

    yaml = _DRAFT_YAML.replace("draft-simple", "rollback-target")
    resp = client.post("/templates", json={"name": "rollback-target", "yaml": yaml})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "rolled back" in detail["detail"]
    assert detail["errors"]
    # The new file was rolled back (it did not exist before), simple.yaml is untouched, no temp left.
    assert not (tdir / "rollback-target.yaml").exists()
    assert (tdir / "simple.yaml").read_text(encoding="utf-8") == simple_before
    leftovers = [p.name for p in tdir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"rollback left temp files behind: {leftovers}"


# ── post-write reload that RAISES (not just .errors) still rolls back ─────────────
def test_save_template_reload_raises_rolls_back_not_false_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the post-write reload RAISES (e.g. an FS/decode error load_all doesn't catch), the save is
    rolled back and the new file removed — not a 500 with the file left on disk and no rollback.

    The previous code ran load_all() outside any try/except, so a raise bypassed rollback. We force
    the first (verify) reload to raise; the second (post-rollback resync) runs for real so the
    registry ends consistent. The new file must be gone and simple.yaml untouched.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    tdir = main_mod.settings.templates_dir
    simple_before = (tdir / "simple.yaml").read_text(encoding="utf-8")

    real_load_all = main_mod.registry.load_all
    calls = {"n": 0}

    def fake_load_all() -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated unreadable template file")
        return real_load_all()

    monkeypatch.setattr(main_mod.registry, "load_all", fake_load_all)

    yaml = _DRAFT_YAML.replace("draft-simple", "reload-raises")
    resp = client.post("/templates", json={"name": "reload-raises", "yaml": yaml})
    # Rolled back, not a false success and not an unhandled 500 with the file left behind.
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "rolled back" in detail["detail"]
    assert detail["errors"]
    assert not (tdir / "reload-raises.yaml").exists()
    assert (tdir / "simple.yaml").read_text(encoding="utf-8") == simple_before
    leftovers = [p.name for p in tdir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"rollback left temp files behind: {leftovers}"


def test_save_template_rollback_failure_is_500_not_false_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the reload fails AND the rollback restore itself raises OSError, the endpoint returns 500
    explaining the on-disk state may be inconsistent — NOT the 422 'rolled back' message (a lie)."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)

    # Force the verify reload to report an error so the rollback path is taken.
    real_load_all = main_mod.registry.load_all
    calls = {"n": 0}

    def fake_load_all() -> list[str]:
        loaded = real_load_all()
        calls["n"] += 1
        if calls["n"] == 1:
            main_mod.registry._errors = ["injected: simulated reload failure"]
        return loaded

    monkeypatch.setattr(main_mod.registry, "load_all", fake_load_all)

    # Make the rollback's atomic restore raise OSError (no previous file existed, so rollback would
    # call path.unlink — patch unlink to raise instead).
    def boom_unlink(*_a: object, **_k: object) -> None:
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(main_mod.Path, "unlink", boom_unlink)

    yaml = _DRAFT_YAML.replace("draft-simple", "rollback-fails")
    resp = client.post("/templates", json={"name": "rollback-fails", "yaml": yaml})
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "inconsistent" in detail["detail"]
    assert "rolled back" not in detail["detail"]


# ── a duplicate internal name claimed by another file → rollback, not 200 ─────────
def test_save_template_duplicate_internal_name_rolls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If after reload the registered template for our name resolves to a DIFFERENT file, the save is
    rolled back with a 422 rather than falsely reporting success.

    We construct the mismatch by patching registry.get to return a template whose source_path points
    at another file, simulating a second YAML later in sort order that claims the same internal name.
    """
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "templates_writable", True)
    tdir = main_mod.settings.templates_dir

    real_get = main_mod.registry.get

    class _Other:
        source_path = tdir / "some-other-file.yaml"

    def fake_get(name: str) -> object:
        if name == "dup-name":
            return _Other()
        return real_get(name)

    monkeypatch.setattr(main_mod.registry, "get", fake_get)

    yaml = _DRAFT_YAML.replace("draft-simple", "dup-name")
    resp = client.post("/templates", json={"name": "dup-name", "yaml": yaml})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "rolled back" in detail["detail"]
    assert any("already declares the internal name" in e for e in detail["errors"])
    # The file we wrote was rolled back (it did not exist before).
    assert not (tdir / "dup-name.yaml").exists()


# ── Step 5: SNMP media/fault print preflight (closes the phantom-success hole) ──────────
# The QL-810W rasterises a job and only then rejects a media mismatch at the hardware level (red
# blink, prints nothing) while its :9100 NIC stays silent — so a mismatch used to record a phantom
# 200. /print and /reprint now query SNMP first and refuse a mismatch or a hard fault with 409.


def _write_label_template(main_mod: object, name: str, label: str) -> None:
    """Write a minimal no-field template bound to ``label`` into the live registry."""
    import textwrap

    yaml = textwrap.dedent(f"""\
        name: {name}
        description: Template bound to the {label} label
        label: "{label}"
        rotate: 0
        fields:
          required: []
          optional: []
        layout:
          - {{type: text, text: "hello"}}
    """)
    (main_mod.registry.templates_dir / f"{name}.yaml").write_text(yaml)  # type: ignore[attr-defined]
    main_mod.registry.load_all()  # type: ignore[attr-defined]


def _arm_network_snmp(monkeypatch: pytest.MonkeyPatch, main_mod: object, loaded: object) -> None:
    """Point the app at a network printer with SNMP enabled and stub the SNMP read to ``loaded``."""
    monkeypatch.setattr(main_mod.settings, "printer_uri", "tcp://192.168.5.14:9100")  # type: ignore[attr-defined]
    monkeypatch.setattr(main_mod.settings, "snmp_enabled", True)  # type: ignore[attr-defined]
    monkeypatch.setattr(main_mod, "_query_loaded_media", lambda: loaded)  # type: ignore[attr-defined]


class _SilentNetworkTransport:
    """A network transport whose send/query succeed without touching a socket (match-path prints)."""

    def __init__(self, uri: str) -> None:
        pass

    def send(self, data: bytes):  # type: ignore[no-untyped-def]
        from app.transports.base import PrinterStatus

        return PrinterStatus.synthetic_ok()

    def query_status(self, request: bytes):  # type: ignore[no-untyped-def]
        from app.transports.base import PrinterStatus

        return PrinterStatus.synthetic_ok()

    def close(self) -> None:
        pass


def _loaded_62_continuous() -> object:
    from app.transports.snmp import PrinterSNMPStatus

    return PrinterSNMPStatus(
        reachable=True,
        model="Brother QL-810W",
        media_name='62mm / 2.4"',
        media_width_mm=62.0,
        media_length_mm=None,
        media_type="continuous",
    )


def test_print_media_mismatch_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 62x29 die-cut template against the loaded 62mm continuous roll is refused with 409.

    This is exactly the production failure (HTTP 200, red-blink-prints-nothing) the guard closes.
    """
    import app.main as main_mod

    _write_label_template(main_mod, "diecut", "62x29")
    _arm_network_snmp(monkeypatch, main_mod, _loaded_62_continuous())
    # If the guard let the print through it would hit the transport; make that loud.
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})

    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert detail["label"] == "62x29"
    assert "62x29" in detail["media_required"]
    assert "62mm" in detail["media_loaded"]
    # The driver must never have been asked to rasterise a job that can't physically print.
    assert main_mod._driver.render_payload.call_count == 0


def test_print_media_mismatch_enforced_without_media_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatch is still refused with 409 when the printer omits the descriptive media_name OID.

    media_name is best-effort (it only labels the 409 detail), so a version-skewed agent that reports
    the safety geometry but not media_name must NOT escape the guard — the detail falls back to a
    geometry description. Guards the over-broad-critical-OIDs fix end to end."""
    import app.main as main_mod
    from app.transports.snmp import PrinterSNMPStatus

    loaded = PrinterSNMPStatus(
        reachable=True,
        media_name=None,  # descriptive OID absent
        media_width_mm=62.0,
        media_length_mm=None,
        media_type="continuous",
    )
    _write_label_template(main_mod, "diecut", "62x29")
    _arm_network_snmp(monkeypatch, main_mod, loaded)
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})

    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert detail["label"] == "62x29"
    assert "62mm continuous" in detail["media_loaded"], "falls back to a geometry description"
    assert main_mod._driver.render_payload.call_count == 0


def test_print_media_match_prints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A continuous 62 template against the loaded 62mm continuous roll passes the guard and prints."""
    import app.main as main_mod

    _write_label_template(main_mod, "cont", "62")
    _arm_network_snmp(monkeypatch, main_mod, _loaded_62_continuous())
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    resp = client.post("/print", json={"template": "cont", "fields": {}, "dry_run": False})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert main_mod._driver.render_payload.call_count == 1


def test_print_snmp_unreachable_fails_open(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNMP unreachable ⇒ fail open: the print proceeds (we don't block on an unverifiable state)."""
    import app.main as main_mod
    from app.transports.snmp import PrinterSNMPStatus

    _write_label_template(main_mod, "diecut", "62x29")  # would mismatch IF we could read media
    _arm_network_snmp(monkeypatch, main_mod, PrinterSNMPStatus.unreachable())
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    before = main_mod.PREFLIGHT_SNMP_UNREACHABLE._value.get()
    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})

    assert resp.status_code == 200, f"Expected 200 (fail-open), got {resp.status_code}: {resp.text}"
    assert main_mod._driver.render_payload.call_count == 1
    # The fail-open path must be observable: a print that skipped the guard increments the counter.
    after = main_mod.PREFLIGHT_SNMP_UNREACHABLE._value.get()
    assert after == before + 1, (
        "an unverified (fail-open) print must increment the SNMP-unreachable counter"
    )


def test_print_printer_fault_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard printer fault (cover open / no media) reported over SNMP refuses the print with 409."""
    import app.main as main_mod
    from app.transports.snmp import PrinterSNMPStatus

    faulted = PrinterSNMPStatus(
        reachable=True,
        media_width_mm=62.0,
        media_type="continuous",
        error_state_bits=0x10,  # a non-zero detected-error mask
        errors=["doorOpen"],
    )
    _write_label_template(main_mod, "cont", "62")  # media itself matches; the fault is the blocker
    _arm_network_snmp(monkeypatch, main_mod, faulted)
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    resp = client.post("/print", json={"template": "cont", "fields": {}, "dry_run": False})

    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert "fault" in detail["msg"].lower()
    assert "doorOpen" in detail["errors"]
    assert main_mod._driver.render_payload.call_count == 0


def test_print_dry_run_skips_snmp_guard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run never reaches the printer, so the guard must not even query SNMP."""
    import app.main as main_mod

    _write_label_template(main_mod, "diecut", "62x29")
    monkeypatch.setattr(main_mod.settings, "printer_uri", "tcp://192.168.5.14:9100")
    monkeypatch.setattr(main_mod.settings, "snmp_enabled", True)

    def _boom() -> object:
        raise AssertionError("SNMP must not be queried for a dry run")

    monkeypatch.setattr(main_mod, "_query_loaded_media", _boom)

    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": True})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_print_snmp_disabled_skips_guard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With SNMP_ENABLED=false (the documented opt-out) the guard is bypassed and a mismatch prints."""
    import app.main as main_mod

    _write_label_template(main_mod, "diecut", "62x29")
    monkeypatch.setattr(main_mod.settings, "printer_uri", "tcp://192.168.5.14:9100")
    monkeypatch.setattr(main_mod.settings, "snmp_enabled", False)
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    def _boom() -> object:
        raise AssertionError("SNMP must not be queried when SNMP is disabled")

    monkeypatch.setattr(main_mod, "_query_loaded_media", _boom)

    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert main_mod._driver.render_payload.call_count == 1


def test_print_non_network_transport_skips_guard(client: TestClient) -> None:
    """The default file:// transport has no SNMP agent, so a die-cut template prints unguarded."""
    import app.main as main_mod

    # client fixture leaves printer_uri as file://… and snmp_enabled at its default.
    _write_label_template(main_mod, "diecut", "62x29")

    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert main_mod._driver.render_payload.call_count == 1


def test_print_media_mismatch_increments_metric(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A media mismatch increments label_errors_total{reason="media_mismatch"}."""
    import app.main as main_mod

    _write_label_template(main_mod, "diecut", "62x29")
    _arm_network_snmp(monkeypatch, main_mod, _loaded_62_continuous())
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    before = main_mod.LABEL_ERRORS.labels(reason="media_mismatch")._value.get()
    resp = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})
    assert resp.status_code == 409
    after = main_mod.LABEL_ERRORS.labels(reason="media_mismatch")._value.get()
    assert after == before + 1, "media_mismatch metric must increment on a rejected print"


def test_reprint_media_mismatch_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved job replayed against a printer now loaded with mismatching media is refused with 409.

    First print the die-cut template over the unguarded file:// transport to seed history, then
    switch to a network printer reporting 62mm continuous loaded and reprint — the guard fires.
    """
    import app.main as main_mod

    _write_label_template(main_mod, "diecut", "62x29")
    seed = client.post("/print", json={"template": "diecut", "fields": {}, "dry_run": False})
    assert seed.status_code == 200, f"seed print failed: {seed.text}"
    job_id = seed.json()["job_id"]

    _arm_network_snmp(monkeypatch, main_mod, _loaded_62_continuous())
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    resp = client.post(f"/reprint/{job_id}")

    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    assert resp.json()["detail"]["label"] == "62x29"


def test_reprint_media_match_prints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A saved continuous job replayed against the matching loaded roll reprints successfully."""
    import app.main as main_mod

    _write_label_template(main_mod, "cont", "62")
    seed = client.post("/print", json={"template": "cont", "fields": {}, "dry_run": False})
    assert seed.status_code == 200, f"seed print failed: {seed.text}"
    job_id = seed.json()["job_id"]

    _arm_network_snmp(monkeypatch, main_mod, _loaded_62_continuous())
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _SilentNetworkTransport)

    resp = client.post(f"/reprint/{job_id}")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
