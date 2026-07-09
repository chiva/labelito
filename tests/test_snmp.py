# SPDX-License-Identifier: GPL-3.0-or-later
"""SNMP client tests — golden BER wire fixtures, decode mapping, and the mocked UDP socket seam.

The default suite is fully mocked: no test here touches a real printer. The golden request/response
hex strings pin the BER edge cases the hand-rolled codec must get right — signed/negative INTEGER
(feed ``-1``), OID sub-ids > 127 (the Brother enterprise arc ``2435``), and long-form lengths
(the response body is > 127 bytes).
"""

from __future__ import annotations

import pytest

from app.transports import snmp
from app.transports.snmp import (
    OID_DEVICE_ID_1284,
    OID_HR_DEVICE_DESCR,
    OID_HR_PRINTER_DETECTED_ERROR_STATE,
    OID_HR_PRINTER_STATUS,
    OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT,
    OID_PRT_COVER_STATUS,
    OID_PRT_INPUT_MEDIA_DIM_FEED_DIR,
    OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR,
    OID_PRT_INPUT_MEDIA_NAME,
    OID_PRT_MARKER_LIFE_COUNT,
    OID_SERIAL,
    OID_SYS_NAME,
    PrinterSNMPStatus,
    SNMPError,
    build_snmp_status,
    query_snmp_status,
    snmp_get,
)

# ── Golden wire fixtures ──────────────────────────────────────────────────────────────
# A pinned request-id so the request bytes are deterministic. The OID set deliberately includes
# OID_DEVICE_ID_1284 (the Brother enterprise arc 2435) to exercise multi-byte OID sub-ids.
_PINNED_REQUEST_ID = 0x1A2B3C4D
_GOLDEN_REQUEST_OIDS = [
    OID_HR_PRINTER_DETECTED_ERROR_STATE,
    OID_PRT_INPUT_MEDIA_NAME,
    OID_PRT_INPUT_MEDIA_DIM_FEED_DIR,
    OID_DEVICE_ID_1284,
]
# Exact bytes the PDU builder must produce for the pinned request-id + OID set above.
_GOLDEN_REQUEST_HEX = (
    "306402010004067075626c6963a05702041a2b3c4d0201000201003049300f060b2b0601020119030501020105"
    "003010060c2b060102012b0802010c010105003010060c2b060102012b08020104010105003012060e2b0601040"
    "19303020309010107000500"
)

# A real-shaped GetResponse packet built from the doc's verified live values: error bitmask 0x00,
# console "READY", media name '62mm / 2.4"', xfeed 6200, feed -1 (signed negative), serial
# "B2Z160525", lifecount 9 (a Gauge32). The whole message body exceeds 127 bytes, so the outer
# SEQUENCE and PDU lengths use the BER long form.
_GOLDEN_RESPONSE_HEX = (
    "3081b702010004067075626c6963a281a902041a2b3c4d02010002010030819a3010060b2b060102011903050102"
    "010401003015060c2b060102012b10050102010104055245414459301b060c2b060102012b0802010c0101040b363"
    "26d6d202f20322e34223012060c2b060102012b080201050101020218383011060c2b060102012b0802010401010"
    "201ff3018060b2b060102012b0501011101040942325a3136303532353011060c2b060102012b0a0201040101420109"
)


def test_golden_request_bytes_match_for_pinned_request_id() -> None:
    """The PDU builder must emit the exact request bytes for a pinned request-id + OID set.

    Pins multi-byte OID sub-ids (the 2435 arc) and short/long-form length encoding in the request.
    """
    packet = snmp._build_get_request("public", _PINNED_REQUEST_ID, _GOLDEN_REQUEST_OIDS)
    assert packet.hex() == _GOLDEN_REQUEST_HEX, (
        "request bytes drifted from the golden fixture — a BER encoding change would silently "
        "produce a malformed datagram"
    )


