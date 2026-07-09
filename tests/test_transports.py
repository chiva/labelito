# SPDX-License-Identifier: GPL-3.0-or-later
"""Transport tests — network URI validation, scheme inference, and printer-status readback."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.transports.base import PrinterStatus, infer_transport
from app.transports.file import FileTransport
from app.transports.network import STATUS_PACKET_LEN, NetworkTransport
from app.transports.snmp import PrinterSNMPStatus

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


# ── Crafted Brother QL status packets ────────────────────────────────────────────
# A real printer answers a print with a SEQUENCE of 32-byte packets starting 80:20:42. Error bits
# live in bytes 8 (error information 1) and 9 (error information 2); the status/phase type live in
# bytes 18/19. We craft these to drive the parser without hardware. Bit meanings and the
# status/phase byte values come straight from brother_ql.reader (RESP_ERROR_INFORMATION_*,
# RESP_STATUS_TYPES, RESP_PHASE_TYPES).
_ERR1_NO_MEDIA_BIT = 0  # "No media when printing"
_ERR2_COVER_OPEN_BIT = 4  # "Cover opened while printing (Except QL-500)"

# brother_ql.reader.RESP_STATUS_TYPES / RESP_PHASE_TYPES byte values.
_STATUS_REPLY_TO_REQUEST = 0x00  # "Reply to status request" (the first, benign frame)
_STATUS_PRINTING_COMPLETED = 0x01  # "Printing completed"
_STATUS_PHASE_CHANGE = 0x06  # "Phase change"
_PHASE_WAITING_TO_RECEIVE = 0x00  # "Waiting to receive"


def _status_packet(
    err1: int = 0x00,
    err2: int = 0x00,
    status_type: int = _STATUS_REPLY_TO_REQUEST,
    phase_type: int = _PHASE_WAITING_TO_RECEIVE,
) -> bytes:
    """Build a valid 32-byte status packet (QL-800, 62mm continuous) with the given fields."""
    pkt = bytearray(STATUS_PACKET_LEN)
    pkt[0], pkt[1], pkt[2] = 0x80, 0x20, 0x42  # mandatory header interpret_response checks for
    pkt[4] = 0x38  # model code → QL-800
    pkt[8] = err1  # error information 1
    pkt[9] = err2  # error information 2
    pkt[10] = 62  # media width (mm)
    pkt[11] = 0x0A  # media type: continuous length tape
    pkt[18] = status_type  # status type (RESP_STATUS_TYPES)
    pkt[19] = phase_type  # phase type (RESP_PHASE_TYPES)
    return bytes(pkt)


# The benign first frame a printer always sends — clean, but NOT proof the page printed.
_REPLY_TO_REQUEST_PACKET = _status_packet()
# The frames that together prove a successful print: completion + ready-to-receive.
_PRINTING_COMPLETED_PACKET = _status_packet(status_type=_STATUS_PRINTING_COMPLETED)
_PHASE_WAITING_PACKET = _status_packet(
    status_type=_STATUS_PHASE_CHANGE, phase_type=_PHASE_WAITING_TO_RECEIVE
)
# A realistic successful exchange: benign reply, then completion, then phase→waiting-to-receive.
_OK_SEQUENCE = [_REPLY_TO_REQUEST_PACKET, _PRINTING_COMPLETED_PACKET, _PHASE_WAITING_PACKET]

_NO_MEDIA_PACKET = _status_packet(
    err1=1 << _ERR1_NO_MEDIA_BIT, status_type=0x02
)  # "Error occurred"
_COVER_OPEN_PACKET = _status_packet(err2=1 << _ERR2_COVER_OPEN_BIT, status_type=0x02)


class _FakeSocket:
    """Stand-in for socket.socket that records sent bytes and replies with crafted packet(s).

    Only the methods NetworkTransport.send touches are implemented. ``recv`` yields each configured
    reply frame in turn, then empty bytes (mirroring a closed stream). Accepts either a single
    bytes reply or a list of frames so tests can model the multi-frame status sequence a real
    Brother QL printer emits.
    """

    def __init__(self, reply: bytes | list[bytes]) -> None:
        self._frames: list[bytes] = [reply] if isinstance(reply, bytes) else list(reply)
        self.sent = b""
        self.timeouts: list[float] = []
        self.connected_to: tuple[str, int] | None = None
        self.closed = False

    def settimeout(self, t: float) -> None:
        self.timeouts.append(t)

    def connect(self, addr: tuple[str, int]) -> None:
        self.connected_to = addr

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if not self._frames:
            return b""
        return self._frames.pop(0)[:n]

    def close(self) -> None:
        self.closed = True


def _patch_socket(monkeypatch: pytest.MonkeyPatch, fake: _FakeSocket) -> None:
    import app.transports.network as net_mod

    monkeypatch.setattr(net_mod.socket, "socket", lambda *a, **k: fake)
    # These tests exercise the ESC i S readback path (_read_status), which send() only runs when
    # network_status_readback is enabled. The default is OFF (the QL NIC never answers over TCP —
    # see test_network_send_skips_readback_by_default), so opt these readback tests in explicitly.
    monkeypatch.setattr(net_mod.settings, "network_status_readback", True)


def _disable_snmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable SNMP so NetworkTransport.query_status takes the no-status-channel branch.

    query_status reads the printer over SNMP by default (the channel that actually answers on the QL
    NIC). With SNMP off there is no TCP status channel (the standalone ESC i S query was removed), so
    query_status returns ``unreachable``; mutating the shared settings singleton is reverted
    automatically by monkeypatch.
    """
    import app.transports.network as net_mod

    monkeypatch.setattr(net_mod.settings, "snmp_enabled", False)


def _patch_snmp(monkeypatch: pytest.MonkeyPatch, result: PrinterSNMPStatus) -> dict[str, object]:
    """Patch query_snmp_status (as referenced by network.py) to return ``result`` and capture its
    call kwargs, so the SNMP-backed query_status path can be tested without a real UDP socket."""
    import app.transports.network as net_mod

    captured: dict[str, object] = {}

    def fake_query(host: str, **kwargs: object) -> PrinterSNMPStatus:
        captured["host"] = host
        captured.update(kwargs)
        return result

    monkeypatch.setattr(net_mod, "query_snmp_status", fake_query)
    monkeypatch.setattr(net_mod.settings, "snmp_enabled", True)
    return captured


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("tcp://192.168.1.50:9100", "network"),
        ("usb://0x04f9:0x209c", "usb"),
        ("file:///tmp/output.bin", "file"),
    ],
)
def test_infer_transport_maps_scheme_to_registered_transport(uri: str, expected: str) -> None:
    assert infer_transport(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "ftp://printer:21",  # unsupported scheme
        "http://printer:9100",
        "/tmp/output.bin",  # bare path — no scheme, must not silently mean `file`
        "192.168.1.55:9100",  # looks like host:port but has no tcp:// scheme
        "",  # unset
    ],
)
def test_infer_transport_rejects_unsupported_or_missing_scheme(uri: str) -> None:
    """A missing scheme must fail loudly rather than guess a transport."""
    with pytest.raises(ValueError, match="Cannot infer transport"):
        infer_transport(uri)


