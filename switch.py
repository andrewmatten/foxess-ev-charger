"""Switch entities for FoxESS EV Charger."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, REG_LOCK_CONTROL, REG_AUTO_PHASE_SWITCH,
    BLOCK_STATUS, BLOCK_PHASE_BOX, SESSION_ACTIVE_STATUSES,
)
from .__init__ import FoxESSChargerCoordinator, FoxESSBlockAvailabilityMixin, build_device_info
from .modbus_client import FoxESSModbusClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    d = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        FoxESSChargingSwitch(d["coordinator"], d["client"], entry),
        FoxESSLockSwitch(d["coordinator"], d["client"], entry),
        FoxESSAutoPhaseSwitchSwitch(d["coordinator"], d["client"], entry),
    ])


async def _write_or_raise(
    hass: HomeAssistant, client: FoxESSModbusClient,
    register: int, value: int, action_desc: str,
) -> None:
    """Writes a register or raises HomeAssistantError on failure.

    A failed write used to be a silent no-op (log an error, return) - HA had
    no way to surface it, so the UI showed the toggle flip back with no
    explanation. Raising here lets HA report the failure to the user and in
    the logbook, same as any other failed service call.
    """
    success = await hass.async_add_executor_job(client.write_holding_register, register, value)
    if not success:
        raise HomeAssistantError(
            f"FoxESS: failed to {action_desc} (write to 0x{register:04X} failed)"
        )


async def _refresh_and_verify(
    coordinator: FoxESSChargerCoordinator, check_ok, mismatch_msg: str,
    *, fresh_block: str | None = None, previous_success_count: int | None = None,
) -> bool:
    """Requests a coordinator refresh and confirms the charger actually
    applied the write, instead of trusting the optimistic patch alone.

    The write's own success just means the charger *acknowledged* the
    command over Modbus - not that it necessarily took effect. If the
    read-back after a refresh doesn't match, that's a real, worth-surfacing
    failure mode (the charger accepted the write but didn't apply it), not
    something to retry automatically - just logged clearly, per the agreed
    scope for this.
    """
    await asyncio.sleep(1.5)
    await coordinator.async_request_refresh()
    confirmed = check_ok(coordinator.data or {})
    if fresh_block is not None and previous_success_count is not None:
        confirmed = confirmed and (
            coordinator.block_success_count(fresh_block) > previous_success_count
        )
    if not confirmed:
        _LOGGER.warning(mismatch_msg)
    return confirmed


class FoxESSChargingSwitch(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:ev-plug-type2"
    _block = BLOCK_STATUS  # is_on reads "status" from the status batch
    _attr_translation_key = "charging"

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 client: FoxESSModbusClient, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._client      = client
        self._attr_unique_id   = f"{entry.entry_id}_charging"
        self._attr_name        = "Charging"
        self._attr_device_info = build_device_info(entry, coordinator)

    @property
    def is_on(self) -> bool:
        # 2=start, 3=charging, 4=pause (suspended by the car, not by a stop
        # command - the session is still active and will resume on its own,
        # so it should read as "on" rather than looking identical to stopped).
        # SESSION_ACTIVE_STATUSES (const.py) is the same set the coordinator's
        # background heartbeat gates on - one shared definition so the two
        # can't drift apart on what "active" means.
        return (self.coordinator.data or {}).get("status") in SESSION_ACTIVE_STATUSES

    async def async_turn_on(self, **kwargs) -> None:
        # async_send_start sets _charging_desired=True inside its locked
        # section. Stop inhibition/pending clears only after status confirms
        # an active session below, not from the command acknowledgement.
        # Moving the desired-state change inside the lock avoids a race
        # with Stop: doing it here, after the lock was released, could
        # concurrent stop's flag-clear (which runs before that stop even
        # tries to acquire the lock) and could silently stomp it back to
        # True.
        #
        # Routed through the coordinator's async_send_start (rather than a
        # direct write here) so this shares the same command lock as the
        # heartbeat's setpoint pushes and the stop path - see
        # _async_send_setpoint's docstring in __init__.py for why a single
        # shared lock across all of start/stop/heartbeat is what actually
        # closes the race this integration used to have.
        start_generation = self.coordinator._charging_generation
        self.coordinator._start_confirmation_pending = True
        success = await self.coordinator.async_send_start()
        if not success:
            self.coordinator._start_confirmation_pending = False
            self.coordinator._wake_heartbeat()
            raise HomeAssistantError(
                "FoxESS: failed to start charging (write to 0x4001 failed)"
            )
        self.coordinator.data["status"] = 3
        self.async_write_ha_state()
        # Push the current desired setpoints immediately rather than waiting
        # for the heartbeat's next scheduled tick - direct set() is safe
        # here, this method runs on the event loop (see
        # FoxESSChargerCoordinator._wake_heartbeat in __init__.py).
        self.coordinator._wake_heartbeat()
        status_reads_before_refresh = self.coordinator.block_success_count(BLOCK_STATUS)
        start_confirmed = False
        try:
            start_confirmed = await _refresh_and_verify(
                self.coordinator,
                lambda data: data.get("status") in SESSION_ACTIVE_STATUSES,
                "FoxESS: sent start-charging command but charger status did not "
                "reflect an active session after refresh",
                fresh_block=BLOCK_STATUS,
                previous_success_count=status_reads_before_refresh,
            )
        finally:
            # Stop can run while the refresh sleeps. Its generation bump is
            # authoritative: a successful Start read-back must not undo that
            # newer request by clearing stop-pending or re-arming heartbeat.
            self.coordinator._start_confirmation_pending = False
            if (
                start_confirmed
                and self.coordinator._charging_generation == start_generation
            ):
                self.coordinator._stop_pending = False
                self.coordinator._stop_inhibit = False
                self.coordinator._charging_desired = True
                self.coordinator._session_state_dirty = True
                try:
                    await self.coordinator.async_flush_session_state()
                except Exception:
                    _LOGGER.exception("Could not persist confirmed FoxESS Start")
            self.coordinator._wake_heartbeat()

    async def async_turn_off(self, **kwargs) -> None:
        # Must be the very first two statements, before the stop write is even
        # sent - see _async_send_setpoint's docstring (__init__.py) for why
        # this ordering (clear desired + bump generation BEFORE acquiring the
        # command lock) is what lets a queued heartbeat write correctly see
        # "stopped" the instant it gets the lock, rather than racing on stale
        # state. Writing 0x3001/0x3002 is itself an implicit "resume
        # charging" per this firmware's documented behavior - an unlucky
        # race could otherwise silently restart the very session the user
        # just told it to stop. This ordering must NOT change.
        self.coordinator._charging_desired = False
        self.coordinator._charging_generation += 1
        # See _stop_inhibit's own comment (__init__.py, near __init__) for
        # the 2026-09-18 incident this exists to prevent: while this is
        # True, an unexpected active status (e.g. this firmware's
        # implicit-resume-on-setpoint-write behavior) is not treated as a
        # legitimate Plug & Charge start, so the heartbeat won't sustain it.
        self.coordinator._stop_inhibit = True
        self.coordinator._stop_pending = True
        self.coordinator._session_state_dirty = True
        self.coordinator._heartbeat_retry_pending = True
        try:
            await self.coordinator.async_flush_session_state()
        except Exception:
            _LOGGER.exception(
                "Could not persist pending FoxESS Stop; retry remains active in memory"
            )
        success = await self.coordinator.async_send_stop()
        if not success:
            self.coordinator.stop_write_failures += 1
            # Leave the stop pending and retry it from the heartbeat. The
            # cap is deliberately not refreshed while Stop is unresolved:
            # this firmware can resume on a setpoint write.
            self.coordinator._wake_heartbeat()
            raise HomeAssistantError(
                "FoxESS: failed to stop charging (write to 0x4001 failed)"
            )
        self.coordinator._wake_heartbeat()
        await _refresh_and_verify(
            self.coordinator,
            lambda data: data.get("status") not in SESSION_ACTIVE_STATUSES,
            "FoxESS: sent stop-charging command but charger status still "
            "shows an active session after refresh",
        )


class FoxESSLockSwitch(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:lock"
    _block = BLOCK_STATUS  # is_on reads "lock_status" from the status batch
    _attr_translation_key = "lock"

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 client: FoxESSModbusClient, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._client      = client
        self._attr_unique_id   = f"{entry.entry_id}_lock"
        self._attr_name        = "Lock"
        self._attr_device_info = build_device_info(entry, coordinator)

    @property
    def is_on(self) -> bool:
        # 0x100F: 0 = unlocked, 1 = locked (simple 2-value enum per spec)
        val = (self.coordinator.data or {}).get("lock_status")
        return val not in (None, 0)

    async def async_turn_on(self, **kwargs) -> None:
        _LOGGER.debug("FoxESS Lock: send Lock (REG_LOCK_CONTROL=2)")
        await _write_or_raise(self.hass, self._client, REG_LOCK_CONTROL, 2, "lock the connector")
        # Kein optimistisches Update – echter Wert vom Gerät abwarten
        await _refresh_and_verify(
            self.coordinator,
            lambda data: data.get("lock_status") not in (None, 0),
            "FoxESS: sent lock command but lock_status still reads unlocked "
            "after refresh",
        )

    async def async_turn_off(self, **kwargs) -> None:
        _LOGGER.debug("FoxESS Lock: send Unlock (REG_LOCK_CONTROL=1)")
        await _write_or_raise(self.hass, self._client, REG_LOCK_CONTROL, 1, "unlock the connector")
        # Kein optimistisches Update – echter Wert vom Gerät abwarten
        await _refresh_and_verify(
            self.coordinator,
            lambda data: data.get("lock_status") in (None, 0),
            "FoxESS: sent unlock command but lock_status still reads locked "
            "after refresh",
        )


class FoxESSAutoPhaseSwitchSwitch(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:auto-fix"
    # Phase switching requires an external phase-switch-box accessory, which
    # single-phase A7300P1-E-B-WO hardware does not have.
    _attr_entity_registry_enabled_default = False
    _block = BLOCK_PHASE_BOX
    _attr_translation_key = "auto_phase_switch"

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 client: FoxESSModbusClient, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._client      = client
        self._attr_unique_id   = f"{entry.entry_id}_auto_phase_switch"
        self._attr_name        = "Auto Phase Switch"
        self._attr_device_info = build_device_info(entry, coordinator)

    @property
    def is_on(self) -> bool:
        return (self.coordinator.data or {}).get("auto_phase_switch") == 1

    async def async_turn_on(self, **kwargs) -> None:
        await _write_or_raise(
            self.hass, self._client, REG_AUTO_PHASE_SWITCH, 1, "enable auto phase switch"
        )
        self.coordinator.data["auto_phase_switch"] = 1
        self.async_write_ha_state()
        await _refresh_and_verify(
            self.coordinator,
            lambda data: data.get("auto_phase_switch") == 1,
            "FoxESS: enabled auto phase switch but read-back still shows "
            "disabled after refresh",
        )

    async def async_turn_off(self, **kwargs) -> None:
        await _write_or_raise(
            self.hass, self._client, REG_AUTO_PHASE_SWITCH, 0, "disable auto phase switch"
        )
        self.coordinator.data["auto_phase_switch"] = 0
        self.async_write_ha_state()
        await _refresh_and_verify(
            self.coordinator,
            lambda data: data.get("auto_phase_switch") == 0,
            "FoxESS: disabled auto phase switch but read-back still shows "
            "enabled after refresh",
        )