def test_golden_response_decodes_to_exact_values() -> None:
    """The decoder must parse the hand-built live-shaped response to the exact varbind values.

    Pins signed negative INTEGER (feed -1), the Gauge32 application tag (lifecount), long-form
    lengths (body > 127 bytes), and OCTET STRING containing a quote char ('62mm / 2.4"').
    """
    response = bytes.fromhex(_GOLDEN_RESPONSE_HEX)
    values = snmp._parse_response(response, _PINNED_REQUEST_ID)

    assert values[OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT] == "READY"
    assert values[OID_PRT_INPUT_MEDIA_NAME] == '62mm / 2.4"'
    assert values[OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR] == 6200
    assert values[OID_PRT_INPUT_MEDIA_DIM_FEED_DIR] == -1, "negative INTEGER must decode signed"
    assert values[OID_SERIAL] == "B2Z160525"
    assert values[OID_PRT_MARKER_LIFE_COUNT] == 9, "Gauge32 application tag must decode as int"


def test_golden_response_maps_to_continuous_62mm_status() -> None:
    """End-to-end: golden response → PrinterSNMPStatus reflects 62mm continuous, ready, no errors."""
    response = bytes.fromhex(_GOLDEN_RESPONSE_HEX)
    values = snmp._parse_response(response, _PINNED_REQUEST_ID)
    status = build_snmp_status(values)

    assert status.reachable is True
    assert status.media_width_mm == 62.0
    assert status.media_length_mm is None
    assert status.media_type == "continuous"
    assert status.console_text == "READY"
    assert status.error_state_bits == 0
    assert status.serial == "B2Z160525"
    assert status.label_lifecount == 9
    assert status.errors == []


def test_response_request_id_mismatch_raises() -> None:
    """A response whose request-id does not echo the request must be rejected, not trusted."""
    response = bytes.fromhex(_GOLDEN_RESPONSE_HEX)
    with pytest.raises(SNMPError, match="request-id"):
        snmp._parse_response(response, _PINNED_REQUEST_ID + 1)


# ── BER primitive edge cases ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("value", "expected_hex"),
    [
        (0, "020100"),
        (127, "02017f"),
        (128, "02020080"),  # needs a leading 0x00 to keep the sign bit clear
        (-1, "0201ff"),
        (-128, "020180"),
        (-129, "0202ff7f"),
        (6200, "02021838"),
        (256, "02020100"),
    ],
)
def test_encode_integer_minimal_twos_complement(value: int, expected_hex: str) -> None:
    """Signed INTEGERs encode as minimal two's-complement octets (the BER shortest form)."""
    assert snmp._encode_integer(value).hex() == expected_hex


def test_encode_length_long_form_above_127() -> None:
    """Lengths > 127 must use the long form (0x81 NN for one length octet)."""
    assert snmp._encode_length(0x7F) == b"\x7f"
    assert snmp._encode_length(0x80) == b"\x81\x80"
    assert snmp._encode_length(0x1B7) == b"\x82\x01\xb7"


def test_oid_roundtrip_with_multibyte_subid() -> None:
    """The Brother enterprise arc 2435 needs a multi-byte base-128 sub-id; encode/decode round-trip."""
    encoded = snmp._encode_oid(OID_DEVICE_ID_1284)
    tag, content, _ = snmp._decode_tlv(encoded)
    assert tag == snmp.TAG_OID
    assert snmp._decode_oid(content) == OID_DEVICE_ID_1284


# ── Unit decode tests for canned OID maps ─────────────────────────────────────────────────
def test_build_status_loaded_62_continuous() -> None:
    values: dict[str, object] = {
        OID_HR_PRINTER_STATUS: 3,
        OID_HR_PRINTER_DETECTED_ERROR_STATE: b"\x00",
        OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT: "READY",
        OID_PRT_INPUT_MEDIA_NAME: '62mm / 2.4"',
        OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR: 6200,
        OID_PRT_INPUT_MEDIA_DIM_FEED_DIR: -1,
        OID_HR_DEVICE_DESCR: "Brother QL-810W",
        OID_SERIAL: "B2Z160525",
        OID_SYS_NAME: "BRWF889D22FBB15",
        OID_PRT_COVER_STATUS: 3,
        OID_PRT_MARKER_LIFE_COUNT: 9,
    }
    status = build_snmp_status(values)

    assert status.reachable is True
    assert status.model == "Brother QL-810W"
    assert status.hostname == "BRWF889D22FBB15"
    assert status.media_width_mm == 62.0
    assert status.media_type == "continuous"
    assert status.media_length_mm is None
    assert status.printer_status == 3
    assert status.cover_status == 3
    assert status.errors == []


