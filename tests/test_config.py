# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for app.config.Settings — SNMP printer-status settings: defaults, env-var override,
and bounds validation enforced at Settings construction (snmp_port 1-65535, snmp_timeout > 0,
<= 60, finite)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


# ── SNMP settings: defaults ──────────────────────────────────────────────────────
def test_snmp_defaults() -> None:
    """The out-of-the-box SNMP defaults match the snmp_get / query_snmp_status signature:
    enabled, community 'public', UDP 161, 2.0 s timeout."""
    s = Settings()
    assert s.snmp_enabled is True
    assert s.snmp_community == "public"
    assert s.snmp_port == 161
    assert s.snmp_timeout == 2.0


# ── SNMP settings: env-var override ────────────────────────────────────────────────
def test_snmp_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SNMP_* env vars override every default (case-insensitive per env_prefix config)."""
    monkeypatch.setenv("SNMP_ENABLED", "false")
    monkeypatch.setenv("SNMP_COMMUNITY", "private")
    monkeypatch.setenv("SNMP_PORT", "1161")
    monkeypatch.setenv("SNMP_TIMEOUT", "5.5")
    s = Settings()
    assert s.snmp_enabled is False
    assert s.snmp_community == "private"
    assert s.snmp_port == 1161
    assert s.snmp_timeout == 5.5


# ── SNMP settings: snmp_port bounds (1-65535) ──────────────────────────────────────
def test_snmp_port_zero_rejected() -> None:
    """SNMP_PORT=0 is out of range (gt=0) and must fail at Settings construction."""
    with pytest.raises(ValidationError):
        Settings(snmp_port=0)


def test_snmp_port_above_65535_rejected() -> None:
    """SNMP_PORT=65536 exceeds the 16-bit port space (le=65535) and must fail."""
    with pytest.raises(ValidationError):
        Settings(snmp_port=65536)


def test_snmp_port_boundaries_accepted() -> None:
    """The first and last valid ports (1 and 65535) load successfully."""
    assert Settings(snmp_port=1).snmp_port == 1
    assert Settings(snmp_port=65535).snmp_port == 65535


# ── SNMP settings: snmp_timeout bounds (> 0, <= 60, finite) ─────────────────────────
def test_snmp_timeout_zero_rejected() -> None:
    """SNMP_TIMEOUT=0 is out of range (gt=0) and must fail at Settings construction."""
    with pytest.raises(ValidationError):
        Settings(snmp_timeout=0)


def test_snmp_timeout_above_60_rejected() -> None:
    """SNMP_TIMEOUT=61 exceeds the upper bound (le=60) and must fail."""
    with pytest.raises(ValidationError):
        Settings(snmp_timeout=61)


def test_snmp_timeout_nan_rejected() -> None:
    """SNMP_TIMEOUT=nan is non-finite and must fail at Settings construction."""
    with pytest.raises(ValidationError):
        Settings(snmp_timeout=math.nan)


def test_snmp_timeout_inf_rejected() -> None:
    """SNMP_TIMEOUT=inf is non-finite and must fail at Settings construction."""
    with pytest.raises(ValidationError):
        Settings(snmp_timeout=math.inf)


def test_snmp_timeout_valid_loads() -> None:
    """A valid SNMP_TIMEOUT (e.g. 10.0) loads successfully and is stored as-is."""
    assert Settings(snmp_timeout=10.0).snmp_timeout == 10.0


# ── Metrics path (METRICS_PATH) ────────────────────────────────────────────────────
def test_metrics_path_default() -> None:
    """The Prometheus exposition defaults to /metrics (same app/port as the rest of the API)."""
    assert Settings().metrics_path == "/metrics"


def test_metrics_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """METRICS_PATH relocates the exposition path."""
    monkeypatch.setenv("METRICS_PATH", "/internal/telemetry")
    assert Settings().metrics_path == "/internal/telemetry"


def test_metrics_path_adds_leading_slash() -> None:
    """A path without a leading slash is normalized to a valid route mount."""
    assert Settings(metrics_path="telemetry").metrics_path == "/telemetry"


def test_metrics_path_strips_trailing_slash() -> None:
    """A trailing slash is stripped so it matches a scrape of the bare path."""
    assert Settings(metrics_path="/metrics/").metrics_path == "/metrics"


def test_metrics_path_empty_rejected() -> None:
    """An empty or root METRICS_PATH would shadow the web UI and is rejected."""
    with pytest.raises(ValidationError):
        Settings(metrics_path="/")
    with pytest.raises(ValidationError):
        Settings(metrics_path="")


def test_metrics_enabled_default_off() -> None:
    """Metrics are opt-in: the exposition is disabled by default."""
    assert Settings().metrics_enabled is False


def test_metrics_enabled_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """METRICS_ENABLED=true opts into the Prometheus exposition."""
    monkeypatch.setenv("METRICS_ENABLED", "true")
    assert Settings().metrics_enabled is True


def test_metrics_path_nested_literal_allowed() -> None:
    """A nested literal path is fine (it is mounted verbatim)."""
    assert Settings(metrics_path="/internal/metrics").metrics_path == "/internal/metrics"
    assert Settings(metrics_path="/m-1_2.3").metrics_path == "/m-1_2.3"


def test_metrics_path_rejects_path_parameters_and_wildcards() -> None:
    """Brace path-parameters / converters must be rejected — they would register a catch-all route
    in FastAPI (``/{p:path}`` shadows every page; ``/metrics/{x}`` serves metrics for any suffix)."""
    for bad in ("/{path:path}", "/metrics/{secret}", "/{x}"):
        with pytest.raises(ValidationError):
            Settings(metrics_path=bad)


def test_metrics_path_rejects_non_literal_characters() -> None:
    """Query/fragment/space and empty internal segments are rejected (literal segments only)."""
    for bad in ("/a?b=c", "/a#frag", "/a b", "/a//b"):
        with pytest.raises(ValidationError):
            Settings(metrics_path=bad)


# ── Example dirs mirror their primary dir unless set explicitly ────────────────────
def test_example_dirs_default_to_primary_dirs() -> None:
    """With nothing overridden, the example dirs equal their primary dirs (single-dir behavior)."""
    s = Settings()
    assert s.example_templates_dir == s.templates_dir
    assert s.example_translations_dir == s.translations_dir


def test_overriding_only_templates_dir_mirrors_example_templates_dir() -> None:
    """Setting TEMPLATES_DIR alone must move example_templates_dir with it, not leave it at the
    default CWD-relative 'templates' (which would scan an unrelated directory)."""
    s = Settings(templates_dir=Path("/data/labels"))
    assert s.templates_dir == Path("/data/labels")
    assert s.example_templates_dir == Path("/data/labels")


def test_overriding_only_translations_dir_mirrors_example_translations_dir() -> None:
    """Setting TRANSLATIONS_DIR alone must move example_translations_dir with it."""
    s = Settings(translations_dir=Path("/data/i18n"))
    assert s.translations_dir == Path("/data/i18n")
    assert s.example_translations_dir == Path("/data/i18n")


def test_explicit_example_dirs_are_not_overridden() -> None:
    """When the example dir is set on purpose (Docker's split layout), it stays distinct from the
    primary dir — the mirror only fills unset values."""
    s = Settings(
        templates_dir=Path("/data/labels"),
        example_templates_dir=Path("/app/examples/templates"),
        translations_dir=Path("/data/i18n"),
        example_translations_dir=Path("/app/examples/translations"),
    )
    assert s.example_templates_dir == Path("/app/examples/templates")
    assert s.example_translations_dir == Path("/app/examples/translations")


def test_example_dirs_mirror_primary_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror also applies when the primary dir is overridden via its env var and the example
    env var is absent (the real Docker-vs-bare-metal distinction)."""
    monkeypatch.setenv("TEMPLATES_DIR", "/mnt/templates")
    monkeypatch.delenv("EXAMPLE_TEMPLATES_DIR", raising=False)
    s = Settings()
    assert s.example_templates_dir == Path("/mnt/templates")


# ── Env-file compatibility across releases ─────────────────────────────────────────
def test_stale_env_file_keys_are_ignored(tmp_path: Path) -> None:
    """A leftover key from an older release (e.g. the removed LABEL_SIZE) in a user's .env
    must not prevent startup — unknown env-file keys are ignored, not extra_forbidden."""
    env_file = tmp_path / ".env"
    env_file.write_text("LABEL_SIZE=62\nMODEL=QL-820NWB\n", encoding="utf-8")
    s = Settings(_env_file=env_file)
    assert s.model == "QL-820NWB"
    assert not hasattr(s, "label_size")
