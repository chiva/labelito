# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixtures for the end-to-end harness: a live server plus authenticated browser/API clients.

Every test module here is marked ``e2e`` and is therefore deselected by the default ``pytest`` run
(see ``addopts`` in pyproject.toml). Run them explicitly with ``uv run pytest -m e2e`` after a
one-time ``uv run playwright install chromium``.

This conftest must not import ``playwright`` at module scope: the modules are still *collected*
during a normal ``pytest`` run (then deselected), so a top-level import would error for anyone who
has not installed the optional ``e2e`` group. The test modules guard themselves with
``pytest.importorskip``; the fixtures below import lazily.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from harness import DEFAULT_API_TOKEN, LiveServer, web_token_init_script

if TYPE_CHECKING:
    import httpx2
    from playwright.sync_api import Browser, Page


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    """One uvicorn process for the whole e2e session; yields its base URL."""
    with LiveServer() as server:
        yield server.base_url


@pytest.fixture(scope="session")
def live_server_snmp() -> Iterator[str]:
    """A server configured as a network printer with SNMP enabled, so the print page renders with
    background status polling ON (``live_status_poll`` true). The unroutable TEST-NET URI is never
    actually dialled — tests that use this fixture intercept ``/printer/status`` in the browser, and
    do not submit a real ``/print`` — it exists only so the server reports the lock-free SNMP path."""
    with LiveServer(
        env_overrides={"PRINTER_URI": "tcp://192.0.2.10:9100", "SNMP_ENABLED": "true"}
    ) as server:
        yield server.base_url


@pytest.fixture
def authed_page_snmp(browser: Browser, live_server_snmp: str) -> Iterator[Page]:
    """Authenticated page against the SNMP-enabled server (live status polling ON)."""
    context = browser.new_context(base_url=live_server_snmp)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture(scope="session")
def live_server_usb() -> Iterator[str]:
    """A server configured as a USB printer (SNMP off), so the pages render with status_supported ON
    but live_status_poll OFF: a one-shot ESC i S roll read at load, and NO background poll (the USB
    device handle must not be claimed on a timer). The device is never opened — tests intercept
    ``/printer/status`` in the browser and do not submit a real ``/print``."""
    with LiveServer(
        env_overrides={"PRINTER_URI": "usb://0x04f9:0x209c", "SNMP_ENABLED": "false"}
    ) as server:
        yield server.base_url


@pytest.fixture
def authed_page_usb(browser: Browser, live_server_usb: str) -> Iterator[Page]:
    """Authenticated page against the USB server (one-shot status read at load, no background poll)."""
    context = browser.new_context(base_url=live_server_usb)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def api_client(live_server: str) -> Iterator[httpx2.Client]:
    """HTTP client pre-authenticated with the harness's default bearer token."""
    import httpx2

    with httpx2.Client(
        base_url=live_server,
        headers={"Authorization": f"Bearer {DEFAULT_API_TOKEN}"},
        timeout=10.0,
    ) as client:
        yield client


@pytest.fixture
def authed_page(browser: Browser, live_server: str) -> Iterator[Page]:
    """A browser page with the default API token pre-seeded into the UI's localStorage."""
    context = browser.new_context(base_url=live_server)
    context.add_init_script(web_token_init_script(DEFAULT_API_TOKEN))
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def anon_page(browser: Browser, live_server: str) -> Iterator[Page]:
    """A page with NO token — used to assert the UI's auth-required path."""
    context = browser.new_context(base_url=live_server)
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()
