# SPDX-License-Identifier: GPL-3.0-or-later
"""Hand-rolled, synchronous, zero-dependency SNMP v1 GET client for printer status.

Production needs only **SNMP v1 GET on a handful of known scalar OIDs in one request** — no walks,
no GETNEXT (those were diagnostic via the ``snmpwalk`` CLI). A hand-rolled GET keeps the transport
layer uniformly synchronous (it runs in the existing ``run_in_threadpool`` worker model, with no
event loop and no ``asyncio.run()`` footgun), carries zero supply-chain and zero 3.14/3.15 compat
risk, and mirrors the codebase's existing hand-rolled brother_ql binary parsing.

The module exposes three layers:
  * BER/ASN.1 encode + decode primitives (``_encode_*`` / ``_decode_tlv``),
  * :func:`snmp_get` — build one v1 GetRequest PDU, send one UDP datagram (with one retry), decode
    the reply, verify the request-id echo and ``error-status == 0``, return ``{oid: value}``,
  * :func:`query_snmp_status` — one ``snmp_get`` over all the printer status OIDs, decoded into a
    :class:`PrinterSNMPStatus` (never raises: any failure ⇒ ``reachable=False`` + a warning log).
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ── SNMP / UDP constants ──────────────────────────────────────────────────────────
SNMP_PORT = 161
SNMP_VERSION_V1 = 0  # SNMPv1 ⇒ version field is INTEGER 0
DEFAULT_COMMUNITY = "public"

# ── BER/ASN.1 tags ──────────────────────────────────────────────────────────────────
# Universal primitive types we encode/decode.
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30  # constructed SEQUENCE / SEQUENCE OF
# SNMP application types (context/application-specific) we only ever decode.
TAG_IP_ADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42  # a.k.a. Unsigned32
TAG_TIMETICKS = 0x43
# SNMP PDU types: context-specific constructed tags. GetRequest is tag 0 ⇒ 0xA0; the only valid
# reply to it is GetResponse, tag 2 ⇒ 0xA2.
TAG_GET_REQUEST = 0xA0
TAG_GET_RESPONSE = 0xA2
# SNMPv2 per-varbind exception values (context-specific primitives) that a v2c-capable agent may
# emit even to a v1 request: noSuchObject / noSuchInstance / endOfMibView. They mean "this OID has
# no value", so a varbind carrying one was NOT actually answered. We reject them rather than let the
# empty content decode to a value (e.g. an empty error-state mask reads as "no errors" — a critical
# fault OID that was never read would otherwise look healthy).
SNMP_EXCEPTION_TAGS = frozenset({0x80, 0x81, 0x82})


# ── Named OID constants (verified live against the QL-810W; see docs/snmp-status-feature.md) ──
OID_HR_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"
OID_HR_PRINTER_DETECTED_ERROR_STATE = "1.3.6.1.2.1.25.3.5.1.2.1"
OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT = "1.3.6.1.2.1.43.16.5.1.2.1.1"
OID_PRT_INPUT_MEDIA_NAME = "1.3.6.1.2.1.43.8.2.1.12.1.1"
OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR = "1.3.6.1.2.1.43.8.2.1.5.1.1"
OID_PRT_INPUT_MEDIA_DIM_FEED_DIR = "1.3.6.1.2.1.43.8.2.1.4.1.1"
OID_PRT_MARKER_TYPE = "1.3.6.1.2.1.43.11.1.1.6.1.1"
OID_PRT_COVER_STATUS = "1.3.6.1.2.1.43.6.1.1.3.1.1"
OID_HR_DEVICE_DESCR = "1.3.6.1.2.1.25.3.2.1.3.1"
OID_DEVICE_ID_1284 = (
    "1.3.6.1.4.1.2435.2.3.9.1.1.7.0"  # Brother enterprise arc 2435 ⇒ multi-byte sub-id
)
OID_SERIAL = "1.3.6.1.2.1.43.5.1.1.17.1"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_PRT_MARKER_LIFE_COUNT = "1.3.6.1.2.1.43.10.2.1.4.1.1"

# Safety-critical OIDs: the error bitmask, loaded-media geometry and console line the print guard
# relies on. They are fetched together and ALL must come back (see query_snmp_status): a reply that
# omits one — even with error-status=0 — is treated as unreachable rather than decoded as a healthy
# printer with no errors. They live in their own GetRequest so an unsupported *optional* OID below
# cannot, via SNMPv1's all-or-nothing noSuchName, take the critical read down with it.
CRITICAL_STATUS_OIDS: tuple[str, ...] = (
    OID_HR_PRINTER_DETECTED_ERROR_STATE,
    OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT,
    OID_PRT_INPUT_MEDIA_NAME,
    OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR,
    OID_PRT_INPUT_MEDIA_DIM_FEED_DIR,
)
# Optional identity/telemetry OIDs: enrich the status card and metrics but are not guard inputs.
# hrPrinterStatus (idle/printing) is informational, so it lives here too. A model/firmware lacking
# any of these (SNMPv1 noSuchName) must not cost us the critical read, so they are fetched in a
# separate best-effort GetRequest.
OPTIONAL_STATUS_OIDS: tuple[str, ...] = (
    OID_HR_PRINTER_STATUS,
    OID_PRT_COVER_STATUS,
    OID_HR_DEVICE_DESCR,
    OID_SERIAL,
    OID_SYS_DESCR,
    OID_SYS_NAME,
    OID_PRT_MARKER_LIFE_COUNT,
)
STATUS_OIDS: tuple[str, ...] = CRITICAL_STATUS_OIDS + OPTIONAL_STATUS_OIDS

# Standard hrPrinterDetectedErrorState bits (RFC 3805, Printer-MIB). Bit 0 is the most significant
# bit of the FIRST octet (BITS encoding), so a one-octet bitmask uses the high nibble of byte 0.
HR_PRINTER_ERROR_BITS: tuple[tuple[int, str], ...] = (
    (0, "lowPaper"),
    (1, "noPaper"),
    (2, "lowToner"),
    (3, "noToner"),
    (4, "doorOpen"),
    (5, "jammed"),
    (6, "offline"),
    (7, "serviceRequested"),
    (8, "inputTrayMissing"),
    (9, "outputTrayMissing"),
    (10, "markerSupplyMissing"),
    (11, "outputNearFull"),
    (12, "outputFull"),
    (13, "inputTrayEmpty"),
    (14, "overduePreventMaint"),
)

# Console text the printer shows when idle/ready; anything else is surfaced as an error string.
CONSOLE_READY = "READY"
# prtInputMediaDimFeedDir sentinels meaning "no discrete length" ⇒ continuous tape.
CONTINUOUS_FEED_SENTINELS = (-1, -2)
MEDIA_TYPE_CONTINUOUS = "continuous"
MEDIA_TYPE_DIE_CUT = "die_cut"
# prtInputMediaDim* values are reported in hundredths of a millimetre.
MEDIA_DIM_HUNDREDTHS_PER_MM = 100


class SNMPError(Exception):
    """Raised inside snmp_get for a protocol-level fault (bad request-id echo, error-status != 0,
    or an undecodable response). query_snmp_status catches it and degrades to reachable=False."""


# ── BER length codec ────────────────────────────────────────────────────────────────
def _encode_length(length: int) -> bytes:
    """Encode a BER length. Lengths ≤ 127 use the short form (one byte); longer lengths use the
    long form: a leading byte 0x80|N followed by N big-endian length octets."""
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _decode_length(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a BER length starting at ``offset``. Returns ``(length, next_offset)`` where
    ``next_offset`` points at the first content byte. Handles the long form (lengths > 127).
    Raises :class:`SNMPError` on a truncated packet so the fail-open path catches it."""
    if offset >= len(data):
        raise SNMPError("truncated SNMP packet: length octet missing")
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    num_octets = first & 0x7F
    if offset + num_octets > len(data):
        raise SNMPError("truncated SNMP packet: long-form length octets missing")
    length = int.from_bytes(data[offset : offset + num_octets], "big")
    return length, offset + num_octets


