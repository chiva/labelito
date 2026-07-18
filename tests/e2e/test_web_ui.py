# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end web UI checks — a real browser driving the page at app/web/index.html.

These exercise the same flows a human (or an AI agent) would: load the page, pick a template, fill
its fields, preview, print (dry-run), and confirm the auth-required path. The token is pre-seeded
into localStorage by the ``authed_page`` fixture, mirroring how the dev harness opens the page.
"""

from __future__ import annotations

import base64
import re

import pytest
from harness import DEFAULT_API_TOKEN, web_token_init_script

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Locator, Page, expect

pytestmark = pytest.mark.e2e

# A shipped template with plain text fields (templates/62-title-subtitle.yaml, name: title-subtitle)
# — stable to drive the UI
# without needing an image upload or QR/barcode payload.
SAMPLE_TEMPLATE = "title-subtitle"

# A shipped template with an `image` layout element bound to the `image` field
# (templates/62-image.yaml, name: image) — used to drive the file-upload UI.
IMAGE_TEMPLATE = "image"

# A shipped template that uses the {{seq}} auto-numbering token (templates/62-numbered-bin.yaml,
# name: numbered-bin) — drives the sequence (batch) controls on the Print page.
SEQ_TEMPLATE = "numbered-bin"


def _png_bytes(color: int, size: tuple[int, int] = (4, 4)) -> bytes:
    """A distinct grayscale PNG so two uploads have different base64 payloads."""
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("L", size, color).save(buf, format="PNG")
    return buf.getvalue()


# A valid 1x1 white PNG, for fulfilling held /preview routes with a real (but recognizable —
# naturalWidth 1) image body.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _fill_all_fields(page: Page, value: str = "E2E test") -> None:
    """Fill every field input the current template renders into #fields-container."""
    inputs = page.locator("#fields-container input")
    expect(inputs.first).to_be_visible()
    for i in range(inputs.count()):
        inputs.nth(i).fill(value)


def _select_template(page: Page, name: str) -> None:
    """Pick a template by clicking its card in the grouped picker (the old <select> equivalent)."""
    page.locator(f'.tpl-card[data-name="{name}"]').click()


def _selected_template(page: Page) -> str:
    """The currently selected template name, via the page's global accessor."""
    return page.evaluate("() => currentTemplate().name")


def _group_labels(page: Page) -> list[str]:
    """The rendered size-group titles, in picker order."""
    return page.eval_on_selector_all(
        "#template-groups .group-label", "els => els.map(e => e.textContent)"
    )


def test_page_loads_and_lists_templates(authed_page: Page) -> None:
    authed_page.goto("/")
    expect(authed_page).to_have_title("labelito")
    cards = authed_page.locator("#template-groups .tpl-card")
    assert cards.count() > 0, "template picker should be populated from the shipped templates"


def test_select_template_renders_fields_and_previews(authed_page: Page) -> None:
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)

    authed_page.click("button.btn-preview")

    # The preview card becomes visible and the <img> is populated with a blob: URL of the PNG.
    expect(authed_page.locator("#preview-section")).to_be_visible()
    # Wait until the image has actually decoded real pixels (not just a src attribute).
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )
    src = authed_page.locator("#preview-img").get_attribute("src")
    assert src and src.startswith("blob:"), f"preview image should be a rendered blob, got {src!r}"
    # No error banner.
    assert authed_page.locator(".status.err").count() == 0


def test_preview_dims_shows_size_and_media_on_two_rows(authed_page: Page) -> None:
    """The preview card head splits the dimensions readout into two stacked rows — the pixel size
    and the media description — rather than one combined `WxHpx · media` line."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.click("button.btn-preview")
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )
    size_text = authed_page.locator("#preview-dims-size").inner_text()
    media_text = authed_page.locator("#preview-dims-media").inner_text()
    assert re.match(r"^\d+\u00d7\d+px$", size_text), f"unexpected size row: {size_text!r}"
    assert media_text, "expected a non-empty media description on the second row"


def test_print_dry_run_round_trip(authed_page: Page) -> None:
    """Print (dry-run) from the UI and assert the /print round-trip succeeds.

    Two things are checked: the network response from /print, and the on-page success banner.
    The banner is now sticky/persistent — doPrint() renders it with ``{sticky: true}``, and the
    post-print doPreview() refresh deliberately does NOT clear a sticky banner — so ".status.ok"
    stays visible (until the x button or its ~8s auto-dismiss fires) rather than racing away.
    """
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.check("#dry-run")

    with authed_page.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("button.btn-print")

    response = resp_info.value
    assert response.status == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["template"] == SAMPLE_TEMPLATE
    assert body["job_id"]

    # The sticky success banner survives the post-print preview refresh.
    expect(authed_page.locator(".status.ok")).to_be_visible()


def test_seq_template_shows_sequence_controls_and_hides_copies(authed_page: Page) -> None:
    """Selecting a {{seq}} template swaps the Copies stepper for the Auto-number (sequence) panel —
    the two are mutually exclusive server-side, so the UI never shows both."""
    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    expect(authed_page.locator("#sequence-field")).to_be_visible()
    expect(authed_page.locator("#copies-field")).to_be_hidden()
    # Switching back to a plain template restores Copies and hides the sequence panel.
    _select_template(authed_page, SAMPLE_TEMPLATE)
    expect(authed_page.locator("#copies-field")).to_be_visible()
    expect(authed_page.locator("#sequence-field")).to_be_hidden()


def test_seq_template_previews_first_item_without_error(authed_page: Page) -> None:
    """A {{seq}} template previews the first item from the UI (the Print page sends the auto-number
    controls as a `sequence` on /preview) — no 422, and the preview image renders. Fields are filled
    first: a preview with an empty required field 422s on the missing field, unrelated to sequence."""
    import json as _json

    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)

    with authed_page.expect_response(
        lambda r: r.url.endswith("/preview") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("button.btn-preview")

    response = resp_info.value
    sent = _json.loads(response.request.post_data or "{}")
    assert sent.get("sequence"), "the preview payload must carry the sequence spec"
    assert sent["copies"] == 1, "copies is pinned to 1 for a sequence template"
    assert response.status == 200, f"seq preview must not 422 with fields filled: {response.status}"
    expect(authed_page.locator("#preview-section")).to_be_visible()
    assert authed_page.locator(".status.err").count() == 0


def test_seq_template_print_sends_sequence_spec(authed_page: Page) -> None:
    """Printing a {{seq}} template (dry-run) sends a `sequence` object built from the Auto-number
    controls, with copies pinned to 1 — the batch path the API-only feature now exposes in the UI."""
    import json as _json

    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.fill("#seq-count", "5")
    authed_page.fill("#seq-start", "10")
    authed_page.fill("#seq-padding", "3")
    authed_page.check("#dry-run")

    with authed_page.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("button.btn-print")

    response = resp_info.value
    assert response.status == 200, f"Expected 200, got {response.status}"
    sent = _json.loads(response.request.post_data or "{}")
    assert sent["sequence"] == {"start": 10, "count": 5, "step": 1, "padding": 3}
    assert sent["copies"] == 1, "copies must be pinned to 1 (mutually exclusive with sequence)"
    body = response.json()
    assert body["dry_run"] is True
    assert body["template"] == SEQ_TEMPLATE
    expect(authed_page.locator(".status.ok")).to_be_visible()


def test_seq_double_submit_prints_only_one_batch(authed_page: Page) -> None:
    """A double-click / re-entrant submit during an in-flight sequence print must NOT queue a second
    batch — the in-flight guard disables the button and doPrint early-returns. Without it a slow
    500-label batch could be duplicated by an impatient second click, wasting a whole roll.

    Deterministic because JS is single-threaded: the first doPrint() sets printInFlight synchronously
    before it awaits fetch, so the second call runs fully (and hits the guard) before the first's
    request resolves. Asserts exactly one /print reaches the network."""
    import json as _json

    seen: list[str] = []

    def handle(route: object) -> None:
        seen.append(route.request.post_data)  # type: ignore[attr-defined]
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_json.dumps(
                {"job_id": "j1", "template": SEQ_TEMPLATE, "copies": 1, "dry_run": True}
            ),
        )

    authed_page.route("**/print", handle)
    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.fill("#seq-count", "500")
    authed_page.check("#dry-run")

    # Two synchronous doPrint() calls: the first suspends at its awaited fetch with printInFlight set;
    # the second must early-return. Capture whether the button was disabled between them.
    disabled_mid = authed_page.evaluate(
        "() => { doPrint();"
        " const d = document.querySelector('.btn-print').disabled;"
        " doPrint(); return d; }"
    )
    assert disabled_mid is True, "the Print button must disable while a print is in flight"
    authed_page.wait_for_timeout(300)
    assert len(seen) == 1, f"a re-entrant submit must not queue a second batch (saw {len(seen)})"
    assert _json.loads(seen[0]).get("sequence"), "the one request must carry the sequence spec"
    # The guard releases after the request settles so the next print can proceed.
    expect(authed_page.locator("button.btn-print")).to_be_enabled()
    assert authed_page.evaluate("() => printInFlight") is False


def test_seq_retry_after_network_error_reuses_idempotency_key(authed_page: Page) -> None:
    """After a /print network error (ambiguous — the server may have printed), an identical resubmit
    must reuse the SAME idempotency_key so the server dedups it instead of printing a second batch.
    A later intentional repeat (after a definitive success) must get a FRESH key so it still prints."""
    import json as _json

    keys: list[str] = []
    state = {"fail_next": True}

    def handle(route: object) -> None:
        keys.append(_json.loads(route.request.post_data).get("idempotency_key"))  # type: ignore[attr-defined]
        if state["fail_next"]:
            state["fail_next"] = False
            route.abort("failed")  # type: ignore[attr-defined]  # network error → doPrint catch
        else:
            route.fulfill(  # type: ignore[attr-defined]
                status=200,
                content_type="application/json",
                body=_json.dumps(
                    {"job_id": "j", "template": SEQ_TEMPLATE, "copies": 1, "dry_run": True}
                ),
            )

    authed_page.route("**/print", handle)
    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.fill("#seq-count", "50")
    authed_page.check("#dry-run")

    # Attempt 1 → aborted (network error); the key + payload are retained for retry.
    authed_page.click("button.btn-print")
    expect(authed_page.locator(".status.err")).to_be_visible()
    expect(authed_page.locator("button.btn-print")).to_be_enabled()

    # Attempt 2 → identical payload → reuses the retained key → server would dedup.
    authed_page.click("button.btn-print")
    expect(authed_page.locator(".status.ok")).to_be_visible()

    # Attempt 3 → same content but a definitive success cleared the retry state → fresh key so an
    # intentional repeat still prints.
    authed_page.click("button.btn-print")
    authed_page.wait_for_timeout(300)

    assert len(keys) == 3, f"expected 3 /print attempts, saw {len(keys)}"
    assert keys[0] and keys[0] == keys[1], "a network-error retry must reuse the idempotency key"
    assert keys[2] and keys[2] != keys[1], (
        "an intentional repeat after success must use a fresh key"
    )


def test_seq_large_print_confirms_before_printing(authed_page: Page) -> None:
    """A non-dry-run sequence batch at/above the confirm threshold must ask via the in-page <dialog>
    — NOT native confirm(), which Chromium auto-accepts under Enter-key activation, waving through an
    irreversible physical batch of up to 500 labels. Cancel → no /print request; confirm ("Print") →
    exactly one batch prints. Mirrors the studio's test_studio_large_seq_print_confirms_via_dialog."""
    import json as _json

    prints: list[str] = []

    def handle(route: object) -> None:
        prints.append(route.request.post_data)  # type: ignore[attr-defined]
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_json.dumps(
                {"job_id": "j", "template": SEQ_TEMPLATE, "copies": 1, "dry_run": False}
            ),
        )

    authed_page.route("**/print", handle)

    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.fill("#seq-count", "25")
    if authed_page.is_checked("#dry-run"):
        authed_page.uncheck("#dry-run")  # a real (non-dry-run) batch triggers the confirm

    # The in-page dialog opens, naming the count + range, with the caller's "Print" label.
    authed_page.click("button.btn-print")
    dlg = authed_page.locator("#confirm-dialog")
    expect(dlg).to_be_visible()
    expect(dlg.locator("#confirm-message")).to_contain_text("25")
    expect(dlg.locator("#confirm-message")).to_contain_text("1..25")
    expect(dlg.locator("#confirm-ok")).to_have_text("Print")

    # Cancel → nothing printed.
    dlg.locator("#confirm-cancel").click()
    expect(dlg).to_be_hidden()
    authed_page.wait_for_timeout(200)
    assert len(prints) == 0, "cancelling the confirm must not print"

    # Confirm → the batch prints exactly once.
    authed_page.click("button.btn-print")
    expect(dlg).to_be_visible()
    dlg.locator("#confirm-ok").click()
    authed_page.wait_for_timeout(300)
    assert len(prints) == 1, "confirming must print exactly one batch"
    sent = _json.loads(prints[0] or "{}")
    assert sent["sequence"]["count"] == 25


def test_seq_retry_after_reload_reuses_idempotency_key(authed_page: Page) -> None:
    """The ambiguous-failure retry key survives a page reload (sessionStorage): a network error, then
    a reload, then an identical resubmit must reuse the SAME idempotency_key so the server dedups —
    the exact failure mode the key exists for. Uses dry-run + a small count to skip the confirm."""
    import json as _json

    keys: list[str] = []
    state = {"fail_next": True}

    def handle(route: object) -> None:
        keys.append(_json.loads(route.request.post_data).get("idempotency_key"))  # type: ignore[attr-defined]
        if state["fail_next"]:
            state["fail_next"] = False
            route.abort("failed")  # type: ignore[attr-defined]
        else:
            route.fulfill(  # type: ignore[attr-defined]
                status=200,
                content_type="application/json",
                body=_json.dumps(
                    {"job_id": "j", "template": SEQ_TEMPLATE, "copies": 1, "dry_run": True}
                ),
            )

    authed_page.route("**/print", handle)
    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.fill("#seq-count", "5")
    authed_page.check("#dry-run")

    # Attempt 1 → aborted; retry key + payload persisted to sessionStorage.
    authed_page.click("button.btn-print")
    expect(authed_page.locator(".status.err")).to_be_visible()

    # Reload — JS globals reset, but sessionStorage (and the restored form) survive.
    authed_page.reload()
    expect(authed_page.locator("#template-groups")).to_be_visible()
    expect(authed_page.locator("button.btn-print")).to_be_enabled()

    # Attempt 2 (post-reload, identical payload) → reuses the persisted key.
    authed_page.click("button.btn-print")
    expect(authed_page.locator(".status.ok")).to_be_visible()

    assert len(keys) == 2, f"expected 2 /print attempts, saw {len(keys)}"
    assert keys[0] and keys[0] == keys[1], (
        "a retry after reload must reuse the persisted idempotency key"
    )


def test_seq_edit_marks_form_dirty_like_a_field_edit(authed_page: Page) -> None:
    """Editing an auto-number control is a new in-progress choice (the number is label content), so
    it must set userOverride and bump formRevision — the same dirty-state a field edit raises. Without
    it, a /print completing while the operator edits the next batch could clear the guard and let a
    roll-driven refocus replace the template out from under the newer edits."""
    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)

    # Baseline after selecting + filling: capture the revision, then reset the guard as a completed
    # print would (userOverride=false), and confirm a seq edit re-raises it.
    rev_before = authed_page.evaluate("() => formRevision")
    authed_page.evaluate("() => { userOverride = false; }")
    authed_page.fill("#seq-start", "7")

    assert authed_page.evaluate("() => userOverride") is True, (
        "a sequence edit must re-raise userOverride"
    )
    assert authed_page.evaluate("() => formRevision") > rev_before, (
        "a sequence edit must bump formRevision"
    )


def test_seq_inputs_normalize_on_commit_not_while_typing(authed_page: Page) -> None:
    """Print-page auto-number inputs must not be rewritten mid-keystroke (a lone '-' used to snap to
    the default, making a negative start un-typeable), while the /print payload stays valid — the
    same deferral the studio uses. Clamping happens on `change` (blur/Enter/spinner), not `input`."""
    authed_page.goto("/")
    _select_template(authed_page, SEQ_TEMPLATE)
    _fill_all_fields(authed_page)
    start = authed_page.locator("#seq-start")
    count = authed_page.locator("#seq-count")

    # Clearing a field stays empty on the input path; the payload still reads a clamped value.
    count.fill("")
    assert count.input_value() == "", "clearing a field must not snap to the default on input"
    assert authed_page.evaluate("() => currentSequenceSpec().count") == 10
    assert count.input_value() == "", "reading the spec must not rewrite the field"
    count.dispatch_event("change")
    assert count.input_value() == "10", "a blank field defaults on commit"

    # A negative start is now typeable (a lone '-' is no longer wiped on input).
    start.fill("")
    start.focus()
    authed_page.keyboard.type("-5")
    assert start.input_value() == "-5", "a negative start must be typeable (not clamped mid-entry)"
    start.dispatch_event("change")
    assert start.input_value() == "-5", "an in-bounds negative start is preserved on commit"


def test_image_field_renders_file_picker_uploads_and_prints(authed_page: Page) -> None:
    """The Print page renders a file picker (not a text input) for an image field, and an uploaded
    image previews and rides into the /print payload as base64 in fields.image."""
    import json as _json

    authed_page.goto("/")
    _select_template(authed_page, IMAGE_TEMPLATE)

    # The image field is a (hidden) file input inside the drop-target widget, keeping the shared
    # id="field-<name>" convention — NOT a text input.
    file_input = authed_page.locator("#field-image")
    expect(file_input).to_have_attribute("type", "file")
    expect(authed_page.locator(".image-dropzone")).to_be_visible()

    # Uploading a file fires the change handler → base64-caches the image and auto-previews.
    file_input.set_input_files(
        files=[{"name": "logo.png", "mimeType": "image/png", "buffer": PNG_1PX}]
    )
    expect(authed_page.locator(".image-dropzone.has-image")).to_be_visible()
    expect(authed_page.locator(".image-filename")).to_have_text("logo.png")

    # The preview renders a real (decoded) image.
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )

    # A dry-run print carries the base64 image under the template's image field.
    authed_page.check("#dry-run")
    with authed_page.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("button.btn-print")

    response = resp_info.value
    assert response.status == 200
    payload = _json.loads(response.request.post_data)
    assert payload["template"] == IMAGE_TEMPLATE
    assert payload["fields"].get("image"), "print payload should carry base64 image bytes"


def test_image_field_clear_removes_cached_image(authed_page: Page) -> None:
    """The clear button drops the chosen image so the widget returns to its empty prompt."""
    authed_page.goto("/")
    _select_template(authed_page, IMAGE_TEMPLATE)
    authed_page.locator("#field-image").set_input_files(
        files=[{"name": "logo.png", "mimeType": "image/png", "buffer": PNG_1PX}]
    )
    expect(authed_page.locator(".image-dropzone.has-image")).to_be_visible()
    authed_page.locator(".image-clear").click()
    expect(authed_page.locator(".image-dropzone.has-image")).to_have_count(0)
    expect(authed_page.locator(".image-prompt")).to_be_visible()


