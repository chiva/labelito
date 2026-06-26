# SPDX-License-Identifier: GPL-3.0-or-later
"""USB transport using brother_ql's USB backend.

Requires: pyusb OR the linux_kernel backend (no extra install on Linux).
Pass uri as 'usb://0x04f9:0x209c' (vendorId:productId) or a device path.
"""

from __future__ import annotations

import logging
import threading

from app.config import settings
from app.transports.base import PrinterStatus, register_transport

log = logging.getLogger(__name__)

# Named constant so the timeout is never a magic number inline. Read from settings so tests can
# monkeypatch it to a small value and the integration test runs in milliseconds, not real seconds.
USB_TIMEOUT = settings.usb_timeout

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
        """USB status query is not supported.

        The pyusb backend's read path is tightly coupled to the print flow (brother_ql's
        blocking helpers.send controls the USB read loop). There is no clean API to send
        ESC i S and read a reply without a print job in flight. Rather than hacking libusb
        directly (which would require device ownership and conflict with the _USB_DEVICE_LOCK
        guard), we return "unsupported" here and document it clearly. The /printer/status
        endpoint returns 503 with an informative message for USB transports.

        The ``request`` argument (the model-correct status-request payload) is accepted for
        interface compliance but ignored: USB status queries are not supported.

        If a prior send is still running (device busy), that is surfaced in the error message
        rather than a generic "unsupported" to give the caller more actionable context.
        """
        if _usb_busy:
            return PrinterStatus.unsupported(
                f"USB device {self._uri!r} is busy with a print transfer; retry after it clears"
            )
        return PrinterStatus.unsupported(
            f"status query is not supported over USB ({self._uri!r}); "
            "use a tcp:// transport to query printer status"
        )

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
