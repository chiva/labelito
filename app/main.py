# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI application — all routes."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from brother_ql.exceptions import BrotherQLUnsupportedCmd
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from prometheus_client import Counter, Gauge, generate_latest
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from app.config import settings
from app.drivers.brother_ql import BrotherQLDriver
from app.history import HistoryStore, build_history_store
from app.loader import (
    Template,
    TemplateLoadError,
    TemplateRegistry,
    validate_template_from_string,
)
from app.media import MediaMatch, RequiredMedia, media_matches, required_media_for
from app.models import (
    CapabilityResponse,
    DraftPreviewRequest,
    HealthResponse,
    HistoryPage,
    LivenessResponse,
    PrinterState,
    PrinterStatusResponse,
    PrintJobRecord,
    PrintRequest,
    PrintResponse,
    ReadinessResponse,
    RenderOptions,
    SaveTemplateRequest,
    SequenceSpec,
    TemplateFieldContract,
    TemplateInfo,
    TemplateMedia,
    TemplateParseRequest,
    TemplateParseResponse,
    TemplateSourceResponse,
)
from app.render.engine import (
    RenderEngine,
    _brother_ql_model_max_rows,
    format_seq,
    image_field_names,
    uses_seq,
)
from app.render.i18n import Translator
from app.transports.base import PrinterStatus, Transport, get_transport, infer_transport
from app.transports.file import FileTransport  # noqa: F401 — registers transport
from app.transports.network import NetworkTransport  # noqa: F401 — registers transport
from app.transports.snmp import (
    CONSOLE_READY,
    HR_PRINTER_ERROR_BITS,
    HR_PRINTER_STATUS_BUSY,
    HR_PRINTER_STATUS_OTHER,
    PrinterSNMPStatus,
    query_snmp_status,
)
from app.transports.usb import USBTransport  # noqa: F401 — registers transport

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Prometheus metrics ─────────────────────────────────────────────────────────
LABELS_PRINTED = Counter("labels_printed_total", "Total labels printed", ["template", "dry_run"])
LABEL_ERRORS = Counter("label_errors_total", "Print errors", ["reason"])
LAST_PRINT_TS = Gauge("last_print_timestamp_seconds", "Unix timestamp of last print")
# A real network print that proceeded WITHOUT the SNMP media/fault preflight because SNMP was enabled
# but unreachable (timeout / filtered UDP 161 / wrong community / unsupported critical OID). The guard
# fails open by design — SNMP is opportunistic, not required — but a rising count means prints are
# going out unverified, i.e. the phantom-success class is unguarded until SNMP becomes reachable.
PREFLIGHT_SNMP_UNREACHABLE = Counter(
    "print_preflight_snmp_unreachable_total",
    "Real prints allowed without an SNMP media/fault check because SNMP was unreachable",
)

# ── SNMP-derived printer telemetry (network transport only) ───────────────────────────────────────
# Freshness model (A) — last-known, refreshed lazily. The app has no background printer poll
# (docs/known-limitations.md), so these gauges are refreshed only when the printer is actually queried
# over SNMP: on a /print preflight and on a /printer/status query. /metrics reports the last-known
# values (which may be stale) and NEVER triggers a live SNMP query per scrape — that would add UDP 161
# traffic and print-lock contention for no real benefit on a home app. PRINTER_STATUS_LAST_QUERY_TS
# makes the staleness visible so an alert can flag "last queried too long ago" if desired.
#
# State model — unknown/not-applicable is NaN, never a misleading 0. A scalar gauge defaults to 0,
# which would read as "printer down" / "zero labels" on a cold start, on a non-network (USB/file)
# deployment, or with SNMP disabled — none of which ever query SNMP. So these are initialized to NaN
# (Prometheus treats NaN as no-data: `printer_up == 0` alerts do not fire on it) and only take a
# concrete value once an SNMP query actually observes one. ``printer_up`` 0 therefore means "queried
# and did not answer" (genuinely down), distinct from NaN "never queried / not applicable".
_METRIC_UNKNOWN = float("nan")
PRINTER_UP = Gauge(
    "printer_up", "1 printer answered the last SNMP query, 0 queried-but-down, NaN not-queried"
)
PRINTER_UP.set(_METRIC_UNKNOWN)
PRINTER_DETECTED_ERROR_STATE = Gauge(
    "printer_detected_error_state",
    "hrPrinterDetectedErrorState per condition: 1 set, 0 clear, NaN when the printer is unreachable",
    ["condition"],
)
PRINTER_LABEL_LIFECOUNT = Gauge(
    "printer_label_lifecount", "Lifetime label count from prtMarkerLifeCount (NaN when unobserved)"
)
PRINTER_LABEL_LIFECOUNT.set(_METRIC_UNKNOWN)
# Only the model is exported (already public via /health). serial/firmware/hostname are stable device
# identifiers and are deliberately NOT put on the unauthenticated metrics surface — they stay on the
# token-protected /printer/status. (/metrics carries no token; leaking identifiers there would be a
# weaker gate than the status route that returns the same fields.)
PRINTER_INFO = Gauge("printer_info", "Printer model (value always 1)", ["model"])
PRINTER_MEDIA_INFO = Gauge(
    "printer_media_info",
    "Currently loaded media (value always 1)",
    ["media_name", "media_type", "width_mm"],
)
PRINTER_STATUS_LAST_QUERY_TS = Gauge(
    "printer_status_last_query_timestamp_seconds",
    "Unix timestamp of the last SNMP printer query (NaN until one happens, so staleness is visible)",
)
PRINTER_STATUS_LAST_QUERY_TS.set(_METRIC_UNKNOWN)


def _set_printer_metrics(
    *,
    reachable: bool,
    error_conditions: list[str],
    label_lifecount: int | None,
    model: str | None,
    media_name: str | None,
    media_type: str | None,
    media_width_mm: float | None,
) -> None:
    """Update the SNMP telemetry gauges from one query's decoded values.

    The single sink for both the /print preflight and the /printer/status query. Per-condition error
    gauges are driven off the SNMP layer's already-DECODED condition names (``error_conditions``), not
    re-derived from the raw bitmask: hrPrinterDetectedErrorState is a BITS value whose bit numbering
    (MSB-first, octet-width-dependent) does not match a plain ``1 << index``, so re-bit-shifting the
    mask here would misclassify real faults. ``printer_info``/``printer_media_info`` are cleared before
    each set so a changed model/loaded-media never leaves a stale series exported at 1.

    Unknown is represented as NaN, never a misleading concrete value: when the printer is unreachable
    ``printer_up`` is 0 (queried-and-down) but every per-condition gauge and the life-count become NaN
    — we cannot know fault state or the counter on a printer that did not answer, and a stale prior
    value must not look current just because the query timestamp refreshed.
    """
    active = set(error_conditions)
    PRINTER_UP.set(1 if reachable else 0)
    PRINTER_STATUS_LAST_QUERY_TS.set_to_current_time()
    for _bit, name in HR_PRINTER_ERROR_BITS:
        PRINTER_DETECTED_ERROR_STATE.labels(condition=name).set(
            (1 if name in active else 0) if reachable else _METRIC_UNKNOWN
        )
    # A nonzero hrPrinterDetectedErrorState the SNMP layer couldn't map to a known RFC 3805 bit is
    # surfaced as an ``unknownErrorBits:*`` string (firmware version skew / nonstandard bit). Without
    # a catch-all series, such a fault would leave every known condition at 0 and read as healthy
    # while the print preflight rejects the job. Expose it as condition="unknown" so alerting still
    # fires on any fault the guard would block.
    has_unknown_fault = any(e.startswith("unknownErrorBits") for e in active)
    PRINTER_DETECTED_ERROR_STATE.labels(condition="unknown").set(
        (1 if has_unknown_fault else 0) if reachable else _METRIC_UNKNOWN
    )
    PRINTER_INFO.clear()
    if reachable and model:
        PRINTER_INFO.labels(model=model).set(1)
    PRINTER_MEDIA_INFO.clear()
    if reachable and (media_name or media_type or media_width_mm is not None):
        PRINTER_MEDIA_INFO.labels(
            media_name=media_name or "",
            media_type=media_type or "",
            width_mm=(f"{media_width_mm:g}" if media_width_mm is not None else ""),
        ).set(1)
    # Reset to unknown (NaN) when the printer is down or the optional counter OID is absent, so a
    # previously-observed count never lingers as if current.
    PRINTER_LABEL_LIFECOUNT.set(
        label_lifecount if (reachable and label_lifecount is not None) else _METRIC_UNKNOWN
    )


def _record_snmp_metrics(snmp: PrinterSNMPStatus) -> None:
    """Record telemetry from a print-preflight :class:`PrinterSNMPStatus`.

    ``snmp.errors`` carries the decoded HR condition names (plus any console/unknown strings, which
    the recorder ignores — it only matches the known condition names)."""
    _set_printer_metrics(
        reachable=snmp.reachable,
        error_conditions=snmp.errors,
        label_lifecount=snmp.label_lifecount,
        model=snmp.model,
        media_name=snmp.media_name,
        media_type=snmp.media_type,
        media_width_mm=snmp.media_width_mm,
    )


def _record_status_metrics(status: PrinterStatus) -> None:
    """Record telemetry from a /printer/status :class:`PrinterStatus`.

    ``status.errors`` carries the same decoded condition names as the SNMP layer (PrinterStatus.from_snmp
    copies them); the loaded-media name rides in ``raw`` so no second SNMP query is needed."""
    raw = status.raw if isinstance(status.raw, dict) else {}
    media_name = raw.get("media_name")
    _set_printer_metrics(
        reachable=status.reachable,
        error_conditions=status.errors,
        label_lifecount=status.label_lifecount,
        model=status.model,
        media_name=media_name if isinstance(media_name, str) else None,
        media_type=status.media_type,
        media_width_mm=status.media_width_mm,
    )


# ── Image upload limits ──────────────────────────────────────────────────────────
# A label is a small thermal print (≤ ~696 px wide at 300 dpi, downscaled before printing),
# so uploads only need to be large enough for a phone photo to crop from — not unbounded.
# These guard against memory exhaustion / decompression bombs on /preview/multipart.
MAX_IMAGE_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB decoded image
# A base64 string inflates the byte count by 4/3; this bounds the encoded string length so an
# oversized JSON image field is rejected before it is decoded into memory.
MAX_IMAGE_B64_CHARS = ((MAX_IMAGE_UPLOAD_BYTES + 2) // 3) * 4
MAX_IMAGE_PIXELS = 16_000_000  # 16 MP decoded (e.g. ~4900x3200); ample for any source photo
# Make PIL refuse decompression bombs everywhere it opens an image (incl. base64 JSON fields).
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# A text element with no max_lines wraps arbitrary field text and allocates a PIL strip sized to
# the wrapped height *before* the canvas is clamped to max_length_px — so unbounded input text can
# allocate a huge buffer and kill the worker. A thermal label holds a few hundred chars at most;
# this cap rejects pathological input long before it reaches the renderer.
MAX_TEXT_FIELD_CHARS = 1000

# Coarse outer guard on the whole request body, checked from Content-Length before the body is
# parsed — so an oversized JSON or multipart upload is rejected without being read into memory or
# spooled to disk (the per-field image/text caps are the fine-grained, authoritative limits). Must
# exceed the largest legitimate body: a JSON request carrying a base64 image is ~7 MiB.
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024

# History records persist field names too, so an unbounded count or pathologically long key would
# bloat every stored history record (and the rows scanned on reprint/idempotency lookups) without
# consuming a label. A real label template has a handful of short-named fields; these caps are
# generous for that.
MAX_FIELD_COUNT = 50
MAX_FIELD_NAME_CHARS = 100

# Browse-UI pagination. A thermal-label home app has a small, glanceable history; one screenful
# at a time is plenty, with a ceiling so a crafted ?limit= cannot ask the store for everything.
DEFAULT_HISTORY_PAGE_SIZE = 20
MAX_HISTORY_PAGE_SIZE = 100
# OFFSET is bound into SQLite as a signed 64-bit integer; a value past that range raises at bind
# time and would surface as a 500. Cap it well below int64 max — and far above any retained history
# (bounded by HISTORY_KEEP_ENTRIES) — so an absurd offset is a controlled 422, not a crash. An
# in-range offset past the actual row count already returns an empty page.
MAX_HISTORY_OFFSET = 10_000_000

# ── OpenAPI tag groups (short descriptions surface as section headers in /docs) ──
OPENAPI_TAGS = [
    {"name": "Printing", "description": "Render and send labels to the printer; preview rasters."},
    {"name": "History", "description": "Browse and delete recorded print jobs."},
    {"name": "Templates", "description": "List label templates and hot-reload them."},
    {"name": "System", "description": "Health, capabilities, and Prometheus metrics."},
]

# Reused documented error responses, keyed by the status codes the routes actually return. Kept
# terse — Swagger shows them under each operation so a client knows the failure modes up front.
# Annotated with the key/value type FastAPI's `responses=` parameter expects so the `**`-spread in
# the route decorators does not trip mypy's dict-item inference (a bare literal infers as the
# narrower dict[int, dict[str, str]], which strict mypy rejects against the wider expected type).
RESPONSE_401: dict[int | str, dict[str, Any]] = {
    401: {"description": "Invalid or missing API token"}
}
RESPONSE_413: dict[int | str, dict[str, Any]] = {
    413: {"description": "Request body exceeds the size limit"}
}

# Pre-typed response maps for the studio routes, composed from the same explicit type as above.
_DRAFT_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Invalid or missing API token"},
    413: {"description": "Request body exceeds the size limit"},
    400: {"description": "Label not supported by the configured printer model"},
    422: {"description": "Invalid template YAML, schema error, or oversized/invalid fields"},
}
_PARSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Invalid or missing API token"},
    413: {"description": "Request body exceeds the size limit"},
    422: {"description": "Invalid template YAML or schema error"},
}
_SAVE_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Invalid or missing API token"},
    413: {"description": "Request body exceeds the size limit"},
    403: {"description": "Server-save is disabled (TEMPLATES_WRITABLE=false)"},
    422: {"description": "Invalid template YAML, schema error, or unsafe template name"},
}
_SOURCE_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Invalid or missing API token"},
    404: {"description": "No template with that name (or its file is missing/unsafe)"},
    413: {"description": "Template file exceeds the size limit"},
}