def test_network_uri_valid_tcp_parses_host_and_port() -> None:
    t = NetworkTransport("tcp://192.168.1.50:9100")
    assert t._host == "192.168.1.50"
    assert t._port == 9100


# ── hostname URI resolves through NetworkTransport unchanged ──────────────────────
def test_network_hostname_uri_passes_hostname_to_socket_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tcp://hostname:port URI (e.g. tcp://BRW123456.local:9100) must be parsed and forwarded to
    the socket layer without modification — the transport must NOT reject it as 'not an IP' and must
    NOT attempt to resolve it before passing it to connect().

    socket.connect() with AF_INET delegates name resolution to the OS resolver (including mDNS
    .local via avahi/nss-mdns on Linux), so no resolution code is needed in the transport itself.
    This test confirms the hostname flows through create_connection unchanged.
    """
    fake = _FakeSocket(_OK_SEQUENCE)
    _patch_socket(monkeypatch, fake)

    transport = NetworkTransport("tcp://BRW123456.local:9100")

    # Hostname must be stored exactly as urlparse returns it (lowercased, no transformation).
    assert transport._host == "brw123456.local", (
        f"hostname must be stored as-is (urlparse lowercases); got {transport._host!r}"
    )
    assert transport._port == 9100

    # Sending over a hostname URI must reach the socket's connect with the hostname, not a raw IP.
    status = transport.send(b"RASTER-BYTES")

    assert fake.connected_to == ("brw123456.local", 9100), (
        f"socket.connect must receive the hostname unchanged; got {fake.connected_to!r} — "
        "the transport must not resolve or mangle hostnames before passing them to the OS"
    )
    assert isinstance(status, PrinterStatus)
    assert status.ok is True


@pytest.mark.parametrize(
    "uri",
    [
        "",  # empty / unset env var
        "192.168.1.55:9100",  # bare host:port, no scheme
        "tcp:/192.168.1.55:9100",  # malformed (single slash)
        "tcp://192.168.1.55",  # missing port
        "tcp://:9100",  # missing host
    ],
)
def test_network_uri_invalid_raises_instead_of_defaulting(uri: str) -> None:
    """A malformed URI must fail loudly, never fall back to a hardcoded default host."""
    with pytest.raises(ValueError, match="Invalid network printer URI"):
        NetworkTransport(uri)


# ── NetworkTransport reads and decodes the status packet ─────────────────────────
def test_network_send_skips_readback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With network_status_readback OFF (the default), send() must transmit the raster and return
    None immediately WITHOUT touching the back-channel — the QL NIC never answers it over TCP, and
    draining would only burn STATUS_READ_DEADLINE while holding the print lock. recv must never be
    called; the socket is still closed."""
    import app.transports.network as net_mod

    assert net_mod.settings.network_status_readback is False, "default must be off"
    fake = _FakeSocket(_OK_SEQUENCE)
    fake.recv = lambda n: pytest.fail("recv must not be called when readback is disabled")  # type: ignore[assignment]
    monkeypatch.setattr(net_mod.socket, "socket", lambda *a, **k: fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"RASTER-BYTES")

    assert status is None, "readback off → state unknown (None), the same verdict this NIC yields"
    assert fake.sent == b"RASTER-BYTES", "the raster must still be sent"
    assert fake.connected_to == ("192.168.1.50", 9100)
    assert fake.closed is True, "socket must be closed in the finally block"


def test_network_send_returns_ok_status_on_completion_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full success sequence (benign reply → completion → waiting-to-receive) surfaces ok=True,
    after the bytes are sent. A clean first frame alone is NOT enough — see the clean-then-error
    test below for why we must keep reading past it."""
    fake = _FakeSocket(_OK_SEQUENCE)
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"RASTER-BYTES")

    assert fake.sent == b"RASTER-BYTES", "payload must still be sent before status readback"
    assert fake.connected_to == ("192.168.1.50", 9100)
    assert fake.closed is True, "socket must be closed in the finally block"
    assert isinstance(status, PrinterStatus)
    assert status.ok is True, f"completion sequence should be ok; got errors={status.errors}"
    assert status.errors == []
    assert status.raw.get("media_width") == 62, "decoded fields are exposed for diagnostics"


def test_network_send_does_not_trust_clean_first_frame_before_later_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A benign 'Reply to status request' frame FOLLOWED BY a cover-open error frame must surface
    ok=False — the printer's later error frame must not be masked by the clean first frame.

    A real Brother QL printer emits the benign reply first and only reports cover-open /
    end-of-media / cutter-jam LATER. Accepting the first clean frame as success silently records a
    phantom print on a job that actually failed.
    """
    fake = _FakeSocket([_REPLY_TO_REQUEST_PACKET, _COVER_OPEN_PACKET])
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is not None
    assert status.ok is False, "a later error frame must not be masked by a clean first frame"
    assert any("Cover opened" in e for e in status.errors), (
        f"expected a cover-open error string from the second frame; got {status.errors}"
    )


def test_network_send_surfaces_no_media_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out-of-media error bit → ok=False and the human-readable reason from brother_ql's parser."""
    fake = _FakeSocket(_NO_MEDIA_PACKET)
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is not None
    assert status.ok is False, "no-media must not be reported as a successful print"
    assert any("No media" in e for e in status.errors), (
        f"expected a no-media error string; got {status.errors}"
    )


def test_network_send_surfaces_cover_open_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover-open error bit → ok=False with the matching reason string."""
    fake = _FakeSocket(_COVER_OPEN_PACKET)
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is not None
    assert status.ok is False
    assert any("Cover opened" in e for e in status.errors), (
        f"expected a cover-open error string; got {status.errors}"
    )


class _ChunkedSocket(_FakeSocket):
    """A socket whose recv yields pre-split byte chunks, modelling TCP segmentation.

    Unlike _FakeSocket (one full frame per recv), this returns exactly the configured chunks in
    order — so a single 32-byte status frame can be delivered across two recv calls (e.g. 20 then
    12 bytes), exercising NetworkTransport's frame-assembly buffer.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__(b"")
        self._chunks = list(chunks)

    def recv(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)[:n]


def test_network_send_assembles_frame_split_across_recv_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single 32-byte completion frame split 20+12 across two recv calls must decode as one
    frame. recv(32) is not guaranteed to return a whole frame — TCP can fragment it — so the
    transport must accumulate bytes before parsing rather than decode a short chunk."""
    completed = _PRINTING_COMPLETED_PACKET
    waiting = _PHASE_WAITING_PACKET
    chunks = [completed[:20], completed[20:] + waiting[:8], waiting[8:]]
    fake = _ChunkedSocket(chunks)
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert isinstance(status, PrinterStatus)
    assert status.ok is True, f"a fragmented completion sequence must decode as ok; got {status}"
    assert status.errors == []


def test_network_send_surfaces_error_frame_delivered_fragmented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean frame followed by a cover-open error frame, both delivered in fragmented chunks that
    don't align to frame boundaries, must still surface ok=False. The error frame must not be
    dropped because its bytes straddled two recv calls."""
    clean = _REPLY_TO_REQUEST_PACKET
    err = _COVER_OPEN_PACKET
    stream = clean + err
    # Fragment at offsets that cut through the middle of both frames.
    chunks = [stream[:10], stream[10:40], stream[40:]]
    fake = _ChunkedSocket(chunks)
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is not None
    assert status.ok is False, "a fragmented later error frame must not be dropped"
    assert any("Cover opened" in e for e in status.errors), (
        f"expected a cover-open error string; got {status.errors}"
    )


