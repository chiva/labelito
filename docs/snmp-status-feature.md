# Feature task doc — SNMP-backed printer status, media cross-check & print guards

> Implementation task document for later execution (agent-leverageable). Each step lists **Goal /
> Files / Work / Acceptance**. Steps are mostly independent after Steps 1–4; do 1→4 first.

## Context

**Problem observed:** Printing from deployed labelito to the Brother QL-810W at
`tcp://192.168.5.14:9100` returned HTTP **200 success** while the printer **blinked red and printed
nothing**.

**Root cause (confirmed against the live printer):**
1. **Media mismatch.** Printer has **62mm continuous** tape loaded. Template `address.yaml` requests
   `label: "62x29"` (die-cut). The QL-810W rasterizes the job, then rejects it at the hardware level
   (loaded media ≠ requested media) → red blink, no output. The other 12 templates use `label: "62"`
   (continuous) and match.
2. **Phantom success.** The QL-810W's network NIC (`Brother NC-36002w`) **accepts the TCP connection
   on :9100 but never returns the 32-byte status frame** (verified: `recv` times out; back-channel is
   reliable over USB, not TCP). `NetworkTransport._read_status` returns `None`, and `_execute_print`
   (`app/main.py:767`) treats `None` as "no error reported" → records `printed`, returns 200. The
   hardware rejection is invisible.

**Why SNMP:** the printer answers **SNMP instantly** (UDP 161, community `public`) and exposes loaded
media, a reliable error bitmask, console status text, identity, and a lifetime label counter — the
status channel that actually works on this hardware. (Reference projects are *less* rigorous:
`pklaus/brother_ql_web` is pure fire-and-forget; `Dodoooh/brother_ql_app`'s "status" is a TCP
connect-liveness probe + SNMP/TCP keep-alive ping. Neither would have caught this.)

## Decisions (confirmed)

| Decision | Choice |
|---|---|
| SNMP client | **Hand-rolled minimal SNMP v1 GET, synchronous, zero-dependency** (see Step 1 rationale) |
| SNMP unreachable/disabled | **Fail-open + warn** — allow print, badge shows `?`, log warning |
| API reject code on media mismatch | **409 Conflict** (matches existing reprint-drift guards) |
| SNMP viewer page | **Skip for now** (OID table below is the spec if revisited) |
| Print dropdown over TCP | Disable mismatching templates in the print `<select>`; keep all selectable in the editor |
| Telemetry freshness | **Last-known, refreshed lazily** on `/printer/status` + each print (no background poll) |
| Live-printer / SNMP-on-real-hardware tests | Behind the **existing `hardware` pytest marker** (CI runs `-m "not hardware"`); default suite **mocks SNMP** |
| CI Python matrix | Add **3.14** (blocking) and **3.15** (experimental, `continue-on-error`) to the existing `3.12`/`3.13` matrix |
| Printer status card | **Minimal by default** (connection + media-if-TCP) with an **expand** for more values; prominent **green/red status bar/icon**; show error message on error |

## Reference: useful OIDs on the QL-810W (verified live)

