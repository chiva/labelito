# SPDX-License-Identifier: GPL-3.0-or-later
"""Media-compatibility core: required-media lookup and loaded-vs-required comparison.

The Brother QL-810W rasterises a job and only *then* rejects it at the hardware level when the
loaded roll does not match the template's ``label`` (e.g. 62mm continuous loaded, a 62x29 die-cut
template requested) — the red-blink-prints-nothing failure that motivated SNMP status. This module
is the single comparison both the API print guard (``app.main``) and the UI compatibility badge
(``/templates`` feed) use, so the server-side rule and the client-side rule can never disagree.

Two pieces:
  * :func:`required_media_for` — the media a template's brother_ql ``label`` needs, read straight
    from the ``brother_ql_next`` ``ALL_LABELS`` registry (``tape_size`` + ``form_factor``), never
    hand-typed — the same no-drift principle as :mod:`app.drivers.brother_ql`.
  * :func:`media_matches` — compares that required media against the loaded media reported over SNMP
    (:class:`app.transports.snmp.PrinterSNMPStatus`), yielding match / mismatch / unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from brother_ql.labels import ALL_LABELS, FormFactor

if TYPE_CHECKING:
    from app.transports.snmp import PrinterSNMPStatus

# Built once from the library registry (same source as the driver's capability table) so a template
# label and the media it requires can never drift from what brother_ql actually rasterises.
_LABELS = {lbl.identifier: lbl for lbl in ALL_LABELS}

MEDIA_TYPE_CONTINUOUS = "continuous"
MEDIA_TYPE_DIE_CUT = "die_cut"

# The QL reports loaded-media geometry in whole millimetres; allow ±1mm of slop between the
# template's nominal tape size and the printer's measured roll before calling it a mismatch.
WIDTH_TOLERANCE_MM = 1.0
# Die-cut length is the printer-fed axis; the same ±1mm tolerance applies to the discrete label
# length so e.g. a 62x29 template still matches a roll the firmware rounds to 29mm.
LENGTH_TOLERANCE_MM = 1.0


class MediaMatch(StrEnum):
    """Outcome of comparing a template's required media against the loaded roll.

    A ``str`` enum so it serialises to its plain value (``"match"``) in JSON responses and the
    template feed, while staying a single named constant for server-side comparisons.
    """

    MATCH = "match"
    MISMATCH = "mismatch"
    # SNMP unreachable/disabled, or reachable but reporting no loaded-media geometry: callers must
    # fail open (allow the print, badge the UI ``?``) rather than block on an unverifiable state.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RequiredMedia:
    """The media a template's brother_ql ``label`` requires.

    ``length_mm`` is ``None`` for continuous tape (which has no discrete label length) and the
    nominal die-cut label length in millimetres otherwise — mirroring how
    :class:`app.transports.snmp.PrinterSNMPStatus` reports the *loaded* media.
    """

    width_mm: float
    media_type: str  # MEDIA_TYPE_CONTINUOUS | MEDIA_TYPE_DIE_CUT
    length_mm: float | None = None


def required_media_for(label_id: str) -> RequiredMedia:
    """Return the media a template's ``label`` requires, from the brother_ql label registry.

    ``tape_size`` is ``(width_mm, length_mm)`` with ``length_mm == 0`` for continuous rolls;
    ``form_factor == ENDLESS`` is continuous, everything else (``DIE_CUT`` / ``ROUND_DIE_CUT``) is
    die-cut — the same mapping :func:`app.drivers.brother_ql._geometry` uses for pixel geometry.

    Raises :class:`ValueError` for an unknown label id (consistent with
    ``BrotherQLDriver.for_model``), so a malformed template surfaces a clear error rather than a
    silent skip of the media guard.
    """
    label = _LABELS.get(label_id)
    if label is None:
        raise ValueError(f"Unknown brother_ql label {label_id!r}. Known: {sorted(_LABELS)}")
    width_mm, length_mm = label.tape_size
    if label.form_factor == FormFactor.ENDLESS:
        return RequiredMedia(width_mm=float(width_mm), media_type=MEDIA_TYPE_CONTINUOUS)
    return RequiredMedia(
        width_mm=float(width_mm),
        media_type=MEDIA_TYPE_DIE_CUT,
        length_mm=float(length_mm),
    )


def media_matches(required: RequiredMedia, loaded: PrinterSNMPStatus | None) -> MediaMatch:
    """Compare required media against the loaded roll reported over SNMP.

    Returns :attr:`MediaMatch.UNKNOWN` when the loaded media cannot be determined (SNMP
    unreachable/disabled, or reachable but reporting no width/type) so the caller fails open.
    Otherwise compares roll width (±:data:`WIDTH_TOLERANCE_MM`) and the continuous-vs-die-cut form;
    for two die-cut media it also compares the label length (±:data:`LENGTH_TOLERANCE_MM`) when the
    printer reports one. A difference on any compared axis is a :attr:`MediaMatch.MISMATCH`.

    Scope — geometry only, deliberately NOT media colour. A two-colour label (e.g. ``62red``) maps to
    the same :class:`RequiredMedia` as plain ``62`` continuous, so a red/black template can MATCH a
    plain black/white 62mm roll here. This is intentional: SNMP exposes no reliable loaded-media
    colour signal on the QL (``prtInputMediaName`` is ``"62mm / 2.4\""`` for both, and no verified
    colour OID exists), so guessing colour from a media-name string would risk false mismatches that
    block valid prints — against this guard's fail-open contract. Two-colour capability is enforced
    separately and statically by ``_validate_two_color_supported`` in :mod:`app.main` (model + media
    binding); a red job on plain media degrades to a black-only print (brother_ql drops the red
    layer), a wrong-output case, not the silent prints-nothing failure this guard targets.
    """
    if loaded is None or not loaded.reachable:
        return MediaMatch.UNKNOWN
    if loaded.media_width_mm is None or loaded.media_type is None:
        # Reachable but the agent did not report loaded-media geometry — nothing to compare against.
        return MediaMatch.UNKNOWN

    if abs(required.width_mm - loaded.media_width_mm) > WIDTH_TOLERANCE_MM:
        return MediaMatch.MISMATCH
    if required.media_type != loaded.media_type:
        return MediaMatch.MISMATCH
    # Both die-cut: also require the discrete label length to agree, when the printer reports it.
    # A loaded roll that omits its length still matches on width+type (we cannot disprove the
    # length, so we do not block on it).
    if (
        required.media_type == MEDIA_TYPE_DIE_CUT
        and required.length_mm is not None
        and loaded.media_length_mm is not None
        and abs(required.length_mm - loaded.media_length_mm) > LENGTH_TOLERANCE_MM
    ):
        return MediaMatch.MISMATCH
    return MediaMatch.MATCH
