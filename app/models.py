# SPDX-License-Identifier: GPL-3.0-or-later
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Upper bound on SequenceSpec.padding: a 32-digit zero-padded counter is already far beyond any
# real use case. Without this cap, a tiny authenticated request can set an enormous padding and
# force large string allocations in render_sequence while the print lock is held — a DoS vector.
MAX_SEQUENCE_PADDING: int = 32


class RenderOptions(BaseModel):
    """Rasterization knobs that shape the print raster (not its delivery — ``cut`` stays on the
    request). Grouped so a future option (red/threshold/high_res) is a one-field change here and is
    automatically folded into the idempotency fingerprint and the reprint replay, which hash/store
    this whole object rather than hand-listing each field.

    On ``PrintRequest`` each field is nullable-inherit (``None`` → inherit the server default); on
    ``PrintJobRecord`` each field holds the resolved concrete value (env default already applied),
    so /reprint reproduces the exact label even if the server default later changes.
    """

    # Reject unknown option keys (422) so a misspelled option surfaces as an error rather than
    # silently printing the wrong thing — a free-form dict would lose that.
    model_config = ConfigDict(extra="forbid")

    # Floyd–Steinberg dithering for the print raster. Nullable on purpose: None inherits the
    # DEFAULT_DITHER env default; an explicit true/false overrides it either way (a plain bool with
    # `or` could never turn a True default back off). On PrintRequest this is the nullable request
    # value; on PrintJobRecord it is the resolved effective value persisted for reprint.
    dither: bool | None = Field(
        default=None,
        examples=[None],
        description=(
            "Approximate greys with Floyd–Steinberg dithering on the print raster. null inherits "
            "the DEFAULT_DITHER server default; true/false overrides it. Affects /print and "
            "/reprint only — /preview accepts this field for a shared request shape but ignores "
            "it (the preview is a pre-driver render and is never dithered). Inert when red=true: "
            "brother_ql's two-color path separates layers by HSV filters and ignores dither; the "
            "server canonicalizes dither to false in the fingerprint and history under red."
        ),
    )
    # B/W conversion threshold as a percentage (0-100 exclusive of 0). Pixels whose luminance
    # falls below the threshold become black; above it become white. None inherits DEFAULT_THRESHOLD
    # (70.0 by default). On PrintRequest this is the nullable request value; on PrintJobRecord it
    # is the resolved effective value persisted for reprint. The underlying brother_ql convert()
    # accepts 0-100 and converts internally to 0-255.
    threshold: float | None = Field(
        default=None,
        gt=0,
        le=100,
        examples=[None],
        description=(
            "B/W cutoff threshold as a percentage (0-100, exclusive of 0). Pixels below the "
            "threshold become black; above it become white. null inherits the DEFAULT_THRESHOLD "
            "server default (70.0). Affects /print and /reprint only. Inert when dither=true: "
            "brother_ql ignores threshold under Floyd-Steinberg dither; the server canonicalizes "
            "threshold to the default in the fingerprint and history when dither is on. Under "
            "red=true, threshold IS applied (convert() runs a point() threshold on both the red "
            "and black layers after HSV separation) and is honored in the fingerprint and history."
        ),
    )
    # 600 dpi high-resolution print mode. When True, the render engine doubles the print-head
    # axis (x/width) of the image so convert(dpi_600=True) receives the correct 2x input and the
    # driver sets dpi_600=True. Physical label size is unchanged -- resolution improves. None
    # inherits DEFAULT_HIGH_RES. On PrintRequest this is the nullable request value; on
    # PrintJobRecord it is the resolved effective value persisted for reprint.
    high_res: bool | None = Field(
        default=None,
        examples=[None],
        description=(
            "Enable 600 dpi (300x600) high-resolution print mode for improved small text and "
            "dense QR codes. When true the render engine doubles the print-head axis (width) "
            "and the driver passes dpi_600=True to convert(). Physical label size is unchanged. "
            "null inherits the DEFAULT_HIGH_RES server default (false). Affects /print and "
            "/reprint only -- /preview ignores this field."
        ),
    )
    # Two-color (red/black) printing. When True the render engine produces an RGB canvas in
    # which elements marked `color: red` draw in pure red (255,0,0) and everything else in black,
    # and the driver passes red=True to convert() so brother_ql separates the red and black layers
    # for QL-800/810W/820NWB + DK-22251 black/red media. Activation is governed solely by this flag:
    # a `color: red` element renders BLACK when red is False (the label still prints, monochrome),
    # so red=False is byte-identical to a no-red render regardless of element colors. None inherits
    # DEFAULT_RED. On PrintRequest this is the nullable request value; on PrintJobRecord it is the
    # resolved effective value persisted for reprint. A red=True print on a model/media that does not
    # support two-color is rejected with a clean 4xx (the driver maps BrotherQLUnsupportedCmd).
    red: bool | None = Field(
        default=None,
        examples=[None],
        description=(
            "Enable two-color (red/black) printing for supported printer/media "
            "(QL-800/810W/820NWB + DK-22251 black/red). When true, elements with `color: red` in "
            "the template render in red and the rest in black, and the driver passes red=True to "
            "convert(). A `color: red` element prints black when this is false. null inherits the "
            "DEFAULT_RED server default (false). Affects /print and /reprint only -- /preview "
            "ignores this field (the preview is a pre-driver render and is never two-color)."
        ),
    )