def test_build_status_die_cut_variant() -> None:
    """A positive feed value ⇒ die-cut with a discrete length (feed/100 mm)."""
    values: dict[str, object] = {
        OID_HR_PRINTER_DETECTED_ERROR_STATE: b"\x00",
        OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT: "READY",
        OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR: 6200,
        OID_PRT_INPUT_MEDIA_DIM_FEED_DIR: 2900,  # 29.00 mm
    }
    status = build_snmp_status(values)

    assert status.media_width_mm == 62.0
    assert status.media_type == "die_cut"
    assert status.media_length_mm == 29.0
    assert status.errors == []


def test_build_status_error_bit_set_populates_errors() -> None:
    """A non-zero hrPrinterDetectedErrorState (doorOpen) sets error_state_bits and an error string."""
    values: dict[str, object] = {
        OID_HR_PRINTER_DETECTED_ERROR_STATE: b"\x08",  # bit 4 (doorOpen) in a one-octet BITS value
        OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT: "COVER OPEN",
        OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR: 6200,
        OID_PRT_INPUT_MEDIA_DIM_FEED_DIR: -1,
    }
    status = build_snmp_status(values)

    assert status.error_state_bits != 0
    assert "doorOpen" in status.errors
    assert any("COVER OPEN" in e for e in status.errors), (
        "a non-READY console string must also be surfaced as an error"
    )


def test_latch_predicates_distinguish_latch_from_transient_and_ready() -> None:
    """The shared latch/transient predicates (used by the /print preflight and the status badge)
    must agree: only other(1)+a non-READY, non-transient console is the latch; other(1)+a working
    console ("BUSY") is transient-busy; and neither fires for READY, a missing console, or a
    non-other printer_status. Case/whitespace on the console is normalized."""
    from app.transports.snmp import (
        HR_PRINTER_STATUS_IDLE,
        HR_PRINTER_STATUS_OTHER,
        HR_PRINTER_STATUS_PRINTING,
        is_latched_fault,
        is_transient_busy_console,
    )

    # The real sticky latch: other(1) + "ERROR".
    assert is_latched_fault(HR_PRINTER_STATUS_OTHER, "ERROR") is True
    assert is_transient_busy_console(HR_PRINTER_STATUS_OTHER, "ERROR") is False

    # The end-of-print transient: other(1) + "BUSY" (case/whitespace insensitive) — NOT a fault.
    assert is_latched_fault(HR_PRINTER_STATUS_OTHER, " busy ") is False
    assert is_transient_busy_console(HR_PRINTER_STATUS_OTHER, " busy ") is True

    # READY, absent/blank console, and non-other statuses fire neither predicate. A blank buffer
    # matters specifically: _as_str decodes a zero-length OCTET STRING to "" (not None), and an empty
    # console is the ABSENCE of a fault signal — it must NOT read as the sticky latch.
    for ps, console in (
        (HR_PRINTER_STATUS_OTHER, "READY"),
        (HR_PRINTER_STATUS_OTHER, None),
        (HR_PRINTER_STATUS_OTHER, ""),
        (HR_PRINTER_STATUS_OTHER, "   "),
        (HR_PRINTER_STATUS_PRINTING, "PRINTING"),
        (HR_PRINTER_STATUS_IDLE, "READY"),
        (None, "ERROR"),
    ):
        assert is_latched_fault(ps, console) is False, (ps, console)
        assert is_transient_busy_console(ps, console) is False, (ps, console)


def test_build_status_ready_console_not_treated_as_error() -> None:
    """A 'READY' console (any case/whitespace) is the happy path and must not add an error."""
    values: dict[str, object] = {
        OID_HR_PRINTER_DETECTED_ERROR_STATE: b"\x00",
        OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT: " ready ",
    }
    status = build_snmp_status(values)
    assert status.errors == []


def test_build_status_unnamed_error_bits_still_register() -> None:
    """A nonzero error mask whose set bit has no known name must still surface as an error, so a
    caller keying OK/ERROR off the errors list cannot mark a faulted printer healthy."""
    values: dict[str, object] = {
        OID_HR_PRINTER_DETECTED_ERROR_STATE: b"\x00\x01",  # bit set beyond the named-bit range
    }
    status = build_snmp_status(values)

    assert status.error_state_bits != 0
    assert any(e.startswith("unknownErrorBits:") for e in status.errors), (
        "an unrecognised nonzero error bit must not decode to an empty errors list"
    )


