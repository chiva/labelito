# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the OpenAPI/Swagger polish (security scheme, tags, documented error responses)."""

from __future__ import annotations

from fastapi.testclient import TestClient


# ── OpenAPI / Swagger polish ─────────────────────────────────────────────────────
def test_openapi_has_bearer_security_scheme(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert "HTTPBearer" in schemes
    assert schemes["HTTPBearer"]["scheme"] == "bearer"


def test_protected_route_carries_security_requirement(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    print_op = spec["paths"]["/print"]["post"]
    assert any("HTTPBearer" in req for req in print_op.get("security", []))


def test_operations_are_tagged(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["paths"]["/print"]["post"]["tags"] == ["Printing"]
    assert spec["paths"]["/health"]["get"]["tags"] == ["System"]
    assert spec["paths"]["/templates"]["get"]["tags"] == ["Templates"]
    assert spec["paths"]["/history/list"]["get"]["tags"] == ["History"]


def test_reprint_documents_404_and_409(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/reprint/{job_id}"]["post"]["responses"]
    assert "404" in responses
    assert "409" in responses
    assert "401" in responses
