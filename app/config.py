# SPDX-License-Identifier: GPL-3.0-or-later
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical python logging level names accepted by LOG_LEVEL (the aliases WARN/FATAL and the
# NOTSET pseudo-level are deliberately excluded: documenting one spelling per level keeps the
# setting unambiguous, and NOTSET would silently defer to the root logger's default).
LOG_LEVEL_NAMES: tuple[str, ...] = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


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
    # networks only). Without any of api_token / web_auth_* / this flag, the service refuses to start.
    allow_unauthenticated: bool = False
    # Optional HTTP Basic auth for the web UI: when BOTH web_auth_user and web_auth_password are set,
    # the whole UI (page shells + API) sits behind a native browser login. The browser then re-attaches
    # its Basic credentials to every same-origin fetch automatically, so no in-page API-token entry is
    # needed. Protected endpoints accept EITHER these Basic credentials OR the api_token bearer, so
    # api_token stays available (optional) for scripts/automation. Settable via WEB_AUTH_USER /
    # WEB_AUTH_PASSWORD; configuring Basic auth satisfies the fail-closed startup guard on its own.
    web_auth_user: str | None = None
    web_auth_password: str | None = None
    # Realm advertised in the WWW-Authenticate challenge (shown in the browser login dialog).
    web_auth_realm: str = "labelito"

    @model_validator(mode="after")
    def _validate_web_auth(self) -> "Settings":
        """Basic auth is both-or-neither and non-blank, mirroring the API_TOKEN startup guard.

        A half-configured pair (only user, or only password) is almost certainly a deployment
        mistake that would silently leave the UI open; a blank value likewise. Fail fast at load so
        the operator fixes it, rather than shipping an unintended unauthenticated service.
        """
        user = (self.web_auth_user or "").strip()
        password = (self.web_auth_password or "").strip()
        if bool(self.web_auth_user) != bool(self.web_auth_password):
            raise ValueError(
                "WEB_AUTH_USER and WEB_AUTH_PASSWORD must be set together (both or neither) to "
                "enable HTTP Basic auth for the web UI."
            )
        if self.web_auth_user is not None and (not user or not password):
            raise ValueError(
                "WEB_AUTH_USER / WEB_AUTH_PASSWORD are set but empty/blank. Provide real "
                "credentials, or unset both to disable HTTP Basic auth."
            )
        if not user.isascii() or not password.isascii():
            raise ValueError(
                "WEB_AUTH_USER / WEB_AUTH_PASSWORD must be ASCII — HTTP Basic auth cannot reliably "
                "transport non-ASCII credentials across browsers."
            )
        return self

    @property
    def basic_auth_enabled(self) -> bool:
        """True when HTTP Basic auth is fully configured (both user and password present)."""
        return bool(self.web_auth_user and self.web_auth_password)

    # Directories
    templates_dir: Path = Path("templates")
    # Bundled example templates, baked into the image at a path OUTSIDE the templates_dir VOLUME
    # (mirrors icon_collections_dir). They are LOADED IN ADDITION to templates_dir, so a user who
    # bind-mounts an empty/own templates_dir still gets the shipped examples, and an image upgrade
    # always ships the latest examples. A user file wins over a bundled example of the same internal
    # `name` (see TemplateRegistry.load_all). When not set explicitly it MIRRORS templates_dir (see
    # _mirror_example_dirs_to_primary), so overriding TEMPLATES_DIR alone keeps both pointed at the
    # same directory and bare-metal/dev loads the single dir once (the loader skips the second pass
    # when the two resolve equal); Docker sets EXAMPLE_TEMPLATES_DIR=/app/examples/templates to split
    # them.
    example_templates_dir: Path = Path("templates")
    fonts_dir: Path = Path("fonts")
    icons_dir: Path = Path("assets/icons")
    # Bundled icon collections (FontAwesome/Material/Octicons) baked into the image. Kept separate
    # from icons_dir so a user bind-mounting their own assets/icons cannot shadow the collections;
    # this path is read-only image content, never a runtime volume.
    icon_collections_dir: Path = Path("assets/icon-collections")
    data_dir: Path = Path("data")
    translations_dir: Path = Path("translations")
    # Bundled translation catalogs, baked outside the translations_dir VOLUME — same rationale as
    # example_templates_dir. Merged UNDER translations_dir (a user catalog for a language overrides
    # the bundled one; user-only languages add to it). Guarantees the DEFAULT_LANGUAGE catalog always
    # exists even against an empty translations mount, so the service no longer hard-fails on boot
    # when the volume is empty. Mirrors translations_dir when not set explicitly (see
    # _mirror_example_dirs_to_primary). Docker sets EXAMPLE_TRANSLATIONS_DIR=/app/examples/translations.
    example_translations_dir: Path = Path("translations")
    # Load the bundled example templates AND translation catalogs (default true). Set
    # LOAD_EXAMPLES=false to load ONLY the user's own templates_dir/translations_dir — the shipped
    # examples are skipped entirely (the example dirs are passed as None to the registry/translator).
    # With examples off and an empty user translations_dir, there is no default-language catalog: this
    # is allowed (boot warns instead of failing) and `[[token]]` chrome words render as their raw key.
    load_examples: bool = True
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

    # Root log level for the process, applied by app.main's logging.basicConfig at import. Standard
    # python level names (LOG_LEVEL_NAMES), case-insensitive; an unknown name fails at settings load
    # so a typo'd LOG_LEVEL aborts boot instead of silently logging at the hardcoded default.
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        """Normalize to the canonical upper-case level name and reject unknown levels.

        The value is handed verbatim to ``logging.basicConfig(level=...)``, which raises a
        ``ValueError`` deep inside logging on an unknown name — validating here surfaces the
        mistake as a legible settings error at startup, consistent with the other settings.
        """
        level = value.strip().upper()
        if level not in LOG_LEVEL_NAMES:
            raise ValueError(
                f"LOG_LEVEL {value!r} is not a valid logging level; choose one of "
                f"{', '.join(LOG_LEVEL_NAMES)} (case-insensitive)"
            )
        return level

    # Render limits for continuous labels
    min_length_px: int = 200
    max_length_px: int = 6000

    @model_validator(mode="after")
    def _mirror_example_dirs_to_primary(self) -> "Settings":
        """Point each example dir at its primary dir when it was not configured explicitly.

        ``example_templates_dir`` / ``example_translations_dir`` are separate settings so Docker can
        split the bundled, image-baked content from the user's writable volume. But their literal
        defaults are independent of ``templates_dir`` / ``translations_dir``: overriding only
        ``TEMPLATES_DIR`` (or ``TRANSLATIONS_DIR``) would otherwise leave the example loader pointed at
        the default CWD-relative ``templates``/``translations`` — a *different* directory — which both
        breaks the intended single-dir behavior (the two no longer resolve equal, so the loader scans
        twice) and can pull unrelated YAML into the registry/translator. Mirroring the primary here
        keeps them aligned unless the example dir was set on purpose (``EXAMPLE_*_DIR`` present in the
        environment, which pydantic-settings records in ``model_fields_set``).
        """
        if "example_templates_dir" not in self.model_fields_set:
            self.example_templates_dir = self.templates_dir
        if "example_translations_dir" not in self.model_fields_set:
            self.example_translations_dir = self.translations_dir
        return self


settings = Settings()
