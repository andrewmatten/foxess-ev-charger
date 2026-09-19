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
) -> None:
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
    if not check_ok(coordinator.data or {}):
        _LOGGER.warning(mismatch_msg)


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
        # async_send_start sets _charging_desired=True (and clears
        # _stop_inhibit) itself, INSIDE its own command-lock section, on a
        # confirmed-successful write - not here, and not before the write
        # even happens. See async_send_start's own docstring (__init__.py)
        # for why that state transition had to move inside the lock: doing
        # it here, after the lock was already released, raced against a
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
        success = await self.coordinator.async_send_start()
        if not success:
            raise HomeAssistantError(
                "FoxESS: failed to start charging (write to 0x3000 failed)"
            )
        self.coordinator.data["status"] = 3
        self.async_write_ha_state()
        # Push the current desired setpoints immediately rather than waiting
        # for the heartbeat's next scheduled tick - direct set() is safe
        # here, this method runs on the event loop (see
        # FoxESSChargerCoordinator._wake_heartbeat in __init__.py).
        self.coordinator._wake_heartbeat()
        await _refresh_and_verify(
            self.coordinator,
            lambda data: data.get("status") in SESSION_ACTIVE_STATUSES,
            "FoxESS: sent start-charging command but charger status did not "
            "reflect an active session after refresh",
        )

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
        self.coordinator._session_state_dirty = True
        success = await self.coordinator.async_send_stop()
        if not success:
            # The stop write itself failed. Found in review, 2026-09-18:
            # this used to restore _charging_desired=True (and clear
            # _stop_inhibit) whenever a fresh status read still showed an
            # active session, on the theory that a genuinely-still-running
            # session deserves heartbeat protection even though the stop
            # attempt that would have ended it didn't land. But this
            # integration cannot tell that case apart from the one that
            # actually happened tonight: an *unwanted* resume, where "the
            # charger is still active" is exactly the problem being
            # stopped, not evidence protection should resume. Reinforcing
            # it in that case is actively harmful - the whole point of this
            # handler's first three statements above. So this branch now
            # deliberately does nothing beyond raising: _charging_desired
            # stays False and _stop_inhibit stays True, matching this
            # project's fail-toward-the-protective-branch precedent (see
            # the low_soc pause condition's own history) - a genuinely
            # normal session that failed to stop loses heartbeat protection
            # for one cycle until the stop is retried or the vehicle
            # disconnects, which is a much smaller risk than silently
            # re-arming an intentional stop.
            raise HomeAssistantError(
                "FoxESS: failed to stop charging (write to 0x3000 failed)"
            )
        self.coordinator.data["status"] = 5
        self.async_write_ha_state()
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