# Upper bound for a template YAML the studio will load back into the editor. Template files are tiny
# (a few KiB at most), so this is a generous ceiling that simply caps a pathologically large file
# rather than streaming it into a JSON response.
MAX_TEMPLATE_SOURCE_BYTES = 256 * 1024

# ── App init ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Labelito",
    version="0.1.0",
    description="Self-hosted label printing for Brother QL printers.",
    license_info={"name": "GPL-3.0-or-later"},
    openapi_tags=OPENAPI_TAGS,
)


@app.middleware("http")
async def _limit_body_size(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject oversized request bodies by Content-Length, before they are read or spooled.

    Endpoint-level caps (image bytes, text chars) still apply, but they run only after the body
    has been parsed; this coarse guard stops a runaway upload from being materialized at all.

    The guard is Content-Length based, so a chunked body (which declares no length) would otherwise
    slip past unmeasured. Rather than count bytes off the ASGI stream, a chunked request is rejected
    outright (411): a label API has no streaming-upload use case, and our clients always send a
    Content-Length. An empty-body request that sends neither header is left alone.
    """
    if request.headers.get("transfer-encoding"):
        return Response(
            "Length Required: chunked request bodies are not accepted; send a Content-Length",
            status_code=411,
        )
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            return Response("Invalid Content-Length header", status_code=400)
        if length > MAX_REQUEST_BODY_BYTES:
            return Response(
                f"Request body too large: {length} bytes (max {MAX_REQUEST_BODY_BYTES})",
                status_code=413,
            )
    return await call_next(request)


_templates_dir = settings.templates_dir.resolve()
_web_dir = Path(__file__).parent / "web"
jinja = Jinja2Templates(directory=str(_web_dir))

# Serialize physical prints: /print and /reprint render + send off the event loop (see their
# routes), and a single printer must receive one job at a time, so concurrent requests queue here
# instead of interleaving raster sends.
_print_lock = asyncio.Lock()

registry = TemplateRegistry(_templates_dir)
translator = Translator(settings.translations_dir.resolve(), settings.default_language)
engine = RenderEngine(
    fonts_dir=settings.fonts_dir.resolve(),
    icons_dir=settings.icons_dir.resolve(),
    icon_collections_dir=settings.icon_collections_dir.resolve(),
    translator=translator,
    min_length_px=settings.min_length_px,
    max_length_px=settings.max_length_px,
    # Derive the high_res ENDLESS row ceiling from the configured model so wide-format printers
    # (QL-1100-class, ~35434 rows) are not silently clipped to the sub-1050 minimum (11811).
    # Falls back to the conservative global minimum for unknown identifiers.
    max_raster_rows=_brother_ql_model_max_rows(settings.model),
)

# Load templates at startup
_driver_cls = BrotherQLDriver.for_model(settings.model)
_driver = _driver_cls()


def _resolve_transport() -> type[Transport]:
    """Transport class for the configured printer_uri, resolved per call so a runtime-overridden
    URI (e.g. monkeypatched in tests) is always honoured."""
    return get_transport(infer_transport(settings.printer_uri))


# ── SNMP print preflight (close the phantom-success hole) ──────────────────────────────
# The QL-810W rasterises a job and only THEN rejects it at the hardware level when the loaded roll
# does not match the template's media (red blink, prints nothing) — yet its :9100 NIC never returns
# the status back-channel, so the send still records a 200. SNMP (UDP 161) is the channel that does
# answer, so before a real print we ask SNMP what is actually loaded and refuse a mismatch up front.


def _snmp_guard_applies() -> bool:
    """True when the SNMP media/fault preflight should run for a real print.

    Only the network transport has an SNMP agent to query, and only when SNMP is enabled. File/USB
    transports and a disabled-SNMP deployment skip the guard entirely (the latter is the documented
    opt-out for sites that cannot reach UDP 161)."""
    return settings.snmp_enabled and infer_transport(settings.printer_uri) == "network"


def _query_loaded_media() -> PrinterSNMPStatus:
    """Blocking SNMP query of the configured network printer's loaded media + fault state.

    Runs off the event loop (call via ``run_in_threadpool`` while holding ``_print_lock``, like the
    print itself). Never raises: an unreachable/undecodable agent yields ``reachable=False`` so the
    caller fails open. The SNMP host is the ``printer_uri`` hostname; the UDP 161 port/community/
    timeout come from settings (independent of the :9100 print port)."""
    host = urlparse(settings.printer_uri).hostname or ""
    return query_snmp_status(
        host,
        community=settings.snmp_community,
        port=settings.snmp_port,
        timeout=settings.snmp_timeout,
    )


def _describe_media(width_mm: float | None, media_type: str | None, length_mm: float | None) -> str:
    """Human-readable media description for a 409 detail, e.g. ``62mm continuous`` or ``62x29mm
    die-cut``. ``:g`` trims the trailing ``.0`` from whole-millimetre values."""
    if width_mm is None or media_type is None:
        return "unknown media"
    kind = "die-cut" if media_type == "die_cut" else media_type
    if media_type == "die_cut" and length_mm is not None:
        return f"{width_mm:g}x{length_mm:g}mm {kind}"
    return f"{width_mm:g}mm {kind}"


def _describe_required(required: RequiredMedia) -> str:
    return _describe_media(required.width_mm, required.media_type, required.length_mm)


def _raise_if_media_incompatible(label_id: str, loaded: PrinterSNMPStatus) -> None:
    """Reject a print up front on a hard printer fault or a loaded-vs-required media mismatch.

    Pure decision logic over an already-fetched :class:`PrinterSNMPStatus` (no I/O), so it is safe to
    call from the async handler and unit-testable without a socket. Fails open — returns without
    raising — when SNMP is unreachable or reports no comparable media (``MediaMatch.UNKNOWN``), or
    when the template's label is unknown to brother_ql (the downstream render surfaces that). Raises
    :class:`HTTPException` 409 otherwise, incrementing the matching ``label_errors_total`` series."""
    if not loaded.reachable:
        # Fail open by design: SNMP is opportunistic (it may be disabled on the printer or blocked
        # in transit), so an unreachable agent must not block printing. But a print then goes out
        # WITHOUT the media/fault check — so count it, making a silently-unguarded run observable
        # (a rising counter means the phantom-success class is unguarded until SNMP recovers).
        PREFLIGHT_SNMP_UNREACHABLE.inc()
        log.warning(
            "SNMP preflight: printer unreachable; allowing print without a media/fault check "
            "(fail-open). Fix SNMP reachability to re-enable the guard, or set SNMP_ENABLED=false."
        )
        return

    # Two fault gates, both rejecting before send so a fault is an explicit 409, never a phantom 200.
    #
    # (1) A non-zero hrPrinterDetectedErrorState — the RFC 3805 machine-readable fault signal (cover
    # open, no media, jam): the job would red-blink and print nothing. We gate on the bitmask, NOT on
    # console text: build_snmp_status flags any console line != "READY" as an error, but transient
    # non-fault display states (PRINTING / RECEIVING / COOLING) are also non-READY, and blocking on
    # them would 409 a valid back-to-back print whose predecessor is still processing.
    if loaded.error_state_bits != 0:
        LABEL_ERRORS.labels(reason="printer_error").inc()
        raise HTTPException(
            409,
            detail={
                "msg": "Printer reports a fault and cannot print; clear it and retry",
                "errors": loaded.errors,
                "media_loaded": _describe_media(
                    loaded.media_width_mm, loaded.media_type, loaded.media_length_mm
                ),
            },
        )

    # (2) A latched fault the bitmask MISSES. Verified live on the QL-810W (2026-06-30): sending a
    # die-cut template to a continuous roll red-blinks and *latches* the printer — every later job,
    # even a media-matching one, is buffered and silently returns 200 until a manual reset — yet
    # hrPrinterDetectedErrorState stays 00. It surfaces only as hrPrinterStatus=other(1) with a
    # non-READY console line. Gate on BOTH signals so we reject the latch without re-introducing the
    # transient-state false positives gate (1) avoids: idle reads idle(3)/"READY" and PRINTING/WARMUP
    # read printing(4)/warmup(5), so other(1) + a non-READY console uniquely identifies the latch.
    if (
        loaded.printer_status == HR_PRINTER_STATUS_OTHER
        and loaded.console_text is not None
        and loaded.console_text.strip().upper() != CONSOLE_READY
    ):
        LABEL_ERRORS.labels(reason="printer_error").inc()
        raise HTTPException(
            409,
            detail={
                "msg": "Printer reports a fault and cannot print; clear it and retry",
                "errors": loaded.errors,
                "media_loaded": _describe_media(
                    loaded.media_width_mm, loaded.media_type, loaded.media_length_mm
                ),
            },
        )

    try:
        required = required_media_for(label_id)
    except ValueError:
        # An unknown label can't be compared; the render path will surface it. Don't block here.
        log.warning("SNMP preflight: unknown label %r; skipping media check (fail-open)", label_id)
        return

    if media_matches(required, loaded) == MediaMatch.MISMATCH:
        LABEL_ERRORS.labels(reason="media_mismatch").inc()
        loaded_desc = (
            loaded.media_name
            if loaded.media_name
            else _describe_media(loaded.media_width_mm, loaded.media_type, loaded.media_length_mm)
        )
        raise HTTPException(
            409,
            detail={
                "msg": (
                    f"Loaded media ({loaded_desc}) does not match the media required by template "
                    f"label {label_id!r} ({_describe_required(required)}). Load the matching roll "
                    "or print a template that matches what is loaded."
                ),
                "label": label_id,
                "media_required": _describe_required(required),
                "media_loaded": loaded_desc,
            },
        )


async def _enforce_print_preflight(label_id: str, *, dry_run: bool) -> None:
    """Run the SNMP media/fault preflight for a real network print, if applicable.

    A no-op for dry runs (nothing reaches the printer), for non-network transports, and when SNMP is
    disabled. Otherwise queries SNMP off the event loop and raises 409 on a fault or media mismatch
    (fail-open on an unreachable agent). Call inside ``_print_lock`` so the query cannot race an
    in-flight print on the same transport."""
    if dry_run or not _snmp_guard_applies():
        return
    loaded = await run_in_threadpool(_query_loaded_media)
    # Refresh the SNMP telemetry gauges from this query (freshness model A) before the guard decision,
    # so a print that is about to be rejected on a fault still updates printer_up / error-state.
    _record_snmp_metrics(loaded)
    _raise_if_media_incompatible(label_id, loaded)


# Job history backend (idempotency de-dup + reprint substrate). Rebuilt in startup() so runtime
# env config is honoured and a file database lands under the created data dir.
_history: HistoryStore = build_history_store(settings)


@app.on_event("startup")
async def startup() -> None:
    global _history
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _history.close()  # release the import-time placeholder before swapping in the configured store
    _history = build_history_store(settings)
    log.info("History store: mode=%s", settings.history_mode)
    supported = _driver_cls.CAPABILITY.supported_labels
    if settings.label_size not in supported:
        raise RuntimeError(
            f"LABEL_SIZE {settings.label_size!r} is not supported by model "
            f"{settings.model!r}. Supported sizes: {supported}"
        )
    loaded = registry.load_all()
    log.info("Loaded %d templates: %s", len(loaded), loaded)
    langs = translator.load_all()
    if not translator.has(settings.default_language):
        raise RuntimeError(
            f"DEFAULT_LANGUAGE {settings.default_language!r} has no catalog in "
            f"{settings.translations_dir}. Available languages: {langs}"
        )
    log.info("Loaded %d translation catalogs: %s", len(langs), langs)
    _require_auth_or_optout()
    # Resolve the transport from PRINTER_URI now so an unsupported scheme fails at boot, not on the
    # first print. For the network transport, also construct it to validate host:port eagerly.
    if infer_transport(settings.printer_uri) == "network":
        _resolve_transport()(settings.printer_uri)


@app.on_event("shutdown")
async def shutdown() -> None:
    _history.close()


# ── Auth ────────────────────────────────────────────────────────────────────────
bearer = HTTPBearer(auto_error=False)


def _require_auth_or_optout() -> None:
    """Fail closed: an unauthenticated service must be an explicit, conscious choice.

    The protected endpoints (/print, /reprint, /reload, /preview) drive a physical printer,
    so a network-reachable default install must not be open by accident.
    """
    if settings.api_token is not None and not settings.api_token.strip():
        raise RuntimeError(
            "API_TOKEN is set but empty/blank. Provide a real secret, or unset it entirely and "
            "set ALLOW_UNAUTHENTICATED=true to run without authentication."
        )
    if not settings.api_token and not settings.allow_unauthenticated:
        raise RuntimeError(
            "No API_TOKEN configured. Set API_TOKEN to require authentication, or set "
            "ALLOW_UNAUTHENTICATED=true to explicitly run without auth "
            "(intranet/trusted networks only)."
        )
    if not settings.api_token:
        log.warning(
            "Running WITHOUT authentication (ALLOW_UNAUTHENTICATED=true): any host that can "
            "reach this service can print and control the printer. Trusted intranet use only."
        )


def check_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    if not settings.api_token:
        return
    if creds is None or creds.credentials != settings.api_token:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_geometry(label_id: str) -> tuple[int, int | None]:
    """Return (width_px, height_px) for the label id; height_px=None for continuous."""
    geo = _driver_cls.CAPABILITY.label_geometries.get(label_id)
    if geo is None:
        raise HTTPException(400, f"Label {label_id!r} not supported by this printer model")
    return geo.width_px, geo.height_px


def _render_template_preview(
    tmpl: Template,
    fields: dict[str, Any],
    language: str,
    now: datetime | None = None,
) -> bytes:
    """Render a resolved :class:`Template` to a preview PNG.

    The single render path shared by the saved-template preview (:func:`_render_preview`) and the
    draft studio preview (``/preview/draft``): a pre-driver monochrome render at the media's
    printable width with the template's ``rotate`` applied in PIL for display parity. Because both
    callers route through here with the same arguments, a draft built from the same YAML + fields
    renders byte-identically to its saved equivalent — no dither/red/high_res divergence, matching
    ``/preview`` exactly.
    """
    width_px, height_px = _get_geometry(tmpl.label)
    return engine.render_to_png(
        tmpl.layout, fields, width_px, height_px, tmpl.rotate, language, now=now
    )


def _render_preview(
    template_name: str,
    fields: dict[str, Any],
    language: str,
    now: datetime | None = None,
) -> bytes:
    tmpl = registry.get(template_name)
    if tmpl is None:
        raise HTTPException(404, f"Template {template_name!r} not found")
    return _render_template_preview(tmpl, fields, language, now=now)


def _try_save_job(record: PrintJobRecord) -> bool:
    """Persist a history record, swallowing (and logging) I/O errors. Returns success.

    Used on paths where the physical print outcome is already decided: an audit-write failure
    must not change the HTTP result, since the label state on the printer is the source of truth.
    """
    try:
        _history.save(record)
        return True
    except (OSError, sqlite3.Error):
        log.exception("Failed to persist history record for job %s", record.job_id)
        return False


def _load_job(job_id: str) -> PrintJobRecord | None:
    return _history.get(job_id)


def _require_history_ui() -> None:
    """Guard the browse endpoints behind ``settings.history_ui``.

    When the browse UI is off, the page and its list/delete routes must behave as if they do not
    exist — a bare 404 with no history-specific detail, so the response is indistinguishable from
    an unrouted path and never reveals that history is merely hidden. ``/reprint`` deliberately
    does not call this: reprint-by-id stays available regardless of browse visibility.
    """
    if not settings.history_ui:
        raise HTTPException(404)  # generic "Not Found"; no detail that discloses the hidden UI


def _require_editor_enabled() -> None:
    """Guard the template studio endpoints behind ``settings.editor_enabled``.

    When the studio is off, the editor page and its draft-preview/parse/save routes must behave as
    if they do not exist — a bare 404 with no editor-specific detail. Listed *before* ``check_token``
    on every studio route so the visibility gate wins: with EDITOR_ENABLED=false they 404 rather than
    401 (which would reveal a hidden-but-present endpoint), mirroring ``_require_history_ui``.
    """
    if not settings.editor_enabled:
        raise HTTPException(404)  # generic "Not Found"; no detail that discloses the hidden studio


def _require_templates_loadable() -> None:
    """Guard the load-existing-template route behind ``settings.templates_loadable``.

    A sub-gate under the editor: even with the studio on, an operator may disable loading existing
    template sources. Listed *before* ``check_token`` (after ``_require_editor_enabled``) so a 404
    hides the route entirely when the feature is off, rather than a 401 disclosing it exists.
    """
    if not settings.templates_loadable:
        raise HTTPException(404)  # generic "Not Found"; no detail that discloses the hidden route


def _find_idempotent_job(key: str) -> PrintJobRecord | None:
    """Return the most recent non-failed job recorded under ``key`` (for retry de-duplication).

    Failed jobs are ignored so a retry after a genuine failure still prints. Matching against
    completed records is sufficient because the caller looks this up while holding ``_print_lock``:
    an in-flight original holds the lock until its record is persisted, so a racing retry can only
    reach this check *after* that record exists.
    """
    return _history.find_idempotent(key)


def _request_fingerprint(request: PrintRequest, options: RenderOptions) -> str:
    """Hash the print-relevant fields of a request, so a reused idempotency key can be checked.

    Two requests are "the same print" only if every field that affects the output or its
    delivery matches — notably ``dry_run`` (a dry-run keyed job must not satisfy a later real
    print) and the rasterization ``options`` (different rasterization is a different print).
    ``idempotency_key`` itself is excluded: it is the lookup, not part of the identity.

    ``options`` is the resolved RenderOptions (env defaults already applied), hashed *wholesale*
    via ``model_dump()`` rather than hand-listing each option — so any option added to
    RenderOptions is folded into the fingerprint automatically and can never be silently forgotten
    (a key reused with a different effective option would otherwise collide and wrongly dedupe). A
    ``null`` request option and an explicit one that resolve to the same effective value fingerprint
    identically (they produce the same output).

    ``sequence`` is included wholesale via ``model_dump()`` so two requests differing only in
    their sequence spec (different start/count/step/padding) get distinct fingerprints and do not
    collide on idempotency. ``copies`` is included for the plain-copies path; for sequence batches
    ``copies`` is always 1 (enforced by the model validator) so it does not affect the sequence
    fingerprint.
    """
    payload = {
        "template": request.template,
        "fields": request.fields,
        "copies": request.copies,
        "dry_run": request.dry_run,
        "cut": request.cut,
        "options": options.model_dump(),
        "language": request.language,
        "sequence": request.sequence.model_dump() if request.sequence is not None else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _execute_print(
    tmpl: Template,
    fields: dict[str, Any],
    *,
    copies: int,
    dry_run: bool,
    cut: bool,
    options: RenderOptions,
    language: str,
    now: datetime,
    job_id: str,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    sequence: SequenceSpec | None = None,
) -> PrintResponse:
    """Render with frozen inputs, persist the job, and (unless dry-run) send to the printer.

    Shared by /print and /reprint; the only difference is the origin of ``now`` — a live print
    uses the current instant, a reprint replays the frozen original so computed ``{{date}}``
    tokens reproduce exactly.

    Rotation is applied by the driver, not here: the raster is rendered unrotated at the media's
    printable width (``rotate=0``) and ``tmpl.rotate`` is forwarded to ``render_payload``. brother_ql
    needs the image at the roll's printable width to rasterize continuous labels correctly; the
    ``/preview`` path rotates in PIL purely for display parity.

    Sequence batches: when ``sequence`` is not None, the batch is sent ONE LABEL AT A TIME.
    Each item's ``{{seq}}`` is resolved per item, then that single label is rendered → converted →
    sent → its returned ``PrinterStatus`` is inspected, before the next item is rendered. Because a
    single small label completes well within the transport's status-read window, status readback is
    meaningful per label: an explicit ``status.ok is False`` stops the batch at that label and the
    job is recorded ``failed`` with the partial printed count; a ``None`` status (state unknown —
    USB / silent back-channel) means state is unknown and does NOT fail (proceed to the next label).
    Peak decoded-image memory is a single label regardless of ``count`` (the prior label falls out
    of scope before the next is rendered). ``labels_printed_total`` advances by the number of labels
    ACTUALLY sent (the partial count on failure, ``count`` on success). The whole loop runs under
    ``_print_lock`` (held by the caller) so the N printer jobs are one uninterleaved logical batch.
    One history row is recorded per batch, carrying the frozen ``sequence`` spec so /reprint replays
    the whole batch the same per-label way. ``copies`` is 1 in this path (model validator).
    """
    width_px, height_px = _get_geometry(tmpl.label)
    effective_high_res = bool(options.high_res) if options.high_res is not None else False
    effective_red = bool(options.red) if options.red is not None else False
    effective_threshold = float(
        options.threshold if options.threshold is not None else settings.default_threshold
    )

    # Persist the job without any image blob (rendering below uses the full fields). Built before
    # any render/send so the failure paths can record a uniform record.
    persisted_fields, image_stripped = _strip_image_fields(tmpl, fields)

    def _record(status: str) -> PrintJobRecord:
        return PrintJobRecord(
            job_id=job_id,
            template=tmpl.name,
            fields=persisted_fields,
            copies=copies,
            dry_run=dry_run,
            timestamp=datetime.utcnow().isoformat(),
            language=language,
            cut=cut,
            options=options,
            render_now=now.isoformat(),
            status=status,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            image_stripped=image_stripped,
            sequence=sequence,
        )

    # Render a single label PNG. ``seq`` is the pre-formatted per-item string ("" for the
    # non-sequence path). Kept distinct from conversion+send so a render failure stays a
    # ``render_error`` (no failed record) while a driver/transport failure is a ``print_error``
    # (recorded failed), exactly as the pre-sequence split did.
    def _render_png_for(seq: str) -> bytes:
        return engine.render_to_png(
            tmpl.layout,
            fields,
            width_px,
            height_px,
            rotate=0,
            language=language,
            now=now,
            high_res=effective_high_res,
            red=effective_red,
            seq=seq,
        )

    # Convert a rendered PNG to QL raster bytes. copies=1 for the sequence path (one printer job per
    # label); the non-sequence path keeps the request's copies so identical labels print
    # back-to-back via the driver's multiply path.
    def _convert(png: bytes, label_copies: int) -> bytes:
        driver_opts: dict[str, Any] = {
            "model": settings.model,
            "label": tmpl.label,
            "rotate": tmpl.rotate,  # driver rotates the printable-width raster for the hardware
            # Each sequence label is its own printer job, so cut applies per label exactly as for a
            # single print: die-cut media yields N identical pieces; continuous tape feeds/cuts at
            # the end of each label when cut is True (one extra feed/cut per label vs one batch cut).
            "cut": cut,
            "copies": label_copies,
            "dither": bool(options.dither),
            "threshold": effective_threshold,
            "high_res": effective_high_res,
            "red": effective_red,
        }
        return _driver.render_payload(png, driver_opts)

    def _send(payload: bytes) -> PrinterStatus | None:
        transport_cls = _resolve_transport()
        transport = transport_cls(settings.printer_uri)
        try:
            return transport.send(payload)
        finally:
            transport.close()

    # ── dry-run: render for validation, never send ──────────────────────────────
    if dry_run:
        try:
            if sequence is not None:
                # Pull the lazy generator to completion so every item is actually rendered
                # (surfacing any per-item render error), discarding each PNG immediately so a large
                # count cannot buffer the whole batch / OOM.
                for _ in engine.render_sequence(
                    tmpl.layout,
                    fields,
                    width_px,
                    height_px,
                    start=sequence.start,
                    count=sequence.count,
                    step=sequence.step,
                    padding=sequence.padding,
                    rotate=0,
                    language=language,
                    now=now,
                    high_res=effective_high_res,
                    red=effective_red,
                ):
                    pass
            else:
                engine.render_to_png(
                    tmpl.layout,
                    fields,
                    width_px,
                    height_px,
                    rotate=0,
                    language=language,
                    now=now,
                    high_res=effective_high_res,
                    red=effective_red,
                )
        except Exception as exc:
            LABEL_ERRORS.labels(reason="render_error").inc()
            log.exception("Render error for template %s", tmpl.name)
            raise HTTPException(500, f"Render error: {exc}") from exc

        if not _try_save_job(_record("dry-run")):
            log.error("Dry-run job %s could not be recorded to history", job_id)
        effective_count = sequence.count if sequence is not None else copies
        LABELS_PRINTED.labels(template=tmpl.name, dry_run="True").inc(effective_count)
        LAST_PRINT_TS.set_to_current_time()
        return PrintResponse(job_id=job_id, template=tmpl.name, copies=copies, dry_run=dry_run)

    # ── sequence: render → send → confirm, one label at a time ──────────────────
    if sequence is not None:
        printed = 0  # labels ACTUALLY sent (the partial count on a mid-batch failure)
        for i in range(sequence.count):
            seq_str = format_seq(sequence.start, i, sequence.step, sequence.padding)

            # ── render (try 1 of 2): classified as render_error, never print_error ──
            # Mirrors the plain-copies path: a template/render failure means the printer
            # was never involved, so it is a render_error (not a print_error). When no
            # labels have been sent yet (printed == 0) the job is NOT recorded as a
            # failed print — same behaviour as the plain path, which raises without
            # saving a failed row. When labels were already sent (printed > 0) physical
            # output happened; record the partial result as failed (render fault) so the
            # job is visible in history, then surface the render error.
            try:
                png = _render_png_for(seq_str)
            except Exception as exc:
                LABEL_ERRORS.labels(reason="render_error").inc()
                log.exception("Render error for template %s", tmpl.name)
                if printed == 0:
                    # No physical output yet — behave exactly like the plain render-error
                    # path: do NOT record a failed print row.
                    raise HTTPException(500, f"Render error: {exc}") from exc
                # Some labels already printed — record the partial failure so the job is
                # visible (with the partial count and accurate render-fault reason).
                _try_save_job(_record("failed"))
                LABELS_PRINTED.labels(template=tmpl.name, dry_run="False").inc(printed)
                LAST_PRINT_TS.set_to_current_time()
                raise HTTPException(500, f"Render error: {exc}") from exc

            # ── convert + send (try 2 of 2): classified as print_error ──────────────
            try:
                payload = _convert(png, label_copies=1)
                status = _send(payload)
            except BrotherQLUnsupportedCmd as exc:
                LABEL_ERRORS.labels(reason="unsupported_two_color").inc()
                log.warning(
                    "Two-color print unsupported for job %s at label %d/%d: %s",
                    job_id,
                    i + 1,
                    sequence.count,
                    exc,
                )
                _try_save_job(_record("failed"))
                if printed:
                    LABELS_PRINTED.labels(template=tmpl.name, dry_run="False").inc(printed)
                    LAST_PRINT_TS.set_to_current_time()
                raise HTTPException(422, f"Two-color (red) printing not supported: {exc}") from exc
            except Exception as exc:
                LABEL_ERRORS.labels(reason="print_error").inc()
                log.exception(
                    "Print error for job %s; printed %d/%d before failing",
                    job_id,
                    printed,
                    sequence.count,
                )
                _try_save_job(_record("failed"))
                if printed:
                    LABELS_PRINTED.labels(template=tmpl.name, dry_run="False").inc(printed)
                    LAST_PRINT_TS.set_to_current_time()
                raise HTTPException(500, f"Print error: {exc}") from exc

            # Per-label status readback: a single small label finishes inside the status-read
            # window, so an explicit not-ok status is a real, attributable error — stop the
            # batch here. A None status (state unknown: USB / silent back-channel) means state
            # is unknown and does NOT fail; proceed to the next label.
            if status is not None and not status.ok:
                LABEL_ERRORS.labels(reason="printer_error").inc()
                detail = "; ".join(status.errors) or "printer reported an error"
                log.error(
                    "Printer reported errors for job %s at label %d/%d (seq=%s): %s; "
                    "printed %d/%d before stopping",
                    job_id,
                    i + 1,
                    sequence.count,
                    seq_str,
                    detail,
                    printed,
                    sequence.count,
                )
                _try_save_job(_record("failed"))
                # Count only the labels actually sent before the failing one.
                if printed:
                    LABELS_PRINTED.labels(template=tmpl.name, dry_run="False").inc(printed)
                    LAST_PRINT_TS.set_to_current_time()
                raise HTTPException(502, f"Printer error: {detail}")
            printed += 1

        # Whole batch sent without an explicit error. Record printed; count exactly what was sent.
        if not _try_save_job(_record("printed")):
            log.error(
                "Job %s printed but its history record was lost; /reprint will not find it", job_id
            )
        LABELS_PRINTED.labels(template=tmpl.name, dry_run="False").inc(printed)
        LAST_PRINT_TS.set_to_current_time()
        return PrintResponse(job_id=job_id, template=tmpl.name, copies=copies, dry_run=dry_run)

    # ── plain copies (no sequence): unchanged single render+send ────────────────
    try:
        png = _render_png_for("")
    except Exception as exc:
        LABEL_ERRORS.labels(reason="render_error").inc()
        log.exception("Render error for template %s", tmpl.name)
        raise HTTPException(500, f"Render error: {exc}") from exc

    try:
        payload = _convert(png, label_copies=copies)
        status = _send(payload)
    except BrotherQLUnsupportedCmd as exc:
        # red=True on a model brother_ql does not consider two-color (e.g. a stale reprint of a
        # record whose model later changed). The /print gate normally prevents this; map it to a
        # clean 422 rather than a 500 on the paths that bypass the gate (reprint).
        LABEL_ERRORS.labels(reason="unsupported_two_color").inc()
        log.warning("Two-color print unsupported for job %s: %s", job_id, exc)
        _try_save_job(_record("failed"))
        raise HTTPException(422, f"Two-color (red) printing not supported: {exc}") from exc
    except Exception as exc:
        LABEL_ERRORS.labels(reason="print_error").inc()
        log.exception("Print error for job %s", job_id)
        _try_save_job(_record("failed"))
        raise HTTPException(500, f"Print error: {exc}") from exc

    # The bytes were sent without raising, but a networked printer may still report an error
    # (out of media, cover open, loaded media ≠ requested label) in its status packet. When a
    # transport surfaces that, fail the job and emit the error metric instead of recording a
    # phantom print. A None status means the transport could not read state back (USB, or a
    # silent network back-channel) — treated as "no error reported", so the happy path stands.
    if status is not None and not status.ok:
        LABEL_ERRORS.labels(reason="printer_error").inc()
        detail = "; ".join(status.errors) or "printer reported an error"
        log.error("Printer reported errors for job %s: %s", job_id, detail)
        _try_save_job(_record("failed"))
        raise HTTPException(502, f"Printer error: {detail}")

    # The label is now physically printed. The history append is best-effort: a successful send must
    # not be reported as a failure just because the audit write failed, or a client retry would
    # print a duplicate. A lost record is logged loudly.
    if not _try_save_job(_record("printed")):
        log.error(
            "Job %s printed but its history record was lost; /reprint will not find it", job_id
        )
    LABELS_PRINTED.labels(template=tmpl.name, dry_run="False").inc(copies)
    LAST_PRINT_TS.set_to_current_time()
    return PrintResponse(job_id=job_id, template=tmpl.name, copies=copies, dry_run=dry_run)


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/health", response_model=HealthResponse, tags=["System"])
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        driver=settings.driver,
        model=settings.model,
        transport=infer_transport(settings.printer_uri),
        uri=settings.printer_uri,
        label_size=settings.label_size,
        template_count=len(registry),
        default_language=settings.default_language,
        languages=translator.available(),
    )


@app.get("/livez", response_model=LivenessResponse, tags=["System"])
def livez() -> LivenessResponse:
    """Kubernetes liveness probe: the process is up. Always 200, no dependencies, unauthenticated.

    A liveness failure tells the orchestrator to RESTART the pod, so it must never depend on an
    external resource (printer, history store, templates) — only that the event loop can answer.
    Cheap and side-effect free.
    """
    return LivenessResponse(status="alive")


@app.get(
    "/readyz",
    response_model=ReadinessResponse,
    tags=["System"],
    responses={503: {"description": "Not ready to serve (see per-check reasons)"}},
)
def readyz() -> ReadinessResponse | JSONResponse:
    """Kubernetes readiness probe: can the app serve print requests? 200 ready / 503 not-ready.

    Checks the dependencies a print actually needs — at least one template loaded, a resolvable
    transport scheme, and an open history store — and reports each in ``checks`` so a not-ready
    response says WHY. Deliberately does NOT probe the printer: a print service should keep accepting
    requests while the printer is briefly unreachable (that live state is /printer/status), and a
    printer-coupled readiness would flap the pod out of its Service on a transient blip. Unauthenticated
    and exposes no sensitive data (probes carry no token).
    """
    checks: dict[str, str] = {}

    checks["templates"] = "ok" if len(registry) else "no templates loaded"

    # An unknown/unregistered PRINTER_URI scheme means the app cannot route any print — not ready.
    try:
        get_transport(infer_transport(settings.printer_uri))
        checks["transport"] = "ok"
    except (ValueError, KeyError) as exc:
        checks["transport"] = f"unresolved transport for {settings.printer_uri!r}: {exc}"

    # A cheap probe that the history store is open (a closed/broken store raises). The broad catch is
    # deliberate here and ONLY here: a readiness probe must turn ANY dependency failure into a
    # structured 503, never let it surface as a 500 — so it cannot assume a specific store backend.
    try:
        _history.count()
        checks["history"] = "ok"
    except Exception as exc:
        checks["history"] = f"history store unavailable: {exc}"

    ready = all(v == "ok" for v in checks.values())
    body = ReadinessResponse(ready=ready, checks=checks)
    if not ready:
        return JSONResponse(status_code=503, content=body.model_dump())
    return body


@app.get("/capabilities", response_model=CapabilityResponse, tags=["System"])
def capabilities() -> CapabilityResponse:
    cap = _driver_cls.CAPABILITY
    return CapabilityResponse(
        driver=cap.name,
        model=settings.model,
        dpi=cap.dpi,
        cut=cap.cut,
        two_color=cap.two_color,
        supported_labels=cap.supported_labels,
        red_labels=cap.red_labels,
        label_geometries=cap.label_geometries,
    )


def _build_status_request() -> bytes:
    """Model-correct ESC i S status-request payload: a model-sized invalidate (NUL) prefix before
    the status-information command. Without the prefix, a printer whose command buffer is dirty after
    an interrupted job treats ESC i S as raster data and never replies with the 32-byte status frame.
    Consumed only on the ESC i S fallback path; the SNMP status read ignores it (see NetworkTransport
    .query_status), but it is cheap and the transport API takes a request either way."""
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(settings.model)
    qlr.add_invalidate()
    qlr.add_status_information()
    return bytes(qlr.data)  # brother_ql is untyped; coerce the buffer to a concrete bytes


def _query_printer_status(request: bytes) -> PrinterStatus:
    """Blocking transport status query. Opens the configured transport, queries, and always closes it.
    Run via ``run_in_threadpool`` so the socket I/O never blocks the event loop."""
    transport = _resolve_transport()(settings.printer_uri)
    try:
        return transport.query_status(request)
    finally:
        transport.close()


def _status_has_hard_fault(status: PrinterStatus) -> bool:
    """True when a reachable SNMP status reflects a genuine hardware fault, mirroring the print
    preflight's two gates (see :func:`_raise_if_media_incompatible`) so the status badge and the print
    gate agree on what a "fault" is: (1) a non-zero ``hrPrinterDetectedErrorState`` bitmask, or (2) the
    latch — ``hrPrinterStatus=other(1)`` with a non-``READY`` console. Deliberately does NOT treat a
    non-READY console alone as a fault: transient display states (PRINTING / RECEIVING / COOLING) are
    non-READY but normal, so keying off ``status.errors`` (which echoes the console line) would
    false-alarm mid-print. The error bitmask and hrPrinterStatus ride in ``raw`` (see from_snmp)."""
    if (status.raw.get("error_state_bits") or 0) != 0:
        return True
    console = status.console_text
    return (
        status.raw.get("printer_status") == HR_PRINTER_STATUS_OTHER
        and console is not None
        and console.strip().upper() != CONSOLE_READY
    )


def _status_is_busy(status: PrinterStatus) -> bool:
    """True when the SNMP read itself reports a working state — hrPrinterStatus printing(4) or
    warmup(5). Independent of this server's _print_lock so an external job, or a printer still
    finishing after our send returns, is reported as PRINTING rather than misreported as idle."""
    return status.raw.get("printer_status") in HR_PRINTER_STATUS_BUSY


def _status_response(status: PrinterStatus, state: PrinterState) -> PrinterStatusResponse:
    """Build the full PrinterStatusResponse from a reachable printer query under a given ``state``.
    Single builder so the PRINTING, IDLE, and ERROR responses can never drift field-by-field."""
    return PrinterStatusResponse(
        state=state,
        uri=settings.printer_uri,
        reachable=status.reachable,
        model=status.model,
        media_width_mm=status.media_width_mm,
        media_length_mm=status.media_length_mm,
        media_type=status.media_type,
        status=status.status_type,
        phase=status.phase_type,
        errors=status.errors,
        serial=status.serial,
        firmware=status.firmware,
        console_text=status.console_text,
        label_lifecount=status.label_lifecount,
    )


def _busy_503() -> JSONResponse:
    """The 503 "printer is busy" reply for the ESC i S path, where a status readback shares the :9100
    socket with an in-flight print and cannot run concurrently."""
    return JSONResponse(
        status_code=503,
        content=PrinterStatusResponse(
            state=PrinterState.PRINTING,
            uri=settings.printer_uri,
            reachable=False,
            errors=["printer is busy with an in-progress print job; retry shortly"],
        ).model_dump(),
    )


def _unreachable_503(status: PrinterStatus) -> JSONResponse:
    """The 503 reply for a reachable-transport-but-no-printer query (state=off, reachable=false)."""
    return JSONResponse(
        status_code=503,
        content=PrinterStatusResponse(
            state=PrinterState.OFF,
            uri=settings.printer_uri,
            reachable=False,
            errors=status.errors,
        ).model_dump(),
    )


@app.get(
    "/printer/status",
    response_model=PrinterStatusResponse,
    tags=["System"],
    responses={
        401: {"description": "Invalid or missing API token"},
        503: {"description": "Printer unreachable, busy, or status query not supported"},
    },
)
async def printer_status(
    _auth: Annotated[None, Depends(check_token)],
) -> PrinterStatusResponse | JSONResponse:
    """Query the physical printer and return its current state.

    On the default network path with SNMP enabled the status comes over SNMP (UDP 161); otherwise it
    is read via the ESC i S back-channel over the configured transport. Returns loaded media (width,
    length, type), the printer model, identity/telemetry, and any reported error bits.

    Returns 503 when the printer is unreachable, or (ESC i S path only) when a print is in progress
    and the :9100 status readback cannot run. The response body always has ``reachable: false`` in
    those cases so callers can branch without inspecting the status code.

    Note: the optional background keep-alive ping was intentionally omitted;
    it adds a background task and concurrency surface not warranted for a home app.
    """
    # SNMP is an independent, read-only channel (UDP 161): a status query on it does NOT contend with
    # an in-flight print's :9100 raster send, so it must NOT serialize behind _print_lock. Decoupling
    # lets the status card poll live during a print. While the lock is held a print is mid-job, which
    # we surface as PRINTING — but only when the read is itself clean. The ESC i S fallback below DOES
    # share the :9100 socket, so it keeps the busy short-circuit and the lock.
    if _snmp_guard_applies():
        status = await run_in_threadpool(_query_printer_status, _build_status_request())
        # Peek (never acquire) the print lock as a read-only "is a print in progress" signal. A brief
        # stale read only mislabels a single poll cycle, harmless for a status card.
        print_in_flight = _print_lock.locked()
        # Refresh the SNMP telemetry gauges from this query (freshness model A).
        _record_status_metrics(status)
        # Precedence is deliberate: an unreachable read or a genuine HARD fault must surface EVEN
        # mid-print — masking either behind "printing" is the phantom-success failure mode this feature
        # exists to close. But a non-READY console alone is NOT a fault (PRINTING/RECEIVING/COOLING are
        # transient), so the fault test reuses the preflight's hard-fault gates rather than the raw
        # errors list — otherwise a normal mid-print poll would false-alarm as error. Only a reachable,
        # hard-fault-free read under a held lock reports live PRINTING.
        if not status.reachable:
            return _unreachable_503(status)
        if _status_has_hard_fault(status):
            return _status_response(status, PrinterState.ERROR)
        # Busy if the SNMP read itself says so (printing/warmup) OR this server holds the print lock
        # (covers the window before the printer's hrPrinterStatus catches up, and a read that can't
        # report status). Only a reachable, fault-free, non-busy read is IDLE (ready).
        if _status_is_busy(status) or print_in_flight:
            return _status_response(status, PrinterState.PRINTING)
        return _status_response(status, PrinterState.IDLE)

    # ── ESC i S fallback (file / USB / SNMP disabled): the status readback shares the :9100 socket
    #    with printing, so it MUST serialize behind _print_lock. Non-blocking acquire — if a print is
    #    in progress return 503 "busy" immediately rather than hanging the request thread.
    if _print_lock.locked():
        return _busy_503()
    async with _print_lock:
        status = await run_in_threadpool(_query_printer_status, _build_status_request())

    if not status.reachable:
        return _unreachable_503(status)
    return _status_response(status, PrinterState.ERROR if status.errors else PrinterState.IDLE)


def _template_media(label: str) -> TemplateMedia | None:
    """The media a template's ``label`` requires, as the UI-facing model — ``None`` when the label is
    not a known brother_ql label (the template still lists and prints; it just gets no compatibility
    badge). Reuses :func:`app.media.required_media_for` so the badge can never drift from the
    server-side print guard's comparison."""
    try:
        required = required_media_for(label)
    except ValueError:
        return None
    return TemplateMedia(
        width_mm=required.width_mm,
        media_type=required.media_type,
        length_mm=required.length_mm,
    )


@app.get("/templates", response_model=list[TemplateInfo], tags=["Templates"])
def list_templates() -> list[TemplateInfo]:
    return [
        TemplateInfo(
            name=t.name,
            description=t.description,
            label=t.label,
            rotate=t.rotate,
            fields=TemplateFieldContract(
                required=t.required_fields,
                optional=t.optional_fields,
            ),
            media=_template_media(t.label),
        )
        for t in registry.all()
    ]


# The source route lists ``_require_editor_enabled`` and ``_require_templates_loadable`` *before*
# ``check_token`` so the visibility gates win (404 when the feature is off, 401 only when it is on but
# the request is unauthenticated), matching the studio precedent on /preview/draft etc.
@app.get(
    "/templates/{name}/source",
    response_model=TemplateSourceResponse,
    dependencies=[
        Depends(_require_editor_enabled),
        Depends(_require_templates_loadable),
        Depends(check_token),
    ],
    responses=_SOURCE_RESPONSES,
    tags=["Templates"],
)
def get_template_source(name: str) -> TemplateSourceResponse:
    """Return an existing template's verbatim YAML for loading into the template studio editor.

    Security: ``name`` is NEVER treated as a filesystem path. It is looked up in the in-memory
    registry (a dict keyed by internal template name), which only ever holds validated ``*.yaml``
    files loaded from ``templates_dir`` — so ``../../etc/passwd`` is simply not a key (→ 404) and no
    path-traversal or unrelated-file read is possible. A defence-in-depth check then confirms the
    resolved source is a real, non-symlink ``.yaml`` directly under the resolved templates dir before
    reading (symlinks are already rejected at load time, so this is belt-and-suspenders).
    """
    tmpl = registry.get(name)
    if tmpl is None:
        raise HTTPException(404, f"No template named {name!r}")

    real_dir = settings.templates_dir.resolve()
    real_src = tmpl.source_path.resolve()
    if (
        tmpl.source_path.is_symlink()
        or real_src.parent != real_dir
        or tmpl.source_path.suffix != ".yaml"
    ):
        # A registered template whose file is not a plain .yaml directly under templates_dir should
        # be unreachable (load_all rejects symlinks), but never serve it if the invariant is violated.
        raise HTTPException(404, f"No template named {name!r}")

    try:
        if real_src.stat().st_size > MAX_TEMPLATE_SOURCE_BYTES:
            raise HTTPException(
                413,
                f"Template {name!r} is too large to load "
                f"({real_src.stat().st_size} bytes, max {MAX_TEMPLATE_SOURCE_BYTES})",
            )
        yaml_text = tmpl.source_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        # TOCTOU: the file was deleted/replaced after the registry loaded it. It no longer exists.
        raise HTTPException(404, f"Template {name!r} file is no longer present") from exc
    except OSError as exc:
        log.exception("Failed to read template source %s", tmpl.source_path)
        raise HTTPException(500, "Failed to read template source") from exc

    return TemplateSourceResponse(name=tmpl.name, yaml=yaml_text)


@app.post(
    "/preview",
    dependencies=[Depends(check_token)],
    tags=["Printing"],
    responses={
        **RESPONSE_401,
        400: {"description": "Label not supported by the configured printer model"},
        **RESPONSE_413,
    },
)
async def preview(request: PrintRequest, download: bool = False) -> Response:
    tmpl = _resolve_template(request.template, request.fields)

    # A preview request carries no sequence object, so a {{seq}} template would resolve {{seq}} to
    # "" and render a blank-numbered raster a user could approve. Reject it (see the helper) rather
    # than let the user OK a label that prints differently than it previews.
    # preview=True skips the reciprocal check: a non-seq template with no sequence is fine.
    _validate_sequence_matches_template(tmpl, has_sequence=False, preview=True)
    _validate_image_fields(tmpl, request.fields)
    _validate_text_fields(tmpl, request.fields)
    language = request.language or settings.default_language
    png = _render_preview(tmpl.name, request.fields, language)
    headers = (
        {"Content-Disposition": f'attachment; filename="{tmpl.name}.png"'} if download else None
    )
    return Response(content=png, media_type="image/png", headers=headers)


@app.post(
    "/print",
    response_model=PrintResponse,
    dependencies=[Depends(check_token)],
    tags=["Printing"],
    responses={**RESPONSE_401, **RESPONSE_413},
)
async def print_label(request: PrintRequest) -> PrintResponse:
    # Resolve each nullable option against its env default: None inherits the default, an explicit
    # true/false overrides it either way. Resolved here so the concrete options feed the idempotency
    # fingerprint (a key reused with different effective options is a different print, not a retry)
    # and are frozen verbatim into history for an exact /reprint.
    effective_dither = (
        settings.default_dither if request.options.dither is None else request.options.dither
    )
    effective_threshold = (
        settings.default_threshold
        if request.options.threshold is None
        else request.options.threshold
    )
    effective_high_res = (
        settings.default_high_res if request.options.high_res is None else request.options.high_res
    )
    effective_red = settings.default_red if request.options.red is None else request.options.red
    # Under two-color (red=True), brother_ql's convert() uses fixed HSV filters to separate layers
    # and does NOT apply Floyd-Steinberg dithering. Canonicalize dither to False so the fingerprint
    # is honest and history is truthful (red prints differing only in the no-op dither dedupe correctly).
    # threshold IS applied under red (convert() runs a point() threshold on both the red and black
    # layers), so it must NOT be canonicalized — callers can request a non-default threshold and it
    # materially changes output. Apply red→dither canonicalization BEFORE the dither→threshold
    # rule so the latter only fires on the non-red dither path.
    if effective_red:
        # dither inert under two-color (HSV separation, no Floyd-Steinberg)
        effective_dither = False
    # threshold is a no-op under Floyd-Steinberg dither in brother_ql (the driver ignores it in the
    # dither branch), so collapse it to the canonical default when dither is on. This keeps the
    # idempotency fingerprint honest (identical physical output → identical fingerprint) and avoids
    # spurious 409s when callers vary a threshold that has no effect. Reprint reproducibility is
    # unaffected: the driver ignores threshold in dither mode regardless of what is stored.
    # NOTE: this fires only when effective_dither is True; red→dither above ensures that under
    # red=True, effective_dither is always False, so this block does NOT fire for red prints —
    # threshold is honored under red (it IS applied by convert()).
    if effective_dither:
        effective_threshold = settings.default_threshold
    resolved_options = RenderOptions(
        dither=effective_dither,
        threshold=effective_threshold,
        high_res=effective_high_res,
        red=effective_red,
    )
    fingerprint = (
        _request_fingerprint(request, resolved_options) if request.idempotency_key else None
    )

    try:
        tmpl = _resolve_template(request.template, request.fields)
    except HTTPException as exc:
        LABEL_ERRORS.labels(
            reason="not_found" if exc.status_code == 404 else "missing_fields"
        ).inc()
        raise

    # Two-color capability gate: reject a red print up front when the configured model lacks
    # two-color support, so brother_ql's BrotherQLUnsupportedCmd never surfaces as a 500. The check
    # is on resolved `red` (an inherited DEFAULT_RED counts), and is keyed off the model only — a
    # red-vs-plain *media* mismatch is the printer's call (we cannot know what is physically loaded
    # without a status read), so we gate on what we can prove statically. The template's `label`
    # still binds the geometry; a model with no red media at all (red_labels empty) is also rejected
    # since the print could never come out two-color.
    if effective_red:
        _validate_two_color_supported(tmpl)

    _validate_image_fields(tmpl, request.fields)
    _validate_text_fields(tmpl, request.fields)
    # Enforce the biconditional: sequence iff {{seq}}. Forward direction: a {{seq}} template without
    # a sequence spec would resolve {{seq}} to "" (misleading 200). Reciprocal: a non-{{seq}} template
    # with a sequence spec would silently print up to 500 identical labels, bypassing the copies cap.
    _validate_sequence_matches_template(tmpl, has_sequence=request.sequence is not None)
    language = request.language or settings.default_language
    now = datetime.now()

    # Render + blocking socket send run in a worker thread so an offline/slow printer can't stall
    # the event loop (health, metrics, auth). The lock keeps sends serialized to the one printer.
    #
    # Retry de-duplication is checked *inside* the lock, immediately before printing: two same-key
    # requests that race in together both resolve their template, but only one holds the lock at a time, so
    # the second sees the first's just-appended history record and returns it instead of printing a
    # duplicate. Checking before the lock (as a pre-lock fast path) would reopen that exact race. A
    # key reused with a *different* request is a client mistake (e.g. a dry-run key then a real
    # print), not a retry: reject it with 409 rather than silently returning the old job.
    async with _print_lock:
        if request.idempotency_key:
            prior = _find_idempotent_job(request.idempotency_key)
            if prior is not None:
                if prior.request_fingerprint != fingerprint:
                    raise HTTPException(
                        409,
                        f"idempotency_key {request.idempotency_key!r} was already used for a "
                        "different request; use a new key or omit it to print again",
                    )
                return PrintResponse(
                    job_id=prior.job_id,
                    template=prior.template,
                    copies=prior.copies,
                    dry_run=prior.dry_run,
                )
        # Pre-flight the loaded media over SNMP before committing the (silent-on-this-NIC) raster
        # send: a media mismatch or a hard printer fault is rejected with 409 here rather than
        # recorded as a phantom success. Held inside _print_lock so the query can't race a print.
        await _enforce_print_preflight(tmpl.label, dry_run=request.dry_run)
        return await run_in_threadpool(
            _execute_print,
            tmpl,
            request.fields,
            copies=request.copies,
            dry_run=request.dry_run,
            cut=request.cut,
            options=resolved_options,
            language=language,
            now=now,
            job_id=str(uuid.uuid4()),
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            sequence=request.sequence,
        )


@app.post(
    "/reprint/{job_id}",
    response_model=PrintResponse,
    dependencies=[Depends(check_token)],
    tags=["Printing"],
    responses={
        **RESPONSE_401,
        404: {"description": "Job not found in history"},
        409: {"description": "Job cannot be reprinted (failed, image-stripped, or schema drift)"},
    },
)
async def reprint(job_id: str) -> PrintResponse:
    record = _load_job(job_id)
    if record is None:
        raise HTTPException(404, f"Job {job_id!r} not found in history")
    if record.status == "failed":
        raise HTTPException(409, f"Job {job_id!r} failed to print and cannot be reprinted")
    if record.image_stripped:
        raise HTTPException(
            409,
            f"Job {job_id!r} contained an image, which is not retained in history; "
            "re-submit the original /print request to reproduce it",
        )
    tmpl = registry.get(record.template)
    if tmpl is None:
        raise HTTPException(409, f"Template {record.template!r} no longer exists; cannot reprint")

    # Enforce the current required-field contract on replay. /print rejects missing/blank required
    # fields up front; a saved row can fall short of it after schema drift (the template gained a
    # required field) or because it predates the contract. Rendering would substitute "" and emit a
    # physically blank required label while returning success — fail the replay with 409 instead.
    missing = _missing_required_fields(tmpl, record.fields)
    if missing:
        raise HTTPException(
            409,
            detail={
                "msg": "Saved job no longer satisfies the template's required fields",
                "template": tmpl.name,
                "missing_required": missing,
            },
        )

    # Re-apply the current input guards to fields replayed from durable history. /print validates on
    # the way in, but a row persisted by an earlier build can predate these validators (or the
    # image_stripped flag, which defaults False), so reprinting it would otherwise stream oversized
    # text or a retained image blob straight into the renderer, bypassing the caps /print enforces.
    # A row still carrying an image value is rejected like a freshly-stripped image job: image blobs
    # are not reproducible from history regardless of how old the record is.
    if any(_is_provided(record.fields.get(name)) for name in _image_field_names(tmpl.layout)):
        raise HTTPException(
            409,
            f"Job {job_id!r} contains an image, which is not retained in history; "
            "re-submit the original /print request to reproduce it",
        )
    _validate_text_fields(tmpl, record.fields)

    # Enforce the biconditional {{seq}}-sequence guard on replay too (schema drift in both directions).
    # Forward: saved row has no sequence spec but current template now uses {{seq}} → would resolve
    # {{seq}} to "" and reprint a silently blank-numbered label.
    # Reciprocal: saved row has a sequence spec but current template no longer uses {{seq}} → would
    # replay a batch of up to 500 identical unnumbered labels.
    # Both cases are schema drift; reject with 409 like the other reprint-drift guards.
    if record.sequence is None and uses_seq(tmpl.layout):
        raise HTTPException(
            409,
            f"Template {record.template!r} now uses the {{{{seq}}}} auto-numbering token but the "
            "saved job has no sequence spec; it cannot be reprinted. Submit a fresh /print with a "
            "`sequence` object.",
        )
    if record.sequence is not None and not uses_seq(tmpl.layout):
        raise HTTPException(
            409,
            f"Template {record.template!r} no longer uses the {{{{seq}}}} auto-numbering token but "
            "the saved job has a sequence spec; replaying it would print a batch of identical "
            "unnumbered labels. Submit a fresh /print without a sequence spec.",
        )

    # Two-color media drift guard: if the saved job requested red=True, verify the CURRENT
    # template's model + media still support two-color. The original /print gate ran at submit time;
    # since then the template may have been re-bound to plain (non-red) media, or the configured
    # model may have changed. convert(red=True) silently loses the red layer on non-red media rather
    # than raising, so we must check statically — a silent "successful" reprint with no red output
    # is worse than an explicit 409. Maps to 409 (not 422) because this is schema/media drift on
    # an existing record, consistent with the other reprint-drift guards above.
    if record.options.red:
        reason = _two_color_unsupported_reason(tmpl)
        if reason is not None:
            LABEL_ERRORS.labels(reason="unsupported_two_color").inc()
            raise HTTPException(
                409,
                f"Job {job_id!r} was printed with red=True, but the current template/model no "
                f"longer supports two-color printing: {reason}",
            )

    # Replay the frozen reference instant so the reprinted label's computed dates are identical.
    now = datetime.fromisoformat(record.render_now) if record.render_now else datetime.now()
    async with _print_lock:
        # Same SNMP media/fault preflight as /print: a saved job replayed against a printer now
        # loaded with different media (or in a fault state) is rejected with 409, not phantom-printed.
        await _enforce_print_preflight(tmpl.label, dry_run=record.dry_run)
        return await run_in_threadpool(
            _execute_print,
            tmpl,
            record.fields,
            copies=record.copies,
            dry_run=record.dry_run,
            cut=record.cut,
            options=record.options,
            language=record.language or settings.default_language,
            now=now,
            job_id=str(uuid.uuid4()),
            sequence=record.sequence,
        )


# The browse routes list ``_require_history_ui`` *before* ``check_token`` so the visibility gate
# wins: with HISTORY_UI=false they 404 (route appears absent) rather than 401 (which would reveal a
# hidden-but-present endpoint). FastAPI resolves a flat dependency list in declaration order.
@app.get("/history", response_class=HTMLResponse, dependencies=[Depends(_require_history_ui)])
async def history_page(request: Request) -> HTMLResponse:
    """Browse-history page shell. Public like ``GET /`` — it carries no history data (that is
    fetched client-side from the token-protected ``/history/list``) and must be reachable from a
    plain link so the browser can render its token input. 404s only when browsing is disabled."""
    return jinja.TemplateResponse(request, "history.html", {})


@app.get(
    "/history/list",
    response_model=HistoryPage,
    dependencies=[Depends(_require_history_ui), Depends(check_token)],
    tags=["History"],
    responses={**RESPONSE_401},
)
def history_list(
    offset: Annotated[int, Query(ge=0, le=MAX_HISTORY_OFFSET)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_PAGE_SIZE)] = DEFAULT_HISTORY_PAGE_SIZE,
) -> HistoryPage:
    """Paginated, newest-first slice of job history for the browse UI."""
    return HistoryPage(
        entries=_history.page(offset=offset, limit=limit),
        total=_history.count(),
        offset=offset,
        limit=limit,
    )


