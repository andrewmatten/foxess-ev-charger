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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER,
    REG_ALLOWED_CHARGE_TIME,  REG_ALLOWED_CHARGE_ENERGY,
    REG_TIME_VALIDITY,        REG_DEFAULT_CURRENT,
    REG_MIN_SWITCH_INTERVAL,  ACTIVE_CHARGING_STATUSES,
)
from .__init__ import FoxESSChargerCoordinator, build_device_info
from .modbus_client import FoxESSModbusClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class FoxESSNumberDescription(NumberEntityDescription):
    register:         int                    = 0
    data_key:         str                    = ""
    scale_to_raw:     Callable[[float], int] = lambda v: int(round(v))
    scale_to_ha:      Callable[[int], float] = lambda v: float(v)
    blank_sentinel:   int | None             = None
    # True for the two registers where a device write doubles as an implicit
    # "resume charging" command on FoxESS firmware (see const.py's
    # ACTIVE_CHARGING_STATUSES comment). For those, async_set_native_value
    # below withholds the Modbus write entirely while charging isn't active,
    # rather than risk starting a session just from moving a slider.
    gate_on_charging: bool                  = False


NUMBERS: tuple[FoxESSNumberDescription, ...] = (
    FoxESSNumberDescription(
        key="max_charging_current", name="Max Charging Current",
        icon="mdi:current-ac",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_min_value=6.0, native_max_value=32.0, native_step=0.1,
        register=REG_MAX_CHARGING_CURRENT, data_key="max_charging_current_raw",
        scale_to_raw=lambda v: int(round(v * 10)),
        scale_to_ha =lambda v: round(v * 0.1, 1),
        gate_on_charging=True,
    ),
    FoxESSNumberDescription(
        key="max_charging_power", name="Max Charging Power",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        native_min_value=0.0, native_max_value=7.3, native_step=0.1,
        register=REG_MAX_CHARGING_POWER, data_key="max_charging_power_raw",
        scale_to_raw=lambda v: int(round(v * 10)),
        scale_to_ha =lambda v: round(v * 0.1, 1),
        gate_on_charging=True,
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
        native_min_value=10, native_max_value=60, native_step=1,
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


class FoxESSNumber(NumberEntity, RestoreEntity):
    _attr_has_entity_name = True
    entity_description: FoxESSNumberDescription

    def __init__(
        self,
        coordinator: FoxESSChargerCoordinator,
        client: FoxESSModbusClient,
        description: FoxESSNumberDescription,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._client      = client
        self.entity_description = description
        self._attr_unique_id   = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)

    async def async_added_to_hass(self) -> None:
        """Restore this entity's pre-restart value for the two
        gate_on_charging entries (Max Charging Current/Power).

        coordinator.data is purely in-memory - it starts empty on every HA
        restart, so without this, a limit configured while stopped (and
        deliberately withheld from the device - see async_set_native_value
        below) would be lost the moment HA restarts, even though it was
        never wrong, just not-yet-applied. If the device happens to already
        be mid-session when HA comes back up, the freshly-read live value
        is the more trustworthy source, so restoration is skipped and this
        cycle's real read (from _fetch(), already gated the same way in
        __init__.py) wins instead.
        """
        await super().async_added_to_hass()
        desc = self.entity_description
        if not desc.gate_on_charging:
            return
        if (self._coordinator.data or {}).get("status") in ACTIVE_CHARGING_STATUSES:
            return

        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            restored_value = float(last_state.state)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "FoxESS: could not restore %s from last state %r",
                desc.key, last_state.state,
            )
            return

        raw = desc.scale_to_raw(restored_value)
        self._coordinator.data[desc.data_key] = raw
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.last_update_success

    @property
    def native_value(self) -> float | None:
        desc = self.entity_description
        raw  = (self._coordinator.data or {}).get(desc.data_key)
        if raw is None or raw == desc.blank_sentinel:
            return None
        return desc.scale_to_ha(raw)

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        raw  = desc.scale_to_raw(value)

        if desc.gate_on_charging and (self._coordinator.data or {}).get("status") not in ACTIVE_CHARGING_STATUSES:
            # Withhold the device write: on this firmware, writing a nonzero
            # max-power/current register is itself an implicit "resume
            # charging" command, not a passive limit update (see
            # ACTIVE_CHARGING_STATUSES in const.py). Cache the desired value
            # locally instead - FoxESSChargingSwitch.async_turn_on pushes it
            # the moment charging is actually turned on, and the heartbeat
            # keeps it applied for the rest of the session.
            _LOGGER.debug(
                "FoxESS: %s=%s cached but not sent - charging is not active "
                "(0x%04X would resume it)", desc.key, value, desc.register,
            )
            self._coordinator.data[desc.data_key] = raw
            self.async_write_ha_state()
            return

        _LOGGER.debug(
            "FoxESS: write %s=%s (raw=%d) → 0x%04X",
            desc.key, value, raw, desc.register,
        )
        success = await self.hass.async_add_executor_job(
            self._client.write_holding_register, desc.register, raw
        )
        if success:
            self._coordinator.data[desc.data_key] = raw
            self.async_write_ha_state()
        else:
            _LOGGER.error("FoxESS: Write failed for %s", desc.key)
        await asyncio.sleep(1.5)
        await self._coordinator.async_request_refresh()