def test_parse_response_keeps_binary_error_bits_lossless() -> None:
    """Regression: a multi-octet error mask that is also valid UTF-8 (0xdf 0xbf) must decode to its
    real bits through the full parse path — not be zeroed by a UTF-8 round-trip that hides a fault."""
    error_octets = b"\xdf\xbf"  # valid UTF-8 (U+07FF) yet a non-zero BITS error mask
    varbind = snmp._encode_sequence(
        snmp._encode_oid(OID_HR_PRINTER_DETECTED_ERROR_STATE),
        snmp._encode_octet_string(error_octets),
    )
    pdu = snmp._tlv(
        snmp.TAG_GET_RESPONSE,
        snmp._encode_integer(_PINNED_REQUEST_ID)
        + snmp._encode_integer(0)
        + snmp._encode_integer(0)
        + snmp._encode_sequence(varbind),
    )
    response = snmp._encode_sequence(
        snmp._encode_integer(0),
        snmp._encode_octet_string("public"),
        pdu,
    )

    values = snmp._parse_response(response, _PINNED_REQUEST_ID)
    assert values[OID_HR_PRINTER_DETECTED_ERROR_STATE] == error_octets, (
        "the error-state OID must stay raw bytes through parsing, not a lossy decoded str"
    )
    status = build_snmp_status(values)
    assert status.error_state_bits == 0xDFBF, "the real BITS mask must survive intact"
    assert status.errors, "a non-zero error mask must surface at least one error"


# ── Mocked UDP socket seam (same monkeypatch family as tests/test_transports.py) ──────────
class _FakeUDPSocket:
    """Stand-in for a SOCK_DGRAM socket: records the datagram sent and replies with a canned packet.

    Implements only the methods snmp_get touches plus the context-manager protocol (snmp_get uses
    ``with socket.socket(...)`` so the socket is closed without a ResourceWarning).
    """

    def __init__(self, reply: bytes) -> None:
        self._reply = reply
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.timeout: float | None = None
        self.peer: tuple[str, int] | None = None
        self.closed = False

    def settimeout(self, t: float) -> None:
        self.timeout = t

    def connect(self, addr: tuple[str, int]) -> None:
        self.peer = addr

    def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
        self.sent.append((data, addr))
        return len(data)

    def send(self, data: bytes) -> int:
        # snmp_get connects first, so send() targets the bound peer — mirror sendto's recording.
        assert self.peer is not None, "send() called before connect()"
        return self.sendto(data, self.peer)

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        return self._reply, ("192.168.5.14", 161)

    def recv(self, bufsize: int) -> bytes:
        # Delegate to recvfrom so subclasses that override the reply still work via recv().
        data, _addr = self.recvfrom(bufsize)
        return data

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakeUDPSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _TimingOutUDPSocket(_FakeUDPSocket):
    """A socket whose recvfrom always times out, exercising the retry-then-give-up path."""

    def __init__(self) -> None:
        super().__init__(b"")
        self.recv_calls = 0

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        self.recv_calls += 1
        raise TimeoutError("no SNMP reply")


def _patch_udp(monkeypatch: pytest.MonkeyPatch, fake: _FakeUDPSocket) -> None:
    """Patch the socket.socket reference in the snmp module — same seam as test_transports.py."""
    monkeypatch.setattr(snmp.socket, "socket", lambda *a, **k: fake)


def test_snmp_get_sends_pinned_request_and_decodes_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """snmp_get sends the golden request datagram (pinned id) and decodes the canned reply."""
    fake = _FakeUDPSocket(bytes.fromhex(_GOLDEN_RESPONSE_HEX))
    _patch_udp(monkeypatch, fake)

    values = snmp_get(
        "192.168.5.14",
        "public",
        _GOLDEN_REQUEST_OIDS,
        timeout=1.0,
        request_id=_PINNED_REQUEST_ID,
    )

    assert len(fake.sent) == 1, "exactly one UDP datagram must be sent on the happy path"
    sent_bytes, addr = fake.sent[0]
    assert sent_bytes.hex() == _GOLDEN_REQUEST_HEX, "the datagram must be the golden request"
    assert addr == ("192.168.5.14", 161)
    assert values[OID_PRT_INPUT_MEDIA_NAME] == '62mm / 2.4"'
    assert fake.closed is True, "the UDP socket must be closed (no ResourceWarning)"


