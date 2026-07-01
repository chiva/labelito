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
          applyMediaCompat();
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
          applyMediaCompat();
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
