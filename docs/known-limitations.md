# Known limitations

Labelito targets a single-printer, single-process home/intranet deployment. A few behaviours
are deliberate trade-offs for that scope rather than bugs. They are recorded here so they are a
conscious choice, not a surprise — and so the mitigations are ready if the project ever outgrows
the home setup.

## The network back-channel is silent → SNMP is the status channel

Brother's network NIC (e.g. the QL-810W's `Brother NC-36002w`) **accepts the `:9100` TCP connection
and the raster bytes but never returns the status frame** that the same printer returns over USB.
Verified live: `recv` on the back-channel times out. So `NetworkTransport`'s readback sees `None`,
which the print path treats as "no error reported" — and a job the **hardware** rejects (most often
because the loaded media does not match the template's `label`: a `62x29` die-cut template against a
62 mm continuous roll) makes the printer **blink red and print nothing while `/print` returns `200`
success**. The status channel that actually works on this hardware is **SNMP** (UDP 161, community
`public`), which answers instantly with loaded media, a reliable error bitmask, console text, and
identity (see [docs/snmp-status.md](snmp-status.md)).

**What is done about it:** over the **network** transport, `/printer/status` reads via SNMP rather
than the unreliable `:9100` readback, and `/print` + `/reprint` run a **pre-flight media-mismatch
guard** that rejects a loaded-vs-required mismatch with `409` before sending. This closes the common
phantom-success case (wrong media loaded) for network printers.

**Residual (accepted): the guard is fail-open, not fail-closed.** When SNMP is unreachable or
`SNMP_ENABLED=false`, the guard does **not** block — it logs a warning and proceeds, and the UI
badges status as unknown (`?`). A print sent while SNMP is down can therefore still hit the original
phantom-success hole (raster accepted, hardware rejects, `200` reported). This is deliberate: a home
print service should keep working when its *optional* status sidecar is briefly unreachable, and
failing closed would turn every SNMP blip into a hard print outage. The guard only ever *adds*
certainty.

**Other residuals:** the guard cross-checks **media geometry**, not every possible hardware fault —
a fault the status channel does not surface (or one that appears only after the raster is accepted)
can still slip through. The guard now covers **both** the network+SNMP path and the **USB** path
(`usb://` answers a standalone ESC i S status query, unlike the network NIC — see the USB status
section below), each re-queried under `_print_lock` at pre-flight so the read cannot race the send;
`file://` is a synthetic-OK debug sink with no printer, and a **network** printer with
`SNMP_ENABLED=false` has no status channel at all (the silent `:9100` back-channel), so it stays
unguarded. And because the raster `send()` path is unchanged, a job that passes the pre-flight check
but is rejected *after* the bytes are sent still reports `None` (unknown) → recorded `printed` — the
pre-flight check is a backstop in front of that path, not a replacement for it.

**Mitigation if needed later:** poll SNMP during/after the send for a post-print error transition, or
move to a printer/firmware that returns the `:9100` status frame, so the send path itself can confirm
the outcome instead of relying on a separate pre-flight read. *(Partially realized for observability:
the web status card now polls `/printer/status` on a background timer and answers mid-print, so an
operator sees a post-send fault transition within a poll cycle — but the **recorded job outcome** is
still the send-path `None`→`printed`, unchanged; this surfaces the transition, it does not gate on it.)*

**Verified live (2026-06-30, QL-810W): the post-send media fault is invisible to the error bitmask
and latches until a manual reset.** Sending a die-cut template to the loaded continuous roll put the
printer into a red-blink error; two behaviours held across repeated SNMP reads and matter for both
alerting and recovery:

- **The error bitmask stayed `00`.** `hrPrinterDetectedErrorState` — the source for the
  `printer_detected_error_state` gauge — did **not** flag this fault. It surfaced only in
  `prtConsoleDisplayBufferText` (`"ERROR"`) and `hrPrinterStatus` (`other`). `/printer/status` still
  catches it because the SNMP decode maps a console line ≠ `READY` into `errors[]` and reports
  `state=error`, but the per-condition Prometheus gauge reads **all-zero** during this class of fault.
  Alert on `printer_up` plus the console/status signal, not on the error-bit gauge alone.
