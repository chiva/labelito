# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the MCP server (app.mcp) and its mount/auth wiring in app.main.

The MCP tools reuse the exact same handlers as the REST API, so these tests focus on what is new:
the read/write tool gating, the auth guard on the mounted endpoint, and that each tool round-trips
through the reused handler. Tool logic is driven by calling the registered tool callables directly
(``Tool.fn``) against the ``client`` fixture's monkeypatched singletons (temp templates dir, file://
sink printer, in-memory history), which keeps every test hermetic — no socket, no real printer.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

import app.main as main
from app.mcp import build_mcp_asgi_app, build_mcp_server

READ_TOOLS = {
    "list_templates",
    "get_template",
    "get_capabilities",
    "get_printer_status",
    "preview_label",
    "preview_ephemeral_label",
    "list_history",
    "get_history_label",
}
WRITE_TOOLS = {"print_label", "print_ephemeral_label", "reprint_history_label"}


def _tools(server: FastMCP) -> dict[str, Callable[..., Any]]:
    """Map tool name -> its underlying callable for direct invocation in tests."""
    return {t.name: t.fn for t in server._tool_manager.list_tools()}


def _build_server(monkeypatch: pytest.MonkeyPatch, *, writable: bool) -> FastMCP:
    monkeypatch.setattr(main.settings, "mcp_enabled", True)
    monkeypatch.setattr(main.settings, "mcp_writable", writable)
    return build_mcp_server()


# ── Mount / gating ───────────────────────────────────────────────────────────────


def test_disabled_by_default_has_no_mount() -> None:
    """With MCP_ENABLED false at import (the default), nothing is mounted and no server is built."""
    assert main._mcp_server is None
    assert not any(getattr(r, "path", None) == "/mcp" for r in main.app.routes)


def test_readonly_registers_only_read_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = set(_tools(_build_server(monkeypatch, writable=False)))
    assert tools == READ_TOOLS
    assert not (tools & WRITE_TOOLS)


def test_writable_registers_read_and_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = set(_tools(_build_server(monkeypatch, writable=True)))
    assert tools == READ_TOOLS | WRITE_TOOLS


# ── Auth guard (_mcp_authorized) ─────────────────────────────────────────────────


def _basic_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_authorized_noop_when_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_token", None)
    monkeypatch.setattr(main.settings, "web_auth_user", None)
    monkeypatch.setattr(main.settings, "web_auth_password", None)
    assert main._mcp_authorized(None) is True
    assert main._mcp_authorized("Bearer anything") is True


def test_authorized_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_token", "secret")
    monkeypatch.setattr(main.settings, "web_auth_user", None)
    monkeypatch.setattr(main.settings, "web_auth_password", None)
    assert main._mcp_authorized("Bearer secret") is True
    assert main._mcp_authorized("Bearer wrong") is False
    assert main._mcp_authorized(None) is False
    assert main._mcp_authorized("Basic whatever") is False


def test_authorized_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.settings, "api_token", None)
    monkeypatch.setattr(main.settings, "web_auth_user", "alice")
    monkeypatch.setattr(main.settings, "web_auth_password", "pw")
    assert main._mcp_authorized(_basic_header("alice", "pw")) is True
    assert main._mcp_authorized(_basic_header("alice", "bad")) is False
    assert main._mcp_authorized("Basic not-base64!!") is False
    assert main._mcp_authorized("Basic " + base64.b64encode(b"nocolon").decode()) is False


# ── Read tools (via the client fixture's env) ────────────────────────────────────


