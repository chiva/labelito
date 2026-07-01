# Labelito

Self-hosted, containerized label printing for [Brother QL](https://www.brother.com/en/products/all/labelmachine/index.htm)
label printers — a small FastAPI service with declarative YAML templates, a live preview,
a web UI, and a clean HTTP API for automation (Home Assistant, scripts, cron, etc.).

```bash
curl -X POST http://localhost:8765/print \
  -H 'Content-Type: application/json' \
  -d '{"template":"freezer-icon","fields":{"title":"Bolognese sauce"}}'
```

→ renders a dated freezer label with a snowflake icon and prints it on your QL-810W in well under a second.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [Configuration & defaults](#configuration--defaults)
- [Supported printers](#supported-printers)
- [Label sizes](#label-sizes)
- [HTTP API](#http-api)
- [Job history & idempotency](#job-history--idempotency)
- [Template format](#template-format)
- [Home Assistant integration](#home-assistant-integration)
- [Recommendations](#recommendations)
- [Development](#development)
- [License](#license)

---

## What it does

You define labels once as **YAML templates** (`templates/*.yaml`). Each template declares a
media size, an optional rotation, and an ordered **layout** of elements (title, subtitle, text,
QR, barcode, image, icon, lines, boxes, spacers). At print time you `POST` a template name plus a
small `fields` dict; the service renders the layout to a PNG, converts it to Brother QL raster
data, and ships it to the printer over the network, USB, or to a file.

Highlights:

- **Declarative YAML templates** — drop a `.yaml` into `templates/` and hot-reload it with
  `POST /reload`; no rebuild, no restart.
- **Live preview** — `POST /preview` returns the exact `image/png` that would be printed, so you
  see the result before consuming a label.
- **Computed fields** — `{{date}}` and `{{now:%fmt}}` are resolved at render time,
  so callers don't pass dates (ideal for food-storage labels).
- **Pluggable drivers & transports** — adding a Brother QL model is a single capability-table
  entry; the transport layer (network / USB / file) is a registry of small classes.
- **Web UI** — template picker with dynamic field inputs, live preview, and a print button at `/`.
- **Observability** — Prometheus counters at `/metrics` and a `/health` endpoint for container
  health checks.
- **Fail-closed auth** — set `API_TOKEN` to require a bearer token on all write/preview
  endpoints. The service refuses to start with neither a token nor an explicit
  `ALLOW_UNAUTHENTICATED=true` opt-out.

## Architecture

```
HTTP request ─▶ FastAPI (app/main.py)
                  │
                  ├─ TemplateRegistry (app/loader.py)      load + validate templates/*.yaml
                  ├─ RenderEngine     (app/render/)        layout + fields ──▶ PNG
                  ├─ BrotherQLDriver  (app/drivers/)       PNG ──▶ QL raster bytes
                  └─ Transport        (app/transports/)    raster bytes ──▶ printer
                                                           (network | usb | file)
```

Job history (used by `POST /reprint/{job_id}` and idempotency de-duplication) is kept in a SQLite
store whose mode is set by `HISTORY_MODE` — `memory` (default, ephemeral), `file`
(`{DATA_DIR}/history.db`, durable), or `disabled`. The mode changes behaviour, not just
durability — see [Job history & idempotency](#job-history--idempotency).

## Quick start (Docker Compose)

The shipped `docker-compose.yml`:

```yaml
services:
  labelito:
    build: .
    image: ghcr.io/chiva/labelito:latest
    container_name: labelito
    restart: unless-stopped
    user: "${UID:-1000}:${GID:-1000}"  # non-root; ./data must be writable by this uid (1000 = image default)
    ports:
      - "8765:8765"
    volumes:
      - ./templates:/app/templates:ro     # your label templates (read-only)
      - ./translations:/app/translations:ro  # language catalogs (read-only)
      - ./assets/icons:/app/assets/icons:ro
      - ./fonts:/app/fonts:ro
      - ./data:/app/data                  # print history (persistent bind mount)
    environment:
      MODEL: QL-810W
      PRINTER_URI: tcp://192.168.1.100:9100  # transport is inferred from the scheme (tcp/usb/file)
      LABEL_SIZE: "62"
      DEFAULT_LANGUAGE: en               # label chrome language (per-request override available)
      HISTORY_MODE: file                 # durable reprint/dedup on the ./data bind (code default is memory)
      # Auth — no default secret is shipped; the service refuses to start unless ONE is set:
      #   put API_TOKEN=<your-secret> in a .env file (auto-loaded), OR
      #   comment the line below and set ALLOW_UNAUTHENTICATED for trusted-intranet open mode.
      API_TOKEN: ${API_TOKEN:?Set API_TOKEN in .env, or comment this and set ALLOW_UNAUTHENTICATED=true}
      # ALLOW_UNAUTHENTICATED: "true"    # run open (trusted intranet only; token is sniffable over plain HTTP)
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

Bring it up:

```bash
# 1. Clone
git clone https://github.com/chiva/labelito && cd labelito

# 2. Point it at your printer. Keep machine-specific overrides out of the
#    tracked compose file by using an override (auto-merged by Docker Compose):
cp docker-compose.yml docker-compose.override.yml
# edit MODEL / PRINTER_URI in docker-compose.override.yml

# 3. Build & run
docker compose up -d

# 4. Open the web UI
open http://localhost:8765        # or: xdg-open / just visit in a browser
```

Verify it's healthy and talking to the right printer:

```bash
curl -s http://localhost:8765/health | python -m json.tool
```

> **Finding your printer's address.** For network models (`-W` / `-NW` / `-NWB`), use the IP the
> printer reports on its config printout or in your router's DHCP table, with port `9100`:
> `tcp://<printer-ip>:9100`. Pin it with a DHCP reservation so it survives reboots.

## Configuration & defaults

All settings are environment variables (read from the process environment or a `.env` file in the
working directory; names are case-insensitive). Defaults come from `app/config.py`.

| Variable | Default | Description |
|---|---|---|
| `MODEL` | `QL-810W` | Brother QL model — must be in the [supported list](#supported-printers). |
| `PRINTER_URI` | `tcp://192.168.1.100:9100` | Printer address. The transport is **inferred from the scheme** — `tcp://` → network, `usb://` → usb, `file://` → file (see formats below). |
| `LABEL_SIZE` | `62` | Default media id reported by `/health`. Templates declare their own `label`. |
| `API_TOKEN` | *(unset)* | If set, all `/preview` and `/print*` / `/reload` endpoints require `Authorization: Bearer <token>`. The service **refuses to start** unless this or `ALLOW_UNAUTHENTICATED` is set. |
| `ALLOW_UNAUTHENTICATED` | `false` | Set `true` to explicitly run protected endpoints without a token (trusted intranet only). Logs a loud warning at startup. |
| `TEMPLATES_DIR` | `templates` (`/app/templates` in Docker) | Template search path. |
| `FONTS_DIR` | `fonts` (`/app/fonts`) | Custom TrueType font directory. Falls back to bundled DejaVu. |
| `ICONS_DIR` | `assets/icons` (`/app/assets/icons`) | Custom icon directory (svg/png), referenced by `icon` elements by filename. See [Icons](#icons). |
| `ICON_COLLECTIONS_DIR` | `assets/icon-collections` (`/app/assets/icon-collections`) | Bundled icon collections (FontAwesome/Material/Octicons) baked into the image. Read-only, not a volume. |
| `DATA_DIR` | `data` (`/app/data`) | Persistent state: the SQLite history DB in `file` mode (`history.db`). |
| `TRANSLATIONS_DIR` | `translations` (`/app/translations`) | Translation catalogs (`<lang>.yaml`) for `[[key]]` chrome words and locale date formats. |
| `DEFAULT_LANGUAGE` | `en` | Default label language; the per-request `language` field overrides it. Service fails fast at startup if this language has no catalog. |
| `HISTORY_MODE` | `memory` | Job-history backend: `memory` (in-process, reset on restart), `file` (durable SQLite at `{DATA_DIR}/history.db`), or `disabled` (no dedup, `/reprint` 404s). See [Job history & idempotency](#job-history--idempotency). |
| `HISTORY_KEEP_ENTRIES` | `1000` | Rows retained after a prune (SQLite modes). Bounds the reprint/dedup window. |
| `HISTORY_PRUNE_AT_ENTRIES` | `1500` | Prune triggers once the table exceeds this (hysteresis). Must be greater than `HISTORY_KEEP_ENTRIES`. |
| `HISTORY_UI` | `true` | Browse-history visibility, independent of storage. `false` makes `/history`, `/history/list`, and `DELETE /history/{job_id}` return 404 while **`/reprint`-by-id and idempotency de-dup keep working** (those follow `HISTORY_MODE`). Set `false` to keep reprint but never expose the printed-job list in the browser. |
| `EDITOR_ENABLED` | `false` | YAML template studio visibility. `false` (default) makes `GET /editor`, `POST /preview/draft`, `POST /templates/parse`, `GET /templates/{name}/source`, and `POST /templates` return 404, hiding the studio entirely. Set `true` to enable the in-browser template editor. Server-save (`POST /templates`) additionally requires `TEMPLATES_WRITABLE=true`. |
| `TEMPLATES_WRITABLE` | `false` | Permit `POST /templates` to persist a draft YAML to `TEMPLATES_DIR` and hot-reload the registry. Default false because docker-compose mounts `templates/` read-only. Requires `EDITOR_ENABLED=true` — enabling writable without the editor still 404s the save route. |
| `TEMPLATES_LOADABLE` | `true` | Permit the studio to load an existing template's raw YAML for editing via `GET /templates/{name}/source` (and show the "Load existing template" picker). Default true — read-only and safe: the name is resolved by an in-memory registry lookup, never as a filesystem path, so traversal/unrelated-file reads are impossible. Set `false` to hide the picker and 404 the route. Requires `EDITOR_ENABLED=true`. |
| `MIN_LENGTH_PX` | `200` | Minimum rendered length for **continuous** labels (clamps tiny labels up). |
| `MAX_LENGTH_PX` | `6000` | Maximum rendered length for continuous labels (guards against runaway height). |

**`PRINTER_URI` formats by transport:**

| Transport | URI example | Notes |
|---|---|---|
| `network` | `tcp://192.168.1.100:9100` | `tcp://<host>:<port>`. A malformed URI fails fast at startup — it never falls back to a default host. Accepts a hostname in place of a raw IP (see [Stable printer addresses](#stable-printer-addresses)). |
| `usb` | `usb://0x04f9:0x209c` | `vendorId:productId`, or a device path. Uses the `pyusb` backend (libusb is installed in the image). |
| `file` | `file:///tmp/out.bin` | Writes raster bytes to disk instead of printing. Great for debugging. |

### Stable printer addresses

DHCP can reassign your printer's IP after a reboot, breaking `PRINTER_URI`. Two zero-code fixes:

- **DHCP reservation (recommended).** In your router's DHCP settings, bind the printer's MAC address
  to a fixed lease (sometimes called "static DHCP" or "address reservation"). The IP stays the same
  forever. This is the most portable option — it works identically whether Labelito runs in Docker,
  bare-metal, or any other environment.

- **Hostname URI.** Brother network printers advertise a mDNS hostname of the form `BRWxxxxxx.local`
  (printed on the config sheet or visible in the router's device list). Use it in place of a raw IP:

  ```
  PRINTER_URI=tcp://BRW123456.local:9100
  ```

  The OS resolver (including mDNS/`.local` via avahi + nss-mdns on Linux) resolves the hostname at
  connect time, so the URI survives IP changes. The transport passes the hostname unchanged to the
  kernel — no resolution code is needed inside Labelito.

  **Caveat:** `.local` resolution requires avahi and nss-mdns in the runtime environment. If
  Labelito runs inside a Docker container using the default bridge network, those services may not
  be present. Either install avahi in the image, use `--network host`, or rely on a DHCP reservation
  instead. A plain DNS hostname (one your router assigns, e.g. `brother-ql.lan`) works without
  avahi as long as the container can reach your LAN's DNS server.

We intentionally do not implement printer auto-discovery (mDNS/SNMP browse): it suits interactive
multi-printer GUIs where a human picks a printer at print time; it adds no value to a headless
single-`PRINTER_URI` service.

## Supported printers

`MODEL` accepts any printer in the imported `brother_ql_next` model registry — currently **19**
Brother QL models, from the QL-500 through the QL-1115NWB. Capabilities (supported label sizes,
geometry, auto-cut support) are read straight from the library, so they always match
what actually rasterizes your labels. All run at 300 dpi.

The 102 mm wide media is restricted to the QL-1050/1060N/1100/1100NWB/1115NWB (the QL-1110NWB uses
the 62 mm set only). You don't configure any of this — query your configured model's exact
capabilities:

```bash
curl -s http://localhost:8765/capabilities | python -m json.tool
```

If `brother_ql_next` adds a model, it works here automatically with no code change. See
[CONTRIBUTING.md](CONTRIBUTING.md#adding-a-printer-driver) for adding an entirely new
(non-Brother) driver.

## Label sizes

A template's `label:` value selects the media geometry. `width_px` is the printable width at
300 dpi; **continuous** rolls have no fixed height (length grows with content, clamped to
`MIN_LENGTH_PX`..`MAX_LENGTH_PX`), while **die-cut** labels render to an exact canvas.

**62 mm media set** (all models): `12`, `29`, `38`, `50`, `54`, `62` (continuous) and `29x90`,
`39x90`, `62x29`, `62x100` (die-cut).
**102 mm models** additionally support `102` (continuous), `102x51`, `102x152` (die-cut).

Query the exact geometries your configured model supports:

```bash
curl -s http://localhost:8765/capabilities | python -m json.tool
```

## HTTP API

Interactive OpenAPI docs are served by FastAPI at `/docs` (and the schema at `/openapi.json`).

| Method | Path | Auth* | Description |
|---|---|:--:|---|
| `GET` | `/` | – | Web UI (template picker, preview, print). |
| `GET` | `/health` | – | Status, configured driver/model/transport/uri, template count, default language + loaded languages. |
| `GET` | `/livez` | – | Kubernetes liveness probe. Always `200` (`{"status":"alive"}`); no dependencies. |
| `GET` | `/readyz` | – | Kubernetes readiness probe. `200` when ready, `503` with per-check reasons otherwise. Checks templates loaded, transport scheme resolvable, history store open — **not** the printer. |
| `GET` | `/capabilities` | – | dpi, cut, supported label sizes + geometries. |
| `GET` | `/templates` | – | All templates with their required/optional field contracts. |
| `POST` | `/preview` | ✓ | Render a template → `image/png`. No printing, no history. |
| `POST` | `/preview/multipart` | ✓ | Same as `/preview` but accepts a `multipart/form-data` image upload (for `image` elements). |
| `POST` | `/print` | ✓ | Render → print. Records the job; supports `dry_run`. |
| `POST` | `/reprint/{job_id}` | ✓ | Reproduce a recorded job — replays the original date so the label is identical. Returns 404 when history is `disabled` or the job has been pruned. |
| `GET` | `/history` | ✓ | Browse-history web page (paginated list, reprint, delete). Returns 404 when `HISTORY_UI=false`. |
| `GET` | `/history/list` | ✓ | Paginated job history (`?offset=&limit=`), newest first. 404 when `HISTORY_UI=false`. |
| `DELETE` | `/history/{job_id}` | ✓ | Delete a single history entry. 404 when the entry is missing or `HISTORY_UI=false`. |
| `POST` | `/reload` | ✓ | Hot-reload all templates and translation catalogs. Valid files load; any malformed file is skipped and reported with a `422` (so a YAML typo can't silently drop a template). Returns `200` only when everything loaded cleanly. |
| `GET` | `/metrics` | – | Prometheus exposition (`labels_printed_total`, `label_errors_total`, `last_print_timestamp_seconds`). |

\* Only enforced when `API_TOKEN` is set.

### Kubernetes probes

`/livez` and `/readyz` are unauthenticated and side-effect free. Liveness only confirms the process
answers; readiness confirms the app can serve a print (templates loaded, transport scheme resolvable,
history store open) — it intentionally does **not** depend on the printer being online, so a transient
printer outage never pulls the pod out of its Service. Watch live printer state via `/printer/status`.

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 8765 }
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /readyz, port: 8765 }
  periodSeconds: 10
  failureThreshold: 3
```

### Print / preview request body

```jsonc
{
  "template": "freezer-icon",   // required — the template to render
  "fields": { "title": "..." }, // template-specific values
  "copies": 1,                   // 1..10, default 1
  "dry_run": false,              // true = render + record, but don't send to printer
  "cut": true,                   // auto-cut after printing
  "language": "es"               // optional — overrides DEFAULT_LANGUAGE for this label
}
```

`template` is **required** — the caller always names the label to print (omitting it or sending a
blank name returns `422`; an unknown name returns `404`). Naming a template does not bypass its
required-field contract: a named template still returns `422` if any required field is missing, so
it never prints a blank label. A present-but-blank value (empty or whitespace-only string) counts
as missing.

Example — preview to a file without a printer:

```bash
curl -s -X POST http://localhost:8765/preview \
  -H 'Content-Type: application/json' \
  -d '{"template":"title-subtitle","fields":{"title":"Hello","subtitle":"World"}}' \
  -o preview.png
```

With auth enabled, add `-H "Authorization: Bearer $API_TOKEN"`.

## Job history & idempotency

Every print is recorded in a small SQLite store. That record powers two things:

- **`/reprint/{job_id}`** — replays a recorded job (with its original date) so the label is
  identical.
- **Idempotency** — pass `"idempotency_key": "<unique-id>"` in a `/print` body and a retry with
  the *same* key and payload returns the original job instead of printing a second label. Reusing
  a key with a *different* payload is rejected with `409`. Without a key, identical requests print
  again (so you can intentionally print twice).

Because both features read the store, **`HISTORY_MODE` changes behaviour, not just durability:**

| Mode | Reprint / dedup | On restart | Use when |
|---|---|---|---|
| `memory` *(default)* | work within the running process | **reset** | you mostly care about the current run and don't want a file to manage |
| `file` | work, persisted to `{DATA_DIR}/history.db` | survive | you want reprint/dedup to outlive restarts (the Docker Compose default) |
| `disabled` | **off** — keyed retries reprint, `/reprint` 404s | n/a | you never reprint and accept duplicate-on-retry |

The store keeps at most `HISTORY_KEEP_ENTRIES` rows (default 1000), pruning the oldest once it
exceeds `HISTORY_PRUNE_AT_ENTRIES` (default 1500). A job older than that window can no longer be
reprinted and a stale key past it will reprint on retry — far beyond any realistic home batch.
Image jobs are recorded without the image blob, so they cannot be reprinted (re-submit the
original request); see [known limitations](docs/known-limitations.md).

## Template format

A template is a YAML file in `TEMPLATES_DIR`. Required top-level keys: `name`, `description`,
`label`, `layout`.

```yaml
name: freezer-icon                  # unique id used by the API
description: Freezer label with storage date + snowflake
label: "62"                          # media id (see Label sizes)
rotate: 90                           # 0 / 90 / 180 / 270 — rotate final image
fields:
  required: [title]                  # callers MUST provide these
  optional: [subtitle]               # used if present
layout:                              # ordered, stacked top-to-bottom
  - {type: icon,     name: snowflake, size: 90, align: right}
  - {type: title,    text: "{{title}}", max_lines: 2, bold: true}
  - {type: subtitle, text: "{{subtitle}}"}
  - {type: line}
  - {type: text,     text: "[[frozen]]: {{date}}", size: 30, align: center}
  - {type: spacer,   size: 8}
```

`[[frozen]]` is a **translation token** — see [Languages](#languages) below.

### Element types & their options

Unknown keys on an element are ignored, so it's safe to over-specify. Common options: `align`
(`left`/`center`/`right`).

| Type | Purpose | Key options (defaults) |
|---|---|---|
| `title` | Heading text | `text`, `max_lines` (2), `bold` (true), `align` (left); font ≈ 60 px |
| `subtitle` | Secondary text (skipped if empty) | `text`, `max_lines` (2), `bold` (false); font ≈ 40 px |
| `text` | Body text | `text`, `size` (32), `bold` (false), `max_lines` (none), `align` |
| `qr` | QR code | `data`, `size` (160), `align` (center); error-correction M |
| `barcode` | 1-D barcode | `data`, `symbology` (`code128`), `height` (60), `align` |
| `image` | Caller-supplied image | `field` (`image`) — base64 in `fields` or via `/preview/multipart`; `max_height` (200) |
| `icon` | A named server-side graphic | `name`, `size` (80), `align`, `collection`, `style` (solid) — see [Icons](#icons) |
| `line` | Horizontal rule | `thickness` (2), `margin` (8) |
| `box` | Empty bordered box | `height` (40), `border` (2) |
| `spacer` | Vertical whitespace | `size` (16) |
| `row` | Side-by-side columns on one line | `children` (list of elements), `align_items` (`center`), `spacing` (8) — see [Rows / columns](#rows--columns) |

### Rows / columns

By default each element takes a full-width line. A `row` lays several elements side by side on the
**same** line — e.g. text on the left, a glyph on the right:

```yaml
- type: row
  align_items: center        # row-wide vertical alignment: top | center | bottom
  spacing: 8                 # px gap between columns
  children:
    - type: text             # no width ⇒ flexible: fills the leftover space
      text: "{{label}}"
      align: left
    - type: icon             # fixed 80px column, pinned right
      width: 80
      size: 64
      align: right
      valign: top            # per-child override of the row's align_items
      name: check
      collection: fontawesome
```

Each child renders with its own options (`align`, `size`, …) inside its column.

**Column widths** — children with an explicit `width` (px) reserve that space first; the remainder
(after `spacing` gaps) is split among the rest in proportion to their `weight` (default `1`). So two
weightless children split 50/50; `weight: 3` vs `weight: 1` splits 75/25. If fixed widths exceed the
label, the fixed columns are scaled down to fit — and any flexible column keeps a small minimum
width so its content clips (visible) rather than collapsing to nothing.

**Vertical alignment** — `align_items` on the row sets the default; any child may override it with
`valign` (`top`/`center`/`bottom`). The default is `center`.

**Too-narrow data columns** — give `qr`, `barcode`, and `image` children enough column width to
draw into. A column smaller than a QR's `size`, or one that collapses a barcode/image to nothing,
renders a **crossed box** in that column instead of silently dropping the content — so a misjudged
width is obvious on the label (and in `/preview`) rather than producing a blank, unscannable result.
The label width is model-dependent, so check `/preview` for the label you'll print on.

Rows hold leaf elements only — a `row` cannot contain another `row`.

### Icons

The `icon` element draws a named, server-side graphic, from one of two sources:

- **Custom assets** — drop files into `ICONS_DIR` (`assets/icons/`). `{type: icon, name: snowflake}`
  resolves `snowflake.svg` then `snowflake.png` (the vector is preferred when both exist). Include an
  explicit suffix (`name: logo.png`) to pin a format. SVGs are rasterized; PNGs are used as-is. Both
  are reduced to 1-bit black-on-white.
- **Bundled collections** — set `collection` to render a glyph from an icon set baked into the image:

  ```yaml
  - {type: icon, collection: fontawesome, style: solid, name: mug-hot, size: 90}
  - {type: icon, collection: material,    name: ac_unit}
  - {type: icon, collection: octicons,    name: flame}
  ```

  | Collection | `collection` | `style` | License |
  |---|---|---|---|
  | [Font Awesome Free](https://fontawesome.com/) | `fontawesome` | `solid` (default) / `regular` / `brands` | CC BY 4.0 |
  | [Material Symbols](https://fonts.google.com/icons) | `material` | — | Apache 2.0 |
  | [Octicons](https://primer.style/octicons/) | `octicons` | — | MIT |

The collections live in `ICON_COLLECTIONS_DIR` (`assets/icon-collections/`), which is **baked into the
Docker image, not a runtime volume** — so mounting your own `assets/icons` cannot hide them. The image
builds them in a dedicated `pnpm` stage from `package.json` + `pnpm-lock.yaml` (exact, reproducible
versions; [Renovate](renovate.json) raises bump PRs after a 2-day release-age cooldown). For a non-Docker
checkout, run `scripts/fetch-icons.sh` to populate the directory (needs `pnpm` via corepack; the
directory is git-ignored). A missing, unknown, or unsafe `name`/`collection` renders a blank strip so the
rest of the label still prints. Each collection's license text ships alongside its SVGs.

### Computed fields

Any `text`, `data`, or `name` string is run through field substitution before rendering:

| Token | Resolves to |
|---|---|
| `{{<field>}}` | The matching value from the request `fields` (empty string if absent). |
| `{{date}}` | Current date in the active language's date format (e.g. `en` → `mm/dd/yyyy`, most of Europe → `dd/mm/yyyy`). |
| `{{date±Nunit}}` | Current date shifted by an offset, e.g. `{{date+5d}}`, `{{date+6m}}`, `{{date-1y}}`. Units: `d`ays, `w`eeks, `m`onths, `y`ears. Month/year arithmetic is calendar-aware (Jan 31 + 1m → Feb 28). |
| `{{now}}` / `{{now:%fmt}}` | `datetime.now().strftime(fmt)`; default format is the active language's datetime format. Accepts the same `±Nunit` offset and an explicit format, e.g. `{{now+1d:%d/%m}}`. |

### Languages

Static "chrome" words on a template (e.g. *Frozen* / *Expires*) are written as `[[key]]`
**translation tokens**, resolved per label from a catalog in `TRANSLATIONS_DIR`. Tokens are
resolved *before* `{{field}}` substitution, so user-supplied values are never translated.

The active language is `DEFAULT_LANGUAGE` (default `en`), overridable per request with the
`language` field. It also selects the date format used by `{{date}}`/`{{now}}`.

```bash
# Same template, two languages — German chrome + German date format
curl -sX POST http://localhost:8765/preview -H 'Content-Type: application/json' \
  -d '{"template":"freezer-dated","fields":{"title":"Suppe"},"language":"de"}' -o de.png
```

A catalog is a flat `translations/<code>.yaml` of `key: "word"` pairs, plus optional reserved
keys `_date_format` / `_datetime_format` (Python `strftime`):

```yaml
# translations/de.yaml
frozen: "Eingefroren"
stored: "Gelagert"
expires: "Haltbar bis"
_date_format: "%d.%m.%Y"
_datetime_format: "%d.%m.%Y %H:%M"
```

Eight languages ship today — **en, es, fr, de, it, pt, nl, pl** (the kitchen templates use
`[[frozen]]`/`[[stored]]`/`[[expires]]`). A missing key falls back to the default language and
then to the raw key, so a partial translation never breaks rendering. `GET /health` lists the
loaded `languages` and the `default_language`. To restore the prior Spanish/European output set
`DEFAULT_LANGUAGE=es`. Adding a language is a one-file PR — see
[CONTRIBUTING.md](CONTRIBUTING.md#adding-a-language).

The repo ships 11 ready-to-use templates:
- **Kitchen** — `freezer-dated` & `fridge-dated` (auto-stamp storage *and* computed best-before dates), `freezer-icon`, `pantry`
- **Generic** — `simple-text`, `title-subtitle`, `title-subtitle-qr`, `storage-box-qr`
- **Homelab / logistics** — `cable-label` (cable ID + endpoints), `asset-tag` (Code128 barcode + location), `address` (62×29 mm die-cut)

## Home Assistant integration

Drive printing by voice or automation via HA's `rest_command`. Add to `configuration.yaml`:

```yaml
rest_command:
  print_label:
    url: "http://label-printer:8765/print"
    method: POST
    content_type: "application/json"
    payload: >
      {"template": "{{ template }}",
       "fields": {"title": "{{ title }}", "subtitle": "{{ subtitle | default('') }}"},
       "copies": {{ copies | default(1) }}}
```

```yaml
intent_script:
  PrintFreezerLabel:
    action:
      action: rest_command.print_label
      data:
        template: "freezer-icon"
        title: "{{ label_text }}"
    speech:
      text: "Imprimiendo etiqueta de congelador para {{ label_text }}"
```

Voice: *"imprime etiqueta de congelador salsa boloñesa"* → the freezer label prints, auto-dated.

> If `API_TOKEN` is set, add a bearer header to the `rest_command` via the `headers:` key.

## Recommendations

- **Transport.** Prefer `network` for the Wi-Fi/Ethernet models (`-W`/`-NW`/`-NWB`). Pin the
  printer's IP with a DHCP reservation, or use a hostname URI (`tcp://BRWxxxxxx.local:9100`) so IP
  changes don't break printing — see [Stable printer addresses](#stable-printer-addresses) for
  trade-offs. Use `usb` only for directly-attached printers (Linux host; pass the USB device into
  the container). Use `file` purely for debugging.
- **Default media.** 62 mm continuous (`label: "62"`) is the most flexible choice — content-driven
  length, no wasted label. Reach for die-cut sizes only when you need a fixed footprint.
- **Auth & exposure.** The service fails closed: it won't start without either `API_TOKEN` or an
  explicit `ALLOW_UNAUTHENTICATED=true`. Prefer a token whenever the service is reachable beyond a
  trusted LAN — it guards every write/preview path. The bearer token is sent in clear over plain
  HTTP, so for anything past a trusted intranet put Labelito behind a TLS-terminating reverse proxy
  rather than relying on the token alone. Don't expose `8765` to the public internet.
- **Request size.** Bodies over ~8 MiB are rejected (413) by `Content-Length` before being read,
  bounding upload memory use. A body-bearing request (`POST`/`PUT`/`PATCH`) that omits
  `Content-Length` — i.e. a chunked upload — is rejected with `411`, so the guard can't be
  bypassed; a public-facing deployment should still set a hard body limit at the reverse proxy.
- **Volumes.** Keep `templates/`, `fonts/`, `assets/icons/` mounted read-only; keep `data/` on a
  host bind mount (`./data`). With `HISTORY_MODE=file` (the Compose default) the reprint history
  lives there and survives container recreation; the code default `memory` keeps no file and
  resets on restart.
- **Non-root.** The image runs as uid 1000 (`app`) by default, and Compose sets
  `user: "${UID:-1000}:${GID:-1000}"`. The only path written at runtime is `./data` (the history
  DB), so that host directory must be writable by the chosen uid — uid 1000 matches the image
  default. To run as the invoking host user instead: `UID=$(id -u) GID=$(id -g) docker compose up`
  (or set `UID`/`GID` in `.env`), and `chown` `./data` to that uid.
- **Iterate with preview.** Use `POST /preview` (or `dry_run: true` on `/print`) while designing a
  template so you don't burn label stock.
- **Fonts.** The image installs DejaVu via the `fonts-dejavu-core` apt package (at a system path
  *outside* the `fonts/` volume), so rendering works even if you mount an empty `FONTS_DIR`. The
  `fonts/` volume is a **DejaVu override slot**, not a general font store: the renderer loads
  `DejaVuSans.ttf`/`DejaVuSans-Bold.ttf` by name, so drop files with *those* names into `FONTS_DIR`
  to replace the bundled faces. A differently-named `.ttf` is ignored (templates can't select a
  font family).

## Development

The project uses [**uv**](https://docs.astral.sh/uv/) for dependency management and
[hatchling](https://hatch.pypa.io/) for builds. Python **3.12+** is required.

> **New here?** [docs/development.md](docs/development.md) is the full onboarding guide, and a VS
> Code / Codespaces **dev container** (`.devcontainer/`) provisions Python + uv + system libs in one
> click.

```bash
# Install uv (https://docs.astral.sh/uv/getting-started/installation/), then:
uv sync                       # create .venv and install all dependency groups
uv run pre-commit install     # enable lint/format/type hooks on commit

# Run the service locally (file:// scheme = file transport, no printer needed)
PRINTER_URI=file:///tmp/out.bin \
  uv run uvicorn app.main:app --reload --port 8765
```

For **faithful local previews**, fetch the printer's font once — the Docker image gets DejaVu from
apt, but a bare macOS/Windows host has no copy, so previews fall back to a different OS font (or, if
none is found, a bitmap that renders arrows/accents as boxes) and won't match the printed label:

```bash
scripts/fetch-fonts.sh          # download DejaVu into ./fonts (git-ignored); validates the glyphs
```

Without it the app still runs and warns once that the preview is off-font.

Quality gates (mirrored in CI and `.pre-commit-config.yaml`):

```bash
uv run pytest -m "not hardware"      # full suite, skipping tests that need a real printer
uv run pytest                        # everything, including hardware-marked tests
uv run ruff check . && uv run ruff format --check .
uv run mypy app/                     # strict mode
```

- **Tests** live in `tests/`; the `hardware`, `e2e`, and `slow` markers are declared in
  `pyproject.toml`. Warnings are errors (`filterwarnings = ["error", ...]`).

### End-to-end harness (browser + API)

`tests/e2e/` drives a **real** uvicorn process (printer-less `file://` sink, in-memory history) with
a real browser via Playwright — covering the web UI and the HTTP API end-to-end, complementing the
in-process `TestClient` unit tests. It is opt-in (it downloads a browser and starts a server), so it
is **skipped by default** and enabled with `--e2e`:

```bash
uv run playwright install chromium      # one-time: download the browser
uv run pytest --e2e -m e2e              # run the e2e suite
```

The same launcher backs a **manual dev harness** that starts the server with a default API token and
opens a browser to the page with that token pre-filled, so preview/print work immediately:

```bash
uv run python scripts/dev_harness.py            # open a browser to the running app
uv run python scripts/dev_harness.py --no-browser   # just run the server; open it yourself
uv run python scripts/dev_harness.py --check        # one-shot headless smoke (CI / AI agents)
```

The default token is a **harness-only** value (`tests/e2e/harness.py`, overridable via
`LABELITO_E2E_TOKEN` or `--token`) — the service itself still refuses to start without an explicit
token. AI agents can drive the page headlessly with `pytest --e2e` or `dev_harness.py --check`.
- **Coverage** target is **≥85%** on `app/` (`fail_under = 85`); the raw network/USB transports are
  excluded from coverage since they require hardware.
- **Style:** Ruff (line length 100) + mypy `strict`. Prefer self-documenting code over comments.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/); releases are managed by
  release-please.

Build the package or image directly:

```bash
uv build                                   # wheel + sdist into dist/
docker build -t ghcr.io/chiva/labelito .    # multi-stage image (uv builder → slim runtime)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for adding printer models/drivers and the full PR checklist.

## License

[GPL-3.0-or-later](LICENSE). This is required: the service imports `brother_ql` (via
`brother_ql_next`), which is GPL-3.0-or-later, pinning the combined work to GPL regardless of
individual file headers. Contributions are inbound = outbound under the same license.