@app.delete(
    "/history/{job_id}",
    dependencies=[Depends(_require_history_ui), Depends(check_token)],
    tags=["History"],
    responses={**RESPONSE_401, 404: {"description": "Job not found in history"}},
)
def history_delete(job_id: str) -> dict[str, bool]:
    """Delete a single history entry by job id.

    404 is reserved for a *confirmed* miss. A storage failure must not be collapsed into 404:
    delete is an irreversible, privacy-facing action, and telling the client "not found" while the
    row may still be retained would hide that the deletion never happened. So a store error
    surfaces as 500 instead.
    """
    try:
        deleted = _history.delete(job_id)
    except (OSError, sqlite3.Error) as exc:
        log.exception("Failed to delete history record for job %s", job_id)
        raise HTTPException(500, "Failed to delete history entry") from exc
    if not deleted:
        raise HTTPException(404, f"Job {job_id!r} not found in history")
    return {"deleted": True}


@app.post(
    "/reload",
    dependencies=[Depends(check_token)],
    tags=["Templates"],
    responses={**RESPONSE_401, 422: {"description": "Reload completed with errors; files skipped"}},
)
def reload_templates() -> dict[str, Any]:
    """Hot-reload templates and translation catalogs.

    Malformed files are skipped so the valid ones still load, but their errors are reported with a
    422 instead of a misleading 200 — otherwise a single YAML typo could silently drop a template
    or the default-language catalog while the API claims success, and the next print would quietly
    misbehave. The default-language catalog disappearing is itself treated as a reload failure.
    """
    loaded = registry.load_all()
    langs = translator.load_all()

    errors = registry.errors + translator.errors
    if not translator.has(settings.default_language):
        errors.append(
            f"default language {settings.default_language!r} has no catalog after reload "
            f"(available: {langs})"
        )
    if errors:
        raise HTTPException(
            422,
            {
                "detail": "Reload completed with errors; some files were skipped",
                "errors": errors,
                "loaded": loaded,
                "languages": langs,
            },
        )
    return {"loaded": len(loaded), "templates": loaded, "languages": langs}