def test_list_templates(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    names = {t["name"] for t in tools["list_templates"]()}
    assert "simple" in names
    simple = next(t for t in tools["list_templates"]() if t["name"] == "simple")
    assert simple["fields"]["required"] == ["title"]


@pytest.mark.asyncio
async def test_get_template(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The client fixture enables the editor, so source is exposed by default.
    tools = _tools(_build_server(monkeypatch, writable=False))
    result = await tools["get_template"]("simple")
    assert result["name"] == "simple"
    assert result["required_fields"] == ["title"]
    assert "layout" in result["yaml"]


@pytest.mark.asyncio
async def test_get_template_unknown_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    with pytest.raises(ToolError):
        await tools["get_template"]("does-not-exist")


def test_get_capabilities(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    caps = tools["get_capabilities"]()
    assert "supported_labels" in caps
    assert caps["dpi"] > 0


@pytest.mark.asyncio
async def test_get_printer_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The file:// sink reports a synthetic-ok status, so the tool returns a status dict without error.
    tools = _tools(_build_server(monkeypatch, writable=False))
    status = await tools["get_printer_status"]()
    assert "reachable" in status


@pytest.mark.asyncio
async def test_invalid_option_raises_tool_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An out-of-range threshold (RenderOptions bounds it to 0<threshold<=100) surfaces the pydantic
    # ValidationError as a clean ToolError rather than an opaque failure.
    tools = _tools(_build_server(monkeypatch, writable=False))
    with pytest.raises(ToolError):
        await tools["preview_label"]("simple", {"title": "x"}, threshold=200.0)


@pytest.mark.asyncio
async def test_preview_label_returns_png(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    result = await tools["preview_label"]("simple", {"title": "Hello"})
    assert isinstance(result, Image)
    assert result.data.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_preview_ephemeral_label_returns_png(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    yaml = (
        "name: adhoc\n"
        "description: designed on the fly\n"
        'label: "62"\n'
        "rotate: 0\n"
        "fields:\n  required: [title]\n  optional: []\n"
        'layout:\n  - {type: title, text: "{{title}}"}\n'
    )
    result = await tools["preview_ephemeral_label"](yaml, {"title": "Ad hoc"})
    assert isinstance(result, Image)
    assert result.data.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_preview_ephemeral_invalid_yaml_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    with pytest.raises(ToolError):
        await tools["preview_ephemeral_label"]("not: a: valid: template", {})


@pytest.mark.asyncio
async def test_preview_label_supports_seq_template(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stored {{seq}} template must be previewable — that needs the sequence parameter, else
    # main.preview 422s and the template can never be previewed via MCP.
    (main.settings.templates_dir / "seq.yaml").write_text(
        "name: seq\n"
        "description: auto-numbered\n"
        'label: "62"\n'
        "rotate: 0\n"
        "fields:\n  required: []\n  optional: []\n"
        'layout:\n  - {type: title, text: "{{seq}}"}\n'
    )
    main.registry.load_all()
    tools = _tools(_build_server(monkeypatch, writable=False))
    # Without a sequence spec it is rejected (mirrors the REST 422) ...
    with pytest.raises(ToolError):
        await tools["preview_label"]("seq", {})
    # ... and with one it renders the first item.
    result = await tools["preview_label"]("seq", {}, sequence={"start": 1, "count": 3})
    assert isinstance(result, Image)
    assert result.data.startswith(b"\x89PNG")


# ── Write tools + history round-trip ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_print_label_and_history_flow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=True))
    printed = await tools["print_label"]("simple", {"title": "Milk"})
    job_id = printed["job_id"]
    assert printed["template"] == "simple"

    # The job is retrievable and appears in the browse listing.
    record = await tools["get_history_label"](job_id)
    assert record["job_id"] == job_id
    assert record["fields"] == {"title": "Milk"}

    page = await tools["list_history"]()
    assert page["total"] >= 1
    assert any(e["job_id"] == job_id for e in page["entries"])
    # The frozen inline body is redacted from the listing.
    assert all("template_source" not in e for e in page["entries"])

    # Reprint replays it, producing a new job id.
    reprinted = await tools["reprint_history_label"](job_id)
    assert reprinted["job_id"] != job_id
    assert reprinted["template"] == "simple"


@pytest.mark.asyncio
async def test_print_ephemeral_label_and_reprint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=True))
    yaml = (
        "name: adhoc\n"
        "description: on the fly\n"
        'label: "62"\n'
        "rotate: 0\n"
        "fields:\n  required: [title]\n  optional: []\n"
        'layout:\n  - {type: title, text: "{{title}}"}\n'
    )
    printed = await tools["print_ephemeral_label"](yaml, {"title": "Ephemeral"})
    job_id = printed["job_id"]
    # The frozen inline body is redacted from the tool output (never surfaced by any REST GET) ...
    record = await tools["get_history_label"](job_id)
    assert "template_source" not in record
    # ... but is retained internally, so a reprint still reproduces the inline job.
    reprinted = await tools["reprint_history_label"](job_id)
    assert reprinted["job_id"] != job_id


@pytest.mark.asyncio
async def test_print_label_missing_required_field_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=True))
    with pytest.raises(ToolError):
        await tools["print_label"]("simple", {})  # 'title' is required


@pytest.mark.asyncio
async def test_reprint_unknown_job_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=True))
    with pytest.raises(ToolError):
        await tools["reprint_history_label"]("00000000-0000-0000-0000-000000000000")


@pytest.mark.asyncio
async def test_print_dry_run_does_not_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=True))
    result = await tools["print_label"]("simple", {"title": "Dry"}, dry_run=True)
    assert result["dry_run"] is True


# ── Feature-flag / bounds hardening (MCP tools honor the same operator-intent gates as REST) ──────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor", "loadable", "source_visible"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
async def test_get_template_source_gated_on_editor_and_loadable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    editor: bool,
    loadable: bool,
    source_visible: bool,
) -> None:
    # The raw YAML source is exposed only when BOTH EDITOR_ENABLED and TEMPLATES_LOADABLE are on,
    # matching the gates the REST /templates/{name}/source route sits behind.
    monkeypatch.setattr(main.settings, "editor_enabled", editor)
    monkeypatch.setattr(main.settings, "templates_loadable", loadable)
    tools = _tools(_build_server(monkeypatch, writable=False))
    result = await tools["get_template"]("simple")
    assert result["required_fields"] == ["title"]  # field contract always returned
    if source_visible:
        assert "layout" in result["yaml"]
    else:
        assert result["yaml"] is None


