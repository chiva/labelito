# SPDX-License-Identifier: GPL-3.0-or-later
"""Model Context Protocol (MCP) server for labelito.

Exposes labelito's label capabilities to MCP clients (Claude Desktop, etc.) as tools, served over
streamable HTTP and mounted at ``/mcp`` on the SAME FastAPI app + uvicorn port (see ``app.main``).
Nothing here re-implements rendering or printing: every tool calls the exact same route handlers /
internal helpers the HTTP API uses (``app.main.preview`` / ``print_label`` / ``print_draft`` /
``reprint`` / ``history_list`` / …), so ``_print_lock`` serialization, idempotency de-dup, the
SNMP/USB media preflight, field validation, and history all behave identically to a REST call.

Two env gates govern the surface (see :class:`app.config.Settings`):

* ``MCP_ENABLED`` — whether the server is built and mounted at all (this module is only imported
  when it is true).
* ``MCP_WRITABLE`` — whether the *write* tools (print stored / print ephemeral / reprint) are
  registered alongside the always-on read-only tools. With it false an MCP client's ``tools/list``
  never even shows the write tools, so an AI cannot drive the printer by mistake.

``app.main`` is imported lazily inside :func:`build_mcp_server` (which runs at mount time, after the
whole ``app.main`` module body — singletons, handlers, helpers — has been defined) so there is no
import cycle: ``app.main`` imports *this* module lazily from its mount block.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from app.models import (
    DraftPreviewRequest,
    DraftPrintRequest,
    PrintRequest,
    RenderOptions,
    SequenceSpec,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette


def _format_http_detail(exc: HTTPException) -> str:
    """Flatten an :class:`HTTPException` raised by a reused handler into a client-facing string.

    Handlers raise ``detail`` as either a plain string or a structured mapping (e.g. the media
    mismatch / missing-fields 409/422 shapes). MCP tool errors are plain text, so a mapping is
    JSON-encoded rather than stringified to ``dict`` repr, keeping the message legible to a client.
    """
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, default=str)
    except (TypeError, ValueError):
        return str(detail)


@contextmanager
def _as_tool_error() -> Iterator[None]:
    """Translate the errors the reused handlers raise into a clean :class:`ToolError`.

    The HTTP handlers signal every failure mode as an ``HTTPException`` (404 unknown template, 422
    missing fields / invalid YAML, 409 media mismatch / reprint drift, 503 printer unreachable), and
    hand-built request models can raise pydantic ``ValidationError`` (e.g. an out-of-range threshold
    or a sequence with ``copies > 1``). Both would otherwise surface to the MCP client as an opaque
    internal error; converting them to ``ToolError`` gives the caller the actual reason.
    """
    try:
        yield
    except HTTPException as exc:
        raise ToolError(_format_http_detail(exc)) from exc
    except ValidationError as exc:
        raise ToolError(f"Invalid tool arguments: {exc}") from exc


def _sequence_spec(sequence: dict[str, Any] | None) -> SequenceSpec | None:
    """Build a :class:`SequenceSpec` from a loosely-typed tool argument, or ``None``.

    A ``{{seq}}`` auto-numbering batch is described by ``{start, count, step, padding}``; validation
    (bounds, required keys) is delegated to the model so a bad spec becomes a ``ToolError`` via
    :func:`_as_tool_error` rather than a raw 500.
    """
    if sequence is None:
        return None
    return SequenceSpec.model_validate(sequence)


def build_mcp_server() -> FastMCP:
    """Construct the labelito :class:`FastMCP` server with its tools registered.

    Read-only tools are always registered; the write tools are registered only when
    ``MCP_WRITABLE=true``. Called once from ``app.main``'s mount block when ``MCP_ENABLED`` is set.
    """
    # Imported here (not at module top) so this runs at mount time, after app.main is fully defined,
    # avoiding the app.main <-> app.mcp import cycle. `main` is captured by every tool closure below.
    import app.main as main
    from app.config import settings

    mcp = FastMCP(
        "labelito",
        instructions=(
            "labelito prints labels on a Brother QL label printer from reusable YAML templates. "
            "Use list_templates/get_template to discover a template and its fields, "
            "preview_label / preview_ephemeral_label to see a PNG before committing, and "
            "(when writable) print_label to print a stored template, print_ephemeral_label to "
            "print a label designed on the fly, or reprint_history_label to reprint a past job."
        ),
        # Stateless + plain-JSON responses: each tool call is self-contained (no server-side session
        # to keep alive) and returns a single JSON body rather than an SSE stream — simplest for both
        # AI clients and curl. The endpoint is idempotent per call, matching the REST surface.
        stateless_http=True,
        json_response=True,
        # The streamable-HTTP route sits at the mount root; app.main mounts this app at "/mcp", so the
        # effective endpoint is /mcp (a bare /mcp 307-redirects to /mcp/, which clients follow).
        streamable_http_path="/",
        # DNS-rebinding Host/Origin validation is disabled: labelito is a self-hosted service reached
        # at an arbitrary, deployment-specific host/IP (and often behind a reverse proxy), so the
        # allowlist can't be known here. The /mcp mount is instead guarded by the app's own bearer/
        # Basic auth (see app.main._guard_mcp) plus network placement, the same control as the REST API.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def _require_history_browsing() -> None:
        """Raise when HISTORY_UI is off, so history-browse tools honor the same gate as the REST UI.

        HISTORY_UI=false hides the printed-job list from the browser (the REST /history routes 404);
        reprint-by-id stays available. The MCP history-browse tools mirror that: they refuse here,
        while reprint_history_label does not call this — it stays usable like /reprint.
        """
        if not settings.history_ui:
            raise ToolError("History browsing is disabled (HISTORY_UI=false)")

    # ── Read-only tools (always registered) ──────────────────────────────────────────────────────

    @mcp.tool()
    def list_templates() -> list[dict[str, Any]]:
        """List the available label templates with their field contracts and required media.

        Each entry gives the template name, description, required/optional fields (the values a
        print needs), the label/media it targets, and whether it uses {{seq}} auto-numbering.
        """
        with _as_tool_error():
            return [t.model_dump(mode="json") for t in main.list_templates()]

    @mcp.tool()
    def get_template(name: str) -> dict[str, Any]:
        """Get one template's field contract, plus its raw YAML source when source-loading is enabled.

        The field contract (name, fields, media) is always returned — it is the same non-sensitive
        data ``list_templates`` exposes. The raw ``yaml`` source is included only when
        ``TEMPLATES_LOADABLE`` is true (else ``None``): that flag governs whether template source may
        be read (the REST ``/templates/{name}/source`` route 404s when it is off), so honoring it here
        keeps the MCP surface from disclosing source an operator has deliberately hidden.
        """
        with _as_tool_error():
            tmpl = main.registry.get(name)
            if tmpl is None:
                raise ToolError(f"No template named {name!r}")
            yaml = main.get_template_source(name).yaml if settings.templates_loadable else None
            return {
                "name": tmpl.name,
                "description": tmpl.description,
                "label": tmpl.label,
                "rotate": tmpl.rotate,
                "valign": tmpl.valign,
                "required_fields": tmpl.required_fields,
                "optional_fields": tmpl.optional_fields,
                "is_example": tmpl.is_example,
                "yaml": yaml,
            }

    @mcp.tool()
    def get_capabilities() -> dict[str, Any]:
        """Report the configured printer's capabilities: model, dpi, supported labels, geometries."""
        with _as_tool_error():
            return main.capabilities().model_dump(mode="json")

    @mcp.tool()
    async def get_printer_status() -> dict[str, Any]:
        """Query the physical printer's live state: loaded media, model, and any fault/error bits."""
        with _as_tool_error():
            res = await main.printer_status(None)
            if isinstance(res, JSONResponse):
                # 503 (unreachable/busy): the body is always a PrinterStatusResponse dump (a dict).
                decoded: dict[str, Any] = json.loads(bytes(res.body))
                return decoded
            return res.model_dump(mode="json")

    @mcp.tool()
    async def preview_label(
        template: str,
        fields: dict[str, Any] | None = None,
        language: str | None = None,
        dither: bool | None = None,
        threshold: float | None = None,
    ) -> Image:
        """Render a PNG preview of a STORED template (no print). Ephemeral — nothing is sent or saved.

        `fields` supplies the template's field values. `dither`/`threshold` control the black/white
        conversion (None inherits the server defaults), matching what print_label would produce.
        """
        with _as_tool_error():
            request = PrintRequest(
                template=template,
                fields=fields or {},
                language=language,
                options=RenderOptions(dither=dither, threshold=threshold),
            )
            response = await main.preview(request)
            return Image(data=bytes(response.body), format="png")

    @mcp.tool()
    async def preview_ephemeral_label(
        yaml: str,
        fields: dict[str, Any] | None = None,
        language: str | None = None,
        dither: bool | None = None,
        threshold: float | None = None,
        sequence: dict[str, Any] | None = None,
    ) -> Image:
        """Render a PNG preview of an INLINE template designed on the fly (no print, nothing saved).

        `yaml` is a full label template body (same schema as a stored template); it is validated
        exactly like a saved file. Use this to iterate on a design before print_ephemeral_label.
        """
        with _as_tool_error():
            request = DraftPreviewRequest(
                yaml=yaml,
                fields=fields or {},
                language=language,
                options=RenderOptions(dither=dither, threshold=threshold),
                sequence=_sequence_spec(sequence),
            )
            response = await main.preview_draft(request)
            return Image(data=bytes(response.body), format="png")

    @mcp.tool()
    def list_history(
        limit: int = main.DEFAULT_HISTORY_PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        """Browse recorded print jobs, newest first. Returns entries plus the total for pagination.

        Each entry's frozen inline template body is omitted; use get_history_label(job_id) for a
        single job's full detail, or reprint_history_label(job_id) to reprint it. Hidden (errors)
        when HISTORY_UI=false, mirroring the REST browse routes.
        """
        with _as_tool_error():
            _require_history_browsing()
            # Enforce the SAME bounds the REST /history/list route applies via its Query() constraints
            # — a direct handler call bypasses them, so a negative/huge limit or offset would otherwise
            # reach SQLite raw (limit=-1 dumps the whole table; an out-of-int64 offset raises deep in
            # the bind). Validate up front so the caller gets a clean ToolError, not an unbounded dump
            # or an opaque internal error.
            if not 1 <= limit <= main.MAX_HISTORY_PAGE_SIZE:
                raise ToolError(f"limit must be between 1 and {main.MAX_HISTORY_PAGE_SIZE}")
            if not 0 <= offset <= main.MAX_HISTORY_OFFSET:
                raise ToolError(f"offset must be between 0 and {main.MAX_HISTORY_OFFSET}")
            page = main.history_list(offset=offset, limit=limit)
            # Redact the frozen inline template body from the listing, exactly as GET /history/list
            # does — it is retained only so reprint can reconstruct an inline job, never browsed.
            return page.model_dump(
                mode="json", exclude={"entries": {"__all__": {"template_source"}}}
            )

    @mcp.tool()
    def get_history_label(job_id: str) -> dict[str, Any]:
        """Get one recorded print job's detail by its job id (template, fields, options, status).

        Hidden (errors) when HISTORY_UI=false, mirroring the REST browse routes. The frozen inline
        template body (``template_source``) is redacted — the REST API never surfaces it through any
        GET route (it is retained only so reprint can reconstruct an inline job internally).
        """
        with _as_tool_error():
            _require_history_browsing()
            record = main._load_job(job_id)
            if record is None:
                raise ToolError(f"Job {job_id!r} not found in history")
            return record.model_dump(mode="json", exclude={"template_source"})

    # ── Write tools (registered only when MCP_WRITABLE=true) ──────────────────────────────────────
    if settings.mcp_writable:

        @mcp.tool()
        async def print_label(
            template: str,
            fields: dict[str, Any] | None = None,
            copies: int = 1,
            language: str | None = None,
            dry_run: bool = False,
            dither: bool | None = None,
            threshold: float | None = None,
            high_res: bool | None = None,
            red: bool | None = None,
            idempotency_key: str | None = None,
            sequence: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Print a STORED template on the printer. Returns the created job (job_id, copies, …).

            `fields` supplies the template's values. Set `dry_run=true` to render/validate without
            sending to hardware. `idempotency_key` makes a retry return the same job instead of
            printing twice. Requires MCP_WRITABLE=true.
            """
            with _as_tool_error():
                request = PrintRequest(
                    template=template,
                    fields=fields or {},
                    copies=copies,
                    dry_run=dry_run,
                    language=language,
                    options=RenderOptions(
                        dither=dither, threshold=threshold, high_res=high_res, red=red
                    ),
                    idempotency_key=idempotency_key,
                    sequence=_sequence_spec(sequence),
                )
                return (await main.print_label(request)).model_dump(mode="json")

        @mcp.tool()
        async def print_ephemeral_label(
            yaml: str,
            fields: dict[str, Any] | None = None,
            copies: int = 1,
            language: str | None = None,
            dry_run: bool = False,
            dither: bool | None = None,
            threshold: float | None = None,
            high_res: bool | None = None,
            red: bool | None = None,
            idempotency_key: str | None = None,
            sequence: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Print an EPHEMERAL label designed on the fly from an inline YAML template body.

            `yaml` is a full label template (validated like a saved file); it is never written to
            disk, but the print IS recorded in history (with the frozen body) so it can be reprinted.
            Requires MCP_WRITABLE=true.
            """
            with _as_tool_error():
                request = DraftPrintRequest(
                    yaml=yaml,
                    fields=fields or {},
                    copies=copies,
                    dry_run=dry_run,
                    language=language,
                    options=RenderOptions(
                        dither=dither, threshold=threshold, high_res=high_res, red=red
                    ),
                    idempotency_key=idempotency_key,
                    sequence=_sequence_spec(sequence),
                )
                return (await main.print_draft(request)).model_dump(mode="json")

        @mcp.tool()
        async def reprint_history_label(job_id: str) -> dict[str, Any]:
            """Reprint a past job exactly, by its job id (from list_history). Requires MCP_WRITABLE=true.

            Reproduces the original label — same template, fields, options, and computed dates.
            Errors if the job is unknown, failed, contained an image, or no longer validates.
            """
            with _as_tool_error():
                return (await main.reprint(job_id)).model_dump(mode="json")

    return mcp


def build_mcp_asgi_app() -> tuple[FastMCP, Starlette]:
    """Build the MCP server and its mountable streamable-HTTP ASGI app.

    Returns ``(server, asgi_app)``: ``app.main`` mounts ``asgi_app`` at ``/mcp`` (behind its auth
    guard) and runs ``server.session_manager.run()`` inside the app lifespan — the streamable-HTTP
    session manager's task group must be active for the mounted route to serve requests.
    """
    server = build_mcp_server()
    return server, server.streamable_http_app()
