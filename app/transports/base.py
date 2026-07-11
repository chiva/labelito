# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlparse  # matches app/transports/network.py

from app.media import canonical_media_type

if TYPE_CHECKING:
    # Type-only import: base is a leaf the SNMP module never imports, so this avoids any runtime
    # coupling (and the appearance of a cycle) while still typing from_snmp's argument.
    from app.transports.snmp import PrinterSNMPStatus


class PrinterUnreachable(OSError):
    """The printer could not be reached BEFORE any label bytes were sent.

    Raised by a transport when its connect phase fails (connection refused, no route to host, or a
    connect timeout on a blackholed host). It is deliberately an *unambiguous* failure: nothing was
    printed, so the caller can present a clean, retry-safe "printer unreachable" error — distinct from
    a mid-send/read failure, whose outcome is unknown and must not be dressed up as retryable.
    """


@dataclass(frozen=True)
class PrinterStatus:
    """Outcome of a ``send`` as reported back by the printer (status readback).

    Brother QL printers answer a print with a 32-byte status packet whose error bytes tell us
    whether the label actually came out — out-of-media, cover-open, media-mismatch, etc. A
    transport that can read this back surfaces it here so the caller can fail the job instead of
    silently recording a phantom print. ``ok`` is the single bit callers must check; ``errors``
    carries the human-readable strings from brother_ql's parser for logging/diagnostics, and
    ``raw`` keeps the decoded fields for richer endpoints (e.g. a future /printer/status).

    Transports that cannot read printer state (``file://`` has no printer) return a synthetic
    OK so the happy path is unchanged. ``send`` may also return ``None`` for a transport that
    does not implement readback (``usb`` routes through brother_ql's own blocking helper), which
    the caller treats as "no error reported" — backward-compatible with the old contract.

    ``query_status()`` on each transport also returns a ``PrinterStatus``; the extended
    optional fields (model, media_width_mm, media_length_mm, media_type, status_type, phase_type)
    are populated from the parsed ``interpret_response`` dict when available.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)
    # Extended fields populated by query_status() from interpret_response (None when not queried
    # or when the transport is not backed by a real printer).
    model: str | None = None
    # Float, not int: the SNMP media guard compares these against the printer's reported dimensions
    # with a ±1mm tolerance, and the web UI mirrors that comparison off this same value (via
    # /printer/status). Rounding here would let the UI read a different number than the server-side
    # guard's unrounded compare and disagree at the tolerance boundary, so we keep full precision.
    media_width_mm: float | None = None
    media_length_mm: float | None = None
    media_type: str | None = None
    status_type: str | None = None
    phase_type: str | None = None
    # SNMP-derived identity/health fields (populated by from_snmp on the network transport; None on
    # the ESC i S / file / USB paths, which cannot read them). serial/hostname are inventory
    # identity; console_text is the printer's front-panel line ("READY" when idle); cover_status is
    # the raw prtCoverStatus enum; label_lifecount is the lifetime prtMarkerLifeCount gauge.
    serial: str | None = None
    hostname: str | None = None
    console_text: str | None = None
    cover_status: int | None = None
    label_lifecount: int | None = None
    # Whether the status came from a real printer query (True) or is synthetic (False). Lets the
    # /printer/status endpoint distinguish "printer replied, all ok" from "no printer to query".
    reachable: bool = True

    @classmethod
    def synthetic_ok(cls) -> "PrinterStatus":
        """A success with no printer behind it (file sink / dry-run-style transports)."""
        return cls(ok=True, errors=[], raw={}, reachable=False)

    @classmethod
    def unreachable(cls, reason: str = "printer did not respond") -> "PrinterStatus":
        """A status representing a printer that could not be reached or queried."""
        return cls(ok=False, errors=[reason], raw={}, reachable=False)

    @classmethod
    def from_parsed(cls, decoded: dict[str, object]) -> "PrinterStatus":
        """Build a PrinterStatus from a parsed ``brother_ql.reader.interpret_response`` dict.

        ``media_type`` is normalized to the canonical ``continuous``/``die_cut`` (via
        :func:`app.media.canonical_media_type`), NOT brother_ql's raw ``'Continuous length tape'``
        string, so the ESC i S status channel (USB) lands on the same two values the SNMP path emits
        and the print guard / UI badge compare against. ``status_type``/``phase_type`` stay as the raw
        brother_ql strings — they are diagnostics, not compared anywhere."""
        raw_errors = decoded.get("errors")
        errors: list[str] = [str(e) for e in raw_errors] if isinstance(raw_errors, list) else []
        media_width = decoded.get("media_width")
        media_length = decoded.get("media_length")
        return cls(
            ok=not bool(errors),
            errors=errors,
            raw=decoded,
            model=str(decoded["model_name"]) if "model_name" in decoded else None,
            media_width_mm=float(media_width) if isinstance(media_width, int) else None,
            media_length_mm=float(media_length) if isinstance(media_length, int) else None,
            media_type=canonical_media_type(decoded),
            status_type=str(decoded["status_type"]) if "status_type" in decoded else None,
            phase_type=str(decoded["phase_type"]) if "phase_type" in decoded else None,
            reachable=True,
        )

    @classmethod
    def from_snmp(cls, snmp: "PrinterSNMPStatus") -> "PrinterStatus":
        """Build a PrinterStatus from a :class:`PrinterSNMPStatus` (the network status channel).

        The Brother QL NIC accepts the :9100 TCP back-channel but never returns the 32-byte status
        frame, so SNMP — not ESC i S — is the channel that actually answers. An unreachable SNMP
        query maps to :meth:`unreachable` so the caller fails open (allow the print, badge unknown);
        a reachable one maps the decoded identity/media/error fields across. ``ok`` is False when the
        SNMP layer surfaced any error string (a nonzero hrPrinterDetectedErrorState or a non-READY
        console line), mirroring the ESC i S path's ``errors`` ⇒ ``ok=False`` contract.

        Media width/length carry the SNMP layer's full float precision (no rounding): the media
        guard compares them with a ±1mm tolerance and the web UI mirrors that compare off the same
        value via /printer/status, so rounding here would let the two disagree at the boundary.
        ``status_type``/``phase_type`` stay None — those are ESC i S concepts with no SNMP analogue.
        The error bitmask, hrPrinterStatus enum and authoritative loaded-media name ride in ``raw``
        so later consumers (metrics, the status card) can recover them without a second query.
        """
        if not snmp.reachable:
            return cls.unreachable("printer SNMP agent did not respond")
        return cls(
            ok=not bool(snmp.errors),
            errors=list(snmp.errors),
            raw={
                "error_state_bits": snmp.error_state_bits,
                "printer_status": snmp.printer_status,
                "media_name": snmp.media_name,
            },
            model=snmp.model,
            media_width_mm=snmp.media_width_mm,
            media_length_mm=snmp.media_length_mm,
            media_type=snmp.media_type,
            status_type=None,
            phase_type=None,
            serial=snmp.serial,
            hostname=snmp.hostname,
            console_text=snmp.console_text,
            cover_status=snmp.cover_status,
            label_lifecount=snmp.label_lifecount,
            reachable=True,
        )


@runtime_checkable
class Transport(Protocol):
    def __init__(self, uri: str) -> None: ...
    # send() may optionally return a PrinterStatus parsed from the printer's reply. Returning
    # None keeps the contract backward-compatible for transports that cannot read state; callers
    # treat None as "no error reported".
    def send(self, data: bytes) -> "PrinterStatus | None": ...
    def close(self) -> None: ...
    # query_status() sends the given status-request bytes and reads the printer's one-shot 32-byte
    # reply without a print job. The caller is responsible for building a model-correct request via
    # BrotherQLRaster (invalidate prefix + ESC i S); the transport is model-agnostic — it just
    # sends whatever bytes are passed. Returns a PrinterStatus with extended media/model fields
    # populated. Transports that cannot cleanly query state return PrinterStatus.unsupported() or
    # PrinterStatus.unreachable().
    def query_status(self, request: bytes) -> "PrinterStatus": ...


TRANSPORTS: dict[str, type[Transport]] = {}

# The transport is derived from the PRINTER_URI scheme rather than configured separately, so the
# two can never contradict each other. A scheme is required — a scheme-less string (e.g. a bare
# path or a "host:port" with no tcp://) is rejected rather than guessed, so a forgotten scheme
# fails loudly instead of silently writing labels to a file. The file sink needs explicit file://.
SCHEME_TO_TRANSPORT = {"tcp": "network", "usb": "usb", "file": "file"}


def register_transport(name: str):  # type: ignore[no-untyped-def]
    def decorator(cls: type[Transport]) -> type[Transport]:
        TRANSPORTS[name] = cls
        return cls

    return decorator


def get_transport(name: str) -> type[Transport]:
    if name not in TRANSPORTS:
        raise ValueError(f"Unknown transport {name!r}. Available: {sorted(TRANSPORTS)}")
    return TRANSPORTS[name]


def infer_transport(uri: str) -> str:
    """Resolve the registered transport name from a ``PRINTER_URI`` scheme.

    ``tcp://`` → ``network``, ``usb://`` → ``usb``, ``file://`` → ``file``. An unsupported or
    missing scheme raises ``ValueError`` so a typo (or a forgotten ``tcp://``) fails fast at
    startup rather than silently selecting the wrong transport.
    """
    scheme = urlparse(uri).scheme  # "" when no scheme is present
    name = SCHEME_TO_TRANSPORT.get(scheme)
    if name is None or name not in TRANSPORTS:
        raise ValueError(
            f"Cannot infer transport from PRINTER_URI {uri!r}: unsupported or missing scheme "
            f"{scheme!r}. Supported schemes: {sorted(SCHEME_TO_TRANSPORT)} "
            f"(e.g. tcp://host:port, usb://vendor:product, file:///path)."
        )
    return name