@pytest.mark.asyncio
async def test_get_history_label_redacts_template_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=True))
    printed = await tools["print_label"]("simple", {"title": "Hi"})
    record = await tools["get_history_label"](printed["job_id"])
    assert "template_source" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": -1}, {"limit": 101}, {"offset": -1}])
async def test_list_history_rejects_out_of_range(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, bad: dict[str, int]
) -> None:
    tools = _tools(_build_server(monkeypatch, writable=False))
    with pytest.raises(ToolError):
        await tools["list_history"](**bad)


@pytest.mark.asyncio
async def test_history_tools_hidden_when_history_ui_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.settings, "history_ui", False)
    tools = _tools(_build_server(monkeypatch, writable=True))
    with pytest.raises(ToolError):
        await tools["list_history"]()
    with pytest.raises(ToolError):
        await tools["get_history_label"]("whatever")


@pytest.mark.asyncio
async def test_reprint_still_works_when_history_ui_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reprint-by-id stays available with browsing hidden, exactly like the REST /reprint route.
    tools = _tools(_build_server(monkeypatch, writable=True))
    printed = await tools["print_label"]("simple", {"title": "Keep"})
    monkeypatch.setattr(main.settings, "history_ui", False)
    reprinted = await tools["reprint_history_label"](printed["job_id"])
    assert reprinted["job_id"] != printed["job_id"]


# ── End-to-end HTTP handshake through the mounted, guarded endpoint ───────────────

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _mounted_app(server: FastMCP, asgi: Any) -> FastAPI:
    """A minimal host app that mounts the guarded MCP endpoint and runs its session manager.

    Bypasses app.main's own startup() (heavy, and irrelevant to the MCP transport) while still
    exercising the real mount + auth guard against the monkeypatched singletons.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        async with server.session_manager.run():
            yield

    host = FastAPI(lifespan=lifespan)
    host.mount("/mcp", main._guard_mcp(asgi))
    return host


def test_http_handshake_and_tools_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real MCP client handshake over HTTP: initialize succeeds and tools/list returns the tools."""
    monkeypatch.setattr(main.settings, "mcp_enabled", True)
    monkeypatch.setattr(main.settings, "mcp_writable", False)
    monkeypatch.setattr(main.settings, "api_token", None)
    monkeypatch.setattr(main.settings, "web_auth_user", None)
    monkeypatch.setattr(main.settings, "web_auth_password", None)
    server, asgi = build_mcp_asgi_app()
    with TestClient(_mounted_app(server, asgi)) as http:
        init = http.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
        assert init.status_code == 200
        assert init.json()["result"]["serverInfo"]["name"] == "labelito"
        listing = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=_MCP_HEADERS,
        )
        names = {t["name"] for t in listing.json()["result"]["tools"]}
        assert READ_TOOLS <= names
        assert not (names & WRITE_TOOLS)


def test_http_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """With API_TOKEN set, the mounted endpoint 401s without the bearer and serves with it."""
    monkeypatch.setattr(main.settings, "mcp_enabled", True)
    monkeypatch.setattr(main.settings, "mcp_writable", False)
    monkeypatch.setattr(main.settings, "api_token", "secret")
    monkeypatch.setattr(main.settings, "web_auth_user", None)
    monkeypatch.setattr(main.settings, "web_auth_password", None)
    server, asgi = build_mcp_asgi_app()
    with TestClient(_mounted_app(server, asgi)) as http:
        denied = http.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
        assert denied.status_code == 401
        allowed = http.post(
            "/mcp",
            json=_INIT,
            headers={**_MCP_HEADERS, "Authorization": "Bearer secret"},
        )
        assert allowed.status_code == 200


def test_http_rejects_cross_site_under_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under Basic auth, a cross-site POST (ambient credentials) to /mcp is refused with 403."""
    monkeypatch.setattr(main.settings, "mcp_enabled", True)
    monkeypatch.setattr(main.settings, "mcp_writable", True)
    monkeypatch.setattr(main.settings, "api_token", None)
    monkeypatch.setattr(main.settings, "web_auth_user", "alice")
    monkeypatch.setattr(main.settings, "web_auth_password", "pw")
    auth = {"Authorization": _basic_header("alice", "pw")}
    server, asgi = build_mcp_asgi_app()
    with TestClient(_mounted_app(server, asgi)) as http:
        cross = http.post(
            "/mcp", json=_INIT, headers={**_MCP_HEADERS, **auth, "Sec-Fetch-Site": "cross-site"}
        )
        assert cross.status_code == 403
        # A same-origin request with the same credentials is allowed.
        same = http.post(
            "/mcp", json=_INIT, headers={**_MCP_HEADERS, **auth, "Sec-Fetch-Site": "same-origin"}
        )
        assert same.status_code == 200