def test_network_send_returns_none_when_completion_never_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the benign 'Reply to status request' frame arrives (no error, no completion), then the
    stream closes → None ('state unknown'), so a quiet printer doesn't fail an otherwise-fine job."""
    fake = _FakeSocket([_REPLY_TO_REQUEST_PACKET])
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is None, "a clean-but-incomplete exchange must be indeterminate, not OK or failed"


def test_network_send_returns_none_on_silent_printer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No reply (empty recv) → None ('state unknown'), not a fabricated failure."""
    fake = _FakeSocket(b"")
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is None, "a silent back-channel must not be treated as an error"
    assert fake.sent == b"x", "the page is still sent even when no status comes back"


def test_network_send_returns_none_on_garbled_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply missing the 80:20:42 header is unparseable → None, not a hard failure."""
    fake = _FakeSocket(b"\x00" * STATUS_PACKET_LEN)  # wrong header → interpret_response raises
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is None


def test_network_send_returns_none_when_deadline_exhausted_by_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A printer that only ever times out → None once the OVERALL deadline is exhausted.

    Each recv times out (a real printer can be momentarily slow), so the transport keeps polling;
    only when the whole STATUS_READ_DEADLINE budget is spent without a completion/error frame does
    it give up with None. We advance a fake monotonic clock per recv so the deadline is reached
    deterministically instead of busy-looping for the real ~10s budget.
    """
    import app.transports.network as net_mod

    clock = {"t": 0.0}
    monkeypatch.setattr(net_mod.time, "monotonic", lambda: clock["t"])

    class _TimingOutSocket(_FakeSocket):
        def recv(self, n: int) -> bytes:
            clock["t"] += net_mod.STATUS_READ_TIMEOUT  # advance the budget by one poll interval
            raise TimeoutError("status read timed out")

    fake = _TimingOutSocket(b"")
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is None, "an exhausted deadline of pure timeouts must yield None, not a failure"


def test_network_send_decodes_complete_frame_buffered_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMPLETE error frame that arrives on the very recv that pushes the clock past the OVERALL
    deadline must still be decoded → ok=False, NOT discarded as None.

    The loop checked the deadline at the top, BEFORE draining frames already buffered. A recv that
    appended a full cover-open frame right as STATUS_READ_DEADLINE expired was then dropped — the
    next iteration broke on the deadline and returned None, so main.py recorded a real printer error
    as a successful print. The fix drains complete buffered frames before the deadline break, so the
    error verdict survives. Contrast with the timeouts test above, where no complete frame is ever
    buffered and None remains the correct outcome.
    """
    import app.transports.network as net_mod

    clock = {"t": 0.0}
    monkeypatch.setattr(net_mod.time, "monotonic", lambda: clock["t"])

    class _ErrorAtDeadlineSocket(_FakeSocket):
        def recv(self, n: int) -> bytes:
            # This recv both delivers a full error frame AND exhausts the overall deadline, so the
            # next loop iteration would break on the deadline if the frame weren't drained first.
            clock["t"] += net_mod.STATUS_READ_DEADLINE + 1
            return super().recv(n)

    fake = _ErrorAtDeadlineSocket([_COVER_OPEN_PACKET])
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is not None, (
        "a complete frame buffered at the deadline must not be dropped as None"
    )
    assert status.ok is False, "the buffered error frame must be decoded before the deadline break"
    assert any("Cover opened" in e for e in status.errors), (
        f"expected a cover-open error string from the deadline-boundary frame; got {status.errors}"
    )


def test_network_send_propagates_connect_failure_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused/unreachable connection propagates to the caller (main.py maps transport exceptions
    to a failed job) — send() must not swallow it into a None 'state unknown' — and the socket is
    still closed by the finally block, with nothing ever sent."""

    class _RefusingSocket(_FakeSocket):
        def connect(self, addr: tuple[str, int]) -> None:
            raise ConnectionRefusedError("connection refused")

    fake = _RefusingSocket(b"")
    _patch_socket(monkeypatch, fake)

    with pytest.raises(ConnectionRefusedError):
        NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert fake.sent == b"", "nothing must be sent when the connection never opened"
    assert fake.closed is True, "the socket must be closed even when connect() fails"


def test_network_close_closes_cached_socket_and_is_idempotent() -> None:
    """close() releases a cached socket exactly once: the handle is closed, the reference cleared,
    and a second close() is a no-op instead of double-closing."""
    transport = NetworkTransport("tcp://192.168.1.50:9100")
    transport.close()  # no cached socket yet — must be a silent no-op

    fake = _FakeSocket(b"")
    transport._sock = fake  # type: ignore[assignment]
    transport.close()

    assert fake.closed is True
    assert transport._sock is None, "the cached socket reference must be cleared after close()"
    transport.close()  # idempotent: nothing left to close


def test_network_send_returns_none_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine connection error (reset/broken pipe) on recv → None ('state unknown'), and it must
    NOT be swallowed by the socket-timeout branch — the back-channel is gone, stop polling."""

    class _ResettingSocket(_FakeSocket):
        def recv(self, n: int) -> bytes:
            raise ConnectionResetError("connection reset by peer")

    fake = _ResettingSocket(b"")
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is None, "a connection reset must return None without spinning to the deadline"


def test_network_send_keeps_polling_past_first_timeout_until_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow printer that times out ONCE before emitting its completion sequence must NOT be
    abandoned on that first per-read timeout. The transport must keep polling within the overall
    deadline and surface ok=True from the later completion frames."""

    class _SlowThenCompletingSocket(_FakeSocket):
        """recv raises socket.timeout once, then yields the configured completion frames."""

        def __init__(self, reply: list[bytes]) -> None:
            super().__init__(reply)
            self._raised = False

        def recv(self, n: int) -> bytes:
            if not self._raised:
                self._raised = True
                raise TimeoutError("printer still working on the page")
            return super().recv(n)

    fake = _SlowThenCompletingSocket(list(_OK_SEQUENCE))
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert isinstance(status, PrinterStatus), "a single early timeout must not abandon the read"
    assert status.ok is True, f"completion after a timeout must surface ok=True; got {status}"
    assert status.errors == []


def test_network_send_keeps_polling_past_first_timeout_until_late_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow printer that times out once and THEN reports a cover-open error must surface ok=False,
    not None: the late error frame must not be dropped by the first per-read timeout."""

    class _SlowThenErroringSocket(_FakeSocket):
        def __init__(self, reply: list[bytes]) -> None:
            super().__init__(reply)
            self._raised = False

        def recv(self, n: int) -> bytes:
            if not self._raised:
                self._raised = True
                raise TimeoutError("printer still working on the page")
            return super().recv(n)

    fake = _SlowThenErroringSocket([_REPLY_TO_REQUEST_PACKET, _COVER_OPEN_PACKET])
    _patch_socket(monkeypatch, fake)

    status = NetworkTransport("tcp://192.168.1.50:9100").send(b"x")

    assert status is not None, "a late error after a timeout must not be lost as None"
    assert status.ok is False, "the late cover-open error must surface ok=False"
    assert any("Cover opened" in e for e in status.errors), (
        f"expected a cover-open error string; got {status.errors}"
    )