def test_image_field_edit_then_reload_initializes_cleanly(authed_page: Page) -> None:
    """Regression: an image field must never be persisted as a text value.

    After choosing an image and editing the sibling text field, saveFields used to snapshot the file
    input's fake "C:\\fakepath\\…" path under the image field name. On the next load restoreSavedFields
    assigned that non-empty string back to the file input, which throws InvalidStateError and aborts
    the inline init (options/picker/preview never wire up). The page must reload cleanly and restore
    the text field.
    """
    errors: list[str] = []
    authed_page.on("pageerror", lambda exc: errors.append(str(exc)))

    authed_page.goto("/")
    _select_template(authed_page, IMAGE_TEMPLATE)
    authed_page.locator("#field-image").set_input_files(
        files=[{"name": "logo.png", "mimeType": "image/png", "buffer": PNG_1PX}]
    )
    # Editing the optional title fires saveFields — the moment a bad fake path would be persisted.
    authed_page.fill("#field-title", "Reload me")

    # Reload: init runs restoreSavedFields synchronously against the persisted snapshot.
    authed_page.goto("/")

    assert errors == [], f"page init raised: {errors}"
    # Init completed: the image field re-rendered as a file input and the text value was restored.
    expect(authed_page.locator("#field-image")).to_have_attribute("type", "file")
    expect(authed_page.locator("#field-title")).to_have_value("Reload me")


def test_image_field_replace_uses_last_pick(authed_page: Page) -> None:
    """Replacing an image before printing uses the latest pick, not a stale one.

    Guards the read-generation logic that discards a superseded FileReader completion: two picks in
    succession must leave the widget and the /print payload carrying the SECOND image, never the first.
    """
    import json as _json

    authed_page.goto("/")
    _select_template(authed_page, IMAGE_TEMPLATE)

    first, second = _png_bytes(0, (16, 16)), _png_bytes(255, (4, 4))
    fi = authed_page.locator("#field-image")
    fi.set_input_files(files=[{"name": "first.png", "mimeType": "image/png", "buffer": first}])
    fi.set_input_files(files=[{"name": "second.png", "mimeType": "image/png", "buffer": second}])

    expect(authed_page.locator(".image-filename")).to_have_text("second.png")

    authed_page.check("#dry-run")
    with authed_page.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("button.btn-print")
    payload = _json.loads(resp_info.value.request.post_data)
    assert payload["fields"]["image"] == base64.b64encode(second).decode(), (
        "the last-picked image must win"
    )


def test_print_disabled_while_image_reading(authed_page: Page) -> None:
    """Print is disabled while an image read is in flight, then re-enabled once it commits.

    Disabling the button (rather than awaiting inside doPrint) keeps the print snapshot synchronous:
    the payload is exactly the click-time state, and a print can never be built from a half-loaded
    image cache (which would submit the label without the just-chosen image). Uses a deferred
    FileReader so the read is controllable.
    """
    import json as _json

    authed_page.add_init_script(
        """
        window.__frQueue = [];
        window.__flushFR = () => { const q = window.__frQueue.splice(0); q.forEach(fn => fn()); };
        const orig = FileReader.prototype.readAsDataURL;
        FileReader.prototype.readAsDataURL = function (blob) {
          window.__frQueue.push(() => orig.call(this, blob));
        };
        """
    )
    authed_page.goto("/")
    _select_template(authed_page, IMAGE_TEMPLATE)
    authed_page.locator("#field-image").set_input_files(
        files=[{"name": "logo.png", "mimeType": "image/png", "buffer": PNG_1PX}]
    )
    authed_page.check("#dry-run")

    # Read pending → Print disabled.
    expect(authed_page.locator("button.btn-print")).to_be_disabled()

    # Release the read → Print re-enabled, and printing now carries the committed image.
    authed_page.evaluate("window.__flushFR()")
    expect(authed_page.locator("button.btn-print")).to_be_enabled()
    with authed_page.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("button.btn-print")
    payload = _json.loads(resp_info.value.request.post_data)
    assert payload["fields"].get("image"), "print after the read must carry the image"


def test_image_pick_survives_late_status_refocus(authed_page_snmp: Page) -> None:
    """Choosing an image marks the form user-edited synchronously, so a background /printer/status
    poll landing DURING the (async) read cannot refocus the picker and discard the just-chosen image.

    Setup: pre-seed sessionStorage so the image template is restored as the selection with
    userOverride still false (a submitted-choice restore skips the guard), and defer the FileReader so
    the read is in flight when the roll becomes known.
    """
    # Restore the image template as an already-submitted choice → selected but userOverride stays
    # false; and defer readAsDataURL so the read is controllable.
    authed_page_snmp.add_init_script(
        """
        sessionStorage.setItem('labelito_template', 'image');
        sessionStorage.setItem('labelito_choice_submitted', '1');
        window.__frQueue = [];
        window.__flushFR = () => { const q = window.__frQueue.splice(0); q.forEach(fn => fn()); };
        const orig = FileReader.prototype.readAsDataURL;
        FileReader.prototype.readAsDataURL = function (blob) {
          window.__frQueue.push(() => orig.call(this, blob));
        };
        """
    )
    phase = {"reachable": False}

    def handle(route: object) -> None:
        if phase["reachable"]:
            # A DIFFERENT roll (29mm) than the image template's 62mm — would refocus if unguarded.
            body = _status_body(media_width_mm=29, media_type="continuous", media_length_mm=None)
        else:
            body = (
                '{"state": "off", "uri": "tcp://192.0.2.10:9100", "reachable": false, "errors": []}'
            )
        route.fulfill(status=200, content_type="application/json", body=body)  # type: ignore[attr-defined]

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    assert _selected_template(authed_page_snmp) == IMAGE_TEMPLATE
    # Pick an image (read stays queued) — onSelect marks the form user-edited synchronously.
    authed_page_snmp.locator("#field-image").set_input_files(
        files=[{"name": "logo.png", "mimeType": "image/png", "buffer": PNG_1PX}]
    )

    # Roll becomes known mid-read; force the refresh the background poll would do.
    phase["reachable"] = True
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")

    # The guard held: template unchanged and the image survives once the read is released.
    assert _selected_template(authed_page_snmp) == IMAGE_TEMPLATE, (
        "a late status refocus must not switch away from the image template after a pick"
    )
    authed_page_snmp.evaluate("window.__flushFR()")
    committed = authed_page_snmp.evaluate(
        "() => collectImageFields((currentTemplate().image_fields) || [])"
    )
    assert committed.get("image"), "the chosen image must survive a mid-read status refocus"


def test_invalid_replacement_cancels_pending_image_read(authed_page: Page) -> None:
    """A rejected replacement pick cancels an older in-flight read.

    Pick valid image A (read deferred), then pick invalid B (rejected). When A's deferred read is
    finally released it must NOT commit — the invalid pick superseded it — so no stale image A can
    ride into a later print.
    """
    authed_page.add_init_script(
        """
        window.__frQueue = [];
        window.__flushFR = () => { const q = window.__frQueue.splice(0); q.forEach(fn => fn()); };
        const orig = FileReader.prototype.readAsDataURL;
        FileReader.prototype.readAsDataURL = function (blob) {
          window.__frQueue.push(() => orig.call(this, blob));
        };
        """
    )
    authed_page.goto("/")
    _select_template(authed_page, IMAGE_TEMPLATE)

    fi = authed_page.locator("#field-image")
    # Valid A — its read is queued (deferred), not yet committed.
    fi.set_input_files(
        files=[{"name": "A.png", "mimeType": "image/png", "buffer": _png_bytes(0, (16, 16))}]
    )
    # Invalid B (wrong type) — rejected synchronously, but must supersede A's pending read.
    fi.set_input_files(
        files=[{"name": "B.txt", "mimeType": "text/plain", "buffer": b"not an image"}]
    )
    expect(authed_page.locator(".status.err")).to_be_visible()

    # Release A's read; the generation bumped by B must cause it to be discarded.
    authed_page.evaluate("window.__flushFR()")
    committed = authed_page.evaluate(
        "() => collectImageFields((currentTemplate().image_fields) || [])"
    )
    assert committed == {}, f"a superseded image must not commit, got fields {list(committed)}"


def test_studio_undeclared_image_field_renders_picker(authed_page: Page) -> None:
    """An image element whose field is NOT in fields.required/optional is still fillable in the Studio.

    The loader accepts such a template (an image `field` is not a {{token}}), so the field renderer
    must render a picker for it — otherwise the image is unfillable and the draft previews blank.
    """
    authed_page.goto("/editor")
    expect(authed_page.locator("#yaml")).to_be_visible()
    yaml = (
        "name: draft-undeclared\n"
        "description: image field not declared in fields\n"
        'label: "62"\n'
        "rotate: 0\n"
        "fields:\n"
        "  required: []\n"
        "  optional: []\n"
        "layout:\n"
        "  - {type: image, field: photo}\n"
    )
    authed_page.fill("#yaml", yaml)
    expect(authed_page.locator("#field-photo")).to_have_attribute("type", "file")


def test_print_page_background_poll_converges_status_badge(authed_page_snmp: Page) -> None:
    """On an SNMP deployment (live_status_poll ON) the print page polls /printer/status on a
    visible-tab background timer, so the status badge converges to the printer's real state with no
    manual ↻. Serve state=printing on the first call and state=idle on every call after; assert the
    badge ends at Idle on its own and the poll fired more than once (the background timer, not just
    the single init fetch)."""
    import json

    calls = {"n": 0}

    def handle(route: object) -> None:
        calls["n"] += 1
        state = "printing" if calls["n"] == 1 else "idle"
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "state": state,
                    "uri": "tcp://192.0.2.10:9100",
                    "reachable": True,
                    "model": "Brother QL-810W",
                    "errors": [],
                }
            ),
        )

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    # No click, no ↻ — the badge must reach Idle purely via a later background poll.
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Idle", timeout=15000)
    assert calls["n"] >= 2, (
        f"expected the background poll to fire at least twice (init + a timed poll); got {calls['n']}"
    )


def test_print_page_does_not_background_poll_without_snmp(authed_page: Page) -> None:
    """On the non-SNMP (file:// / ESC i S) deployment, background polling must be OFF: there the
    status read takes the print lock, so a timer poll would risk delaying a print. Count /printer/status
    hits — only the single init fetch may fire; no further hits after waiting well past a poll interval.
    Guards against unconditional polling reintroducing lock contention."""
    calls = {"n": 0}

    def handle(route: object) -> None:
        calls["n"] += 1
        route.fulfill(  # type: ignore[attr-defined]
            status=503,
            content_type="application/json",
            body='{"state": "off", "uri": "file:///dev/null", "reachable": false, "errors": []}',
        )

    authed_page.route("**/printer/status", handle)
    authed_page.goto("/")
    # Let any (incorrectly scheduled) background timer fire — the base interval is ~4s.
    authed_page.wait_for_timeout(9000)
    assert calls["n"] <= 1, (
        f"non-SNMP deployment must not background-poll; got {calls['n']} /printer/status hits"
    )


def test_print_page_status_poll_recovers_from_hung_fetch(authed_page_snmp: Page) -> None:
    """A hung /printer/status fetch must not wedge the UI: the client-side abort timeout fires, resets
    the in-flight guard, and the badge resolves to Unreachable instead of freezing forever. A hung request must not pin
    statusInFlight and stall the whole poll loop."""
    # Never fulfil the request — the browser fetch hangs until the page's AbortController aborts it.
    authed_page_snmp.route("**/printer/status", lambda route: None)
    authed_page_snmp.goto("/")
    # STATUS_FETCH_TIMEOUT_MS is 8s; allow margin. Freezing (the bug) would leave the badge unresolved.
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Unreachable", timeout=12000)


def _status_body(**media: object) -> str:
    """A /printer/status JSON body for a reachable network printer with the given loaded media."""
    import json

    return json.dumps(
        {
            "state": "idle",
            "uri": "tcp://192.0.2.10:9100",
            "reachable": True,
            "model": "Brother QL-810W",
            "errors": [],
            **media,
        }
    )


def test_print_page_groups_templates_by_size(authed_page: Page) -> None:
    """With the loaded roll unknown (non-SNMP / file:// deployment), the picker still groups templates
    into .tpl-group sections by size denomination and shows every group — there's nothing to filter
    against, so no group is focused and the size-filter control stays hidden."""
    authed_page.goto("/")
    groups = authed_page.locator("#template-groups .tpl-group")
    # The shipped catalog spans several sizes (12/29/62mm continuous + 17x54/29x90/62x29 die-cut).
    expect(groups).not_to_have_count(0)
    labels = _group_labels(authed_page)
    assert any("62mm continuous" in label for label in labels), labels
    assert len(labels) >= 4, f"expected templates grouped across several sizes, got {labels}"
    # Unknown roll → nothing focused, so no loaded-roll marker and the show-all control is hidden.
    expect(authed_page.locator("#template-groups .tpl-group[data-match]")).to_have_count(0)
    expect(authed_page.locator("#size-filter")).to_be_hidden()


def test_print_page_focuses_matching_size_group(authed_page_snmp: Page) -> None:
    """When the loaded roll is known (SNMP), the picker FOCUSES the matching size group and collapses
    the rest behind a "Show all sizes" toggle — so a 62mm roll surfaces only 62mm templates. Clicking
    the toggle reveals every size again."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62, media_type="continuous", media_length_mm=None),
        ),
    )
    authed_page_snmp.goto("/")

    groups = authed_page_snmp.locator("#template-groups .tpl-group")
    # Focus mode: only the matching 62mm continuous group remains, marked as the loaded roll.
    expect(groups).to_have_count(1, timeout=8000)
    expect(groups.first.locator(".group-label")).to_have_text("62mm continuous")
    expect(groups.first).to_have_attribute("data-match", "1")
    expect(groups.first.locator(".roll-pill")).to_have_text("loaded roll")
    # The hidden-count hint and the reveal toggle are shown.
    expect(authed_page_snmp.locator("#size-filter")).to_be_visible()
    expect(authed_page_snmp.locator("#size-filter-hint")).to_contain_text("hidden")
    toggle = authed_page_snmp.locator("#size-filter-toggle")
    expect(toggle).to_have_text("Show all sizes")

    toggle.click()
    # Show-all: every size group reappears (the 62mm one stays marked as the loaded roll).
    labels = _group_labels(authed_page_snmp)
    assert len(labels) >= 4, f"show-all should reveal every size group, got {labels}"
    expect(
        authed_page_snmp.locator("#template-groups .tpl-group[data-match] .group-label")
    ).to_have_text("62mm continuous")


def test_size_group_mismatch_marker_on_header_not_per_card(authed_page_snmp: Page) -> None:
    """A size group that doesn't match the loaded roll is flagged ONCE on its header (an amber
    '≠ loaded roll' pill) in show-all mode — replacing the old, redundant per-card 'needs …' note that
    repeated identically on every card in the group."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62, media_type="continuous", media_length_mm=None),
        ),
    )
    authed_page_snmp.goto("/")
    # Reveal every size group (focus mode collapses the non-matching ones by default).
    toggle = authed_page_snmp.locator("#size-filter-toggle")
    expect(toggle).to_be_visible(timeout=8000)
    toggle.click()

    # A non-matching group carries the amber marker on its header…
    non_match = authed_page_snmp.locator("#template-groups .tpl-group:not([data-match])").first
    expect(non_match.locator(".tpl-group-head .roll-pill")).to_have_text("≠ loaded roll")
    # …the matching group keeps the green "loaded roll" pill…
    expect(
        authed_page_snmp.locator("#template-groups .tpl-group[data-match] .roll-pill")
    ).to_have_text("loaded roll")
    # …and the old per-card note is gone entirely.
    assert authed_page_snmp.locator(".tpl-card .tpl-needs").count() == 0


