"""Number entities for FoxESS EV Charger."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower, UnitOfTime, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER,
    REG_ALLOWED_CHARGE_TIME,  REG_ALLOWED_CHARGE_ENERGY,
    REG_TIME_VALIDITY,        REG_DEFAULT_CURRENT,
    REG_MIN_SWITCH_INTERVAL,
    BLOCK_CONFIG, BLOCK_PHASE_BOX,
    get_capabilities,
)
from .__init__ import FoxESSChargerCoordinator, FoxESSBlockAvailabilityMixin, build_device_info
from .adapter_api import CONFIRMED, ControlError

_LOGGER = logging.getLogger(__name__)

# The two limits the controller owns as desired intent (staged while
# paused, applied and confirmed while charging). Every other number is a
# plain allowlisted register write through the controller.
INTENT_REGISTERS = {REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER}


@dataclass(frozen=True, kw_only=True)
class FoxESSNumberDescription(NumberEntityDescription):
    register:       int                    = 0
    data_key:       str                    = ""
    scale_to_raw:   Callable[[float], int] = lambda v: int(v)
    scale_to_ha:    Callable[[int], float] = lambda v: float(v)
    blank_sentinel: int | None             = None
    # See FoxESSChargerSensorDescription.block in sensor.py - same mechanism.
    # Every number here reads from the 0x3000 config block except
    # min_switch_interval, which overrides it to BLOCK_PHASE_BOX below.
    block:          str | None             = BLOCK_CONFIG
    # Which MODEL_CAPABILITIES key (const.py) this entity's native_max_value
    # should be derived from, instead of the static native_max_value above -
    # set only on max_charging_current/max_charging_power, whose rated max
    # is model-dependent (7.3kW/32A single-phase vs 11-22kW/16-32A
    # three-phase). None means "use native_max_value as-is" (see
    # FoxESSNumber.native_max_value below).
    capability_key: str | None             = None


NUMBERS: tuple[FoxESSNumberDescription, ...] = (
    FoxESSNumberDescription(
        key="max_charging_current", name="Max Charging Current",
        icon="mdi:current-ac",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_min_value=6.0, native_max_value=32.0, native_step=0.1,
        capability_key="max_current_a",  # 32A default is the A7300 fallback; see native_max_value
        register=REG_MAX_CHARGING_CURRENT, data_key="max_charging_current_raw",
        scale_to_raw=lambda v: int(round(v * 10)),
        scale_to_ha =lambda v: round(v * 0.1, 1),
    ),
    FoxESSNumberDescription(
        key="max_charging_power", name="Max Charging Power",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        native_min_value=0.0, native_max_value=7.3, native_step=0.1,
        capability_key="max_power_kw",  # 7.3kW default is the A7300 fallback; see native_max_value
        register=REG_MAX_CHARGING_POWER, data_key="max_charging_power_raw",
        scale_to_raw=lambda v: int(round(v * 10)),
        scale_to_ha =lambda v: round(v * 0.1, 1),
    ),
    FoxESSNumberDescription(
        key="allowed_charge_time", name="Allowed Charge Time",
        icon="mdi:timer",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0, native_max_value=1440, native_step=1,
        register=REG_ALLOWED_CHARGE_TIME, data_key="allowed_charge_time",
        blank_sentinel=0xFFFF,  # 65535 = "no limit set" per spec, not a real value
    ),
    FoxESSNumberDescription(
        key="allowed_charge_energy", name="Allowed Charge Energy",
        icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        native_min_value=0, native_max_value=999, native_step=1,
        register=REG_ALLOWED_CHARGE_ENERGY, data_key="allowed_charge_energy",
        blank_sentinel=0xFFFF,  # 65535 = "no limit set" per spec, not a real value
    ),
    FoxESSNumberDescription(
        key="time_validity", name="Command Time Validity",
        icon="mdi:clock-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # Originally documented range was 10-60s. Corrected on observed
        # behaviour: the charger reports/accepts time_validity=180 - well outside that assumed range. 255 (a natural
        # single-byte register boundary) is used as the new UI ceiling
        # instead of just widening it to exactly 180, since 180 is only the
        # highest value actually observed, not necessarily the highest the
        # register supports - this is a "don't misrepresent what the
        # hardware accepts" correction, not a claim that 255 itself has been
        # tested. Do not clamp the live-read value down to the old 60s
        # bound (see REG_TIME_VALIDITY in const.py).
        native_min_value=10, native_max_value=255, native_step=1,
        register=REG_TIME_VALIDITY, data_key="time_validity",
    ),
    FoxESSNumberDescription(
        key="default_current", name="Default Current (Fallback)",
        icon="mdi:current-ac",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_min_value=6.0, native_max_value=32.0, native_step=0.1,
        register=REG_DEFAULT_CURRENT, data_key="default_current_raw",
        scale_to_raw=lambda v: int(round(v * 10)),
        scale_to_ha =lambda v: round(v * 0.1, 1),
    ),
    FoxESSNumberDescription(
        key="min_switch_interval", name="Min Phase Switch Interval",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=5, native_max_value=30, native_step=1,
        register=REG_MIN_SWITCH_INTERVAL, data_key="min_switch_interval",
        entity_registry_enabled_default=False,  # phase-switch-box only
        block=BLOCK_PHASE_BOX,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([FoxESSNumber(coordinator, desc, entry) for desc in NUMBERS])


class FoxESSNumber(FoxESSBlockAvailabilityMixin, CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    entity_description: FoxESSNumberDescription

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 description: FoxESSNumberDescription, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)
        self._block = description.block
        self._attr_translation_key = description.key
        self._last_outcome: str | None = None

    @property
    def _controller(self):
        return self.coordinator.controller

    @property
    def native_max_value(self) -> float:
        """Model-dependent maximum for the power/current limits."""
        desc = self.entity_description
        if desc.capability_key is None:
            return desc.native_max_value
        model = (self.coordinator.data or {}).get("id_model_code")
        return get_capabilities(model)[desc.capability_key]

    def _desired_raw(self) -> int | None:
        desc = self.entity_description
        if desc.register == REG_MAX_CHARGING_POWER:
            return self._controller.desired_power_raw
        if desc.register == REG_MAX_CHARGING_CURRENT:
            return self._controller.desired_current_raw
        return None

    def _observed_raw(self) -> int | None:
        raw = (self.coordinator.data or {}).get(self.entity_description.data_key)
        if raw is None or raw == self.entity_description.blank_sentinel:
            return None
        return raw

    @property
    def native_value(self) -> float | None:
        desc = self.entity_description
        if desc.register in INTENT_REGISTERS:
            # The saved limit is what this entity controls; the charger's
            # own reading is reported separately as `observed`.
            desired = self._desired_raw()
            if desired is not None:
                return desc.scale_to_ha(desired)
        observed = self._observed_raw()
        return None if observed is None else desc.scale_to_ha(observed)

    @property
    def extra_state_attributes(self) -> dict | None:
        desc = self.entity_description
        if desc.register not in INTENT_REGISTERS:
            return None
        desired, observed = self._desired_raw(), self._observed_raw()
        return {
            "desired": None if desired is None else desc.scale_to_ha(desired),
            "observed": None if observed is None else desc.scale_to_ha(observed),
            "last_outcome": self._last_outcome,
            "confirmed": (
                self._last_outcome == CONFIRMED
                and desired is not None and desired == observed
            ),
        }

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        raw = desc.scale_to_raw(value)
        try:
            if desc.register == REG_MAX_CHARGING_POWER:
                result = await self._controller.async_set_power(raw)
            elif desc.register == REG_MAX_CHARGING_CURRENT:
                result = await self._controller.async_set_current(raw)
            else:
                result = await self._controller.async_set_register(desc.register, raw)
        except ControlError as err:
            raise HomeAssistantError(
                f"FoxESS: could not set {desc.key} to {value}: {err}"
            ) from err
        self._last_outcome = getattr(result, "outcome", None)
        _LOGGER.debug("FoxESS: %s=%s (raw=%d) -> %s", desc.key, value, raw, self._last_outcome)
        await self.coordinator.async_refresh()
        self.async_write_ha_state()
