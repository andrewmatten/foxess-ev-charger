"""Polling coordinator: exposes the controller's observations to entities.

It holds no command state. Every poll asks the controller for one fresh
observation (``controller.async_poll()``), then layers on what only HA
needs: per-block freshness for entity availability, energy plausibility
guarding, logical-session tracking and persistence of those two.
"""
from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ALARM_BITS, BLOCK_CONFIG, BLOCK_PHASE_BOX, BLOCK_STALENESS_FACTOR, BLOCK_STATUS,
    DOMAIN, EVENT_SESSION_COMPLETED, FAULT_BITS, MAX_STATUS_READ_FAILURES,
    WORK_MODE_MAP, decode_bitmask,
)
from .energy import EnergyTracker
from .session import SessionTracker

if TYPE_CHECKING:
    from .adapter_api import ControllerLike
    from .persistence import ChargerStorage

_LOGGER = logging.getLogger(__name__)

# Observation keys per register block. When a block could not be read this
# poll, its keys keep their last successfully-read values for display while
# the block's freshness (and so its entities' availability) ages out.
BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    BLOCK_STATUS: (
        "device_address", "software_version", "stop_reason", "status",
        "cp_status", "cc_status", "port_temp_raw", "ambient_temp_raw",
        "l1_voltage_raw", "l2_voltage_raw", "l3_voltage_raw",
        "l1_current_raw", "l2_current_raw", "l3_current_raw", "power_raw",
        "lock_status", "phase_sequence", "max_power_raw", "min_power_raw",
        "max_current_raw", "min_current_raw", "alarm_code",
        "total_energy_raw", "current_energy_raw", "fault_code", "rfid_card",
        "status_valid", "cc_status_valid",
    ),
    BLOCK_CONFIG: (
        "work_mode", "max_charging_current_raw", "max_charging_power_raw",
        "allowed_charge_time", "allowed_charge_energy", "time_validity",
        "default_current_raw",
    ),
    BLOCK_PHASE_BOX: ("auto_phase_switch", "min_switch_interval"),
}
TRANSPORT_COUNTERS = (
    "txid_mismatches", "short_reads", "connection_errors",
    "malformed_headers", "unit_id_mismatches", "write_echo_mismatches",
    "stale_replies",
)
CONTROL_COUNTERS = (
    "setpoint_reasserts", "setpoint_drift_events", "heartbeat_write_failures",
    "stop_pending", "stop_write_failures", "stop_retries",
)


def block_ok(observation: dict, block: str) -> bool:
    """Whether ``block`` was freshly acquired in ``observation`` (the
    snapshot reports this as ``<block>_block_ok``)."""
    return observation.get(f"{block}_block_ok") is True