def metrics() -> Response:
    """Prometheus exposition handler. Registered at settings.metrics_path at the END of this module
    (see ``_register_metrics_route``) so it is mounted AFTER every fixed route — a misconfigured
    METRICS_PATH that happens to equal a real route can never shadow it (first-registered wins)."""
    return Response(content=generate_latest(), media_type="text/plain; version=0.0.4")


@app.post(
    "/preview/multipart",
    dependencies=[Depends(check_token)],
    tags=["Printing"],
    responses={
        **RESPONSE_401,
        **RESPONSE_413,
        415: {"description": "Unsupported upload type; expected an image"},
    },
)
async def preview_multipart(
    template: Annotated[str, Form()],
    fields_json: Annotated[str, Form()] = "{}",
    language: Annotated[str | None, Form()] = None,
    image: Annotated[UploadFile | None, File()] = None,
) -> Response:
    fields: dict[str, Any] = json.loads(fields_json)
    if image is not None:
        if image.content_type and not image.content_type.startswith("image/"):
            raise HTTPException(
                415, f"Unsupported upload type {image.content_type!r}; expected an image"
            )
        # Read at most one byte past the cap so an oversized upload is bounded in memory; the
        # truncated read then trips the size check in _validate_upload_image with a 413.
        img_bytes = await image.read(MAX_IMAGE_UPLOAD_BYTES + 1)
        _validate_upload_image(img_bytes)
        fields["image"] = base64.b64encode(img_bytes).decode()
    # Build the same model the JSON routes receive. Constructing it by hand here bypasses FastAPI's
    # automatic body validation, so a blank template name (min_length=1) would raise ValidationError
    # as an unhandled 500 — translate it to the 422 the JSON routes would have returned.
    try:
        req = PrintRequest(template=template, fields=fields, language=language)
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(include_url=False)) from exc
    return await preview(req)