def test_media_pill_shows_tick_and_loaded_media_type(authed_page_snmp: Page) -> None:
    """The single Media pill reads ✓/✗ + the ACTUAL loaded roll — the loaded media type lives only
    here, never duplicated as a separate detail line."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62, media_type="continuous", media_length_mm=None),
        ),
    )
    authed_page_snmp.goto("/")
    badge = authed_page_snmp.locator("#media-badge")
    # Focus mode auto-selects a matching 62mm template → green ✓ + the loaded roll's description.
    expect(badge).to_have_class(re.compile(r"media-ok"), timeout=8000)
    expect(badge).to_contain_text("✓")
    expect(badge).to_contain_text("62mm continuous")


def test_print_page_refocuses_when_media_changes_mid_session(authed_page_snmp: Page) -> None:
    """A roll swap AFTER the page is open is detected by the background poll (SNMP deployments): the
    picker re-focuses to the newly-loaded size and lands on a usable template, with no page reload or
    manual ↻. Serve 62mm first, then 29mm on later polls; assert the focused group follows the roll."""
    calls = {"n": 0}

    def handle(route: object) -> None:
        calls["n"] += 1
        # First (init) fetch = 62mm; every later background poll = 29mm (the swapped-in roll).
        width = 62 if calls["n"] == 1 else 29
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=width, media_type="continuous", media_length_mm=None),
        )

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    groups = authed_page_snmp.locator("#template-groups .tpl-group")
    match_label = authed_page_snmp.locator("#template-groups .tpl-group[data-match] .group-label")
    expect(groups).to_have_count(1, timeout=8000)
    expect(match_label).to_have_text("62mm continuous")

    # No reload, no ↻ — a later background poll sees the new 29mm roll and re-focuses on its own.
    expect(match_label).to_have_text("29mm continuous", timeout=15000)
    assert _selected_template(authed_page_snmp) == "simple-text-29", (
        "the roll swap should land the selection on a 29mm template"
    )


def test_late_status_does_not_discard_typed_input(authed_page_snmp: Page) -> None:
    """A slow /printer/status reply must not silently change the template or wipe entered values. The
    roll-driven refocus only lands a fresh page on a usable template — once the user has typed, a late
    status arrival (the roll becoming known) must keep their selection and their input — auto-refocus
    must not discard in-progress form data."""
    phase = {"reachable": False}

    def handle(route: object) -> None:
        if phase["reachable"]:
            body = _status_body(media_width_mm=62, media_type="continuous", media_length_mm=None)
        else:
            # Roll unknown at first (reachable=false) → no focus, initial selection stands.
            body = (
                '{"state": "off", "uri": "tcp://192.0.2.10:9100", "reachable": false, "errors": []}'
            )
        route.fulfill(status=200, content_type="application/json", body=body)  # type: ignore[attr-defined]

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    template_before = _selected_template(authed_page_snmp)
    field = authed_page_snmp.locator("#fields-container input").first
    expect(field).to_be_visible()
    field.fill("DONOTLOSE")  # fires 'input' → marks the form touched

    # The roll becomes known late; force the refresh the background poll would do.
    phase["reachable"] = True
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")

    # Late status must NOT change the template or discard the typed value.
    assert _selected_template(authed_page_snmp) == template_before, (
        "a late status reply must not change the selected template after the user has typed"
    )
    expect(field).to_have_value("DONOTLOSE")


def test_roll_swap_after_print_still_refocuses(authed_page_snmp: Page) -> None:
    """The dirty-input guard is scoped, not a permanent latch: after a user fills and prints a label,
    swapping the roll must still re-focus the picker to a template for the new size (the input was
    consumed by the print). A permanent touch-latch must not disable
    refocus forever after the first interaction — that would break the core roll-swap workflow."""
    phase = {"width": 29}

    def handle(route: object) -> None:
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=phase["width"], media_type="continuous", media_length_mm=None
            ),
        )

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    # Loaded 29mm roll → focus the 29mm group and land on its template.
    match_label = authed_page_snmp.locator("#template-groups .tpl-group[data-match] .group-label")
    expect(match_label).to_have_text("29mm continuous", timeout=8000)
    expect(authed_page_snmp.locator(".tpl-card.selected")).to_have_attribute(
        "data-name", "simple-text-29"
    )

    # Fill its field and dry-run print — this consumes the input (clears the dirty guard).
    authed_page_snmp.locator("#fields-container input").first.fill("done")
    authed_page_snmp.check("#dry-run")
    with authed_page_snmp.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ):
        authed_page_snmp.click("button.btn-print")
    expect(authed_page_snmp.locator(".status.ok")).to_be_visible()

    # Swap to a 62mm roll; the next poll must re-focus to a 62mm template (input already consumed).
    phase["width"] = 62
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")
    expect(match_label).to_have_text("62mm continuous")
    assert _selected_template(authed_page_snmp) != "simple-text-29", (
        "a roll swap after a print should refocus off the 29mm template, not stay latched"
    )


def test_roll_swap_after_print_still_refocuses_across_a_reload(authed_page_snmp: Page) -> None:
    """Same consumed-choice rule as test_roll_swap_after_print_still_refocuses, but with a reload
    between the print and the roll swap: the sessionStorage restore must treat an already-printed
    snapshot as consumed (SUBMITTED_KEY) rather than re-raising the override guard — otherwise the
    refocus that works for a user who stayed on the page would silently stop working for one who
    switched tabs or reloaded after printing."""
    phase = {"width": 62}

    def handle(route: object) -> None:
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=phase["width"], media_type="continuous", media_length_mm=None
            ),
        )

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    match_label = authed_page_snmp.locator("#template-groups .tpl-group[data-match] .group-label")
    expect(match_label).to_have_text("62mm continuous", timeout=8000)

    # An EXPLICIT pick (this is what writes the sessionStorage snapshot — a roll-driven focus
    # deliberately does not), then fill and dry-run print: the choice is consumed.
    _select_template(authed_page_snmp, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page_snmp)
    authed_page_snmp.check("#dry-run")
    with authed_page_snmp.expect_response(
        lambda r: r.url.endswith("/print") and r.request.method == "POST"
    ):
        authed_page_snmp.click("button.btn-print")
    expect(authed_page_snmp.locator(".status.ok")).to_be_visible()

    # Navigate away and back (reload): the snapshot restores the printed choice for convenience...
    authed_page_snmp.reload()
    expect(match_label).to_have_text("62mm continuous", timeout=8000)
    expect(authed_page_snmp.locator(".tpl-card.selected")).to_have_attribute(
        "data-name", SAMPLE_TEMPLATE
    )

    # ...but as a CONSUMED choice — a roll swap must still refocus, exactly as without the reload.
    phase["width"] = 29
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")
    expect(match_label).to_have_text("29mm continuous")
    assert _selected_template(authed_page_snmp) != SAMPLE_TEMPLATE, (
        "the restored already-printed choice must not block the roll-swap refocus"
    )


def test_late_status_does_not_override_manual_template_choice(authed_page_snmp: Page) -> None:
    """A manual template pick is an explicit choice that a late status reply must not override — even
    with no typing. Pick a 62mm template while the roll is unknown, then have a 29mm roll arrive late;
    the selection must stand (otherwise Print would silently submit a different template).
    Selection-only interaction must also be covered by the refocus guard, not just typing."""
    phase = {"reachable": False}

    def handle(route: object) -> None:
        if phase["reachable"]:
            body = _status_body(media_width_mm=29, media_type="continuous", media_length_mm=None)
        else:
            body = (
                '{"state": "off", "uri": "tcp://192.0.2.10:9100", "reachable": false, "errors": []}'
            )
        route.fulfill(status=200, content_type="application/json", body=body)  # type: ignore[attr-defined]

    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/")

    selected = authed_page_snmp.locator(".tpl-card.selected")
    # Explicitly pick a 62mm template while the roll is still unknown — no typing.
    _select_template(authed_page_snmp, "title-subtitle")
    expect(selected).to_have_attribute("data-name", "title-subtitle")

    # A 29mm roll becomes known late; the explicit pick must NOT be auto-replaced.
    phase["reachable"] = True
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")
    expect(selected).to_have_attribute("data-name", "title-subtitle")


def test_edit_during_in_flight_print_is_not_wiped_by_stale_completion(
    authed_page_snmp: Page,
) -> None:
    """Stale-completion race: a /print reply that lands AFTER the user has started editing the next
    label must not clear the refocus guard and let a later status refresh wipe the newer input. Hold
    /print in flight, edit during the delay, release it, then a roll change must NOT refocus —
    doPrint must not clear userOverride unconditionally."""
    phase = {"width": 62}

    def status_handler(route: object) -> None:
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=phase["width"], media_type="continuous", media_length_mm=None
            ),
        )

    authed_page_snmp.route("**/printer/status", status_handler)

    held: dict[str, object] = {}

    def print_handler(route: object) -> None:
        held["route"] = route  # hold the response in flight; the test fulfils it later

    authed_page_snmp.route("**/print", print_handler)

    authed_page_snmp.goto("/")
    expect(
        authed_page_snmp.locator("#template-groups .tpl-group[data-match] .group-label")
    ).to_have_text("62mm continuous", timeout=8000)
    template_before = _selected_template(authed_page_snmp)

    field = authed_page_snmp.locator("#fields-container input").first
    field.fill("first")
    with authed_page_snmp.expect_request(lambda r: r.url.endswith("/print")):
        authed_page_snmp.click("button.btn-print")

    # The user edits the NEXT label while the print is still in flight.
    field.fill("EDITED-IN-FLIGHT")

    # The stale print now completes — must not clear the guard the new edit re-raised.
    held["route"].fulfill(  # type: ignore[attr-defined]
        status=200,
        content_type="application/json",
        body=f'{{"job_id": "j1", "template": "{template_before}", "copies": 1, "dry_run": true}}',
    )

    # A roll change arrives; the guard must hold (edited after send) → no refocus, no wipe.
    phase["width"] = 29
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")
    expect(field).to_have_value("EDITED-IN-FLIGHT")
    expect(authed_page_snmp.locator(".tpl-card.selected")).to_have_attribute(
        "data-name", template_before
    )


def test_print_page_shows_all_when_loaded_roll_has_no_templates(authed_page_snmp: Page) -> None:
    """Empty-state guard: if the loaded roll has no matching template (e.g. a 50mm continuous roll we
    ship nothing for), the picker must not collapse to an empty matching group — it falls back to
    showing every size with a note, so the user always has something to pick."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=50, media_type="continuous", media_length_mm=None),
        ),
    )
    authed_page_snmp.goto("/")

    # No 50mm template exists → fall back to all sizes with an explanatory note.
    hint = authed_page_snmp.locator("#size-filter-hint")
    expect(hint).to_contain_text("No templates for the loaded", timeout=8000)
    labels = _group_labels(authed_page_snmp)
    assert len(labels) >= 4, f"empty-match fallback must show every size group, got {labels}"
    # Nothing matched, so no group is focused/marked as the loaded roll.
    expect(authed_page_snmp.locator("#template-groups .tpl-group[data-match]")).to_have_count(0)


def test_studio_reference_renders_keys_and_tokens_as_tables(authed_page: Page) -> None:
    """The Template-format reference lists Top-level keys and Fields & tokens as one-row-per-item
    tables (previously comma-separated prose), matching the existing Element-types table — so the
    card now holds three tables and a discriminating key/token appears in its own row."""
    authed_page.goto("/editor")
    ref = authed_page.locator(".help-ref")
    expect(ref).to_contain_text("Top-level keys")
    # Three tables: Top-level keys, Fields & tokens, Element types.
    expect(ref.locator("table")).to_have_count(3)
    # A per-item row exists for a top-level key and for a computed token (each unique to one table).
    expect(ref.locator("table tbody tr td code", has_text="rotate")).to_have_count(1)
    expect(ref.locator("table tbody tr td code", has_text="{{seq}}")).to_have_count(1)


def test_switching_template_clears_the_previous_preview(authed_page: Page) -> None:
    """Picking a DIFFERENT template blanks the live preview immediately, so the previous template's
    label doesn't linger (dimmed) while the new one renders — the new render reveals it on success."""
    authed_page.goto("/")
    expect(authed_page.locator("#template-groups .tpl-card").first).to_be_visible(timeout=8000)
    # Seed a prior "successful render" on the current template (the first preview would otherwise 422
    # on empty required fields). A 1x1 data URI needs no /preview round-trip and no blob to revoke.
    authed_page.evaluate(
        """() => {
          const img = document.getElementById('preview-img');
          img.src = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
          document.getElementById('preview-section').style.display = '';
        }"""
    )
    expect(authed_page.locator("#preview-img")).to_have_attribute("src", re.compile(r".+"))
    # Make the NEXT /preview hang so the interim blanked state is observable.
    authed_page.route("**/preview", lambda route: None)
    names = authed_page.evaluate("() => TEMPLATES.map(t => t.name)")
    current = authed_page.evaluate("() => currentTemplate().name")
    other = next(n for n in names if n != current)
    _select_template(authed_page, other)
    # The previous image's src is dropped the instant the template changes (before the new one lands).
    expect(authed_page.locator("#preview-img:not([src])")).to_have_count(1)


def test_editor_download_yaml_uses_yaml_extension(authed_page: Page) -> None:
    """The studio's "Download YAML" button names the file ``<template-name>.yaml``.

    The ``.yaml`` extension matches the shipped templates/ files, so a downloaded draft drops
    straight into the templates dir. Caveat that motivated this test: under Playwright (and any
    headless harness) a download is intercepted and written to a temp path with a GUID name and no
    extension — so eyeballing the saved file is misleading. The human-facing name a real Chrome tab
    would use lives in ``download.suggested_filename``, which is what the page's ``a.download``
    attribute drives and what we assert here. The editor seeds ``name: my-label``, so the expected
    name is deterministic.
    """
    authed_page.goto("/editor")
    expect(authed_page.locator("#yaml")).to_be_visible()

    with authed_page.expect_download() as download_info:
        authed_page.click("button:has-text('Download YAML')")

    assert download_info.value.suggested_filename == "my-label.yaml", (
        download_info.value.suggested_filename
    )


def test_studio_image_field_renders_picker_and_previews(authed_page: Page) -> None:
    """The Template Studio's sample-field panel renders a file picker for an image field detected in
    the draft YAML, and an uploaded image renders a real draft preview."""
    authed_page.goto("/editor")
    expect(authed_page.locator("#yaml")).to_be_visible()

    yaml = (
        "name: draft-image\n"
        "description: image draft\n"
        'label: "62"\n'
        "rotate: 0\n"
        "fields:\n"
        "  required: [photo]\n"
        "  optional: []\n"
        "layout:\n"
        "  - {type: image, field: photo}\n"
    )
    authed_page.fill("#yaml", yaml)

    # After the parse round-trip, the detected image field renders a (hidden) file input.
    file_input = authed_page.locator("#field-photo")
    expect(file_input).to_have_attribute("type", "file")
    file_input.set_input_files(
        files=[{"name": "p.png", "mimeType": "image/png", "buffer": PNG_1PX}]
    )

    # The draft preview renders a real (decoded) image once the upload is merged into the payload.
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )


def test_studio_seq_template_shows_controls_and_previews_first_item(authed_page: Page) -> None:
    """The Template Studio reveals its Auto-number controls for a {{seq}} draft and previews the
    FIRST item — a {{seq}} draft is no longer preview-blind in the editor. Toggling the YAML back to
    a non-seq layout hides the controls again."""
    authed_page.goto("/editor")
    expect(authed_page.locator("#yaml")).to_be_visible()

    seq_yaml = (
        "name: draft-seq\n"
        "description: seq draft\n"
        'label: "62"\n'
        "rotate: 0\n"
        "layout:\n"
        '  - {type: title, text: "Box {{seq}}"}\n'
    )
    authed_page.fill("#yaml", seq_yaml)

    # The auto-number panel appears (uses_seq from the parse round-trip) and the draft renders.
    expect(authed_page.locator("#sequence-field")).to_be_visible()
    authed_page.fill("#seq-start", "5")
    authed_page.fill("#seq-padding", "3")

    with authed_page.expect_response(
        lambda r: r.url.endswith("/preview/draft") and r.request.method == "POST"
    ) as resp_info:
        authed_page.fill("#seq-count", "12")  # any auto-number edit re-previews

    response = resp_info.value
    import json as _json

    sent = _json.loads(response.request.post_data or "{}")
    assert sent.get("sequence"), "the draft preview payload must carry the sequence spec"
    assert response.status == 200, f"seq draft must preview, not 422: {response.status}"
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )

    # Switching to a non-seq layout hides the controls again.
    authed_page.fill(
        "#yaml",
        'name: d2\ndescription: plain\nlabel: "62"\nlayout:\n  - {type: title, text: "Static"}\n',
    )
    expect(authed_page.locator("#sequence-field")).to_be_hidden()


def test_studio_seq_inputs_normalize_on_commit_not_while_typing(authed_page: Page) -> None:
    """An auto-number input must NOT be rewritten mid-keystroke (that made a negative start
    un-typeable — a lone "-" snapped straight to the default), yet the preview payload must stay
    valid throughout. Clamping is deferred to `change` (blur/Enter/spinner); the preview reads
    clamped values purely, without touching the field."""
    authed_page.goto("/editor")
    expect(authed_page.locator("#yaml")).to_be_visible()
    authed_page.fill(
        "#yaml",
        "name: draft-seq\ndescription: seq\n"
        'label: "62"\nrotate: 0\nlayout:\n  - {type: title, text: "Box {{seq}}"}\n',
    )
    expect(authed_page.locator("#sequence-field")).to_be_visible()
    start = authed_page.locator("#seq-start")
    count = authed_page.locator("#seq-count")

    # Clearing a field stays empty on the INPUT path (previously it snapped to the default the moment
    # it went blank, so you couldn't clear-to-retype).
    count.fill("")
    assert count.input_value() == "", "clearing a field must not snap to the default on input"
    # ...yet the preview payload is still valid: currentSequenceSpec reads a CLAMPED value without
    # mutating the field (blank count → default 10), so /preview/draft never sees a broken spec.
    assert authed_page.evaluate("() => currentSequenceSpec().count") == 10
    assert count.input_value() == "", "reading the spec must not rewrite the field"
    # Committing (change / blur / spinner) normalizes the display.
    count.dispatch_event("change")
    assert count.input_value() == "10", "a blank field defaults on commit"

    # A negative start is now typeable: a lone '-' used to be wiped on input (parseInt('-') is NaN →
    # reset to the default), so the next digit produced a positive number. Type it key by key.
    start.fill("")
    start.focus()
    authed_page.keyboard.type("-5")
    assert start.input_value() == "-5", "a negative start must be typeable (not clamped mid-entry)"
    start.dispatch_event("change")
    assert start.input_value() == "-5", "an in-bounds negative start is preserved on commit"


def test_status_banner_does_not_execute_injected_markup(authed_page: Page) -> None:
    """The status banner renders untrusted text as text, never markup (SNMP-to-browser XSS guard).

    A /print failure stringifies the server's 409 detail into ``showStatus``; that detail now carries
    device/network-supplied values (the printer's SNMP ``media_name`` and console-derived error
    strings). A spoofed/hostile printer string containing HTML must not be parsed into DOM on this
    token-bearing page — otherwise an injected ``onerror`` could exfiltrate the localStorage API
    token. Drives ``showStatus`` directly with a malicious payload and asserts no element/script
    materialises and the markup survives as literal text.
    """
    authed_page.goto("/")
    payload = '<img src=x onerror="window.__xss_fired = true">'
    authed_page.evaluate("(m) => window.showStatus('Print error: ' + m, 'err')", payload)

    banner = authed_page.locator(".status.err")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("<img src=x onerror=")  # shown verbatim, not parsed
    assert authed_page.locator(".status.err img").count() == 0, "injected <img> must not become DOM"
    assert not authed_page.evaluate("() => window.__xss_fired"), (
        "onerror from an injected tag must never fire"
    )


def test_media_compatibility_badges_are_advisory(authed_page: Page) -> None:
    """The print page badges each template against the loaded roll — advisory only (Step 7).

    Drives the client-side compatibility consumer deterministically: seed a continuous + a die-cut
    template, set the loaded roll over a network (tcp://) printer, and assert the badge reflects ✓/✗
    (the same width/form rule the server-side 409 guard applies). The badge NEVER disables Print or
    the dropdown — /print does a fresh SNMP check and is the authoritative guard, so blocking from
    cached status would wrongly lock out a print after a roll swap. Preview is never blocked either."""
    authed_page.goto("/")
    # Seed two known templates with explicit required media (drives templateMap + the badge directly;
    # the cards need not re-render — the badge consumes currentTemplate(), not the picker DOM).
    authed_page.evaluate(
        """() => {
          templateMap['__cont'] = {name:'__cont', description:'cont', required:[], optional:[],
            media:{width_mm:62.0, media_type:'continuous', length_mm:null}};
          templateMap['__dc'] = {name:'__dc', description:'die cut', required:[], optional:[],
            media:{width_mm:62.0, media_type:'die_cut', length_mm:29.0}};
          TEMPLATES.push(templateMap['__cont'], templateMap['__dc']);
        }"""
    )

    # Loaded roll = 62mm continuous on a network printer; select the (mismatching) die-cut template.
    authed_page.evaluate(
        """() => {
          printerStatus = {state:'idle', uri:'tcp://192.168.5.14:9100', reachable:true,
            media_width_mm:62, media_type:'continuous', media_length_mm:null};
          selectedTemplateName = '__dc';
          renderFields();
          updateMediaBadge();
        }"""
    )
    # Mismatch → red ✗ badge, but NOTHING is disabled: advisory only.
    badge = authed_page.locator("#media-badge")
    expect(badge).to_have_class(re.compile(r"media-bad"))
    expect(badge).to_contain_text("✗")
    assert authed_page.eval_on_selector(".btn-print", "b => b.disabled") in (False, None), (
        "Print must NOT be disabled from cached status — /print is the authoritative guard"
    )
    assert authed_page.eval_on_selector(".btn-preview", "b => b.disabled") in (False, None), (
        "Preview must never be blocked by a media mismatch"
    )
    assert authed_page.evaluate(
        "() => [...document.querySelectorAll('.tpl-card input[type=radio]')]"
        ".every(r => !r.disabled)"
    ), "every template card must stay selectable despite the mismatch"

    # Swap the loaded roll to die-cut 62x29: the badge flips ✗ → ✓ for the same template.
    authed_page.evaluate(
        """() => {
          printerStatus = {state:'idle', uri:'tcp://192.168.5.14:9100', reachable:true,
            media_width_mm:62, media_type:'die_cut', media_length_mm:29};
          updateMediaBadge();
        }"""
    )
    expect(authed_page.locator("#media-badge")).to_have_class(re.compile(r"media-ok"))
    expect(authed_page.locator("#media-badge")).to_contain_text("✓")
    assert authed_page.eval_on_selector(".btn-print", "b => b.disabled") in (False, None)


