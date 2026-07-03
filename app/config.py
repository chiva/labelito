# SPDX-License-Identifier: GPL-3.0-or-later
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        # A user's .env may carry keys from older releases (e.g. the removed LABEL_SIZE);
        # pydantic's default would refuse to start on them (extra_forbidden).
        extra="ignore",
    )

    # Printer selection. The transport is inferred from the printer_uri scheme
    # (tcp:// → network, usb:// → usb, file:// → file), so there is no separate transport setting.
    driver: str = "brother_ql"
    model: str = "QL-810W"
    printer_uri: str = "tcp://192.168.1.100:9100"
    # Floyd-Steinberg dithering default for the print raster. Per-request `dither` overrides it;
    # None inherits this. Settable via env var DEFAULT_DITHER. Also resolved (independently, from
    # the request's own `options.dither`) for /preview so the preview mirrors the print's B/W
    # conversion — see app.main._preview_bw_convert.
    default_dither: bool = False
    # B/W threshold default (percentage 0-100, exclusive of 0). Per-request `threshold` overrides
    # it; None inherits this. Settable via env var DEFAULT_THRESHOLD. Passed straight to
    # brother_ql convert() which converts 0-100 internally to the 0-255 range it works with; /preview
    # applies the identical 0-100→0-255 mapping so its cutoff matches the print exactly.
    # Bounds mirror RenderOptions.threshold so an invalid DEFAULT_THRESHOLD fails at startup.
    default_threshold: float = Field(default=70.0, gt=0, le=100, allow_inf_nan=False)
    # 600 dpi high-resolution print mode default. Per-request `high_res` overrides it; None
    # inherits this. Settable via env var DEFAULT_HIGH_RES. Print-only — /preview never uses
    # high_res (preview is a pre-driver render at the native engine resolution).
    default_high_res: bool = False
    # Two-color (red/black) printing default. Per-request `red` overrides it; None inherits
    # this. Settable via env var DEFAULT_RED. Print-only — /preview is always a monochrome
    # pre-driver render. A red print on a model/media that lacks two-color support is rejected with
    # a clean 4xx, so leaving this false is safe on every model.
    default_red: bool = False

    # Auth
    api_token: str | None = None
    # Explicit opt-in to run protected endpoints without a token (intranet/trusted
    # networks only). Without either api_token or this flag, the service refuses to start.
    allow_unauthenticated: bool = False

    # Directories
    templates_dir: Path = Path("templates")
    fonts_dir: Path = Path("fonts")
    icons_dir: Path = Path("assets/icons")
    # Bundled icon collections (FontAwesome/Material/Octicons) baked into the image. Kept separate
    # from icons_dir so a user bind-mounting their own assets/icons cannot shadow the collections;
    # this path is read-only image content, never a runtime volume.
    icon_collections_dir: Path = Path("assets/icon-collections")
    data_dir: Path = Path("data")
    translations_dir: Path = Path("translations")
    # Template studio server-save gate. Default false because docker-compose mounts
    # templates/ read-only: with the mount read-only a write would fail anyway, and a default-on
    # write endpoint would be an unexpected authoring-surface change. Set TEMPLATES_WRITABLE=true
    # (and provide a writable templates_dir) to let POST /templates persist a draft YAML and reload.
    templates_writable: bool = False
    # Template studio: allow the editor to load an existing template's raw YAML for editing
    # (GET /templates/{name}/source). Default true — read-only and safe (the name is resolved by an
    # in-memory registry lookup, never as a filesystem path, so traversal/unrelated-file reads are
    # impossible). Set TEMPLATES_LOADABLE=false to hide the load picker and 404 the source route.
    templates_loadable: bool = True

    # History storage. History backs idempotency de-dup and /reprint, so the mode changes
    # behaviour, not just durability (see docs/known-limitations.md):
    #   memory   — in-process SQLite; dedup/reprint reset on restart (default)
    #   file     — durable SQLite at {data_dir}/history.db; survives restarts
    #   disabled — no history: no dedup (keyed retries reprint), /reprint always 404s
    history_mode: Literal["file", "memory", "disabled"] = "memory"
    history_keep_entries: int = 1000  # rows retained after a prune
    history_prune_at_entries: int = 1500  # prune triggers once the table exceeds this
    # Browse-UI visibility, independent of whether history is stored. When false, the /history page
    # and its list/delete endpoints 404, but /reprint-by-id and idempotency de-dup still work (those
    # are governed by history_mode). Set false to keep reprint while never exposing the printed-job
    # list in the browser.
    history_ui: bool = True
    # YAML template studio. Default false so the editor surface is opt-in (it exposes draft
    # preview and parse endpoints, and optionally server-save when TEMPLATES_WRITABLE is also true).
    # Set EDITOR_ENABLED=true to expose GET /editor, POST /preview/draft, POST /templates/parse, and
    # POST /templates (the server-save route still also requires TEMPLATES_WRITABLE=true).
    editor_enabled: bool = False

    # Internationalization — default label language; per-request `language` overrides it
    default_language: str = "en"

    # Transport timeouts
    # Network timeout is enforced inside NetworkTransport (TIMEOUT = 10 s class constant).
    # USB: the blocking helpers.send() call (pyusb readback loop) has no built-in timeout; we wrap
    # it in a worker thread and join for at most this many seconds. USB prints legitimately take
    # longer than a network status read (the pyusb readback loop polls until completion), so this
    # is deliberately larger than the network TIMEOUT. 30 s covers even a long multi-copy job while
    # still freeing the print lock if the device hangs.
    usb_timeout: int = 30

    # SNMP printer status (network transport only). The Brother NIC accepts the :9100 TCP
    # connection but never returns the status back-channel, so a print can report success while the
    # hardware rejects it (see docs/known-limitations.md). SNMP (UDP 161, community public) is the
    # status channel that actually answers on this hardware: loaded media, a reliable error bitmask,
    # console text and identity. These apply ONLY when the transport inferred from printer_uri is
    # network; the SNMP host is derived from printer_uri's hostname (there is no separate host
    # setting). USB and file transports ignore them entirely.
    snmp_enabled: bool = True
    snmp_community: str = "public"
    snmp_port: int = Field(default=161, gt=0, le=65535)
    # Per-request SNMP timeout in seconds, passed to snmp_get's connected-UDP recv. Kept short
    # because the status read sits in the print/preflight path; SNMP unreachable fails open (warn +
    # proceed), so a tight bound trades a slow printer's status for not stalling the request.
    snmp_timeout: float = Field(default=2.0, gt=0, le=60, allow_inf_nan=False)

    # Reverse-proxy / Home Assistant ingress support. When set (e.g. PROXY_PATH_HEADER=X-Ingress-Path),
    # each request's value of that header becomes the ASGI root_path: generated URLs — page links,
    # static assets, /docs, the OpenAPI servers entry — are prefixed with it, while route matching is
    # unaffected (the proxy strips the prefix before forwarding). Trusting the header is opt-in by
    # design: only enable it behind a proxy that sets or overwrites the header on every request.
    # Unset (default), the header is ignored entirely.
    proxy_path_header: str | None = None

    # Prometheus exposition is OFF by default (opt-in): a home print service is not normally scraped,
    # and an open /metrics leaks printer identity/usage. Set METRICS_ENABLED=true to expose it; while
    # disabled the endpoint 404s as if absent. The telemetry gauges are still updated in-memory on a
    # print/status query regardless — they are simply not exposed until this is enabled.
    metrics_enabled: bool = False
    # Path the Prometheus exposition is served at, on the SAME port/app as the web UI and the rest of
    # the API (there is no separate metrics server/port). Override (env var METRICS_PATH) to relocate
    # it — e.g. behind a hard-to-guess path, or to avoid a collision with an upstream proxy route. The
    # path is read once at startup, so a change takes effect on restart.
    metrics_path: str = "/metrics"

    @field_validator("metrics_path")
    @classmethod
    def _normalize_metrics_path(cls, value: str) -> str:
        """Normalize to a single leading slash, no trailing slash, and restrict to a LITERAL path.

        A path without a leading slash (``metrics``) is not a valid route mount and would 404; a
        trailing slash (``/metrics/``) would not match a scrape of ``/metrics``. Both are normalized.

        Critically, the value is interpolated straight into ``@app.get(...)``, where FastAPI treats
        braces as path parameters — so ``/{p:path}`` would register a catch-all that shadows every
        page, and ``/metrics/{x}`` would serve metrics for any suffix. Restrict to literal path
        segments of an unreserved URL charset (letters, digits, ``-._~``) so no path-parameter,
        wildcard, query, or fragment syntax can ever reach the router. Empty/``/`` is rejected (it
        would shadow the web UI).
        """
        path = "/" + value.strip().strip("/")
        if path == "/":
            raise ValueError("METRICS_PATH must not be empty or '/' (it would shadow the web UI)")
        segments = path.lstrip("/").split("/")
        if not all(re.fullmatch(r"[A-Za-z0-9._~-]+", seg) for seg in segments):
            raise ValueError(
                f"METRICS_PATH {value!r} must be a literal URL path of /-separated segments using "
                "only letters, digits, and '-._~' (no path parameters '{...}', wildcards, query, "
                "or fragment) — it is mounted as a FastAPI route verbatim"
            )
        return path

    # Render limits for continuous labels
    min_length_px: int = 200
    max_length_px: int = 6000


settings = Settings()
