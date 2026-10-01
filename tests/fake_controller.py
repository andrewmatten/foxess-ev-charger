"""Fake ChargingController implementing the public API from CONTRACTS.md C.

Used to test the HA adapters in isolation from the real controller. It
records every call, returns CommandResult-shaped objects, raises
``ControlError`` on demand and produces observations in the snapshot
format of protocol.async_read_snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass

from custom_components.foxess_charger.adapter_api import ControlError
from custom_components.foxess_charger.const import (
    REG_ALLOWED_CHARGE_ENERGY, REG_ALLOWED_CHARGE_TIME, REG_AUTO_PHASE_SWITCH,
    REG_DEFAULT_CURRENT, REG_LOCK_CONTROL, REG_MIN_SWITCH_INTERVAL,
    REG_PHASE_SWITCHING, REG_TIME_VALIDITY, REG_WORK_MODE,
)

REGISTER_KEYS = {
    REG_WORK_MODE: "work_mode",
    REG_ALLOWED_CHARGE_TIME: "allowed_charge_time",
    REG_ALLOWED_CHARGE_ENERGY: "allowed_charge_energy",
    REG_TIME_VALIDITY: "time_validity",
    REG_DEFAULT_CURRENT: "default_current_raw",
    REG_AUTO_PHASE_SWITCH: "auto_phase_switch",
    REG_MIN_SWITCH_INTERVAL: "min_switch_interval",
    REG_PHASE_SWITCHING: "phase_sequence",
    REG_LOCK_CONTROL: None,
}


@dataclass(frozen=True)
class CommandResult:
    outcome: str
    revision: int
    observed: int | None = None


class FakeController:
    def __init__(self, *, status: int = 1, cc_status: int = 1) -> None:
        self.persist = None
        self.calls: list[tuple] = []
        self.fail_next: str | None = None   # method name to fail once
        self.fail_poll = 0                   # polls to fail
        self.fail_initialize = False
        self.closed = False
        self.started = False
        self.saved = "unset"
        self.revision = 0
        self._enabled = True
        self._power: int | None = None
        self._current: int | None = None
        self.safety_latched = False
        self.stop_fallback = False
        self.phase = "idle"
        self.diagnostics = {"txid_mismatches": 0, "short_reads": 0}
        self.config_ok = True
        self.phase_box: dict | None = None   # phase-switch box absent by default
        self.hw = {
            "device_address": 1, "software_version": 100, "stop_reason": 0,
            "status": status, "cp_status": 3, "cc_status": cc_status,
            "port_temp_raw": 750, "ambient_temp_raw": 750,
            "l1_voltage_raw": 2400, "l2_voltage_raw": 0, "l3_voltage_raw": 0,
            "l1_current_raw": 0, "l2_current_raw": 0, "l3_current_raw": 0,
            "power_raw": 0, "lock_status": 0, "phase_sequence": 0,
            "max_power_raw": 73, "min_power_raw": 14, "max_current_raw": 320,
            "min_current_raw": 60, "alarm_code": 0, "total_energy_raw": 1000,
            "current_energy_raw": 0, "fault_code": 0, "rfid_card": 0,
            "work_mode": 0, "max_charging_current_raw": 320,
            "max_charging_power_raw": 73, "allowed_charge_time": 0xFFFF,
            "allowed_charge_energy": 0xFFFF, "time_validity": 60,
            "default_current_raw": 60,
        }
        self.data: dict = {}

    # ── state record ────────────────────────────────────────────────────
    @property
    def intent_enabled(self) -> bool:
        return self._enabled

    @property
    def desired_power_raw(self):
        return self._power

    @property
    def desired_current_raw(self):
        return self._current

    def export_state(self) -> dict:
        return {
            "schema": 1, "enabled": self._enabled, "power_raw": self._power,
            "current_raw": self._current, "revision": self.revision,
            "safety_latched": self.safety_latched, "stop_fallback": self.stop_fallback,
        }

    async def _persist(self) -> None:
        if self.persist is not None:
            await self.persist(self.export_state())

    def _maybe_fail(self, name: str) -> None:
        if self.fail_next == name:
            self.fail_next = None
            raise ControlError(f"{name} failed (fake)")

    # ── lifecycle ───────────────────────────────────────────────────────
    async def async_initialize(self, saved=None, *, configure_safety=True):
        self.calls.append(("initialize", saved))
        self.saved = saved
        if self.fail_initialize:
            raise ControlError("initialize failed (fake)")
        if saved is None:
            self._enabled = True
        else:
            self._enabled = saved["enabled"]
            self._power = saved["power_raw"]
            self._current = saved["current_raw"]
            self.revision = saved["revision"]
            self.safety_latched = saved["safety_latched"]
            self.stop_fallback = saved["stop_fallback"]

    async def async_start(self):
        self.started = True

    async def async_close(self):
        self.closed = True

    async def async_poll(self) -> dict:
        self.calls.append(("poll",))
        if self.fail_poll:
            self.fail_poll -= 1
            raise ControlError("status block unreadable (fake)")
        snap = dict(self.hw)
        snap["status_valid"] = snap["status"] in {0, 1, 2, 3, 4, 5, 6, 8, 9}
        snap["cc_status_valid"] = snap["cc_status"] in {0, 1}
        if not snap["status_valid"]:
            snap["status"] = None
        snap["status_block_ok"] = True
        snap["config_block_ok"] = self.config_ok
        if not self.config_ok:
            for key in ("work_mode", "max_charging_current_raw", "max_charging_power_raw",
                        "allowed_charge_time", "allowed_charge_energy", "time_validity",
                        "default_current_raw"):
                snap.pop(key)
        if self.phase_box is None:
            snap["phase_box_block_ok"] = False
        else:
            snap.update(self.phase_box)
            snap["phase_box_block_ok"] = True
        snap["id_model_code"] = "A7300P1-E-B-WO"
        snap["id_serial_number"] = "SERIAL"
        self.data = snap
        return dict(snap)

    # ── commands ────────────────────────────────────────────────────────
    def _connected_active(self) -> bool:
        return self.hw["cc_status"] == 1

    async def async_set_power(self, raw: int):
        self.calls.append(("set_power", raw))
        self._maybe_fail("set_power")
        self.revision += 1
        self._power = raw
        await self._persist()
        if not self._enabled:
            return CommandResult("staged", self.revision, self.hw["max_charging_power_raw"])
        self.hw["max_charging_power_raw"] = raw
        return CommandResult("confirmed", self.revision, raw)

    async def async_set_current(self, raw: int):
        self.calls.append(("set_current", raw))
        self._maybe_fail("set_current")
        self.revision += 1
        self._current = raw
        await self._persist()
        if not self._enabled:
            return CommandResult("staged", self.revision, self.hw["max_charging_current_raw"])
        self.hw["max_charging_current_raw"] = raw
        return CommandResult("confirmed", self.revision, raw)

    async def async_enable(self):
        self.calls.append(("enable",))
        self._maybe_fail("enable")
        if not self._connected_active():
            raise ControlError("no vehicle connected (fake)")
        self.revision += 1
        self._enabled = True
        await self._persist()
        self.hw["status"] = 3
        self.hw["power_raw"] = self._power if self._power is not None else 73
        return CommandResult("confirmed", self.revision, self.hw["power_raw"])

    async def async_pause(self):
        self.calls.append(("pause",))
        self.revision += 1
        self._enabled = False
        await self._persist()
        self._maybe_fail("pause")
        self.hw["status"] = 4 if self.hw["status"] in (2, 3, 4, 9) else self.hw["status"]
        self.hw["power_raw"] = 0
        return CommandResult("confirmed", self.revision, 0)

    async def async_set_register(self, address: int, value: int):
        self.calls.append(("set_register", address, value))
        self._maybe_fail("set_register")
        if address not in REGISTER_KEYS:
            raise ControlError(f"register 0x{address:04X} not allowlisted (fake)")
        key = REGISTER_KEYS[address]
        if address == REG_LOCK_CONTROL:
            self.hw["lock_status"] = 1 if value == 2 else 0
        elif self.phase_box is not None and key in self.phase_box:
            self.phase_box[key] = value
        elif key is not None:
            self.hw[key] = value
        self.revision += 1
        return CommandResult("confirmed", self.revision, value)
