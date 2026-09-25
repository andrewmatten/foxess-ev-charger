"""Number entities for FoxESS EV Charger."""
from __future__ import annotations

import asyncio
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
from .modbus_client import FoxESSModbusClient

_LOGGER = logging.getLogger(__name__)

# Only these two reset to the charger's maximum after each session (per spec),
# so only these need re-asserting. The rest persist on their own.
REASSERTED_REGISTERS = {REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER}

# A successful Modbus reply can arrive before the charger has reflected a
# changed setpoint in its readable configuration registers.
READ_BACK_ATTEMPTS = 3
READ_BACK_DELAY = 1.5


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
        # Originally documented range was 10-60s. Corrected 2026-09 on live
        # evidence: Andrew's charger reports/accepts time_validity=180,
        # confirmed live - well outside that assumed range. 255 (a natural
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
    d = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        FoxESSNumber(d["coordinator"], d["client"], desc, entry) for desc in NUMBERS
    ])


class FoxESSNumber(FoxESSBlockAvailabilityMixin, CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    entity_description: FoxESSNumberDescription

    def __init__(
        self,
        coordinator: FoxESSChargerCoordinator,
        client: FoxESSModbusClient,
        description: FoxESSNumberDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._client      = client
        self.entity_description = description
        self._attr_unique_id   = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)
        self._block = description.block
        # translation_key == key for every number entity (see NUMBERS
        # above), so it's set here once rather than repeated as a literal
        # in each description.
        self._attr_translation_key = description.key

    @property
    def native_max_value(self) -> float:
        """Overrides NumberEntity's cached_property with the detected
        charger's own rated max, for the two descriptions that set
        capability_key (max_charging_current/max_charging_power) - falls
        back to entity_description.native_max_value (the A7300 single-phase
        default) via get_capabilities() itself if no model has been read
        yet or it doesn't match a known one. See MODEL_CAPABILITIES in
        const.py.
        """
        desc = self.entity_description
        if desc.capability_key is None:
            return desc.native_max_value
        model = (self.coordinator.data or {}).get("id_model_code")
        return get_capabilities(model)[desc.capability_key]

    @property
    def native_value(self) -> float | None:
        desc = self.entity_description
        if desc.register in REASSERTED_REGISTERS:
            # desired_setpoints is the authoritative "what should this be"
            # value for these two registers, independent of whether it's
            # actually been written to hardware yet (see
            # async_set_native_value below and
            # FoxESSChargerCoordinator.async_set_desired_setpoint's
            # docstring in __init__.py) - prefer it over the live register
            # read so the entity shows the user's saved intent even while
            # charging is stopped and nothing has been pushed to the
            # charger.
            desired_raw = self.coordinator.desired_setpoints.get(desc.register)
            if desired_raw is not None:
                return desc.scale_to_ha(desired_raw)
        raw = (self.coordinator.data or {}).get(desc.data_key)
        if raw is None or raw == desc.blank_sentinel:
            return None
        return desc.scale_to_ha(raw)

    async def _async_verify_read_back(
        self, desc: FoxESSNumberDescription, raw: int,
    ) -> None:
        """Verify a write without warning for a delayed or superseded value."""
        read_back = None
        for _attempt in range(READ_BACK_ATTEMPTS):
            await asyncio.sleep(READ_BACK_DELAY)
            await self.coordinator.async_request_refresh()
            read_back = (self.coordinator.data or {}).get(desc.data_key)
            if read_back == raw:
                return

            # A newer desired value has replaced this request. Its result,
            # rather than this older value's read-back, is now authoritative.
            if (
                desc.register in REASSERTED_REGISTERS
                and self.coordinator.desired_setpoints.get(desc.register) != raw
            ):
                return

        _LOGGER.warning(
            "FoxESS: wrote %s=%d but read-back after %d refreshes is %s - "
            "the charger acknowledged the write but may not have applied it",
            desc.key, raw, READ_BACK_ATTEMPTS, read_back,
        )

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        raw  = desc.scale_to_raw(value)
        _LOGGER.debug(
            "FoxESS: write %s=%s (raw=%d) → 0x%04X",
            desc.key, value, raw, desc.register,
        )

        if desc.register in REASSERTED_REGISTERS:
            # async_set_desired_setpoint always records the value first
            # (see its own docstring in __init__.py) and only submits the
            # physical write when charging is genuinely active right now,
            # decided fresh inside the coordinator's command lock. It
            # returns None when correctly skipped - not an error, native_value
            # above already reflects the saved intent - or True/False for
            # whether an attempted physical write itself succeeded.
            result = await self.coordinator.async_set_desired_setpoint(desc.register, raw)
            if result is False:
                await self.coordinator.async_flush_desired_setpoints()
                raise HomeAssistantError(
                    f"FoxESS: failed to write {desc.key}={value} (write to "
                    f"0x{desc.register:04X} failed)"
                )
            self.async_write_ha_state()
            # The coordinator poll normally persists this state later, but
            # an immediate integration reload can beat that poll. Flush the
            # staged intent before returning from the user's service call.
            await self.coordinator.async_flush_desired_setpoints()
            if result is None:
                return  # skipped, nothing landed on the charger - nothing to read back
            await self._async_verify_read_back(desc, raw)
            return

        success = await self.hass.async_add_executor_job(
            self._client.write_holding_register, desc.register, raw
        )
        if not success:
            raise HomeAssistantError(
                f"FoxESS: failed to write {desc.key}={value} (write to "
                f"0x{desc.register:04X} failed)"
            )
        self.coordinator.data[desc.data_key] = raw
        self.async_write_ha_state()

        await self._async_verify_read_back(desc, raw)