def _tlv(tag: int, content: bytes) -> bytes:
    """Wrap content in a tag-length-value triple with a correctly long-form-aware length."""
    return bytes([tag]) + _encode_length(len(content)) + content


# ── BER value encoders ───────────────────────────────────────────────────────────────
def _encode_integer(value: int) -> bytes:
    """Encode a signed INTEGER as minimal two's-complement octets (BER requires the shortest form,
    with one extra leading byte only when needed to preserve the sign bit)."""
    if value == 0:
        body = b"\x00"
    else:
        byte_len = (value.bit_length() + 8) // 8  # +8 reserves a guard bit for the sign
        body = value.to_bytes(byte_len, "big", signed=True)
        # Strip redundant leading 0x00 / 0xFF octets that don't change the sign-extended value.
        while len(body) > 1 and (
            (body[0] == 0x00 and not (body[1] & 0x80)) or (body[0] == 0xFF and (body[1] & 0x80))
        ):
            body = body[1:]
    return _tlv(TAG_INTEGER, body)


def _encode_octet_string(value: str | bytes) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return _tlv(TAG_OCTET_STRING, raw)


def _encode_null() -> bytes:
    return _tlv(TAG_NULL, b"")


def _encode_subid(subid: int) -> bytes:
    """Encode one OID sub-identifier in base-128: 7 bits per octet, high bit set on all but the
    last. Sub-ids > 127 (e.g. the Brother enterprise arc 2435) therefore span multiple octets."""
    if subid < 0x80:
        return bytes([subid])
    out = bytearray()
    out.insert(0, subid & 0x7F)  # final octet: high bit clear
    subid >>= 7
    while subid:
        out.insert(0, (subid & 0x7F) | 0x80)
        subid >>= 7
    return bytes(out)