| Purpose | OID | Live value | Notes |
|---|---|---|---|
| Printer status | `1.3.6.1.2.1.25.3.5.1.1.1` (hrPrinterStatus) | `idle(3)` | idle/printing/warmup/other |
| **Error bitmask** | `1.3.6.1.2.1.25.3.5.1.2.1` (hrPrinterDetectedErrorState) | `0x00` | non-zero ⇒ error; primary error signal |
| **Console text** | `1.3.6.1.2.1.43.16.5.1.2.1.1` (prtConsoleDisplayBufferText) | `"READY"` | human-readable; error text when faulted |
| **Loaded media name** | `1.3.6.1.2.1.43.8.2.1.12.1.1` (prtInputMediaName) | `"62mm / 2.4\""` | authoritative loaded-media string |
| Media width | `1.3.6.1.2.1.43.8.2.1.5.1.1` (prtInputMediaDimXFeedDir) | `6200` | hundredths of mm ⇒ 62.00mm |
| Media length/type | `1.3.6.1.2.1.43.8.2.1.4.1.1` (prtInputMediaDimFeedDir) | `-1` | `-1/-2` ⇒ continuous; `>0` ⇒ die-cut length |
| Marker type | `1.3.6.1.2.1.43.11.1.1.6.1.1` | `"Thermal"` | |
| Cover status | `1.3.6.1.2.1.43.6.1.1.3.1.1` (prtCoverStatus) | — | open/closed |
| Model | `1.3.6.1.2.1.25.3.2.1.3.1` (hrDeviceDescr) | `"Brother QL-810W"` | cross-check vs `MODEL` config |
| Model (1284 ID) | `1.3.6.1.4.1.2435.2.3.9.1.1.7.0` | `MFG:Brother;...;MDL:QL-810W;...` | |
| **Serial** | `1.3.6.1.2.1.43.5.1.1.17.1` | `"B2Z160525"` | asset id |
| Firmware / NIC | `1.3.6.1.2.1.1.1.0` (sysDescr) | `Brother NC-36002w, Firmware Ver.1.00` | |
| Hostname | `1.3.6.1.2.1.1.5.0` (sysName) | `BRWF889D22FBB15` | |
| **Lifetime label count** | `1.3.6.1.2.1.43.10.2.1.4.1.1` (prtMarkerLifeCount) | `9` | Prometheus gauge; reconcile vs `labels_printed_total` |

Community `public`, SNMP v1/v2c, UDP 161. Use **one `multiget`** for all status OIDs.

## Codebase facts (verified)

- **Transport seam:** `app/transports/base.py` — `Transport` Protocol (`send`, `close`,
  `query_status(request) -> PrinterStatus`); `PrinterStatus` frozen dataclass with extended optional
  fields + `synthetic_ok()`/`unreachable()`/`unsupported()`/`from_parsed()`; `register_transport`;
  `SCHEME_TO_TRANSPORT` (`tcp→network`, `usb→usb`, `file→file`); `infer_transport`.
- **`query_status` today:** network = silent ESC i S over :9100 (unreliable here); USB =
  `unsupported()`; file = `synthetic_ok()`.
- **Status endpoint:** `app/main.py` `GET /printer/status` (~L826) → `PrinterStatusResponse`
  (`app/models.py:459`). Same body for 200/503; uses `_print_lock` (503 busy when a print holds it).
  `PrinterState` enum: off/idle/printing/error.
- **Templates:** `GET /templates` → `TemplateInfo` (`app/models.py:283`) carries `label`. Index route
  (`app/main.py:~1912`) passes `templates`, `editor_enabled`, `history_ui`, `two_color`, defaults.
- **Label → media (authoritative):** `app/drivers/brother_ql.py` imports `ALL_LABELS` / `FormFactor`.
  Each label: **`tape_size = (width_mm, length_mm)`** (`length_mm == 0` for continuous) and
  **`form_factor`** (`FormFactor.ENDLESS` == `2` ⇒ continuous, e.g. `"62"`; `FormFactor.DIE_CUT` ==
  `1` ⇒ die-cut, e.g. `"62x29"` → `(62, 29)`). `_geometry()` already maps ENDLESS→"continuous" else
  "die_cut". `CAPABILITY.label_geometries[label]` → `LabelGeometry(width_px, height_px, media_type)`.
- **Settings:** `app/config.py` pydantic-settings, env-driven; `printer_uri` host →
  `urlparse(printer_uri).hostname` is the SNMP target (network only).
- **Web UI:** vanilla JS in inline `<script>`; shared `TOKEN_KEY='labelito_api_token'` localStorage,
  `authHeaders()`, `handleAuthError()`, `.card`/`.state-badge`/`.status` CSS; `renderPrinterStatus()`
  + `refreshPrinterStatus()` already in `index.html`. No new HTML page needed for this feature.