# ── file:// transport reports a synthetic OK (no printer behind it) ──────────────
def test_file_transport_send_returns_synthetic_ok(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out.bin"
    status = FileTransport(f"file://{out}").send(b"RASTER")

    assert isinstance(status, PrinterStatus)
    assert status.ok is True, "file sink has no printer, so it must report a clean OK"
    assert out.read_bytes() == b"RASTER"


# ── USBTransport maps brother_ql's helper status dict → PrinterStatus ────────────
# brother_ql.backends.helpers.send returns a dict (it does NOT raise on a printer error); the
# helper does the USB readback loop internally. USBTransport must map that dict so out-of-media /
# cover-open fails the job instead of recording a phantom print. We patch the helper since there is
# no USB device in CI.
def _patch_usb_helper(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, object]
) -> dict[str, object]:
    """Patch brother_ql.backends.helpers.send to return ``result`` and capture its kwargs."""
    import app.transports.usb as usb_mod

    captured: dict[str, object] = {}

    def fake_send(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return result

    # USBTransport imports send inside the method, so patch it on the source module.
    import brother_ql.backends.helpers as helpers_mod

    monkeypatch.setattr(helpers_mod, "send", fake_send)
    # Defensive: also bind on the usb module namespace in case it ever imports at module scope.
    if hasattr(usb_mod, "send"):
        monkeypatch.setattr(usb_mod, "send", fake_send, raising=False)
    return captured


def test_usb_send_maps_error_dict_to_failed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """outcome='error' with printer_state.errors → PrinterStatus(ok=False) carrying those errors."""
    from app.transports.usb import USBTransport

    captured = _patch_usb_helper(
        monkeypatch,
        {
            "outcome": "error",
            "printer_state": {"errors": ["No media when printing"]},
            "did_print": False,
            "ready_for_next_job": False,
        },
    )

    status = USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    assert isinstance(status, PrinterStatus)
    assert status.ok is False, "a USB printer error must fail the job, not record a phantom print"
    assert any("No media" in e for e in status.errors), f"errors not surfaced; got {status.errors}"
    assert captured.get("blocking") is True, "blocking=True is required for the readback loop"


def test_usb_send_maps_printed_dict_to_ok_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """outcome='printed' with did_print and ready_for_next_job → PrinterStatus(ok=True)."""
    from app.transports.usb import USBTransport

    _patch_usb_helper(
        monkeypatch,
        {
            "outcome": "printed",
            "printer_state": {"errors": [], "status_type": "Printing completed"},
            "did_print": True,
            "ready_for_next_job": True,
        },
    )

    status = USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    assert isinstance(status, PrinterStatus)
    assert status.ok is True
    assert status.errors == []


def test_usb_send_returns_none_on_indeterminate_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """outcome='sent' with no printer_state (backend without readback) → None ('state unknown')."""
    from app.transports.usb import USBTransport

    _patch_usb_helper(
        monkeypatch,
        {
            "outcome": "sent",
            "printer_state": None,
            "did_print": False,
            "ready_for_next_job": False,
        },
    )

    status = USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    assert status is None, "an indeterminate readback must not fail (or fabricate success on) a job"


# ── main._execute_print maps a printer error → failed job + error metric ─────────
class _FakeTransport:
    """A transport whose send() returns a preset PrinterStatus, for the print-flow integration."""

    _status: PrinterStatus | None = None

    def __init__(self, uri: str) -> None:
        self._uri = uri

    def send(self, data: bytes) -> PrinterStatus | None:
        return type(self)._status

    def close(self) -> None:
        pass


def _make_error_transport(status: PrinterStatus | None) -> type[_FakeTransport]:
    return type("_BoundFakeTransport", (_FakeTransport,), {"_status": status})


def _label_errors_count(reason: str) -> float:
    import app.main as main_mod

    return main_mod.LABEL_ERRORS.labels(reason=reason)._value.get()


def _labels_printed_count(template: str, dry_run: bool) -> float:
    import app.main as main_mod

    return main_mod.LABELS_PRINTED.labels(template=template, dry_run=str(dry_run))._value.get()


def test_print_marks_job_failed_on_printer_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A printer-reported error fails the job, increments label_errors_total{reason=printer_error},
    and must NOT increment labels_printed_total."""
    import app.main as main_mod

    err_status = PrinterStatus(ok=False, errors=["No media when printing"])
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _make_error_transport(err_status))

    errors_before = _label_errors_count("printer_error")
    printed_before = _labels_printed_count("simple", dry_run=False)

    resp = client.post("/print", json={"template": "simple", "fields": {"title": "Hi"}})

    assert resp.status_code == 502, f"printer error should be a 502; got {resp.status_code}"
    assert "No media" in resp.json()["detail"]
    assert _label_errors_count("printer_error") == errors_before + 1, (
        "label_errors_total{reason=printer_error} must increment exactly once"
    )
    assert _labels_printed_count("simple", dry_run=False) == printed_before, (
        "labels_printed_total must NOT increment when the printer reported an error"
    )

    history = client.get("/history/list").json()["entries"]
    assert history[0]["status"] == "failed", "the job must be recorded as failed, not printed"


def test_print_succeeds_on_ok_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean printer status records a normal printed job and the printed metric increments."""
    import app.main as main_mod

    monkeypatch.setattr(
        main_mod, "_resolve_transport", lambda: _make_error_transport(PrinterStatus(ok=True))
    )

    printed_before = _labels_printed_count("simple", dry_run=False)
    errors_before = _label_errors_count("printer_error")

    resp = client.post("/print", json={"template": "simple", "fields": {"title": "Hi"}})

    assert resp.status_code == 200, f"clean status should succeed; got {resp.json()}"
    assert _labels_printed_count("simple", dry_run=False) == printed_before + 1
    assert _label_errors_count("printer_error") == errors_before, (
        "no printer_error must be counted on a clean status"
    )
    history = client.get("/history/list").json()["entries"]
    assert history[0]["status"] == "printed"


def test_print_succeeds_on_none_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A None status (transport can't read state, e.g. USB) is treated as 'no error reported'."""
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: _make_error_transport(None))

    resp = client.post("/print", json={"template": "simple", "fields": {"title": "Hi"}})

    assert resp.status_code == 200, f"None status must not fail the job; got {resp.json()}"
    history = client.get("/history/list").json()["entries"]
    assert history[0]["status"] == "printed"


def test_print_marks_job_failed_on_usb_printer_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real USBTransport: a USB printer error (mapped from brother_ql's helper dict)
    must record the job failed, emit label_errors_total{reason=printer_error}, and NOT increment
    labels_printed_total — proving the helper's status dict reaches the print-flow error handling."""
    import app.main as main_mod
    from app.transports.usb import USBTransport

    _patch_usb_helper(
        monkeypatch,
        {
            "outcome": "error",
            "printer_state": {"errors": ["No media when printing"]},
            "did_print": False,
            "ready_for_next_job": False,
        },
    )
    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: USBTransport)
    monkeypatch.setattr(main_mod.settings, "printer_uri", "usb://0x04f9:0x209c")
    # The USB print preflight now reads device status first; stub it to fail open so this test isolates
    # send()'s error handling and never touches a real attached device.
    monkeypatch.setattr(
        main_mod,
        "_query_printer_status",
        lambda request: PrinterStatus.unreachable("status stubbed for send-path isolation"),
    )

    errors_before = _label_errors_count("printer_error")
    printed_before = _labels_printed_count("simple", dry_run=False)

    resp = client.post("/print", json={"template": "simple", "fields": {"title": "Hi"}})

    assert resp.status_code == 502, f"USB printer error should be a 502; got {resp.status_code}"
    assert "No media" in resp.json()["detail"]
    assert _label_errors_count("printer_error") == errors_before + 1, (
        "label_errors_total{reason=printer_error} must increment exactly once"
    )
    assert _labels_printed_count("simple", dry_run=False) == printed_before, (
        "labels_printed_total must NOT increment on a USB printer error"
    )
    history = client.get("/history/list").json()["entries"]
    assert history[0]["status"] == "failed", "the USB-error job must be recorded as failed"


# ── USBTransport enforces a hard timeout via a worker thread ──────────────────
def test_usb_send_raises_timeout_error_when_helper_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """helpers.send blocking longer than USB_TIMEOUT raises USBTimeoutError.

    We patch USB_TIMEOUT to 0.05 s so the test runs in ~50 ms, then make the helper sleep
    longer than that so the join expires before the worker completes.
    """
    import time

    import brother_ql.backends.helpers as helpers_mod

    import app.transports.usb as usb_mod
    from app.transports.usb import USBTimeoutError, USBTransport

    monkeypatch.setattr(usb_mod, "USB_TIMEOUT", 0.05)

    def _blocking_send(**kwargs: object) -> dict[str, object]:
        time.sleep(5)  # far longer than the 0.05 s test timeout
        return {
            "outcome": "printed",
            "printer_state": None,
            "did_print": True,
            "ready_for_next_job": True,
        }

    monkeypatch.setattr(helpers_mod, "send", _blocking_send)
    if hasattr(usb_mod, "send"):
        monkeypatch.setattr(usb_mod, "send", _blocking_send, raising=False)

    start = time.monotonic()
    with pytest.raises(USBTimeoutError, match="timed out"):
        USBTransport("usb://0x04f9:0x209c").send(b"RASTER")
    elapsed = time.monotonic() - start

    # Must have raised within a generous window around the fake timeout (not waited for the 5 s sleep).
    assert elapsed < 1.0, f"timeout should have fired near 0.05 s, but took {elapsed:.2f} s"


def test_usb_send_fast_path_returns_mapped_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """When helpers.send completes within USB_TIMEOUT the normal status-mapping path runs.

    Using a generous patched timeout so the fast helper is never racing.
    """
    import app.transports.usb as usb_mod
    from app.transports.usb import USBTransport

    monkeypatch.setattr(usb_mod, "USB_TIMEOUT", 10)

    _patch_usb_helper(
        monkeypatch,
        {
            "outcome": "printed",
            "printer_state": {"errors": [], "status_type": "Printing completed"},
            "did_print": True,
            "ready_for_next_job": True,
        },
    )

    status = USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    assert isinstance(status, PrinterStatus)
    assert status.ok is True
    assert status.errors == []


def test_usb_timeout_records_job_failed_and_emits_print_error_metric(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A USB timeout propagates through _execute_print: the job is recorded failed and
    label_errors_total{reason=print_error} increments (USBTimeoutError is caught by the generic
    transport Exception handler in main.py, matching the existing 'print_error' reason)."""
    import time

    import brother_ql.backends.helpers as helpers_mod

    import app.main as main_mod
    import app.transports.usb as usb_mod
    from app.transports.usb import USBTransport

    monkeypatch.setattr(usb_mod, "USB_TIMEOUT", 0.05)

    def _blocking_send(**kwargs: object) -> dict[str, object]:
        time.sleep(5)
        return {
            "outcome": "printed",
            "printer_state": None,
            "did_print": True,
            "ready_for_next_job": True,
        }

    monkeypatch.setattr(helpers_mod, "send", _blocking_send)
    if hasattr(usb_mod, "send"):
        monkeypatch.setattr(usb_mod, "send", _blocking_send, raising=False)

    monkeypatch.setattr(main_mod, "_resolve_transport", lambda: USBTransport)
    monkeypatch.setattr(main_mod.settings, "printer_uri", "usb://0x04f9:0x209c")
    # The USB print preflight now reads device status first; stub it to fail open so this test isolates
    # send()'s timeout behavior (USB_TIMEOUT is set tiny here for send, which would otherwise make the
    # real preflight query orphan and mask the timeout under test) and never touches a real device.
    monkeypatch.setattr(
        main_mod,
        "_query_printer_status",
        lambda request: PrinterStatus.unreachable("status stubbed for send-path isolation"),
    )

    errors_before = _label_errors_count("print_error")
    printed_before = _labels_printed_count("simple", dry_run=False)

    resp = client.post("/print", json={"template": "simple", "fields": {"title": "Hi"}})

    assert resp.status_code == 500, f"USB timeout should be a 500; got {resp.status_code}"
    assert "timed out" in resp.json()["detail"].lower()
    assert _label_errors_count("print_error") == errors_before + 1, (
        "label_errors_total{reason=print_error} must increment exactly once on USB timeout"
    )
    assert _labels_printed_count("simple", dry_run=False) == printed_before, (
        "labels_printed_total must NOT increment on a USB timeout"
    )
    history = client.get("/history/list").json()["entries"]
    assert history[0]["status"] == "failed", "the timed-out USB job must be recorded as failed"


# ── USB busy-lock guard prevents competing transfers after a timeout ──────────────
def test_usb_busy_error_raised_when_prior_worker_still_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second USB send attempted while the first worker is still stuck (orphaned by timeout)
    must raise USBBusyError immediately WITHOUT spawning a second helpers.send call.

    Sequence:
      1. Monkeypatch helpers.send to block until a 'release' event is set (simulates stuck transfer).
      2. USBTransport.send with tiny USB_TIMEOUT → USBTimeoutError; worker thread still running.
      3. Second USBTransport.send → USBBusyError, helpers.send invoked only once in total.
      4. Set the release event so the orphaned worker can finish and release _USB_DEVICE_LOCK.

    Module-level USB state (_usb_busy, _USB_DEVICE_LOCK) is reset by the _reset_usb_module_state
    autouse fixture in conftest.py so this test starts with a clean device.
    """
    import brother_ql.backends.helpers as helpers_mod

    import app.transports.usb as usb_mod
    from app.transports.usb import USBBusyError, USBTimeoutError, USBTransport

    monkeypatch.setattr(usb_mod, "USB_TIMEOUT", 0.05)

    release_event = __import__("threading").Event()
    call_count = 0

    def _blocking_send(**kwargs: object) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        release_event.wait()  # blocks until the test signals release
        return {
            "outcome": "printed",
            "printer_state": None,
            "did_print": True,
            "ready_for_next_job": True,
        }

    monkeypatch.setattr(helpers_mod, "send", _blocking_send)
    if hasattr(usb_mod, "send"):
        monkeypatch.setattr(usb_mod, "send", _blocking_send, raising=False)

    # First send — times out; worker is now orphaned and still inside _blocking_send.
    with pytest.raises(USBTimeoutError, match="timed out"):
        USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    # _usb_busy must be True: the orphaned worker set it and has not cleared it yet.
    assert usb_mod._usb_busy is True, "_usb_busy must remain True while the worker is stuck"

    # Second send — must raise USBBusyError immediately, NOT call helpers.send a second time.
    with pytest.raises(USBBusyError, match="busy"):
        USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    assert call_count == 1, (
        f"helpers.send must have been called exactly once; got {call_count} calls — "
        "a second call means a competing transfer was started against the stuck device"
    )

    # Release the orphaned worker so it can exit cleanly (avoids daemon-thread resource leak).
    release_event.set()
    # Give the worker a moment to clear _usb_busy in its finally block.
    import time

    deadline = time.monotonic() + 2.0
    while usb_mod._usb_busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert usb_mod._usb_busy is False, "_usb_busy must be cleared once the orphaned worker exits"


def test_usb_send_succeeds_after_orphaned_worker_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a stuck worker finishes and releases _USB_DEVICE_LOCK, the next USB send succeeds.

    This verifies no deadlock: the busy guard must not permanently poison the device.

    Module-level USB state (_usb_busy, _USB_DEVICE_LOCK) is reset by the _reset_usb_module_state
    autouse fixture in conftest.py so this test starts with a clean device.
    """
    import time

    import brother_ql.backends.helpers as helpers_mod

    import app.transports.usb as usb_mod
    from app.transports.usb import USBTransport

    monkeypatch.setattr(usb_mod, "USB_TIMEOUT", 0.05)

    release_event = __import__("threading").Event()

    # Phase 1: blocking send that times out.
    def _blocking_send(**kwargs: object) -> dict[str, object]:
        release_event.wait()
        return {
            "outcome": "printed",
            "printer_state": None,
            "did_print": True,
            "ready_for_next_job": True,
        }

    monkeypatch.setattr(helpers_mod, "send", _blocking_send)
    if hasattr(usb_mod, "send"):
        monkeypatch.setattr(usb_mod, "send", _blocking_send, raising=False)

    with pytest.raises(usb_mod.USBTimeoutError):
        USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    # Unblock the orphaned worker and wait for it to release the lock.
    release_event.set()
    deadline = time.monotonic() + 2.0
    while usb_mod._usb_busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert usb_mod._usb_busy is False, "device must be free after orphaned worker exits"

    # Phase 2: fast helper that completes immediately — device should be available again.
    def _fast_send(**kwargs: object) -> dict[str, object]:
        return {
            "outcome": "printed",
            "printer_state": {"errors": [], "status_type": "Printing completed"},
            "did_print": True,
            "ready_for_next_job": True,
        }

    monkeypatch.setattr(helpers_mod, "send", _fast_send)
    if hasattr(usb_mod, "send"):
        monkeypatch.setattr(usb_mod, "send", _fast_send, raising=False)

    from app.transports.base import PrinterStatus

    monkeypatch.setattr(usb_mod, "USB_TIMEOUT", 10)
    status = USBTransport("usb://0x04f9:0x209c").send(b"RASTER")

    assert isinstance(status, PrinterStatus), "send after device released must return PrinterStatus"
    assert status.ok is True, "post-release send must succeed"
    assert usb_mod._usb_busy is False, "device lock must be released after a successful send"


# ── query_status() — NetworkTransport ────────────────────────────────────────────
# A minimal placeholder for tests that exercise error/timeout paths and don't care what bytes
# were sent. Real call sites (main.py) build the full library-generated payload.
_DUMMY_STATUS_REQUEST = b"\x00" * 400 + b"\x1b\x69\x53"


def _status_packet_with_model(
    model_code: int = 0x38,  # 0x38 = QL-800
    media_width: int = 62,
    media_length: int = 0,
    media_type: int = 0x0A,  # continuous tape
    err1: int = 0x00,
    err2: int = 0x00,
) -> bytes:
    """Build a 32-byte status packet with configurable media/model for query_status tests."""
    pkt = bytearray(STATUS_PACKET_LEN)
    pkt[0], pkt[1], pkt[2] = 0x80, 0x20, 0x42
    pkt[4] = model_code
    pkt[8] = err1
    pkt[9] = err2
    pkt[10] = media_width
    pkt[11] = media_type
    pkt[13] = media_length
    return bytes(pkt)


def _build_status_request(model: str) -> bytes:
    """Build the full model-correct status-request payload via brother_ql.

    This is the invalidate (NUL) prefix + ESC i S command, exactly as BrotherQLRaster builds it.
    Used in tests to assert the bytes sent by query_status() match the library-built sequence.
    """
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(model)
    qlr.add_invalidate()
    qlr.add_status_information()
    return qlr.data


def test_network_query_status_snmp_disabled_returns_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With SNMP disabled there is no TCP-native status channel on this hardware — the QL-810W NIC
    never returns the ESC i S frame, so the standalone TCP query was removed. query_status returns
    reachable=False directly, and must NOT open a socket or fabricate ok=True.

    (An operator who genuinely wants a working ESC i S status channel uses a usb:// transport, whose
    back-channel does answer — see the USB query_status tests below.)"""
    import app.transports.network as net_mod

    _disable_snmp(monkeypatch)
    # No status channel exists on this path: opening a TCP socket would be a bug.
    monkeypatch.setattr(
        net_mod.socket,
        "socket",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("SNMP-disabled query_status must not open a TCP socket")
        ),
    )

    status = NetworkTransport("tcp://192.168.1.50:9100").query_status(_DUMMY_STATUS_REQUEST)

    assert status.reachable is False, "no status channel ⇒ reachable=False"
    assert status.ok is False, "unreachable must not be ok"
    assert status.errors, "must carry a reason explaining the absent status channel"


# ── query_status() — NetworkTransport SNMP path (default) ─────────────────────────
# A reachable QL-810W as the SNMP layer would decode it: 62mm continuous, clean, fully identified.
_SNMP_REACHABLE = PrinterSNMPStatus(
    reachable=True,
    model="Brother QL-810W",
    serial="B2Z160525",
    hostname="BRWF889D22FBB15",
    console_text="READY",
    error_state_bits=0,
    printer_status=3,
    media_name='62mm / 2.4"',
    media_width_mm=62.0,
    media_length_mm=None,
    media_type="continuous",
    cover_status=3,
    label_lifecount=9,
    errors=[],
)


def test_network_query_status_uses_snmp_and_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """With SNMP enabled (the default), query_status reads the printer over SNMP and maps the
    decoded identity/media/error fields onto PrinterStatus — and must NOT open the TCP back-channel
    (the ESC i S request bytes are ignored on the SNMP path)."""
    import app.transports.network as net_mod

    captured = _patch_snmp(monkeypatch, _SNMP_REACHABLE)
    # The SNMP path must not touch the TCP socket at all: make any socket creation an error.
    monkeypatch.setattr(
        net_mod.socket,
        "socket",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("SNMP path must not open a TCP socket")
        ),
    )

    status = NetworkTransport("tcp://192.168.5.14:9100").query_status(_DUMMY_STATUS_REQUEST)

    assert captured["host"] == "192.168.5.14", "SNMP must be queried against the transport's host"
    assert status.reachable is True
    assert status.ok is True, f"a clean SNMP status has no errors; got {status.errors}"
    assert status.model == "Brother QL-810W"
    assert status.media_width_mm == 62.0, "SNMP media width flows through as a float (not rounded)"
    assert status.media_type == "continuous"
    assert status.serial == "B2Z160525"
    assert status.hostname == "BRWF889D22FBB15"
    assert status.console_text == "READY"
    assert status.cover_status == 3
    assert status.label_lifecount == 9
    # status/phase are ESC i S concepts with no SNMP analogue.
    assert status.status_type is None and status.phase_type is None
    # The error bitmask, hrPrinterStatus enum and loaded-media name ride in raw, losslessly.
    assert status.raw["error_state_bits"] == 0
    assert status.raw["printer_status"] == 3
    assert status.raw["media_name"] == '62mm / 2.4"'