# The studio routes list ``_require_editor_enabled`` *before* ``check_token`` so the visibility gate
# wins: with EDITOR_ENABLED=false they 404 (route appears absent) rather than 401 (which would
# reveal a hidden-but-present endpoint). FastAPI resolves a flat dependency list in declaration order.
@app.post(
    "/preview/draft",
    dependencies=[Depends(_require_editor_enabled), Depends(check_token)],
    tags=["Templates"],
    responses=_DRAFT_RESPONSES,
)
async def preview_draft(request: DraftPreviewRequest) -> Response:
    """Live-render an in-memory draft template to PNG — no file is written.

    The raw ``yaml`` body is validated through the SAME path as ``app.loader`` (schema +
    reserved-name/{{seq}} + undeclared-token checks) without touching the filesystem. The same
    input caps as ``/preview`` and ``/print`` apply to ``fields`` (image size/pixel caps, text
    length cap, field-count cap) — a draft does NOT bypass any cap. The render is stateless: it
    does not acquire ``_print_lock``, touch history, or write any file, and like ``/preview`` it is
    a pre-driver monochrome render (dither/red/high_res are print-only and not accepted here).
    """
    tmpl = _validate_draft_template(request.yaml)

    # A draft with a {{seq}} layout but no sequence object would render {{seq}} to "" — reject it
    # exactly like /preview does for a saved {{seq}} template (forward direction only; preview
    # carries no sequence object).
    _validate_sequence_matches_template(tmpl, has_sequence=False, preview=True)
    # Same input caps as /preview and /print — reuse the shared validators verbatim so a draft
    # cannot smuggle an oversized image/text field or an unbounded field count past the guards.
    _validate_image_fields(tmpl, request.fields)
    _validate_text_fields(tmpl, request.fields)
    language = request.language or settings.default_language
    png = _render_template_preview(tmpl, request.fields, language)
    return Response(content=png, media_type="image/png")


