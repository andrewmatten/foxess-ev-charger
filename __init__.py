"""FoxESS EV Charger integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, PLATFORMS,
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID,
    DEFAULT_SCAN_INTERVAL,
    FAULT_BITS, ALARM_BITS, decode_bitmask,
    REG_TOTAL_ENERGY, REG_CURRENT_ENERGY, REG_FAULT_CODE, REG_RFID_CARD,
    REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER, ACTIVE_CHARGING_STATUSES,
)
from .modbus_client import FoxESSModbusClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "A7300P1-E-B-WO"

# Registers the charger's own "Command Time Validity" timeout (0x3005, §2.34
# in the FoxESS Modbus spec) applies to. Per the protocol, the charger reverts
# these to its device maximum if neither is rewritten within that window -
# mirrors evcc's foxess-evc driver, which re-asserts the same register
# (0x3002) on a heartbeat for the same reason (see evcc-io/evcc discussion
# #26218 and charger/foxess-evc.go).
HEARTBEAT_REGISTERS: tuple[tuple[str, int], ...] = (
    ("max_charging_current_raw", REG_MAX_CHARGING_CURRENT),
    ("max_charging_power_raw",   REG_MAX_CHARGING_POWER),
)

# Half the device's own Command Time Validity window, same margin evcc uses
# (heartbeat interval = timeValidity / 2). Clamped so a misread/zero value
# can't produce a zero or negative sleep.
MIN_HEARTBEAT_INTERVAL = 5      # seconds - matches the register's own minimum
DEFAULT_TIME_VALIDITY  = 60     # seconds - used until the first read succeeds


def build_device_info(entry: ConfigEntry, coordinator: "FoxESSChargerCoordinator") -> DeviceInfo:
    """Builds DeviceInfo with the model read from the charger (0x101E) when
    available, falling back to the default single-phase model string only
    if the device hasn't answered yet. Single source of truth instead of
    the same literal repeated in every platform file."""
    model = (coordinator.data or {}).get("id_model_code") or DEFAULT_MODEL
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FoxESS Charger",
        manufacturer="FoxESS",
        model=model,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host      = entry.data[CONF_HOST]
    port      = entry.data[CONF_PORT]
    slave_id  = entry.data[CONF_SLAVE_ID]
    scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)

    client      = FoxESSModbusClient(host, port, slave_id)
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval)

    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_heartbeat()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client":      client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        # Stop the heartbeat before dropping the connection, so it can't fire
        # a write against a socket that's about to be closed out from under it.
        await data["coordinator"].async_stop_heartbeat()
        await hass.async_add_executor_job(data["client"].disconnect)
    return unload_ok


class FoxESSChargerCoordinator(DataUpdateCoordinator):
    """Coordinator: pollt alle Modbus-Register des Chargers."""

    def __init__(self, hass: HomeAssistant, client: FoxESSModbusClient,
                 scan_interval: int) -> None:
        self.client = client
        self._heartbeat_task: asyncio.Task | None = None
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    # ── Heartbeat: re-assert the charge-limit registers ──────────────────────
    # See HEARTBEAT_REGISTERS above for why this exists. Mirrors evcc's
    # foxess-evc driver (charger/foxess-evc.go: heartbeat()), which runs the
    # same re-assert loop at half the device's Command Time Validity window.

    @property
    def _heartbeat_interval(self) -> float:
        """Half the device's own Command Time Validity (0x3005), like evcc."""
        time_validity = (self.data or {}).get("time_validity") or DEFAULT_TIME_VALIDITY
        return max(MIN_HEARTBEAT_INTERVAL, time_validity / 2)

    def async_start_heartbeat(self) -> None:
        """Start the background heartbeat task. Call once after first refresh."""
        if self._heartbeat_task is None:
            self._heartbeat_task = self.hass.loop.create_task(
                self._heartbeat_loop(), name=f"{DOMAIN}_heartbeat"
            )

    async def async_stop_heartbeat(self) -> None:
        """Cancel the heartbeat task, if running, and wait for it to exit."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._heartbeat_interval)
            except asyncio.CancelledError:
                return
            await self._async_heartbeat_tick()

    async def _async_heartbeat_tick(self) -> None:
        """Gate, then re-assert. Called only on the heartbeat's own schedule.

        Gated on ACTIVE_CHARGING_STATUSES: on FoxESS firmware, writing a
        nonzero max-power/current register is itself an implicit "resume
        charging" command, not a passive limit update. Re-asserting it while
        the Charging switch has stopped the session (status "finished") would
        silently restart charging out from under the user every heartbeat
        tick - which is exactly what re-asserting unconditionally used to do.
        """
        data = self.data or {}
        if data.get("status") not in ACTIVE_CHARGING_STATUSES:
            return
        await self.async_reassert_charge_limits()

    async def async_reassert_charge_limits(self) -> None:
        """Write the last known value of each heartbeat register, right now.

        Uses whatever is already cached in self.data - the same value the
        coordinator's own poll last read back, or that a number entity's
        optimistic update last set - so this never invents a value of its
        own. If neither register has a cached value yet (e.g. a fresh
        install with no prior session), there's nothing to push and this is
        a no-op.

        Unlike _async_heartbeat_tick, this is NOT gated on session status -
        it's the shared "push both registers" primitive, called either by
        the heartbeat (after it has already checked status) or directly by
        FoxESSChargingSwitch.async_turn_on, which calls this immediately on
        turning charging on rather than waiting up to time_validity/2 seconds
        for the next heartbeat tick to apply the currently-configured limit.
        """
        data = self.data or {}
        writes = [
            (register, data[data_key])
            for data_key, register in HEARTBEAT_REGISTERS
            if data.get(data_key) is not None
        ]
        if not writes:
            return

        def _write_all() -> None:
            for register, value in writes:
                if not self.client.write_holding_register(register, value):
                    _LOGGER.warning(
                        "FoxESS: failed to assert 0x%04X=%d",
                        register, value,
                    )

        try:
            await self.hass.async_add_executor_job(_write_all)
        except Exception as err:  # noqa: BLE001 - never let a caller crash on this
            _LOGGER.error("FoxESS: %s", err)

    async def _async_update_data(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self._fetch)
        except Exception as err:
            raise UpdateFailed(f"Modbus error: {err}") from err

    def _fetch(self) -> dict:
        # Start from the last known-good values instead of a blank dict, so a
        # single failed register-block read doesn't wipe out everything else
        # that's still valid (e.g. right after a write, before the charger's
        # ready to answer the follow-up read).
        data: dict = dict(self.data) if self.data else {}

        # ── 0x1000–0x1008: 9 core status registers (all hardware) ───────────
        # L2/L3 voltage/current (0x1009/0x100A/0x100C/0x100D) used to be read
        # in the same single 22-register request as everything else here.
        # On single-phase hardware those four registers don't exist, and
        # Modbus TCP fails the ENTIRE request the moment it touches even one
        # invalid address in the range - so the other 18 perfectly valid
        # registers (status, cp_status, cc_status, temperatures, L1 readings,
        # lock status, etc.) went dark too, every single poll cycle. This is
        # the exact same failure class already fixed once below for the
        # phase-switch-box registers (0x300A/0x300B) - just never applied to
        # this block. Splitting around the four three-phase-only registers
        # isolates them the same way.
        regs = self.client.read_registers(0x1000, 9)
        if regs and len(regs) >= 9:
            data["device_address"]  = regs[0]
            data["software_version"]= regs[1]
            data["stop_reason"]     = regs[2]
            data["status"]          = regs[3]
            data["cp_status"]       = regs[4]
            data["cc_status"]       = regs[5]
            data["port_temp_raw"]   = regs[6]
            data["ambient_temp_raw"]= regs[7]
            data["l1_voltage_raw"]  = regs[8]
        else:
            _LOGGER.warning("Could not read status registers 0x1000–0x1008")

        # ── 0x1009–0x100A: L2/L3 voltage (three-phase hardware only) ────────
        l23v = self.client.read_registers(0x1009, 2, quiet=True)
        if l23v and len(l23v) >= 2:
            data["l2_voltage_raw"] = l23v[0]
            data["l3_voltage_raw"] = l23v[1]
        else:
            _LOGGER.debug(
                "Could not read L2/L3 voltage registers 0x1009–0x100A "
                "(expected on single-phase hardware)"
            )

        # ── 0x100B: L1 current (all hardware) ────────────────────────────────
        # Its own single-register read because it sits directly between the
        # L2/L3 voltage and L2/L3 current registers above/below - there's no
        # contiguous span that includes it but excludes both phase-specific
        # pairs.
        l1c = self.client.read_registers(0x100B, 1)
        if l1c and len(l1c) >= 1:
            data["l1_current_raw"] = l1c[0]
        else:
            _LOGGER.warning("Could not read L1 current register 0x100B")

        # ── 0x100C–0x100D: L2/L3 current (three-phase hardware only) ────────
        l23c = self.client.read_registers(0x100C, 2, quiet=True)
        if l23c and len(l23c) >= 2:
            data["l2_current_raw"] = l23c[0]
            data["l3_current_raw"] = l23c[1]
        else:
            _LOGGER.debug(
                "Could not read L2/L3 current registers 0x100C–0x100D "
                "(expected on single-phase hardware)"
            )

        # ── 0x100E–0x1015: 8 remaining core status registers (all hardware) ──
        regs2 = self.client.read_registers(0x100E, 8)
        if regs2 and len(regs2) >= 8:
            data["power_raw"]       = regs2[0]
            data["lock_status"]     = regs2[1]
            data["phase_sequence"]  = regs2[2]
            data["max_power_raw"]   = regs2[3]
            data["min_power_raw"]   = regs2[4]
            data["max_current_raw"] = regs2[5]
            data["min_current_raw"] = regs2[6]
            data["alarm_code"]      = regs2[7]
        else:
            _LOGGER.warning("Could not read status registers 0x100E–0x1015")

        # ── 0x1016/0x1018/0x101A/0x101C: UINT32 Register ─────────────────────
        # Addresses come from const.py rather than literals here - these were
        # duplicated as magic numbers until 2.1.2, so fixing the swapped energy
        # registers in const.py alone had no effect on what was actually read.
        for key, addr in [
            ("total_energy_raw",   REG_TOTAL_ENERGY),
            ("current_energy_raw", REG_CURRENT_ENERGY),
            ("fault_code",         REG_FAULT_CODE),
            ("rfid_card",          REG_RFID_CARD),
        ]:
            val = self.client.read_uint32(addr)
            if val is not None:
                data[key] = val

        # ── 0x3000–0x3006: R/W Config Register (single-phase-safe core block) ──
        # Split from the phase-switch-box block below because a single failed
        # register in one read fails the *entire* Modbus request (Illegal Data
        # Address) - on single-phase hardware (e.g. A7300P1-E-B-WO), 0x300A/
        # 0x300B (phase-switch-box only) don't exist in firmware, which was
        # taking down this whole block - including work_mode, max charging
        # current/power, allowed charge time/energy, and time validity - even
        # though those registers are all readable on their own.
        cfg = self.client.read_registers(0x3000, 7)
        if cfg and len(cfg) >= 7:
            data["work_mode"] = cfg[0]

            # Only trust the device's max-power/current registers while a
            # session is actually active - see the block comment above. But
            # a fetch that has nothing cached yet (a fresh coordinator, or
            # right after startup before any prior value exists) still
            # needs *something* to show rather than leaving these keys
            # permanently unset, so the very first population always takes
            # the device's value regardless of status; RestoreEntity (in
            # number.py) corrects it afterwards if a pre-restart value
            # should take precedence instead.
            active = data.get("status") in ACTIVE_CHARGING_STATUSES
            if active or "max_charging_current_raw" not in data:
                data["max_charging_current_raw"] = cfg[1]
            if active or "max_charging_power_raw" not in data:
                data["max_charging_power_raw"] = cfg[2]

            data["allowed_charge_time"]      = cfg[3]
            data["allowed_charge_energy"]    = cfg[4]
            data["time_validity"]            = cfg[5]
            data["default_current_raw"]      = cfg[6]
        else:
            _LOGGER.warning("Could not read config registers 0x3000–0x3006")

        # ── 0x300A–0x300B: Phase-Switch-Box Register (three-phase only) ────────
        # Expected to fail on single-phase hardware where these registers
        # aren't implemented - that's fine, it's independent of the read above.
        phase_cfg = self.client.read_registers(0x300A, 2, quiet=True)
        if phase_cfg and len(phase_cfg) >= 2:
            data["auto_phase_switch"]   = phase_cfg[0]
            data["min_switch_interval"] = phase_cfg[1]
        else:
            _LOGGER.debug("Could not read phase-switch-box registers 0x300A–0x300B (expected on single-phase hardware)")

        # ── 0x101E/0x1022: Id Model Code / Id Serial Number (ASCII, static) ────
        # Read once and cached forever via the seed-from-last-known-good
        # pattern above - these don't change, no need to re-poll every cycle.
        if not data.get("id_model_code"):
            model = self.client.read_ascii(0x101E, 4)
            if model:
                data["id_model_code"] = model
        if not data.get("id_serial_number"):
            serial = self.client.read_ascii(0x1022, 16)
            if serial:
                data["id_serial_number"] = serial

        # ── Fault/Alarm bitmask decode ──────────────────────────────────────────
        # fault_code/alarm_code are bitmasks (Appendix 2/3) - multiple
        # conditions can be active at once. Decode into readable name lists
        # for the sensors' extra_state_attributes instead of a raw integer.
        data["active_faults"] = decode_bitmask(data.get("fault_code"), FAULT_BITS)
        data["active_alarms"] = decode_bitmask(data.get("alarm_code"), ALARM_BITS)

        return data