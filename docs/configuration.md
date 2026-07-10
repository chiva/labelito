# Configuration reference

All settings are environment variables (read from the process environment or a `.env` file in the
working directory; names are case-insensitive). Defaults come from `app/config.py`.

- [Environment variables](#environment-variables)
- [`PRINTER_URI` formats by transport](#printer_uri-formats-by-transport)
- [Stable printer addresses](#stable-printer-addresses)
- [Label sizes](#label-sizes)
- [Job history & idempotency](#job-history--idempotency)
- [Deployment & security notes](#deployment--security-notes)

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MODEL` | `QL-810W` | Brother QL model — must be in the supported list (see [Supported printers](#label-sizes) / `GET /capabilities`). |
| `PRINTER_URI` | `tcp://192.168.1.100:9100` | Printer address. The transport is **inferred from the scheme** — `tcp://` → network, `usb://` → usb, `file://` → file (see formats below). |
| `API_TOKEN` | *(unset)* | If set, the protected endpoints (`/preview`, `/print*`, `/reprint/{job_id}`, `/reload`, `GET /templates`, history/studio routes) require `Authorization: Bearer <token>`. The service **refuses to start** unless this, `WEB_AUTH_USER`/`WEB_AUTH_PASSWORD`, or `ALLOW_UNAUTHENTICATED` is set. Optional (bearer for scripts/automation) once HTTP Basic auth is configured — endpoints accept either credential. |
| `WEB_AUTH_USER` | *(unset)* | Username for optional **HTTP Basic auth** on the web UI. Set together with `WEB_AUTH_PASSWORD` (both or neither, non-blank — a half-configured pair fails startup). When enabled, the **whole UI is a login wall**: the page shells (`/`, `/editor`, `/history`) and the protected API sit behind the native browser login. The browser then re-sends its credentials on every same-origin request automatically, so no in-page API-token entry is shown. Best for deployments **without** a reverse proxy that want the UI protected. |
| `WEB_AUTH_PASSWORD` | *(unset)* | Password for HTTP Basic auth (see `WEB_AUTH_USER`). |
| `WEB_AUTH_REALM` | `labelito` | Realm string shown in the browser's Basic-auth login dialog. |
| `ALLOW_UNAUTHENTICATED` | `false` | Set `true` to explicitly run without any app-level auth (trusted intranet, or auth handled by a reverse proxy). Logs a loud warning at startup. Only satisfies the startup guard when neither `API_TOKEN` nor `WEB_AUTH_*` is set. |
| `TEMPLATES_DIR` | `templates` (`/app/templates` in Docker) | Template search path — the user/override slot. Loaded **in addition** to `EXAMPLE_TEMPLATES_DIR`; a file here wins over a bundled example of the same internal `name`. May be empty. |
| `EXAMPLE_TEMPLATES_DIR` | `templates` (`/app/examples/templates` in Docker) | Bundled example templates, baked into the image **outside** the `TEMPLATES_DIR` volume so a bind-mount can't shadow them and upgrades ship new examples. Read-only, not a volume. Defaults to `TEMPLATES_DIR` on bare-metal (loaded once). |
| `FONTS_DIR` | `fonts` (`/app/fonts`) | Custom TrueType font directory. Falls back to bundled DejaVu. |
| `ICONS_DIR` | `assets/icons` (`/app/assets/icons`) | Custom icon directory (svg/png), referenced by `icon` elements by filename. See [template format → Icons](template-format.md). |
| `ICON_COLLECTIONS_DIR` | `assets/icon-collections` (`/app/assets/icon-collections`) | Bundled icon collections (FontAwesome/Material/Octicons) baked into the image. Read-only, not a volume. |
| `DATA_DIR` | `data` (`/app/data`) | Persistent state: the SQLite history DB in `file` mode (`history.db`). |
| `TRANSLATIONS_DIR` | `translations` (`/app/translations`) | Translation catalogs (`<lang>.yaml`) for `[[key]]` chrome words and locale date formats — the user/override slot. Loaded **on top of** `EXAMPLE_TRANSLATIONS_DIR` (a catalog here overrides the bundled one for that language; new languages are added). May be empty. |
| `EXAMPLE_TRANSLATIONS_DIR` | `translations` (`/app/examples/translations` in Docker) | Bundled translation catalogs, baked **outside** the `TRANSLATIONS_DIR` volume (same anti-shadowing split as templates). Guarantees the `DEFAULT_LANGUAGE` catalog always exists — so an empty `TRANSLATIONS_DIR` mount no longer crashes startup. Read-only, not a volume. |
| `LOAD_EXAMPLES` | `true` | Load the bundled example templates **and** translation catalogs. Set `false` to load **only** your own `TEMPLATES_DIR`/`TRANSLATIONS_DIR` — the shipped examples are skipped entirely. With examples off and an empty `TRANSLATIONS_DIR`, there is no `DEFAULT_LANGUAGE` catalog: startup **warns** (no longer fails) and `[[key]]` chrome words render as their raw key until you provide one. |
| `DEFAULT_LANGUAGE` | `en` | Default label language; the per-request `language` field overrides it. If this language has no catalog in either dir (e.g. `LOAD_EXAMPLES=false` with an empty `TRANSLATIONS_DIR`), startup warns and `[[key]]` words render as their raw key — it is not fatal. The bundled `en` satisfies the default whenever `LOAD_EXAMPLES` is on. |
| `HISTORY_MODE` | `memory` | Job-history backend: `memory` (in-process, reset on restart), `file` (durable SQLite at `{DATA_DIR}/history.db`), or `disabled` (no dedup, `/reprint` 404s). See [Job history & idempotency](#job-history--idempotency). |
| `HISTORY_KEEP_ENTRIES` | `1000` | Rows retained after a prune (SQLite modes). Bounds the reprint/dedup window. |
| `HISTORY_PRUNE_AT_ENTRIES` | `1500` | Prune triggers once the table exceeds this (hysteresis). Must be greater than `HISTORY_KEEP_ENTRIES`. |
| `HISTORY_UI` | `true` | Browse-history visibility, independent of storage. `false` makes `/history`, `/history/list`, and `DELETE /history/{job_id}` return 404 while **`/reprint`-by-id and idempotency de-dup keep working** (those follow `HISTORY_MODE`). Set `false` to keep reprint but never expose the printed-job list in the browser. |
| `EDITOR_ENABLED` | `false` | YAML template studio visibility. `false` (default) makes `GET /editor`, `POST /preview/draft`, `POST /templates/parse`, `GET /templates/{name}/source`, and `POST /templates` return 404, hiding the studio entirely. Set `true` to enable the in-browser template editor. Server-save (`POST /templates`) additionally requires `TEMPLATES_WRITABLE=true`. |
| `TEMPLATES_WRITABLE` | `false` | Permit `POST /templates` to persist a draft YAML to `TEMPLATES_DIR` and hot-reload the registry. Default false because docker-compose mounts `templates/` read-only. Requires `EDITOR_ENABLED=true` — enabling writable without the editor still 404s the save route. |
| `TEMPLATES_LOADABLE` | `true` | Permit the studio to load an existing template's raw YAML for editing via `GET /templates/{name}/source` (and show the "Load existing template" picker). Default true — read-only and safe: the name is resolved by an in-memory registry lookup, never as a filesystem path, so traversal/unrelated-file reads are impossible. Set `false` to hide the picker and 404 the route. Requires `EDITOR_ENABLED=true`. |
| `INLINE_TEMPLATES_ENABLED` | `false` | Accept a full template body inline on `POST /print` and `POST /preview` via the `template_inline` field (mutually exclusive with `template`), instead of only a stored name — so a client / git repo / integration can hold the template off-platform and print it per request with no save step. The body runs through the **same** validation as a saved file and, on `/print`, is frozen into history so `/reprint` reproduces it. Default false: template authoring is otherwise doubly gated (`EDITOR_ENABLED` + `TEMPLATES_WRITABLE`), so letting any print-token holder submit template DSL is an opt-in posture change. Disabled ⇒ an inline request is `403`. Inline jobs count under `labels_printed_total{template="<inline>"}`. See [template format → Inline templates](template-format.md#inline-templates-printing-without-storing). |
| `MIN_LENGTH_PX` | `200` | Minimum rendered length for **continuous** labels (clamps tiny labels up). |
| `MAX_LENGTH_PX` | `6000` | Maximum rendered length for continuous labels (guards against runaway height). |
| `SNMP_ENABLED` | `true` | Use SNMP (UDP 161) as the printer status channel for the **network** transport. Brother's NIC accepts the `:9100` print connection but never returns the status back-channel, so without SNMP a hardware-rejected print reports phantom success (see [SNMP status & the media guard](snmp-status.md)). SNMP supplies the loaded media, a reliable error bitmask, console text, identity, and the lifetime label counter — and backs the pre-flight media-mismatch guard. Ignored for `usb://`/`file://`. Set `false` to skip the status query and the guard (prints proceed, status badges as unknown). |
| `SNMP_COMMUNITY` | `public` | SNMPv1 community string. The QL-810W answers v1/v2c `public`. |
| `SNMP_PORT` | `161` | SNMP UDP port on the printer (`1..65535`). |
| `SNMP_TIMEOUT` | `2.0` | Per-request SNMP receive timeout in seconds (`0 < t ≤ 60`). Kept short because the status read sits in the print pre-flight path; an unreachable printer **fails open** (warn + proceed) rather than stalling the request. |
| `METRICS_ENABLED` | `false` | Prometheus exposition is **opt-in**. While disabled (default) the metrics endpoint 404s as if absent; set `true` to expose it. Telemetry gauges are still updated in-memory regardless — just not served until enabled. The endpoint carries **no auth** (Prometheus scrapers don't send tokens), so restrict it at the network layer if the deployment is not trusted. `printer_info` exposes only the model; serial/hostname stay on the token-protected `/printer/status`. |
| `METRICS_PATH` | `/metrics` | Path the exposition is served at (when enabled) — on the **same port/app** as the web UI (there is no separate metrics port). Relocate it (e.g. `/internal/metrics`) if convenient; it is not advertised in `/openapi.json`. Read at startup. |
| `PROXY_PATH_HEADER` | *(unset)* | Name of the request header carrying a reverse-proxy **path prefix** (e.g. `X-Ingress-Path` under Home Assistant ingress). When set, the header's value becomes the base path for every generated URL — page links, static assets, `/docs`, the OpenAPI `servers` entry — while route matching is unchanged (the proxy strips the prefix before forwarding). Only enable behind a proxy that sets or overwrites this header on **every** request; when unset (default) the header is ignored. Values not starting with `/` are dropped. |
| `UPDATE_CHECK_ENABLED` | `false` | Report whether a newer release exists in the About modal (and a small dot on the nav info icon when one is). The server queries **GitHub's release API** for the repo, cached ~6h, so it makes at most one outbound call per interval regardless of open tabs. This is the **one outbound call the service makes on its own**, so it is **off by default** — no GitHub egress from a bare install. **`docker-compose.yml` sets it `true`** since that is the primary, connected deployment path; set `true` elsewhere to enable, or leave `false` for air-gapped/privacy hosts. The lookup **fails soft**: a timeout or error just shows no update, never an error. |
| `LOG_LEVEL` | `INFO` | Application log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (case-insensitive). An unknown name **fails startup** with a legible settings error rather than silently logging at the wrong level. Read at startup. |

## `PRINTER_URI` formats by transport

| Transport | URI example | Notes |
|---|---|---|
| `network` | `tcp://192.168.1.100:9100` | `tcp://<host>:<port>`. A malformed URI fails fast at startup — it never falls back to a default host. Accepts a hostname in place of a raw IP (see [Stable printer addresses](#stable-printer-addresses)). |
| `usb` | `usb://0x04f9:0x209c` | `vendorId:productId`, or a device path. Uses the `pyusb` backend (libusb is installed in the image). |
| `file` | `file:///tmp/out.bin` | Writes raster bytes to disk instead of printing. Great for debugging. |

## Stable printer addresses

DHCP can reassign your printer's IP after a reboot, breaking `PRINTER_URI`. Two zero-code fixes:

- **DHCP reservation (recommended).** In your router's DHCP settings, bind the printer's MAC address
  to a fixed lease (sometimes called "static DHCP" or "address reservation"). The IP stays the same
  forever. This is the most portable option — it works identically whether labelito runs in Docker,
  bare-metal, or any other environment.

- **Hostname URI.** Brother network printers advertise a mDNS hostname of the form `BRWxxxxxx.local`
  (printed on the config sheet or visible in the router's device list). Use it in place of a raw IP:

  ```
  PRINTER_URI=tcp://BRW123456.local:9100
  ```

  The OS resolver (including mDNS/`.local` via avahi + nss-mdns on Linux) resolves the hostname at
  connect time, so the URI survives IP changes. The transport passes the hostname unchanged to the
  kernel — no resolution code is needed inside labelito.

  **Caveat:** `.local` resolution requires avahi and nss-mdns in the runtime environment. If
  labelito runs inside a Docker container using the default bridge network, those services may not
  be present. Either install avahi in the image, use `--network host`, or rely on a DHCP reservation
  instead. A plain DNS hostname (one your router assigns, e.g. `brother-ql.lan`) works without
  avahi as long as the container can reach your LAN's DNS server.

We intentionally do not implement printer auto-discovery (mDNS/SNMP browse): it suits interactive
multi-printer GUIs where a human picks a printer at print time; it adds no value to a headless
single-`PRINTER_URI` service.

## Label sizes

`MODEL` accepts any printer in the imported `brother_ql_next` model registry — currently **19**
Brother QL models, from the QL-500 through the QL-1115NWB. Capabilities (supported label sizes,
geometry, auto-cut support) are read straight from the library, so they always match what actually
rasterizes your labels. All run at 300 dpi. If `brother_ql_next` adds a model, it works here
automatically with no code change.

A template's `label:` value selects the media geometry. `width_px` is the printable width at
300 dpi; **continuous** rolls have no fixed height (length grows with content, clamped to
`MIN_LENGTH_PX`..`MAX_LENGTH_PX`), while **die-cut** labels render to an exact canvas.

- **62 mm media set** (all models): `12`, `29`, `38`, `50`, `54`, `62` (continuous) and `29x90`,
  `39x90`, `62x29`, `62x100` (die-cut).
- **102 mm models** additionally support `102` (continuous), `102x51`, `102x152` (die-cut). The
  102 mm wide media is restricted to the QL-1050/1060N/1100/1100NWB/1115NWB (the QL-1110NWB uses the
  62 mm set only).

You don't configure any of this — query your configured model's exact capabilities:

```bash
curl -s http://localhost:8765/capabilities | python -m json.tool
```

See [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-printer-driver) for adding an entirely new
(non-Brother) driver.

## Job history & idempotency

Every print is recorded in a small SQLite store. That record powers two things:

- **`/reprint/{job_id}`** — replays a recorded job (with its original date) so the label is
  identical. On the History page, when the loaded roll is known (SNMP), rows whose template needs a
  different roll are dimmed and their **Reprint** is disabled with the reason inline — advisory only,
  mirroring the print page; the server still enforces the same media check and `409`s a real
  mismatch. The page background-polls printer status on the SNMP path (same gate as the print page),
  so swapping the roll re-gates the rows live with no reload. With the roll unknown (non-SNMP /
  unreachable) there is no poll and every row stays reprintable.
- **Idempotency** — pass `"idempotency_key": "<unique-id>"` in a `/print` body and a retry with
  the *same* key and payload returns the original job instead of printing a second label. Reusing
  a key with a *different* payload is rejected with `409`. Without a key, identical requests print
  again (so you can intentionally print twice).

  The fingerprint covers the request payload (fields, options, language, sequence) — **not** the
  render date. So a template whose output is entirely clock-derived (e.g. the `today` label, or a
  dated label reprinted with an unchanged title) has an *identical* payload from one day to the
  next. Where dedup is active — `HISTORY_MODE=file`, or `memory` while the process is up and the
  prior entry is still retained (see the table below) — a fixed key reused across days returns the
  first day's job and silently skips the fresh label; `HISTORY_MODE=disabled` reprints on every
  keyed retry, so it never skips. For scheduled automation, give each run a date-unique key (e.g.
  `today-2026-07-11`) or omit the key.

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
original request); see [known limitations](known-limitations.md).

## Deployment & security notes

- **Auth & network exposure.** The service fails closed — it won't start without one of `API_TOKEN`,
  `WEB_AUTH_USER`/`WEB_AUTH_PASSWORD`, or an explicit `ALLOW_UNAUTHENTICATED=true`. Prefer some auth
  whenever the service is reachable beyond a trusted LAN; it guards every write/preview path.
  Credentials are sent **in clear over plain HTTP** (the bearer token, and Basic auth's base64 which
  is trivially reversible), so for anything past a trusted intranet put labelito behind a
  TLS-terminating reverse proxy. **Don't expose port `8765` to the public internet.** Serving under a
  sub-path (`https://host/labelito/`)? Have the proxy send the prefix in a header and set
  `PROXY_PATH_HEADER` to that header's name so generated URLs stay prefix-correct.
- **Choosing an auth mode.** All three are permutations of the same build:

  | Mode | Set | Page shells | API auth | Browser token entry |
  |---|---|---|---|---|
  | **Behind a reverse proxy** (proxy does auth) | `ALLOW_UNAUTHENTICATED=true` | public (app); proxy guards | none (app) | hidden |
  | **Bearer token** (scripts / advanced) | `API_TOKEN=…` | public | bearer | shown — a key button in the nav opens a token dialog, saved to this browser only |
  | **HTTP Basic** (no proxy, protect the UI) | `WEB_AUTH_USER=…` `WEB_AUTH_PASSWORD=…` | login wall | Basic **or** bearer | hidden (browser sends Basic automatically) |

  Basic and bearer can be set together: the browser logs in once via Basic, scripts keep using the
  bearer token (`Authorization: Bearer …`) or `curl -u user:pass`. Note HTTP Basic has **no clean
  logout** — browsers cache the credentials until closed.
- **Reverse-proxy auth recipe.** To let a proxy (Caddy, nginx, Authelia, Home Assistant ingress)
  handle authentication and TLS, run labelito open with `ALLOW_UNAUTHENTICATED=true` and never expose
  its port directly — only the proxy should reach it. Example Caddy Basic-auth front:

  ```caddy
  labels.example.com {
    basic_auth {
      me $2a$14$…              # caddy hash-password
    }
    reverse_proxy 127.0.0.1:8765
  }
  ```
- **Request size.** Bodies over ~8 MiB are rejected (`413`) by `Content-Length` before being read,
  bounding upload memory. A body-bearing request that omits `Content-Length` (a chunked upload) is
  rejected with `411`, so the guard can't be bypassed; a public-facing deployment should still set a
  hard body limit at the reverse proxy.
- **Volumes.** Keep `templates/`, `fonts/`, and `assets/icons/` mounted read-only; keep `data/` on a
  host bind mount (`./data`). With `HISTORY_MODE=file` (the Compose default) the reprint history lives
  there and survives container recreation; the code default `memory` keeps no file and resets on restart.
- **Non-root.** The image runs as uid 1000 (`app`) by default, and Compose sets
  `user: "${UID:-1000}:${GID:-1000}"`. The only path written at runtime is `./data` (the history DB),
  so that host directory must be writable by the chosen uid — uid 1000 matches the image default. To
  run as the invoking host user instead: `UID=$(id -u) GID=$(id -g) docker compose up` (or set
  `UID`/`GID` in `.env`), and `chown ./data` to that uid. A fresh clone ships `data/` pre-created
  (via `data/.gitkeep`) so Docker never auto-creates it root-owned; if ownership is still wrong,
  startup fails fast with an actionable message instead of a permission traceback.
- **Fonts.** The image installs DejaVu via the `fonts-dejavu-core` apt package (at a system path
  *outside* the `fonts/` volume), so rendering works even if you mount an empty `FONTS_DIR`. The
  `fonts/` volume is a **DejaVu override slot**, not a general font store: the renderer loads
  `DejaVuSans.ttf` / `DejaVuSans-Bold.ttf` by name, so drop files with *those* names into `FONTS_DIR`
  to replace the bundled faces. A differently-named `.ttf` is ignored (templates can't select a font
  family). For faithful **local** previews on a bare host, run `scripts/fetch-fonts.sh` once.
- **Metrics.** `/metrics` is opt-in (`METRICS_ENABLED=true`) and carries no auth — restrict it at the
  network layer if the deployment isn't trusted. See the `METRICS_*` rows above.
