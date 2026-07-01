# SPDX-License-Identifier: GPL-3.0-or-later
"""Media-compatibility helper tests — required-media lookup + loaded-vs-required comparison.

No hardware and no socket: ``required_media_for`` reads the brother_ql label registry directly and
``media_matches`` is compared against hand-built :class:`PrinterSNMPStatus` fixtures mirroring the
live QL-810W (currently 62mm continuous loaded — the roll that makes ``address.yaml``'s 62x29
die-cut template mismatch in production).
"""

from __future__ import annotations

import pytest

from app.media import (
    LENGTH_TOLERANCE_MM,
    MEDIA_TYPE_CONTINUOUS,
    MEDIA_TYPE_DIE_CUT,
    WIDTH_TOLERANCE_MM,
    MediaMatch,
    RequiredMedia,
    media_matches,
    required_media_for,
)
from app.transports.snmp import PrinterSNMPStatus

# ── SNMP fixtures mirroring the live printer ────────────────────────────────────────
# What the QL-810W actually reports today: 62mm continuous tape loaded, no fault.
_LOADED_62_CONTINUOUS = PrinterSNMPStatus(
    reachable=True,
    media_name='62mm / 2.4"',
    media_width_mm=62.0,
    media_length_mm=None,
    media_type=MEDIA_TYPE_CONTINUOUS,
)
# A hypothetical 62x29 die-cut roll, for the inverse comparison.
_LOADED_62X29_DIE_CUT = PrinterSNMPStatus(
    reachable=True,
    media_name="62mm x 29mm",
    media_width_mm=62.0,
    media_length_mm=29.0,
    media_type=MEDIA_TYPE_DIE_CUT,
)


# ── required_media_for ──────────────────────────────────────────────────────────────
def test_required_media_for_continuous() -> None:
    """A continuous label (62) → 62mm continuous, no discrete length."""
    media = required_media_for("62")
    assert media == RequiredMedia(width_mm=62.0, media_type=MEDIA_TYPE_CONTINUOUS, length_mm=None)


def test_required_media_for_die_cut() -> None:
    """A die-cut label (62x29) -> 62x29mm die-cut with a length."""
    media = required_media_for("62x29")
    assert media == RequiredMedia(width_mm=62.0, media_type=MEDIA_TYPE_DIE_CUT, length_mm=29.0)


def test_required_media_for_narrow_continuous() -> None:
    """A narrower continuous label (29) → 29mm continuous."""
    media = required_media_for("29")
    assert media.width_mm == 29.0
    assert media.media_type == MEDIA_TYPE_CONTINUOUS
    assert media.length_mm is None


def test_required_media_for_red_label_is_continuous_62() -> None:
    """Two-color media (62red) is still 62mm continuous — color is not a media-geometry concern."""
    media = required_media_for("62red")
    assert media.width_mm == 62.0
    assert media.media_type == MEDIA_TYPE_CONTINUOUS


def test_required_media_for_returns_float_dimensions() -> None:
    """Dimensions are floats so they compare cleanly against the SNMP float geometry."""
    media = required_media_for("62x29")
    assert isinstance(media.width_mm, float)
    assert isinstance(media.length_mm, float)


def test_required_media_for_unknown_label_raises() -> None:
    with pytest.raises(ValueError, match="Unknown brother_ql label"):
        required_media_for("not-a-real-label")


# ── media_matches: the production scenario ──────────────────────────────────────────
def test_address_template_mismatches_loaded_continuous_roll() -> None:
    """The motivating bug: a 62x29 die-cut template against the loaded 62mm continuous roll.

    This is exactly the production failure (HTTP 200 but red-blink-prints-nothing) the guard closes.
    """
    required = required_media_for("62x29")
    assert media_matches(required, _LOADED_62_CONTINUOUS) == MediaMatch.MISMATCH


def test_continuous_template_matches_loaded_continuous_roll() -> None:
    """The other 12 templates use the continuous 62 label and match the loaded roll."""
    required = required_media_for("62")
    assert media_matches(required, _LOADED_62_CONTINUOUS) == MediaMatch.MATCH