def test_network_query_status_snmp_unreachable_maps_to_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable SNMP agent (timeout / decode error) → reachable=False, ok=False, with a
    descriptive error — so the caller fails open / returns 503, never a fabricated healthy status."""
    _patch_snmp(monkeypatch, PrinterSNMPStatus.unreachable())

    status = NetworkTransport("tcp://192.168.5.14:9100").query_status(_DUMMY_STATUS_REQUEST)

    assert status.reachable is False, "an unreachable SNMP agent must yield reachable=False"
    assert status.ok is False
    assert status.errors, "at least one error string must describe the failure"


def test_network_query_status_snmp_error_state_surfaces_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable printer reporting an error condition (e.g. cover/door open) → reachable=True but
    ok=False, carrying the SNMP error strings — the printer answered, but it is faulted."""
    faulted = PrinterSNMPStatus(
        reachable=True,
        model="Brother QL-810W",
        error_state_bits=1 << 11,  # arbitrary set bit
        media_width_mm=62.0,
        media_type="continuous",
        errors=["doorOpen"],
    )
    _patch_snmp(monkeypatch, faulted)

    status = NetworkTransport("tcp://192.168.5.14:9100").query_status(_DUMMY_STATUS_REQUEST)

    assert status.reachable is True, "a printer that answered SNMP is reachable even when faulted"
    assert status.ok is False, "a nonzero error state must surface as ok=False"
    assert "doorOpen" in status.errors


