# SNMP-backed printer status & media guard

Reference for how Labelito reads printer status over SNMP, why it exists, and the verified OID map.
This is the "how it works now" companion to the original task document
[snmp-status-feature.md](snmp-status-feature.md) (the step-by-step implementation plan) and the
trade-off record in [known-limitations.md](known-limitations.md#the-network-back-channel-is-silent--snmp-is-the-status-channel).

## Why SNMP

Brother's network NIC (the QL-810W ships a `Brother NC-36002w`) **accepts the `:9100` TCP print
connection and the raster bytes but never returns the status frame** the same printer returns over
USB — verified live, `recv` on the back-channel times out. So the network transport's readback sees
`None`, the print path reads that as "no error reported", and a job the **hardware** rejects reports
HTTP `200` success while the printer blinks red and prints nothing.

The most common trigger is a **media mismatch**: a template whose `label:` is `62x29` (die-cut)
printed against a 62 mm continuous roll. The printer rasterizes the job, then rejects it at the
hardware level because the loaded media ≠ the requested media.

The same printer answers **SNMP instantly** (UDP 161, community `public`, v1/v2c) and exposes the
loaded media, a reliable error bitmask, the console status line, identity, and a lifetime label
counter. SNMP is therefore the status channel Labelito uses for the **network** transport. (USB
keeps its working print-time readback; `file://` is a synthetic-OK debug sink.)

## Verified OIDs (live, QL-810W)

All scalar, fetched in two `GetRequest`s (see [Implementation](#implementation)). Values below are a
live snapshot with 62 mm continuous tape loaded and the printer idle.

| Purpose | OID | Live value | Notes |
|---|---|---|---|
| Printer status | `1.3.6.1.2.1.25.3.5.1.1.1` (hrPrinterStatus) | `idle(3)` | idle/printing/warmup/other |
| **Error bitmask** | `1.3.6.1.2.1.25.3.5.1.2.1` (hrPrinterDetectedErrorState) | `0x00` | non-zero ⇒ error; **primary error signal**; BITS |
| Console text | `1.3.6.1.2.1.43.16.5.1.2.1.1` (prtConsoleDisplayBufferText) | `"READY"` | human-readable; error text when faulted |
| Loaded media name | `1.3.6.1.2.1.43.8.2.1.12.1.1` (prtInputMediaName) | `"62mm / 2.4\""` | descriptive loaded-media string |
| **Media width** | `1.3.6.1.2.1.43.8.2.1.5.1.1` (prtInputMediaDimXFeedDir) | `6200` | hundredths of mm ⇒ 62.00 mm |
| **Media length/type** | `1.3.6.1.2.1.43.8.2.1.4.1.1` (prtInputMediaDimFeedDir) | `-1` | `-1`/`-2` ⇒ continuous; `>0` ⇒ die-cut length (hundredths mm) |
| Marker type | `1.3.6.1.2.1.43.11.1.1.6.1.1` | `"Thermal"` | |
| Cover status | `1.3.6.1.2.1.43.6.1.1.3.1.1` (prtCoverStatus) | — | open/closed |
| Model | `1.3.6.1.2.1.25.3.2.1.3.1` (hrDeviceDescr) | `"Brother QL-810W"` | cross-check vs `MODEL` |
| Model (1284 ID) | `1.3.6.1.4.1.2435.2.3.9.1.1.7.0` | `MFG:Brother;…;MDL:QL-810W;…` | Brother enterprise arc `2435` ⇒ multi-byte sub-id |
| Serial | `1.3.6.1.2.1.43.5.1.1.17.1` | `"B2Z160525"` | asset id |
| Firmware / NIC | `1.3.6.1.2.1.1.1.0` (sysDescr) | `Brother NC-36002w, Firmware Ver.1.00` | |
| Hostname | `1.3.6.1.2.1.1.5.0` (sysName) | `BRWF889D22FBB15` | |
| Lifetime label count | `1.3.6.1.2.1.43.10.2.1.4.1.1` (prtMarkerLifeCount) | `9` | Prometheus gauge; reconcile vs `labels_printed_total` |

The named OID constants live in `app/transports/snmp.py` (`OID_*`); they are the source of truth if
this table ever drifts.

### `hrPrinterDetectedErrorState` is a BITS value

The error mask is a `BITS` value carried as an `OCTET STRING`. In BITS encoding **bit 0 is the most
significant bit of the first octet**, so a one-octet mask uses the high bits of byte 0 — e.g. byte
`0x08` ⇒ bit 4 ⇒ `doorOpen`. The decoder keeps the raw octets (never the lossy UTF-8 decode of the
string) and re-indexes against the actual octet width, so the bit-name mapping is exact. The RFC
3805 bit names Labelito decodes (`HR_PRINTER_ERROR_BITS`):

```
0 lowPaper      1 noPaper        2 lowToner       3 noToner
4 doorOpen      5 jammed         6 offline        7 serviceRequested
8 inputTrayMissing   9 outputTrayMissing   10 markerSupplyMissing
11 outputNearFull    12 outputFull         13 inputTrayEmpty
14 overduePreventMaint
```

A non-zero mask with no recognised bit still registers as an error (`unknownErrorBits:0x…`) so a
faulted-but-unmapped state can never read as healthy.

### Media decode

- **Width** = `prtInputMediaDimXFeedDir / 100` mm (the value is in hundredths of a mm: `6200` ⇒ 62.00 mm).
- **Type/length** from `prtInputMediaDimFeedDir`: `-1`/`-2` ⇒ continuous (no discrete length); any
  other value ⇒ die-cut with length = `value / 100` mm.

## Implementation

`app/transports/snmp.py` is a **hand-rolled, synchronous, zero-dependency SNMPv1 GET client** — no
`puresnmp`/`pysnmp`. Production needs only v1 GET on a handful of known scalar OIDs in one round-trip
(no walks/GETNEXT — those were diagnostic via the `snmpwalk` CLI). Hand-rolling keeps the transport
layer uniformly synchronous (it runs in the existing `run_in_threadpool` worker model — no event
loop, no `asyncio.run()` footgun, no `ResourceWarning` under `filterwarnings=["error"]`), carries
zero supply-chain and forward-compat risk, and mirrors the codebase's existing hand-rolled brother_ql
binary parsing.

Three layers:

- **BER/ASN.1 primitives** — `_encode_*` / `_decode_tlv` (integer, octet-string, null, OID with
  base-128 multi-byte sub-ids for the `2435` arc, long-form lengths).
- **`snmp_get(host, community, oids, *, port, timeout, retries=1)`** — builds one v1 GetRequest, sends
  one UDP datagram (with one retry — UDP drops), decodes the reply, and returns `{oid: value}`.
- **`query_snmp_status(host, community, port, timeout)`** — one read of the status OIDs decoded into
  a `PrinterSNMPStatus`. **Never raises**: any failure ⇒ `reachable=False` + a warning log.

### Critical vs. optional OIDs (two GetRequests)

SNMPv1 fails a whole GET if **any** requested OID is unsupported (`noSuchName`). To stop a model that
omits a descriptive OID from taking down the safety read, the OIDs are split:

- **Critical** (`CRITICAL_STATUS_OIDS`) — only the values the print guard enforces on: the error
  bitmask + the loaded-media geometry (xfeed width, feed length/type). Fetched first, **all-or-nothing**:
  a reply that omits or mistypes one — even with `error-status=0` — is treated as **unreachable**
  (fail open), never decoded as a healthy printer with no errors.
- **Optional** (`OPTIONAL_STATUS_OIDS`) — descriptive media name, console text, `hrPrinterStatus`,
  cover, identity (model/serial/sysDescr/sysName), and the label lifecount. Fetched in a separate
  best-effort GetRequest; its failure simply leaves those fields absent and the guard still enforces.

### Anti-spoofing / robustness

The fail-open posture means a malformed or forged reply must never *clear* a real fault. The client:

- `connect()`s the UDP socket so the kernel drops datagrams from any source other than the printer;
- verifies the response **request-id echo** and requires a **GetResponse** PDU (`0xA2`) — a reflected
  GetRequest can't decode as a healthy printer;
- rejects SNMPv2 per-varbind exceptions (`noSuchObject`/`noSuchInstance`/`endOfMibView`) rather than
  letting their empty content decode to a benign value;
- type-checks the critical OIDs (error mask = bytes/int, dimensions = int) and treats a mistype as
  unreachable;
- only lets the optional read populate optional fields, so it can never overwrite the critical
  error/media state the first read established.

## Settings

SNMP applies **only when the transport inferred from `PRINTER_URI` is `network`** (`tcp://`). The
SNMP host is derived from `urlparse(PRINTER_URI).hostname` — there is no separate host setting. USB
and file transports ignore SNMP entirely.

| Variable | Default | Meaning |
|---|---|---|
| `SNMP_ENABLED` | `true` | Use SNMP as the network status channel + media guard. `false` skips both (fail open). |
| `SNMP_COMMUNITY` | `public` | SNMPv1 community string. |
| `SNMP_PORT` | `161` | SNMP UDP port (`1..65535`). |
| `SNMP_TIMEOUT` | `2.0` | Per-request receive timeout, seconds (`0 < t ≤ 60`). Short because it sits in the print pre-flight path. |

## Behaviour

- **`GET /printer/status`** (network transport) reports state from `PrinterStatus.from_snmp(...)`:
  connection, loaded media, decoded error state/console text, and identity. `200` with the body, or
  `503` (same body) when a print holds the lock or the printer is unreachable.
- **Pre-flight media guard.** `/print` and `/reprint` query SNMP before sending and reject a
  loaded-vs-required media mismatch with **`409 Conflict`** (the detail names both), incrementing
  `label_errors_total{reason="media_mismatch"}`. The web UI is **advisory only**: a mismatching
  template shows a red **✗** media badge and a `(needs …)` suffix on its dropdown option, but the
  option and the Print button stay enabled (preview/dry-run still work, and a stale client status
  must never hard-block) — the server's 409 is the authoritative gate. The editor is untouched.
- **Fail-open.** SNMP unreachable or `SNMP_ENABLED=false` ⇒ the guard logs a warning and proceeds;
  the UI badges status unknown (`?`). See the [accepted residuals](known-limitations.md#the-network-back-channel-is-silent--snmp-is-the-status-channel).
- **Telemetry** (opt-in, `METRICS_ENABLED=true`). SNMP-derived gauges — `printer_up`,
  `printer_detected_error_state{condition}`, `printer_label_lifecount`, `printer_info{model}`,
  `printer_media_info{media_name,media_type,width_mm}`, and
  `printer_status_last_query_timestamp_seconds`. They are refreshed **lazily** on each
  `/printer/status` call and each print — a bare `/metrics` scrape never triggers a live SNMP query
  (no per-scrape SNMP traffic or print-lock contention); the values may be stale, and the timestamp
  gauge makes that visible. `printer_info` exposes only the model — serial/firmware/hostname stay on
  the token-protected `/printer/status`, never on the unauthenticated metrics surface.

## Verifying against a real printer

From a host on the printer's network (replace the IP):

```bash
# loaded media name
snmpget -v1 -c public 192.168.5.14 1.3.6.1.2.1.43.8.2.1.12.1.1
# media width (hundredths of mm) and feed length/type sentinel
snmpget -v1 -c public 192.168.5.14 1.3.6.1.2.1.43.8.2.1.5.1.1 1.3.6.1.2.1.43.8.2.1.4.1.1
# error bitmask (00 == healthy)
snmpget -v1 -c public 192.168.5.14 1.3.6.1.2.1.25.3.5.1.2.1

# the same view through Labelito:
curl -s localhost:8765/printer/status -H "Authorization: Bearer $API_TOKEN" | jq
```

Tests that touch a real printer are gated behind the `hardware` pytest marker
(`uv run pytest -m hardware`); the default suite mocks SNMP at the socket / `query_status` seam and
runs with no printer present.

## Out of scope

- **SNMP viewer page** — the OID table above is the spec if it is ever revisited.
- **SNMP v3 auth** — the printer answers v1/v2c `public`.
- **Replacing the raster `send()` path** — the pre-flight SNMP guard is a backstop in front of the
  existing best-effort readback, not a replacement; a job rejected *after* the bytes are sent is
  still unobservable over this NIC.
