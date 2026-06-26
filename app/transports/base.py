# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse  # matches app/transports/network.py


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
    media_width_mm: int | None = None
    media_length_mm: int | None = None
    media_type: str | None = None
    status_type: str | None = None
    phase_type: str | None = None
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
    def unsupported(
        cls, reason: str = "status query not supported over this transport"
    ) -> "PrinterStatus":
        """A status for transports that cannot query printer state (e.g. USB)."""
        return cls(ok=False, errors=[reason], raw={}, reachable=False)

    @classmethod
    def from_parsed(cls, decoded: dict[str, object]) -> "PrinterStatus":
        """Build a PrinterStatus from a parsed interpret_response dict."""
        raw_errors = decoded.get("errors")
        errors: list[str] = [str(e) for e in raw_errors] if isinstance(raw_errors, list) else []
        media_width = decoded.get("media_width")
        media_length = decoded.get("media_length")
        return cls(
            ok=not bool(errors),
            errors=errors,
            raw=decoded,
            model=str(decoded["model_name"]) if "model_name" in decoded else None,
            media_width_mm=int(media_width) if isinstance(media_width, int) else None,
            media_length_mm=int(media_length) if isinstance(media_length, int) else None,
            media_type=str(decoded["media_type"]) if "media_type" in decoded else None,
            status_type=str(decoded["status_type"]) if "status_type" in decoded else None,
            phase_type=str(decoded["phase_type"]) if "phase_type" in decoded else None,
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