def test_network_query_status_snmp_uses_configured_community_port_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_status forwards the configured SNMP community / port / timeout to query_snmp_status,
    not hardcoded defaults — so SNMP_COMMUNITY / SNMP_PORT / SNMP_TIMEOUT actually take effect."""
    import app.transports.network as net_mod

    captured = _patch_snmp(monkeypatch, _SNMP_REACHABLE)
    monkeypatch.setattr(net_mod.settings, "snmp_community", "private")
    monkeypatch.setattr(net_mod.settings, "snmp_port", 1161)
    monkeypatch.setattr(net_mod.settings, "snmp_timeout", 5.5)

    NetworkTransport("tcp://192.168.5.14:9100").query_status(_DUMMY_STATUS_REQUEST)

    assert captured["community"] == "private"
    assert captured["port"] == 1161
    assert captured["timeout"] == 5.5


def test_network_query_status_snmp_preserves_fractional_media_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A die-cut roll's float width/length from SNMP flow through to PrinterStatus UNROUNDED.

    Rounding here would let the web UI (which reads these via /printer/status) disagree with the
    server-side media guard (which compares the same unrounded floats with a ±1mm tolerance) at the
    tolerance boundary. A fractional value proves the precision is preserved, not rounded to int."""
    die_cut = PrinterSNMPStatus(
        reachable=True,
        model="Brother QL-810W",
        media_width_mm=62.4,
        media_length_mm=29.6,
        media_type="die_cut",
        errors=[],
    )
    _patch_snmp(monkeypatch, die_cut)

    status = NetworkTransport("tcp://192.168.5.14:9100").query_status(_DUMMY_STATUS_REQUEST)

    assert status.media_width_mm == 62.4, "width must keep full precision (no rounding to 62)"
    assert status.media_length_mm == 29.6, "length must keep full precision (no rounding to 30)"
    assert status.media_type == "die_cut"


