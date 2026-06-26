# SPDX-License-Identifier: GPL-3.0-or-later
"""Root pytest configuration.

Gates the opt-in end-to-end suite behind ``--e2e``. The e2e tests launch a real uvicorn process and
drive a real browser, so they are skipped by default — including under any ``-m`` expression (a
``-m`` on the command line replaces, rather than ANDs with, a default marker filter, so a
marker-based opt-out here would silently break ``pytest -m "not hardware"``). Skipping via a
collection hook is immune to that.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="run the end-to-end suite (needs `playwright install chromium`; launches a real server)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--e2e"):
        return
    skip_e2e = pytest.mark.skip(
        reason="e2e test — pass --e2e to run (needs a browser + live server)"
    )
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