def _encode_oid(oid: str) -> bytes:
    """Encode a dotted OID string. The first two sub-ids are packed into one octet as 40*x+y."""
    parts = [int(p) for p in oid.split(".")]
    if len(parts) < 2:
        raise ValueError(f"OID must have at least two arcs: {oid!r}")
    body = bytearray(_encode_subid(40 * parts[0] + parts[1]))
    for sub in parts[2:]:
        body += _encode_subid(sub)
    return _tlv(TAG_OID, bytes(body))


def _encode_sequence(*parts: bytes) -> bytes:
    return _tlv(TAG_SEQUENCE, b"".join(parts))


# ── BER decoder ───────────────────────────────────────────────────────────────────────
def _decode_oid(content: bytes) -> str:
    """Decode an OID content octet stream back to a dotted string, reversing the base-128 packing
    and the 40*x+y first-octet combination."""
    if not content:
        return ""
    arcs: list[int] = []
    first = content[0]
    arcs.extend((first // 40, first % 40))
    subid = 0
    for byte in content[1:]:
        subid = (subid << 7) | (byte & 0x7F)
        if not (byte & 0x80):  # high bit clear ⇒ final octet of this sub-id
            arcs.append(subid)
            subid = 0
    return ".".join(str(a) for a in arcs)


def _decode_value(tag: int, content: bytes) -> object:
    """Map a primitive TLV to a Python value. Unknown tags fall back to raw bytes so an unexpected
    type never crashes the decode."""
    if tag == TAG_INTEGER:
        return int.from_bytes(content, "big", signed=True)
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS):
        # SNMP application counters/gauges/timeticks are unsigned.
        return int.from_bytes(content, "big", signed=False)
    if tag == TAG_OCTET_STRING:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content
    if tag == TAG_OID:
        return _decode_oid(content)
    if tag == TAG_NULL:
        return None
    if tag == TAG_IP_ADDRESS:
        return ".".join(str(b) for b in content)
    return content


def _decode_tlv(data: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    """Read one TLV starting at ``offset``. Returns ``(tag, content_bytes, next_offset)``. The
    caller decides whether to recurse (constructed types) or decode the content as a primitive.
    Raises :class:`SNMPError` on a truncated packet so the fail-open path catches it."""
    if offset >= len(data):
        raise SNMPError("truncated SNMP packet: TLV tag missing")
    tag = data[offset]
    length, value_start = _decode_length(data, offset + 1)
    if value_start + length > len(data):
        raise SNMPError("truncated SNMP packet: TLV content shorter than declared length")
    content = data[value_start : value_start + length]
    return tag, content, value_start + length


# ── GetRequest PDU build / parse ───────────────────────────────────────────────────────
def _new_request_id() -> int:
    """A non-cryptographic, varying 31-bit request-id (kept positive so it always encodes as a
    one-to-four-octet INTEGER). Tests pin this via the ``request_id`` parameter for golden bytes."""
    return int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF


def _build_get_request(community: str, request_id: int, oids: list[str]) -> bytes:
    """Build a complete SNMPv1 GetRequest message: SEQUENCE(version, community, GetRequest-PDU)."""
    varbinds = [_encode_sequence(_encode_oid(oid), _encode_null()) for oid in oids]
    varbind_list = _encode_sequence(*varbinds)
    pdu = _tlv(
        TAG_GET_REQUEST,
        _encode_integer(request_id)
        + _encode_integer(0)  # error-status
        + _encode_integer(0)  # error-index
        + varbind_list,
    )
    return _encode_sequence(
        _encode_integer(SNMP_VERSION_V1),
        _encode_octet_string(community),
        pdu,
    )


def _parse_response(data: bytes, expected_request_id: int) -> dict[str, object]:
    """Parse a GetResponse message into ``{oid: value}``, verifying the request-id echo and
    ``error-status == 0``. Raises :class:`SNMPError` on any protocol fault."""
    msg_tag, msg_content, _ = _decode_tlv(data)
    if msg_tag != TAG_SEQUENCE:
        raise SNMPError(f"expected SEQUENCE message, got tag {msg_tag:#04x}")

    # version, community, then the PDU.
    _, _, off = _decode_tlv(msg_content, 0)  # version INTEGER
    _, _, off = _decode_tlv(msg_content, off)  # community OCTET STRING
    pdu_tag, pdu_content, _ = _decode_tlv(msg_content, off)
    # Require a GetResponse (0xA2). The only valid reply to our GetRequest is a GetResponse;
    # accepting any context-constructed PDU would let a reflected GetRequest (0xA0, all-Null
    # varbinds) decode as a healthy reachable printer and bypass the status/media guard.
    if pdu_tag != TAG_GET_RESPONSE:
        raise SNMPError(f"expected GetResponse PDU (0xA2), got tag {pdu_tag:#04x}")

    _rid_tag, rid_content, off = _decode_tlv(pdu_content, 0)
    request_id = int.from_bytes(rid_content, "big", signed=True)
    if request_id != expected_request_id:
        raise SNMPError(
            f"response request-id {request_id} does not match request {expected_request_id}"
        )

    _err_tag, err_content, off = _decode_tlv(pdu_content, off)  # error-status
    error_status = int.from_bytes(err_content, "big", signed=True)
    _, _, off = _decode_tlv(pdu_content, off)  # error-index (unused)
    if error_status != 0:
        raise SNMPError(f"SNMP error-status {error_status}")

    vbl_tag, vbl_content, _ = _decode_tlv(pdu_content, off)  # varbind-list SEQUENCE
    if vbl_tag != TAG_SEQUENCE:
        raise SNMPError(f"expected varbind-list SEQUENCE, got tag {vbl_tag:#04x}")

    result: dict[str, object] = {}
    pos = 0
    while pos < len(vbl_content):
        _, vb_content, pos = _decode_tlv(vbl_content, pos)  # one varbind SEQUENCE
        name_tag, name_content, val_off = _decode_tlv(vb_content, 0)
        if name_tag != TAG_OID:
            raise SNMPError(f"varbind name is not an OID (tag {name_tag:#04x})")
        oid = _decode_oid(name_content)
        val_tag, val_content, _ = _decode_tlv(vb_content, val_off)
        if val_tag in SNMP_EXCEPTION_TAGS:
            # noSuchObject/noSuchInstance/endOfMibView ⇒ the agent has no value for this OID. The
            # empty content would otherwise decode to a falsely-benign value (e.g. a zero error
            # mask), so fail the whole GET — query_snmp_status then degrades to unreachable for the
            # critical read (fail open) rather than reporting a healthy printer it never measured.
            raise SNMPError(
                f"varbind {oid} carries SNMP exception {val_tag:#04x} (OID not available)"
            )
        if oid == OID_HR_PRINTER_DETECTED_ERROR_STATE and val_tag == TAG_OCTET_STRING:
            # This OID is binary BITS data, not text. Keep the raw octets: a multi-octet error
            # mask can be valid UTF-8 (e.g. 0xdf 0xbf), and decoding then re-encoding it would
            # silently zero out real error bits, making a faulted printer look healthy.
            result[oid] = val_content
        else:
            result[oid] = _decode_value(val_tag, val_content)
    return result


def snmp_get(
    host: str,
    community: str,
    oids: list[str],
    *,
    port: int = SNMP_PORT,
    timeout: float,
    retries: int = 1,
    request_id: int | None = None,
) -> dict[str, object]:
    """Send one SNMPv1 GetRequest for ``oids`` and return ``{oid: decoded_value}``.

    The socket is ``connect``-ed to the target so the kernel only delivers datagrams from that
    peer: a spoofed or stray reply from another source (which could otherwise race the cleartext
    request-id and forge a printer state) is dropped before it reaches us. One datagram is sent,
    the reply is awaited up to ``timeout`` seconds, and the exchange is retried ``retries`` extra
    times (UDP can silently drop a datagram). The response's request-id echo and ``error-status``
    are verified. ``request_id`` may be pinned by a caller (tests) for golden-byte assertions;
    otherwise a varying value is generated.

    Raises ``OSError``/``TimeoutError`` if no reply arrives within the retry budget, or
    :class:`SNMPError` for a protocol-level fault. The socket is always closed (no ResourceWarning).
    """
    if request_id is None:
        request_id = _new_request_id()
    packet = _build_get_request(community, request_id, oids)

    last_error: OSError | None = None
    # One send + up to `retries` resends. A context manager guarantees the socket is closed even on
    # a raised SNMPError, so filterwarnings=["error"] never sees a ResourceWarning.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))  # bind the peer so recv() ignores datagrams from other sources
        for _ in range(retries + 1):
            try:
                sock.send(packet)
                reply = sock.recv(65535)
            except TimeoutError as exc:
                last_error = exc
                continue
            return _parse_response(reply, request_id)
    raise last_error if last_error is not None else TimeoutError("no SNMP reply")


# ── High-level status query ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PrinterSNMPStatus:
    """Printer state decoded from one SNMP status GetRequest.

    ``reachable`` is the single bit callers must check first: when ``False`` the SNMP query failed
    (timeout / socket error / decode error) and every other field is ``None``/empty — callers should
    fail open (allow the print, badge the UI as unknown). The optional identity/media fields mirror
    the frozen-dataclass, optional-field style of :class:`app.transports.base.PrinterStatus`.
    """

    reachable: bool
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    hostname: str | None = None
    console_text: str | None = None
    error_state_bits: int = 0
    printer_status: int | None = None
    media_name: str | None = None
    media_width_mm: float | None = None
    media_length_mm: float | None = None
    media_type: str | None = None  # "continuous" | "die_cut" | None
    cover_status: int | None = None
    label_lifecount: int | None = None
    errors: list[str] = field(default_factory=list)

    @classmethod
    def unreachable(cls) -> PrinterSNMPStatus:
        """A status representing a printer whose SNMP agent could not be reached or decoded."""
        return cls(reachable=False)


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return None


def _decode_error_bits(error_state: object) -> tuple[int, list[str]]:
    """Decode hrPrinterDetectedErrorState (a BITS value carried as OCTET STRING, or an INTEGER) into
    a single bitmask int and the list of human-readable condition names that are set.

    ``_parse_response`` keeps this OID's OCTET-STRING form as raw ``bytes`` (never the lossy
    decoded ``str``), so the bit decode here is exact for any octet pattern. Some agents instead
    report a plain INTEGER bitmask, which is handled separately.
    """
    if isinstance(error_state, bytes):
        mask = int.from_bytes(error_state, "big") if error_state else 0
        # BITS number bit 0 as the most-significant bit of the first octet, so re-index against the
        # total bit width to recover RFC 3805 bit positions. Bits beyond the octets actually present
        # can never be set (the QL returns a single octet), so they are skipped rather than shifted
        # negatively.
        total_bits = len(error_state) * 8
        names = [
            name
            for bit, name in HR_PRINTER_ERROR_BITS
            if bit < total_bits and mask & (1 << (total_bits - 1 - bit))
        ]
        return mask, names
    if isinstance(error_state, int):
        # Some agents return a plain integer bitmask; treat bit 0 as the least-significant bit.
        names = [name for bit, name in HR_PRINTER_ERROR_BITS if error_state & (1 << bit)]
        return error_state, names
    return 0, []


def _decode_media(values: dict[str, object]) -> tuple[float | None, float | None, str | None]:
    """Decode loaded-media geometry: width = xfeed/100 mm; feed in {-1,-2} ⇒ continuous, else
    die-cut with length = feed/100 mm."""
    xfeed = _as_int(values.get(OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR))
    feed = _as_int(values.get(OID_PRT_INPUT_MEDIA_DIM_FEED_DIR))
    width_mm = xfeed / MEDIA_DIM_HUNDREDTHS_PER_MM if xfeed is not None else None
    if feed is None:
        return width_mm, None, None
    if feed in CONTINUOUS_FEED_SENTINELS:
        return width_mm, None, MEDIA_TYPE_CONTINUOUS
    return width_mm, feed / MEDIA_DIM_HUNDREDTHS_PER_MM, MEDIA_TYPE_DIE_CUT


def build_snmp_status(values: dict[str, object]) -> PrinterSNMPStatus:
    """Map a decoded ``{oid: value}`` map onto a :class:`PrinterSNMPStatus` (reachable=True).

    Split out from :func:`query_snmp_status` so the pure mapping can be unit-tested against canned
    OID maps without any socket.
    """
    error_state_bits, error_names = _decode_error_bits(
        values.get(OID_HR_PRINTER_DETECTED_ERROR_STATE)
    )
    console_text = _as_str(values.get(OID_PRT_CONSOLE_DISPLAY_BUFFER_TEXT))
    width_mm, length_mm, media_type = _decode_media(values)

    errors: list[str] = list(error_names)
    if error_state_bits != 0 and not error_names:
        # A nonzero mask with no recognised bit name must still register as an error: callers that
        # key OK/ERROR off the errors list would otherwise mark a faulted printer healthy.
        errors.append(f"unknownErrorBits:{error_state_bits:#x}")
    if console_text is not None and console_text.strip().upper() != CONSOLE_READY:
        errors.append(f"console: {console_text}")

    return PrinterSNMPStatus(
        reachable=True,
        model=_as_str(values.get(OID_HR_DEVICE_DESCR)),
        serial=_as_str(values.get(OID_SERIAL)),
        firmware=_as_str(values.get(OID_SYS_DESCR)),
        hostname=_as_str(values.get(OID_SYS_NAME)),
        console_text=console_text,
        error_state_bits=error_state_bits,
        printer_status=_as_int(values.get(OID_HR_PRINTER_STATUS)),
        media_name=_as_str(values.get(OID_PRT_INPUT_MEDIA_NAME)),
        media_width_mm=width_mm,
        media_length_mm=length_mm,
        media_type=media_type,
        cover_status=_as_int(values.get(OID_PRT_COVER_STATUS)),
        label_lifecount=_as_int(values.get(OID_PRT_MARKER_LIFE_COUNT)),
        errors=errors,
    )


def query_snmp_status(
    host: str,
    community: str = DEFAULT_COMMUNITY,
    port: int = SNMP_PORT,
    timeout: float = 2.0,
) -> PrinterSNMPStatus:
    """Query the printer status OIDs and decode them into a :class:`PrinterSNMPStatus`.

    The safety-critical OIDs (status/error/media) are fetched first; optional identity/telemetry
    OIDs follow in a separate best-effort GetRequest, so a model that lacks one of them does not
    cost us the critical read (SNMPv1 fails a whole GET on a single unsupported OID).

    Never raises: if the critical read fails (timeout / socket error / decode error) it logs a
    warning and returns ``PrinterSNMPStatus.unreachable()`` so the caller can fail open (allow the
    print, badge the UI as unknown).
    """
    try:
        values = snmp_get(host, community, list(CRITICAL_STATUS_OIDS), port=port, timeout=timeout)
    except (OSError, SNMPError, ValueError) as exc:
        log.warning("SNMP status query to %s:%d failed: %s", host, port, exc)
        return PrinterSNMPStatus.unreachable()
    # A conformant GetResponse echoes every requested varbind. If a degraded agent answers
    # error-status=0 but omits a critical OID, the missing error/media data would otherwise decode
    # to a falsely-healthy status — treat that as unreachable so the guard fails open instead.
    missing = [oid for oid in CRITICAL_STATUS_OIDS if oid not in values]
    if missing:
        log.warning(
            "SNMP reply from %s:%d omitted critical OIDs %s; treating as unreachable",
            host,
            port,
            missing,
        )
        return PrinterSNMPStatus.unreachable()
    # Defence-in-depth beyond presence: a conformant agent answers each safety-critical OID with its
    # expected type. A version-skewed or hostile agent could echo error-status=0 yet return the
    # error-state OID as a NULL/OID (decoding to None/str) or a media dimension as a non-integer —
    # values that would silently decode to a falsely-healthy status. The error mask must be the raw
    # BITS octets (bytes) or an integer mask; the loaded-media dimensions must be integers. Anything
    # else means we did not actually measure the fault state, so fail open as unreachable.
    error_state = values.get(OID_HR_PRINTER_DETECTED_ERROR_STATE)
    mistyped: list[str] = []
    if not isinstance(error_state, bytes | int):
        mistyped.append(OID_HR_PRINTER_DETECTED_ERROR_STATE)
    mistyped.extend(
        oid
        for oid in (OID_PRT_INPUT_MEDIA_DIM_XFEED_DIR, OID_PRT_INPUT_MEDIA_DIM_FEED_DIR)
        if not isinstance(values.get(oid), int)
    )
    if mistyped:
        log.warning(
            "SNMP reply from %s:%d returned unusable types for critical OIDs %s; treating as "
            "unreachable",
            host,
            port,
            mistyped,
        )
        return PrinterSNMPStatus.unreachable()
    try:
        optional = snmp_get(host, community, list(OPTIONAL_STATUS_OIDS), port=port, timeout=timeout)
        # Only let the optional read populate optional fields. snmp_get does not constrain a reply
        # to the requested OIDs, so a malformed or spoofed optional response could otherwise echo
        # error-free critical OIDs and clear a fault the critical read already established.
        values.update({oid: val for oid, val in optional.items() if oid in OPTIONAL_STATUS_OIDS})
    except (OSError, SNMPError, ValueError) as exc:
        log.info(
            "SNMP optional telemetry query to %s:%d failed; status still usable: %s",
            host,
            port,
            exc,
        )
    return build_snmp_status(values)