def test_snmp_get_retries_once_then_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """With retries=1, a persistently-silent agent gets two recvfrom attempts before TimeoutError."""
    fake = _TimingOutUDPSocket()
    _patch_udp(monkeypatch, fake)

    with pytest.raises(TimeoutError):
        snmp_get("192.168.5.14", "public", [OID_SYS_NAME], timeout=0.01, retries=1)

    assert fake.recv_calls == 2, "retries=1 means one initial attempt plus one retry"
    assert fake.closed is True, "socket must be closed even when every attempt times out"


class _EchoingUDPSocket(_FakeUDPSocket):
    """A UDP socket that, like a real SNMP agent, echoes the request-id from the request it received
    into its reply. ``query_snmp_status`` generates a fresh request-id internally, so a fixed canned
    reply would never match — the fake must splice the actual id (the ``02 04 <id>`` field) in."""

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        request, _addr = self.sent[-1]
        marker = b"\x02\x04"  # the request-id INTEGER (4 octets) in both request and reply
        req_id = request[request.index(marker) + 2 : request.index(marker) + 6]
        reply = bytearray(self._reply)
        idx = reply.index(marker)
        reply[idx + 2 : idx + 6] = req_id
        return bytes(reply), ("192.168.5.14", 161)


class _ReflectingUDPSocket(_FakeUDPSocket):
    """Echoes the exact request datagram back — a reflected GetRequest (PDU tag stays 0xA0),
    simulating a misbehaving peer or spoofer. Its request-id matches by construction, so only the
    PDU-tag check stands between it and being mistaken for a healthy GetResponse."""

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        request, _addr = self.sent[-1]
        return request, ("192.168.5.14", 161)


def test_parse_response_rejects_reflected_get_request() -> None:
    """A reflected GetRequest (PDU tag 0xA0, all-Null varbinds) must be rejected — only a
    GetResponse (0xA2) is a valid answer, else a reflection looks like a healthy reachable printer."""
    reflected = snmp._build_get_request("public", _PINNED_REQUEST_ID, _GOLDEN_REQUEST_OIDS)
    with pytest.raises(SNMPError, match="GetResponse"):
        snmp._parse_response(reflected, _PINNED_REQUEST_ID)


def test_query_snmp_status_unreachable_on_reflected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reflected GetRequest must fail open (reachable=False), not read as a healthy printer."""
    fake = _ReflectingUDPSocket(b"")
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False


def test_query_snmp_status_returns_status_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_snmp_status decodes a full status reply (mocked socket) into a reachable status."""
    # Pin the request-id to a 4-octet value so the echo fake's fixed `02 04 <id>` splice always
    # matches: a random id below 0x800000 would BER-encode in fewer octets and never be found.
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    fake = _EchoingUDPSocket(bytes.fromhex(_GOLDEN_RESPONSE_HEX))
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is True
    assert status.media_type == "continuous"
    assert status.media_width_mm == 62.0
    assert status.serial == "B2Z160525"


def test_query_snmp_status_unreachable_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout must degrade to reachable=False (fail-open) rather than raising."""
    fake = _TimingOutUDPSocket()
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 0.01)

    assert status == PrinterSNMPStatus.unreachable()
    assert status.reachable is False
    assert status.errors == []


def test_query_snmp_status_unreachable_on_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A garbled (non-SNMP) reply must degrade to reachable=False, not raise."""
    fake = _FakeUDPSocket(b"\xff\xff\xff\xff")
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False


@pytest.mark.parametrize("reply", [b"", b"\x30", b"\x30\x82\x01"])
def test_query_snmp_status_unreachable_on_truncated_reply(
    monkeypatch: pytest.MonkeyPatch, reply: bytes
) -> None:
    """A short/truncated UDP datagram must fail open (reachable=False), not raise IndexError —
    a malformed packet would otherwise escape the documented catch and 500 the status route."""
    fake = _FakeUDPSocket(reply)
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False


@pytest.mark.parametrize("packet", [b"", b"\x30", b"\x30\x82\x01"])
def test_parse_response_raises_snmperror_on_truncation(packet: bytes) -> None:
    """The decoder converts every truncation into SNMPError (never a bare IndexError), so the
    fail-open contract in query_snmp_status holds for any malformed reply."""
    with pytest.raises(SNMPError):
        snmp._parse_response(packet, _PINNED_REQUEST_ID)


