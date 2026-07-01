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


def test_media_compatibility_badges_and_guards(authed_page: Page) -> None:
    """The print page badges each template against the loaded roll and blocks a mismatch (Step 7).

    Drives the client-side compatibility consumer deterministically: seed a continuous + a die-cut
    template, set the loaded roll over a network (tcp://) printer, and assert the dropdown disables
    the mismatching template, the Print button is disabled, and the badge reflects ✓/✗ — the same
    width/form rule the server-side 409 guard applies. Preview is never blocked."""
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
    assert (
        authed_page.eval_on_selector("#template-select option[value='__dc']", "o => o.disabled")
        is True
    ), "the die-cut template must be disabled against a continuous roll"
    assert authed_page.eval_on_selector(".btn-print", "b => b.disabled") is True, (
        "Print must be blocked for the mismatching selected template"
    )
    assert authed_page.eval_on_selector(".btn-preview", "b => b.disabled") in (False, None), (
        "Preview must never be blocked by a media mismatch"
    )
    badge = authed_page.locator("#media-badge")
    expect(badge).to_have_class(re.compile(r"media-bad"))
    expect(badge).to_contain_text("✗")
    assert (
        authed_page.eval_on_selector("#template-select option[value='__cont']", "o => o.disabled")
        is False
    ), "the matching continuous template must stay enabled"

    # Swap the loaded roll to die-cut 62x29: now the die-cut template matches → Print re-enabled, ✓.
    authed_page.evaluate(
        """() => {
          printerStatus = {state:'idle', uri:'tcp://192.168.5.14:9100', reachable:true,
            media_width_mm:62, media_type:'die_cut', media_length_mm:29};
          applyMediaCompat();
        }"""
    )
    assert authed_page.eval_on_selector(".btn-print", "b => b.disabled") is False
    expect(authed_page.locator("#media-badge")).to_have_class(re.compile(r"media-ok"))
    expect(authed_page.locator("#media-badge")).to_contain_text("✓")


def test_unauthenticated_preview_shows_auth_error(anon_page: Page) -> None:
    """With no token seeded, the server rejects /preview and the UI surfaces the auth prompt."""
    anon_page.goto("/")
    anon_page.select_option("#template-select", SAMPLE_TEMPLATE)
    _fill_all_fields(anon_page)
    anon_page.click("button.btn-preview")

    status = anon_page.locator(".status.err")
    expect(status).to_be_visible()
    expect(status).to_contain_text("Authentication required")