@app.post(
    "/templates/parse",
    response_model=TemplateParseResponse,
    dependencies=[Depends(_require_editor_enabled), Depends(check_token)],
    tags=["Templates"],
    responses=_PARSE_RESPONSES,
)
async def parse_template(request: TemplateParseRequest) -> TemplateParseResponse:
    """Validate a draft YAML body and return its auto-detected field contract.

    Reuses the loader's field-contract computation: ``required``/``optional`` are the declared
    user fields, and computed/i18n tokens ({{date}}, {{now}}, {{seq}}, [[translation]]) are
    excluded by that logic, so the studio's generated form only asks for real user values. No file
    is written and nothing is rendered.
    """
    tmpl = _validate_draft_template(request.yaml)
    return TemplateParseResponse(
        name=tmpl.name,
        description=tmpl.description,
        label=tmpl.label,
        rotate=tmpl.rotate,
        fields=TemplateFieldContract(
            required=tmpl.required_fields,
            optional=tmpl.optional_fields,
        ),
    )


# Restrict a save target to a bare template name → exactly one .yaml file directly under
# TEMPLATES_DIR. Rejects path separators, parent traversal, hidden/extension tricks, and absolute
# paths so a crafted ``name`` can never escape the templates directory.
_SAFE_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")


def _safe_template_path(name: str) -> Path:
    """Resolve ``name`` to ``{templates_dir}/{name}.yaml`` or reject it (422) on traversal.

    Two independent guards: a strict allowlist regex on the bare name (no separators, dots, or
    ``..``), then a defence-in-depth check that the resolved path's parent is the templates dir —
    so even if the regex were ever loosened, a path that escapes the directory is rejected.
    """
    if not _SAFE_TEMPLATE_NAME.fullmatch(name):
        raise HTTPException(
            422,
            f"Invalid template name {name!r}: use only letters, digits, '-' and '_' "
            "(no path separators, dots, or extension)",
        )
    templates_dir = settings.templates_dir.resolve()
    candidate = (templates_dir / f"{name}.yaml").resolve()
    if candidate.parent != templates_dir:
        raise HTTPException(422, f"Invalid template name {name!r}: path traversal rejected")
    # On a case-insensitive filesystem (common for a macOS home install or a bind mount) ``Foo.yaml``
    # and ``foo.yaml`` address the SAME file, so saving internal name ``Foo`` while ``foo.yaml`` exists
    # would silently overwrite a DIFFERENT template — and the duplicate-internal-name registry guard
    # never fires because the two names differ in case. Reject a case-only collision with an existing
    # file unless it is the exact target (same case), so a save can only overwrite the template it
    # names. ``casefold`` (not ``lower``) handles non-ASCII correctly; the name allowlist is ASCII, so
    # this is conservative either way.
    for existing in templates_dir.glob("*.yaml"):
        stem = existing.stem
        if stem != name and stem.casefold() == name.casefold():
            raise HTTPException(
                422,
                f"Invalid template name {name!r}: collides with existing template {stem!r} on a "
                "case-insensitive filesystem; choose a name that differs by more than case",
            )
    return candidate