class SequenceSpec(BaseModel):
    """Auto-numbering spec for a batch of labels using the ``{{seq}}`` computed token.

    When present on ``PrintRequest``, the engine renders ``count`` labels independently, each with
    ``{{seq}}`` resolved to a zero-padded integer starting at ``start`` and advancing by ``step``.
    The ``sequence`` and ``copies`` fields are mutually exclusive: supplying both is a 422.

    Design choices:
    - ``sequence`` is mutually exclusive with ``copies``: sequence drives the item count, copies
      multiplies identical duplicates — mixing them would be confusing and error-prone. A 422 is
      the cleanest contract.
    - One history row per batch: the sequence spec is recorded in the job record (``sequence``
      field on ``PrintJobRecord``), so the batch is inspectable without N rows. /reprint replays
      the whole batch from the frozen spec.
    - The ``{{seq}}`` token is a COMPUTED_TOKEN (like ``{{date}}``): it is resolved per-item by
      the engine and never surfaced as a required user field.
    - Idempotency fingerprint includes the sequence spec via ``model_dump()``, so two requests
      differing only in their sequence are fingerprinted differently.
    """

    model_config = ConfigDict(extra="forbid")

    start: int = Field(
        default=1,
        ge=-(10**9),
        le=10**9,
        description="First sequence number (inclusive).",
        examples=[1],
    )
    count: int = Field(
        ge=1,
        le=500,
        description="Number of labels to print (1-500).",
        examples=[10],
    )
    step: int = Field(
        default=1,
        ge=1,
        le=10**6,
        description="Increment between consecutive sequence numbers (must be >= 1).",
        examples=[1],
    )
    padding: int = Field(
        default=0,
        ge=0,
        le=MAX_SEQUENCE_PADDING,
        description=(
            f"Minimum digit width for zero-padding (0 = no padding, max {MAX_SEQUENCE_PADDING}). "
            "E.g. padding=3 renders 1 as '001'."
        ),
        examples=[3],
    )


class PrintRequest(BaseModel):
    # Reject unknown top-level keys (422) instead of silently ignoring them, so a misspelled
    # option surfaces as an error rather than printing the wrong thing. Per-template values live
    # inside `fields`, not as top-level keys.
    model_config = ConfigDict(extra="forbid")

    # The template is always named explicitly; the caller chooses which label to print. (There is no
    # field-based auto-discovery — it gave no NLP flexibility, only ambiguity as the catalog grew.)
    template: str = Field(min_length=1, examples=["simple"])
    fields: dict[str, Any] = Field(default={}, examples=[{"title": "Hello", "subtitle": "World"}])
    copies: int = Field(default=1, ge=1, le=10, examples=[1])
    dry_run: bool = Field(default=False, examples=[False])
    cut: bool = Field(default=True, examples=[True])
    # overrides settings.default_language; capped — a BCP-47 tag is short, and the value is
    # persisted to history, so an oversized string must not bloat the file.
    language: str | None = Field(default=None, max_length=35, examples=["en"])
    # Rasterization knobs grouped into one sub-model (see RenderOptions). Per-field nullable-inherit
    # semantics: a null option inherits its server default. Grouping means a future option folds into
    # the idempotency fingerprint and reprint replay automatically (both work the whole object).
    options: RenderOptions = Field(default_factory=RenderOptions)
    # Opt-in retry de-duplication: if a previous non-failed job carried the same key, /print
    # returns that job's result instead of printing again. Omit it to allow intentional
    # duplicate prints of the same label. Capped because it is persisted verbatim to history.
    idempotency_key: str | None = Field(default=None, max_length=200, examples=[None])
    # Auto-numbering batch. When set, ``{{seq}}`` resolves per item; the engine renders
    # ``count`` distinct images instead of multiplying one. Mutually exclusive with copies > 1:
    # sequence drives item count; copies multiplies identical labels — mixing them is a 422.
    sequence: SequenceSpec | None = Field(
        default=None,
        description=(
            "Auto-numbering spec for a batch of labels. When set, the {{seq}} token is resolved "
            "per item and the engine renders sequence.count distinct labels. Mutually exclusive "
            "with copies > 1."
        ),
    )

    @model_validator(mode="after")
    def _sequence_copies_exclusive(self) -> "PrintRequest":
        if self.sequence is not None and self.copies > 1:
            raise ValueError(
                "'sequence' and 'copies' > 1 are mutually exclusive: sequence drives the item "
                "count; set copies=1 (the default) when using a sequence spec"
            )
        return self