# ── media_matches: width comparison ─────────────────────────────────────────────────
def test_width_mismatch() -> None:
    required = required_media_for("29")  # 29mm continuous
    assert media_matches(required, _LOADED_62_CONTINUOUS) == MediaMatch.MISMATCH


def test_width_within_tolerance_matches() -> None:
    """A sub-tolerance width difference (firmware rounding) still matches."""
    loaded = PrinterSNMPStatus(
        reachable=True,
        media_width_mm=62.0 + WIDTH_TOLERANCE_MM,  # exactly on the tolerance boundary
        media_type=MEDIA_TYPE_CONTINUOUS,
    )
    assert media_matches(required_media_for("62"), loaded) == MediaMatch.MATCH


def test_width_just_over_tolerance_mismatches() -> None:
    loaded = PrinterSNMPStatus(
        reachable=True,
        media_width_mm=62.0 + WIDTH_TOLERANCE_MM + 0.01,
        media_type=MEDIA_TYPE_CONTINUOUS,
    )
    assert media_matches(required_media_for("62"), loaded) == MediaMatch.MISMATCH


# ── media_matches: continuous vs die-cut form ───────────────────────────────────────
def test_continuous_template_mismatches_die_cut_roll() -> None:
    required = required_media_for("62")  # continuous
    assert media_matches(required, _LOADED_62X29_DIE_CUT) == MediaMatch.MISMATCH


def test_die_cut_template_matches_same_die_cut_roll() -> None:
    required = required_media_for("62x29")
    assert media_matches(required, _LOADED_62X29_DIE_CUT) == MediaMatch.MATCH


# ── media_matches: die-cut length comparison ────────────────────────────────────────
def test_die_cut_length_mismatch() -> None:
    """Same width + form, different label length ⇒ mismatch (e.g. a 62x100 template on a 62x29 roll)."""
    required = RequiredMedia(width_mm=62.0, media_type=MEDIA_TYPE_DIE_CUT, length_mm=100.0)
    assert media_matches(required, _LOADED_62X29_DIE_CUT) == MediaMatch.MISMATCH


def test_die_cut_length_within_tolerance_matches() -> None:
    required = RequiredMedia(
        width_mm=62.0, media_type=MEDIA_TYPE_DIE_CUT, length_mm=29.0 + LENGTH_TOLERANCE_MM
    )
    assert media_matches(required, _LOADED_62X29_DIE_CUT) == MediaMatch.MATCH


def test_die_cut_matches_when_printer_omits_length() -> None:
    """A die-cut roll reporting width+type but no length matches on those axes (length unverifiable)."""
    loaded = PrinterSNMPStatus(
        reachable=True,
        media_width_mm=62.0,
        media_length_mm=None,
        media_type=MEDIA_TYPE_DIE_CUT,
    )
    assert media_matches(required_media_for("62x29"), loaded) == MediaMatch.MATCH


# ── media_matches: unknown (fail-open) cases ────────────────────────────────────────
def test_unreachable_snmp_is_unknown() -> None:
    assert media_matches(required_media_for("62x29"), PrinterSNMPStatus.unreachable()) == (
        MediaMatch.UNKNOWN
    )


def test_none_loaded_is_unknown() -> None:
    assert media_matches(required_media_for("62"), None) == MediaMatch.UNKNOWN


def test_reachable_but_no_media_geometry_is_unknown() -> None:
    """SNMP answered but reported no loaded-media width/type ⇒ nothing to compare ⇒ unknown."""
    loaded = PrinterSNMPStatus(reachable=True, media_width_mm=None, media_type=None)
    assert media_matches(required_media_for("62"), loaded) == MediaMatch.UNKNOWN


def test_reachable_missing_width_only_is_unknown() -> None:
    loaded = PrinterSNMPStatus(
        reachable=True, media_width_mm=None, media_type=MEDIA_TYPE_CONTINUOUS
    )
    assert media_matches(required_media_for("62"), loaded) == MediaMatch.UNKNOWN


# ── MediaMatch serialises as its plain string value ─────────────────────────────────
def test_media_match_serialises_as_string() -> None:
    assert MediaMatch.MATCH == "match"
    assert MediaMatch.MISMATCH == "mismatch"
    assert MediaMatch.UNKNOWN == "unknown"
    assert str(MediaMatch.MATCH) == "match"
