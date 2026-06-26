# SPDX-License-Identifier: GPL-3.0-or-later
"""File transport — writes raster bytes to a file. Used by tests and dry-run."""

from pathlib import Path

from app.transports.base import PrinterStatus, register_transport


@register_transport("file")
class FileTransport:
    def __init__(self, uri: str) -> None:
        # uri like "file:///tmp/output.bin" or just a path string
        path_str = uri.removeprefix("file://")
        self._path = Path(path_str)
        self.last_written: bytes = b""

    def send(self, data: bytes) -> PrinterStatus:
        # No printer behind a file sink, so there is no status to read back: report a synthetic
        # OK so the caller's error handling treats the write as a clean print.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(data)
        self.last_written = data
        return PrinterStatus.synthetic_ok()

    def query_status(self, request: bytes) -> PrinterStatus:
        """File sink has no printer — return synthetic ok (not a real device, not reachable).

        The ``request`` argument (the model-correct status-request payload) is accepted for
        interface compliance but ignored: there is no physical device to query.
        """
        return PrinterStatus.synthetic_ok()

    def close(self) -> None:
        pass
