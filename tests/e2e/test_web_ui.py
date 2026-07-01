# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end web UI checks — a real browser driving the page at app/web/index.html.

These exercise the same flows a human (or an AI agent) would: load the page, pick a template, fill
its fields, preview, print (dry-run), and confirm the auth-required path. The token is pre-seeded
into localStorage by the ``authed_page`` fixture, mirroring how the dev harness opens the page.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

# A shipped template with plain text fields (templates/title-subtitle.yaml) — stable to drive the UI
# without needing an image upload or QR/barcode payload.
SAMPLE_TEMPLATE = "title-subtitle"


def _fill_all_fields(page: Page, value: str = "E2E test") -> None:
    """Fill every field input the current template renders into #fields-container."""
    inputs = page.locator("#fields-container input")
    expect(inputs.first).to_be_visible()
    for i in range(inputs.count()):
        inputs.nth(i).fill(value)


def test_page_loads_and_lists_templates(authed_page: Page) -> None:
    authed_page.goto("/")
    expect(authed_page).to_have_title("Labelito")
    options = authed_page.locator("#template-select option")
    assert options.count() > 0, "template picker should be populated from the shipped templates"


def test_select_template_renders_fields_and_previews(authed_page: Page) -> None:
    authed_page.goto("/")
    authed_page.select_option("#template-select", SAMPLE_TEMPLATE)
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


def test_print_dry_run_round_trip(authed_page: Page) -> None:
    """Print (dry-run) from the UI and assert the /print round-trip succeeds.

    Two things are checked: the network response from /print, and the on-page success banner.
    The banner is now sticky/persistent — doPrint() renders it with ``{sticky: true}``, and the
    post-print doPreview() refresh deliberately does NOT clear a sticky banner — so ".status.ok"
    stays visible (until the x button or its ~8s auto-dismiss fires) rather than racing away.
    """
    authed_page.goto("/")
    authed_page.select_option("#template-select", SAMPLE_TEMPLATE)
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
    Regression guard for the Codex finding that unconditional polling reintroduces lock contention."""
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
    the in-flight guard, and the badge resolves to Unreachable instead of freezing forever. Regression
    for the Codex finding that a hung request could pin statusInFlight and stall the whole poll loop."""
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
    into <optgroup>s by size denomination and shows every group — there's nothing to filter against, so
    no group is focused and the size-filter control stays hidden."""
    authed_page.goto("/")
    groups = authed_page.locator("#template-select optgroup")
    # The shipped catalog spans several sizes (12/29/62mm continuous + 17x54/29x90/62x29 die-cut).
    expect(groups).not_to_have_count(0)
    labels = authed_page.eval_on_selector_all(
        "#template-select optgroup", "els => els.map(e => e.label)"
    )
    assert any("62mm continuous" in label for label in labels), labels
    assert len(labels) >= 4, f"expected templates grouped across several sizes, got {labels}"
    # Unknown roll → nothing focused, so no ✓ marker and the show-all/focus control is hidden.
    assert not any(label.startswith("✓") for label in labels), labels
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

    groups = authed_page_snmp.locator("#template-select optgroup")
    # Focus mode: only the matching 62mm continuous group remains, marked with a ✓.
    expect(groups).to_have_count(1, timeout=8000)
    assert groups.first.get_attribute("label") == "✓ 62mm continuous"
    # The hidden-count hint and the reveal toggle are shown.
    expect(authed_page_snmp.locator("#size-filter")).to_be_visible()
    expect(authed_page_snmp.locator("#size-filter-hint")).to_contain_text("hidden")
    toggle = authed_page_snmp.locator("#size-filter-toggle")
    expect(toggle).to_have_text("Show all sizes")

    toggle.click()
    # Show-all: every size group reappears (the 62mm one stays ✓-marked and first).
    labels = authed_page_snmp.eval_on_selector_all(
        "#template-select optgroup", "els => els.map(e => e.label)"
    )
    assert len(labels) >= 4, f"show-all should reveal every size group, got {labels}"
    assert any(label.startswith("✓") for label in labels), labels


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

    groups = authed_page_snmp.locator("#template-select optgroup")
    expect(groups).to_have_count(1, timeout=8000)
    expect(groups.first).to_have_attribute("label", "✓ 62mm continuous")

    # No reload, no ↻ — a later background poll sees the new 29mm roll and re-focuses on its own.
    expect(groups.first).to_have_attribute("label", "✓ 29mm continuous", timeout=15000)
    assert authed_page_snmp.locator("#template-select").input_value() == "simple-text-29", (
        "the roll swap should land the selection on a 29mm template"
    )


def test_late_status_does_not_discard_typed_input(authed_page_snmp: Page) -> None:
    """A slow /printer/status reply must not silently change the template or wipe entered values. The
    roll-driven refocus only lands a fresh page on a usable template — once the user has typed, a late
    status arrival (the roll becoming known) must keep their selection and their input. Regression for
    the Codex finding that auto-refocus could discard in-progress form data."""
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

    template_before = authed_page_snmp.locator("#template-select").input_value()
    field = authed_page_snmp.locator("#fields-container input").first
    expect(field).to_be_visible()
    field.fill("DONOTLOSE")  # fires 'input' → marks the form touched

    # The roll becomes known late; force the refresh the background poll would do.
    phase["reachable"] = True
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")

    # Late status must NOT change the template or discard the typed value.
    assert authed_page_snmp.locator("#template-select").input_value() == template_before, (
        "a late status reply must not change the selected template after the user has typed"
    )
    expect(field).to_have_value("DONOTLOSE")


def test_roll_swap_after_print_still_refocuses(authed_page_snmp: Page) -> None:
    """The dirty-input guard is scoped, not a permanent latch: after a user fills and prints a label,
    swapping the roll must still re-focus the picker to a template for the new size (the input was
    consumed by the print). Regression for the Codex finding that a permanent touch-latch disabled
    refocus forever after the first interaction — breaking the core roll-swap workflow."""
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
    sel = authed_page_snmp.locator("#template-select")
    expect(authed_page_snmp.locator("#template-select optgroup").first).to_have_attribute(
        "label", "✓ 29mm continuous", timeout=8000
    )
    expect(sel).to_have_value("simple-text-29")

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
    expect(authed_page_snmp.locator("#template-select optgroup").first).to_have_attribute(
        "label", "✓ 62mm continuous"
    )
    assert sel.input_value() != "simple-text-29", (
        "a roll swap after a print should refocus off the 29mm template, not stay latched"
    )


def test_late_status_does_not_override_manual_template_choice(authed_page_snmp: Page) -> None:
    """A manual template pick is an explicit choice that a late status reply must not override — even
    with no typing. Pick a 62mm template while the roll is unknown, then have a 29mm roll arrive late;
    the selection must stand (otherwise Print would silently submit a different template). Regression
    for the Codex finding that selection-only interaction wasn't covered by the refocus guard."""
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

    sel = authed_page_snmp.locator("#template-select")
    # Explicitly pick a 62mm template while the roll is still unknown — no typing.
    sel.select_option("title-subtitle")
    expect(sel).to_have_value("title-subtitle")

    # A 29mm roll becomes known late; the explicit pick must NOT be auto-replaced.
    phase["reachable"] = True
    authed_page_snmp.evaluate("async () => { await refreshPrinterStatus(); }")
    expect(sel).to_have_value("title-subtitle")