- **Tests:** `tests/test_transports.py` mocks `_FakeSocket`/`_ChunkedSocket` via
  `monkeypatch.setattr(net_mod.socket, "socket", ...)`; `tests/conftest.py` `client` fixture uses
  `file://` transport + monkeypatches `main_mod.settings`; `tests/test_api.py` mocks the transport via
  `_resolve_transport` returning a `_FakeTransport` subclass with bound `_status`.
- **Test markers (`pyproject.toml [tool.pytest.ini_options].markers`):** `hardware` already exists
  ("requires physical Brother printer"); CI runs `uv run pytest -m "not hardware"`. **All SNMP unit
  tests must mock** (`monkeypatch` the puresnmp call / the transport `query_status`) and run by
  default; **any test that touches the real printer or live SNMP must be marked `@pytest.mark.hardware`**
  so it is deselected by default and only runs on demand (`uv run pytest -m hardware`) when the printer
  is reachable. `filterwarnings = ["error", ...]` — warnings are errors, so new deps/code must be
  warning-clean.
- **CI (`.github/workflows/ci.yml`):** `test` job matrix is currently `["3.12","3.13"]`;
  `requires-python = ">=3.12"`; classifiers list 3.12/3.13. mypy pinned to `python_version = "3.13"`.
- **Deps:** uv-managed; `pyproject.toml` `dependencies = [...]`; mypy `strict=true`,
  `ignore_missing_imports=true`. Dev venv Python 3.14; prod image Python 3.13. **This feature adds no
  new runtime dependency** — SNMP is hand-rolled (Step 1).
- **Concurrency model (why SNMP stays sync):** routes are async; blocking work (`send`, pyusb, SNMP)
  runs via `run_in_threadpool` while the event loop stays free; `_print_lock` (asyncio.Lock) serializes
  to the one printer. Async would not help (blocking libs need threads anyway; max printer concurrency
  is 1), so the SNMP client is synchronous and runs in the same worker-thread model.

---

## Tasks

### [ ] Step 1 — SNMP client module (hand-rolled, synchronous, zero-dependency)
- **Files:** new `app/transports/snmp.py`. **No new dependency** (no puresnmp/pysnmp).
- **Rationale (decided):** production needs only **SNMP v1 GET on ~10 known scalar OIDs in one
  request** — no walks/GETNEXT (those were diagnostic via the `snmpwalk` CLI). A hand-rolled GET keeps
  the transport layer **uniformly synchronous** (runs in the existing `run_in_threadpool` worker model,
  no event loop, no `asyncio.run()` footgun, no `ResourceWarning`-as-error under
  `filterwarnings=["error"]`), is **zero supply-chain / zero 3.14·3.15 compat risk** (serves Step 14),
  and matches the codebase's existing hand-rolled brother_ql binary parsing. Async buys nothing here:
  the blocking transports (raw socket, pyusb) need threads regardless, and the printer is serialized to
  one job at a time, so there is no concurrency to exploit.
- **Module surface:**
  - BER/ASN.1 encode helpers: integer, octet-string, null, OID (base-128 sub-ids — the `2435`
    enterprise arc needs multi-byte), sequence, plus SNMP tags (GetRequest `0xA0`). Length encoding
    must handle `>127` (long form).
  - `snmp_get(host, community, oids, *, port=161, timeout, retries=1) -> dict[str, value]`: build a v1
    GetRequest PDU (version=0, a random-ish request-id, varbinds = OIDs with Null values), send **one
    UDP datagram** (`socket.SOCK_DGRAM`, `sendto`), `recvfrom` with timeout + **one retry** (UDP can
    drop), decode the response, verify request-id match and `error-status == 0`. Decode varbinds to
    python (int, str, OID, Counter32, TimeTicks).
  - Named OID constants (the table above) + `PrinterSNMPStatus` dataclass (`reachable`, `model`,
    `serial`, `firmware`, `hostname`, `console_text`, `error_state_bits`, `printer_status`,
    `media_name`, `media_width_mm`, `media_length_mm`, `media_type`, `cover_status`, `label_lifecount`,
    `errors: list[str]`).
  - `query_snmp_status(host, community, port, timeout) -> PrinterSNMPStatus`: one `snmp_get` for all
    OIDs. Decode width = `xfeed/100` mm; `feed in (-1,-2)` ⇒ continuous else die_cut(len=feed/100). Map
    non-zero `hrPrinterDetectedErrorState` bits + console text ≠ "READY" into `errors`. On
    timeout/socket/decode error → `reachable=False` (no raise; log warning).
