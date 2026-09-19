"""Sensors for FoxESS EV Charger."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent, UnitOfElectricPotential,
    UnitOfEnergy, UnitOfPower, UnitOfTemperature, UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, STATUS_MAP, CP_STATUS_MAP, CC_STATUS_MAP, LOCK_STATUS_MAP,
    WORK_MODE_MAP, PHASE_SEQ_MAP, STOP_REASON_MAP, decode_enum,
    BLOCK_STATUS, BLOCK_CONFIG,
    get_capabilities,
)
from .__init__ import FoxESSChargerCoordinator, FoxESSBlockAvailabilityMixin, build_device_info

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class FoxESSChargerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], StateType] = lambda _: None
    attrs_fn: Callable[[dict], dict] = lambda _: {}
    # Physical plausibility bounds for numeric sensors (current/power/
    # temperature/voltage) - a raw register value wildly outside what this
    # hardware can ever legitimately produce (a corrupt read, not a real
    # measurement) is reported as unavailable rather than displayed as-is.
    # None means "no bound in that direction".
    min_plausible: float | None = None
    max_plausible: float | None = None
    # 2026-09 (second audit): which MODEL_CAPABILITIES key (const.py) this
    # entity's max_plausible bound should be derived from instead of the
    # static literal above. Set only on the three power sensors below - a
    # hardcoded ~10kW ceiling (right for the single-phase 7.3kW A7300) would
    # incorrectly reject perfectly real readings from the three-phase A011
    # (11kW) / A022 (22kW) models this codebase already has capability
    # entries for (number.py's Max Charging Current/Power already derives
    # its bounds the same way - see FoxESSNumber.native_max_value). None
    # means "use the static max_plausible above as-is" - the current
    # sensors' static 40A ceiling already comfortably covers every known
    # model's rated current (32A max), so they don't need this.
    capability_key: str | None = None
    # Headroom multiplier applied on top of the detected model's own rated
    # max when capability_key is set - same SAFETY_FACTOR-style reasoning as
    # energy_guard.py: margin for measurement noise/rounding, not a second
    # guess at the real rating. 1.5x mirrors ENERGY_GUARD_SAFETY_FACTOR.
    capability_margin: float = 1.5
    # Which batched register block (see const.py's BLOCK_* constants) this
    # entity's value_fn reads from - drives FoxESSBlockAvailabilityMixin's
    # availability check (see __init__.py). Defaults to BLOCK_STATUS since
    # nearly every sensor here reads the 0x1000-0x101D batch; the couple
    # that don't (work_mode_sensor from the 0x3000 config block) override it
    # explicitly below, and transport_errors sets None (not tied to any
    # single block - it reflects the Modbus client's own counters).
    block: str | None = BLOCK_STATUS


SENSORS: tuple[FoxESSChargerSensorDescription, ...] = (
    # ── System ──────────────────────────────────────────────────────────────
    FoxESSChargerSensorDescription(
        key="software_version", name="Software Version", icon="mdi:information-outline",
        translation_key="software_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: f"{d.get('software_version',0)>>8}.{d.get('software_version',0)&0xFF}",
    ),
    FoxESSChargerSensorDescription(
        key="device_address", name="Device Address", icon="mdi:identifier",
        translation_key="device_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("device_address"),
    ),
    FoxESSChargerSensorDescription(
        key="id_serial_number", name="Serial Number", icon="mdi:card-account-details-outline",
        translation_key="id_serial_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("id_serial_number"),
    ),
    # ── Status ───────────────────────────────────────────────────────────────
    # All of these route through decode_enum() rather than a `.get(raw,
    # "unknown")` fallback or a hand-rolled ternary. An unrecognized raw
    # value passes unknown_label="unknown" here - a real, translatable state
    # (added to `options` below, since HA raises if an ENUM sensor's value
    # isn't one of its declared options) rather than a ternary's "else"
    # branch silently claiming a specific known state that isn't actually
    # what was read. Distinct from "no reading yet" (raw value is None),
    # which still surfaces as HA's own native unknown state - see
    # decode_enum()'s docstring in const.py.
    FoxESSChargerSensorDescription(
        key="status", name="Status", icon="mdi:ev-station",
        device_class=SensorDeviceClass.ENUM,
        translation_key="status",
        options=[*STATUS_MAP.values(), "unknown"],
        value_fn=lambda d: decode_enum(d.get("status"), STATUS_MAP, "status", unknown_label="unknown"),
    ),
    FoxESSChargerSensorDescription(
        key="cp_status", name="CP Status", icon="mdi:connection",
        device_class=SensorDeviceClass.ENUM,
        translation_key="cp_status",
        options=[*CP_STATUS_MAP.values(), "unknown"],
        value_fn=lambda d: decode_enum(d.get("cp_status"), CP_STATUS_MAP, "cp_status", unknown_label="unknown"),
    ),
    FoxESSChargerSensorDescription(
        key="cc_status", name="CC Status", icon="mdi:cable-data",
        device_class=SensorDeviceClass.ENUM,
        translation_key="cc_status",
        options=[*CC_STATUS_MAP.values(), "unknown"],
        value_fn=lambda d: decode_enum(d.get("cc_status"), CC_STATUS_MAP, "cc_status", unknown_label="unknown"),
    ),
    FoxESSChargerSensorDescription(
        key="lock_status", name="Lock Status", icon="mdi:lock",
        device_class=SensorDeviceClass.ENUM,
        translation_key="lock_status",
        options=[*LOCK_STATUS_MAP.values(), "unknown"],
        value_fn=lambda d: decode_enum(d.get("lock_status"), LOCK_STATUS_MAP, "lock_status", unknown_label="unknown"),
    ),
    FoxESSChargerSensorDescription(
        key="work_mode_sensor", name="Work Mode", icon="mdi:cog",
        device_class=SensorDeviceClass.ENUM,
        translation_key="work_mode",  # entity key differs from the translation key on purpose
        options=[*WORK_MODE_MAP.values(), "unknown"],
        block=BLOCK_CONFIG,  # work_mode comes from the 0x3000 config block, not the status batch
        value_fn=lambda d: decode_enum(d.get("work_mode"), WORK_MODE_MAP, "work_mode", unknown_label="unknown"),
    ),
    FoxESSChargerSensorDescription(
        key="phase_sequence", name="Phase Sequence", icon="mdi:electric-switch",
        device_class=SensorDeviceClass.ENUM,
        translation_key="phase_sequence",
        options=[*PHASE_SEQ_MAP.values(), "unknown"],
        entity_registry_enabled_default=False,  # phase-switch-box only
        value_fn=lambda d: decode_enum(d.get("phase_sequence"), PHASE_SEQ_MAP, "phase_sequence", unknown_label="unknown"),
    ),
    FoxESSChargerSensorDescription(
        key="stop_reason", name="Stop Reason", icon="mdi:information",
        translation_key="stop_reason",
        # Not a device_class=ENUM sensor (no declared `options` list to
        # violate), so no unknown_label needed - an unrecognized value just
        # surfaces as HA's native unknown state, same as always.
        value_fn=lambda d: decode_enum(d.get("stop_reason"), STOP_REASON_MAP, "stop_reason"),
    ),
    # ── Temperaturen ─────────────────────────────────────────────────────────
    FoxESSChargerSensorDescription(
        key="port_temperature", name="Port Temperature",
        translation_key="port_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer",
        # Not fitted on every model - the A7300P1 reports the 65535 "no sensor"
        # sentinel here, so this sits permanently at `unknown` and looks broken.
        # Disabled by default; enable it manually if your hardware has the probe.
        # Port over-temperature protection still exists in firmware regardless
        # (fault bit 4, charging_port_overtemp) - only the reading is absent.
        entity_registry_enabled_default=False,
        # -40..120C: generous bound for board-mounted electronics - well
        # outside anything this hardware can plausibly reach, but well
        # inside what a corrupt/garbled register read can produce.
        min_plausible=-40, max_plausible=120,
        value_fn=lambda d: round(d["port_temp_raw"] * 0.1 - 50, 1)
            if d.get("port_temp_raw") not in (None, 65535) else None,
    ),
    FoxESSChargerSensorDescription(
        # Named "Ambient" in the protocol spec, but it is a board-mounted sensor
        # inside the enclosure, not room air: measured 23.6 C idle and 55.5 C
        # while delivering 30 A on the same unit, same afternoon. Reported as
        # internal temperature so the value isn't mistaken for the garage.
        key="ambient_temperature", name="Internal Temperature",
        translation_key="ambient_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer",
        min_plausible=-40, max_plausible=120,
        value_fn=lambda d: round(d.get("ambient_temp_raw", 0) * 0.1 - 50, 1),
    ),
    # ── Spannungen ───────────────────────────────────────────────────────────
    FoxESSChargerSensorDescription(
        key="l1_voltage", name="L1 Voltage",
        translation_key="l1_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        icon="mdi:flash",
        # 0-300V: covers every real-world single/split-phase mains voltage
        # worldwide with margin, while still catching a garbled register
        # (e.g. the raw=65535 sentinel would decode as 6553.5V).
        min_plausible=0, max_plausible=300,
        value_fn=lambda d: round(d.get("l1_voltage_raw", 0) * 0.1, 1),
    ),
    FoxESSChargerSensorDescription(
        key="l2_voltage", name="L2 Voltage",
        translation_key="l2_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        icon="mdi:flash",
        entity_registry_enabled_default=False,  # single-phase: no L2
        min_plausible=0, max_plausible=300,
        value_fn=lambda d: round(d.get("l2_voltage_raw", 0) * 0.1, 1),
    ),
    FoxESSChargerSensorDescription(
        key="l3_voltage", name="L3 Voltage",
        translation_key="l3_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        icon="mdi:flash",
        entity_registry_enabled_default=False,  # single-phase: no L3
        min_plausible=0, max_plausible=300,
        value_fn=lambda d: round(d.get("l3_voltage_raw", 0) * 0.1, 1),
    ),
    # ── Ströme ───────────────────────────────────────────────────────────────
    # 0-40A: 32A rated max plus headroom for measurement noise/rounding -
    # anything beyond this on single-phase 7.3kW hardware is a bad read,
    # not a real current.
    FoxESSChargerSensorDescription(
        key="l1_current", name="L1 Current",
        translation_key="l1_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        min_plausible=0, max_plausible=40,
        value_fn=lambda d: round(d.get("l1_current_raw", 0) * 0.1, 1),
    ),
    FoxESSChargerSensorDescription(
        key="l2_current", name="L2 Current",
        translation_key="l2_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        entity_registry_enabled_default=False,  # single-phase: no L2
        min_plausible=0, max_plausible=40,
        value_fn=lambda d: round(d.get("l2_current_raw", 0) * 0.1, 1),
    ),
    FoxESSChargerSensorDescription(
        key="l3_current", name="L3 Current",
        translation_key="l3_current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        entity_registry_enabled_default=False,  # single-phase: no L3
        min_plausible=0, max_plausible=40,
        value_fn=lambda d: round(d.get("l3_current_raw", 0) * 0.1, 1),
    ),
    # ── Leistung ─────────────────────────────────────────────────────────────
    # Bound derived from the detected model's rated max power (see
    # capability_key above) rather than a fixed literal - the single-phase
    # A7300's 7.3kW rating would otherwise incorrectly reject valid readings
    # from the three-phase A011 (11kW)/A022 (22kW) models. Falls back to the
    # A7300 default via get_capabilities() itself before a model is known.
    FoxESSChargerSensorDescription(
        key="charging_power", name="Charging Power",
        translation_key="charging_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        icon="mdi:lightning-bolt",
        min_plausible=0, capability_key="max_power_kw",
        value_fn=lambda d: round(d.get("power_raw", 0) * 0.1, 2),
    ),
    FoxESSChargerSensorDescription(
        key="max_supported_power", name="Max Supported Power",
        translation_key="max_supported_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        icon="mdi:lightning-bolt-outline",
        min_plausible=0, capability_key="max_power_kw",
        value_fn=lambda d: round(d.get("max_power_raw", 0) * 0.1, 1),
    ),
    FoxESSChargerSensorDescription(
        key="min_supported_power", name="Min Supported Power",
        translation_key="min_supported_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        icon="mdi:lightning-bolt-outline",
        min_plausible=0, capability_key="max_power_kw",
        value_fn=lambda d: round(d.get("min_power_raw", 0) * 0.1, 1),
    ),
    # ── Strom-Limits ─────────────────────────────────────────────────────────
    FoxESSChargerSensorDescription(
        key="max_supported_current", name="Max Supported Current",
        translation_key="max_supported_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        min_plausible=0, max_plausible=40,
        value_fn=lambda d: round(d.get("max_current_raw", 0) * 0.1, 1),
    ),
    FoxESSChargerSensorDescription(
        key="min_supported_current", name="Min Supported Current",
        translation_key="min_supported_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        icon="mdi:current-ac",
        min_plausible=0, max_plausible=40,
        value_fn=lambda d: round(d.get("min_current_raw", 0) * 0.1, 1),
    ),
    # ── Energie ──────────────────────────────────────────────────────────────
    FoxESSChargerSensorDescription(
        key="current_session_energy", name="Current Session Energy",
        translation_key="current_session_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:counter",
        value_fn=lambda d: round(d.get("current_energy_raw", 0) * 0.1, 2),
    ),
    FoxESSChargerSensorDescription(
        key="total_energy", name="Total Energy",
        translation_key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:counter",
        value_fn=lambda d: round(d.get("total_energy_raw", 0) * 0.1, 2),
    ),
    # ── Konfigurationssensoren ────────────────────────────────────────────────
    FoxESSChargerSensorDescription(
        key="alarm_code", name="Alarm Code", icon="mdi:alert",
        translation_key="alarm_code",
        value_fn=lambda d: d.get("alarm_code"),
        attrs_fn=lambda d: {"active_alarms": d.get("active_alarms", [])},
    ),
    FoxESSChargerSensorDescription(
        key="fault_code", name="Fault Code", icon="mdi:alert-circle",
        translation_key="fault_code",
        value_fn=lambda d: d.get("fault_code"),
        attrs_fn=lambda d: {"active_faults": d.get("active_faults", [])},
    ),
    FoxESSChargerSensorDescription(
        key="rfid_card", name="RFID Card", icon="mdi:card-account-details",
        translation_key="rfid_card",
        # Card IDs are sensitive - disabled by default for privacy. Users
        # who want it (e.g. to automate per-card charging limits) can
        # enable it manually in the entity settings.
        entity_registry_enabled_default=False,
        value_fn=lambda d: f"{d.get('rfid_card',0):08X}" if d.get("rfid_card", 0) > 0 else "None",
    ),
    # ── Session history ───────────────────────────────────────────────────────
    # Derived by the coordinator from status transitions - the charger itself
    # stores none of this. current_energy resets each session and stop_reason
    # is overwritten before anyone sees it.
    FoxESSChargerSensorDescription(
        key="last_session_energy", name="Last Session Energy",
        translation_key="last_session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:history",
        # Deliberately NOT total_increasing: this is a per-session figure that
        # goes up and down between sessions, not a cumulative counter. Marking
        # it as a total would feed nonsense into long-term statistics.
        value_fn=lambda d: (d.get("last_session") or {}).get("energy_kwh"),
        attrs_fn=lambda d: d.get("last_session") or {},
    ),
    FoxESSChargerSensorDescription(
        key="last_session_duration", name="Last Session Duration",
        translation_key="last_session_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer-outline",
        value_fn=lambda d: (d.get("last_session") or {}).get("duration_min"),
    ),
    # ── Diagnostics ───────────────────────────────────────────────────────────
    # The 2026-08/09 transport desync was invisible until it had already
    # corrupted long-term statistics. Non-zero and climbing here means it is
    # happening again; flat zero means the transport fixes are holding.
    #
    # 2026-09 (second audit): MEASUREMENT, not TOTAL_INCREASING. The
    # underlying counters (txid_mismatches et al. on FoxESSModbusClient) are
    # plain in-memory instance attributes - FoxESSModbusClient is recreated
    # from scratch in async_setup_entry on every integration reload (options
    # change, HA restart, manual reload), so this value legitimately drops
    # back to zero at that point, not just increases. HA's recorder treats a
    # TOTAL_INCREASING sensor's drop as a meter rollover and folds the old
    # value back into the cumulative sum, which would silently inflate this
    # sensor's long-term statistics by however many errors had accumulated
    # before the reload - the opposite of what a diagnostic counter should
    # do. Persisting the counters across reloads (the alternative fix) was
    # considered but left out of this pass: it would mean either extending
    # the 2.2.0 session Store's schema or adding a third Store solely for
    # transport counters, and diagnostics-only state surviving a reload
    # isn't worth that persistence-layer footprint - MEASUREMENT already
    # correctly represents "this process's error count since it last
    # started", which is exactly what the value actually is.
    FoxESSChargerSensorDescription(
        key="transport_errors", name="Transport Errors",
        translation_key="transport_errors",
        icon="mdi:lan-disconnect",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        # Not tied to any single register block - these are the Modbus
        # client's own transport counters, updated every poll regardless of
        # which blocks succeeded or failed.
        block=None,
        value_fn=lambda d: (
            d.get("diag_txid_mismatches", 0)
            + d.get("diag_short_reads", 0)
            + d.get("diag_connection_errors", 0)
            + d.get("diag_malformed_headers", 0)
            + d.get("diag_unit_id_mismatches", 0)
            + d.get("diag_write_echo_mismatches", 0)
        ),
        attrs_fn=lambda d: {
            "txid_mismatches":     d.get("diag_txid_mismatches", 0),
            "short_reads":         d.get("diag_short_reads", 0),
            "connection_errors":   d.get("diag_connection_errors", 0),
            "malformed_headers":   d.get("diag_malformed_headers", 0),
            "unit_id_mismatches":  d.get("diag_unit_id_mismatches", 0),
            "write_echo_mismatches": d.get("diag_write_echo_mismatches", 0),
            "energy_rejections":   d.get("diag_energy_rejections", 0),
            "setpoint_reasserts":  d.get("diag_setpoint_reasserts", 0),
            # Full context on each rejected energy read, so a recorder
            # correction can be computed rather than guessed at later.
            "recent_energy_rejections": d.get("energy_rejection_log", []),
        },
    ),
)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FoxESSChargerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        FoxESSChargerSensor(coordinator, desc, entry) for desc in SENSORS
    ])


class FoxESSChargerSensor(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    entity_description: FoxESSChargerSensorDescription

    def __init__(self, coordinator: FoxESSChargerCoordinator,
                 description: FoxESSChargerSensorDescription, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)
        self._block = description.block

    @property
    def native_value(self) -> StateType:
        if not (self.coordinator.data and self.entity_description.value_fn):
            return None
        value = self.entity_description.value_fn(self.coordinator.data)
        desc = self.entity_description
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (desc.min_plausible is not None or desc.max_plausible is not None
                 or desc.capability_key is not None)
        ):
            lo = desc.min_plausible if desc.min_plausible is not None else float("-inf")
            if desc.capability_key is not None:
                model = self.coordinator.data.get("id_model_code")
                rated = get_capabilities(model)[desc.capability_key]
                hi = rated * desc.capability_margin
            else:
                hi = desc.max_plausible if desc.max_plausible is not None else float("inf")
            if not (lo <= value <= hi):
                _LOGGER.warning(
                    "%s: implausible reading %s (expected %s to %s) - "
                    "reporting unavailable rather than a physically impossible value",
                    desc.key, value, lo, hi,
                )
                return None
        return value

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data:
            return self.entity_description.attrs_fn(self.coordinator.data)
        return {}