def _expand_label_table(page: Page) -> None:
    """Reveal the collapsed "All supported labels" table so its rows are visible and clickable.

    The reference table now lives inside a <details> (collapsed by default). Its rows are populated
    regardless of open state, so `.count()`/class assertions still work closed — but Playwright
    visibility checks and `.click()` actionability need the disclosure open.
    """
    page.locator("details.label-table > summary").click()


def test_editor_label_reference_lists_labels_and_use_button_sets_yaml(authed_page: Page) -> None:
    """The studio's label-reference panel lists supported labels and "Use" writes the YAML (Step 8).

    Drives the deterministic, server-embedded half: the table is populated from the editor route's
    LABELS context (no printer needed), and clicking a row's "Use" button replaces the top-level
    ``label:`` in the editor textarea so an author can pick a valid media without hand-typing.
    """
    authed_page.goto("/editor")
    _expand_label_table(authed_page)
    body = authed_page.locator("#label-ref-body")
    expect(body.locator("tr").first).to_be_visible()
    assert body.locator("tr").count() > 0, "the label reference must list the model's labels"
    # The seeded starter YAML uses label "62"; switch it to the die-cut 62x29 via its Use button.
    assert 'label: "62"' in authed_page.locator("#yaml").input_value()
    row = body.locator("tr").filter(has_text="62x29").first
    expect(row).to_contain_text("die-cut")
    row.locator("button:has-text('Use')").click()
    assert 'label: "62x29"' in authed_page.locator("#yaml").input_value()


def test_editor_label_reference_renders_when_status_never_resolves(authed_page: Page) -> None:
    """The static label table must not be gated on the (optional) live printer-status fetch.

    Rows must not appear only after /printer/status resolves: a stuck
    SNMP/TCP query would otherwise hide the core picker. Hang the status request so it never returns,
    then assert the reference rows still populate from the server-embedded LABELS.
    """
    # Intercept /printer/status and never fulfil it — simulates a stuck SNMP/TCP query.
    authed_page.route("**/printer/status", lambda route: None)
    authed_page.goto("/editor")
    _expand_label_table(authed_page)
    expect(authed_page.locator("#label-ref-body tr").first).to_be_visible()
    assert authed_page.locator("#label-ref-body tr").count() > 0, (
        "the static label table must render even when printer status never resolves"
    )


def test_editor_use_button_replaces_noncanonical_label_key(authed_page: Page) -> None:
    """ "Use" replaces a non-canonical top-level ``label`` key in place — never duplicates it.

    Regression for the finding: ``label : "62"`` (whitespace before the colon) is valid YAML but
    the old exact-match regex missed it and inserted a second ``label`` key. Seed that form, click Use
    for 62x29, and assert exactly one top-level label key remains and it is the selected id.
    """
    authed_page.goto("/editor")
    _expand_label_table(authed_page)
    authed_page.evaluate(
        """() => { document.getElementById('yaml').value =
`name: my-label
description: A new label
label : "62"
rotate: 0
`; }"""
    )
    row = authed_page.locator("#label-ref-body tr").filter(has_text="62x29").first
    row.locator("button:has-text('Use')").click()
    yaml = authed_page.locator("#yaml").input_value()
    label_keys = re.findall(r'^["\']?label["\']?\s*:', yaml, re.MULTILINE)
    assert len(label_keys) == 1, (
        f"expected exactly one top-level label key, got {len(label_keys)}: {yaml!r}"
    )
    assert 'label: "62x29"' in yaml, yaml


def test_editor_your_printer_highlights_matching_labels(authed_page: Page) -> None:
    """When the printer answers SNMP, the studio flags the loaded roll and the matching label id(s).

    The e2e server uses a file:// transport (no network printer), so the live highlight is driven
    deterministically: inject a 62mm-continuous network status through the same client functions the
    real /printer/status fetch uses, then assert the "Your Printer" box names the loaded media and at
    least one reference row is marked as matching.
    """
    authed_page.goto("/editor")
    authed_page.evaluate(
        """() => {
          const status = {state:'idle', uri:'tcp://192.168.5.14:9100', reachable:true,
            media_width_mm:62, media_type:'continuous', media_length_mm:null};
          const loaded = renderYourPrinter(status);
          renderLabelReference(loaded);
        }"""
    )
    yp = authed_page.locator("#your-printer")
    expect(yp).to_contain_text("Your Printer")
    expect(yp).to_contain_text("62mm continuous")
    expect(yp).to_contain_text("Matching label id(s)")
    assert authed_page.locator("#label-ref-body tr.match").count() >= 1, (
        "the loaded 62mm continuous roll must highlight at least the matching '62' label"
    )


def test_editor_label_reference_refetches_status_on_token_entry(anon_page: Page) -> None:
    """First-run recovery: entering the API token refetches printer status for the label panel.

    On a secured deployment the first visitor lands tokenless, so the initial /printer/status load
    401s and the "Your Printer" highlight stays blank. Typing the token must trigger a fresh status
    fetch (the fix for the first-run finding) — asserted by waiting for a /printer/status
    request after the field is filled. The e2e server's file:// transport can't produce a real
    highlight, so this asserts the refetch wiring, not the highlight content.
    """
    anon_page.goto("/editor")
    _expand_label_table(anon_page)
    # The reference table itself populates from the server-embedded LABELS regardless of auth.
    expect(anon_page.locator("#label-ref-body tr").first).to_be_visible()
    # Let the tokenless initial /printer/status load settle so the response we capture below is the
    # one the token entry triggers, not the page-load one.
    anon_page.wait_for_load_state("networkidle")
    # The token input now lives in the shared nav dialog (opened from the key button), not an inline
    # card — open it before typing.
    anon_page.click("#token-open")
    expect(anon_page.locator("#api-token")).to_be_visible()
    with anon_page.expect_response(lambda r: r.url.endswith("/printer/status")) as resp_info:
        anon_page.fill("#api-token", "a-token")  # 'input' → debounced loadLabelReference()
    assert resp_info.value.url.endswith("/printer/status"), (
        "entering the token must refetch /printer/status so the panel can recover from a 401"
    )


def test_editor_red_label_is_geometry_only_match(authed_page: Page) -> None:
    """A red/black label is shown as a geometry-only match, not a definite one.

    62 and 62red share the same 62mm-continuous geometry, but SNMP can't prove the loaded roll is red.
    With a plain 62mm-continuous roll, the plain ``62`` row must be a definite match (``tr.match``)
    while ``62red`` must be the qualified geometry-only class (``tr.match-geo``) and appear under the
    "roll colour not verified" line — never in the definite "Matching label id(s)" list.
    """
    authed_page.goto("/editor")
    authed_page.evaluate(
        """() => {
          const status = {state:'idle', uri:'tcp://192.168.5.14:9100', reachable:true,
            media_width_mm:62, media_type:'continuous', media_length_mm:null};
          const loaded = renderYourPrinter(status);
          renderLabelReference(loaded);
        }"""
    )
    # The plain 62 row is a definite match; the 62red row is geometry-only (amber), not tr.match.
    row62 = authed_page.locator("#label-ref-body tr").filter(has_text=re.compile(r"^62\b")).first
    red_row = authed_page.locator("#label-ref-body tr").filter(has_text="62red").first
    expect(red_row).to_have_class(re.compile(r"match-geo"))
    assert "match-geo" not in (row62.get_attribute("class") or ""), (
        "plain 62 must be a definite match"
    )
    # "Your Printer": 62red is listed under the red caveat, never the definite matching line.
    yp = authed_page.locator("#your-printer")
    expect(yp.locator(".yp-matches")).not_to_contain_text("62red")
    expect(yp.locator(".yp-red")).to_contain_text("62red")


def test_editor_use_collapses_duplicate_label_keys(authed_page: Page) -> None:
    """ "Use" collapses a duplicate-key draft to exactly one ``label`` so no stale value survives.

    Regression for the finding: PyYAML keeps the LAST of duplicate mapping keys, so leaving any
    duplicate ``label:`` (even with equal values) keeps the template structurally ambiguous — a later
    edit to only the first line would silently revert preview/save to the stale later key. Seed two
    top-level ``label`` keys, click Use, and assert exactly one ``label`` key remains with the id.
    """
    authed_page.goto("/editor")
    _expand_label_table(authed_page)
    authed_page.evaluate(
        """() => { document.getElementById('yaml').value =
`name: my-label
label: "62"
description: A new label
label: "29"
rotate: 0
`; }"""
    )
    row = authed_page.locator("#label-ref-body tr").filter(has_text="62x29").first
    row.locator("button:has-text('Use')").click()
    yaml = authed_page.locator("#yaml").input_value()
    label_keys = re.findall(r'^["\']?label["\']?\s*:', yaml, re.MULTILINE)
    assert len(label_keys) == 1, f"duplicate label keys must collapse to exactly one: {yaml!r}"
    assert 'label: "62x29"' in yaml, yaml


def test_editor_use_preserves_indented_root_mapping(authed_page: Page) -> None:
    """ "Use" edits the label at the document's root indentation, not always column 0.

    PyYAML accepts a uniformly-indented root mapping, so a column-0 label insert would mix indentation
    levels and turn valid YAML into a parse error. Seed a 2-space-indented root mapping, click Use,
    and assert the new label keeps that indentation, no column-0 ``label:`` is introduced, and exactly
    one label key remains — i.e. every root key stays at a single consistent indent.
    """
    authed_page.goto("/editor")
    _expand_label_table(authed_page)
    authed_page.evaluate(
        """() => { document.getElementById('yaml').value =
`  name: my-label
  description: A new label
  label: "62"
  rotate: 0
`; }"""
    )
    row = authed_page.locator("#label-ref-body tr").filter(has_text="62x29").first
    row.locator("button:has-text('Use')").click()
    yaml = authed_page.locator("#yaml").input_value()
    assert '  label: "62x29"' in yaml, yaml
    assert re.search(r"^label:", yaml, re.MULTILINE) is None, (
        f"must not introduce a column-0 label that mixes indentation: {yaml!r}"
    )
    assert len(re.findall(r'^\s*["\']?label["\']?\s*:', yaml, re.MULTILINE)) == 1, yaml
    # Every non-blank line shares the same 2-space root indent — the mapping stays consistent/valid.
    for ln in yaml.splitlines():
        if ln.strip():
            assert ln.startswith("  ") and not ln.startswith("   "), f"indentation drifted: {ln!r}"


def test_editor_use_handles_document_marker_and_indented_root(authed_page: Page) -> None:
    """ "Use" edits in place under a `---` document marker + indented root mapping.

    Regression: a valid template that opens with `---` followed by a uniformly indented root mapping
    must not make the edit derive an empty indent and prepend a column-0 `label:` before the marker
    (which would split the file into an invalid/multi-document stream). The marker is skipped when
    deriving the root indent, so the real indented `label:` is replaced in place.
    """
    authed_page.goto("/editor")
    _expand_label_table(authed_page)
    authed_page.evaluate(
        """() => { document.getElementById('yaml').value =
`---
  name: my-label
  description: A new label
  label: "62"
  rotate: 0
`; }"""
    )
    row = authed_page.locator("#label-ref-body tr").filter(has_text="62x29").first
    row.locator("button:has-text('Use')").click()
    yaml = authed_page.locator("#yaml").input_value()
    assert '  label: "62x29"' in yaml, yaml
    # Exactly one document marker, still first, and no column-0 label inserted before it.
    assert yaml.splitlines()[0].strip() == "---", yaml
    assert yaml.count("---") == 1, yaml
    assert re.search(r"^label:", yaml, re.MULTILINE) is None, yaml
    assert len(re.findall(r'^\s*["\']?label["\']?\s*:', yaml, re.MULTILINE)) == 1, yaml


def test_studio_load_dropdown_groups_by_media(authed_page: Page) -> None:
    """The studio's #load-select buckets existing templates into <optgroup>s by media size — the
    same groupKeyOf/groupTitleOf grouping the print page's template picker uses — instead of one
    flat list, and the placeholder option stays first."""
    authed_page.goto("/editor")
    sel = authed_page.locator("#load-select")
    expect(sel).to_be_visible()
    authed_page.wait_for_function(
        "() => document.querySelectorAll('#load-select optgroup').length > 0"
    )

    # Placeholder stays the first child, outside any optgroup.
    first_tag = sel.evaluate("el => el.children[0].tagName")
    first_value = sel.evaluate("el => el.children[0].value")
    assert first_tag == "OPTION" and first_value == "", (
        "the placeholder option must remain first and outside every optgroup"
    )

    optgroups = sel.locator("optgroup")
    assert optgroups.count() >= 1, "expected at least one media optgroup"
    labels = optgroups.evaluate_all("els => els.map(e => e.label)")
    assert all(labels), f"every optgroup must carry a non-empty label, got {labels!r}"
    option_counts = optgroups.evaluate_all(
        "els => els.map(e => e.querySelectorAll('option').length)"
    )
    assert all(c > 0 for c in option_counts), "every optgroup must contain at least one template"


def test_editor_yaml_highlight_overlay_smoke(authed_page: Page) -> None:
    """The studio's YAML syntax-highlight overlay (#yaml-hl) paints key and placeholder token spans
    for the current draft, while #yaml stays the real, authoritative textarea whose .value
    round-trips typed input unchanged. The overlay also mirrors the textarea's scroll offset."""
    authed_page.goto("/editor")
    expect(authed_page.locator("#yaml")).to_be_visible()

    # The seeded starter template is painted immediately (a programmatic write fires no `input`,
    # so the page calls syncYamlHighlight explicitly after the seed).
    hl = authed_page.locator("#yaml-hl")
    expect(hl.locator(".yhl-key").first).to_have_text("name")
    expect(hl.locator(".yhl-placeholder").first).to_have_text("{{title}}")

    # Typing syncs the overlay immediately (not debounced like doPreview) AND the textarea's .value
    # stays authoritative — the exact typed text round-trips unchanged.
    typed = 'name: overlay-smoke\n# a comment\ncount: 42\ntext: "{{subtitle}}"'
    authed_page.fill("#yaml", typed)
    assert authed_page.locator("#yaml").input_value() == typed
    expect(hl.locator(".yhl-key").first).to_have_text("name")
    expect(hl.locator(".yhl-comment")).to_have_text("# a comment")
    expect(hl.locator(".yhl-number")).to_have_text("42")
    expect(hl.locator(".yhl-placeholder")).to_have_text("{{subtitle}}")
    # The overlay renders the same text the textarea holds — alignment depends on it.
    hl_text = authed_page.locator("#yaml-hl-code").inner_text()
    assert hl_text.rstrip("\n") == typed, f"overlay text must mirror the textarea: {hl_text!r}"

    # Flow-mapping list items accent the keys INSIDE the braces — and never let the optional `- `
    # lead backtrack into painting `- {type` as one key (regression: YAML_HL_KEY_RE excludes `-`
    # as a key's first char).
    flow = 'layout:\n  - {type: title, text: "{{title}}"}'
    authed_page.fill("#yaml", flow)
    key_texts = authed_page.locator("#yaml-hl .yhl-key").all_inner_texts()
    assert key_texts == ["layout", "type", "text"], (
        f"flow-mapping keys mis-tokenized: {key_texts!r}"
    )

    # Scroll sync: overflow the textarea, scroll it, and the overlay follows.
    long_draft = "\n".join(f"key{i}: value{i}" for i in range(200))
    authed_page.fill("#yaml", long_draft)
    authed_page.evaluate("() => { document.getElementById('yaml').scrollTop = 500; }")
    authed_page.wait_for_function(
        "() => document.getElementById('yaml-hl').scrollTop"
        " === document.getElementById('yaml').scrollTop"
    )


def test_studio_draft_preview_unavailable_until_required_field_filled(authed_page: Page) -> None:
    """The studio's Draft preview matches the print page's Live preview for a missing required field:
    the seeded starter declares a required `title` that starts blank, so the preview opens on the
    #preview-placeholder ("Preview unavailable") + an inline #preview-error naming the field — never a
    silently blank-field label. Typing a title renders the label and clears both. Regression guard for
    /preview/draft's required-field enforcement (previously it returned a blank 200 render)."""
    authed_page.goto("/editor")
    # The auto-detected field form still renders even though the preview can't — that is the point:
    # the operator sees exactly which input to fill.
    expect(authed_page.locator("#field-title")).to_be_visible()
    expect(authed_page.locator("#preview-placeholder")).to_be_visible()
    expect(authed_page.locator("#preview-img")).to_be_hidden()
    error = authed_page.locator("#preview-error")
    expect(error).to_contain_text("title")
    assert "missing_required" not in error.inner_text(), (
        f"must render a friendly sentence, not the raw JSON key: {error.inner_text()!r}"
    )
    expect(authed_page.locator("#draft-status")).to_have_text("invalid")

    # Filling the required field renders the label and clears the placeholder + inline error.
    with authed_page.expect_response(lambda r: "/preview/draft" in r.url):
        authed_page.fill("#field-title", "Freezer A")
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )
    expect(authed_page.locator("#preview-placeholder")).to_be_hidden()
    expect(authed_page.locator("#preview-img")).to_be_visible()
    expect(error).to_have_text("")
    expect(authed_page.locator("#draft-status")).to_have_text("valid")