- **Acceptance / tests:**
  - **Golden wire fixtures:** capture a real `snmpget` request+response off the wire (tcpdump, or
    reuse the verified live values) and assert the encoder produces the exact request bytes and the
    decoder parses the exact response bytes — this pins the BER edge cases (signed ints, OID sub-ids
    `>127`, long-form lengths).
  - Unit decode tests for canned OID maps (loaded-62-continuous, die-cut, error-bit-set,
    timeout→unreachable) assert media/error mapping.
  - Mock the UDP socket (`sendto`/`recvfrom`) — same fake-socket seam family as
    `tests/test_transports.py`. Any test hitting a **real** printer is `@pytest.mark.hardware`.

### [ ] Step 2 — SNMP settings
- **Files:** `app/config.py`.
- **Work:** `snmp_enabled: bool = True`, `snmp_community: str = "public"`, `snmp_port: int = 161`,
  `snmp_timeout: float = 2.0`. Host derived from `printer_uri` (no separate setting). Doc: SNMP applies
  only when transport is `network`.

### [ ] Step 3 — Route network status through SNMP
- **Files:** `app/transports/network.py`, `app/transports/base.py`.
- **Work:** add SNMP fields to `PrinterStatus` (+`serial`, `firmware`, `console_text`,
  `label_lifecount`, `cover`); add `PrinterStatus.from_snmp(PrinterSNMPStatus)`.
  `NetworkTransport.query_status`: when `settings.snmp_enabled`, call `query_snmp_status` for the
  transport host → `from_snmp(...)`; SNMP unreachable → `unreachable(...)`. Keep `send()`/`_read_status`
  as-is. USB stays `unsupported()`, file stays `synthetic_ok()`.
- **Acceptance:** `tests/test_transports.py` — network `query_status` returns SNMP fields when mocked;
  unreachable when mock raises.

### [ ] Step 4 — Media-compatibility core helper
- **Files:** `app/drivers/brother_ql.py` (or new `app/media.py`).
- **Work:** `required_media_for(label_id) -> {width_mm, media_type, length_mm|None}` from
  `ALL_LABELS` (`tape_size`, `form_factor`). `media_matches(required, loaded_snmp) ->
  "match"|"mismatch"|"unknown"`: `unknown` when SNMP unavailable; compare width (±1mm) and
  continuous-vs-die-cut (+ die-cut length).
- **Acceptance:** `tests/test_drivers.py` — `"62"`→62mm continuous, `"62x29"`→62×29 die-cut; vs
  loaded-62-continuous fixture: `address.yaml`⇒mismatch, others⇒match, SNMP-down⇒unknown.

### [ ] Step 5 — API enforcement (close the phantom-success hole)
- **Files:** `app/main.py`.
- **Work:** in `/print` and `/reprint`, when `infer_transport(printer_uri) == "network"` and
  `settings.snmp_enabled`: query SNMP, then `media_matches(required_media_for(tmpl.label), loaded)`:
  `mismatch` → **409** (detail names loaded vs required) + `label_errors_total{reason="media_mismatch"}`;
  `unknown` → **fail-open** (log + proceed). Also surface/reject when `error_state_bits != 0` (e.g.
  cover open). Run inside the existing `_print_lock`/threadpool flow.