- **The latch is sticky and locks the printer out.** While latched, the QL **buffered** every
  subsequent job — including a media-*matching* one — without printing (`prtMarkerLifeCount` frozen,
  `state` stuck at `error`), and each blocked job still returned `200`. Neither an SNMP write
  (read-only agent), nor an `ESC @` invalidate+initialize over `:9100`, nor a valid matching print
  cleared it. Only a device-side reset (power button / cover cycle) cleared the latch — and doing so
  **flushed the buffered job**, so the held label then printed. There is no remote recovery once
  latched.

**Now guarded (when SNMP is reachable).** The print preflight gained a second gate alongside the error
bitmask: a job is rejected with `409` when `hrPrinterStatus=other(1)` **and** the console line is not
`READY` — the exact signature of this latch. Idle reads `idle(3)`/`READY` and transient-busy states
read `printing(4)`/`warmup(5)`, so the gate fires only on the latch, not on valid back-to-back prints.
This turns the buffered phantom-`200` into an explicit `409` rather than silently queueing labels that
never print. The residual stands only when SNMP is unreachable or disabled (the guard is fail-open),
and a manual reset is still the only way to *clear* an existing latch.

This is the strongest argument for the guard being **pre-flight**: the failure it prevents is not a
clean one-shot rejection but a device-side lockout that silently buffers jobs behind a `200` and needs
someone physically at the printer to clear. The only reliable fix is to never send the mismatching job.

**Best-effort seam — live-status readiness can read stale-idle.** `hrPrinterStatus` and the console
line ride the *optional* (best-effort) SNMP GET, which SNMPv1 discards wholesale if any single OID in
it is unsupported or its datagram drops. When that optional read fails but the critical read (media +
error bitmask) succeeds, `/printer/status` has no readiness signal: a printer that is actually
busy/warming can read `state=idle` for that poll. The blast radius is small — the status card is
advisory and self-corrects on the next ≈4 s poll; a hard fault is still caught via the critical error
bitmask; and *our own* in-flight jobs still read `printing` via the print-lock fallback regardless of
the OID. The residual is only an **external** job (or a printer still finishing after our send) coinciding
with an optional-read failure. **Mitigation if it ever matters:** fetch `hrPrinterStatus` (+ console) in
a dedicated status GET, or promote them to the critical read — both trade a round-trip or some
fail-open robustness on firmware that does not implement those OIDs. Same root cause as the latch
guard's fail-open seam; deferred together pending a decision to restructure the SNMP batching.

## Loaded-roll awareness (media badge + picker size-focus) requires SNMP or USB

The print page badges the selected template ✓/✗ against the loaded roll and groups the picker by label
size, focusing the group that matches the loaded media (see [docs/template-format.md](template-format.md)
and [docs/snmp-status.md](snmp-status.md)). All of this needs to know the **physically loaded roll**,
reported in a normalized `continuous`/`die_cut` form. **Two** channels report it: SNMP on the network
transport, and a standalone **ESC i S** query on USB (`from_parsed` normalizes brother_ql's raw
`"Continuous length tape"`/`"Die-cut labels"` string to the canonical values via the shared
`app.media.canonical_media_type`, so both channels compare identically). On those paths the badge,
size-focus, live re-gating, and the `409` media-mismatch **print guard** all work.

The paths that stay *unknown* (badge neutral, every size shown, no guard) are the ones with **no
status channel**: `file://` has no printer, and a **network printer with `SNMP_ENABLED=false`** relies
on the silent `:9100` back-channel — its standalone ESC i S query was removed (the QL-810W NIC never
returns the frame, so it only burned the read deadline before reporting unreachable). On those paths
the loaded media reads as unknown and the print still goes out, just without the advisory badge/focus.

**USB cadence caveat:** USB status is read **on demand only** — page load, the manual ↻ button, and the
print preflight — never on the print page's 4 s background poll. Unlike SNMP (lock-free UDP 161), a USB
status query claims the single device handle and serializes through `_print_lock`, so polling it every
few seconds would contend with printing. A USB roll swap is therefore reflected on the next page load or
↻, not live mid-session. The gates are wired accordingly: `_status_query_supported()` (SNMP **or** USB)
drives the one-shot read + reprint gating, while `_snmp_guard_applies()` (`live_status_poll`, SNMP only)
drives the background poll.

## The TCP ESC i S status query was removed — network status needs SNMP

