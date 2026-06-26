# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end API checks against a real running server (not the in-process TestClient).

These complement tests/test_api.py: that suite verifies handler logic with a mocked driver in
process; this one confirms the fully assembled, network-reachable service — auth enforcement,
template loading from disk, and the render → file-sink print path — behaves over real HTTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytestmark = pytest.mark.e2e

if TYPE_CHECKING:
    import httpx2

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _sample_template(api_client: httpx2.Client) -> tuple[str, dict[str, str]]:
    """Pick a shipped template and build a fields dict satisfying its required fields.

    Driven off the live /templates contract so the test stays correct as templates evolve. Prefers
    title-subtitle (plain text) but falls back to the first template with only text-like required
    fields rather than hard-coding a name.
    """
    templates = api_client.get("/templates").json()
    assert templates, "server should expose at least one template"
    chosen = next((t for t in templates if t["name"] == "title-subtitle"), templates[0])
    fields = dict.fromkeys(chosen["fields"]["required"], "E2E")
    return chosen["name"], fields


def test_health_ok(api_client: httpx2.Client) -> None:
    resp = api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["template_count"] > 0


def test_templates_listed(api_client: httpx2.Client) -> None:
    resp = api_client.get("/templates")
    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()]
    assert "title-subtitle" in names


def test_preview_requires_auth(api_client: httpx2.Client) -> None:
    """A request without the bearer token is rejected — auth is enforced end-to-end."""
    name, fields = _sample_template(api_client)
    resp = api_client.post(
        "/preview", json={"template": name, "fields": fields}, headers={"Authorization": ""}
    )
    assert resp.status_code == 401


def test_preview_returns_png(api_client: httpx2.Client) -> None:
    name, fields = _sample_template(api_client)
    resp = api_client.post("/preview", json={"template": name, "fields": fields})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(PNG_MAGIC)


def test_print_dry_run(api_client: httpx2.Client) -> None:
    name, fields = _sample_template(api_client)
    resp = api_client.post("/print", json={"template": name, "fields": fields, "dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["template"] == name
    assert body["job_id"]


def test_missing_template_is_422(api_client: httpx2.Client) -> None:
    """The post-discovery contract holds over real HTTP: template is required."""
    resp = api_client.post("/preview", json={"fields": {"title": "x"}})
    assert resp.status_code == 422


def test_unknown_template_is_404(api_client: httpx2.Client) -> None:
    resp = api_client.post("/preview", json={"template": "no-such-template", "fields": {}})
    assert resp.status_code == 404