class FoxESSChargerCoordinator(DataUpdateCoordinator):
    """Thin polling adapter over the charging controller."""

    def __init__(self, hass: HomeAssistant, controller: "ControllerLike",
                 scan_interval: int, *, storage: "ChargerStorage | None" = None,
                 entry_id: str | None = None) -> None:
        self.controller = controller
        self.entry_id = entry_id
        self._storage = storage
        self.energy = EnergyTracker()
        self.sessions = SessionTracker(on_complete=self._fire_session_completed)
        self._status_failures = 0
        self._wrong_work_mode: int | None = None  # unsupported mode currently reported
        self._block_last_success: dict[str, float] = {}
        self._block_success_count: dict[str, int] = {}
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=scan_interval),
        )

    # ── restore/persist ──────────────────────────────────────────────────

    def restore(self, session: dict, energy: dict[str, dict]) -> None:
        self.sessions.restore(session)
        self.energy.restore(energy)

    async def _async_persist(self, *, force: bool = False) -> None:
        if self._storage is None:
            return
        if force or self.sessions.dirty:
            try:
                await self._storage.async_save_session(
                    self.sessions.export(), rearm_required=self.sessions.rearm_required,
                )
                self.sessions.dirty = False
            except Exception:  # noqa: BLE001 - stays dirty, retried next poll
                _LOGGER.exception("Could not persist FoxESS session state")
        if force or self.energy.dirty:
            try:
                await self._storage.async_save_energy(self.energy.export())
                self.energy.dirty = False
            except Exception:  # noqa: BLE001 - stays dirty, retried next poll
                _LOGGER.exception("Could not persist FoxESS energy baseline")

    async def async_flush(self) -> None:
        """Persist everything now (used on unload)."""
        await self._async_persist(force=True)

    # ── polling ──────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        try:
            observation = await self.controller.async_poll()
        except Exception as err:  # noqa: BLE001 - the status block is mandatory
            _LOGGER.warning("FoxESS poll failed: %s", err)
            observation = {}
        if not isinstance(observation, dict):
            observation = {}

        previous = self.data or {}
        data = dict(observation)
        fresh = {block: block_ok(observation, block) for block in BLOCK_KEYS}
        now = time.monotonic()
        for block, ok in fresh.items():
            if ok:
                self._block_last_success[block] = now
                self._block_success_count[block] = self._block_success_count.get(block, 0) + 1
            else:
                for key in BLOCK_KEYS[block]:
                    if key in previous:
                        data[key] = previous[key]
                    else:
                        data.pop(key, None)
        for key in ("id_model_code", "id_serial_number"):
            if not data.get(key) and previous.get(key):
                data[key] = previous[key]

        if fresh[BLOCK_STATUS]:
            self._status_failures = 0
            self.energy.apply(data, session_boundary=self.sessions.is_boundary(data))
            self.sessions.update(data, intent_enabled=self._intent_enabled())
        else:
            self._status_failures += 1
            _LOGGER.warning(
                "Could not read FoxESS status block (%d consecutive)", self._status_failures,
            )
            if self.data is None or self._status_failures >= MAX_STATUS_READ_FAILURES:
                raise UpdateFailed(
                    f"Charger unreachable: status block failed {self._status_failures} "
                    "polls in a row"
                )
            data["session_active"] = self.sessions.active
        if self.sessions.last_session is not None:
            data["last_session"] = self.sessions.last_session

        if "active_faults" not in observation:
            data["active_faults"] = decode_bitmask(data.get("fault_code"), FAULT_BITS, "fault_code")
        if "active_alarms" not in observation:
            data["active_alarms"] = decode_bitmask(data.get("alarm_code"), ALARM_BITS, "alarm_code")
        self._add_controller_view(data)
        if fresh[BLOCK_CONFIG]:
            self._sync_work_mode_issue(data.get("work_mode"))
        await self._async_persist()
        return data

    @property
    def _work_mode_issue_id(self) -> str:
        return f"wrong_work_mode_{self.entry_id}"

    def _sync_work_mode_issue(self, work_mode) -> None:
        if work_mode not in WORK_MODE_MAP:
            return
        wrong = work_mode if work_mode != 1 else None
        if wrong == self._wrong_work_mode:
            return
        self._wrong_work_mode = wrong
        if wrong is not None:
            ir.async_create_issue(
                self.hass, DOMAIN, self._work_mode_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="wrong_work_mode",
                translation_placeholders={"mode": WORK_MODE_MAP[work_mode]},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._work_mode_issue_id)

    def async_clear_work_mode_issue(self) -> None:
        self._wrong_work_mode = None
        ir.async_delete_issue(self.hass, DOMAIN, self._work_mode_issue_id)

    def _intent_enabled(self) -> bool | None:
        value = getattr(self.controller, "intent_enabled", None)
        return value if isinstance(value, bool) else None

    def _add_controller_view(self, data: dict) -> None:
        diag = getattr(self.controller, "diagnostics", None) or {}
        for name in TRANSPORT_COUNTERS:
            data[f"diag_{name}"] = diag.get(name, 0)
        for name in CONTROL_COUNTERS:
            if name in diag:
                data[f"diag_{name}"] = diag[name]
        data["diag_energy_rejections"] = len(self.energy.rejections)
        data["energy_rejection_log"] = self.energy.rejections[-5:]
        data["intent_enabled"] = self._intent_enabled()
        data["control_phase"] = getattr(self.controller, "phase", None)
        data["desired_power_raw"] = getattr(self.controller, "desired_power_raw", None)
        data["desired_current_raw"] = getattr(self.controller, "desired_current_raw", None)

    # ── user pause boundary ──────────────────────────────────────────────

    async def async_user_paused(self) -> None:
        """A user pause was confirmed: end the logical session (once)."""
        data = dict(self.data or {})
        if self.sessions.end_for_user_pause(data):
            await self._async_persist()
            self.async_set_updated_data(data)

    def _fire_session_completed(self, record: dict) -> None:
        if self.entry_id is not None:
            self.hass.bus.async_fire(
                EVENT_SESSION_COMPLETED, {"entry_id": self.entry_id, **record},
            )

    # ── block freshness ──────────────────────────────────────────────────

    def block_success_count(self, block: str) -> int:
        return self._block_success_count.get(block, 0)

    def block_is_fresh(self, block: str | None) -> bool:
        """Whether ``block``'s last successful read is within
        BLOCK_STALENESS_FACTOR scan intervals. ``None`` is always fresh; a
        block that never succeeded is never fresh."""
        if block is None:
            return True
        last = self._block_last_success.get(block)
        if last is None:
            return False
        threshold = self.update_interval.total_seconds() * BLOCK_STALENESS_FACTOR
        return (time.monotonic() - last) <= threshold

    def block_health_snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        return {
            block: {
                "seconds_since_last_success": (
                    round(now - self._block_last_success[block], 1)
                    if block in self._block_last_success else None
                ),
                "fresh": self.block_is_fresh(block),
            }
            for block in (BLOCK_STATUS, BLOCK_CONFIG, BLOCK_PHASE_BOX)
        }
