# SPDX-License-Identifier: GPL-3.0-or-later
"""Printer driver Protocol and capability model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.models import LabelGeometry


@dataclass
class Capability:
    name: str
    dpi: int
    cut: bool
    # Two-color (red/black) printing support for the configured model. True for
    # QL-800/810W/820NWB; False otherwise. The print path rejects a red=True request on a
    # two_color=False model with a clean 4xx instead of a 500.
    two_color: bool
    supported_labels: list[str]
    # Subset of supported_labels that are black/red media (Color.BLACK_RED_WHITE), e.g. "62red". A
    # red print needs both two_color support AND one of these loaded.
    red_labels: list[str]
    label_geometries: dict[str, LabelGeometry]  # label_id -> geometry


@runtime_checkable
class PrinterDriver(Protocol):
    CAPABILITY: Capability

    def render_payload(self, png: bytes, opts: dict[str, Any]) -> bytes:
        """Convert a PNG image to printer-ready bytes."""
        ...


DRIVERS: dict[str, type[PrinterDriver]] = {}


def register_driver(name: str):  # type: ignore[no-untyped-def]
    """Decorator to register a driver class."""

    def decorator(cls: type[PrinterDriver]) -> type[PrinterDriver]:
        DRIVERS[name] = cls
        return cls

    return decorator


def get_driver(name: str) -> type[PrinterDriver]:
    if name not in DRIVERS:
        raise ValueError(f"Unknown driver {name!r}. Available: {sorted(DRIVERS)}")
    return DRIVERS[name]