def _atomic_write_template_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: temp file in the same dir, fsync, then os.replace.

    A plain write truncates the target before writing, so a crash/short-write mid-write leaves a
    corrupt or empty template. Writing a sibling temp file and os.replace()-ing it onto the target is
    atomic on the same filesystem: the target is either the old content or the complete new content,
    never a half-written mix. The temp file is removed on any failure so a failed save leaves no
    stray ``*.tmp`` behind.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp_name).replace(path)
    except OSError:
        # mkstemp created the temp file; remove it so a failed write never leaks a partial sibling.
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _atomic_write_template(path: Path, text: str) -> None:
    """UTF-8 convenience wrapper over :func:`_atomic_write_template_bytes`."""
    _atomic_write_template_bytes(path, text.encode("utf-8"))


@app.post(
    "/templates",
    dependencies=[Depends(_require_editor_enabled), Depends(check_token)],
    tags=["Templates"],
    responses=_SAVE_RESPONSES,
)
async def save_template(request: SaveTemplateRequest) -> dict[str, Any]:
    """Persist a draft template YAML to TEMPLATES_DIR and hot-reload (gated by TEMPLATES_WRITABLE).

    Opt-in behind ``TEMPLATES_WRITABLE`` (default false) because docker-compose mounts
    ``templates/`` read-only. When disabled the route is a 403 — the studio offers Copy/Download
    instead. The YAML is validated through the shared loader path BEFORE the write so a broken
    template is never persisted, and ``name`` is constrained to a bare file name under the templates
    directory (path-traversal guarded). After writing, the registry is reloaded so the new template
    is immediately printable.
    """
    if not settings.templates_writable:
        raise HTTPException(
            403,
            "Server-save is disabled; set TEMPLATES_WRITABLE=true (with a writable templates "
            "directory) to enable it, or use Download/Copy YAML instead",
        )
    # Validate before writing — never persist a YAML that would fail to load.
    tmpl = _validate_draft_template(request.yaml)
    # The validated template's internal `name` is the SINGLE source of truth for the save target.
    # The registry indexes by that internal name, so deriving the filename from request.name (which
    # may differ) would let `{name: simple, <yaml has name: renamed>}` write simple.yaml while
    # registering "renamed" — clobbering simple.yaml and registering a template at a mismatched key,
    # then falsely reporting saved=simple. Writing to `<tmpl.name>.yaml` keeps filename == registry
    # key == internal name. The path-traversal guard still applies (tmpl.name is user-controlled).
    path = _safe_template_path(tmpl.name)
    # Atomic write: a plain write_text() truncates the target first, so a short write / disk error
    # mid-write corrupts or empties an existing template while the API reports saved. Instead write a
    # temp file in the SAME directory (so os.replace is atomic on one filesystem), fsync it, then
    # replace the target in one syscall. Capture the prior content first so a failed reload can roll
    # back to exactly what was there before.
    previous_bytes = path.read_bytes() if path.exists() else None
    try:
        _atomic_write_template(path, request.yaml)
    except OSError as exc:
        log.exception("Failed to write template %s", path)
        raise HTTPException(500, f"Failed to write template: {exc}") from exc

    # The /reload endpoint treats reload errors as 422; save must not be weaker. Verify the saved
    # template actually registered AND reload reported no error before claiming success.
    #
    # registry.load_all() only catches TemplateLoadError; a non-UTF8/unreadable file or an FS error
    # raises straight out, so the reload (and the post-reload identity check) run inside a try/except
    # — without it such a failure would 500 with the new file left on disk and NO rollback. We
    # collect the reason(s) to roll back, then roll back ONCE so success and every failure share one
    # restore path.
    rollback_reasons: list[str] = []
    try:
        loaded = registry.load_all()
        errors = registry.errors
        if tmpl.name not in loaded:
            rollback_reasons.append(f"template {tmpl.name!r} did not register after reload")
        elif errors:
            rollback_reasons.extend(errors)
        else:
            # tmpl.name registered AND no errors, but a DIFFERENT file later in sort order may
            # declare the same internal `name` — the registry would then index that name to the OTHER
            # file while we wrongly report success. Confirm the registered template resolves to the
            # file we just wrote (Template.source_path is the loaded file's path).
            registered = registry.get(tmpl.name)
            if registered is None or registered.source_path != path:
                other = registered.source_path.name if registered is not None else "unknown"
                rollback_reasons.append(
                    f"another template file ({other}) already declares the internal name "
                    f"{tmpl.name!r}; rename this template so its name is unique"
                )
    except Exception as exc:  # any reload failure (FS/decode/unexpected) must roll back, not 500
        log.exception("Post-write reload of template %s failed", path)
        errors = [str(exc)]
        rollback_reasons.append(f"reload raised: {exc}")

    if rollback_reasons:
        # Roll back: restore the previous file content (or delete the new file if none existed
        # before). If the restore/delete ITSELF fails, the on-disk state is now inconsistent — report
        # that truthfully as a 500 rather than the 422 "rolled back" message (which would be a lie).
        try:
            if previous_bytes is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_template_bytes(path, previous_bytes)
        except OSError as exc:
            log.exception("Failed to roll back template %s after reload error", path)
            raise HTTPException(
                500,
                {
                    "detail": (
                        "Save failed AND rollback failed: the templates directory may be in an "
                        "inconsistent state on disk — inspect it manually"
                    ),
                    "errors": [*rollback_reasons, f"rollback error: {exc}"],
                    "saved": None,
                },
            ) from exc
        # Rollback succeeded; reload (best-effort, guarded) so the registry matches the restored disk
        # state. A failure here cannot un-restore the file, so it must not turn the truthful 422 into
        # a 500 — log it and still report the rolled-back save.
        try:
            registry.load_all()
        except Exception:  # best-effort resync; the disk is already consistent after restore
            log.exception("Best-effort registry reload after rollback of %s failed", path)
        raise HTTPException(
            422,
            {
                "detail": "Save rolled back: the written template failed to reload",
                "errors": rollback_reasons,
                "saved": None,
            },
        )
    # Report the name actually registered after reload (the file's stem == tmpl.name), so the
    # response can never claim a save under a name that was not the one persisted.
    return {"saved": tmpl.name, "path": path.name, "loaded": loaded, "errors": errors}


def _validate_upload_image(raw: bytes) -> None:
    """Reject oversized, malformed, or decompression-bomb image uploads with a clear 4xx.

    Bounds are sized for thermal label printing (see MAX_IMAGE_* constants), so a runaway upload
    can't force multiple full-size copies into memory and crash the process.
    """
    if len(raw) > MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(
            413, f"Image too large: {len(raw)} bytes (max {MAX_IMAGE_UPLOAD_BYTES})"
        )
    try:
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
    except Image.DecompressionBombError as exc:
        raise HTTPException(413, f"Image has too many pixels: {exc}") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(422, f"Invalid image upload: {exc}") from exc
    if width * height > MAX_IMAGE_PIXELS:
        raise HTTPException(
            413, f"Image too large: {width}x{height} px (max {MAX_IMAGE_PIXELS} px)"
        )


def _image_field_names(layout: list[dict[str, Any]]) -> set[str]:
    """Names of the fields the template's image elements read; see :func:`engine.image_field_names`.

    Thin wrapper over the canonical row-aware walker so request validation and history stripping
    share one source of truth with the loader's image/text field-collision check.
    """
    return image_field_names(layout)