Earlier revisions had `NetworkTransport` fall back to a standalone **ESC i S** status query over the
`:9100` TCP back-channel when SNMP was disabled (invalidate prefix + `1b 69 53`, then read a 32-byte
status frame). That fallback was **removed**: on the only network hardware we have — the **QL-810W**
(`Brother NC-36002w` NIC) — the back-channel **accepts the request but never returns the status frame**
(`recv` times out). So the query could not succeed; it only burned the full read deadline before also
reporting the printer unreachable. Over **USB** the *same* printer answers a standalone ESC i S query
cleanly (verified live 2026-07-01), which is why the query lives on the USB transport and not the
network one — the asymmetry is in the NIC's back-channel, not the ESC i S protocol.

**Consequence:** a **network** printer with `SNMP_ENABLED=false` has **no status channel** — no media
badge, no size-focus, no pre-flight media guard (fail-open), status reads `unreachable`. SNMP (UDP 161)
is the only network status path. This is deliberate for our hardware, not an oversight.

**What we'd need to re-support TCP ESC i S** (only worth doing if a *different* model is confirmed to
answer the `:9100` back-channel):

- Restore a bounded `_query_status_esc_i_s` on `NetworkTransport` (invalidate + `1b6953`, then a
  deadline-bounded `recv` loop to a full 32-byte frame → `interpret_response` → `from_parsed`), mirroring
  the USB `_read_status_frame` shape but over the socket. The old `STATUS_*` frame constants that the
  print-readback path (`_read_status`) still uses are a starting point.
- **Gate it behind an explicit opt-in** — a per-printer/model flag or a `NETWORK_STATUS_MODE` setting —
  rather than making it the automatic SNMP-disabled fallback again, so a QL-810W-class NIC that swallows
  the frame does not silently re-introduce the deadline-burning dead path.
- Add coverage for `query_status` with `SNMP_ENABLED=false` against a working TCP status reply (the
  regression Codex specifically asked for), plus a timeout case proving the deadline is bounded.

Until a model is actually confirmed to return the frame, this stays removed: **no SNMP ⇒ no network
status.**

## Reprint replays the *current* template, not the original

`/reprint/{job_id}` re-renders the named template from the live registry using the original
job's frozen fields and timestamp. It does **not** snapshot the template definition at print
time.

**Consequence:** if a template is edited (and `/reload`ed) between the original print and the
reprint, the reprinted label can differ from the original — different layout, fonts, element
positions, or barcode/QR geometry — even though the API reports it as a faithful reprint.

**Why this is acceptable here:** templates are author-controlled and change rarely; reprints are
rare and usually happen soon after the original (a jam, a smudge). The frozen field/date values
— the parts that vary per print — *are* reproduced exactly, which is what matters for dated
labels.

**Mitigation if needed later:** store an immutable snapshot with each job — either the rendered
PNG/raster payload, the resolved layout, or at minimum a template content hash — and have
`/reprint` either render from the snapshot or refuse when the current template's hash differs.

## Retry de-duplication is opt-in and best-effort

`/print` de-duplicates retries only when the client supplies an `idempotency_key`, and only
against already-recorded **non-failed** jobs. A key reused with a *different* request (different
fields, template, copies, or `dry_run`) is rejected with 409 rather than silently returning the
old job. Without a key, identical requests print again (so you *can* intentionally print the same
label twice).

The de-dupe record is written **after** the irreversible printer send, not before, so three
narrow windows remain where a retry can produce a second physical label:

- **No record after a successful send.** If the history append never lands once `transport.send`
  has returned, the key is never recorded and a keyed retry reprints. This covers the process
  dying or the response being lost *and* the append itself failing (disk full, permission error):
  `_try_save_job` swallows the `OSError`, logs loudly that the record was lost, and still returns
  `200`, because the label is already physical and a successful print must not be reported as a
  failure.
- **Ambiguous send outcome.** A transport error is recorded as `failed`, and failed jobs are not
  de-duplicated, so a keyed retry reprints. If the bytes had actually reached the printer before
  the error, that retry is a duplicate.

These are deliberate trade-offs for a single-printer home setup, not oversights:

- Re-printing after a *failed* send is almost always what you want at home — the label usually
  didn't come out, and a wasted sticker on the rare "printed-then-TCP-error" case is cheap.
- Closing the no-record window would require a durable persist-**before**-send reservation (write
  `pending`, send, update to `printed`/`failed`) and an "unknown outcome" state that blocks
  re-sends until the operator confirms. That machinery is appropriate for a print *service* with
  paid/serialized output; it is overkill for one printer on a home LAN where a duplicate costs a
  sticker. It is intentionally not implemented.

