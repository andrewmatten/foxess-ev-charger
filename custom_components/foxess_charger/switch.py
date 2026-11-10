"""Switch platform: charging on/off, the connector lock and automatic phase
switching.

Every switch acts through the controller. A command that cannot be confirmed
on the charger is raised to the caller as a HomeAssistantError; otherwise the
switch refreshes the observation so its state follows the charger.
"""
from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import (
    FoxESSBlockAvailabilityMixin,
    FoxESSChargerCoordinator,
    FoxESSLongContextMixin,
    build_device_info,
)
from .adapter_api import CONFIRMED, ControlError
from .const import (
    BLOCK_PHASE_BOX,
    BLOCK_STATUS,
    DOMAIN,
    REG_AUTO_PHASE_SWITCH,
    REG_LOCK_CONTROL,
    SESSION_ACTIVE_STATUSES,
)
from .session import STATUS_PHASE_SWITCHING, valid_status

# Values for the 0x4000 lock command register.
LOCK_COMMAND = 2
UNLOCK_COMMAND = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the charging, lock and auto phase switch entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [switch(coordinator, entry)
         for switch in (FoxESSChargingSwitch, FoxESSLockSwitch, FoxESSAutoPhaseSwitchSwitch)]
    )


class _ControllerSwitch(
    FoxESSLongContextMixin, FoxESSBlockAvailabilityMixin, CoordinatorEntity, SwitchEntity,
):
    """Shared plumbing: identity, and running a controller command."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry,
                 unique_suffix: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        # Set explicitly, so it is used instead of the translated name.
        self._attr_name = name
        self._attr_device_info = build_device_info(entry, coordinator)

    async def _async_command(self, command: Awaitable[Any], action: str) -> Any:
        """Await a controller command, then refresh and publish the state.
        ``action`` completes the error message ("failed to <action>")."""
        try:
            result = await command
        except ControlError as err:
            raise HomeAssistantError(f"FoxESS: failed to {action}: {err}") from err
        await self.coordinator.async_refresh()
        self.async_write_ha_state()
        return result


class FoxESSChargingSwitch(_ControllerSwitch):
    """On while charging is enabled and a session is in progress, including
    a pause requested by the vehicle. Off when the user paused charging or
    no session is running; unknown while the status cannot be read."""

    _attr_icon = "mdi:ev-plug-type2"
    _attr_translation_key = "charging"
    _block = BLOCK_STATUS

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charging", "Charging")

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.controller.intent_enabled:
            return False
        status = valid_status(self.coordinator.data or {})
        if status is None:
            return None
        return status in SESSION_ACTIVE_STATUSES or status == STATUS_PHASE_SWITCHING

    @property
    def extra_state_attributes(self) -> dict:
        controller = self.coordinator.controller
        return {
            "intent_enabled": controller.intent_enabled, "control_phase": controller.phase,
            "latch_cause": controller.latch_cause, "needs_human": controller.needs_human,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        # A hard latch needs a person (a call with a user, not an automation)
        # who could already see it in Home Assistant.
        person = self._context is not None and self._context.user_id is not None
        shown = (self.coordinator.data or {}).get("needs_human") is True
        await self._async_command(
            self.coordinator.controller.async_enable(clear_hard=person and shown),
            "start charging",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        result = await self._async_command(
            self.coordinator.controller.async_pause(), "pause charging",
        )
        # A confirmed user pause closes the logical session.
        if getattr(result, "outcome", None) == CONFIRMED:
            await self.coordinator.async_user_paused()


class FoxESSLockSwitch(_ControllerSwitch):
    """Connector lock: on = locked (register 0x100F reads 1)."""

    _attr_icon = "mdi:lock"
    _attr_translation_key = "lock"
    _block = BLOCK_STATUS

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "lock", "Lock")

    @property
    def is_on(self) -> bool | None:
        lock = (self.coordinator.data or {}).get("lock_status")
        if lock not in (0, 1):  # e.g. 65535: unknown, not unlocked
            return None
        return lock == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_LOCK_CONTROL, LOCK_COMMAND),
            "lock the connector",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_LOCK_CONTROL, UNLOCK_COMMAND),
            "unlock the connector",
        )


class FoxESSAutoPhaseSwitchSwitch(_ControllerSwitch):
    """Automatic phase switching; needs the phase-switch box."""

    _attr_icon = "mdi:auto-fix"
    _attr_translation_key = "auto_phase_switch"
    _attr_entity_registry_enabled_default = False
    _block = BLOCK_PHASE_BOX

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "auto_phase_switch", "Auto Phase Switch")

    @property
    def is_on(self) -> bool:
        return (self.coordinator.data or {}).get("auto_phase_switch") == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_AUTO_PHASE_SWITCH, 1),
            "enable auto phase switch",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_AUTO_PHASE_SWITCH, 0),
            "disable auto phase switch",
        )
