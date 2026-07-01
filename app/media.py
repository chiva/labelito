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
from typing import Protocol, runtime_checkable

from brother_ql.labels import ALL_LABELS, FormFactor


@runtime_checkable
class LoadedMedia(Protocol):
    """The loaded-roll fields :func:`media_matches` compares — the structural contract both status
    channels satisfy: :class:`app.transports.snmp.PrinterSNMPStatus` (SNMP) and
    :class:`app.transports.base.PrinterStatus` (ESC i S / USB). Comparing against a Protocol rather
    than a concrete class keeps this module decoupled from both transport layers and lets one
    comparison serve every channel.

    Declared as read-only properties (not plain attributes) so both status types — which are FROZEN
    dataclasses with read-only fields — structurally satisfy the protocol."""

    @property
    def reachable(self) -> bool: ...
    @property
    def media_width_mm(self) -> float | None: ...
    @property
    def media_type(self) -> str | None: ...
    @property
    def media_length_mm(self) -> float | None: ...


# Built once from the library registry (same source as the driver's capability table) so a template
# label and the media it requires can never drift from what brother_ql actually rasterises.
_LABELS = {lbl.identifier: lbl for lbl in ALL_LABELS}

MEDIA_TYPE_CONTINUOUS = "continuous"
MEDIA_TYPE_DIE_CUT = "die_cut"

# brother_ql.reader maps the status frame's media-type byte (0x0A/0x0B) to these human strings — the
# printer's DIRECT continuous-vs-die-cut report — so :func:`canonical_media_type` keys off them first.
# Lower-cased for a case-insensitive match. When the byte is an unrecognised code brother_ql leaves
# ``media_type`` an int and we fall back to the identified label's form factor.
_RAW_MEDIA_TYPE_TO_CANONICAL = {
    "continuous length tape": MEDIA_TYPE_CONTINUOUS,
    "die-cut labels": MEDIA_TYPE_DIE_CUT,
}

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


def media_type_for_form_factor(form_factor: FormFactor) -> str:
    """Map a brother_ql ``FormFactor`` to the canonical media type.

    ``ENDLESS`` is continuous tape; everything else (``DIE_CUT`` / ``ROUND_DIE_CUT``) is die-cut. The
    single place this mapping lives, so :func:`required_media_for` (required side) and
    :func:`canonical_media_type` (loaded side, ESC i S) can never disagree on the form."""
    return MEDIA_TYPE_CONTINUOUS if form_factor == FormFactor.ENDLESS else MEDIA_TYPE_DIE_CUT


def canonical_media_type(decoded: dict[str, object]) -> str | None:
    """Canonical media type (``continuous``/``die_cut``) for a ``brother_ql.reader.interpret_response``
    dict, or ``None`` when it cannot be determined.

    Keys off the printer's direct media-type report (the ``media_type`` string brother_ql decodes from
    the status frame's 0x0A/0x0B byte) first, then falls back to the width/length-identified label's
    ``form_factor`` — the same brother_ql source :func:`required_media_for` compares against. This is
    the normalization the SNMP path already applies (``_decode_media``), so every status channel lands
    on the same two values the UI badge and print guard expect."""
    raw = decoded.get("media_type")
    if isinstance(raw, str):
        canonical = _RAW_MEDIA_TYPE_TO_CANONICAL.get(raw.strip().lower())
        if canonical is not None:
            return canonical
    form_factor = getattr(decoded.get("identified_media"), "form_factor", None)
    if isinstance(form_factor, FormFactor):
        return media_type_for_form_factor(form_factor)
    return None


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
        return RequiredMedia(
            width_mm=float(width_mm), media_type=media_type_for_form_factor(label.form_factor)
        )
    return RequiredMedia(
        width_mm=float(width_mm),
        media_type=media_type_for_form_factor(label.form_factor),
        length_mm=float(length_mm),
    )


def media_matches(required: RequiredMedia, loaded: LoadedMedia | None) -> MediaMatch:
    """Compare required media against the loaded roll (SNMP or ESC i S).

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
