"""Sensor platform: charger telemetry, energy counters, session history and
transport health.

Every sensor is declared as a description: where its value comes from in the
coordinator's observation, how a raw register turns into a state, and which
register block decides its availability. A number that only a garbled frame
could produce (6553.5 V from a 0xFFFF register, say) is reported as unknown
instead of being shown.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FoxESSBlockAvailabilityMixin, FoxESSChargerCoordinator, build_device_info
from .const import (
    BLOCK_CONFIG,
    BLOCK_STATUS,
    CC_STATUS_MAP,
    CP_STATUS_MAP,
    DOMAIN,
    LOCK_STATUS_MAP,
    PHASE_SEQ_MAP,
    STATUS_MAP,
    STOP_REASON_MAP,
    WORK_MODE_MAP,
    decode_enum,
    get_capabilities,
)
from .protocol import STATUS_PHASE_SWITCHING
from .session import valid_status

_LOGGER = logging.getLogger(__name__)

Observation = Mapping[str, Any]

# Status values the sensor can show: the register's own table plus the
# phase-switching state three-phase units report while changing over.
STATUS_SENSOR_MAP = {**STATUS_MAP, STATUS_PHASE_SWITCHING: "phase_switching"}

# Counters summed into the Transport Errors state. stale_replies is left out
# on purpose: discarding a late reply is the client coping with a slow
# charger, not a framing failure.
_FRAMING_ERRORS = (
    "txid_mismatches", "short_reads", "connection_errors",
    "malformed_headers", "unit_id_mismatches", "write_echo_mismatches",
)
# Controller counters shown as attributes. The later ones are 2.4.3 names the
# 3.x controller does not produce: they read None (unknown), never a made-up 0.
_CONTROL_COUNTERS = (
    "refreshes", "refresh_failures", "stop_fallbacks", "persist_failures",
    "setpoint_reasserts", "setpoint_drift_events", "heartbeat_write_failures",
    "stop_pending", "stop_write_failures", "stop_retries",
)


@dataclass(frozen=True, kw_only=True)
class FoxESSSensorEntityDescription(SensorEntityDescription):
    """How one sensor reads its state out of an observation."""

    value_fn: Callable[[Observation], StateType] = lambda _: None
    attrs_fn: Callable[[Observation], dict] = lambda _: {}
    # Readings outside [valid_min, valid_max] cannot be real measurements on
    # this hardware and are reported as unknown. None leaves that side open.
    valid_min: float | None = None
    valid_max: float | None = None
    # When set, the upper limit is this MODEL_CAPABILITIES rating of the
    # detected model times rated_headroom (valid_max is then ignored), so a
    # 22 kW reading is accepted on an A022 but not on the 7.3 kW A7300. The
    # 1.5 headroom matches the energy guard's safety factor.
    rated_key: str | None = None
    rated_headroom: float = 1.5
    # Register block whose freshness gates availability (const.BLOCK_*);
    # None = not tied to any block.
    block: str | None = BLOCK_STATUS

    def valid_range(self, data: Observation) -> tuple[float, float] | None:
        """Accepted (low, high) range for a numeric reading, or None when
        this sensor has no plausibility limits."""
        if self.valid_min is None and self.valid_max is None and self.rated_key is None:
            return None
        low = -math.inf if self.valid_min is None else self.valid_min
        if self.rated_key is not None:
            rating = get_capabilities(data.get("id_model_code"))[self.rated_key]
            return low, rating * self.rated_headroom
        return low, math.inf if self.valid_max is None else self.valid_max


# ── value helpers ────────────────────────────────────────────────────────────
# A missing register reads as 0 in the scaled helpers below; that has always
# been the behaviour and the plausibility limits apply to the result.

def _scaled(key: str, digits: int = 1) -> Callable[[Observation], float]:
    """Register holding tenths of the unit."""
    return lambda data: round(data.get(key, 0) * 0.1, digits)


def _decoded(
    key: str, table: dict[int, str], *, enum: bool = True,
) -> Callable[[Observation], str | None]:
    """Enum register through its code table. For an ENUM sensor an
    unrecognised code shows the translatable "unknown" option; otherwise the
    state is simply left empty. A missing reading is always None."""
    unknown = "unknown" if enum else None
    return lambda data: decode_enum(data.get(key), table, key, unknown_label=unknown)


def _firmware(data: Observation) -> str:
    raw = data.get("software_version", 0)
    return f"{raw >> 8}.{raw & 0xFF}"


def _port_celsius(data: Observation) -> float | None:
    raw = data.get("port_temp_raw")
    if raw in (None, 65535):  # 65535: no probe fitted
        return None
    return round(raw * 0.1 - 50, 1)


def _board_celsius(data: Observation) -> float:
    return round(data.get("ambient_temp_raw", 0) * 0.1 - 50, 1)


def _kwh(key: str) -> Callable[[Observation], float | None]:
    """Energy counter in kWh. A counter the energy guard rejected with no
    earlier good value is absent and stays unknown: a 0 on a
    total_increasing sensor would read as a meter reset."""
    def value(data: Observation) -> float | None:
        raw = data.get(key)
        return None if raw is None else round(raw * 0.1, 2)
    return value


def _rfid(data: Observation) -> str:
    card = data.get("rfid_card", 0)
    return f"{card:08X}" if card > 0 else "None"


def _last_session(field: str) -> Callable[[Observation], Any]:
    return lambda data: (data.get("last_session") or {}).get(field)


def _transport_errors(data: Observation) -> int:
    return sum(data.get(f"diag_{name}", 0) for name in _FRAMING_ERRORS)


def _transport_details(data: Observation) -> dict:
    details = {name: data.get(f"diag_{name}", 0) for name in _FRAMING_ERRORS}
    details["stale_replies"] = data.get("diag_stale_replies", 0)
    details["energy_rejections"] = data.get("diag_energy_rejections", 0)
    details.update({name: data.get(f"diag_{name}") for name in _CONTROL_COUNTERS})
    # Full context of recent energy rejections, so a recorder correction can
    # be worked out later rather than guessed.
    details["recent_energy_rejections"] = data.get("energy_rejection_log", [])
    return details


# Why the car is not charging, first match wins. A latch that clears by
# itself is named by its cause; any other cause counts as a charger fault.
_LATCH_REASONS = {"stop_failed": "stop_failed", "comms": "link_problem"}
STATUS_REASONS = (
    "charger_offline", "needs_attention", "stop_failed", "charger_fault", "link_problem",
    "link_uncertain", "car_not_connected", "charger_locked", "charging_off", "waiting_for_car",
    "starting", "charging",
)


def _status_reason(data: Observation) -> str:
    status = valid_status(data)
    cause, phase = data.get("latch_cause"), data.get("control_phase")
    # status_block_ok is absent when the poll returned nothing, so anything but
    # True means this poll got no fresh status (stale carried-over fields).
    if status is None or data.get("status_block_ok") is not True:
        return "charger_offline"
    if data.get("needs_human"):
        return "needs_attention"
    if cause:
        return _LATCH_REASONS.get(cause, "charger_fault")
    if status == 6:  # the charger itself reports a fault
        return "charger_fault"
    if phase in ("uncertain", "faulted"):  # protective zero, link or cap unconfirmed
        return "link_uncertain"
    if status == 0 or data.get("cc_status") == 0:
        return "car_not_connected"
    if status == 8:  # the charger refuses to charge
        return "charger_locked"
    if data.get("intent_enabled") is False or phase in ("paused", "paused_below_minimum", "stopped"):
        return "charging_off"
    if status == 3:
        return "charging"
    return "starting" if status == 2 else "waiting_for_car"


def _status_reason_attrs(data: Observation) -> dict:
    attrs = {"latch_cause": data.get("latch_cause"), "needs_human": bool(data.get("needs_human"))}
    retry = data.get("latch_release_in")
    if isinstance(retry, (int, float)) and not isinstance(retry, bool):
        attrs["auto_retry_in"] = retry
    return attrs


def _enum_options(table: dict[int, str]) -> list[str]:
    return [*table.values(), "unknown"]


# ── descriptions ─────────────────────────────────────────────────────────────

def _voltage(phase: int, *, enabled: bool) -> FoxESSSensorEntityDescription:
    key = f"l{phase}_voltage"
    return FoxESSSensorEntityDescription(
        key=key, name=f"L{phase} Voltage", translation_key=key, icon="mdi:flash",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=enabled,
        # 300 V covers any single/split-phase mains with margin and still
        # catches a 0xFFFF register (6553.5 V).
        valid_min=0, valid_max=300,
        value_fn=_scaled(f"{key}_raw"),
    )


def _current(key: str, name: str, raw_key: str, *, measured: bool = True,
             enabled: bool = True) -> FoxESSSensorEntityDescription:
    return FoxESSSensorEntityDescription(
        key=key, name=name, translation_key=key, icon="mdi:current-ac",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT if measured else None,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=enabled,
        # 40 A: the highest rated current of any known model (32 A) plus
        # headroom; anything above is a bad read.
        valid_min=0, valid_max=40,
        value_fn=_scaled(raw_key),
    )


def _power(key: str, name: str, raw_key: str, *, icon: str, digits: int,
           measured: bool) -> FoxESSSensorEntityDescription:
    return FoxESSSensorEntityDescription(
        key=key, name=name, translation_key=key, icon=icon,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT if measured else None,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        valid_min=0, rated_key="max_power_kw",
        value_fn=_scaled(raw_key, digits),
    )


def _enum_sensor(key: str, name: str, icon: str, data_key: str, table: dict[int, str], *,
                 translation_key: str | None = None, block: str = BLOCK_STATUS,
                 enabled: bool = True) -> FoxESSSensorEntityDescription:
    return FoxESSSensorEntityDescription(
        key=key, name=name, translation_key=translation_key or key, icon=icon,
        device_class=SensorDeviceClass.ENUM, options=_enum_options(table),
        entity_registry_enabled_default=enabled, block=block,
        value_fn=_decoded(data_key, table),
    )


SENSORS: tuple[FoxESSSensorEntityDescription, ...] = (
    # Identity (diagnostic).
    FoxESSSensorEntityDescription(
        key="software_version", name="Software Version", translation_key="software_version",
        icon="mdi:information-outline", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_firmware,
    ),
    FoxESSSensorEntityDescription(
        key="device_address", name="Device Address", translation_key="device_address",
        icon="mdi:identifier", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("device_address"),
    ),
    FoxESSSensorEntityDescription(
        key="id_serial_number", name="Serial Number", translation_key="id_serial_number",
        icon="mdi:card-account-details-outline", entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("id_serial_number"),
    ),
    # State. Each table gains an "unknown" option so an unrecognised code is
    # a visible, translated state (and never mistaken for a real one), while
    # "no reading yet" stays HA's own unknown.
    # "phase_switching" comes after the original states so they keep their
    # option positions.
    _enum_sensor("status", "Status", "mdi:ev-station", "status", STATUS_SENSOR_MAP),
    _enum_sensor("cp_status", "CP Status", "mdi:connection", "cp_status", CP_STATUS_MAP),
    _enum_sensor("cc_status", "CC Status", "mdi:cable-data", "cc_status", CC_STATUS_MAP),
    _enum_sensor("lock_status", "Lock Status", "mdi:lock", "lock_status", LOCK_STATUS_MAP),
    # Read from the settings block. The entity key ("work_mode_sensor") and
    # translation key ("work_mode") differ on purpose.
    _enum_sensor("work_mode_sensor", "Work Mode", "mdi:cog", "work_mode", WORK_MODE_MAP,
                 translation_key="work_mode", block=BLOCK_CONFIG),
    _enum_sensor("phase_sequence", "Phase Sequence", "mdi:electric-switch", "phase_sequence",
                 PHASE_SEQ_MAP, enabled=False),  # phase-switch box only
    # A plain text sensor (no options), so an unrecognised code is just empty.
    FoxESSSensorEntityDescription(
        key="stop_reason", name="Stop Reason", translation_key="stop_reason",
        icon="mdi:information",
        value_fn=_decoded("stop_reason", STOP_REASON_MAP, enum=False),
    ),
    # One answer to "why is the car not charging right now?". Not tied to a
    # register block, so it can still say the charger is offline.
    FoxESSSensorEntityDescription(
        key="charging_status_reason", name="Charging Status Reason",
        translation_key="charging_status_reason", icon="mdi:help-circle-outline",
        device_class=SensorDeviceClass.ENUM, options=list(STATUS_REASONS),
        entity_category=EntityCategory.DIAGNOSTIC, block=None,
        value_fn=_status_reason, attrs_fn=_status_reason_attrs,
    ),
    # Temperatures. -40..120 °C is far outside anything the electronics can
    # reach yet well inside what a corrupt register decodes to.
    FoxESSSensorEntityDescription(
        key="port_temperature", name="Port Temperature", translation_key="port_temperature",
        icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # Off by default: the A7300P1 has no port probe and only ever reports
        # 65535, which would leave a permanently unknown entity. The firmware's
        # port over-temperature protection (fault bit 4) does not need it.
        entity_registry_enabled_default=False,
        valid_min=-40, valid_max=120,
        value_fn=_port_celsius,
    ),
    FoxESSSensorEntityDescription(
        # The protocol calls this "ambient", but the sensor sits on the board
        # inside the enclosure: 23.6 °C idle vs 55.5 °C at 30 A on the same
        # unit, same afternoon. Named Internal so nobody reads it as the
        # garage temperature.
        key="ambient_temperature", name="Internal Temperature",
        translation_key="ambient_temperature",
        icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        valid_min=-40, valid_max=120,
        value_fn=_board_celsius,
    ),
    # Mains, per phase. L2/L3 only exist on three-phase units.
    _voltage(1, enabled=True),
    _voltage(2, enabled=False),
    _voltage(3, enabled=False),
    _current("l1_current", "L1 Current", "l1_current_raw"),
    _current("l2_current", "L2 Current", "l2_current_raw", enabled=False),
    _current("l3_current", "L3 Current", "l3_current_raw", enabled=False),
    # Power: limited by the detected model's rating (A7300 until the model
    # has been read).
    _power("charging_power", "Charging Power", "power_raw",
           icon="mdi:lightning-bolt", digits=2, measured=True),
    _power("max_supported_power", "Max Supported Power", "max_power_raw",
           icon="mdi:lightning-bolt-outline", digits=1, measured=False),
    _power("min_supported_power", "Min Supported Power", "min_power_raw",
           icon="mdi:lightning-bolt-outline", digits=1, measured=False),
    _current("max_supported_current", "Max Supported Current", "max_current_raw",
             measured=False),
    _current("min_supported_current", "Min Supported Current", "min_current_raw",
             measured=False),
    # Energy counters, already filtered by the energy guard.
    FoxESSSensorEntityDescription(
        key="current_session_energy", name="Current Session Energy",
        translation_key="current_session_energy", icon="mdi:counter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_kwh("current_energy_raw"),
    ),
    FoxESSSensorEntityDescription(
        key="total_energy", name="Total Energy", translation_key="total_energy",
        icon="mdi:counter", device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_kwh("total_energy_raw"),
    ),
    # Alarm/fault registers are bitmasks; the active condition names go in
    # an attribute.
    FoxESSSensorEntityDescription(
        key="alarm_code", name="Alarm Code", translation_key="alarm_code", icon="mdi:alert",
        value_fn=lambda data: data.get("alarm_code"),
        attrs_fn=lambda data: {"active_alarms": data.get("active_alarms", [])},
    ),
    FoxESSSensorEntityDescription(
        key="fault_code", name="Fault Code", translation_key="fault_code",
        icon="mdi:alert-circle",
        value_fn=lambda data: data.get("fault_code"),
        attrs_fn=lambda data: {"active_faults": data.get("active_faults", [])},
    ),
    FoxESSSensorEntityDescription(
        key="rfid_card", name="RFID Card", translation_key="rfid_card",
        icon="mdi:card-account-details",
        # Card IDs are personal; enable it to automate per card.
        entity_registry_enabled_default=False,
        value_fn=_rfid,
    ),
    # The last completed session, as recorded by the coordinator's session
    # tracker: the charger keeps none of this (its session counter restarts
    # and the stop reason is overwritten).
    FoxESSSensorEntityDescription(
        key="last_session_energy", name="Last Session Energy",
        translation_key="last_session_energy", icon="mdi:history",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # No state class: a per-session figure moves both ways between
        # sessions and must not feed long-term statistics as a total.
        value_fn=_last_session("energy_kwh"),
        attrs_fn=lambda data: data.get("last_session") or {},
    ),
    FoxESSSensorEntityDescription(
        key="last_session_duration", name="Last Session Duration",
        translation_key="last_session_duration", icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_last_session("duration_min"),
    ),
    # Modbus framing problems since the integration last (re)loaded. A
    # transport desync once went unnoticed until it had corrupted long-term
    # statistics; a climbing count here is the early warning. MEASUREMENT,
    # not TOTAL_INCREASING: the counters live in memory and restart at 0 on
    # every reload, which the recorder would otherwise treat as a meter
    # rollover and add to the sum again.
    FoxESSSensorEntityDescription(
        key="transport_errors", name="Transport Errors", translation_key="transport_errors",
        icon="mdi:lan-disconnect", entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        block=None,  # client counters, updated whichever blocks were read
        value_fn=_transport_errors,
        attrs_fn=_transport_details,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one entity per sensor description."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [FoxESSChargerSensor(coordinator, description, entry) for description in SENSORS]
    )


class FoxESSChargerSensor(FoxESSBlockAvailabilityMixin, CoordinatorEntity, SensorEntity):
    """A sensor whose state is computed from the latest observation."""

    _attr_has_entity_name = True
    entity_description: FoxESSSensorEntityDescription

    def __init__(
        self,
        coordinator: FoxESSChargerCoordinator,
        description: FoxESSSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)
        self._block = description.block

    @property
    def available(self) -> bool:
        # The status reason must stay visible while the charger is offline.
        if self.entity_description.key == "charging_status_reason":
            return self.coordinator.data is not None
        return super().available

    @property
    def native_value(self) -> StateType:
        data = self.coordinator.data
        if not data:
            return None
        description = self.entity_description
        value = description.value_fn(data)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        limits = description.valid_range(data)
        if limits is None or limits[0] <= value <= limits[1]:
            return value
        _LOGGER.warning(
            "%s: implausible reading %s (accepted range %s to %s); reporting "
            "it as unknown instead", description.key, value, *limits,
        )
        return None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return self.entity_description.attrs_fn(data) if data else {}