def _strip_image_fields(tmpl: Template, fields: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return ``fields`` with image blobs dropped, and whether any were dropped.

    A base64 image is up to ~7 MiB; persisting it verbatim in the history store would let a few
    image jobs bloat the database (and, in ``memory`` mode, resident RAM) into a disk/latency
    problem. Image blobs are not needed for audit or idempotency (the latter keys off
    ``request_fingerprint``), so they are omitted from the stored record. The cost is that an
    image job cannot be reprinted; ``/reprint`` refuses it explicitly via the returned flag.
    """
    image_names = _image_field_names(tmpl.layout)
    stripped = False
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name in image_names and isinstance(value, str) and value:
            stripped = True  # drop the blob, keep the record small
            continue
        out[name] = value
    return out, stripped


def _is_provided(value: Any) -> bool:
    """A field counts as provided only if it carries a meaningful value.

    A blank or whitespace-only string is treated as *missing*: required fields are enforced to
    avoid rendering a blank physical label, and an empty box that the UI happened to submit must
    not satisfy that contract.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _missing_required_fields(tmpl: Template, fields: dict[str, Any]) -> list[str]:
    """Sorted required fields of *tmpl* not satisfied by *fields*.

    A blank or whitespace-only value counts as missing (see :func:`_is_provided`) so the contract
    is identical on the live-print and history-replay paths.
    """
    provided = {k for k, v in fields.items() if _is_provided(v)}
    return sorted(set(tmpl.required_fields) - provided)


def _validate_draft_template(yaml_text: str) -> Template:
    """Validate a draft YAML body into a :class:`Template`, mapping failures to a structured 422.

    Routes the user-supplied YAML through the SAME validator the file loader uses
    (:func:`validate_template_from_string` → ``build_template_from_mapping``): schema checks,
    reserved-name/``{{seq}}`` rules, undeclared-token rejection, and per-element layout validation.
    A malformed YAML body or any schema error becomes a 422 with a clear message so the studio can
    surface it inline — it must never reach the client as an unhandled 500.
    """
    try:
        return validate_template_from_string(yaml_text)
    except TemplateLoadError as exc:
        raise HTTPException(
            422,
            detail={"msg": "Invalid template YAML", "error": str(exc)},
        ) from exc


def _resolve_template(template_name: str, fields: dict[str, Any]) -> Template:
    """Look up a template by name and enforce its required-field contract.

    The caller always names the template explicitly. A named template must still not print a blank
    label: every required field must be present and non-blank (a whitespace-only value counts as
    missing, see :func:`_is_provided`). Unknown name → 404; missing required fields → 422.
    """
    tmpl = registry.get(template_name)
    if tmpl is None:
        raise HTTPException(404, f"Template {template_name!r} not found")
    missing = _missing_required_fields(tmpl, fields)
    if missing:
        raise HTTPException(
            422,
            detail={
                "msg": "Missing required fields",
                "template": tmpl.name,
                "missing_required": missing,
            },
        )
    return tmpl


def _validate_sequence_matches_template(
    tmpl: Template, has_sequence: bool, *, preview: bool = False
) -> None:
    """Enforce the biconditional: a ``sequence`` spec is required IFF the template uses ``{{seq}}``.

    Two directions are checked (unless *preview* is True, which skips the reciprocal):

    * Forward — ``uses_seq`` but no ``sequence`` spec: ``{{seq}}`` would resolve to "" and print a
      silently blank-numbered label.  → 422.

    * Reciprocal (non-preview only) — ``sequence`` spec present but template does NOT use ``{{seq}}``:
      every item in the batch renders identically, silently printing up to 500 duplicate labels and
      bypassing the ``copies`` cap (10).  → 422.

    ``seq`` is a COMPUTED_TOKEN so the loader never declares it as a required field — the forward
    check catches what the field-presence check misses.  The reciprocal check closes the inverse gap.

    Pass ``preview=True`` from the /preview route: preview never carries a sequence object, so only
    the forward direction is meaningful there; a non-seq template previews normally.
    """
    template_uses_seq = uses_seq(tmpl.layout)
    if not has_sequence and template_uses_seq:
        raise HTTPException(
            422,
            f"Template {tmpl.name!r} uses the {{{{seq}}}} auto-numbering token; a `sequence` spec "
            "is required. Submit /print with a `sequence` object (start/count/step/padding) so each "
            "label gets a distinct number. /preview cannot render a {{seq}} template — preview a "
            "non-sequence template, or use /print with a sequence to print the batch.",
        )
    if not preview and has_sequence and not template_uses_seq:
        raise HTTPException(
            422,
            f"Template {tmpl.name!r} does not use the {{{{seq}}}} auto-numbering token; a `sequence` "
            "spec is not applicable — use `copies` to print duplicates of a non-sequence template.",
        )


def _two_color_unsupported_reason(tmpl: Template) -> str | None:
    """Return a human-readable reason string if the current model/media cannot produce a red print.

    Returns ``None`` when both conditions are satisfied (two-color is safe to proceed):

    1. The configured model supports two-color (``CAPABILITY.two_color`` — QL-800/810W/820NWB).
    2. The template's ``label`` is a black/red media identifier (in ``CAPABILITY.red_labels``,
       e.g. ``62red``).

    Gated on what is statically provable (model + the template's bound media). Whether the *physical*
    roll loaded matches is the printer's responsibility (a live status read), not knowable here.

    Callers map this to the appropriate status code: /print → 422 (client sent an invalid request);
    /reprint → 409 (the saved job can no longer be satisfied — model/media drifted since print time).
    """
    cap = _driver_cls.CAPABILITY
    if not cap.two_color:
        return (
            f"Two-color (red) printing is not supported by model {settings.model!r}; "
            "set red=false or omit it (two-color models: QL-800/810W/820NWB)."
        )
    if tmpl.label not in cap.red_labels:
        return (
            f"Template {tmpl.name!r} uses media {tmpl.label!r}, which is not black/red two-color "
            f"media; red printing requires one of {cap.red_labels or ['(none for this model)']}."
        )
    return None


def _validate_two_color_supported(tmpl: Template) -> None:
    """Reject a ``red=true`` /print request the configured printer/media cannot produce with a 422.

    Delegates the capability check to :func:`_two_color_unsupported_reason` and maps a non-None
    result to a 422 (client error: the request itself is invalid for the current model/media).
    /reprint uses the same helper but maps to 409 (schema/media drift) — see the reprint handler.
    """
    reason = _two_color_unsupported_reason(tmpl)
    if reason is not None:
        LABEL_ERRORS.labels(reason="unsupported_two_color").inc()
        raise HTTPException(422, reason)


def _validate_image_fields(tmpl: Template, fields: dict[str, Any]) -> None:
    """Apply the upload size/pixel caps to every base64 image field the template declares.

    The multipart route validates the raw upload before encoding; this covers /preview and
    /print, where an image arrives as a base64 string that would otherwise reach PIL unguarded.
    Validating by the template's actual image-element field names (not just the literal ``image``)
    closes the bypass where a template reads its image from a custom field. The encoded length is
    checked before decoding so an oversized field is rejected without materializing it.
    """
    for name in _image_field_names(tmpl.layout):
        raw = fields.get(name)
        if raw is None or raw == "":
            continue  # absent/blank: the required-field contract governs presence, not type
        if not isinstance(raw, str):
            # A provided image field must be a base64 string. A list/object/number is exempt from
            # the text-field cap (it is an image field) and would otherwise reach ImageElement.render
            # where base64.b64decode raises TypeError → a 500 instead of a clear client error.
            raise HTTPException(
                422, f"Image field {name!r} must be a base64 string, not a {type(raw).__name__}"
            )
        if len(raw) > MAX_IMAGE_B64_CHARS:
            raise HTTPException(413, f"Image field {name!r} too large: {len(raw)} base64 chars")
        try:
            decoded = base64.b64decode(raw)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, f"Invalid base64 image field {name!r}: {exc}") from exc
        _validate_upload_image(decoded)


def _validate_text_fields(tmpl: Template, fields: dict[str, Any]) -> None:
    """Reject field values whose rendered text the render path cannot safely allocate.

    Text elements without a ``max_lines`` cap wrap their field text into a strip whose height
    grows with the input, allocated before the canvas is clamped. Bounding the input here keeps a
    single oversized request from exhausting memory. Image fields are excluded — they carry a
    (separately capped) base64 blob, not displayable text.

    The cap is applied to the *rendered* representation (``str(value)``), not just to values that
    arrive as strings: ``{{field}}`` substitution stringifies whatever it is given, so a JSON list
    or a giant number would otherwise slip past a ``str``-only check and reach the renderer as a
    multi-megabyte string. List/object values are rejected outright (422) — a label field is a
    single scalar; a collection can never render to a sensible label.
    """
    if len(fields) > MAX_FIELD_COUNT:
        raise HTTPException(413, f"Too many fields: {len(fields)} (max {MAX_FIELD_COUNT})")
    image_fields = _image_field_names(tmpl.layout)
    for name, value in fields.items():
        if len(name) > MAX_FIELD_NAME_CHARS:
            raise HTTPException(
                413,
                f"Field name too long: {len(name)} chars (max {MAX_FIELD_NAME_CHARS})",
            )
        if name in image_fields:
            continue
        if isinstance(value, dict | list):
            raise HTTPException(
                422,
                f"Field {name!r} must be a scalar value, not a {type(value).__name__}",
            )
        rendered = str(value)
        if len(rendered) > MAX_TEXT_FIELD_CHARS:
            raise HTTPException(
                413,
                f"Field {name!r} too long: {len(rendered)} chars (max {MAX_TEXT_FIELD_CHARS})",
            )


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    """Serve the app logo as the favicon. Public (no auth) — a tab icon must load without a token."""
    return FileResponse(_web_dir / "logo.svg", media_type="image/svg+xml")


@app.get("/editor", response_class=HTMLResponse, dependencies=[Depends(_require_editor_enabled)])
async def editor_page(request: Request) -> HTMLResponse:
    """In-browser YAML template studio.

    Public like ``GET /`` — the shell carries no privileged data; the token-protected draft
    preview / parse / save calls are made client-side with the saved Bearer token. ``two_color`` /
    ``templates_writable`` toggle UI affordances only. 404s when EDITOR_ENABLED=false.
    """
    # The label-reference panel: every label the configured model supports, each with the media it
    # requires (mm). Sourced from the same _template_media()/required_media_for() the print-page
    # badge and the /print media guard use, so the studio author sees exactly the media the server
    # will enforce. Embedded server-side (like index.html's TEMPLATES) so the panel renders without a
    # round-trip; the live "Your Printer" highlight is layered on client-side from /printer/status.
    # ``red`` flags black/red two-colour media (e.g. 62red). Geometry-only media matching treats
    # 62red and plain 62 as the same roll (see app.media.media_matches — SNMP reports no roll colour),
    # so the studio must NOT badge a red label as a definite match against a roll whose colour it can't
    # verify; the client surfaces red matches as geometry-only to avoid steering authors onto red
    # media that would print black-only on a plain roll.
    red_labels = set(_driver_cls.CAPABILITY.red_labels)
    labels = [
        {
            "id": label_id,
            "media": (m.model_dump() if (m := _template_media(label_id)) is not None else None),
            "red": label_id in red_labels,
        }
        for label_id in _driver_cls.CAPABILITY.supported_labels
    ]
    return jinja.TemplateResponse(
        request,
        "editor.html",
        {
            "history_ui": settings.history_ui,
            "templates_writable": settings.templates_writable,
            "templates_loadable": settings.templates_loadable,
            "labels": labels,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def web_ui(request: Request) -> HTMLResponse:
    tmpl_list = [
        {
            "name": t.name,
            "description": t.description,
            "required": t.required_fields,
            "optional": t.optional_fields,
            # Required media per template (None when the label is unknown to brother_ql) so the page
            # can badge each template against the loaded roll from GET /printer/status. Same source
            # as TemplateInfo.media, serialised for the inline TEMPLATES JSON.
            "media": (m.model_dump() if (m := _template_media(t.label)) is not None else None),
        }
        for t in registry.all()
    ]
    return jinja.TemplateResponse(
        request,
        "index.html",
        {
            "templates": tmpl_list,
            "history_ui": settings.history_ui,
            "editor_enabled": settings.editor_enabled,
            "default_dither": settings.default_dither,
            "default_threshold": settings.default_threshold,
            "default_high_res": settings.default_high_res,
            "default_red": settings.default_red,
            # Surface whether the configured model supports two-color so the UI can hide the toggle
            # on models that can never print red (the print gate would 422 it anyway).
            "two_color": _driver_cls.CAPABILITY.two_color,
            # Gate the background status poll to deployments where /printer/status is served lock-free
            # over SNMP. On the ESC i S fallback (USB/file, or SNMP_ENABLED=false) the status read
            # takes _print_lock, so a background poll would sit on the lock and delay a later /print —
            # reintroducing the contention this change removed. There, only manual ↻ / post-print
            # refresh poll (explicit user actions, as before).
            "live_status_poll": _snmp_guard_applies(),
        },
    )


# ── Metrics route (registered LAST) ───────────────────────────────────────────────────────────────
# Registered here, after every other route is defined, at the env-configured settings.metrics_path
# (default /metrics; charset already restricted to a literal by Settings._normalize_metrics_path, so
# no path parameter/wildcard can reach the router). Registering last is a safety property: Starlette
# matches the FIRST-registered route, so a misconfigured METRICS_PATH equal to a real route can never
# shadow that page/endpoint. A direct collision is still a config mistake (metrics would be
# unreachable there), so it is rejected fail-fast at import rather than silently swallowed.
@app.middleware("http")
async def _metrics_disabled_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Make the metrics path behave as TRULY ABSENT when METRICS_ENABLED=false — for every method.

    A per-route dependency only runs on the matched (GET) route, so a disabled GET /metrics 404s but
    POST /metrics returns 405 (Starlette resolves method-not-allowed before dependencies), and that
    405 + ``Allow: GET`` leaks that the (possibly relocated) path exists. Gating in middleware — which
    runs before routing — returns a uniform 404 for any method to the path while disabled, matching a
    genuinely missing path. Runtime-toggleable (reads the setting per request). When enabled it is a
    no-op and normal routing applies (a 405 on POST is then the honest "real endpoint" response).
    """
    if not settings.metrics_enabled and request.url.path == settings.metrics_path:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


def _register_metrics_route() -> None:
    # Reject any path a GET would already resolve to — using Starlette's real route matching, not a
    # literal string compare. A string compare misses DYNAMIC shadowing: a literal METRICS_PATH like
    # /templates/foo/source is captured by the earlier /templates/{name}/source route, so (since
    # metrics registers last) a scrape would silently hit that handler instead of the exposition.
    # Match.FULL = an existing route fully handles GET at this path; reject so the misconfig fails fast.
    probe = {"type": "http", "method": "GET", "path": settings.metrics_path}
    for route in app.routes:
        match, _ = route.matches(probe)
        if match == Match.FULL:
            raise RuntimeError(
                f"METRICS_PATH {settings.metrics_path!r} is already served by route "
                f"{getattr(route, 'path', route)!r}; choose a different path."
            )
    # Always registered (so the harness/an enabled deployment serves it); the disabled state is
    # enforced uniformly by _metrics_disabled_gate above, not a per-route dependency.
    app.add_api_route(
        settings.metrics_path,
        metrics,
        methods=["GET"],
        tags=["System"],
        include_in_schema=False,  # not advertised via the unauthenticated /openapi.json
    )


_register_metrics_route()