def test_studio_print_draft_dry_run_round_trip(authed_page: Page) -> None:
    """Print the current draft straight from the studio (dry-run) — no save needed. Asserts the
    /print/draft round-trip: the payload carries the raw YAML + typed fields, the response reports
    the draft's parsed name, and the sticky success banner appears. The starter seed is used as-is,
    so this also proves the flow works before the template exists anywhere on disk."""
    authed_page.goto("/editor")
    expect(authed_page.locator("#field-title")).to_be_visible()
    authed_page.fill("#field-title", "Straight from the studio")
    authed_page.check("#dry-run")

    with authed_page.expect_response(
        lambda r: r.url.endswith("/print/draft") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("#print-draft-btn")

    import json as _json

    response = resp_info.value
    sent = _json.loads(response.request.post_data or "{}")
    assert sent["yaml"].startswith("name: my-label"), "payload must carry the draft YAML verbatim"
    assert sent["fields"]["title"] == "Straight from the studio"
    assert "options" in sent and "dither" in sent["options"]
    assert response.status == 200, f"/print/draft must succeed: {response.status}"
    body = response.json()
    assert body["dry_run"] is True
    assert body["template"] == "my-label"  # the draft's parsed internal name
    assert body["job_id"]

    expect(authed_page.locator(".status.ok")).to_be_visible()


def test_studio_copies_input_clamps_typed_value(authed_page: Page) -> None:
    """Typing an out-of-range Copies value is clamped into 1..10 on input (the min/max attributes
    only constrain the spinner arrows) — same behavior as the Print page, so a physical print never
    goes out on a 422 the user has to decode."""
    authed_page.goto("/editor")
    copies = authed_page.locator("#copies")
    expect(copies).to_be_visible()
    copies.fill("20")
    expect(copies).to_have_value("10")
    copies.fill("0")
    expect(copies).to_have_value("1")


def test_studio_print_draft_seq_hides_copies_and_sends_sequence(authed_page: Page) -> None:
    """A {{seq}} draft swaps the Copies input for the Auto-number panel (mutually exclusive
    server-side) and a dry-run print carries the sequence spec with copies pinned to 1."""
    authed_page.goto("/editor")
    expect(authed_page.locator("#copies-cell")).to_be_visible()

    seq_yaml = (
        "name: draft-seq\n"
        "description: seq draft\n"
        'label: "62"\n'
        "layout:\n"
        '  - {type: title, text: "Box {{seq}}"}\n'
    )
    authed_page.fill("#yaml", seq_yaml)
    expect(authed_page.locator("#sequence-field")).to_be_visible()
    expect(authed_page.locator("#copies-cell")).to_be_hidden()

    authed_page.fill("#seq-count", "3")
    authed_page.check("#dry-run")
    with authed_page.expect_response(
        lambda r: r.url.endswith("/print/draft") and r.request.method == "POST"
    ) as resp_info:
        authed_page.click("#print-draft-btn")

    import json as _json

    response = resp_info.value
    sent = _json.loads(response.request.post_data or "{}")
    assert sent["copies"] == 1, "a seq draft must pin copies=1 (sequence drives the count)"
    assert sent["sequence"]["count"] == 3
    assert response.status == 200, f"seq draft must print, not 422: {response.status}"

    # Back to a plain draft: Copies returns, the sequence panel hides.
    authed_page.fill(
        "#yaml",
        'name: d2\ndescription: plain\nlabel: "62"\nlayout:\n  - {type: title, text: "Static"}\n',
    )
    expect(authed_page.locator("#sequence-field")).to_be_hidden()
    expect(authed_page.locator("#copies-cell")).to_be_visible()


def test_studio_large_seq_print_confirms_via_dialog(authed_page: Page) -> None:
    """A non-dry-run sequence batch at/above the confirm threshold must ask via the in-page
    <dialog> — NOT native confirm(), which Chromium auto-accepts under Enter-key activation.
    Cancel → no /print/draft request; confirm ("Print") → exactly one batch prints."""
    import json as _json

    prints: list[str] = []

    def handle(route: object) -> None:
        prints.append(route.request.post_data)  # type: ignore[attr-defined]
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_json.dumps(
                {"job_id": "j", "template": "draft-seq", "copies": 1, "dry_run": False}
            ),
        )

    authed_page.route("**/print/draft", handle)

    authed_page.goto("/editor")
    authed_page.fill(
        "#yaml",
        'name: draft-seq\ndescription: seq\nlabel: "62"\nlayout:\n  - {type: title, text: "Box {{seq}}"}\n',
    )
    expect(authed_page.locator("#sequence-field")).to_be_visible()
    authed_page.fill("#seq-count", "25")
    if authed_page.is_checked("#dry-run"):
        authed_page.uncheck("#dry-run")  # a real (non-dry-run) batch triggers the confirm

    # The in-page dialog opens, naming the count + range, with the caller's "Print" label.
    authed_page.click("#print-draft-btn")
    dlg = authed_page.locator("#confirm-dialog")
    expect(dlg).to_be_visible()
    expect(dlg.locator("#confirm-message")).to_contain_text("25")
    expect(dlg.locator("#confirm-message")).to_contain_text("1..25")
    expect(dlg.locator("#confirm-ok")).to_have_text("Print")

    # Cancel → nothing printed.
    dlg.locator("#confirm-cancel").click()
    expect(dlg).to_be_hidden()
    authed_page.wait_for_timeout(200)
    assert len(prints) == 0, "cancelling the confirm must not print"

    # Confirm → the batch prints exactly once.
    authed_page.click("#print-draft-btn")
    expect(dlg).to_be_visible()
    dlg.locator("#confirm-ok").click()
    authed_page.wait_for_timeout(300)
    assert len(prints) == 1, "confirming must print exactly one batch"
    sent = _json.loads(prints[0] or "{}")
    assert sent["sequence"]["count"] == 25 and sent["copies"] == 1


def test_studio_horizontal_scroll_proxy_shows_and_syncs_for_long_lines(authed_page: Page) -> None:
    """A long, unwrapped line overflows #yaml horizontally: the themed proxy scroller (#yaml-hscroll)
    becomes visible, and its scrollLeft stays mirrored with the textarea's in both directions. The
    proxy exists because Chromium paints its own text cursor over a textarea's own scrollbars — the
    cursor itself isn't assertable here, only the mirrored scroll range/position."""
    authed_page.goto("/editor")
    hscroll = authed_page.locator("#yaml-hscroll")

    authed_page.fill("#yaml", "key: " + "x" * 500)
    expect(hscroll).not_to_have_class(re.compile(r"\bhscroll-hidden\b"))

    # textarea scroll -> proxy scroll.
    authed_page.evaluate("() => { document.getElementById('yaml').scrollLeft = 120; }")
    authed_page.wait_for_function(
        "() => document.getElementById('yaml-hscroll').scrollLeft === 120"
    )

    # proxy scroll -> textarea scroll.
    authed_page.evaluate("() => { document.getElementById('yaml-hscroll').scrollLeft = 40; }")
    authed_page.wait_for_function("() => document.getElementById('yaml').scrollLeft === 40")


def test_studio_horizontal_scroll_proxy_hidden_for_short_drafts(authed_page: Page) -> None:
    """A draft that fits within the panel's width shows no dead scrollbar strip below it."""
    authed_page.goto("/editor")
    authed_page.fill("#yaml", "name: short\ndescription: fits on screen\n")
    expect(authed_page.locator("#yaml-hscroll")).to_have_class(re.compile(r"\bhscroll-hidden\b"))


def test_unauthenticated_preview_shows_auth_error(anon_page: Page) -> None:
    """With no token seeded, the server rejects /preview and the UI surfaces the auth prompt."""
    anon_page.goto("/")
    _select_template(anon_page, SAMPLE_TEMPLATE)
    _fill_all_fields(anon_page)
    anon_page.click("button.btn-preview")

    status = anon_page.locator(".status.err")
    expect(status).to_be_visible()
    expect(status).to_contain_text("Authentication required")

    # First-run (no token) keeps the amber needs-token breathe — NOT the red rejected-token blink,
    # which would mislabel "not set yet" as "the stored token is wrong".
    key_btn = anon_page.locator("#token-open")
    expect(key_btn).to_have_class(re.compile(r"\bneeds-token\b"))
    expect(key_btn).not_to_have_class(re.compile(r"\bauth-failed\b"))


def test_wrong_token_blinks_key_icon_until_edited(anon_page: Page) -> None:
    """A stored-but-wrong bearer token 401s on preview: the nav key button blinks (`.auth-failed`) to
    point the user at where to fix it — unlike `.needs-token`, which only fires when NO token is stored.
    Editing the token input (the user acting on the 401) clears the blink."""
    anon_page.add_init_script(web_token_init_script("wrong-token"))
    anon_page.goto("/")
    _select_template(anon_page, SAMPLE_TEMPLATE)
    _fill_all_fields(anon_page)
    anon_page.click("button.btn-preview")

    key_btn = anon_page.locator("#token-open")
    expect(key_btn).to_have_class(re.compile(r"\bauth-failed\b"))

    # Open the dialog and edit the token — the blink should stop.
    anon_page.click("#token-open")
    anon_page.fill("#api-token", DEFAULT_API_TOKEN)
    expect(key_btn).not_to_have_class(re.compile(r"\bauth-failed\b"))


# ── History page: loaded-roll size gating ──────────────────────────────────────────────────────────
# The history page disables Reprint for rows whose template needs a roll different from the one
# loaded — advisory UI in front of the server's existing /reprint SNMP preflight (which 409s the same
# mismatch). It engages only when the loaded roll is KNOWN; otherwise every row stays reprintable.

# Two templates spanning two sizes: a 62mm continuous and a 17x54 die-cut.
_HISTORY_TEMPLATES = [
    {
        "name": "text-62",
        "description": "62mm continuous",
        "label": "62",
        "rotate": 0,
        "fields": {"required": ["title"], "optional": []},
        "media": {"width_mm": 62.0, "media_type": "continuous"},
    },
    {
        "name": "addr-17x54",
        "description": "17x54 die-cut",
        "label": "17x54",
        "rotate": 0,
        "fields": {"required": ["name"], "optional": []},
        "media": {"width_mm": 17.0, "media_type": "die_cut", "length_mm": 54.0},
    },
]


def _history_list_body() -> str:
    """A /history/list page with one printed row per seeded template."""
    import json

    def row(template: str, field_key: str, field_val: str, job: str) -> dict[str, object]:
        return {
            "job_id": job,
            "template": template,
            "fields": {field_key: field_val},
            "copies": 1,
            "dry_run": False,
            "timestamp": "2026-06-30T12:00:00",
            "status": "printed",
            "dither": False,
            "image_stripped": False,
            "sequence": None,
        }

    entries = [
        row("text-62", "title", "Hello 62", "aaaaaaaa-0000-0000-0000-000000000001"),
        row("addr-17x54", "name", "Jane Doe", "bbbbbbbb-0000-0000-0000-000000000002"),
    ]
    return json.dumps({"entries": entries, "total": len(entries), "offset": 0, "limit": 20})


def _route_history_lists(page: Page) -> None:
    """Mock the two static endpoints (templates + history list). Status is routed by the caller."""
    import json

    page.route(
        "**/templates",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HISTORY_TEMPLATES)
        ),
    )
    page.route(
        "**/history/list*",
        lambda r: r.fulfill(status=200, content_type="application/json", body=_history_list_body()),
    )


def _route_history(page: Page, status_body: str) -> None:
    """Mock the three endpoints the history page reads, with a static printer-status body."""
    _route_history_lists(page)
    page.route(
        "**/printer/status",
        lambda r: r.fulfill(status=200, content_type="application/json", body=status_body),
    )


def test_history_disables_reprint_for_size_mismatched_rows(authed_page_snmp: Page) -> None:
    """On the SNMP deployment with a 62mm continuous roll loaded, the 62mm row reprints normally while
    the 17x54 die-cut row is size-gated: its Reprint is disabled, it carries a ✗ tag, the row is dimmed,
    and Delete still works. A roll-note states which roll is loaded. (Gating is SNMP-only — see
    test_history_does_not_probe_status_on_non_snmp for the non-SNMP fallback.)"""
    _route_history(authed_page_snmp, _status_body(media_width_mm=62.0, media_type="continuous"))
    authed_page_snmp.goto("/history")

    expect(authed_page_snmp.locator("#roll-note")).to_be_visible()
    expect(authed_page_snmp.locator("#roll-note")).to_contain_text("62mm continuous")

    match_row = authed_page_snmp.locator("#history-body .job-row").filter(has_text="text-62")
    mismatch_row = authed_page_snmp.locator("#history-body .job-row").filter(has_text="addr-17x54")

    # Matching 62mm row: reprint enabled, no mismatch tag, not dimmed.
    expect(match_row.locator("button.btn-reprint")).to_be_enabled()
    expect(match_row.locator(".tag-mismatch")).to_have_count(0)
    expect(match_row).not_to_have_class(re.compile("row-incompatible"))

    # Mismatched 17x54 row: reprint disabled with a ✗ tag; the row is dimmed; Delete still enabled.
    expect(mismatch_row.locator("button.btn-reprint")).to_be_disabled()
    mismatch_tag = mismatch_row.locator(".tag-mismatch")
    expect(mismatch_tag).to_contain_text("needs 17mm")
    expect(mismatch_tag).to_contain_text("54mm die-cut")
    expect(mismatch_row).to_have_class(re.compile("row-incompatible"))
    expect(mismatch_row.locator("button.btn-delete")).to_be_enabled()


def test_history_keeps_reprint_enabled_when_roll_unknown(authed_page_snmp: Page) -> None:
    """On the SNMP deployment, when the printer is unreachable the loaded roll is unknown, so size
    gating fails open: every row stays reprintable, no row is dimmed, and the roll-note is hidden."""
    import json

    unreachable = json.dumps(
        {"state": "off", "uri": "tcp://192.0.2.10:9100", "reachable": False, "errors": []}
    )
    _route_history(authed_page_snmp, unreachable)
    authed_page_snmp.goto("/history")

    # Both rows render and neither reprint is gated.
    rows = authed_page_snmp.locator("#history-body .job-row")
    expect(rows).to_have_count(2)
    expect(authed_page_snmp.locator("#history-body button.btn-reprint:disabled")).to_have_count(0)
    expect(authed_page_snmp.locator("#history-body .tag-mismatch")).to_have_count(0)
    expect(authed_page_snmp.locator("#history-body .job-row.row-incompatible")).to_have_count(0)
    expect(authed_page_snmp.locator("#roll-note")).to_be_hidden()


def _history_options_body() -> str:
    """A /history/list page whose rows exercise each render-option pill. The server default threshold
    in the e2e harness is the config default (70.0), so a 70.0 cutoff must show NO threshold pill."""
    import json

    def row(job: str, marker: str, options: dict[str, object]) -> dict[str, object]:
        return {
            "job_id": job,
            "template": "text-62",
            "fields": {"title": marker},
            "copies": 1,
            "dry_run": False,
            "timestamp": "2026-06-30T12:00:00",
            "status": "printed",
            "options": options,
            "image_stripped": False,
            "sequence": None,
        }

    base = {"dither": False, "threshold": 70.0, "high_res": False, "red": False}
    # Markers must be mutually non-substring — the row locator filters on has_text (substring match).
    entries = [
        row("aaaaaaaa-0000-0000-0000-000000000001", "mk-defaults", {**base}),
        row("aaaaaaaa-0000-0000-0000-000000000002", "mk-hires", {**base, "high_res": True}),
        row("aaaaaaaa-0000-0000-0000-000000000003", "mk-twocolor", {**base, "red": True}),
        row("aaaaaaaa-0000-0000-0000-000000000004", "mk-ditheronly", {**base, "dither": True}),
        row("aaaaaaaa-0000-0000-0000-000000000005", "mk-thronly", {**base, "threshold": 55.0}),
        row(
            "aaaaaaaa-0000-0000-0000-000000000006",
            "mk-both",
            {**base, "dither": True, "threshold": 55.0},
        ),
    ]
    return json.dumps({"entries": entries, "total": len(entries), "offset": 0, "limit": 20})


def test_history_shows_render_option_pills(authed_page: Page) -> None:
    """Each history row surfaces its non-default render options as pills: a defaults-only job shows
    none; high-res/two-color/dither each show their pill; a custom threshold shows `thr N%` — but only
    when dither is off (it is inert under dither, so no threshold pill appears on a dithered row)."""
    import json

    authed_page.route(
        "**/templates",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HISTORY_TEMPLATES)
        ),
    )
    authed_page.route(
        "**/history/list*",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=_history_options_body()
        ),
    )
    authed_page.goto("/history")

    def pills(marker: str) -> Locator:
        row = authed_page.locator("#history-body .job-row").filter(has_text=marker)
        return row.locator(".job-opts .pill")

    # Defaults only → no option pills at all (no .job-opts line rendered).
    expect(pills("mk-defaults")).to_have_count(0)
    # Each active boolean option → exactly its pill.
    expect(pills("mk-hires")).to_have_text(["600 dpi"])
    expect(pills("mk-twocolor")).to_have_text(["two-color"])
    expect(pills("mk-ditheronly")).to_have_text(["dither"])
    # Non-default threshold with dither off → the threshold pill.
    expect(pills("mk-thronly")).to_have_text(["thr 55%"])
    # Dither on suppresses the (inert) threshold pill: only the dither pill remains.
    expect(pills("mk-both")).to_have_text(["dither"])


def test_history_regates_reprints_live_on_roll_swap(authed_page_snmp: Page) -> None:
    """On the SNMP deployment (live_status_poll ON) the history page background-polls /printer/status,
    so swapping the roll re-gates the rows with no reload — the print page's model applied to reprints.
    Serve a 62mm roll first (gates the 17x54 row), then 17x54 on every later poll (gates the 62mm row
    instead). Regression guard that the gating is driven live by the poll, not frozen at page load."""
    import json

    calls = {"n": 0}

    def handle(route: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            media = {"media_width_mm": 62.0, "media_type": "continuous"}
        else:
            media = {"media_width_mm": 17.0, "media_type": "die_cut", "media_length_mm": 54.0}
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "state": "idle",
                    "uri": "tcp://192.0.2.10:9100",
                    "reachable": True,
                    "model": "Brother QL-810W",
                    "errors": [],
                    **media,
                }
            ),
        )

    _route_history_lists(authed_page_snmp)
    authed_page_snmp.route("**/printer/status", handle)
    authed_page_snmp.goto("/history")

    row_62 = authed_page_snmp.locator("#history-body .job-row").filter(has_text="text-62")
    row_17 = authed_page_snmp.locator("#history-body .job-row").filter(has_text="addr-17x54")

    # Initial 62mm roll: the 17x54 row is gated, the 62mm row is not.
    expect(row_17.locator("button.btn-reprint")).to_be_disabled()
    expect(row_62.locator("button.btn-reprint")).to_be_enabled()

    # After the background poll reports the swapped 17x54 roll, the gating flips with no reload.
    expect(row_62.locator("button.btn-reprint")).to_be_disabled(timeout=15000)
    expect(row_17.locator("button.btn-reprint")).to_be_enabled()
    expect(authed_page_snmp.locator("#roll-note")).to_contain_text("17mm")
    assert calls["n"] >= 2, f"expected the background poll to fire at least twice; got {calls['n']}"


def _history_list_body_custom(*rows: dict[str, object]) -> str:
    """A /history/list page from explicit row dicts (for the dry-run / fail-open regression cases)."""
    import json

    return json.dumps({"entries": list(rows), "total": len(rows), "offset": 0, "limit": 20})


def _hist_row(template: str, job: str, *, dry_run: bool = False, status: str = "printed") -> dict:
    return {
        "job_id": job,
        "template": template,
        "fields": {"name": "Jane Doe"},
        "copies": 1,
        "dry_run": dry_run,
        "timestamp": "2026-06-30T12:00:00",
        "status": status,
        "dither": False,
        "image_stripped": False,
        "sequence": None,
    }


