"""Switch entities for FoxESS EV Charger."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BLOCK_PHASE_BOX, BLOCK_STATUS, DOMAIN, REG_AUTO_PHASE_SWITCH, REG_LOCK_CONTROL,
    SESSION_ACTIVE_STATUSES,
)
from .__init__ import (
    FoxESSChargerCoordinator, FoxESSBlockAvailabilityMixin, FoxESSLongContextMixin,
    build_device_info,
)
from .adapter_api import CONFIRMED, ControlError
from .session import STATUS_PHASE_SWITCHING, valid_status

_LOGGER = logging.getLogger(__name__)

LOCK_COMMAND = 2
UNLOCK_COMMAND = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        FoxESSChargingSwitch(coordinator, entry),
        FoxESSLockSwitch(coordinator, entry),
        FoxESSAutoPhaseSwitchSwitch(coordinator, entry),
    ])


class _FoxESSSwitch(FoxESSLongContextMixin, FoxESSBlockAvailabilityMixin, CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry,
                 key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = build_device_info(entry, coordinator)

    async def _async_command(self, call, action: str):
        try:
            result = await call
        except ControlError as err:
            raise HomeAssistantError(f"FoxESS: failed to {action}: {err}") from err
        await self.coordinator.async_refresh()
        self.async_write_ha_state()
        return result


class FoxESSChargingSwitch(_FoxESSSwitch):
    """On = charging is enabled and a session is underway (including a
    vehicle-initiated pause). Off = paused by the user, or no session."""

    _attr_icon = "mdi:ev-plug-type2"
    _block = BLOCK_STATUS
    _attr_translation_key = "charging"

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
            "intent_enabled": controller.intent_enabled,
            "control_phase": controller.phase,
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_command(
            self.coordinator.controller.async_enable(), "start charging",
        )

    async def async_turn_off(self, **kwargs) -> None:
        result = await self._async_command(
            self.coordinator.controller.async_pause(), "pause charging",
        )
        if getattr(result, "outcome", None) == CONFIRMED:
            await self.coordinator.async_user_paused()


class FoxESSLockSwitch(_FoxESSSwitch):
    _attr_icon = "mdi:lock"
    _block = BLOCK_STATUS
    _attr_translation_key = "lock"

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "lock", "Lock")

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get("lock_status")
        return None if value is None else value != 0

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_LOCK_CONTROL, LOCK_COMMAND),
            "lock the connector",
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_LOCK_CONTROL, UNLOCK_COMMAND),
            "unlock the connector",
        )


class FoxESSAutoPhaseSwitchSwitch(_FoxESSSwitch):
    _attr_icon = "mdi:auto-fix"
    _attr_entity_registry_enabled_default = False
    _block = BLOCK_PHASE_BOX
    _attr_translation_key = "auto_phase_switch"

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "auto_phase_switch", "Auto Phase Switch")

    @property
    def is_on(self) -> bool:
        return (self.coordinator.data or {}).get("auto_phase_switch") == 1

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_AUTO_PHASE_SWITCH, 1),
            "enable auto phase switch",
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_AUTO_PHASE_SWITCH, 0),
            "disable auto phase switch",
        )
