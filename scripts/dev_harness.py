#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual dev harness: run labelito locally and open a browser to it, ready to use.

Starts a real server (token auth on, printer-less ``file://`` sink, in-memory history) and opens a
browser to the web UI with the default API token already filled in, so previewing/printing works
without any setup. Reuses the same launcher the e2e tests drive (tests/e2e/harness.py), so "what
the dev sees" and "what the tests check" can never drift.

Usage (from the repo root):

    uv run python scripts/dev_harness.py             # open a headed browser to the page
    uv run python scripts/dev_harness.py --headless  # same, but a headless browser window
    uv run python scripts/dev_harness.py --no-browser  # just run the server; open it yourself
    uv run python scripts/dev_harness.py --check     # one-shot headless smoke (CI/AI agents)
    uv run python scripts/dev_harness.py --port 8765 # pin the port (default: a free one)

The browser modes need a one-time `uv run playwright install chromium`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Reuse the test harness's launcher. It lives under tests/e2e so the e2e suite and this manual tool
# share one source of truth for the default token, port handling, and server lifecycle.
_HARNESS_DIR = Path(__file__).resolve().parents[1] / "tests" / "e2e"
sys.path.insert(0, str(_HARNESS_DIR))

from harness import (  # noqa: E402  (path must be set up first)
    DEFAULT_API_TOKEN,
    LiveServer,
    web_token_init_script,
)

SAMPLE_TEMPLATE = "title-subtitle"  # a plain-text shipped template, used by --check


def _open_browser(base_url: str, token: str, *, headless: bool) -> None:
    """Open a browser to the UI with the token pre-seeded, and keep it open for manual use."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is not installed. Install the e2e group and the browser:\n"
            "  uv sync --group e2e && uv run playwright install chromium\n"
            "Or run with --no-browser and open the URL yourself."
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        # no_viewport=True lets the page track the real window size. Playwright's default emulates a
        # fixed 1280x720 viewport decoupled from the window, so dragging the headed window to resize
        # would not reflow the responsive layout. (The e2e fixtures deliberately keep a fixed
        # viewport for reproducibility; this manual harness wants live resize instead.)
        context = browser.new_context(base_url=base_url, no_viewport=True)
        context.add_init_script(web_token_init_script(token))
        page = context.new_page()
        page.goto("/")
        print(f"\n  Browser open at {base_url}  (API token pre-filled: {token})")
        print("  Interact in the browser window; press Enter here to stop.\n")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            browser.close()


def _check(base_url: str, token: str) -> int:
    """Headless one-shot: load the page, preview a label, assert it renders. Returns an exit code."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed; run: uv run playwright install chromium", file=sys.stderr
        )
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(base_url=base_url)
        context.add_init_script(web_token_init_script(token))
        page = context.new_page()
        try:
            page.goto("/")
            assert page.title() == "labelito", f"unexpected title: {page.title()!r}"
            page.click(f'.tpl-card[data-name="{SAMPLE_TEMPLATE}"]')
            inputs = page.locator("#fields-container input")
            inputs.first.wait_for(state="visible")
            for i in range(inputs.count()):
                inputs.nth(i).fill("smoke check")
            page.click("button.btn-preview")
            page.wait_for_function(
                "() => { const i = document.getElementById('preview-img');"
                " return i && i.naturalWidth > 0; }"
            )
            assert page.locator(".status.err").count() == 0, "an error banner was shown"
        finally:
            browser.close()
    print("OK — page loaded, template selected, preview rendered.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--port", type=int, default=None, help="port to bind (default: a free port)"
    )
    parser.add_argument(
        "--token", default=DEFAULT_API_TOKEN, help="API token (default: harness token)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="open a headless browser window")
    mode.add_argument(
        "--no-browser", action="store_true", help="run the server only; do not open a browser"
    )
    mode.add_argument(
        "--check", action="store_true", help="one-shot headless smoke check, then exit"
    )
    args = parser.parse_args(argv)

    with LiveServer(token=args.token, port=args.port) as server:
        print(f"labelito running at {server.base_url}  (token: {args.token})")

        if args.check:
            return _check(server.base_url, args.token)

        if args.no_browser:
            print("Open the URL above and paste the token into the API-token field.")
            print("Press Enter here to stop.")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
            return 0

        _open_browser(server.base_url, args.token, headless=args.headless)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
