# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import socket
import time
from urllib.parse import urlparse

from brother_ql.reader import interpret_response

from app.config import settings
from app.transports.base import PrinterStatus, register_transport
from app.transports.snmp import query_snmp_status

log = logging.getLogger(__name__)

# Brother QL printers answer a print job with a fixed-size status packet. We read exactly this
# many bytes back to decode error/media information rather than discarding the reply.
STATUS_PACKET_LEN = 32
# How long to wait for each individual status reply after the job bytes are sent. Kept short and
# separate from the connect/send TIMEOUT: a healthy printer answers within a few hundred ms once
# the page is processed, and we must not hang the request thread if a printer never replies (e.g.
# an older firmware or a print server that drops the back-channel).
STATUS_READ_TIMEOUT = 5
# Overall budget for the whole status-read exchange. A real printer emits SEVERAL status frames in
# sequence — a "Reply to status request" frame first, then phase-change/completion frames as the
# page actually runs — so we must keep reading past the first clean frame until we observe the
# completion+ready states (or an error). This caps the total time spent draining frames so a
# chatty-but-never-completing printer can't pin the request thread. Mirrors brother_ql's own
# blocking helper, which polls status for ~10s before giving up.
STATUS_READ_DEADLINE = 10

# brother_ql.reader status/phase strings we key success on (see RESP_STATUS_TYPES /
# RESP_PHASE_TYPES). A print is only successful once the printer reports it finished AND is ready
# for the next job — accepting the first clean frame would re-open the "silently succeed on a
# failed print" hole, because cover-open / end-of-media / cutter-jam frames arrive LATER.
STATUS_PRINTING_COMPLETED = "Printing completed"
STATUS_PHASE_CHANGE = "Phase change"
PHASE_WAITING_TO_RECEIVE = "Waiting to receive"