def test_edit_during_in_flight_print_is_not_wiped_by_stale_completion(
    authed_page_snmp: Page,
) -> None:
    """Stale-completion race: a /print reply that lands AFTER the user has started editing the next
    label must not clear the refocus guard and let a later status refresh wipe the newer input. Hold
    /print in flight, edit during the delay, release it, then a roll change must NOT refocus. Regression
    for the Codex finding that doPrint cleared userOverride unconditionally."""
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
    sel = authed_page_snmp.locator("#template-select")
    expect(authed_page_snmp.locator("#template-select optgroup").first).to_have_attribute(
        "label", "✓ 62mm continuous", timeout=8000
    )
    template_before = sel.input_value()

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
    expect(sel).to_have_value(template_before)


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
    labels = authed_page_snmp.eval_on_selector_all(
        "#template-select optgroup", "els => els.map(e => e.label)"
    )
    assert len(labels) >= 4, f"empty-match fallback must show every size group, got {labels}"
    # Nothing matched, so no group is ✓-focused.
    assert not any(label.startswith("✓") for label in labels), labels


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
    # Seed two known templates with explicit required media and options for them.
    authed_page.evaluate(
        """() => {
          templateMap['__cont'] = {name:'__cont', description:'cont', required:[], optional:[],
            media:{width_mm:62.0, media_type:'continuous', length_mm:null}};
          templateMap['__dc'] = {name:'__dc', description:'die cut', required:[], optional:[],
            media:{width_mm:62.0, media_type:'die_cut', length_mm:29.0}};
          TEMPLATES.push(templateMap['__cont'], templateMap['__dc']);
          const sel = document.getElementById('template-select');
          for (const v of ['__cont','__dc']) {
            const o = document.createElement('option'); o.value = v; o.textContent = v;
            sel.appendChild(o);
          }
        }"""
    )

    # Loaded roll = 62mm continuous on a network printer; select the (mismatching) die-cut template.
    authed_page.evaluate(
        """() => {
          printerStatus = {state:'idle', uri:'tcp://192.168.5.14:9100', reachable:true,
            media_width_mm:62, media_type:'continuous', media_length_mm:null};
          document.getElementById('template-select').value = '__dc';
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
    assert (
        authed_page.eval_on_selector("#template-select option[value='__dc']", "o => o.disabled")
        is False
    ), "the mismatching template must stay selectable"

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

    Regression for the Codex finding that rows only appeared after /printer/status resolved: a stuck
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

    Regression for the Codex finding: ``label : "62"`` (whitespace before the colon) is valid YAML but
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
    fetch (the fix for the Codex first-run finding) — asserted by waiting for a /printer/status
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
    with anon_page.expect_response(lambda r: r.url.endswith("/printer/status")) as resp_info:
        anon_page.fill("#api-token", "a-token")  # 'input' → debounced loadLabelReference()
    assert resp_info.value.url.endswith("/printer/status"), (
        "entering the token must refetch /printer/status so the panel can recover from a 401"
    )


def test_editor_red_label_is_geometry_only_match(authed_page: Page) -> None:
    """A red/black label is shown as a geometry-only match, not a definite one (Step 8 / Codex iter 3).

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

    Regression for the Codex finding: PyYAML keeps the LAST of duplicate mapping keys, so leaving any
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
    """ "Use" edits the label at the document's root indentation, not always column 0 (Codex iter 5).

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
    """ "Use" edits in place under a `---` document marker + indented root mapping (Codex iter 6).

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


def test_unauthenticated_preview_shows_auth_error(anon_page: Page) -> None:
    """With no token seeded, the server rejects /preview and the UI surfaces the auth prompt."""
    anon_page.goto("/")
    anon_page.select_option("#template-select", SAMPLE_TEMPLATE)
    _fill_all_fields(anon_page)
    anon_page.click("button.btn-preview")

    status = anon_page.locator(".status.err")
    expect(status).to_be_visible()
    expect(status).to_contain_text("Authentication required")


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

    match_row = authed_page_snmp.locator("#history-body tr").filter(has_text="text-62")
    mismatch_row = authed_page_snmp.locator("#history-body tr").filter(has_text="addr-17x54")

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
    rows = authed_page_snmp.locator("#history-body tr")
    expect(rows).to_have_count(2)
    expect(authed_page_snmp.locator("#history-body button.btn-reprint:disabled")).to_have_count(0)
    expect(authed_page_snmp.locator("#history-body .tag-mismatch")).to_have_count(0)
    expect(authed_page_snmp.locator("#history-body tr.row-incompatible")).to_have_count(0)
    expect(authed_page_snmp.locator("#roll-note")).to_be_hidden()


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

    row_62 = authed_page_snmp.locator("#history-body tr").filter(has_text="text-62")
    row_17 = authed_page_snmp.locator("#history-body tr").filter(has_text="addr-17x54")

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

    dry_row = authed_page_snmp.locator("#history-body tr").filter(has_text="dry-run")
    printed_row = authed_page_snmp.locator("#history-body tr").filter(has_text="printed")

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
    expect(authed_page_snmp.locator("#history-body tr")).to_have_count(2, timeout=4000)
    # Status never resolved → roll unknown → nothing gated (fail-open default).
    expect(authed_page_snmp.locator("#history-body button.btn-reprint:disabled")).to_have_count(0)


def test_history_does_not_probe_status_on_non_snmp(authed_page: Page) -> None:
    """On a non-SNMP deployment (the ``authed_page`` fixture, live_status_poll OFF) the History page
    must NEVER fetch /printer/status: there the read serializes through the server's print lock, so a
    probe could delay a concurrent /reprint — and the media type reads as unknown anyway, so the gate
    could never fire. Assert zero status hits, that rows render, and that nothing is gated (fail-open).
    Regression for the Codex finding that the unconditional init probe reintroduced lock contention."""
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
    expect(authed_page.locator("#history-body tr")).to_have_count(2)
    # Give any errant init probe / scheduled poll ample time to fire (poll base interval is ~4s).
    authed_page.wait_for_timeout(6000)
    assert status_hits["n"] == 0, (
        f"non-SNMP History must not probe /printer/status; got {status_hits['n']} hits"
    )
    # Even though a 62mm roll would mismatch the 17x54 row, nothing is gated (no status → no roll).
    expect(authed_page.locator("#history-body button.btn-reprint:disabled")).to_have_count(0)
    expect(authed_page.locator("#history-body tr.row-incompatible")).to_have_count(0)
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

    # A known roll → focus mode: only the matching 62mm continuous group, marked ✓, plus the reveal UI.
    groups = authed_page_usb.locator("#template-select optgroup")
    expect(groups).to_have_count(1, timeout=8000)
    assert groups.first.get_attribute("label") == "✓ 62mm continuous"
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
    mismatch_row = authed_page_usb.locator("#history-body tr").filter(has_text="addr-17x54")
    expect(mismatch_row.locator(".tag-mismatch")).to_contain_text("needs 17mm")
    expect(mismatch_row.locator("button.btn-reprint")).to_be_enabled()
    expect(mismatch_row).not_to_have_class(re.compile("row-incompatible"))
    # The matching 62mm row is reprintable and unflagged.
    match_row = authed_page_usb.locator("#history-body tr").filter(has_text="text-62")
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
    token from localStorage. Regression for the Codex finding on history.html showStatus."""
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
