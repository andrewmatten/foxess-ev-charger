"""Logical charging-session tracking, independent of controller commands.

A logical session is what HA users see as "one charge": it starts when a
fresh, valid status shows an active session (start/charging/vehicle-paused)
and it completes exactly once, when either

- the charger reports a known inactive status (vehicle finished, unplugged,
  fault ...), or
- the user deliberately paused charging (controller intent off) and the
  pause has been confirmed.

Active statuses are start/charging/vehicle-paused/phase-switching
(2/3/4/9): a vehicle pause or a phase switch does not end a session. An unknown or invalid status never ends one either - it is
held until a valid reading arrives.

After a user pause has ended a session, the charger typically sits in a
paused/connected state that still reads "active". A new logical session
therefore needs re-arming first: the user enabling charging again, or a
valid inactive status (e.g. unplug) being observed.

Energy is the lifetime-counter (0x1016) delta across the logical session,
so a per-session counter that does not reset on a zero-power pause cannot
double count.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Callable

from .const import STOP_REASON_MAP
from .protocol import ACTIVE_STATUSES as SESSION_ACTIVE_STATUSES
from .protocol import STATUS_PHASE_SWITCHING, is_valid_status
# Measured power (0.1 kW units) at or below which a paused session counts
# as not drawing.
PAUSED_POWER_RAW_MAX = 1


def valid_status(data: dict) -> int | None:
    """The status enum if this observation carries a known, valid one."""
    if data.get("status_valid") is False:
        return None
    status = data.get("status")
    return status if is_valid_status(status) else None


class SessionTracker:
    """Stateful session detector fed one observation per poll."""

    def __init__(self, *, on_complete: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._on_complete = on_complete
        self._clock = clock
        self._wall = wall_clock
        self.start_ts: float | None = None
        self.start_wall: float | None = None
        self.start_total: int | None = None
        self.peak_power_raw = 0
        self.prev_status: int | None = None
        self.last_session: dict | None = None
        self.rearm_required = False
        self.dirty = False

    @property
    def active(self) -> bool:
        return self.start_ts is not None

    def restore(self, stored: dict) -> None:
        """Restore persisted fields (already validated by persistence)."""
        self.last_session = stored.get("last_session")
        self.prev_status = stored.get("prev_status")
        self.rearm_required = bool(stored.get("rearm_required", False))
        wall, total = stored.get("session_start_wall"), stored.get("session_start_total")
        if wall is not None and total is not None:
            elapsed = max(self._wall() - wall, 0.0)
            self.start_ts = self._clock() - elapsed
            self.start_wall = wall
            self.start_total = total
            self.peak_power_raw = 0

    def export(self) -> dict:
        return {
            "session_start_wall": self.start_wall,
            "session_start_total": self.start_total,
            "last_session": self.last_session,
            "prev_status": self.prev_status,
        }

    def is_boundary(self, data: dict) -> bool:
        """Whether this observation starts a new logical session."""
        status = valid_status(data)
        return (
            status in SESSION_ACTIVE_STATUSES
            and not self.active
            and not self.rearm_required
        )

    def update(self, data: dict, *, intent_enabled: bool | None) -> None:
        """Advance on one fresh observation and annotate ``data``."""
        status = valid_status(data)
        if intent_enabled:
            self.rearm_required = False
        if status is None:
            data["session_active"] = self.active
            return

        if status not in SESSION_ACTIVE_STATUSES:
            if self.rearm_required:
                self.rearm_required = False
                self.dirty = True
            if self.active:
                self._complete(data)
        elif (
            status in SESSION_ACTIVE_STATUSES and not self.active
            and not self.rearm_required
            # Already paused by the user and not drawing: nothing to record.
            and not (intent_enabled is False and self._pause_observed(data, status))
        ):
            self._start(data)

        if self.active:
            self.peak_power_raw = max(self.peak_power_raw, data.get("power_raw") or 0)
            if intent_enabled is False and self._pause_observed(data, status):
                self.end_for_user_pause(data)

        if status != self.prev_status:
            self.dirty = True
        self.prev_status = status
        data["session_active"] = self.active
        if self.last_session is not None:
            data["last_session"] = self.last_session

    @staticmethod
    def _pause_observed(data: dict, status: int) -> bool:
        power = data.get("power_raw")
        return (
            status in (4, 5, 0, 1)
            and isinstance(power, int) and power <= PAUSED_POWER_RAW_MAX
        )

    def end_for_user_pause(self, data: dict) -> bool:
        """Complete the current session because the user paused charging.

        Returns True if a session was completed; a second call (or a call
        with no session running) does nothing.
        """
        if not self.active:
            return False
        self._complete(data)
        self.rearm_required = True
        self.dirty = True
        data["session_active"] = False
        return True

    def _start(self, data: dict) -> None:
        self.start_ts = self._clock()
        self.start_wall = self._wall()
        self.start_total = data.get("total_energy_raw")
        self.peak_power_raw = 0
        data["session_start"] = datetime.now(timezone.utc).isoformat()
        self.dirty = True

    def _complete(self, data: dict) -> None:
        duration_s = self._clock() - (self.start_ts or self._clock())
        energy_raw = None
        end_total = data.get("total_energy_raw")
        if self.start_total is not None and isinstance(end_total, int):
            delta = end_total - self.start_total
            if delta >= 0:
                energy_raw = delta
        if energy_raw is None:
            energy_raw = data.get("current_energy_raw") or 0
        hours = duration_s / 3600
        record = {
            "ended": datetime.now(timezone.utc).isoformat(),
            "duration_min": round(duration_s / 60, 1),
            "energy_kwh": round(energy_raw * 0.1, 2),
            "avg_power_kw": round((energy_raw * 0.1) / hours, 2) if hours > 0 else 0,
            "peak_power_kw": round(self.peak_power_raw * 0.1, 2),
            "stop_reason": STOP_REASON_MAP.get(data.get("stop_reason") or 0, "unknown"),
        }
        self.last_session = record
        data["last_session"] = record
        self.start_ts = self.start_wall = self.start_total = None
        self.peak_power_raw = 0
        self.dirty = True
        if self._on_complete is not None:
            self._on_complete(record)