def test_history_does_not_gate_dry_run_reprints(authed_page_snmp: Page) -> None:
    """A dry-run reprint sends nothing to the printer and the server skips the SNMP media preflight for
    dry_run=True, so it is accepted regardless of the loaded roll. The size gate must therefore NEVER
    disable a dry-run row — even one whose template mismatches the loaded roll. A printed row of the
    same mismatching template is the control: it stays gated."""
    import json

    # Loaded 62mm roll; both rows use the 17x54 template (a mismatch), one dry-run, one printed.
    authed_page_snmp.route(
        "**/templates",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HISTORY_TEMPLATES)
        ),
    )
    authed_page_snmp.route(
        "**/printer/status",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62.0, media_type="continuous"),
        ),
    )
    authed_page_snmp.route(
        "**/history/list*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=_history_list_body_custom(
                _hist_row(
                    "addr-17x54",
                    "dddddddd-0000-0000-0000-00000000000d",
                    dry_run=True,
                    status="dry-run",
                ),
                _hist_row("addr-17x54", "eeeeeeee-0000-0000-0000-00000000000e"),
            ),
        ),
    )
    authed_page_snmp.goto("/history")

    dry_row = authed_page_snmp.locator("#history-body .job-row").filter(has_text="dry-run")
    printed_row = authed_page_snmp.locator("#history-body .job-row").filter(has_text="printed")

    # Control first: the printed row of the mismatching template IS gated — proves gating is active
    # here, so the dry-run row's enabled state below is meaningful (not just gating being off).
    expect(printed_row.locator("button.btn-reprint")).to_be_disabled()
    # Dry-run row: reprintable despite the roll mismatch — not disabled, not dimmed, no ✗ tag.
    expect(dry_row.locator("button.btn-reprint")).to_be_enabled()
    expect(dry_row.locator(".tag-mismatch")).to_have_count(0)
    expect(dry_row).not_to_have_class(re.compile("row-incompatible"))


def test_history_renders_promptly_when_status_hangs(authed_page_snmp: Page) -> None:
    """Loaded-roll detection is advisory and must fail open: even on the SNMP path, a slow/hung
    /printer/status must not block the history list (the page's core content) from rendering. With
    status never resolving, the rows must still appear well before the client's 8s abort would fire."""
    _route_history_lists(authed_page_snmp)
    # Never fulfil the status request — it hangs until the page's AbortController aborts it at ~8s.
    authed_page_snmp.route("**/printer/status", lambda route: None)
    authed_page_snmp.goto("/history")

    # Rows must render promptly (< the 8s abort): if init blocked on status, they'd appear only at ~8s.
    expect(authed_page_snmp.locator("#history-body .job-row")).to_have_count(2, timeout=4000)
    # Status never resolved → roll unknown → nothing gated (fail-open default).
    expect(authed_page_snmp.locator("#history-body button.btn-reprint:disabled")).to_have_count(0)


def test_history_does_not_probe_status_on_non_snmp(authed_page: Page) -> None:
    """On a non-SNMP deployment (the ``authed_page`` fixture, live_status_poll OFF) the History page
    must NEVER fetch /printer/status: there the read serializes through the server's print lock, so a
    probe could delay a concurrent /reprint — and the media type reads as unknown anyway, so the gate
    could never fire. Assert zero status hits, that rows render, and that nothing is gated (fail-open).
    An unconditional init probe would reintroduce lock contention here."""
    status_hits = {"n": 0}

    def count_status(route: object) -> None:
        status_hits["n"] += 1
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62.0, media_type="continuous"),
        )

    _route_history_lists(authed_page)
    authed_page.route("**/printer/status", count_status)
    authed_page.goto("/history")

    # The list renders from /history/list with no dependency on the (forbidden) status probe.
    expect(authed_page.locator("#history-body .job-row")).to_have_count(2)
    # Give any errant init probe / scheduled poll ample time to fire (poll base interval is ~4s).
    authed_page.wait_for_timeout(6000)
    assert status_hits["n"] == 0, (
        f"non-SNMP History must not probe /printer/status; got {status_hits['n']} hits"
    )
    # Even though a 62mm roll would mismatch the 17x54 row, nothing is gated (no status → no roll).
    expect(authed_page.locator("#history-body button.btn-reprint:disabled")).to_have_count(0)
    expect(authed_page.locator("#history-body .job-row.row-incompatible")).to_have_count(0)
    expect(authed_page.locator("#roll-note")).to_be_hidden()


def test_print_page_focuses_size_group_on_usb_without_polling(authed_page_usb: Page) -> None:
    """On a USB deployment the print page reads the loaded roll ONCE at load (status_supported) and
    focuses the matching size group — the ESC i S media read now drives grouping just like SNMP — but
    it must NOT background-poll (live_status_poll OFF: the single USB device handle must not be claimed
    on a timer). Route a 62mm continuous USB status; assert focus mode engaged and only the load-time
    read fired."""
    calls = {"n": 0}
    body = _status_body(
        uri="usb://0x04f9:0x209c",
        media_width_mm=62.0,
        media_type="continuous",
        media_length_mm=None,
    )

    def handle(route: object) -> None:
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=body)  # type: ignore[attr-defined]

    authed_page_usb.route("**/printer/status", handle)
    authed_page_usb.goto("/")

    # A known roll → focus mode: only the matching 62mm continuous group (marked as the loaded
    # roll), plus the reveal UI.
    groups = authed_page_usb.locator("#template-groups .tpl-group")
    expect(groups).to_have_count(1, timeout=8000)
    expect(groups.first.locator(".group-label")).to_have_text("62mm continuous")
    expect(groups.first).to_have_attribute("data-match", "1")
    expect(authed_page_usb.locator("#size-filter")).to_be_visible()
    expect(authed_page_usb.locator("#size-filter-hint")).to_contain_text("hidden")

    # No background poll: wait past a poll interval and assert only the load-time read fired.
    authed_page_usb.wait_for_timeout(9000)
    assert calls["n"] <= 1, (
        f"USB print page must not background-poll; got {calls['n']} /printer/status hits"
    )


def test_history_flags_reprints_advisory_on_usb_without_polling(authed_page_usb: Page) -> None:
    """On a USB deployment History reads the loaded roll ONCE at load and FLAGS a size mismatch (✗ tag
    on the 17x54 die-cut row against a 62mm continuous roll) — but ADVISORY only: the Reprint button
    stays ENABLED and the row is NOT dimmed. USB has no background poll to refresh the roll, so a hard
    disable would become a stale block on a valid reprint after a roll swap; the server /reprint 409 is
    the authoritative gate instead (mirrors the print page). Also asserts no background polling."""
    calls = {"n": 0}
    body = _status_body(uri="usb://0x04f9:0x209c", media_width_mm=62.0, media_type="continuous")

    def handle(route: object) -> None:
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=body)  # type: ignore[attr-defined]

    _route_history_lists(authed_page_usb)
    authed_page_usb.route("**/printer/status", handle)
    authed_page_usb.goto("/history")

    # The mismatched 17x54 row is flagged (✗ tag) but stays ENABLED and undimmed — no stale hard block.
    mismatch_row = authed_page_usb.locator("#history-body .job-row").filter(has_text="addr-17x54")
    expect(mismatch_row.locator(".tag-mismatch")).to_contain_text("needs 17mm")
    expect(mismatch_row.locator("button.btn-reprint")).to_be_enabled()
    expect(mismatch_row).not_to_have_class(re.compile("row-incompatible"))
    # The matching 62mm row is reprintable and unflagged.
    match_row = authed_page_usb.locator("#history-body .job-row").filter(has_text="text-62")
    expect(match_row.locator("button.btn-reprint")).to_be_enabled()
    expect(match_row.locator(".tag-mismatch")).to_have_count(0)
    expect(authed_page_usb.locator("#roll-note")).to_contain_text("flagged")

    # No background poll over USB.
    authed_page_usb.wait_for_timeout(9000)
    assert calls["n"] <= 1, (
        f"USB History must not background-poll; got {calls['n']} /printer/status hits"
    )


def test_history_reprint_error_detail_renders_inert(authed_page: Page) -> None:
    """A hostile /reprint 409 detail (e.g. a spoofed SNMP media_name carrying markup) must render as
    INERT TEXT in the status banner, never as live DOM. #status-area is on a token-bearing page, so an
    innerHTML sink here would let a printer-/SNMP-controlled string run script and exfiltrate the API
    token from localStorage. Covers the history.html showStatus render path."""
    import json

    _route_history_lists(authed_page)  # non-SNMP page → every row reprintable
    payload = "<img src=x onerror=window.__xss=1>"
    authed_page.route(
        "**/reprint/*",
        lambda r: r.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"detail": {"msg": "media mismatch", "media_loaded": payload}}),
        ),
    )
    authed_page.goto("/history")
    authed_page.locator("#history-body button.btn-reprint").first.click()

    status_area = authed_page.locator("#status-area")
    expect(status_area).to_contain_text("Reprint error")
    # The markup must NOT have become a real element, and the onerror must not have fired.
    expect(status_area.locator("img")).to_have_count(0)
    assert authed_page.evaluate("() => window.__xss === undefined"), (
        "reprint-error markup must not execute as script"
    )


# ── Shared nav: language picker, theme toggle, feature-flag tabs ──────────────────────────────────


def test_language_choice_is_sent_in_preview_payload(authed_page: Page) -> None:
    """Picking a label language in the nav must ride along as `language` in the /preview payload —
    that is what makes [[chrome-word]] tokens render localized in the live preview."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.select_option("#language-select", "es")
    body = req_info.value.post_data_json
    assert body is not None and body["language"] == "es", (
        f"preview payload should carry the picked language, got {body!r}"
    )


def test_saved_language_applies_to_the_first_preview_after_reload(authed_page: Page) -> None:
    """A persisted language must be live BEFORE the page's initial preview fires — a restore
    that lands later (e.g. at DOMContentLoaded) would render the first preview in the default
    language while a subsequent print sends the saved one."""
    authed_page.goto("/")
    authed_page.select_option("#language-select", "de")

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.reload()
    body = req_info.value.post_data_json
    assert body is not None and body["language"] == "de", (
        f"the first preview after reload should carry the saved language, got {body!r}"
    )


def test_stale_preview_response_does_not_overwrite_a_newer_one(authed_page: Page) -> None:
    """Previews fire from several triggers with no serialization, so responses can land out of
    order — a slow stale response must not replace the newer preview the user is about to print."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    # Drain the debounced field-edit preview completely before installing the route, so no
    # straggler request gets caught in the hold below.
    with authed_page.expect_response(
        lambda r: r.url.endswith("/preview") and r.request.method == "POST"
    ):
        _fill_all_fields(authed_page)
    authed_page.wait_for_timeout(700)

    held: dict[str, object] = {}

    def _hold_first(route) -> None:
        if "route" not in held:
            held["route"] = route  # keep the stale request pending
        else:
            route.continue_()

    authed_page.route("**/preview", _hold_first)

    # Stale trigger: this request is held open server-side...
    with authed_page.expect_request(lambda r: r.url.endswith("/preview") and r.method == "POST"):
        authed_page.select_option("#language-select", "es")
    # ...while a newer trigger completes normally and renders a real label.
    with authed_page.expect_response(
        lambda r: r.url.endswith("/preview") and r.request.method == "POST"
    ):
        authed_page.select_option("#language-select", "de")
    authed_page.wait_for_function("() => document.getElementById('preview-img').naturalWidth > 1")
    fresh_width = authed_page.evaluate("() => document.getElementById('preview-img').naturalWidth")

    # Now the stale response finally lands — as a 1x1 PNG so an overwrite is unmistakable.
    held["route"].fulfill(status=200, content_type="image/png", body=PNG_1PX)  # type: ignore[attr-defined]
    authed_page.wait_for_timeout(300)
    width_after = authed_page.evaluate("() => document.getElementById('preview-img').naturalWidth")
    assert width_after == fresh_width, (
        f"stale preview response replaced the newer preview (naturalWidth {width_after})"
    )


def test_stale_parse_response_does_not_overwrite_the_studio_field_form(authed_page: Page) -> None:
    """The studio's /templates/parse responses can land out of order just like previews — a slow
    stale parse must not replace `contract`/the rendered sample-field form, or the visible inputs
    would belong to an older draft and later preview/save payloads would mis-shape their values."""
    import json

    with authed_page.expect_response(lambda r: "/preview/draft" in r.url):
        authed_page.goto("/editor")
    # Let the initial seeded preview chain drain completely before installing the route.
    authed_page.wait_for_timeout(700)

    held: dict[str, object] = {}

    def _hold_first(route) -> None:
        if "route" not in held:
            held["route"] = route  # keep the stale parse pending
        else:
            route.continue_()

    authed_page.route("**/templates/parse", _hold_first)

    def _draft_yaml(field: str) -> str:
        # The seeded starter template with only the field name swapped — known-valid YAML.
        return (
            f"name: my-label\n"
            f"description: A new label\n"
            f'label: "62"\n'
            f"rotate: 0\n"
            f"fields:\n"
            f"  required: [{field}]\n"
            f"layout:\n"
            f'  - {{type: title, text: "{{{{{field}}}}}"}}\n'
        )

    # Stale trigger: its parse request is held open...
    with authed_page.expect_request(lambda r: r.url.endswith("/templates/parse")):
        authed_page.locator("#yaml").fill(_draft_yaml("stalefield"))
    # ...while a newer edit parses and previews normally, rendering its field input.
    with authed_page.expect_response(lambda r: "/preview/draft" in r.url):
        authed_page.locator("#yaml").fill(_draft_yaml("freshfield"))
    expect(authed_page.locator("#field-freshfield")).to_be_visible()

    # The stale parse finally lands, carrying the older draft's contract.
    held["route"].fulfill(  # type: ignore[attr-defined]
        status=200,
        content_type="application/json",
        body=json.dumps({"fields": {"required": ["stalefield"], "optional": []}}),
    )
    authed_page.wait_for_timeout(300)
    expect(authed_page.locator("#field-freshfield")).to_be_visible()
    assert authed_page.locator("#field-stalefield").count() == 0, (
        "a stale /templates/parse response must not re-render the field form"
    )


def test_language_choice_persists_across_reload(authed_page: Page) -> None:
    """The nav language pick is a durable preference (localStorage), not a per-page transient."""
    authed_page.goto("/")
    authed_page.select_option("#language-select", "de")
    authed_page.reload()
    expect(authed_page.locator("#language-select")).to_have_value("de")


def test_theme_toggle_persists_across_reload(authed_page: Page) -> None:
    """An explicit theme choice must survive a reload (and win over prefers-color-scheme)."""
    authed_page.goto("/")
    initial = authed_page.evaluate("() => document.documentElement.dataset.theme")
    assert initial in ("light", "dark")
    flipped = "dark" if initial == "light" else "light"

    authed_page.click("#theme-toggle")
    assert authed_page.evaluate("() => document.documentElement.dataset.theme") == flipped

    authed_page.reload()
    assert authed_page.evaluate("() => document.documentElement.dataset.theme") == flipped, (
        "explicit theme choice should persist via localStorage"
    )


def test_nav_tabs_reflect_enabled_features_and_active_page(authed_page: Page) -> None:
    """The shared nav renders one tab per enabled feature (the harness enables history + editor)
    and marks the current page's tab active on every shell."""
    authed_page.goto("/")
    tabs = authed_page.locator(".nav .tab")
    expect(tabs).to_have_count(3)
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Print"))

    authed_page.goto("/history")
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("History"))

    authed_page.goto("/editor")
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Studio"))


# ── Printer model/host relocated out of the nav strip (Step 1) ─────────────────────────────────────


def test_nav_no_longer_shows_printer_model_or_host(authed_page: Page) -> None:
    """The model/host string that used to sit in the nav strip is gone entirely."""
    authed_page.goto("/")
    expect(authed_page.locator(".nav-host")).to_have_count(0)


def test_printer_status_model_moved_to_details_disclosure(authed_page_snmp: Page) -> None:
    """Model now lives ONLY in the printer-status card's Details disclosure, next to the URI — not
    in the always-visible primary line."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62, media_type="continuous", media_length_mm=None),
        ),
    )
    authed_page_snmp.goto("/")
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Idle", timeout=8000)

    # Not present in the always-visible primary line (direct <div> children; <details> is separate).
    primary_text = authed_page_snmp.locator("#printer-detail > div").all_inner_texts()
    assert not any("Model" in line for line in primary_text), primary_text

    # Present inside the Details disclosure, alongside the URI.
    details = authed_page_snmp.locator("#printer-detail details")
    details.locator("summary").click()
    expect(details).to_contain_text("Model: Brother QL-810W")
    expect(details).to_contain_text("URI")


# ── Richer printer Details: hostname, status/phase, model-mismatch warning (round 3) ────────────────


def test_printer_status_details_show_hostname_status_and_phase(authed_page_snmp: Page) -> None:
    """The Details disclosure surfaces the printer's SNMP hostname (next to the URI) and, when
    reported, its raw Status/Phase strings — USB users' only live-state detail."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=62,
                media_type="continuous",
                media_length_mm=None,
                hostname="labelprinter",
                status="Reply to status request",
                phase="Waiting to receive",
            ),
        ),
    )
    authed_page_snmp.goto("/")
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Idle", timeout=8000)

    details = authed_page_snmp.locator("#printer-detail details")
    details.locator("summary").click()
    expect(details).to_contain_text("Hostname: labelprinter")
    expect(details).to_contain_text("Status: Reply to status request")
    expect(details).to_contain_text("Phase: Waiting to receive")


def test_printer_status_model_mismatch_warns_in_primary_area(authed_page_snmp: Page) -> None:
    """A model_mismatch=True status renders an amber warning in the always-visible primary area,
    naming both the device-reported and configured (QL-810W) models — surfaces a field that was
    previously serialized but dead in the UI."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=62,
                media_type="continuous",
                media_length_mm=None,
                model="Brother QL-1100",
                model_mismatch=True,
            ),
        ),
    )
    authed_page_snmp.goto("/")
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Idle", timeout=8000)

    warning = authed_page_snmp.locator("#printer-detail > div.hint-warn")
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("Brother QL-1100")
    expect(warning).to_contain_text("differs from the configured")
    # Amber .hint-warn, not the fatal-looking .detail-err reserved for genuine printer faults.
    assert "detail-err" not in (warning.get_attribute("class") or "")


def test_printer_status_console_is_its_own_kv_row(authed_page_snmp: Page) -> None:
    """The SNMP console line renders as a dedicated Media/Connection-style kv row (right-aligned via
    .kv .v), not a loose #printer-detail line — so it aligns with the rest of the status card."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=62,
                media_type="continuous",
                media_length_mm=None,
                console_text="READY",
            ),
        ),
    )
    authed_page_snmp.goto("/")
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Idle", timeout=8000)

    row = authed_page_snmp.locator("#console-row")
    expect(row).to_be_visible()
    expect(row.locator(".k")).to_have_text("Console")
    expect(authed_page_snmp.locator("#console-value")).to_have_text("READY")