class TemplateFieldContract(BaseModel):
    required: list[str]
    optional: list[str]


# Coarse cap on the draft YAML body length. The whole request is already bounded by
# MAX_REQUEST_BODY_BYTES, but a per-field cap keeps a single pathologically large YAML string from
# being parsed at all. A real label template is a few KB; this is generous for that.
MAX_TEMPLATE_YAML_CHARS: int = 64 * 1024


class DraftPreviewRequest(BaseModel):
    """Live-preview a template that exists only as in-memory YAML text.

    Carries the raw template ``yaml`` body (the source of truth, never written to disk), the sample
    ``fields`` to substitute, and an optional ``language``. Rasterization options are intentionally
    absent: like ``/preview``, the draft preview is a pre-driver monochrome render and never
    dithers / goes two-color / 600-dpi (those only affect /print and /reprint), so accepting them
    would be a misleading divergence.
    """

    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(
        min_length=1,
        max_length=MAX_TEMPLATE_YAML_CHARS,
        description="Raw template YAML body (the version-controllable source of truth).",
        examples=[
            "name: draft\ndescription: ''\nlabel: '62'\nlayout:\n  - {type: title, text: Hi}"
        ],
    )
    fields: dict[str, Any] = Field(default={}, examples=[{"title": "Hello"}])
    language: str | None = Field(default=None, max_length=35, examples=["en"])


class TemplateParseRequest(BaseModel):
    """Parse a draft YAML body and return its field contract without rendering."""

    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(
        min_length=1,
        max_length=MAX_TEMPLATE_YAML_CHARS,
        description="Raw template YAML body to validate and extract fields from.",
    )


class TemplateParseResponse(BaseModel):
    """The auto-detected field contract of a valid draft template.

    Only real user fields are surfaced: computed/i18n tokens ({{date}}, {{now}}, {{seq}},
    [[translation]]) are excluded by the loader's field-contract logic, so the studio's generated
    form never asks the user to fill a clock- or translation-derived value.
    """

    name: str
    description: str
    label: str
    rotate: int
    fields: TemplateFieldContract


class SaveTemplateRequest(BaseModel):
    """Persist a draft template YAML to TEMPLATES_DIR (gated by TEMPLATES_WRITABLE).

    ``name`` is the bare template file name (no extension, no path separators) and is validated
    against path traversal server-side; ``yaml`` is the body to write.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=100,
        description="Bare template file name (no extension/path separators).",
        examples=["my-label"],
    )
    yaml: str = Field(min_length=1, max_length=MAX_TEMPLATE_YAML_CHARS)


class TemplateInfo(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "simple",
                    "description": "Simple title + subtitle label",
                    "label": "62",
                    "rotate": 0,
                    "fields": {"required": ["title"], "optional": ["subtitle"]},
                }
            ]
        }
    )

    name: str
    description: str
    label: str
    rotate: int
    fields: TemplateFieldContract


class TemplateSourceResponse(BaseModel):
    """Raw YAML body of an existing template, for loading into the template studio editor.

    Returned by ``GET /templates/{name}/source``. ``yaml`` is the file's verbatim text (comments and
    formatting preserved) so the editor round-trips exactly what is on disk.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "simple",
                    "yaml": 'name: simple\nlabel: "62"\nfields:\n  required: [title]\n'
                    'layout:\n  - {type: title, text: "{{title}}"}\n',
                }
            ]
        }
    )

    name: str
    yaml: str