def _varbind(oid: str, value_tlv: bytes) -> bytes:
    """Assemble one varbind SEQUENCE (OID + an already-encoded value TLV)."""
    return snmp._encode_sequence(snmp._encode_oid(oid), value_tlv)


def _response(request_id: int, varbinds: list[bytes], *, error_status: int = 0) -> bytes:
    """Build a full SNMPv1 GetResponse message from already-encoded varbinds."""
    pdu = snmp._tlv(
        snmp.TAG_GET_RESPONSE,
        snmp._encode_integer(request_id)
        + snmp._encode_integer(error_status)
        + snmp._encode_integer(1 if error_status else 0)  # error-index
        + snmp._encode_sequence(*varbinds),
    )
    return snmp._encode_sequence(snmp._encode_integer(0), snmp._encode_octet_string("public"), pdu)


class _SequencedUDPSocket(_FakeUDPSocket):
    """Returns a queued sequence of canned replies, one per GET. query_snmp_status issues two
    GetRequests (critical then optional), so this models a printer that answers the first and
    fails the second. Replies must already carry the matching (pinned) request-id."""

    def __init__(self, replies: list[bytes]) -> None:
        super().__init__(b"")
        self._replies = list(replies)

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        return self._replies.pop(0), ("192.168.5.14", 161)


def test_query_snmp_status_tolerates_optional_oid_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SNMPv1 noSuchName (error-status=2) on the optional telemetry GET must not sink the
    critical media/error read: status stays reachable with the optional fields simply absent."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    critical = _response(  # every CRITICAL_STATUS_OID present
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._encode_octet_string(b"\x00")),
            _varbind(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT, snmp._encode_octet_string("READY")),
            _varbind(OID_PRT_INPUT_MEDIA_NAME, snmp._encode_octet_string('62mm / 2.4"')),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_integer(6200)),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    optional_failure = _response(_PINNED_REQUEST_ID, [], error_status=2)  # noSuchName
    fake = _SequencedUDPSocket([critical, optional_failure])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is True, "an optional-OID failure must not disable the critical read"
    assert status.media_type == "continuous", "critical media data must survive"
    assert status.media_width_mm == 62.0
    assert status.serial is None, "the failed optional GET leaves telemetry fields absent"
    assert status.label_lifecount is None


def test_query_snmp_status_enforces_when_descriptive_oids_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A printer that reports the error bitmask + media geometry but omits the descriptive
    console/media-name OIDs must still yield an enforceable status (reachable, media decoded).

    Regression for the over-broad-critical-OIDs fail-open hole: prtConsoleDisplayBufferText /
    prtInputMediaName are best-effort, so a version-skewed agent that omits them (here the entire
    optional GET fails with noSuchName) must NOT disable the media/fault guard — the critical read of
    error-state + the two dimensions alone is enough to enforce a mismatch."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    # Critical GET: ONLY error-state + the two media dimensions — no console, no media_name.
    critical = _response(
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._encode_octet_string(b"\x00")),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_integer(6200)),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    # The agent implements none of the descriptive/telemetry OIDs: the whole optional GET fails.
    optional_failure = _response(_PINNED_REQUEST_ID, [], error_status=2)  # noSuchName
    fake = _SequencedUDPSocket([critical, optional_failure])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is True, "missing descriptive OIDs must not disable the guard"
    assert status.media_width_mm == 62.0, "loaded-media geometry still decoded for enforcement"
    assert status.media_type == "continuous"
    assert status.error_state_bits == 0
    assert status.media_name is None, "media_name is best-effort and absent here"
    assert status.console_text is None, "console text is best-effort and absent here"


def test_query_snmp_status_unreachable_when_critical_oid_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GetResponse with error-status=0 that omits a safety-critical OID (the error bitmask) must
    be treated as unreachable — not decoded as a healthy printer with error_state_bits=0/errors=[]."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    degraded = _response(  # error-status=0, but the error-state OID is absent
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_integer(6200)),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    fake = _SequencedUDPSocket([degraded])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False