@register_transport("network")
class NetworkTransport:
    """TCP transport to a networked printer at tcp://host:port."""

    TIMEOUT = 10

    def __init__(self, uri: str) -> None:
        parsed = urlparse(uri)
        if not parsed.hostname or not parsed.port:
            raise ValueError(
                f"Invalid network printer URI {uri!r}: expected tcp://<host>:<port> "
                "(e.g. tcp://192.168.1.50:9100). Refusing to guess a default host — a typo "
                "would silently send labels to the wrong printer."
            )
        self._host = parsed.hostname
        self._port = parsed.port
        self._sock: socket.socket | None = None

    def send(self, data: bytes) -> PrinterStatus | None:
        """Send the raster, then read back the printer's status packets and decode them.

        Returns a :class:`PrinterStatus`. ``ok`` is False when the printer reported any error bit
        (no media, cover open, media mismatch, …) so the caller can fail the job instead of
        recording a phantom print. A real printer emits several 32-byte status frames per job, so
        we keep reading until the printer reports completion+ready (success) or an error. If the
        back-channel goes silent / times out / yields an unparseable frame before completion is
        observed we return ``None`` — the bytes were sent but the printer's state is unknown, which
        the caller treats as "no error reported" (backward-compatible with the original
        fire-and-forget behaviour) rather than failing a job that very likely printed.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.TIMEOUT)
        try:
            sock.connect((self._host, self._port))
            sock.sendall(data)
            return self._read_status(sock)
        finally:
            sock.close()

    def _read_status(self, sock: socket.socket) -> PrinterStatus | None:
        """Drain status frames until the printer reports completion+ready, an error, or goes quiet.

        Brother QL printers answer a print with a *sequence* of 32-byte status frames: a "Reply to
        status request" frame first, then phase-change/completion frames as the page actually runs.
        Accepting the first clean frame as success would miss cover-open / end-of-media /
        cutter-jam / transmission-error frames that arrive LATER. So we mirror
        brother_ql.backends.helpers.send: keep reading, fail immediately on any decoded error, and
        only declare success once we've seen ``Printing completed`` AND phase ``Waiting to
        receive``. A silent/short/garbled read before completion → ``None`` ("state unknown").

        TCP does not preserve message boundaries: a single ``recv`` may return fewer than
        ``STATUS_PACKET_LEN`` bytes (a frame split across segments) or more than one frame's worth
        (back-to-back frames coalesced). Decoding a short chunk makes ``interpret_response`` raise
        and would have dropped a real error frame. So we accumulate into ``buffer`` and only decode
        once we hold a full 32-byte frame, retaining any leftover bytes for the next frame.
        """
        deadline = time.monotonic() + STATUS_READ_DEADLINE
        did_print = False
        ready_for_next = False
        buffer = bytearray()

        while True:
            # Decode every complete frame already buffered BEFORE enforcing the deadline — a recv
            # on the previous iteration may have appended a full completion/error frame at the exact
            # moment the overall budget expired. Draining first ensures such a frame is decoded
            # (and a real error reported) instead of being discarded when the loop breaks below.
            # A single recv may also coalesce several frames, and an error frame must not wait behind
            # another read.
            while len(buffer) >= STATUS_PACKET_LEN:
                frame = bytes(buffer[:STATUS_PACKET_LEN])
                del buffer[:STATUS_PACKET_LEN]
                try:
                    # Decode via brother_ql's own status parser rather than hand-rolling offsets.
                    decoded: dict[str, object] = interpret_response(frame)
                except (NameError, ValueError) as exc:
                    # A full-length frame that still fails to parse (e.g. wrong 80:20:42 header) is
                    # genuinely garbled — treat as "state unknown", not a hard failure.
                    log.warning(
                        "Unparseable status frame from printer %s:%d: %s",
                        self._host,
                        self._port,
                        exc,
                    )
                    return None

                # interpret_response returns the error strings under "errors" (brother_ql.reader);
                # coerce to a concrete list[str] so the typed PrinterStatus contract holds.
                raw_errors = decoded.get("errors")
                errors: list[str] = (
                    [str(e) for e in raw_errors] if isinstance(raw_errors, list) else []
                )
                if errors:
                    # Any reported error fails the job, regardless of how many clean frames led it.
                    log.error("Printer %s:%d reported errors: %s", self._host, self._port, errors)
                    return PrinterStatus(ok=False, errors=errors, raw=decoded)

                if decoded.get("status_type") == STATUS_PRINTING_COMPLETED:
                    did_print = True
                if (
                    decoded.get("status_type") == STATUS_PHASE_CHANGE
                    and decoded.get("phase_type") == PHASE_WAITING_TO_RECEIVE
                ):
                    ready_for_next = True
                if did_print and ready_for_next:
                    return PrinterStatus(ok=True, errors=[], raw=decoded)

            # Only now enforce the overall deadline: any complete frame already buffered has been
            # decoded above, so breaking here can never discard a decodable verdict.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            # Bound each recv by whichever is sooner: the per-read poll interval or the time left
            # in the overall budget. This keeps the socket timeout from overshooting the deadline,
            # so a single slow read can never run past STATUS_READ_DEADLINE.
            sock.settimeout(min(STATUS_READ_TIMEOUT, remaining))
            try:
                chunk = sock.recv(STATUS_PACKET_LEN)
            except TimeoutError:
                # A per-read socket timeout (socket.timeout is an alias of TimeoutError) is NOT
                # end-of-status: a real printer can take longer than the poll interval to emit its
                # completion/error frame (long label, queued copies). Caught BEFORE the broader
                # OSError below since TimeoutError subclasses it. Keep polling until the OVERALL
                # deadline is reached rather than abandoning the read on the first timeout.
                continue
            except OSError as exc:
                # A genuine connection error (broken pipe, reset, …): the back-channel is gone, so
                # no further status is readable. Don't fabricate failure — the page was sent and the
                # state is unknown. Log loudly so a misbehaving printer/print server is visible.
                log.warning("No status reply from printer %s:%d (%s)", self._host, self._port, exc)
                return None

            if not chunk:
                # Closed/empty stream before completion was observed: state unknown, not an error.
                # Any trailing bytes shorter than a full frame are an incomplete tail, not garble.
                log.warning("Empty status reply from printer %s:%d", self._host, self._port)
                return None

            buffer.extend(chunk)

        # Deadline hit before completion was confirmed: we saw clean frames but never the
        # completion+ready pair. State is indeterminate — return None so the job still records as
        # printed rather than failing every job behind a quiet/slow printer.
        log.warning(
            "Printer %s:%d did not confirm completion within %ds (did_print=%s, ready=%s)",
            self._host,
            self._port,
            STATUS_READ_DEADLINE,
            did_print,
            ready_for_next,
        )
        return None

    def query_status(self, request: bytes) -> PrinterStatus:
        """Return the printer's current state over SNMP, or unreachable when SNMP is disabled.

        The Brother QL NIC accepts the :9100 TCP connection but never returns the 32-byte status
        frame (see docs/known-limitations.md), so SNMP (UDP 161) is the only status channel that
        actually answers on this hardware. When ``settings.snmp_enabled`` (the default) we query the
        printer's status OIDs over SNMP and map them via :meth:`PrinterStatus.from_snmp`; an
        unreachable SNMP agent yields ``reachable=False`` so the caller fails open / returns 503.

        When SNMP is disabled there is no TCP-native status channel on this hardware — the legacy
        ESC i S readback over :9100 was removed because it never returned a frame here and only burned
        the read deadline before reporting unreachable anyway. So we return ``reachable=False``
        directly. ``request`` is unused (kept for the :class:`~app.transports.base.Transport`
        signature); USB is the transport that consumes it for a real ESC i S query.
        """
        if settings.snmp_enabled:
            snmp = query_snmp_status(
                self._host,
                community=settings.snmp_community,
                port=settings.snmp_port,
                timeout=settings.snmp_timeout,
            )
            return PrinterStatus.from_snmp(snmp)
        return PrinterStatus.unreachable(
            "status unavailable: SNMP is disabled and this printer's TCP status back-channel does "
            "not return a status frame (set SNMP_ENABLED=true, or use a usb:// transport)"
        )

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None