**Per-label sequence batches amplify the same trade-off.** A sequence batch (R7) that fails
partway through — say, printed 299/500 then hit a driver or render error at label 300 — is
recorded `failed`. Because failed rows are not de-duplicated, a retry with the **same
`idempotency_key`** re-runs the whole sequence from the beginning and re-prints the 299 labels
already produced. This is the same accepted trade-off as the single-label failed-send case above,
just with larger potential duplicate output. A true fix would require persisting the partial printed
count alongside an `unknown`/`pending`/resume state that idempotency treats as consumed, then
resuming the sequence from the next unsent item on retry — the same pre-send reservation state
machine deliberately deemed overkill for a home setup, and out of scope here.

Mitigation if this ever matters: add the pre-send reservation + `unknown` status above, and have
keyed retries against a `pending`/`unknown` attempt return 409/202 instead of reprinting.

## History does not retain image blobs; image jobs are not reprintable

The history store keeps one record per job. A base64 image field can be ~7 MiB, so persisting it
verbatim would be ruinous — in `memory` mode the retained rows live in RAM, and at the prune
ceiling (1500 rows by default) that is gigabytes. Image field values are therefore **dropped**
from the stored record (rendering still uses the full image at print time), and the record is
flagged `image_stripped`.

**Consequence:** `/reprint` of an image-bearing job returns 409 — re-submit the original `/print`
request (with the image) to reproduce it. Text/QR/barcode/date jobs reprint normally. Idempotency
is unaffected: it keys off `request_fingerprint` (a hash of the full request), not the stored
fields.

## History storage mode changes idempotency and reprint behaviour, not just durability

History is **not only an audit log** — it is the substrate for two safety features: idempotency
de-duplication (stop a retried `/print` printing twice) and `/reprint`. The backend is selectable
via `HISTORY_MODE`, and the choice is a behavioural one:

- **`memory`** (default): an in-process SQLite database (`:memory:`). Dedup and reprint work for
  the lifetime of the process and are **reset on restart** — a restart mid-session silently drops
  the dedup state, so a keyed retry issued after the restart prints again. Right when you mostly
  care about the current run and don't want a file to manage.
- **`file`**: a durable SQLite database at `{data_dir}/history.db` (WAL). Dedup and reprint
  survive restarts. The shipped `docker-compose.yml` selects this, because it already mounts a
  persistent volume and `restart: unless-stopped` would otherwise reset duplicate protection on
  every restart.
- **`disabled`**: no history. Idempotency de-duplication is **off** (every keyed retry reprints)
  and `/reprint` always returns 404. This re-opens the duplicate-on-retry window the other modes
  close; it is an explicit opt-out, not a default.

**Pruning bounds the window.** Both SQLite modes keep at most `HISTORY_KEEP_ENTRIES` rows
(default 1000), pruning the oldest once the table exceeds `HISTORY_PRUNE_AT_ENTRIES` (default
1500) — hysteresis, so the delete runs once per ~500 prints rather than on every insert.
Consequence: a job older than the window can no longer be reprinted, and a stale `idempotency_key`
past the window will reprint on retry. For a home printer the default window covers far more than
any realistic batch.

**Why this is acceptable here:** a home setup rarely needs durable cross-restart history, and the
indexed SQLite lookups keep `/print` fast regardless of how full the window is (no more
whole-file scan). **Mitigation if cross-process durability is ever needed:** point `file` mode at
shared storage backed by a real queue or a DB with a unique constraint on `idempotency_key`, so
dedup holds across processes (see the single-worker note below).

## Reload drops a broken file rather than rolling back to its previous version

`/reload` reloads every template and catalog, skips any file that fails to parse/validate, and
reports the skipped files with a `422` (it no longer returns a misleading `200`). It does **not**
keep the *previous* good version of a file that has just become malformed — that file simply
leaves the live set until it is fixed and reloaded.

**Why this is acceptable here:** a dropped template fails loudly on the next `/print` (404 /
unknown template) instead of quietly printing a stale definition, which is the safer surprise for
a home user who just edited the file. The default-language catalog disappearing is treated as a
reload failure in its own right, so localized dates/words can't silently revert.

**Mitigation if needed later:** load into a temporary registry/catalog and swap atomically only
when every file parses, keeping the prior state untouched on any error. That snapshot-and-swap
machinery is more than a single-author, single-printer setup needs and is intentionally not
implemented.

## The service assumes a single worker process