def test_query_snmp_status_optional_cannot_clear_critical_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed/spoofed optional telemetry reply that echoes error-free critical OIDs must not
    overwrite a fault the critical read already established (optional may set only optional fields)."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    critical = _response(  # the printer is faulted: doorOpen + COVER OPEN console
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._encode_octet_string(b"\x08")),
            _varbind(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT, snmp._encode_octet_string("COVER OPEN")),
            _varbind(OID_PRT_INPUT_MEDIA_NAME, snmp._encode_octet_string('62mm / 2.4"')),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_integer(6200)),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    optional_spoof = _response(  # tries to clear the fault by echoing error-free critical OIDs
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._encode_octet_string(b"\x00")),
            _varbind(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT, snmp._encode_octet_string("READY")),
            _varbind(OID_SERIAL, snmp._encode_octet_string("B2Z160525")),
        ],
    )
    fake = _SequencedUDPSocket([critical, optional_spoof])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is True
    assert status.error_state_bits != 0, "the optional reply must not clear the critical fault"
    assert "doorOpen" in status.errors
    assert status.serial == "B2Z160525", "a legitimate optional field still merges"


def test_parse_response_rejects_snmp_exception_varbind() -> None:
    """A varbind whose value is an SNMPv2 exception (noSuchObject, 0x80) means the OID has no value.
    _parse_response must raise rather than decode the empty content to a benign value — otherwise a
    critical fault OID returned as noSuchObject would read as a zero error mask (a healthy printer)."""
    exception_value = snmp._tlv(0x80, b"")  # noSuchObject, empty content
    response = _response(
        _PINNED_REQUEST_ID,
        [_varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, exception_value)],
    )
    with pytest.raises(snmp.SNMPError, match="exception"):
        snmp._parse_response(response, _PINNED_REQUEST_ID)


def test_query_snmp_status_unreachable_on_exception_tagged_critical_oid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a critical GET whose error-state OID comes back as an SNMP exception value must
    degrade to unreachable (fail open) — never a falsely-healthy reachable status. The exception tag
    is rejected in _parse_response, which query_snmp_status catches and maps to unreachable."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    degraded = _response(
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._tlv(0x80, b"")),  # noSuchObject
            _varbind(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT, snmp._encode_octet_string("READY")),
            _varbind(OID_PRT_INPUT_MEDIA_NAME, snmp._encode_octet_string('62mm / 2.4"')),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_integer(6200)),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    fake = _SequencedUDPSocket([degraded])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False, (
        "an exception-tagged critical OID must fail open as unreachable, not read as healthy"
    )


def test_query_snmp_status_unreachable_on_mistyped_error_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A critical reply whose error-state OID is present but the wrong type (NULL, not BITS/INTEGER)
    must be treated as unreachable: the fault state was not actually measured. NULL is not an SNMP
    exception tag, so this exercises the critical-value type guard, not the exception-tag rejection."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    degraded = _response(
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._encode_null()),  # decodes to None
            _varbind(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT, snmp._encode_octet_string("READY")),
            _varbind(OID_PRT_INPUT_MEDIA_NAME, snmp._encode_octet_string('62mm / 2.4"')),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_integer(6200)),
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    fake = _SequencedUDPSocket([degraded])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False


def test_query_snmp_status_unreachable_on_non_integer_media_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A critical reply whose media dimension is a non-integer (here an OCTET STRING) must be treated
    as unreachable rather than decoding to a missing width that reads as benign — the loaded-media
    geometry the print guard relies on was not actually measured."""
    monkeypatch.setattr(snmp, "_new_request_id", lambda: _PINNED_REQUEST_ID)
    degraded = _response(
        _PINNED_REQUEST_ID,
        [
            _varbind(OID_HR_PRINTER_DETECTED_ERROR_STATE, snmp._encode_octet_string(b"\x00")),
            _varbind(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT, snmp._encode_octet_string("READY")),
            _varbind(OID_PRT_INPUT_MEDIA_NAME, snmp._encode_octet_string('62mm / 2.4"')),
            _varbind(
                OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, snmp._encode_octet_string("6200")
            ),  # wrong type
            _varbind(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR, snmp._encode_integer(-1)),
        ],
    )
    fake = _SequencedUDPSocket([degraded])
    _patch_udp(monkeypatch, fake)

    status = query_snmp_status("192.168.5.14", "public", 161, 1.0)

    assert status.reachable is False
