"""Select platform: work mode and manual phase selection (needs a phase-switch box).

Each select shows one enum register and changes it through the controller,
which confirms the new value by reading the charger back.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FoxESSBlockAvailabilityMixin, FoxESSChargerCoordinator, build_device_info
from .adapter_api import ControlError
from .const import (
    BLOCK_CONFIG,
    BLOCK_STATUS,
    CONF_PHASE_BOX_FITTED,
    DOMAIN,
    PHASE_SEQ_MAP,
    REG_PHASE_SWITCHING,
    REG_WORK_MODE,
    WORK_MODE_SELECT_OPTIONS,
    decode_enum,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the work mode and phase selects."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [FoxESSWorkModeSelect(coordinator, entry), FoxESSPhaseSelect(coordinator, entry)]
    )


class _EnumRegisterSelect(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SelectEntity):
    """Select over one enum register.

    Subclasses provide the entity description, the code table (raw value ->
    option), the register written and the observation key read back.
    """

    _attr_has_entity_name = True
    entity_description: SelectEntityDescription
    _codes: dict[int, str]
    _register: int
    _reading_key: str
    _fallback_label: str

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self.entity_description.key}"
        self._attr_options = list(self._codes.values())
        self._attr_device_info = build_device_info(entry, coordinator)
        self._raw_for_option = {option: raw for raw, option in self._codes.items()}

    @property
    def current_option(self) -> str | None:
        raw = (self.coordinator.data or {}).get(self._reading_key)
        return decode_enum(raw, self._codes, self._reading_key)

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.controller.async_set_register(
                self._register, self._raw_for_option[option]
            )
        except ControlError as err:
            label = self.name if getattr(self, "platform_data", None) else self._fallback_label
            raise HomeAssistantError(
                f"FoxESS: failed to set {label} to {option}: {err}"
            ) from err
        await self.coordinator.async_refresh()
        self.async_write_ha_state()


class FoxESSWorkModeSelect(_EnumRegisterSelect):
    """Controlled / Plug&Charge / Locked.

    The options keep their original display-style strings because
    automations select them by value.
    """

    entity_description = SelectEntityDescription(
        key="work_mode",
        translation_key="work_mode_control",
        icon="mdi:ev-station",
    )
    _block = BLOCK_CONFIG
    _codes = WORK_MODE_SELECT_OPTIONS
    _register = REG_WORK_MODE
    _reading_key = "work_mode"
    _fallback_label = "Work Mode"


class FoxESSPhaseSelect(_EnumRegisterSelect):
    """Phases to charge on; only does anything with a phase-switch box.

    Shows the phase sequence from the status block and writes the
    phase-switching command register. Unavailable unless the user has said
    a box is fitted (the `phase_box_fitted` option, read live from the
    entry); the charger gives no reliable sign of the box itself.
    """

    entity_description = SelectEntityDescription(
        key="phase_sequence",
        translation_key="phase_switching_control",
        icon="mdi:electric-switch",
        entity_registry_enabled_default=False,
    )
    _block = BLOCK_STATUS
    _codes = PHASE_SEQ_MAP
    _register = REG_PHASE_SWITCHING
    _reading_key = "phase_sequence"
    _fallback_label = "Phase Sequence"

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._entry = entry

    @property
    def available(self) -> bool:
        return super().available and bool(self._entry.options.get(CONF_PHASE_BOX_FITTED, False))

    async def async_select_option(self, option: str) -> None:
        if not self._entry.options.get(CONF_PHASE_BOX_FITTED, False):
            raise HomeAssistantError(
                "FoxESS: the phase selector only works with a FoxESS phase-switch box. "
                "Turn on 'Phase-switch box fitted' in the integration options if you have one."
            )
        await super().async_select_option(option)