class PrintJobRecord(BaseModel):
    job_id: str
    template: str
    fields: dict[str, Any]
    copies: int
    dry_run: bool
    timestamp: str
    language: str | None = None  # optional → pre-i18n history lines still validate
    # Frozen render inputs so /reprint reproduces the original label exactly.
    # All optional with defaults → older history lines still validate.
    cut: bool = True
    # Resolved rasterization options (env defaults already applied) so /reprint reproduces the
    # exact raster. Default RenderOptions (dither resolves to its False placeholder) keeps
    # pre-existing/legacy history rows loadable; live rows always store fully-resolved values.
    options: RenderOptions = Field(default_factory=RenderOptions)
    render_now: str | None = None  # ISO reference instant for {{date}}/{{now}} resolution
    # Outcome of the job. "failed" jobs are recorded for audit but rejected by /reprint.
    # Defaults to "printed" so pre-existing history lines remain reprintable.
    status: str = "printed"  # "printed" | "dry-run" | "failed"
    idempotency_key: str | None = None  # client retry key, if one was supplied
    request_fingerprint: str | None = None  # hash of the keyed request, to reject key reuse
    image_stripped: bool = False  # image blobs omitted from history → not reprintable
    # Frozen sequence spec: present when the job was a batch with {{seq}} numbering. One row
    # per batch (not one per item). /reprint replays the whole batch from this spec.
    sequence: SequenceSpec | None = None


class PrintResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                    "template": "simple",
                    "copies": 1,
                    "dry_run": False,
                }
            ]
        }
    )

    job_id: str
    template: str
    copies: int
    dry_run: bool


class HistoryPage(BaseModel):
    """One page of job history for the browse UI. ``entries`` are newest-first."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "entries": [
                        {
                            "job_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                            "template": "simple",
                            "fields": {"title": "Hello"},
                            "copies": 1,
                            "dry_run": False,
                            "timestamp": "2026-06-25T12:00:00",
                            "status": "printed",
                        }
                    ],
                    "total": 1,
                    "offset": 0,
                    "limit": 20,
                }
            ]
        }
    )

    entries: list[PrintJobRecord]
    total: int  # total records retained, for pagination controls
    offset: int
    limit: int


class LabelGeometry(BaseModel):
    width_px: int
    height_px: int | None  # None = continuous
    media_type: str  # "continuous" | "die_cut"


class CapabilityResponse(BaseModel):
    driver: str
    model: str
    dpi: int
    cut: bool
    # Two-color (red/black) capability: True when the configured model supports two-color
    # printing (QL-800/810W/820NWB). Clients discover this to decide whether to offer a `red`
    # toggle. The red DK media (e.g. DK-22251) is surfaced via ``red_labels``.
    two_color: bool
    supported_labels: list[str]
    # Label identifiers that are black/red media (Color.BLACK_RED_WHITE in brother_ql, e.g. "62red").
    # A red print needs both a two-color model AND one of these labels loaded.
    red_labels: list[str]
    label_geometries: dict[str, LabelGeometry]


class HealthResponse(BaseModel):
    status: str
    driver: str
    model: str
    transport: str
    uri: str
    label_size: str
    template_count: int
    default_language: str
    languages: list[str]


class PrinterState(StrEnum):
    """The single derived state of the physical printer, surfaced to the web UI.

    Collapses the (reachable, errors, busy) signals into one value the UI can render as a badge:

    * ``OFF`` — the printer could not be reached or queried (powered off, unreachable, or a
      transport that does not support status queries such as USB / the file sink).
    * ``IDLE`` — reachable and reporting no errors; ready to print.
    * ``PRINTING`` — a print job currently holds the transport lock.
    * ``ERROR`` — reachable but reporting one or more errors (out of media, cover open, etc.).
    """

    OFF = "off"
    IDLE = "idle"
    PRINTING = "printing"
    ERROR = "error"


class PrinterStatusResponse(BaseModel):
    """Response from GET /printer/status — describes the physical printer state.

    ``reachable`` is the primary field: False means the service could not open the transport
    and query the printer (unreachable, busy, or unsupported transport). When True the remaining
    fields are populated from the printer's 32-byte status reply. ``state`` collapses these
    signals into a single badge-ready value (see :class:`PrinterState`).
    """

    state: PrinterState = Field(
        description="Derived printer state: off, idle, printing, or error.",
    )
    uri: str = Field(description="The configured printer URI, e.g. 'tcp://192.168.1.100:9100'.")
    reachable: bool = Field(description="True if the printer responded to the status query.")
    model: str | None = Field(
        default=None,
        description="Printer model name as reported by the device, e.g. 'QL-800'.",
    )
    media_width_mm: int | None = Field(
        default=None,
        description="Width of the loaded media in millimetres (e.g. 62).",
    )
    media_length_mm: int | None = Field(
        default=None,
        description="Length of the loaded die-cut media in mm; 0 for continuous tape.",
    )
    media_type: str | None = Field(
        default=None,
        description="Human-readable media type string from the printer, e.g. 'Continuous length tape'.",
    )
    status: str | None = Field(
        default=None,
        description="Status type string from the printer, e.g. 'Reply to status request'.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase type string from the printer, e.g. 'Waiting to receive'.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Human-readable error strings reported by the printer, empty when ok.",
    )


class ErrorDetail(BaseModel):
    detail: str
