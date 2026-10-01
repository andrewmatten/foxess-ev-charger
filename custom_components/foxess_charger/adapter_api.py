"""The slice of the controller's public API that the HA adapters depend on.

Entities and the coordinator only ever talk to the charger through a
``ChargingController`` (controller.py). This module resolves the two names
the adapters need at import time - ``ControlError`` (raised by every user
command that could not be confirmed or staged) and the controller class
itself - without making the platform modules import the transport stack.
"""
from __future__ import annotations

from typing import Any, Protocol

try:  # pragma: no cover - exercised once controller.py is present
    from .controller import ControlError  # type: ignore[attr-defined]
except ImportError:  # controller not built into this tree yet

    class ControlError(Exception):
        """A controller command failed; nothing was confirmed or staged."""


CONFIRMED = "confirmed"
STAGED = "staged"
SUPERSEDED = "superseded"


class ControllerLike(Protocol):
    """Structural type for what the adapters call on the controller."""

    data: dict
    desired_power_raw: int | None
    desired_current_raw: int | None
    phase: str
    diagnostics: dict
    intent_enabled: bool

    async def async_initialize(self, saved: dict | None = None, *, configure_safety: bool = True) -> None: ...
    async def async_poll(self) -> dict: ...
    async def async_set_power(self, raw: int) -> Any: ...
    async def async_set_current(self, raw: int) -> Any: ...
    async def async_enable(self) -> Any: ...
    async def async_pause(self) -> Any: ...
    async def async_set_register(self, address: int, value: int) -> Any: ...
    async def async_start(self) -> None: ...
    async def async_close(self) -> None: ...
    def export_state(self) -> dict: ...
