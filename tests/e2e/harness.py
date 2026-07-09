# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared launcher for the end-to-end harness.

Spawns a real ``uvicorn`` process serving ``app.main:app`` with a known API token and a
printer-less ``file://`` sink, so both the Playwright/API e2e tests and the manual dev harness
(``scripts/dev_harness.py``) drive an honest, fully wired server — not the in-process ``TestClient``
the unit suite uses. The single source of truth for the harness token and the web UI's
localStorage key lives here so the tests and the dev launcher cannot drift apart.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from types import TracebackType

# The default key the harness runs the server with. This is NOT a product default — the service
# itself still refuses to boot without an explicit token (see app/main.py:_require_auth_or_optout).
# It is only ever used for local e2e/manual runs and is pre-seeded into the web UI's localStorage so
# the page works out of the box. Override with the dev harness's --token flag or LABELITO_E2E_TOKEN.
DEFAULT_API_TOKEN = os.environ.get("LABELITO_E2E_TOKEN", "labelito-e2e-dev-token")

# localStorage key the web UI reads its bearer token from (see app/web/index.html: TOKEN_KEY).
WEB_TOKEN_KEY = "labelito_api_token"

# tests/e2e/harness.py → repo root (parents[0]=e2e, [1]=tests, [2]=root)
REPO_ROOT = Path(__file__).resolve().parents[2]

# Generous because a cold uvicorn import (FastAPI + Pillow + cairosvg) plus template/translation
# loading can take a few seconds on first run; the poll exits as soon as /health answers.
DEFAULT_STARTUP_TIMEOUT_S = 30.0


def web_token_init_script(token: str) -> str:
    """A browser init script that pre-fills the web UI's localStorage token before page scripts run.

    Wrapped in try/catch because init scripts also run on about:blank, where localStorage access
    raises; on the real origin it succeeds and index.html picks the token up on load. Shared by the
    e2e fixtures and the dev harness so the "default key just works" behaviour is identical.
    """
    return (
        f"try {{ localStorage.setItem({json.dumps(WEB_TOKEN_KEY)}, {json.dumps(token)}); }} "
        f"catch (e) {{}}"
    )


def find_free_port() -> int:
    """Bind to port 0 to let the OS pick an unused port, then hand it to uvicorn.

    There is a benign race (the port is released before uvicorn rebinds it), but on loopback for a
    short-lived dev/test server it is not worth a more elaborate handshake.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    base_url: str, proc: subprocess.Popen[bytes], log_path: Path, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server process exited early (code {proc.returncode}) before becoming healthy.\n"
                f"--- server log ---\n{log_path.read_text(errors='replace')}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    raise TimeoutError(
        f"server did not become healthy within {timeout}s.\n"
        f"--- server log ---\n{log_path.read_text(errors='replace')}"
    )


class LiveServer:
    """Context manager that runs ``app.main:app`` in a uvicorn subprocess on a free port.

    On ``__enter__`` it launches the process (token auth ON, ``file://`` printer sink, in-memory
    history) and blocks until ``/health`` returns 200. ``base_url`` is then ready to drive.
    """

    def __init__(
        self,
        *,
        token: str = DEFAULT_API_TOKEN,
        port: int | None = None,
        host: str = "127.0.0.1",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_S,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.token = token
        self.host = host
        self.port = port or find_free_port()
        self.base_url = f"http://{self.host}:{self.port}"
        self._startup_timeout = startup_timeout
        # Extra env applied last, so a test can flip e.g. PRINTER_URI/SNMP_ENABLED without touching
        # the file:// sink default. Used by the SNMP-server fixture to enable live status polling.
        self._env_overrides = env_overrides or {}
        self._proc: subprocess.Popen[bytes] | None = None
        # Capture combined stdout+stderr to a file so a failed startup surfaces the real traceback.
        log_fd, log_name = tempfile.mkstemp(prefix="labelito-e2e-", suffix=".log")
        os.close(log_fd)
        self._log_path = Path(log_name)
        self._log_handle = self._log_path.open("wb")
        sink_fd, sink_name = tempfile.mkstemp(prefix="labelito-e2e-", suffix=".bin")
        os.close(sink_fd)
        self._sink_path = Path(sink_name)

    def __enter__(self) -> LiveServer:
        env = dict(os.environ)
        env.update(
            {
                "API_TOKEN": self.token,
                # Force token auth on even if a local .env opted out — the harness asserts 401s.
                "ALLOW_UNAUTHENTICATED": "false",
                # file:// sink → printing writes raster bytes to a temp file, no hardware needed.
                "PRINTER_URI": f"file://{self._sink_path}",
                "HISTORY_MODE": "memory",
                "DEFAULT_LANGUAGE": "en",
                # Never let a page load in dev/e2e make a real GitHub request — the nav calls
                # /update-check on every load. Tests that exercise the update dot mock the response.
                "UPDATE_CHECK_ENABLED": "false",
                # Enable the in-browser label builder (YAML template studio) by default for local
                # dev/e2e runs so it's reachable without extra setup. Server-save (TEMPLATES_WRITABLE)
                # is intentionally left OFF: the harness's templates_dir is the repo's TRACKED
                # templates/ dir, so a writable save would let the builder mutate shipped templates.
                # The builder is fully usable without it — live draft preview + Copy/Download YAML.
                "EDITOR_ENABLED": "true",
            }
        )
        env.update(self._env_overrides)
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                self.host,
                "--port",
                str(self.port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=self._log_handle,
            stderr=self._log_handle,
        )
        try:
            _wait_for_health(self.base_url, self._proc, self._log_path, self._startup_timeout)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        if not self._log_handle.closed:
            self._log_handle.close()
        for path in (self._log_path, self._sink_path):
            path.unlink(missing_ok=True)