Prints are serialized by an in-process lock (`_print_lock`) and de-duplicated through an
in-process history store (a single SQLite connection, `memory` or `file`); both assume **one**
process. The container runs a single uvicorn worker for this reason.

**Consequence:** running multiple workers (`--workers N`), or multiple containers pointed at the
same data dir, would race both mechanisms — the lock no longer serializes (it is per-process) and
two workers can pass the history-based idempotency check at once — so concurrent `/print` requests
could send overlapping raster bytes to the one printer and produce duplicate labels. `memory` mode
makes this worse still: each worker would have its *own* empty history, so cross-worker dedup
could not work even in principle.

The print path renders and sends *off* the event loop (in a worker thread), so an unreachable
printer never stalls `/health`, `/metrics`, `/preview`, or auth — within the single worker, only
the print path itself serializes behind the in-flight job.

**Why this is acceptable here:** one printer needs only one worker; a home setup has no throughput
reason to scale out. The `Dockerfile` CMD and `docker-compose.yml` both carry a comment warning
against adding workers.

**Mitigation if needed later:** move serialization and de-duplication to a shared store (a real
queue, or a DB with a unique constraint on `idempotency_key`) so they hold across processes.

## The USB transport worker thread may outlive the send timeout

The **network** transport bounds a stuck send: a dead printer fails within
`NetworkTransport.TIMEOUT` (10 s), after which the request `500`s and the print lock frees. The
**USB** transport now has a comparable hard timeout: `USB_TIMEOUT` (default 30 s, settable via
`USB_TIMEOUT` env var). When `helpers.send` blocks longer than that, `USBTransport.send` raises
`USBTimeoutError`, the job is recorded as `failed`, the print lock frees, and the service
continues accepting requests.

**Residual caveat:** a blocking libusb call cannot be cleanly cancelled from Python — unlike a
socket, there is no kernel-level abort. The timeout is implemented by running `helpers.send` in a
daemon worker thread and joining with a deadline; if the join expires the main thread is freed, but
the worker thread may still be running inside the kernel USB transfer until the device unblocks or
the container restarts. This is acceptable for a home setup: the client gets a clear `500`, and the
dangling thread clears on unplug/replug or container restart.

**Busy-lock guard:** while the orphaned worker is still inside the kernel transfer, the USB device
is marked busy (`_usb_busy = True`) and any subsequent print attempt is rejected immediately with
`USBBusyError` (`500`, job recorded as failed) rather than starting a competing libusb transfer
against the same device handle. The device becomes available again automatically once the orphaned
worker's `finally` block fires — i.e. when the stuck transfer unblocks, the kernel resets the
endpoint, or the container restarts. Successful sends clear the flag normally so the next print
proceeds without delay.

**Duplicate-on-retry after a timeout:** a `USB_TIMEOUT` expiry is recorded as `failed`, but the
outcome is genuinely *unknown* — the worker may have already written the raster and only be stuck in
the status-readback loop, so the label can still print after the request `500`s. Because
`_find_idempotent_job` ignores `failed` rows (see "Retry de-duplication is opt-in and best-effort"),
a retry with the **same `idempotency_key`** — once the busy flag clears — will send the label again
and may produce a **duplicate**. This is an accepted tradeoff for a home setup: a stuck USB transfer
is rare, and a missing label is worse than an occasional duplicate. Closing it would require a
distinct `unknown`/`timed_out` job state that idempotency treats as terminal (so retries surface the
ambiguous outcome instead of auto-reprinting) — deliberately out of scope here.

**Mitigation if cleaner cancellation is needed:** implement a cancellable transfer in the pyusb
backend itself — a timeout bolted above the blocking call cannot actually free the USB device
handle.

## Sequence batches send one label at a time with per-label status confirmation

A sequence batch (up to `count = 500`) is rendered **and sent** one label at a time. For each item
the print path resolves that item's `{{seq}}` value, renders the single label, converts it
(`copies=1`), sends it with one `transport.send`, and inspects the returned `PrinterStatus` —
*before* the next item is rendered. The whole loop runs while holding `_print_lock`, so the N
printer jobs are one uninterleaved logical batch: no other print can slip between two labels.

This fixes two earlier concerns at once:

- **Peak memory ≈ one label.** The just-sent label's decoded image and raster payload fall out of
  scope before the next item is rendered, so only **one decoded RGB image is resident at any
  moment**, regardless of `count` — a few MB even for high-resolution wide media, instead of the
  ~1–2 GB a 500-item high-res wide batch would have needed if the full list were buffered. The
  dry-run path likewise renders each label lazily (via the `render_sequence` generator) and
  discards each PNG as it goes, so a validation dry-run of a large batch cannot OOM either.