- **Acceptance:** `tests/test_api.py` — mismatch⇒409; match⇒prints; SNMP-down⇒prints.

### [ ] Step 6 — Expose per-template media to the UI
- **Files:** `app/models.py`, `app/main.py`.
- **Work:** extend `TemplateInfo` with `media: {width_mm, media_type, length_mm|None}` (from
  `required_media_for`); include in `/templates` + index context. UI compares each template's `media`
  against `/printer/status` media client-side (mapping stays server-side).

### [ ] Step 7 — Print page: status card (minimal/expandable) + per-template compatibility + guards
- **Files:** `app/web/index.html` (rework `renderPrinterStatus()` and the `#status-card` markup/CSS).
- **Status card — prominent state indicator:** a clear **green/red status bar or filled colored
  icon** driven by `PrinterState`: **green** = idle/ready, **blue** = printing, **red** = error,
  **grey** = off/unreachable/unknown. On `error` (or `errors[]` non-empty), **show the error message**
  text inline next to the bar (e.g. console text / decoded error bits like "Cover open").
- **Minimal by default (always shown):**
  - **Connection** — `IP:port` when TCP (e.g. `192.168.5.14:9100`); else `USB` or `File` per the
    transport (derive from `data.uri` scheme / `/health.transport`).
  - **Media loaded** — only when TCP + reachable (e.g. `62mm continuous`).
- **Expandable ("Show details", e.g. a `<details>`):** model, serial, hostname, firmware, label
  lifecount, console text — render only the fields present (omit when null/not relevant).
- **Per-template compatibility:** next to the template `<select>`, a **green ✓ / red ✗ / grey ?** badge
  vs the loaded media + the template's required media text.
- **Dropdown disable (TCP only):** `<option disabled>` for mismatching templates with a
  "(needs 62×29 die-cut)" suffix; `unknown`/non-network ⇒ all enabled.
- **Print button:** disable when the selected template mismatches (Preview stays enabled); re-evaluate
  on `refreshPrinterStatus()` + template change.
- Editor untouched. XSS-safe DOM (textContent/createElement), mirror existing `.card`/`.state-badge`
  CSS conventions.

### [ ] Step 8 — Template studio: label-options doc dropdown + "Your Printer"
- **Files:** `app/web/editor.html`, `app/main.py` (editor route context); reuse `/capabilities` +
  `/printer/status`.
- **Work:** read-only "Label reference" section at the **bottom of the editor** listing every label
  the model supports + geometry (width mm × length, or "continuous"). When `/printer/status` reachable
  (network + SNMP), add a highlighted **"Your Printer"** entry showing loaded media + matching label
  id(s). Optional: click inserts the `label:` value into the YAML.
- **Acceptance:** editor route exposes capability context; UI mostly manual/e2e.

### [ ] Step 9 — Template YAML parameter documentation
- **Files:** new `docs/template-format.md`; optional help panel + link in `editor.html`.
- **Work:** document top-level keys (`name`, `description`, `label`, `rotate`), every supported layout
  element type + params, computed tokens (`{{date}}`, `{{now}}`, `{{seq}}`), i18n (`[[key]]`),
  required/optional field contract, input caps. **Source of truth — read, don't invent:** enumerate
  real element types/params from `app/loader.py`, `app/render/elements.py`, `app/render/engine.py`.
  Every example must parse via existing validation.

### [ ] Step 10 — Kubernetes health / probe endpoints
- **Files:** `app/main.py`, `README.md`.
- **Work:** `GET /livez` (liveness; always 200, no deps, unauthenticated). `GET /readyz` (readiness:
  templates loaded + transport scheme resolvable + history store open; **not** dependent on printer
  online; 200 ready / 503 not-ready with reasons). Keep human `/health`. Document example k8s
  `livenessProbe`/`readinessProbe`.
- **Acceptance:** `/livez`⇒200; `/readyz`⇒200 ready, 503 when forced not-ready (empty registry / bad
  transport).