def test_printer_status_console_not_duplicated_as_error_line(authed_page_snmp: Page) -> None:
    """A non-READY console (e.g. the transient PRINTING) that the SNMP layer also echoes into `errors`
    as `console: …` must NOT double-render: it shows once in the Console row, and the echo is filtered
    from the red .detail-err fault line."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=62,
                media_type="continuous",
                media_length_mm=None,
                console_text="PRINTING",
                errors=["console: PRINTING"],
            ),
        ),
    )
    authed_page_snmp.goto("/")
    expect(authed_page_snmp.locator("#console-value")).to_have_text("PRINTING", timeout=8000)
    # The echo is filtered — no red fault line repeating the console text.
    assert authed_page_snmp.locator("#printer-detail .detail-err").count() == 0


def test_printer_status_offline_shows_calm_info_notice(authed_page_snmp: Page) -> None:
    """A printer that is simply off/unreachable is a benign, expected state — it must render as a calm
    info notice with a plain-language headline and a "what to do" hint, NOT the alarm-red raw-string
    dump it used to be. The raw diagnostic is preserved (collapsed) under Details → Diagnostic."""
    import json

    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=503,
            content_type="application/json",
            body=json.dumps(
                {
                    "state": "off",
                    "uri": "tcp://192.0.2.10:9100",
                    "reachable": False,
                    "errors": ["printer SNMP agent did not respond"],
                }
            ),
        ),
    )
    authed_page_snmp.goto("/")

    note = authed_page_snmp.locator("#printer-detail .status-note")
    expect(note).to_be_visible(timeout=8000)
    # Info severity, not error — a printer being off is not a fault.
    expect(note).to_have_class(re.compile(r"\bstatus-note--info\b"))
    assert authed_page_snmp.locator(".status-note--error").count() == 0
    expect(note.locator(".status-note-title")).to_have_text("Printer offline")
    expect(note.locator(".status-note-hint")).to_contain_text("turn it on")
    # The raw backend string is never lost — it lives (collapsed) under Details → Diagnostic.
    details = authed_page_snmp.locator("#printer-detail details")
    details.locator("summary").click()
    expect(details).to_contain_text("Diagnostic: printer SNMP agent did not respond")


def test_printer_status_hard_fault_shows_error_notice(authed_page_snmp: Page) -> None:
    """A genuine device fault (an unrecognised condition string) escalates to the red error notice with
    a warning icon — the severity system keeps real faults loud while quieting the benign off state."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(
                media_width_mm=62,
                media_type="continuous",
                media_length_mm=None,
                state="error",
                errors=["cover open"],
            ),
        ),
    )
    authed_page_snmp.goto("/")

    note = authed_page_snmp.locator("#printer-detail .status-note")
    expect(note).to_be_visible(timeout=8000)
    expect(note).to_have_class(re.compile(r"\bstatus-note--error\b"))
    expect(note.locator(".status-note-title")).to_have_text("cover open")


def test_about_modal_opens_fills_runtime_and_closes(authed_page: Page) -> None:
    """The nav About button opens the version/about modal: static rows (version, repo, license) are
    server-rendered, the runtime rows (model/transport/templates) fill from /health on open, and Esc
    closes it (native <dialog>)."""
    authed_page.goto("/")
    dialog = authed_page.locator("#about-dialog")
    expect(dialog).to_be_hidden()

    authed_page.locator("#about-open").click()
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("About labelito")
    expect(dialog.locator("#about-title")).to_be_visible()
    expect(dialog.locator('a[href="https://github.com/chiva/labelito"]')).to_be_visible()
    # Runtime rows resolve from /health (real test server), replacing the "…" placeholder.
    expect(dialog.locator("#about-model")).not_to_have_text("…", timeout=5000)
    expect(dialog.locator("#about-transport")).not_to_have_text("…")

    authed_page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


def test_token_modal_opens_from_nav_and_persists(anon_page: Page) -> None:
    """The API-token entry is a single shared nav modal (not a per-page card): the key button opens
    it, typing persists to localStorage, and Esc closes it. Uses anon_page so the key button also
    carries its "unsaved" hint until a token is entered."""
    anon_page.goto("/")
    dialog = anon_page.locator("#token-dialog")
    key_btn = anon_page.locator("#token-open")
    expect(dialog).to_be_hidden()
    expect(key_btn).to_have_class(re.compile(r"\bneeds-token\b"))

    key_btn.click()
    expect(dialog).to_be_visible()
    anon_page.fill("#api-token", "typed-token")
    assert anon_page.evaluate("() => localStorage.getItem('labelito_api_token')") == "typed-token"
    # A stored token clears the "unsaved" hint.
    expect(key_btn).not_to_have_class(re.compile(r"\bneeds-token\b"))

    anon_page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()


def test_printer_status_details_show_web_ui_link_for_network(authed_page_snmp: Page) -> None:
    """A tcp:// printer gets a Web UI link (http://<host>) in the Details disclosure, built from the
    print URI's host — shown unconditionally (no port-80 probe)."""
    authed_page_snmp.route(
        "**/printer/status",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=_status_body(media_width_mm=62, media_type="continuous", media_length_mm=None),
        ),
    )
    authed_page_snmp.goto("/")
    expect(authed_page_snmp.locator("#printer-state")).to_have_text("Idle", timeout=8000)

    link = authed_page_snmp.locator('#printer-detail details a[href="http://192.0.2.10"]')
    expect(link).to_have_count(1)


# ── Media badge de-duplication (Step 2) ─────────────────────────────────────────────────────────────


def test_media_badge_unknown_has_no_duplicate_media_prefix(authed_page: Page) -> None:
    """The unknown-compat badge text is just the media description — no redundant "media: " prefix
    (the card's own "Media" kv label already says that)."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    badge = authed_page.locator("#media-badge")
    expect(badge).to_have_class(re.compile(r"media-unknown"))
    text = badge.inner_text()
    assert not text.lower().startswith("media:"), f"badge text should not repeat 'media:': {text!r}"


# ── Capability-aware print options (Step 3) ─────────────────────────────────────────────────────────


def test_red_toggle_disabled_with_hint_for_non_red_template(authed_page: Page) -> None:
    """A template not bound to black/red media disables #red client-side with the "needs …" hint,
    even though the configured model (QL-810W) is two-color-capable."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)  # title-subtitle: plain 62 media, not red
    red = authed_page.locator("#red")
    expect(red).to_be_disabled()
    expect(red).not_to_be_checked()
    expect(authed_page.locator("#red-hint-disabled")).to_be_visible()
    expect(authed_page.locator("#red-hint-disabled")).to_contain_text("62red")
    expect(authed_page.locator("#red-roll-note")).to_be_hidden()


def test_red_toggle_enabled_with_roll_note_for_red_capable_template(authed_page: Page) -> None:
    """A red-capable template enables #red and surfaces the "roll colour can't be verified" note.

    None of the shipped templates are bound to red media, so a synthetic one is injected the same
    way test_media_compatibility_badges_are_advisory does, then selected via the same
    onTemplatePicked() path a real card click would take.
    """
    authed_page.goto("/")
    authed_page.evaluate(
        """() => {
          templateMap['__red'] = {name: '__red', description: 'red test', required: [], optional: [],
            media: {width_mm: 62.0, media_type: 'continuous', length_mm: null}, red: true};
          TEMPLATES.push(templateMap['__red']);
          onTemplatePicked('__red');
        }"""
    )
    red = authed_page.locator("#red")
    expect(red).to_be_enabled()
    expect(authed_page.locator("#red-hint-default")).to_be_visible()
    expect(authed_page.locator("#red-hint-disabled")).to_be_hidden()
    expect(authed_page.locator("#red-roll-note")).to_be_visible()
    expect(authed_page.locator("#red-roll-note")).to_contain_text("colour")


def test_red_capable_template_gets_red_pill_on_its_card(authed_page: Page) -> None:
    """A red-capable template's card carries a red pill for discoverability without opening
    Print options first."""
    authed_page.goto("/")
    authed_page.evaluate(
        """() => {
          templateMap['__red2'] = {name: '__red2', description: 'red test', required: [], optional: [],
            media: {width_mm: 62.0, media_type: 'continuous', length_mm: null}, red: true};
          TEMPLATES.push(templateMap['__red2']);
        }"""
    )
    # Force a full picker rebuild (the toggle flips showAllSizes, which bypasses the
    # skip-rebuild-if-nothing-changed fast path) so the freshly-injected template renders a card.
    authed_page.evaluate("() => { showAllSizes = true; rebuildTemplatePicker(); }")
    card = authed_page.locator('.tpl-card[data-name="__red2"]')
    expect(card).to_be_visible()
    expect(card.locator(".pill-red")).to_have_text("red")


def test_high_res_toggle_disabled_with_hint_on_unsupported_model(
    authed_page_low_res: Page,
) -> None:
    """On a model outside the curated 600dpi set (QL-500), #high-res renders disabled with a hint
    naming the model — a purely server-rendered, per-model (not per-template) gate."""
    authed_page_low_res.goto("/")
    high_res = authed_page_low_res.locator("#high-res")
    expect(high_res).to_be_disabled()
    expect(high_res).not_to_be_checked()
    # QL-500 has no two-color support either, so #red is absent and .hint-warn is unambiguous here.
    expect(authed_page_low_res.locator(".hint-warn")).to_contain_text("not supported by QL-500")


def test_dither_and_threshold_share_the_bw_conversion_group(authed_page: Page) -> None:
    """Dither and threshold are two modes of the same B/W conversion and must render inside the same
    visually-grouped `.option-group` container (round 3 discoverability ask)."""
    authed_page.goto("/")
    group = authed_page.locator(".option-group")
    expect(group).to_be_visible()
    expect(group.locator("#dither")).to_be_visible()
    expect(group.locator("#threshold")).to_be_visible()


# ── Inline preview errors (no toast) ────────────────────────────────────────────────────────────────


def test_preview_placeholder_shown_before_any_successful_preview(authed_page: Page) -> None:
    """A failed FIRST preview (no prior successful image to fall back on) must show the deliberate
    #preview-placeholder instead of leaving a sourceless <img> to render the browser's broken-image
    glyph. A subsequent successful preview swaps back to the image and hides the placeholder."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)

    authed_page.route(
        "**/preview",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=422,
            content_type="application/json",
            body='{"detail": "forced preview failure"}',
        ),
    )
    with authed_page.expect_response(lambda r: r.url.endswith("/preview")):
        authed_page.click("button.btn-preview")

    expect(authed_page.locator("#preview-placeholder")).to_be_visible()
    expect(authed_page.locator("#preview-img")).to_be_hidden()
    assert authed_page.locator("#preview-error").inner_text(), (
        "expected an inline error message too"
    )

    authed_page.unroute("**/preview")
    authed_page.click("button.btn-preview")
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )
    expect(authed_page.locator("#preview-placeholder")).to_be_hidden()
    expect(authed_page.locator("#preview-img")).to_be_visible()


def test_preview_refresh_button_spins_while_a_preview_is_generating(authed_page: Page) -> None:
    """The ↻ preview button reflects an in-flight /preview via aria-busy (app.css spins the icon on
    it): busy while the request is held open, idle at the next rotation boundary once it lands — on
    failure too, so an error response can never leave the button spinning forever — and a
    near-instant response still plays one full rotation instead of a sub-perceptual flick."""
    authed_page.goto("/")
    # An explicit pick raises the refocus guard, so no status-driven refocus can fire a surprise
    # preview into the held route below.
    _select_template(authed_page, SAMPLE_TEMPLATE)
    btn = authed_page.locator("#preview-refresh")
    expect(btn).to_have_attribute("aria-busy", "false")  # the pick's own preview has landed

    held: dict[str, object] = {}
    authed_page.route("**/preview", lambda route: held.setdefault("route", route))
    with authed_page.expect_request(lambda r: r.url.endswith("/preview")):
        authed_page.click("button.btn-preview")
    expect(btn).to_have_attribute("aria-busy", "true")
    # And the attribute actually drives the animation — guards the CSS selector wiring too.
    assert (
        authed_page.eval_on_selector(
            "#preview-refresh .icon", "el => getComputedStyle(el).animationName"
        )
        == "lbl-spin"
    )

    held["route"].fulfill(status=200, content_type="image/png", body=PNG_1PX)  # type: ignore[attr-defined]
    expect(btn).to_have_attribute("aria-busy", "false")

    held.clear()
    with authed_page.expect_request(lambda r: r.url.endswith("/preview")):
        authed_page.click("button.btn-preview")
    expect(btn).to_have_attribute("aria-busy", "true")
    held["route"].fulfill(  # type: ignore[attr-defined]
        status=422, content_type="application/json", body='{"detail": "forced preview failure"}'
    )
    expect(btn).to_have_attribute("aria-busy", "false")

    # Minimum one rotation: fulfil instantly, so the response lands within milliseconds — well inside
    # the 800ms spin cycle the button must still be busy, and by the boundary it comes to rest.
    authed_page.unroute("**/preview")
    authed_page.route(
        "**/preview",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=200, content_type="image/png", body=PNG_1PX
        ),
    )
    with authed_page.expect_response(lambda r: r.url.endswith("/preview")):
        authed_page.click("button.btn-preview")
    authed_page.wait_for_timeout(300)
    assert btn.get_attribute("aria-busy") == "true", (
        "a near-instant preview must still spin for a full rotation"
    )
    expect(btn).to_have_attribute("aria-busy", "false")


def test_preview_error_renders_inline_in_preview_card_not_toast(authed_page: Page) -> None:
    """A failed /preview shows its reason INSIDE the Live preview card (dimming the stale image, if
    any) instead of a toast — as one friendly sentence with NO HTTP status code and no raw JSON
    (never JSON.stringify'd) — and a subsequent successful preview clears the error state."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.click("button.btn-preview")
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )

    # Force the next preview to fail with a detail shape the friendly mapper does not recognise (a
    # bare string under 422) — it must fall back to the generic sentence, never echo the raw detail
    # verbatim, the HTTP status code, or JSON punctuation.
    authed_page.route(
        "**/preview",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=422,
            content_type="application/json",
            body='{"detail": "forced preview failure"}',
        ),
    )
    with authed_page.expect_response(lambda r: r.url.endswith("/preview")):
        authed_page.click("button.btn-preview")

    error = authed_page.locator("#preview-error")
    error_text = error.inner_text()
    assert error_text, "expected a friendly inline error message"
    assert "forced preview failure" not in error_text, (
        "an unrecognised detail shape must not be echoed verbatim"
    )
    assert not re.search(r"\d", error_text), (
        f"error must not leak an HTTP status code: {error_text!r}"
    )
    assert "{" not in error_text and "}" not in error_text, (
        f"error must not be raw JSON: {error_text!r}"
    )
    # Warning (amber), not the fatal-looking red .detail-err treatment used for printer faults.
    assert "detail-err" not in (error.get_attribute("class") or "")
    expect(authed_page.locator("#preview-img")).to_have_class(re.compile(r"preview-stale"))
    assert authed_page.locator(".status.err").count() == 0, (
        "a preview failure must render inline, never as a toast"
    )

    # Let the failing route go away and preview again: success clears the error + the stale dimming.
    authed_page.unroute("**/preview")
    authed_page.click("button.btn-preview")
    expect(authed_page.locator("#preview-error")).to_have_text("")
    expect(authed_page.locator("#preview-img")).not_to_have_class(re.compile(r"preview-stale"))


