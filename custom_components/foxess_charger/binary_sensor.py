"""Binary sensor platform: charging, plug, fault, alarm and lock flags.

A flag whose source register is missing or holds an unrecognised value is
unknown (None), never off: automations treat an "off" edge as a real stop or
unplug, so off must come from a reading that actually says so.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FoxESSBlockAvailabilityMixin, FoxESSChargerCoordinator, build_device_info
from .const import BLOCK_PHASE_BOX, BLOCK_STATUS, DOMAIN

Observation = Mapping[str, Any]

_CHARGING = 3  # status code while power flows
_FAULT = 6     # status code for a charger fault


@dataclass(frozen=True, kw_only=True)
class FoxESSBinarySensorEntityDescription(BinarySensorEntityDescription):
    """How one flag is derived from an observation."""

    value_fn: Callable[[Observation], bool | None] = lambda _: None
    # Register block whose freshness gates availability (const.BLOCK_*).
    block: str | None = BLOCK_STATUS


def _when_known(key: str, test: Callable[[int], bool]) -> Callable[[Observation], bool | None]:
    """``test`` applied to a register value, or None while it has none."""
    def flag(data: Observation) -> bool | None:
        value = data.get(key)
        return None if value is None else test(value)
    return flag


def _has_fault(data: Observation) -> bool | None:
    """On for any set fault bit, and for status 6 on its own (the charger can
    report a fault with the bits clear). Unknown only when the fault register
    has no value and the status does not show a fault."""
    code = data.get("fault_code")
    if data.get("status") == _FAULT or (code or 0) > 0:
        return True
    return None if code is None else False


def _unlocked(data: Observation) -> bool | None:
    """HA's lock class means on = unlocked, the reverse of register 0x100F
    (1 = locked). Codes other than 0/1 are unknown."""
    lock = data.get("lock_status")
    if lock not in (0, 1):
        return None
    return lock == 0


BINARY_SENSORS: tuple[FoxESSBinarySensorEntityDescription, ...] = (
    FoxESSBinarySensorEntityDescription(
        key="is_charging", name="Charging", translation_key="is_charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING, icon="mdi:battery-charging",
        # Power flowing only. A vehicle pause still counts as a session for
        # the Charging switch, but not as charging here.
        value_fn=_when_known("status", lambda status: status == _CHARGING),
    ),
    FoxESSBinarySensorEntityDescription(
        key="vehicle_connected", name="Vehicle Connected", translation_key="vehicle_connected",
        device_class=BinarySensorDeviceClass.PLUG, icon="mdi:power-plug",
        value_fn=_when_known("cc_status", lambda cable: cable == 1),
    ),
    FoxESSBinarySensorEntityDescription(
        key="has_fault", name="Fault", translation_key="has_fault",
        device_class=BinarySensorDeviceClass.PROBLEM, icon="mdi:alert-circle",
        value_fn=_has_fault,
    ),
    FoxESSBinarySensorEntityDescription(
        key="has_alarm", name="Alarm", translation_key="has_alarm",
        device_class=BinarySensorDeviceClass.PROBLEM, icon="mdi:alert",
        value_fn=_when_known("alarm_code", lambda bits: bits > 0),
    ),
    FoxESSBinarySensorEntityDescription(
        key="is_locked", name="Locked", translation_key="is_locked",
        device_class=BinarySensorDeviceClass.LOCK, icon="mdi:lock",
        value_fn=_unlocked,
    ),
    FoxESSBinarySensorEntityDescription(
        key="auto_phase_switch", name="Auto Phase Switch", translation_key="auto_phase_switch",
        icon="mdi:auto-fix",
        entity_registry_enabled_default=False,  # needs the phase-switch box
        block=BLOCK_PHASE_BOX,
        value_fn=lambda data: data.get("auto_phase_switch") == 1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one entity per binary sensor description."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [FoxESSBinarySensor(coordinator, description, entry) for description in BINARY_SENSORS]
    )


class FoxESSBinarySensor(FoxESSBlockAvailabilityMixin, CoordinatorEntity, BinarySensorEntity):
    """A flag computed from the latest observation."""

    _attr_has_entity_name = True
    entity_description: FoxESSBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: FoxESSChargerCoordinator,
        description: FoxESSBinarySensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)
        self._block = description.block

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return self.entity_description.value_fn(data) if data else None