# ── query_status() — FileTransport ───────────────────────────────────────────────


def test_file_transport_query_status_returns_synthetic_ok(tmp_path: pytest.TempPathFactory) -> None:
    """FileTransport has no printer — query_status returns synthetic ok with reachable=False."""
    out = tmp_path / "out.bin"  # type: ignore[operator]
    status = FileTransport(f"file://{out}").query_status(_DUMMY_STATUS_REQUEST)

    assert isinstance(status, PrinterStatus)
    assert status.reachable is False, "file sink has no printer, so reachable must be False"
    assert status.ok is True, "file sink reports synthetic OK (not an error)"
    assert status.model is None, "no model from a file sink"
    assert status.errors == []


# ── query_status() — USBTransport ────────────────────────────────────────────────


def _patch_usb_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frame: bytes | None = None,
    raise_on_init: BaseException | None = None,
) -> dict[str, object]:
    """Patch brother_ql's pyusb backend with a fake that returns ``frame`` from ``_read`` (or nothing
    when ``frame`` is None), captures the written request, and records disposal. Returns that state."""
    import brother_ql.backends.pyusb as pyusb_mod

    state: dict[str, object] = {"written": b"", "disposed": False}

    class _FakeBackend:
        def __init__(self, uri: str) -> None:
            if raise_on_init is not None:
                raise raise_on_init
            self.read_timeout = 10.0

        def _write(self, data: bytes) -> None:
            state["written"] = bytes(data)

        def _read(self, length: int = 32) -> bytes:
            return frame if frame is not None else b""

        def _dispose(self) -> None:
            state["disposed"] = True

    monkeypatch.setattr(pyusb_mod, "BrotherQLBackendPyUSB", _FakeBackend)
    return state


