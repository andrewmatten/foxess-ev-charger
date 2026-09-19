"""Select entities for FoxESS EV Charger."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, REG_WORK_MODE, REG_PHASE_SWITCHING, WORK_MODE_SELECT_OPTIONS, PHASE_SEQ_MAP, decode_enum,
    BLOCK_CONFIG, BLOCK_STATUS,
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
        FoxESSWorkModeSelect(d["coordinator"], d["client"], entry),
        FoxESSPhaseSelect(d["coordinator"], d["client"], entry),
    ])


class FoxESSWorkModeSelect(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:ev-station"
    # work_mode is read from the 0x3000 config block, not the status batch.
    _block = BLOCK_CONFIG
    # translation_key still translates the entity NAME ("Work Mode") - that
    # part is independent of the options contract below. It does NOT
    # translate the OPTIONS: those stay the original display-cased strings
    # ("Controlled" etc, not the lowercase WORK_MODE_MAP the plain sensor
    # uses), because HA validates select.select_option against this
    # entity's `options` before ever calling async_select_option - changing
    # these values is an unavoidable breaking change for anything already
    # calling this service with the old strings. The translation JSON's
    # "state" keys for this translation_key (lowercase) simply won't match
    # and HA falls back to showing the raw option - options-translation is
    # deferred to 3.0.0 (see const.py's WORK_MODE_SELECT_OPTIONS comment).
    _attr_translation_key = "work_mode_control"
    _options_map = WORK_MODE_SELECT_OPTIONS
    _reverse_map = {v: k for k, v in WORK_MODE_SELECT_OPTIONS.items()}

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 client: FoxESSModbusClient, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._client      = client
        self._attr_unique_id   = f"{entry.entry_id}_work_mode"
        self._attr_name        = "Work Mode"
        self._attr_options     = list(self._options_map.values())
        self._attr_device_info = build_device_info(entry, coordinator)

    @property
    def current_option(self) -> str | None:
        raw = self.coordinator.data.get("work_mode") if self.coordinator.data else None
        return decode_enum(raw, self._options_map, "work_mode")

    async def async_select_option(self, option: str) -> None:
        value = self._reverse_map[option]
        _LOGGER.debug("FoxESS: write work_mode=%s (%d) → 0x%04X", option, value, REG_WORK_MODE)
        success = await self.hass.async_add_executor_job(
            self._client.write_holding_register, REG_WORK_MODE, value  # ← REG_WORK_MODE (FC 0x10)
        )
        if not success:
            raise HomeAssistantError(f"FoxESS: failed to set Work Mode to {option}")

        self.coordinator.data["work_mode"] = value
        self.async_write_ha_state()

        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()
        read_back = (self.coordinator.data or {}).get("work_mode")
        if read_back != value:
            _LOGGER.warning(
                "FoxESS: set Work Mode to %s (%d) but read-back after "
                "refresh is %s - the charger acknowledged the write but "
                "may not have applied it",
                option, value, read_back,
            )


class FoxESSPhaseSelect(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:electric-switch"
    # Phase switching is only meaningful with an external phase-switch-box
    # accessory, which single-phase A7300P1-E-B-WO hardware does not have.
    _attr_entity_registry_enabled_default = False
    # current_option reads "phase_sequence" (0x1010), part of the status
    # batch - REG_PHASE_SWITCHING (0x4002) itself is write-only, so there's
    # nothing to read back from that register directly.
    _block = BLOCK_STATUS
    _attr_translation_key = "phase_switching_control"
    _options_map = PHASE_SEQ_MAP
    _reverse_map = {v: k for k, v in PHASE_SEQ_MAP.items()}

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 client: FoxESSModbusClient, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._client      = client
        self._attr_unique_id   = f"{entry.entry_id}_phase_sequence"
        self._attr_name        = "Phase Sequence"
        self._attr_options     = list(self._options_map.values())
        self._attr_device_info = build_device_info(entry, coordinator)

    @property
    def current_option(self) -> str | None:
        raw = self.coordinator.data.get("phase_sequence") if self.coordinator.data else None
        return decode_enum(raw, self._options_map, "phase_sequence")

    async def async_select_option(self, option: str) -> None:
        value = self._reverse_map[option]
        _LOGGER.debug("FoxESS: write phase=%s (%d) → 0x%04X", option, value, REG_PHASE_SWITCHING)
        success = await self.hass.async_add_executor_job(
            self._client.write_holding_register, REG_PHASE_SWITCHING, value  # ← FC 0x06 (W-Only)
        )
        if not success:
            raise HomeAssistantError(f"FoxESS: failed to set Phase Sequence to {option}")

        self.coordinator.data["phase_sequence"] = value
        self.async_write_ha_state()

        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()
        read_back = (self.coordinator.data or {}).get("phase_sequence")
        if read_back != value:
            _LOGGER.warning(
                "FoxESS: set Phase Sequence to %s (%d) but read-back after "
                "refresh is %s - the charger acknowledged the write but "
                "may not have applied it",
                option, value, read_back,
            )