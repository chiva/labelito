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
* ``MCP_WRITABLE`` — whether the *mutating* tools are registered alongside the always-on read-only
  ones: the print tools (print stored / print ephemeral / reprint) and, where the editor gates also
  allow it, ``save_template``. With it false an MCP client's ``tools/list`` never even shows them,
  so an AI can neither drive the printer nor change what a later print will produce. It is the one
  switch that makes this server read-only, so every tool with a side effect must sit behind it —
  the other gates (``EDITOR_ENABLED``, ``TEMPLATES_WRITABLE``) narrow the surface further, never
  grant past it.

``app.main`` is imported lazily inside :func:`build_mcp_server` (which runs at mount time, after the
whole ``app.main`` module body — singletons, handlers, helpers — has been defined) so there is no
import cycle: ``app.main`` imports *this* module lazily from its mount block.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.mcp_schema import template_schema_markdown
from app.models import (
    DraftPreviewRequest,
    DraftPrintRequest,
    PrintRequest,
    RenderOptions,
    SaveTemplateRequest,
    SequenceSpec,
    TemplateParseRequest,
)
from app.render.elements import (
    FA_STYLES,
    ICON_ASSET_EXTS,
    ICON_DEFAULT_STYLE,
    KNOWN_COLLECTIONS,
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


# Upper bound on list_icons' page size. FontAwesome free alone runs past a thousand names per
# style, so an unbounded list would blow an MCP client's context on a single call; the tool reports
# `total` and `truncated` instead, steering the caller to narrow its query.
MAX_ICON_RESULTS = 500

# SaveTemplateRequest.name is vestigial: app.main.save_template derives the save path from the
# VALIDATED template's internal `name` (so filename == registry key == internal name) and never
# reads this field. The model still requires a non-empty string, so pass a fixed placeholder rather
# than inventing a second, ignored source of truth for an MCP caller to get wrong.
SAVE_NAME_PLACEHOLDER = "mcp-draft"


def _icon_dir(collection: str, style: str | None, collections_root: Path) -> Path:
    """Directory holding *collection*'s SVGs, mirroring ``IconElement._resolve_path``.

    FontAwesome splits its icons into per-style subdirectories; the other collections are flat and
    ignore ``style`` entirely, exactly as the renderer does.
    """
    base = collections_root / collection
    if collection == "fontawesome":
        base = base / (style if style in FA_STYLES else ICON_DEFAULT_STYLE)
    return base


def _scan_icons(directory: Path, suffixes: tuple[str, ...]) -> list[str]:
    """Sorted icon names (stems) of the files in *directory* matching *suffixes*.

    Blocking filesystem work — callers offload it to a threadpool. A missing directory yields an
    empty list rather than raising: the bundled collections are populated at image build time, so an
    unusual deployment can legitimately lack one, and "none installed" is the honest answer.
    """
    if not directory.is_dir():
        return []
    return sorted({entry.stem for entry in directory.iterdir() if entry.suffix.lower() in suffixes})


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
            "print a label designed on the fly, or reprint_history_label to reprint a past job. "
            "To WRITE a template rather than use an existing one, read the docs://template-schema "
            "resource first — it is the generated reference for the element vocabulary, the "
            "{{token}} grammar and the icon-resolution rules, and it names the failure modes that "
            "render silently (clipped text, an unresolved icon). Then validate_template a draft, "
            "preview_ephemeral_label it, and save_template it when persistence is enabled."
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

    # ── Resources ────────────────────────────────────────────────────────────────────────────────

    @mcp.resource(
        "docs://template-schema",
        name="template-schema",
        title="labelito template schema",
        description=(
            "How to write a labelito template: the envelope, every element type with its fields "
            "and defaults, the {{token}} and [[translation]] grammars, the two icon-resolution "
            "modes, and the silent failure modes to design around. Generated from the renderer, so "
            "it always matches this server's version."
        ),
        mime_type="text/markdown",
    )
    def template_schema() -> str:
        """Serve the generated template-authoring reference (see :mod:`app.mcp_schema`)."""
        return template_schema_markdown()

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
    async def get_template(name: str) -> dict[str, Any]:
        """Get one template's full info (the same shape as a list_templates entry) plus its YAML source.

        The info block — name, description, label, ``fields`` (required/optional/image_fields),
        ``media``, ``uses_seq`` — is reused verbatim from ``list_templates`` (the canonical
        ``TemplateInfo`` shape) so the two tools never drift, and is always returned from the in-memory
        registry. The extra ``yaml`` source is included only when the operator has enabled
        source-loading, i.e. BOTH ``EDITOR_ENABLED`` and ``TEMPLATES_LOADABLE`` are true (the same
        gates the REST ``/templates/{name}/source`` route sits behind, and ``EDITOR_ENABLED`` defaults
        to false) — otherwise ``yaml`` is ``None``. If the source file has since gone missing or is
        oversized, ``yaml`` degrades to ``None`` rather than failing the whole call, mirroring how the
        REST ``/templates`` list still serves the contract when only ``/source`` would 404/413. The
        blocking read is offloaded to a threadpool (FastMCP runs a sync tool on the event loop).
        """
        with _as_tool_error():
            tmpl = main.registry.get(name)
            if tmpl is None:
                raise ToolError(f"No template named {name!r}")
            result = main._template_info(tmpl).model_dump(mode="json")
            result["yaml"] = None
            if settings.editor_enabled and settings.templates_loadable:
                try:
                    source = await run_in_threadpool(main.get_template_source, name)
                    result["yaml"] = source.yaml
                except HTTPException as exc:
                    # Only the "source no longer available" cases degrade to yaml=None (the contract
                    # is still served, as /templates does while /source 404/413s): a file deleted
                    # after load (404) or grown past MAX_TEMPLATE_SOURCE_BYTES (413). A 500 (an
                    # unexpected OSError, e.g. a permissions misconfig) is re-raised so the operator
                    # sees the real failure instead of a misleading "source-loading is off".
                    if exc.status_code not in (404, 413):
                        raise
            return result

    @mcp.tool()
    def get_capabilities() -> dict[str, Any]:
        """Report the configured printer's capabilities: model, dpi, supported labels, geometries."""
        with _as_tool_error():
            return main.capabilities().model_dump(mode="json")

    @mcp.tool()
    async def list_icons(
        collection: str | None = None,
        style: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Find a name for an `icon` element, from the bundled collections or your own asset files.

        Call with no arguments for a summary: every collection and how many icons it holds. Pass a
        `collection` to list names — always with a `query` substring, because a full collection runs
        to thousands and only `limit` of them come back (the response says `total` and `truncated`
        so a filter can be narrowed rather than guessed at).

        `collection` is one of the bundled sets, or `"custom"` for the files you placed in the icons
        directory (which an `icon` element references with NO `collection` key — see
        docs://template-schema). `style` applies to fontawesome only.

        An icon that does not exist renders as blank space rather than an error, so confirming a
        name here is the difference between a missing glyph and a silently empty label.
        """
        with _as_tool_error():
            collections_root = settings.icon_collections_dir
            if collection is None:
                summary = {
                    name: len(
                        await run_in_threadpool(
                            _scan_icons, _icon_dir(name, style, collections_root), (".svg",)
                        )
                    )
                    for name in sorted(KNOWN_COLLECTIONS)
                }
                summary["custom"] = len(
                    await run_in_threadpool(_scan_icons, settings.icons_dir, ICON_ASSET_EXTS)
                )
                return {
                    "collections": summary,
                    "fontawesome_styles": sorted(FA_STYLES),
                    "note": (
                        "Pass collection (and a query) to list names. fontawesome counts are for "
                        f"style={style or ICON_DEFAULT_STYLE!r}."
                    ),
                }

            if collection == "custom":
                names = await run_in_threadpool(_scan_icons, settings.icons_dir, ICON_ASSET_EXTS)
            elif collection in KNOWN_COLLECTIONS:
                names = await run_in_threadpool(
                    _scan_icons, _icon_dir(collection, style, collections_root), (".svg",)
                )
            else:
                known = ", ".join([*sorted(KNOWN_COLLECTIONS), "custom"])
                raise ToolError(f"Unknown collection {collection!r}; known collections: {known}")

            if query:
                needle = query.lower()
                names = [n for n in names if needle in n.lower()]
            if not 1 <= limit <= MAX_ICON_RESULTS:
                raise ToolError(f"limit must be between 1 and {MAX_ICON_RESULTS}")
            return {
                "collection": collection,
                "style": style if collection == "fontawesome" else None,
                "total": len(names),
                "truncated": len(names) > limit,
                "icons": names[:limit],
            }

    @mcp.tool()
    async def get_printer_status() -> dict[str, Any]:
        """Query the physical printer's live state: loaded media, model, and any fault/error bits."""
        with _as_tool_error():
            res = await main.printer_status(None)
            if isinstance(res, JSONResponse):
                # 503 (unreachable/busy): the body is always a PrinterStatusResponse dump (a dict).
                # bytes() coerces the memoryview half of Response.body's bytes|memoryview type, which
                # json.loads does not accept.
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
        sequence: dict[str, Any] | None = None,
    ) -> Image:
        """Render a PNG preview of a STORED template (no print). Ephemeral — nothing is sent or saved.

        `fields` supplies the template's field values. `dither`/`threshold` control the black/white
        conversion (None inherits the server defaults), matching what print_label would produce. A
        template that uses the `{{seq}}` auto-numbering token requires a `sequence` spec
        (`{start, count, step, padding}`); the preview renders its first item.
        """
        with _as_tool_error():
            request = PrintRequest(
                template=template,
                fields=fields or {},
                language=language,
                options=RenderOptions(dither=dither, threshold=threshold),
                sequence=_sequence_spec(sequence),
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
    async def list_history(
        limit: int = main.DEFAULT_HISTORY_PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        """Browse recorded print jobs, newest first. Returns entries plus the total for pagination.

        Each entry's frozen inline template body is omitted; use get_history_label(job_id) for a
        single job's full detail, or reprint_history_label(job_id) to reprint it. Hidden (errors)
        when HISTORY_UI=false, mirroring the REST browse routes. The blocking SQLite read is offloaded
        to a threadpool (FastMCP runs a sync tool on the event loop), as the REST route does.
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
            page = await run_in_threadpool(main.history_list, offset=offset, limit=limit)
            # Redact the frozen inline template body from the listing, exactly as GET /history/list
            # does — it is retained only so reprint can reconstruct an inline job, never browsed.
            return page.model_dump(
                mode="json", exclude={"entries": {"__all__": {"template_source"}}}
            )

    @mcp.tool()
    async def get_history_label(job_id: str) -> dict[str, Any]:
        """Get one recorded print job's detail by its job id (template, fields, options, status).

        Hidden (errors) when HISTORY_UI=false, mirroring the REST browse routes. The frozen inline
        template body (``template_source``) is redacted — the REST API never surfaces it through any
        GET route (it is retained only so reprint can reconstruct an inline job internally). The
        blocking SQLite read is offloaded to a threadpool, as ``main.reprint`` does for the same read.
        """
        with _as_tool_error():
            _require_history_browsing()
            record = await run_in_threadpool(main._load_job, job_id)
            if record is None:
                raise ToolError(f"Job {job_id!r} not found in history")
            return record.model_dump(mode="json", exclude={"template_source"})

    # ── Template authoring ───────────────────────────────────────────────────────────────────────
    # EDITOR_ENABLED is the shared floor, mirroring the gate on the POST /templates/parse and
    # POST /templates routes these reuse: an operator who turns the editor off has said "no
    # template authoring on this deployment", and the MCP surface honours that verbatim.
    #
    # Above that floor the two tools diverge by what they DO, not by which route they wrap.
    # validate_template only parses, so it sits with the read-only tools — and gains nothing an MCP
    # client lacks anyway, since preview_ephemeral_label already validates arbitrary YAML ungated.
    # save_template mutates, so it additionally requires MCP_WRITABLE (see below).
    if settings.editor_enabled:

        @mcp.tool()
        async def validate_template(yaml: str) -> dict[str, Any]:
            """Validate a draft template body and return its auto-detected field contract.

            The cheap half of preview_ephemeral_label: same validation, no rendering. Returns the
            name, description, label, rotate, valign, the required/optional fields the loader
            inferred from the layout's `{{tokens}}`, and whether the draft uses `{{seq}}`.

            Use it to check a draft's shape while writing it — computed tokens ({{date}}, {{now}},
            {{seq}}) and [[translations]] are correctly excluded from the contract, so the fields it
            reports are exactly the ones a print must supply.
            """
            with _as_tool_error():
                response = await main.parse_template(TemplateParseRequest(yaml=yaml))
                return response.model_dump(mode="json")

        # Persisting a template is a WRITE through MCP, so it needs MCP_WRITABLE on top of the
        # editor's own TEMPLATES_WRITABLE opt-in. Both matter, and neither alone is enough:
        # MCP_WRITABLE=false has to mean this server mutates nothing. A saved template outlives the
        # call and is picked up by every later print — including ones made from the web UI or the
        # REST API — so an MCP client able to replace one can cause the WRONG LABEL to come out of
        # a print it never made itself. That is a longer-lived hazard than the single print
        # MCP_WRITABLE already guards, not a lesser one.
        if settings.mcp_writable and settings.templates_writable:

            @mcp.tool()
            async def save_template(yaml: str) -> dict[str, Any]:
                """Persist a template to the templates directory and hot-reload it.

                The body is validated BEFORE anything is written, the write is atomic, and a draft
                that fails to register is rolled back — so a broken template can never replace a
                working one. The file name and the registry key both come from the template's own
                `name:` key, so saving with an existing name REPLACES that template.

                Requires MCP_WRITABLE=true and TEMPLATES_WRITABLE=true with a writable templates
                directory. Returns the saved name; preview it first with preview_ephemeral_label.
                """
                with _as_tool_error():
                    request = SaveTemplateRequest(name=SAVE_NAME_PLACEHOLDER, yaml=yaml)
                    return await main.save_template(request)

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