def test_usb_transport_query_status_reads_and_parses_esc_i_s_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_status writes the model-correct request over USB and parses the 32-byte ESC i S reply
    into canonical media/model fields — the USB back-channel answers a standalone status request
    (unlike the network NIC). media_type must be the canonical 'continuous', not brother_ql's raw
    'Continuous length tape'."""
    from app.transports.usb import USBTransport

    frame = _status_packet_with_model(media_width=62, media_type=0x0A)  # continuous
    state = _patch_usb_backend(monkeypatch, frame=frame)

    request = _build_status_request("QL-810W")
    status = USBTransport("usb://0x04f9:0x209c").query_status(request)

    assert status.reachable is True, "a printer that replies over USB is reachable"
    assert status.ok is True, f"a clean frame has no errors; got {status.errors}"
    assert status.model == "QL-800", f"model parsed from the frame; got {status.model!r}"
    assert status.media_width_mm == 62
    assert status.media_type == "continuous", (
        f"USB media_type must be canonical, not the raw brother_ql string; got {status.media_type!r}"
    )
    assert state["written"] == request, "the model-correct request must be written to the device"
    assert state["disposed"] is True, "the device must be disposed after the query"


def test_usb_transport_query_status_returns_unreachable_when_device_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a USB print is in progress (_usb_busy=True), query_status fast-fails to reachable=False
    with a 'busy' message rather than opening a competing device handle."""
    import app.transports.usb as usb_mod
    from app.transports.usb import USBTransport

    monkeypatch.setattr(usb_mod, "_usb_busy", True)

    status = USBTransport("usb://0x04f9:0x209c").query_status(_DUMMY_STATUS_REQUEST)

    assert status.reachable is False
    assert status.ok is False
    assert any("busy" in e.lower() for e in status.errors), (
        f"error message should indicate the device is busy; got {status.errors}"
    )


def test_usb_transport_query_status_returns_unreachable_on_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend open failure (device not found / claim error) → reachable=False, never raises."""
    from app.transports.usb import USBTransport

    _patch_usb_backend(monkeypatch, raise_on_init=ValueError("Device not found"))

    status = USBTransport("usb://0x04f9:0x209c").query_status(_DUMMY_STATUS_REQUEST)

    assert status.reachable is False
    assert status.ok is False
    assert status.errors


def test_usb_transport_query_status_returns_unreachable_when_no_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A printer that opens but never returns a status frame → reachable=False within the deadline,
    and the device is still disposed."""
    import app.transports.usb as usb_mod
    from app.transports.usb import USBTransport

    # Shrink the drain budget so the test does not wait the full 5s real deadline.
    monkeypatch.setattr(usb_mod, "USB_STATUS_READ_DEADLINE_S", 0.05)
    monkeypatch.setattr(usb_mod, "USB_STATUS_READ_POLL_S", 0.001)
    state = _patch_usb_backend(monkeypatch, frame=None)  # _read always returns b""

    status = USBTransport("usb://0x04f9:0x209c").query_status(_DUMMY_STATUS_REQUEST)

    assert status.reachable is False
    assert status.ok is False
    assert state["disposed"] is True, "device must be disposed even when no frame arrives"


def test_usb_transport_query_status_retries_after_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A USB read TIMEOUT raises usb.core.USBError (the backend does not return b'' on no-data), so the
    frame reader must catch it and keep polling until the deadline — a frame that arrives after the
    first read timeout must still be read, NOT fail the guard open: a transient read timeout must not
    skip the media/fault check and let an unverified label print."""
    import brother_ql.backends.pyusb as pyusb_mod
    import usb.core

    from app.transports.usb import USBTransport

    frame = _status_packet_with_model(media_width=62, media_type=0x0A)
    calls = {"n": 0}

    class _FakeBackend:
        def __init__(self, uri: str) -> None:
            self.read_timeout = 10.0

        def _write(self, data: bytes) -> None:
            pass

        def _read(self, length: int = 32) -> bytes:
            calls["n"] += 1
            if calls["n"] == 1:
                raise usb.core.USBError("Operation timed out", errno=110)
            return frame

        def _dispose(self) -> None:
            pass

    monkeypatch.setattr(pyusb_mod, "BrotherQLBackendPyUSB", _FakeBackend)

    status = USBTransport("usb://0x04f9:0x209c").query_status(_DUMMY_STATUS_REQUEST)

    assert calls["n"] >= 2, "must retry the read after the first USB timeout, not give up"
    assert status.reachable is True, (
        "a frame arriving after a read timeout must be read, not fail-open"
    )
    assert status.ok is True
    assert status.media_type == "continuous"


def test_usb_transport_query_status_times_out_without_pinning_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged libusb call (here _write blocks) must NOT pin the caller: query_status runs the whole
    transaction in a worker joined with USB_TIMEOUT and returns unreachable promptly, so the caller can
    release _print_lock. Bounding only the frame-drain loop would let an
    open/write/dispose hang stall every later print."""
    import threading
    import time as _time

    import brother_ql.backends.pyusb as pyusb_mod

    import app.transports.usb as usb_mod
    from app.transports.usb import USBTransport

    monkeypatch.setattr(
        usb_mod, "USB_STATUS_TIMEOUT", 0.2
    )  # bound the status join tightly for the test
    frame = _status_packet_with_model(media_width=62, media_type=0x0A)
    release = threading.Event()

    class _HangingBackend:
        def __init__(self, uri: str) -> None:
            self.read_timeout = 10.0

        def _write(self, data: bytes) -> None:
            release.wait(30)  # simulate a wedged libusb transfer until the test releases it

        def _read(self, length: int = 32) -> bytes:
            return frame

        def _dispose(self) -> None:
            pass

    monkeypatch.setattr(pyusb_mod, "BrotherQLBackendPyUSB", _HangingBackend)

    try:
        start = _time.monotonic()
        status = USBTransport("usb://0x04f9:0x209c").query_status(_DUMMY_STATUS_REQUEST)
        elapsed = _time.monotonic() - start

        assert status.reachable is False, "a wedged USB status read must fail open (unreachable)"
        assert elapsed < 5.0, f"query_status must return promptly on a hang; took {elapsed:.1f}s"
        assert any("timed out" in e.lower() for e in status.errors), status.errors
    finally:
        # Unblock the orphaned worker so it releases _USB_DEVICE_LOCK / clears _usb_busy before the
        # next test (otherwise a later send()/query worker would block on the held device lock).
        release.set()
        deadline = _time.monotonic() + 2.0
        while usb_mod._usb_busy and _time.monotonic() < deadline:
            _time.sleep(0.01)