def test_preview_error_missing_required_field_shows_friendly_sentence(authed_page: Page) -> None:
    """The real 422 shape /preview sends for missing required fields
    (``{msg, template, missing_required: [...]}``) must render as one friendly sentence naming the
    missing field — never the raw dict, its keys, or the HTTP status code."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)
    authed_page.click("button.btn-preview")
    authed_page.wait_for_function(
        "() => { const i = document.getElementById('preview-img'); return i && i.naturalWidth > 0; }"
    )

    authed_page.route(
        "**/preview",
        lambda route: route.fulfill(  # type: ignore[attr-defined]
            status=422,
            content_type="application/json",
            body=(
                '{"detail": {"msg": "Missing required fields", "template": "title-subtitle", '
                '"missing_required": ["title"]}}'
            ),
        ),
    )
    with authed_page.expect_response(lambda r: r.url.endswith("/preview")):
        authed_page.click("button.btn-preview")

    error_text = authed_page.locator("#preview-error").inner_text()
    assert "title" in error_text, f"expected the missing field named in the message: {error_text!r}"
    assert "missing_required" not in error_text, f"must not leak the raw JSON key: {error_text!r}"
    assert "{" not in error_text and "}" not in error_text, f"must not be raw JSON: {error_text!r}"
    assert not re.search(r"\b422\b", error_text), f"must not leak the HTTP status: {error_text!r}"


# ── Language: default = browser locale unless overridden (Step 9) ──────────────────────────────────


def test_first_preview_after_reload_carries_persisted_language(authed_page: Page) -> None:
    """Regression for the init-order bug: the FIRST /preview payload after a reload must already
    carry the previously-saved language, not the server default (initLanguage must run before the
    page's own first doPreview())."""
    authed_page.goto("/")
    authed_page.select_option("#language-select", "de")

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.reload()
    body = req_info.value.post_data_json
    assert body is not None and body["language"] == "de", (
        f"first preview after reload should carry the persisted language, got {body!r}"
    )


def test_browser_locale_sets_default_language_when_no_saved_choice(
    browser: Browser, live_server: str
) -> None:
    """With no saved language, the dropdown (and the first preview) default to the browser's
    locale's primary subtag when it matches an available option."""
    context = browser.new_context(base_url=live_server, locale="es-ES")
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    page = context.new_page()
    try:
        with page.expect_request(
            lambda r: r.url.endswith("/preview") and r.method == "POST"
        ) as req_info:
            page.goto("/")
        expect(page.locator("#language-select")).to_have_value("es")
        body = req_info.value.post_data_json
        assert body is not None and body["language"] == "es", body
    finally:
        context.close()


def test_saved_language_choice_beats_browser_locale(browser: Browser, live_server: str) -> None:
    """An explicit saved choice always wins over the browser's locale-derived default."""
    context = browser.new_context(base_url=live_server, locale="es-ES")
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script("try { localStorage.setItem('labelito_language', 'de'); } catch (e) {}")
    page = context.new_page()
    try:
        page.goto("/")
        expect(page.locator("#language-select")).to_have_value("de")
    finally:
        context.close()


# ── Theme: default = OS preference unless overridden (Step 10) ─────────────────────────────────────


def test_theme_defaults_to_os_preference_without_persisting(
    browser: Browser, live_server: str
) -> None:
    """With no saved theme, a light-OS-preference context loads in light mode and detection itself
    never writes a localStorage key (only an explicit toggle may)."""
    context = browser.new_context(base_url=live_server, color_scheme="light")
    page = context.new_page()
    try:
        page.goto("/")
        assert page.evaluate("() => document.documentElement.dataset.theme") == "light"
        assert page.evaluate("() => localStorage.getItem('labelito_theme')") is None
    finally:
        context.close()


def test_saved_theme_choice_beats_os_preference(browser: Browser, live_server: str) -> None:
    """An explicit saved theme always wins over the OS colour-scheme preference."""
    context = browser.new_context(base_url=live_server, color_scheme="light")
    context.add_init_script("try { localStorage.setItem('labelito_theme', 'dark'); } catch (e) {}")
    page = context.new_page()
    try:
        page.goto("/")
        assert page.evaluate("() => document.documentElement.dataset.theme") == "dark"
    finally:
        context.close()


def test_theme_follows_os_preference_live_until_explicit_choice(
    browser: Browser, live_server: str
) -> None:
    """Absent an explicit choice, a live OS scheme change (emulated post-load) switches the theme
    without a reload — the whole point of Step 10's live-follow listener."""
    context = browser.new_context(base_url=live_server, color_scheme="light")
    page = context.new_page()
    try:
        page.goto("/")
        assert page.evaluate("() => document.documentElement.dataset.theme") == "light"
        page.emulate_media(color_scheme="dark")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        assert page.evaluate("() => localStorage.getItem('labelito_theme')") is None, (
            "OS-driven theme changes must never persist to localStorage"
        )
    finally:
        context.close()


# ── Print tab: template selection persists across tab switches (round 4) ───────────────────────────


def test_template_selection_persists_across_tab_switches(authed_page: Page) -> None:
    """An explicit template pick on the Print tab survives navigating away (Studio) and back — tabs
    are real page loads, so this only works if the pick is durable (sessionStorage), not a plain JS
    global."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    assert _selected_template(authed_page) == SAMPLE_TEMPLATE

    authed_page.click('.nav .tab[href="/editor"]')
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Studio"))
    authed_page.click('.nav .tab[href="/"]')
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Print"))

    assert _selected_template(authed_page) == SAMPLE_TEMPLATE, (
        "the template pick should survive the Print -> Studio -> Print round trip"
    )
    expect(authed_page.locator(f'.tpl-card[data-name="{SAMPLE_TEMPLATE}"]')).to_have_class(
        re.compile(r"\bselected\b")
    )


def test_page_load_alone_does_not_persist_a_template_choice(authed_page: Page) -> None:
    """Merely loading the Print tab (no click) must not write the persistence key — only an explicit
    pick in onTemplatePicked may, mirroring the theme/language default-must-not-persist guards."""
    authed_page.goto("/")
    assert authed_page.evaluate("() => sessionStorage.getItem('labelito_template')") is None
    assert authed_page.evaluate("() => localStorage.getItem('labelito_template')") is None


def test_saved_template_survives_first_status_arrival_and_keeps_group_visible(
    browser: Browser, live_server_snmp: str
) -> None:
    """A template saved from a previous page view must survive the FIRST /printer/status reply even
    when the loaded roll doesn't match it — restoring re-raises the same userOverride guard a live
    click would set (test_late_status_does_not_override_manual_template_choice, replayed from a prior
    page view). The restored template's own group must also stay visible alongside the matching-roll
    group, per desiredKey in rebuildTemplatePicker."""
    context = browser.new_context(base_url=live_server_snmp)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try { sessionStorage.setItem('labelito_template', 'title-subtitle'); } catch (e) {}"
    )
    page = context.new_page()
    try:
        page.route(
            "**/printer/status",
            lambda route: route.fulfill(  # type: ignore[attr-defined]
                status=200,
                content_type="application/json",
                body=_status_body(media_width_mm=29, media_type="continuous", media_length_mm=None),
            ),
        )
        page.goto("/")
        expect(page.locator("#printer-state")).to_have_text("Idle", timeout=8000)

        assert _selected_template(page) == "title-subtitle", (
            "the restored pick must survive the roll-driven refocus check"
        )
        # 29mm (the loaded roll's matching group) + 62mm (the restored template's own group).
        groups = page.locator("#template-groups .tpl-group")
        expect(groups).to_have_count(2)
        expect(page.locator("#size-filter-hint")).to_contain_text("hidden")
    finally:
        context.close()


def test_saved_template_with_unknown_name_falls_back_to_default(
    browser: Browser, live_server: str
) -> None:
    """A stale/bogus saved name (template renamed or removed since it was saved) is ignored, not
    cleared — the picker falls back to its plain default with no console error."""
    context = browser.new_context(base_url=live_server)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try { sessionStorage.setItem('labelito_template', 'does-not-exist'); } catch (e) {}"
    )
    # pageerror (not console "error" messages) is the right signal here: a background /preview
    # or /printer/status call failing with a non-2xx status is expected on this printer-less
    # file:// harness and logs as a console error unrelated to our restore code — pageerror only
    # fires for an uncaught JS exception, which is what a bad `templateMap[saved]` lookup would be.
    page_errors: list[str] = []
    page = context.new_page()
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        page.goto("/")
        expect(page.locator("#template-groups .tpl-card").first).to_be_visible()
        assert _selected_template(page) != "does-not-exist"
        assert page_errors == [], f"unexpected uncaught page errors: {page_errors}"
    finally:
        context.close()


# ── Print tab: live preview refreshes on B/W + quality option changes (round 5) ─────────────────────


def test_bw_option_change_refreshes_the_live_preview(authed_page: Page) -> None:
    """Dither, threshold, and high-res all affect the rendered preview (per the option-group hint),
    but until this fix no listener wired them to doPreview() — toggling them silently left the
    preview stale. Threshold/high-res are checked before dither so the dither-driven disable of
    #threshold (syncThresholdToggledByDither) doesn't block the threshold interaction."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)
    # Let the pick/field-driven preview settle before watching for the option-driven ones below.
    authed_page.wait_for_timeout(700)

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.fill("#threshold", "42.5")
    body = req_info.value.post_data_json
    assert body is not None and body["options"]["threshold"] == 42.5, body

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.check("#high-res")
    body = req_info.value.post_data_json
    assert body is not None and body["options"]["high_res"] is True, body

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.check("#dither")
    body = req_info.value.post_data_json
    assert body is not None and body["options"]["dither"] is True, body


def test_copies_and_dry_run_do_not_refresh_the_preview(authed_page: Page) -> None:
    """Copies and dry-run are not part of buildPayload's preview-relevant fields — checking dry-run
    and stepping copies must not trigger an extra /preview request."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    _fill_all_fields(authed_page)
    # Let the pick-driven preview and EVERY per-field debounced preview settle before counting
    # requests below — each field input has its OWN debounce(doPreview, 600) instance (renderFields),
    # so filling >1 field can fire more than one request; expect_request would only catch the first.
    authed_page.wait_for_timeout(900)

    preview_requests: list[str] = []
    authed_page.on(
        "request",
        lambda r: (
            preview_requests.append(r.url)
            if r.url.endswith("/preview") and r.method == "POST"
            else None
        ),
    )
    authed_page.check("#dry-run")
    authed_page.click("#copies-plus")
    authed_page.wait_for_timeout(900)  # > the 600ms debounce, so a stray request would have fired
    assert preview_requests == [], (
        f"copies/dry-run changes must not refresh the preview, got {preview_requests}"
    )


# ── Print tab: fields + print options persist across tab switches (round 5) ────────────────────────

# A second shipped template (templates/62-simple-text.yaml) in the SAME size group as SAMPLE_TEMPLATE
# — used to exercise the "switch templates" persistence paths without a picker/group-visibility
# side effect.
OTHER_TEMPLATE = "simple-text"


def test_fields_persist_across_tab_switches(authed_page: Page) -> None:
    """Typed field values on the Print tab survive navigating away (Studio) and back — mirrors
    test_template_selection_persists_across_tab_switches, for the FIELDS_KEY snapshot."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    authed_page.fill("#field-title", "Round 5")
    authed_page.fill("#field-subtitle", "Persisted")

    authed_page.click('.nav .tab[href="/editor"]')
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Studio"))
    authed_page.click('.nav .tab[href="/"]')
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Print"))

    assert _selected_template(authed_page) == SAMPLE_TEMPLATE
    expect(authed_page.locator("#field-title")).to_have_value("Round 5")
    expect(authed_page.locator("#field-subtitle")).to_have_value("Persisted")


def test_print_options_persist_across_tab_switches(authed_page: Page) -> None:
    """Threshold, dither, dry-run, and copies all survive a Print -> Studio -> Print round trip, and
    the restored dither state re-disables #threshold (syncThresholdToggledByDither) and relabels the
    print button (updatePrintLabel) exactly as a live toggle would."""
    authed_page.goto("/")
    authed_page.fill("#threshold", "42.5")
    authed_page.check("#dither")
    authed_page.check("#dry-run")
    authed_page.fill("#copies", "3")

    authed_page.click('.nav .tab[href="/editor"]')
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Studio"))
    authed_page.click('.nav .tab[href="/"]')
    expect(authed_page.locator(".nav .tab.active")).to_have_text(re.compile("Print"))

    expect(authed_page.locator("#threshold")).to_have_value("42.5")
    expect(authed_page.locator("#dither")).to_be_checked()
    expect(authed_page.locator("#dry-run")).to_be_checked()
    expect(authed_page.locator("#copies")).to_have_value("3")
    expect(authed_page.locator("#threshold")).to_be_disabled()
    expect(authed_page.locator("#print-label")).to_have_text("Dry run")


def test_first_preview_after_reload_carries_saved_fields_and_options(authed_page: Page) -> None:
    """Mirrors test_first_preview_after_reload_carries_persisted_language: the FIRST /preview payload
    after a reload must already carry the restored fields + options, not empty/server defaults."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    authed_page.fill("#field-title", "Reload test")
    authed_page.fill("#threshold", "33")
    authed_page.check("#dither")

    with authed_page.expect_request(
        lambda r: r.url.endswith("/preview") and r.method == "POST"
    ) as req_info:
        authed_page.reload()
    body = req_info.value.post_data_json
    assert body is not None, "expected a /preview request body"
    assert body["fields"].get("title") == "Reload test", body
    assert body["options"]["dither"] is True, body
    assert body["options"]["threshold"] == 33.0, body


def test_page_load_alone_does_not_persist_fields_or_options(authed_page: Page) -> None:
    """Merely loading the Print tab (no interaction) must not write FIELDS_KEY or OPTIONS_KEY —
    mirrors test_page_load_alone_does_not_persist_a_template_choice."""
    authed_page.goto("/")
    assert authed_page.evaluate("() => sessionStorage.getItem('labelito_fields')") is None
    assert authed_page.evaluate("() => sessionStorage.getItem('labelito_print_options')") is None


def test_saved_fields_for_a_different_template_are_ignored_not_cleared(
    browser: Browser, live_server: str
) -> None:
    """A fields snapshot saved under a template OTHER than the one currently selected is skipped,
    never cleared — restoreSavedFields requires entry.template === selectedTemplateName."""
    context = browser.new_context(base_url=live_server)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try {"
        " sessionStorage.setItem('labelito_template', 'title-subtitle');"
        " sessionStorage.setItem('labelito_fields', JSON.stringify("
        "   {template: 'simple-text', values: {text: 'should not appear'}}));"
        "} catch (e) {}"
    )
    page = context.new_page()
    try:
        page.goto("/")
        assert page.evaluate("() => currentTemplate().name") == "title-subtitle"
        expect(page.locator("#field-title")).to_have_value("")
        stored = page.evaluate("() => sessionStorage.getItem('labelito_fields')")
        assert stored is not None and "should not appear" in stored, (
            "a stale/mismatched fields entry must be ignored, never cleared"
        )
    finally:
        context.close()


def test_corrupt_saved_fields_and_options_are_harmless(browser: Browser, live_server: str) -> None:
    """Corrupt JSON in either persistence key must not throw (restoreSavedFields/restorePrintOptions
    both parse in try/catch) and must not be cleared — mirrors
    test_saved_template_with_unknown_name_falls_back_to_default's pageerror-based regression check."""
    context = browser.new_context(base_url=live_server)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try {"
        " sessionStorage.setItem('labelito_fields', '{not valid json');"
        " sessionStorage.setItem('labelito_print_options', '{not valid json');"
        "} catch (e) {}"
    )
    page_errors: list[str] = []
    page = context.new_page()
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        page.goto("/")
        expect(page.locator("#template-groups .tpl-card").first).to_be_visible()
        assert page_errors == [], f"unexpected uncaught page errors: {page_errors}"
        assert page.evaluate("() => sessionStorage.getItem('labelito_fields')") == "{not valid json"
        assert (
            page.evaluate("() => sessionStorage.getItem('labelito_print_options')")
            == "{not valid json"
        )
    finally:
        context.close()


def test_restored_red_option_is_regated_disabled_on_non_red_template(
    browser: Browser, live_server: str
) -> None:
    """A saved red:true only means "the toggle was checked at save time" — restorePrintOptions
    restores it unconditionally, but syncRedToggleForTemplate() (run right after, from init's
    rebuildTemplatePicker()) re-disables and unchecks it because none of the default template's media
    is red-capable. Restore-then-let-sync-fix must not leave a stale checked-but-disabled toggle."""
    context = browser.new_context(base_url=live_server)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try { sessionStorage.setItem('labelito_print_options', "
        "JSON.stringify({red: true})); } catch (e) {}"
    )
    page = context.new_page()
    try:
        page.goto("/")
        red = page.locator("#red")
        expect(red).to_be_disabled()
        expect(red).not_to_be_checked()
    finally:
        context.close()


def test_restored_high_res_option_is_ignored_on_unsupported_model(
    browser: Browser, live_server_low_res: str
) -> None:
    """A saved high_res:true must not ride onto a model where the server has disabled the toggle —
    the Jinja capability gate is authoritative; restorePrintOptions checks !highRes.disabled before
    applying the saved value."""
    context = browser.new_context(base_url=live_server_low_res)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try { sessionStorage.setItem('labelito_print_options', "
        "JSON.stringify({high_res: true})); } catch (e) {}"
    )
    page = context.new_page()
    try:
        page.goto("/")
        high_res = page.locator("#high-res")
        expect(high_res).to_be_disabled()
        expect(high_res).not_to_be_checked()
    finally:
        context.close()


def test_restored_out_of_range_copies_clamps_to_the_max(browser: Browser, live_server: str) -> None:
    """A tampered/corrupted saved copies value ("99") is clamped by restorePrintOptions's trailing
    clampCopies() call, the same as a live out-of-range typed value would be."""
    context = browser.new_context(base_url=live_server)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    context.add_init_script(
        "try { sessionStorage.setItem('labelito_print_options', "
        "JSON.stringify({copies: '99'})); } catch (e) {}"
    )
    page = context.new_page()
    try:
        page.goto("/")
        expect(page.locator("#copies")).to_have_value("10")
    finally:
        context.close()


def test_picking_a_template_resets_the_fields_snapshot(authed_page: Page) -> None:
    """onTemplatePicked saves fields AFTER renderFields(), so picking a new template immediately
    snapshots its fresh empty form under the NEW template's name — not the old template's typed
    values under the new name."""
    authed_page.goto("/")
    _select_template(authed_page, SAMPLE_TEMPLATE)
    authed_page.fill("#field-title", "will be left behind")

    _select_template(authed_page, OTHER_TEMPLATE)

    stored = authed_page.evaluate("() => JSON.parse(sessionStorage.getItem('labelito_fields'))")
    assert stored == {"template": OTHER_TEMPLATE, "values": {}}, stored


# ── Nav: visible "Label language" caption (round 5) ─────────────────────────────────────────────────


def test_language_selector_has_visible_label_caption(authed_page: Page) -> None:
    """The language picker's label is now a visible caption, not screen-reader-only text — fails
    against the old `.visually-hidden` label, a real regression test. Shared nav, so all three
    pages (Print/History/Studio) get it."""
    for path in ("/", "/history", "/editor"):
        authed_page.goto(path)
        label = authed_page.locator('label[for="language-select"]')
        expect(label).to_be_visible()
        expect(label).to_have_text("Label language")


# ── Example vs user templates: muted cards + per-card edit pencil deep-link (round 6) ───────────────


def test_edit_pencil_on_every_card_deep_links_to_studio(authed_page_examples: Page) -> None:
    """Every template card — bundled example AND the user's own — carries a pencil `.tpl-edit`
    deep-link to the studio preloaded with that template, with an accessible name. The example card
    is still visually flagged (`.tpl-card-example`); the user's own is not. The legend explaining the
    dashed marker is shown."""
    authed_page_examples.goto("/")

    example_card = authed_page_examples.locator('.tpl-card[data-name="shipped-example"]')
    user_card = authed_page_examples.locator('.tpl-card[data-name="my-own"]')
    expect(example_card).to_have_count(1)
    expect(user_card).to_have_count(1)

    # The example is flagged with the dashed marker; the user's own is not.
    expect(example_card).to_have_class(re.compile(r"\btpl-card-example\b"))
    expect(user_card).not_to_have_class(re.compile(r"\btpl-card-example\b"))

    # Both cards carry a pencil edit link to /editor?load=<name> with an accessible name.
    for card, name in ((example_card, "shipped-example"), (user_card, "my-own")):
        edit = card.locator("a.tpl-edit")
        expect(edit).to_have_count(1)
        href = edit.get_attribute("href")
        assert href is not None and href.endswith(f"/editor?load={name}"), href
        assert edit.get_attribute("aria-label") == f"Edit {name} in Studio"

    # The legend is shown and explains the dashed marker.
    legend = authed_page_examples.locator("#tpl-legend")
    expect(legend).to_be_visible()
    expect(legend).to_contain_text("Dashed = bundled example")


def test_edit_pencil_deep_link_preloads_editor(authed_page_examples: Page) -> None:
    """Opening /editor?load=<name> preloads that template's YAML into the studio textarea — the
    landing target of the Print page's per-card edit pencil."""
    authed_page_examples.goto("/editor?load=shipped-example")
    yaml_box = authed_page_examples.locator("#yaml")
    expect(yaml_box).to_have_value(re.compile(r"name:\s*shipped-example"))


def test_edit_pencil_hidden_when_templates_not_loadable(authed_page_examples_no_load: Page) -> None:
    """With TEMPLATES_LOADABLE=false the /templates/{name}/source route 404s, so the editor cannot
    preload a template — the print page must therefore hide the per-card `.tpl-edit` pencil on every
    card and drop the legend's "use the pencil" hint. The dashed-example legend itself still renders
    (bundled examples are still present); only the pencil affordance is gated off."""
    authed_page_examples_no_load.goto("/")

    # The cards still render (this is only the loadability gate, not a template-listing failure), so
    # wait for the example card before asserting the pencil's absence — the negative check must not
    # race an unrendered grid.
    example_card = authed_page_examples_no_load.locator('.tpl-card[data-name="shipped-example"]')
    expect(example_card).to_have_count(1)

    # No card — example or user's own — carries the edit pencil.
    expect(authed_page_examples_no_load.locator("a.tpl-edit")).to_have_count(0)

    # The legend still explains the dashed marker but omits the pencil hint.
    legend = authed_page_examples_no_load.locator("#tpl-legend")
    expect(legend).to_be_visible()
    expect(legend).to_contain_text("Dashed = bundled example")
    expect(legend).not_to_contain_text("pencil")
