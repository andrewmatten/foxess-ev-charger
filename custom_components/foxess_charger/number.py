"""Number platform: the charger's limits and timing settings.

Max Charging Current and Max Charging Power are the controller's saved
intent: a new value is written to the charger only while a charging session
is running and is otherwise staged for the next one. These two entities show
the saved value, with the charger's own reading as the ``observed``
attribute. Every other number is an allowlisted register write that the
controller confirms by reading it back.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfEnergy, UnitOfPower, UnitOfTime
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
    BLOCK_CONFIG,
    BLOCK_PHASE_BOX,
    DOMAIN,
    REG_ALLOWED_CHARGE_ENERGY,
    REG_ALLOWED_CHARGE_TIME,
    REG_DEFAULT_CURRENT,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_MIN_SWITCH_INTERVAL,
    REG_TIME_VALIDITY,
    get_capabilities,
)

_LOGGER = logging.getLogger(__name__)

# Controller intents a number can stand for.
INTENT_POWER = "power"
INTENT_CURRENT = "current"

NO_LIMIT = 0xFFFF  # what the session limit registers hold when no limit is set


@dataclass(frozen=True, kw_only=True)
class FoxESSNumberEntityDescription(NumberEntityDescription):
    """One writable setting and how its register maps to HA units."""

    register: int
    reading_key: str            # observation key holding the register's value
    tenths: bool = False        # register counts 0.1 units (else whole units)
    no_value_raw: int | None = None  # raw value to show as no value
    # Set for the two limits the controller keeps as intent.
    intent: str | None = None
    # MODEL_CAPABILITIES rating used as the maximum instead of
    # native_max_value, which is then only the A7300 fallback.
    rated_key: str | None = None
    # Register block whose freshness gates availability (const.BLOCK_*).
    block: str | None = BLOCK_CONFIG

    def to_raw(self, value: float) -> int:
        """HA value to register value. Whole-unit settings truncate."""
        return int(round(value * 10)) if self.tenths else int(value)

    def to_native(self, raw: int) -> float:
        return round(raw * 0.1, 1) if self.tenths else float(raw)


NUMBERS: tuple[FoxESSNumberEntityDescription, ...] = (
    # The two charging limits (controller intent).
    FoxESSNumberEntityDescription(
        key="max_charging_current", name="Max Charging Current",
        translation_key="max_charging_current", icon="mdi:current-ac",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_min_value=6.0, native_max_value=32.0, native_step=0.1,
        register=REG_MAX_CHARGING_CURRENT, reading_key="max_charging_current_raw",
        tenths=True, intent=INTENT_CURRENT, rated_key="max_current_a",
    ),
    FoxESSNumberEntityDescription(
        key="max_charging_power", name="Max Charging Power",
        translation_key="max_charging_power", icon="mdi:lightning-bolt",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        native_min_value=0.0, native_max_value=7.3, native_step=0.1,
        register=REG_MAX_CHARGING_POWER, reading_key="max_charging_power_raw",
        tenths=True, intent=INTENT_POWER, rated_key="max_power_kw",
    ),
    # Per-session limits; the charger reports 65535 while none is set, which
    # is shown as no value rather than as a number.
    FoxESSNumberEntityDescription(
        key="allowed_charge_time", name="Allowed Charge Time",
        translation_key="allowed_charge_time", icon="mdi:timer",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0, native_max_value=1440, native_step=1,
        register=REG_ALLOWED_CHARGE_TIME, reading_key="allowed_charge_time",
        no_value_raw=NO_LIMIT,
    ),
    FoxESSNumberEntityDescription(
        key="allowed_charge_energy", name="Allowed Charge Energy",
        translation_key="allowed_charge_energy", icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        native_min_value=0, native_max_value=999, native_step=1,
        register=REG_ALLOWED_CHARGE_ENERGY, reading_key="allowed_charge_energy",
        no_value_raw=NO_LIMIT,
    ),
    FoxESSNumberEntityDescription(
        key="time_validity", name="Command Time Validity",
        translation_key="time_validity", icon="mdi:clock-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # The protocol documents 10-60 s, but the charger reports and accepts
        # 180. The UI allows up to 255 (one byte) so it can show what the
        # hardware holds; 255 itself has not been tested. Never clamp the
        # value read back (see REG_TIME_VALIDITY).
        native_min_value=10, native_max_value=255, native_step=1,
        register=REG_TIME_VALIDITY, reading_key="time_validity",
    ),
    FoxESSNumberEntityDescription(
        key="default_current", name="Default Current (Fallback)",
        translation_key="default_current", icon="mdi:current-ac",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_min_value=6.0, native_max_value=32.0, native_step=0.1,
        register=REG_DEFAULT_CURRENT, reading_key="default_current_raw", tenths=True,
    ),
    FoxESSNumberEntityDescription(
        key="min_switch_interval", name="Min Phase Switch Interval",
        translation_key="min_switch_interval", icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=5, native_max_value=30, native_step=1,
        register=REG_MIN_SWITCH_INTERVAL, reading_key="min_switch_interval",
        entity_registry_enabled_default=False,  # needs the phase-switch box
        block=BLOCK_PHASE_BOX,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one entity per number description."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [FoxESSNumber(coordinator, description, entry) for description in NUMBERS]
    )


class FoxESSNumber(
    FoxESSLongContextMixin, FoxESSBlockAvailabilityMixin, CoordinatorEntity, NumberEntity,
):
    """A charger setting, changed only through the controller."""

    _attr_has_entity_name = True
    entity_description: FoxESSNumberEntityDescription

    def __init__(
        self,
        coordinator: FoxESSChargerCoordinator,
        description: FoxESSNumberEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)
        self._block = description.block
        self._last_outcome: str | None = None  # outcome of the last command

    @property
    def native_max_value(self) -> float:
        """The detected model's rating for the two charging limits; the
        description's static maximum for everything else."""
        description = self.entity_description
        if description.rated_key is None:
            return description.native_max_value
        model = (self.coordinator.data or {}).get("id_model_code")
        return get_capabilities(model)[description.rated_key]

    def _intended_raw(self) -> int | None:
        """The controller's saved value for an intent number."""
        controller = self.coordinator.controller
        intent = self.entity_description.intent
        if intent == INTENT_POWER:
            return controller.desired_power_raw
        if intent == INTENT_CURRENT:
            return controller.desired_current_raw
        return None

    def _reported_raw(self) -> int | None:
        """What the charger itself last reported, if it is a real value."""
        description = self.entity_description
        raw = (self.coordinator.data or {}).get(description.reading_key)
        if raw is None or raw == description.no_value_raw:
            return None
        return raw

    def _native_or_none(self, raw: int | None) -> float | None:
        return None if raw is None else self.entity_description.to_native(raw)

    @property
    def native_value(self) -> float | None:
        # An intent number shows the saved value it controls; without one
        # (or for any other number) the charger's reading is shown.
        if self.entity_description.intent is not None:
            intended = self._intended_raw()
            if intended is not None:
                return self.entity_description.to_native(intended)
        return self._native_or_none(self._reported_raw())

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.intent is None:
            return None
        intended, reported = self._intended_raw(), self._reported_raw()
        return {
            "desired": self._native_or_none(intended),
            "observed": self._native_or_none(reported),
            "last_outcome": self._last_outcome,
            # The charger holds exactly what was asked, and the controller
            # confirmed it rather than staging it.
            "confirmed": (
                self._last_outcome == CONFIRMED
                and intended is not None and intended == reported
            ),
        }

    async def async_set_native_value(self, value: float) -> None:
        description = self.entity_description
        raw = description.to_raw(value)
        controller = self.coordinator.controller
        try:
            if description.intent == INTENT_POWER:
                result = await controller.async_set_power(raw)
            elif description.intent == INTENT_CURRENT:
                result = await controller.async_set_current(raw)
            else:
                result = await controller.async_set_register(description.register, raw)
        except ControlError as err:
            raise HomeAssistantError(
                f"FoxESS: could not set {description.key} to {value}: {err}"
            ) from err
        self._last_outcome = getattr(result, "outcome", None)
        _LOGGER.debug(
            "%s set to %s (register value %d): %s",
            description.key, value, raw, self._last_outcome,
        )
        await self.coordinator.async_refresh()
        self.async_write_ha_state()
