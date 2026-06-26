# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware tests — deselected by default. Requires a live Brother QL printer.

Run with:
    PRINTER_URI=tcp://192.168.1.100:9100 pytest -m hardware
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.hardware
def test_health_reaches_printer() -> None:
    """Confirm the service reaches the configured printer."""
    import httpx2

    base = os.environ.get("SERVICE_URL", "http://localhost:8765")
    resp = httpx2.get(f"{base}/health", timeout=5)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.hardware
def test_print_single_copy() -> None:
    """Send a real 1-copy print job to the live printer."""
    import httpx2

    base = os.environ.get("SERVICE_URL", "http://localhost:8765")
    resp = httpx2.post(
        f"{base}/print",
        json={
            "template": "simple-text",
            "fields": {"text": "HARDWARE TEST"},
            "copies": 1,
            "dry_run": False,
        },
        timeout=15,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is False
    assert "job_id" in data


@pytest.mark.hardware
def test_print_freezer_icon() -> None:
    """Print the freezer-icon template (includes snowflake asset)."""
    import httpx2

    base = os.environ.get("SERVICE_URL", "http://localhost:8765")
    resp = httpx2.post(
        f"{base}/print",
        json={
            "template": "freezer-icon",
            "fields": {"title": "Salsa boloñesa", "subtitle": "Casera"},
            "copies": 1,
            "dry_run": False,
        },
        timeout=15,
    )
    assert resp.status_code == 200
