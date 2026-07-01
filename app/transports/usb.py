# SPDX-License-Identifier: GPL-3.0-or-later
"""USB transport using brother_ql's USB backend.

Requires: pyusb OR the linux_kernel backend (no extra install on Linux).
Pass uri as 'usb://0x04f9:0x209c' (vendorId:productId) or a device path.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from app.config import settings
from app.transports.base import PrinterStatus, register_transport

if TYPE_CHECKING:
    from brother_ql.backends.pyusb import BrotherQLBackendPyUSB

log = logging.getLogger(__name__)

# Named constant so the timeout is never a magic number inline. Read from settings so tests can
# monkeypatch it to a small value and the integration test runs in milliseconds, not real seconds.
USB_TIMEOUT = settings.usb_timeout

# ── Standalone status-query (ESC i S) tuning ──────────────────────────────────────────────────────
# The Brother QL answers ESC i S with a single 32-byte status frame over the USB IN endpoint.
STATUS_PACKET_LEN = 32
# The pyusb backend's default per-read timeout is 10ms — far too short for a cold round trip, so the
# read can miss the reply. Bumped to 500ms; the backend's try_twice strategy sleeps this between its
# two internal attempts, so a healthy printer answers well within it.
USB_STATUS_READ_TIMEOUT_MS = 500
# Overall budget for draining the one status frame, so a silent/wedged device cannot pin the worker.
USB_STATUS_READ_DEADLINE_S = 5.0
# Idle sleep between empty reads while waiting for the frame, so the drain loop does not busy-spin.
USB_STATUS_READ_POLL_S = 0.05
# Outer bound for the WHOLE standalone status transaction (open + write + drain + dispose), run in a
# worker thread. Deliberately MUCH shorter than USB_TIMEOUT (which bounds a real print): a status read
# completes in well under a second, and it runs while the caller holds _print_lock — so a wedged
# libusb open/write/dispose must not monopolize the lock for the full print timeout and queue real
# /print or /reprint requests. Comfortably exceeds USB_STATUS_READ_DEADLINE_S so a healthy-but-slow
# read still completes within it.
USB_STATUS_TIMEOUT = 8.0

# ── USB device concurrency guard ────────────────────────────────────────────────────────────────
#
# Invariant: at most one helpers.send() may touch the USB device at a time.
#
# The mechanism: the worker thread acquires _USB_DEVICE_LOCK at the very start of its body and
# releases it in a finally after helpers.send() returns or raises.  _usb_busy mirrors the lock
# state so the MAIN thread can check device availability without blocking: it reads _usb_busy
# (set True by the worker under the lock) and raises USBBusyError immediately if a prior worker
# is still active — so the main thread NEVER waits on _USB_DEVICE_LOCK and cannot hang.
#
# On timeout: _execute_print raises USBTimeoutError and returns, but the orphaned worker thread
# still holds _USB_DEVICE_LOCK (and _usb_busy remains True) until helpers.send() finishes inside
# the kernel transfer.  Any subsequent print attempt is therefore rejected with USBBusyError
# rather than racing against the stuck transfer.  Once the orphaned thread unblocks it clears
# _usb_busy in its finally, and the device is available again for the next print.
#
# No deadlock: the main thread never acquires _USB_DEVICE_LOCK; only the worker does. A normal
# successful send acquires, finishes, and releases — the next print sees _usb_busy=False and
# proceeds.  A timed-out send keeps _usb_busy=True until the device is physically clear.
_USB_DEVICE_LOCK = threading.Lock()
_usb_busy = False


def usb_device_busy() -> bool:
    """True while a USB send()/query worker still owns the device — including an ORPHANED worker left
    running after a timeout. Read without ever acquiring ``_USB_DEVICE_LOCK`` (so the caller cannot
    block), it lets the print preflight tell 'device busy, a send would USBBusyError' apart from
    'status genuinely unreachable, safe to fail open' — the two both surface as an unreachable status
    but must be handled differently (clean 503 vs allow-the-print)."""
    return _usb_busy


@register_transport("usb")
class USBTransport:
    def __init__(self, uri: str) -> None:
        self._uri = uri

    def send(self, data: bytes) -> PrinterStatus | None:
        # brother_ql's blocking send() does the USB readback loop internally and returns a status
        # dict — it does NOT raise on a printer error. Discarding it recorded out-of-media /
        # cover-open as a successful print. Map the dict onto PrinterStatus so the caller can fail
        # the job, mirroring how NetworkTransport surfaces error frames.
        #
        # blocking=True (the default, passed explicitly) is required for the readback loop to run;
        # with blocking=False the helper returns immediately with outcome 'sent' and no state.
        #
        # The pyusb backend's internal transfer timeouts are unreliable / version-dependent, so we
        # enforce an outer deadline by running helpers.send in a worker thread and joining with a
        # timeout. If the join expires, we raise USBTimeoutError — the worker thread may still be
        # running inside the kernel transfer (it cannot be force-killed in Python) but the main
        # thread is freed and the print lock is released. The orphaned thread will unblock once the
        # device eventually clears or the container restarts.
        from brother_ql.backends.helpers import send

        global _usb_busy

        # Fast fail: if a prior worker (possibly orphaned by a timeout) still holds the device,
        # reject immediately rather than racing or blocking. The main thread NEVER acquires
        # _USB_DEVICE_LOCK directly — it only reads the _usb_busy flag, which is set/cleared
        # exclusively by the worker thread under _USB_DEVICE_LOCK.
        if _usb_busy:
            raise USBBusyError(
                f"USB device {self._uri!r} is busy with a prior transfer; retry after it clears"
            )

        result: list[dict[str, object]] = []
        exc_holder: list[BaseException] = []

        def _worker() -> None:
            global _usb_busy
            # Acquire the device lock for the worker's ENTIRE lifetime, including any orphaned
            # period after the main thread's join has expired. _usb_busy is set inside the lock
            # so the main thread's flag read is always consistent with actual lock ownership.
            with _USB_DEVICE_LOCK:
                _usb_busy = True
                try:
                    result.append(
                        send(
                            instructions=data,
                            printer_identifier=self._uri,
                            backend_identifier="pyusb",
                            blocking=True,
                        )
                    )
                except BaseException as exc:
                    exc_holder.append(exc)
                finally:
                    _usb_busy = False

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=USB_TIMEOUT)

        if thread.is_alive():
            # The join expired — the thread is still running inside the blocking USB transfer.
            # _usb_busy remains True (the worker holds _USB_DEVICE_LOCK) so any subsequent print
            # attempt will receive USBBusyError instead of starting a competing transfer.
            # The worker will clear _usb_busy in its finally once the device unblocks.
            log.error(
                "USB send to %s timed out after %ds; worker thread may still be running",
                self._uri,
                USB_TIMEOUT,
            )
            raise USBTimeoutError(f"USB send to {self._uri!r} timed out after {USB_TIMEOUT}s")

        if exc_holder:
            raise exc_holder[0]

        status = result[0]
        outcome = status.get("outcome")
        printer_state = status.get("printer_state")

        # interpret_response (under "printer_state") carries the error strings; an explicit
        # outcome=='error' covers the same condition the helper detected. Either fails the job.
        raw_state: dict[str, object] = printer_state if isinstance(printer_state, dict) else {}
        raw_errors = raw_state.get("errors")
        errors: list[str] = [str(e) for e in raw_errors] if isinstance(raw_errors, list) else []
        if outcome == "error" or errors:
            return PrinterStatus(ok=False, errors=errors, raw=raw_state)

        # The helper only sets outcome 'printed' once it observed both 'Printing completed' and
        # 'Waiting to receive' (did_print and ready_for_next_job) — the same bar NetworkTransport
        # uses for a confirmed success.
        if outcome == "printed" and status.get("did_print") and status.get("ready_for_next_job"):
            return PrinterStatus(ok=True, errors=[], raw=raw_state)

        # outcome 'sent'/'unknown' or no usable readback (printer_state is None): state unknown.
        # Return None so the job still records as printed rather than failing on indeterminate info,
        # preserving the "couldn't determine, don't fail the job" semantics.
        return None

    def query_status(self, request: bytes) -> PrinterStatus:
        """Send ESC i S over USB and parse the printer's one-shot 32-byte status reply.

        Unlike the QL-810W's network NIC (whose :9100 back-channel never returns the status frame —
        see docs/known-limitations.md, the reason SNMP exists for the network transport), the USB
        back-channel DOES answer a standalone status request. So we open the pyusb backend, write the
        model-correct ``request`` (invalidate prefix + ESC i S, built by the caller via
        ``BrotherQLRaster``), drain one 32-byte frame, and map it via
        :meth:`PrinterStatus.from_parsed` — the same parse the network print-readback uses, yielding
        canonical media/model fields.

        Concurrency & timeout: the whole transaction (open → write → read → dispose) runs in a worker
        thread joined with ``USB_STATUS_TIMEOUT`` — much shorter than the print ``USB_TIMEOUT``, since
        query_status is called under the caller's ``_print_lock`` (the /printer/status non-SNMP branch
        and the print preflight both hold it) and every libusb call (construction/``set_configuration``,
        ``_write``, ``_dispose``) can block. Bounding only the frame-drain loop would let a libusb hang
        pin ``_print_lock`` and stall ALL later prints for the full print timeout. On a join timeout the
        main thread returns promptly (freeing ``_print_lock``) while the orphaned worker keeps
        ``_USB_DEVICE_LOCK`` / ``_usb_busy`` set until the device clears, so a later print/query is
        rejected rather than racing. The main thread NEVER acquires ``_USB_DEVICE_LOCK`` (only reads
        ``_usb_busy``), so it cannot hang.

        Never raises: any device/parse failure or timeout yields :meth:`PrinterStatus.unreachable` so
        the caller fails open (allow the print, badge the UI unknown) and /printer/status returns 503.
        """
        global _usb_busy

        # Fast fail if a prior transfer (possibly an orphaned print/query) still owns the device.
        if _usb_busy:
            return PrinterStatus.unreachable(
                f"USB device {self._uri!r} is busy with a prior transfer; retry after it clears"
            )

        result: list[PrinterStatus] = []

        def _worker() -> None:
            global _usb_busy
            # Hold the device lock for the worker's ENTIRE lifetime, including any orphaned period
            # after the main thread's join expires — same invariant as send().
            with _USB_DEVICE_LOCK:
                _usb_busy = True
                try:
                    result.append(self._query_once(request))
                finally:
                    _usb_busy = False

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=USB_STATUS_TIMEOUT)

        if thread.is_alive():
            # A libusb call is wedged. Return promptly so the caller frees _print_lock; _usb_busy stays
            # True (worker holds the lock) so a later print/query fast-fails instead of racing.
            log.error(
                "USB status query to %s timed out after %gs; worker thread may still be running",
                self._uri,
                USB_STATUS_TIMEOUT,
            )
            return PrinterStatus.unreachable(
                f"USB status query to {self._uri!r} timed out after {USB_STATUS_TIMEOUT:g}s"
            )
        if result:
            return result[0]
        return PrinterStatus.unreachable(f"USB status query to {self._uri!r} produced no result")

    def _query_once(self, request: bytes) -> PrinterStatus:
        """Open the device, send ESC i S, drain one 32-byte frame, and parse it. Runs inside the
        worker thread under ``_USB_DEVICE_LOCK``. Never raises: maps every failure to
        :meth:`PrinterStatus.unreachable`, disposing the device in a finally."""
        from brother_ql.backends.pyusb import BrotherQLBackendPyUSB
        from brother_ql.reader import interpret_response

        backend: BrotherQLBackendPyUSB | None = None
        try:
            backend = BrotherQLBackendPyUSB(self._uri)
            # Bump the backend's 10ms default so a cold round trip is not missed (see constant note).
            backend.read_timeout = USB_STATUS_READ_TIMEOUT_MS
            backend._write(request)
            frame = self._read_status_frame(backend)
            if frame is None:
                return PrinterStatus.unreachable(
                    f"USB printer {self._uri!r} did not return a status frame within "
                    f"{USB_STATUS_READ_DEADLINE_S:g}s"
                )
            # interpret_response raises ValueError/NameError on a garbled/short frame; from_parsed
            # normalizes media_type to canonical continuous/die_cut.
            return PrinterStatus.from_parsed(interpret_response(frame))
        except (OSError, ValueError, NameError) as exc:
            # usb.core.USBError subclasses OSError; ValueError covers "Device not found" and a garbled
            # frame. Log loudly so a claim/permission problem is visible, but fail open.
            log.warning("Could not query USB status from %s: %s", self._uri, exc)
            return PrinterStatus.unreachable(
                f"could not query status over USB ({self._uri!r}): {exc}"
            )
        finally:
            if backend is not None:
                # Release the interface and reattach the kernel driver (Linux) so the next print — or
                # the OS print queue — can claim the device. Never let a dispose error mask the result.
                try:
                    backend._dispose()
                except Exception as exc:
                    log.warning("USB status dispose on %s raised: %s", self._uri, exc)

    def _read_status_frame(self, backend: BrotherQLBackendPyUSB) -> bytes | None:
        """Drain exactly ``STATUS_PACKET_LEN`` bytes from the backend's IN endpoint, or None on timeout.

        A standalone ESC i S reply is a single 32-byte frame, but ``_read`` may return it in pieces or
        return empty while the printer is still assembling it, so we accumulate until full or until
        ``USB_STATUS_READ_DEADLINE_S`` elapses. Mirrors NetworkTransport._recv_one_frame."""
        buffer = bytearray()
        deadline = time.monotonic() + USB_STATUS_READ_DEADLINE_S
        while len(buffer) < STATUS_PACKET_LEN:
            if time.monotonic() >= deadline:
                return None
            try:
                chunk = backend._read(STATUS_PACKET_LEN - len(buffer))
            except OSError:
                # A USB read timeout (usb.core.USBError subclasses OSError) is NOT end-of-status: the
                # pyusb backend RAISES on a no-data read rather than returning b"", and the printer may
                # still answer later within the deadline. Keep polling instead of letting the exception
                # propagate to query_status and fail the guard open on the first slow read — mirrors
                # NetworkTransport._read_status's TimeoutError handling.
                chunk = b""
            if chunk:
                buffer.extend(chunk)
            else:
                time.sleep(USB_STATUS_READ_POLL_S)
        return bytes(buffer[:STATUS_PACKET_LEN])

    def close(self) -> None:
        pass


class USBTimeoutError(TimeoutError):
    """Raised when helpers.send blocks longer than USB_TIMEOUT seconds.

    Classified as a transport-level failure (maps to 'print_error' in main.py's except block)
    so the job is recorded as failed and label_errors_total{reason=print_error} increments —
    reusing the existing generic transport-failure reason rather than adding a new metric label.
    """


class USBBusyError(RuntimeError):
    """Raised when a USB send is attempted while a prior transfer still owns the device.

    This guards against competing libusb operations on a single device handle, which can
    occur when a previous helpers.send() was orphaned by a USBTimeoutError: the main thread
    released _print_lock but the worker thread is still inside the kernel USB transfer.
    Any subsequent print that would start a second helpers.send() against the same device
    is rejected immediately with this error instead of racing.

    Classified as a transport-level failure — caught by _execute_print's generic Exception
    handler in main.py, which records the job failed and emits label_errors_total{reason=print_error}.
    The device becomes available again once the orphaned worker's finally block fires (i.e. when
    the stuck kernel transfer unblocks or the container restarts).
    """