- **Mid-batch failure is detected (no phantom "printed").** Because a single small label completes
  well within the transport's status-read window (`STATUS_READ_DEADLINE`, ~10 s), R1's readback is
  meaningful *per label*. An explicit not-ok `PrinterStatus` (out of media, cover open,
  media-mismatch) **stops the batch at the failing label**: the job is recorded `failed`, the
  `label_errors_total{reason="printer_error"}` counter increments, `labels_printed_total` advances
  by only the labels actually sent (the partial count), and the request returns `502`. A 500-label
  batch that fails at label 300 is recorded `failed` with 299 labels counted — not silently
  recorded `printed` as the old single-atomic-send path did once its one long status read timed out
  to `None`. A `None` status for an individual label (state unknown — USB, or a silent network
  back-channel) keeps the original R1 semantics: it is **not** an error, so the loop proceeds and a
  batch with no explicit error is recorded `printed`.

`/reprint` replays a saved sequence batch the same per-label way (it calls the same code path with
the frozen `sequence` spec), so reprints get the same per-label confirmation.

**Residual (bounded, accepted): it is N printer jobs, not one.** Sending each label individually
means N `transport.send` calls and N status reads instead of one, so a large batch is somewhat
slower than a single atomic send would be, and on **continuous tape** each label is its own job —
so each feeds and (when `cut=true`) cuts at its end, giving one piece per label with a per-label
feed/cut rather than one cut for the whole strip. For **die-cut media** this is identical to before
(each label was always one physical piece). The `count` cap (500) and the single-process
single-worker deployment keep the per-label overhead bounded.

**Why this is acceptable here:** confirming each label is exactly the point — a home user wants to
know *which* label failed and to stop wasting tape, not discover after the fact that a long batch
was recorded as a success while the printer jammed at label 3. The extra per-label feed/cut on
continuous tape is the natural cost of treating each label as an independently-confirmed print, and
on die-cut media (the common case for serialized labels) there is no cost at all.

**Debug sink caveat — `file://` keeps only the last label of a batch.** Because each label is its
own `transport.send`, the `file://` transport (`PRINTER_URI=file:///tmp/out.bin`, the documented
raster-inspection sink) writes the file once per label and **overwrites** it each time, so after a
sequence batch only the *final* label's raster remains on disk. The print still succeeds and all N
labels are confirmed/counted normally — this only affects the debug sink, not a real USB/network
printer (which receives every label). To inspect a specific label's bytes, print that item as its
own single (`count = 1`, `start = <n>`) job. Making `file://` capture every label (numbered sibling
files / a framed append stream) would change its single-print overwrite semantics for marginal
debug value, so it is intentionally left as a last-frame sink.

## Wide-format high-resolution continuous labels can hit the decompression-bomb guard

High-resolution (600 dpi) mode renders the label at 2× linear scale (see R5). The service sets
Pillow's `Image.MAX_IMAGE_PIXELS` to a 16 MP anti-DoS limit for **untrusted uploaded** images, and
Pillow raises `DecompressionBombError` above **2×** that limit (~32 MP). The driver re-opens the
renderer's own (trusted) PNG through the same global limit.

For **standard 62 mm models** (e.g. QL-810W) this is a non-issue: the per-model raster-row ceiling
(11811 rows) caps a high-res continuous label at ~1392 × 11811 ≈ 16.4 MP — above the warn threshold
but well under the ~32 MP error threshold, so it only emits a Pillow warning, never raises.

For **wide-format models** (QL-1100-class, ~2400 px wide, row ceiling ~35434) a high-res
**continuous** label taller than ~13,300 rows (~564 mm) exceeds ~32 MP and the request fails with a
`500` at the internal PNG re-open — even though the printer itself would accept the label. This is an
accepted tradeoff for a home setup (wide-format hardware plus very long high-res continuous labels is
an uncommon combination); standard models are unaffected. The clean fix is to exempt the
renderer's own trusted output from the upload decompression-bomb guard (lift `MAX_IMAGE_PIXELS` only
around that specific internal `Image.open`, or hand the driver a `PIL.Image` directly instead of
re-encoding to PNG) — deliberately out of scope here to avoid touching the untrusted-upload security
boundary.
