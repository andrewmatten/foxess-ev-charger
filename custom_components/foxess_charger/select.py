"""Select entities for FoxESS EV Charger."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BLOCK_CONFIG, BLOCK_STATUS, DOMAIN, PHASE_SEQ_MAP, REG_PHASE_SWITCHING,
    REG_WORK_MODE, WORK_MODE_SELECT_OPTIONS, decode_enum,
)
from .__init__ import FoxESSChargerCoordinator, FoxESSBlockAvailabilityMixin, build_device_info
from .adapter_api import ControlError


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        FoxESSWorkModeSelect(coordinator, entry),
        FoxESSPhaseSelect(coordinator, entry),
    ])


class _FoxESSRegisterSelect(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SelectEntity):
    """A select backed by one enum register, written via the controller."""

    _attr_has_entity_name = True
    _options_map: dict[int, str]
    _data_key: str
    _register: int

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry,
                 key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_options = list(self._options_map.values())
        self._attr_device_info = build_device_info(entry, coordinator)

    @property
    def current_option(self) -> str | None:
        raw = (self.coordinator.data or {}).get(self._data_key)
        return decode_enum(raw, self._options_map, self._data_key)

    async def async_select_option(self, option: str) -> None:
        reverse = {v: k for k, v in self._options_map.items()}
        try:
            await self.coordinator.controller.async_set_register(self._register, reverse[option])
        except ControlError as err:
            raise HomeAssistantError(f"FoxESS: failed to set {self.name} to {option}: {err}") from err
        await self.coordinator.async_refresh()
        self.async_write_ha_state()


class FoxESSWorkModeSelect(_FoxESSRegisterSelect):
    _attr_icon = "mdi:ev-station"
    _block = BLOCK_CONFIG
    _attr_translation_key = "work_mode_control"
    _options_map = WORK_MODE_SELECT_OPTIONS
    _data_key = "work_mode"
    _register = REG_WORK_MODE

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "work_mode", "Work Mode")


class FoxESSPhaseSelect(_FoxESSRegisterSelect):
    _attr_icon = "mdi:electric-switch"
    _attr_entity_registry_enabled_default = False
    _block = BLOCK_STATUS
    _attr_translation_key = "phase_switching_control"
    _options_map = PHASE_SEQ_MAP
    _data_key = "phase_sequence"
    _register = REG_PHASE_SWITCHING

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "phase_sequence", "Phase Sequence")