### [ ] Step 11 — SNMP-derived telemetry & identity
- **Files:** `app/main.py` (alongside existing Prometheus metrics).
- **Work (network only):** `printer_up` (1/0), `printer_detected_error_state{condition=...}` (1/0 per
  bit: `cover_open`, `no_media`, `end_of_media`, `cutter_jam`, `replace_media`, …),
  `printer_label_lifecount`, `printer_info{model,serial,firmware,hostname}=1`,
  `printer_media_info{media_name,media_type,width_mm}=1`. **Skip:** uptime, `hrDeviceErrors`, remaining
  tape (QL doesn't measure it). **Freshness:** refresh gauges on `/printer/status` + each print; do
  NOT query SNMP per `/metrics` scrape; add `printer_status_last_query_timestamp_seconds`. Add
  `serial`/`firmware` to `/printer/status`.
- **Acceptance:** with SNMP mock fixtures, gauges set correctly; bare `/metrics` scrape triggers no
  live SNMP call.

### [ ] Step 12 — Docs & knowledge dump
- **Files:** this doc; update `docs/known-limitations.md` (QL network back-channel silent → SNMP is
  the status channel; fail-open guard) and `README.md` (new `SNMP_*` env vars, media-guard behavior).

### [ ] Step 13 — End-to-end verification
- `uv run pytest` green.
- Manual vs the real printer (`192.168.5.14`, 62mm continuous loaded):
  - `/printer/status` shows QL-810W, "62mm continuous", serial, console "READY".
  - `"62"` template ⇒ 200 + prints; badge green; option enabled.
  - `address.yaml` (62x29) ⇒ **409**; option disabled in print dropdown; Print button disabled; badge
    red. Still usable in the editor.
  - `SNMP_ENABLED=false` ⇒ fail-open (all enabled, badge `?`, prints).
  - Editor label-reference dropdown shows "Your Printer" flagging 62mm continuous + matching labels.
  - `/livez`⇒200; `/readyz`⇒200; `docs/template-format.md` examples parse.
- **Default suite mocks everything** (`uv run pytest -m "not hardware"` stays green with no printer).
  The live-printer checks above are the `-m hardware` set, run on demand when `192.168.5.14` is
  reachable.

### [ ] Step 14 — CI: Python 3.14 + 3.15 forward-compat matrix
- **Files:** `.github/workflows/ci.yml`, `pyproject.toml`.
- **Work:** extend the `test` job matrix from `["3.12","3.13"]` to include **`"3.14"`** (blocking).
  Add **`"3.15"`** as an **experimental** entry that does not fail the build — use
  `matrix.experimental` + `continue-on-error: ${{ matrix.experimental }}` (3.15 is pre-release; `uv
  python install 3.15` may need a pre-release/`--preview` channel — allow install failure too). Add the
  `Programming Language :: Python :: 3.14` classifier; keep `requires-python = ">=3.12"`. Goal: surface
  puresnmp / asyncio / stdlib breakage on 3.14/3.15 early without blocking on an unreleased Python.
- **Acceptance:** CI shows passing 3.12/3.13/3.14 jobs and a non-blocking 3.15 job; `uv lock --check`
  still passes (no new SNMP dependency was added, so the lockfile is unchanged by this feature).

## Verification commands
```
uv run pytest -q
uv run pytest tests/test_transports.py tests/test_drivers.py tests/test_api.py -v
snmpget -v1 -c public 192.168.5.14 1.3.6.1.2.1.43.8.2.1.12.1.1   # loaded media name (from printer's network)
curl -s localhost:8000/printer/status -H "Authorization: Bearer $TOKEN" | jq
```

## Out of scope
- SNMP viewer page (skipped; OID table above is the spec).
- SNMP v3 auth (printer answers v1/v2c `public`).
- Replacing the raster `send()` path — pre-flight SNMP guard + status card are the fix; `send()`
  readback keeps its best-effort `None`=unknown behavior, now backstopped by the pre-flight check.
