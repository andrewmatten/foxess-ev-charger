"""Snapshots of the integration's externally visible contract.

Recorded from the 3.0.7 release, before the entity platforms, const.py,
config_flow.py, modbus_client.py and the translations were rewritten, so
that rewrite (and any later refactor) can be shown not to change what
users and existing installs depend on:

- device/entity registry entries and entity states, in English and in
  German (German entity IDs and names come from de.json);
- what every sensor, binary sensor, number, select and switch reports for
  a range of raw register values (including sentinels, unknown enum
  values and missing keys), which register block it depends on, and which
  controller call each user command becomes;
- the config and options flow: fields, defaults, stored data, errors;
- the translation keys plus every entity name and state label;
- the Modbus TCP frames the client puts on the wire.

Update the snapshots only for a deliberate contract change:

    pytest tests/test_contract_snapshot.py --snapshot-update
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension

from custom_components.foxess_charger import const
from custom_components.foxess_charger.adapter_api import ControlError
from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS, FoxESSBinarySensor
from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.modbus_client import FoxESSModbusClient, decode_ascii
from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber
from custom_components.foxess_charger.select import FoxESSPhaseSelect, FoxESSWorkModeSelect
from custom_components.foxess_charger.sensor import SENSORS, FoxESSChargerSensor
from custom_components.foxess_charger.switch import (
    FoxESSAutoPhaseSwitchSwitch,
    FoxESSChargingSwitch,
    FoxESSLockSwitch,
)

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "contractentry01"
BLOCKS = (const.BLOCK_STATUS, const.BLOCK_CONFIG, const.BLOCK_PHASE_BOX)
TRANSLATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "foxess_charger", "translations",
)


@pytest.fixture
def snapshot(snapshot):
    """syrupy's fixture with HA's serializer (registry IDs, timestamps and
    flow IDs masked). Declared here because the plugin load order lets
    syrupy's plain fixture shadow the HA plugin's version."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


def _outcome(fn):
    """Value of ``fn()``, or the exception it raised (part of the contract
    too: a value_fn that raised before must still raise)."""
    try:
        return fn()
    except Exception as err:  # noqa: BLE001
        return f"raises {type(err).__name__}"


# ── raw observations fed to the entities ─────────────────────────────────────

_IDLE = {
    "device_address": 1, "software_version": 0x0108, "stop_reason": 1,
    "status": 1, "status_valid": True, "cp_status": 1, "cc_status": 1,
    "cc_status_valid": True, "port_temp_raw": 65535, "ambient_temp_raw": 736,
    "l1_voltage_raw": 2412, "l2_voltage_raw": 0, "l3_voltage_raw": 0,
    "l1_current_raw": 0, "l2_current_raw": 0, "l3_current_raw": 0,
    "power_raw": 0, "lock_status": 0, "phase_sequence": 0,
    "max_power_raw": 73, "min_power_raw": 14, "max_current_raw": 320,
    "min_current_raw": 60, "alarm_code": 0, "active_alarms": [],
    "total_energy_raw": 8013, "current_energy_raw": 0, "fault_code": 0,
    "active_faults": [], "rfid_card": 0, "work_mode": 1,
    "max_charging_current_raw": 320, "max_charging_power_raw": 73,
    "allowed_charge_time": 0xFFFF, "allowed_charge_energy": 0xFFFF,
    "time_validity": 60, "default_current_raw": 60,
    "auto_phase_switch": 0, "min_switch_interval": 5,
    "id_model_code": "A7300P1-E-B-WO", "id_serial_number": "SN0001",
}

SAMPLES: dict[str, dict] = {
    "empty": {},
    "idle": _IDLE,
    "charging": {
        **_IDLE, "status": 3, "cp_status": 3, "stop_reason": 0, "power_raw": 69,
        "l1_current_raw": 301, "l1_voltage_raw": 2305, "ambient_temp_raw": 1055,
        "port_temp_raw": 812, "current_energy_raw": 57, "total_energy_raw": 8070,
        "lock_status": 1, "rfid_card": 0x00A1B2C3, "allowed_charge_time": 90,
        "allowed_charge_energy": 20, "max_charging_current_raw": 161,
        "max_charging_power_raw": 37, "auto_phase_switch": 1,
        "min_switch_interval": 10, "time_validity": 180,
        "default_current_raw": 80,
        "last_session": {
            "ended": "2026-10-01T09:00:00+00:00", "duration_min": 131.5,
            "energy_kwh": 14.3, "avg_power_kw": 6.52, "peak_power_kw": 7.0,
            "stop_reason": "command",
        },
        "diag_txid_mismatches": 2, "diag_short_reads": 1,
        "diag_connection_errors": 3, "diag_malformed_headers": 0,
        "diag_unit_id_mismatches": 0, "diag_write_echo_mismatches": 1,
        "diag_stale_replies": 4, "diag_energy_rejections": 1,
        "diag_refreshes": 10, "diag_refresh_failures": 1,
        "diag_stop_fallbacks": 0, "diag_persist_failures": 0,
        "energy_rejection_log": [{"key": "total_energy_raw", "rejected_raw": 65800}],
    },
    "paused_by_vehicle": {**_IDLE, "status": 4, "cp_status": 2},
    "phase_switching": {**_IDLE, "status": 9},
    "fault": {
        **_IDLE, "status": 6, "cp_status": 0, "fault_code": 0b1010,
        "active_faults": ["overvoltage", "overcurrent"], "alarm_code": 0b101,
        "active_alarms": ["card_reader", "phase_loss"], "stop_reason": 11,
    },
    "fault_status_only": {**_IDLE, "status": 6, "fault_code": 0},
    "unknown_enums": {
        **_IDLE, "status": None, "status_valid": False, "status_raw": 7,
        "cp_status": 9, "cc_status": None, "cc_status_valid": False,
        "cc_status_raw": 2, "lock_status": 3, "work_mode": 5,
        "phase_sequence": 4, "stop_reason": 99,
    },
    "sentinels": {
        **_IDLE, "status": None, "status_valid": False, "status_raw": 65535,
        "cp_status": 65535, "cc_status": None, "cc_status_valid": False,
        "cc_status_raw": 65535, "port_temp_raw": 65535,
        "ambient_temp_raw": 65535, "l1_voltage_raw": 65535,
        "l1_current_raw": 65535, "power_raw": 65535, "lock_status": 65535,
        "phase_sequence": 65535, "max_power_raw": 65535,
        "min_power_raw": 65535, "max_current_raw": 65535,
        "min_current_raw": 65535, "alarm_code": 65535,
        "fault_code": 0xFFFFFFFF, "rfid_card": 0xFFFFFFFF,
        "total_energy_raw": 0xFFFFFFFF, "current_energy_raw": 0xFFFFFFFF,
        "work_mode": 65535, "allowed_charge_time": 65535,
        "allowed_charge_energy": 65535, "time_validity": 65535,
        "default_current_raw": 65535, "max_charging_current_raw": 65535,
        "max_charging_power_raw": 65535, "auto_phase_switch": 65535,
        "min_switch_interval": 65535,
    },
    "zeros": {key: 0 for key in _IDLE if key not in (
        "active_alarms", "active_faults", "id_model_code", "id_serial_number",
        "status_valid", "cc_status_valid")},
    "edge_of_bounds": {
        **_IDLE, "port_temp_raw": 100, "ambient_temp_raw": 1700,
        "l1_voltage_raw": 3000, "l2_voltage_raw": 3001, "l1_current_raw": 400,
        "l2_current_raw": 401, "power_raw": 110, "max_power_raw": 109,
        "min_power_raw": 0, "max_current_raw": 400, "min_current_raw": 0,
    },
    "three_phase_a022": {
        **_IDLE, "id_model_code": "A022-SOME-SUFFIX", "power_raw": 220,
        "max_power_raw": 220, "max_charging_power_raw": 220,
        "max_charging_current_raw": 320, "l2_voltage_raw": 2301,
        "l3_voltage_raw": 2299, "l2_current_raw": 155, "l3_current_raw": 154,
    },
    "three_phase_a011": {
        **_IDLE, "id_model_code": "a011xyz", "power_raw": 165,
        "max_power_raw": 110,
    },
    "missing_keys": {"status": 3, "status_valid": True},
    "none_values": {
        **{key: None for key in _IDLE},
        "status_valid": False, "cc_status_valid": False,
    },
}

# The real protocol flags every successful status read; only "empty" is a failed poll.
for _name, _sample in SAMPLES.items():
    if _sample:
        _sample["status_block_ok"] = True


class _Entry:
    entry_id = "valuesentry"
    options = {"phase_box_fitted": True}  # the phase select is off without it


class _Coordinator:
    """Just enough of FoxESSChargerCoordinator for one entity."""

    def __init__(self, data, controller=None, *, fresh=BLOCKS, ok=True) -> None:
        self.data = data
        self.controller = controller or _Controller()
        self.last_update_success = ok
        self._fresh = set(fresh)
        self.refreshes = 0
        self.user_pauses = 0

    def block_is_fresh(self, block):
        return block is None or block in self._fresh

    async def async_refresh(self):
        self.refreshes += 1

    async def async_request_refresh(self):
        self.refreshes += 1

    async def async_user_paused(self):
        self.user_pauses += 1

    def async_add_listener(self, *_args, **_kwargs):
        return lambda: None


class _Controller:
    """Records the calls entities make; answers like the real controller."""

    def __init__(self, *, enabled=True, power=None, current=None, outcome="confirmed",
                 fail=False) -> None:
        self.intent_enabled = enabled
        self.desired_power_raw = power
        self.desired_current_raw = current
        self.phase = "enabled" if enabled else "paused"
        self.latch_cause, self.needs_human = None, False
        self.calls: list[tuple] = []
        self._outcome = outcome
        self._fail = fail

    async def _answer(self, *call):
        self.calls.append(call)
        if self._fail:
            raise ControlError("refused (contract test)")
        return SimpleNamespace(outcome=self._outcome, revision=1, observed=None)

    def async_set_power(self, raw):
        return self._answer("set_power", raw)

    def async_set_current(self, raw):
        return self._answer("set_current", raw)

    def async_set_register(self, address, value):
        return self._answer("set_register", hex(address), value)

    def async_enable(self, *, clear_hard=False):
        return self._answer("enable")

    def async_pause(self):
        return self._answer("pause")


def _static(entity) -> dict:
    """Entity metadata HA reads when it registers the entity."""
    def attr(name):
        return _outcome(lambda: getattr(entity, name))

    return {
        "unique_id": entity.unique_id,
        "name": attr("name"),
        "translation_key": attr("translation_key"),
        "has_entity_name": entity.has_entity_name,
        "icon": attr("icon"),
        "device_class": attr("device_class"),
        "entity_category": attr("entity_category"),
        "enabled_default": entity.entity_registry_enabled_default,
        "device_info": dict(entity.device_info),
        # Which register block's freshness drives availability.
        "available_when_stale": {
            block: entity.__class__.available.fget(
                _with_coordinator(entity, _Coordinator(
                    entity.coordinator.data, entity.coordinator.controller,
                    fresh=[b for b in BLOCKS if b != block])))
            for block in BLOCKS
        },
        "available_when_coordinator_failed": entity.__class__.available.fget(
            _with_coordinator(entity, _Coordinator(
                entity.coordinator.data, entity.coordinator.controller, ok=False))),
        "long_service_context": "_CONTEXT_RECENT_SECONDS" in dir(entity),
    }


def _with_coordinator(entity, coordinator):
    clone = object.__new__(type(entity))
    clone.__dict__.update(entity.__dict__)
    clone.coordinator = coordinator
    return clone


def _no_state_write(entity):
    entity.async_write_ha_state = lambda: None
    return entity


# ── per-platform value contracts ─────────────────────────────────────────────

def test_sensor_contract(snapshot):
    result = {}
    for desc in SENSORS:
        entity = FoxESSChargerSensor(_Coordinator(SAMPLES["idle"]), desc, _Entry())
        info = _static(entity)
        info.update(
            state_class=_outcome(lambda: entity.state_class),
            unit=_outcome(lambda: entity.native_unit_of_measurement),
            options=_outcome(lambda: entity.options),
        )
        values = {}
        for name, data in SAMPLES.items():
            entity.coordinator = _Coordinator(data)
            values[name] = {
                "value": _outcome(lambda: entity.native_value),
                "attributes": _outcome(lambda: entity.extra_state_attributes),
            }
        info["values"] = values
        result[desc.key] = info
    assert result == snapshot


def test_binary_sensor_contract(snapshot):
    result = {}
    for desc in BINARY_SENSORS:
        entity = FoxESSBinarySensor(_Coordinator(SAMPLES["idle"]), desc, _Entry())
        info = _static(entity)
        values = {}
        for name, data in SAMPLES.items():
            entity.coordinator = _Coordinator(data)
            values[name] = _outcome(lambda: entity.is_on)
        info["values"] = values
        result[desc.key] = info
    assert result == snapshot


NUMBER_INPUTS = {
    "max_charging_current": [6.0, 6.05, 6.15, 10.0, 16.25, 31.95, 32.0],
    "max_charging_power": [0.0, 1.35, 1.4, 2.25, 3.7, 7.3],
    "allowed_charge_time": [0, 1, 59.99, 90.7, 1440],
    "allowed_charge_energy": [0, 20, 20.9, 999],
    "time_validity": [10, 60, 180, 255],
    "default_current": [6.0, 6.05, 8.0, 32.0],
    "min_switch_interval": [5, 10.5, 30],
}


async def test_number_contract(snapshot):
    result = {}
    for desc in NUMBERS:
        entity = FoxESSNumber(_Coordinator(SAMPLES["idle"]), desc, _Entry())
        info = _static(entity)
        info.update(
            unit=_outcome(lambda: entity.native_unit_of_measurement),
            min=_outcome(lambda: entity.native_min_value),
            step=_outcome(lambda: entity.native_step),
            mode=_outcome(lambda: entity.mode),
        )
        reads = {}
        for name, data in SAMPLES.items():
            for desired in ((None, None), (22, 100)):
                ctrl = _Controller(power=desired[0], current=desired[1])
                entity.coordinator = _Coordinator(data, ctrl)
                reads[f"{name}/desired={desired}"] = {
                    "value": _outcome(lambda: entity.native_value),
                    "max": _outcome(lambda: entity.native_max_value),
                    "attributes": _outcome(lambda: entity.extra_state_attributes),
                }
        info["reads"] = reads
        writes = {}
        for value in NUMBER_INPUTS[desc.key]:
            for outcome in ("confirmed", "staged", "superseded", "error"):
                ctrl = _Controller(
                    power=None, current=None,
                    outcome="confirmed" if outcome == "error" else outcome,
                    fail=outcome == "error",
                )
                entity = _no_state_write(
                    FoxESSNumber(_Coordinator(SAMPLES["charging"], ctrl), desc, _Entry()))
                try:
                    await entity.async_set_native_value(value)
                    error = None
                except Exception as err:  # noqa: BLE001
                    error = f"{type(err).__name__}: {err}"
                writes[f"{value}/{outcome}"] = {
                    "calls": ctrl.calls,
                    "error": error,
                    "refreshes": entity.coordinator.refreshes,
                    "attributes": entity.extra_state_attributes,
                }
        info["writes"] = writes
        result[desc.key] = info
    assert result == snapshot


SELECTS = {
    "work_mode": (FoxESSWorkModeSelect, ["Controlled", "Plug&Charge", "Locked"]),
    "phase_sequence": (FoxESSPhaseSelect, ["three_phase", "l2_single_phase", "l3_single_phase"]),
}


async def test_select_contract(snapshot):
    result = {}
    for key, (cls, options) in SELECTS.items():
        entity = cls(_Coordinator(SAMPLES["idle"]), _Entry())
        info = _static(entity)
        info["options"] = entity.options
        values = {}
        for name, data in SAMPLES.items():
            entity.coordinator = _Coordinator(data)
            values[name] = _outcome(lambda: entity.current_option)
        info["values"] = values
        writes = {}
        for option in options:
            for fail in (False, True):
                ctrl = _Controller(fail=fail)
                entity = _no_state_write(cls(_Coordinator(SAMPLES["idle"], ctrl), _Entry()))
                try:
                    await entity.async_select_option(option)
                    error = None
                except Exception as err:  # noqa: BLE001
                    error = f"{type(err).__name__}: {err}"
                writes[f"{option}/{'error' if fail else 'ok'}"] = {
                    "calls": ctrl.calls, "error": error,
                    "refreshes": entity.coordinator.refreshes,
                }
        info["writes"] = writes
        result[key] = info
    assert result == snapshot


SWITCHES = {
    "charging": FoxESSChargingSwitch,
    "lock": FoxESSLockSwitch,
    "auto_phase_switch": FoxESSAutoPhaseSwitchSwitch,
}


async def test_switch_contract(snapshot):
    result = {}
    for key, cls in SWITCHES.items():
        entity = cls(_Coordinator(SAMPLES["idle"]), _Entry())
        info = _static(entity)
        values = {}
        for name, data in SAMPLES.items():
            for enabled in (True, False):
                ctrl = _Controller(enabled=enabled)
                entity.coordinator = _Coordinator(data, ctrl)
                values[f"{name}/intent_enabled={enabled}"] = {
                    "is_on": _outcome(lambda: entity.is_on),
                    "attributes": _outcome(lambda: entity.extra_state_attributes),
                }
        info["values"] = values
        writes = {}
        for action in ("turn_on", "turn_off"):
            for outcome in ("confirmed", "staged", "superseded", "error"):
                ctrl = _Controller(
                    outcome="confirmed" if outcome == "error" else outcome,
                    fail=outcome == "error",
                )
                entity = _no_state_write(cls(_Coordinator(SAMPLES["charging"], ctrl), _Entry()))
                try:
                    await getattr(entity, f"async_{action}")()
                    error = None
                except Exception as err:  # noqa: BLE001
                    error = f"{type(err).__name__}: {err}"
                writes[f"{action}/{outcome}"] = {
                    "calls": ctrl.calls, "error": error,
                    "refreshes": entity.coordinator.refreshes,
                    "user_pauses": entity.coordinator.user_pauses,
                }
        info["writes"] = writes
        result[key] = info
    assert result == snapshot


# ── const: the values other modules and stored data rely on ─────────────────

CONST_NAMES = (
    "DOMAIN", "PLATFORMS", "CONF_HOST", "CONF_PORT", "CONF_SLAVE_ID",
    "DEFAULT_PORT", "DEFAULT_SLAVE_ID", "DEFAULT_SCAN_INTERVAL",
    "REG_STATUS_BLOCK_START", "REG_STATUS_BLOCK_COUNT",
    "REG_DEVICE_ADDRESS", "REG_SOFTWARE_VER", "REG_STOP_REASON", "REG_STATUS",
    "REG_CP_STATUS", "REG_CC_STATUS", "REG_PORT_TEMP", "REG_AMBIENT_TEMP",
    "REG_L1_VOLTAGE", "REG_L2_VOLTAGE", "REG_L3_VOLTAGE", "REG_L1_CURRENT",
    "REG_L2_CURRENT", "REG_L3_CURRENT", "REG_ACTIVE_POWER", "REG_LOCK_STATUS",
    "REG_PHASE_SEQUENCE", "REG_MAX_POWER", "REG_MIN_POWER", "REG_MAX_CURRENT",
    "REG_MIN_CURRENT", "REG_ALARM_CODE", "REG_TOTAL_ENERGY",
    "REG_CURRENT_ENERGY", "REG_FAULT_CODE", "REG_RFID_CARD",
    "REG_ID_MODEL_CODE", "REG_ID_SERIAL_NUMBER", "REG_WORK_MODE",
    "REG_MAX_CHARGING_CURRENT", "REG_MAX_CHARGING_POWER",
    "REG_ALLOWED_CHARGE_TIME", "REG_ALLOWED_CHARGE_ENERGY", "REG_TIME_VALIDITY",
    "REG_DEFAULT_CURRENT", "REG_AUTO_PHASE_SWITCH", "REG_MIN_SWITCH_INTERVAL",
    "REG_LOCK_CONTROL", "REG_CHARGING_CONTROL", "REG_PHASE_SWITCHING",
    "REG_RESTART", "STATUS_MAP", "CP_STATUS_MAP", "WORK_MODE_MAP",
    "WORK_MODE_SELECT_OPTIONS", "CC_STATUS_MAP", "LOCK_STATUS_MAP",
    "FAULT_BITS", "ALARM_BITS", "PHASE_SEQ_MAP", "STOP_REASON_MAP",
    "SESSION_ACTIVE_STATUSES", "EVENT_SESSION_COMPLETED",
    "MAX_STATUS_READ_FAILURES", "SETPOINT_REASSERT_MIN_INTERVAL",
    "DEFAULT_TIME_VALIDITY", "FIRMWARE_MAX_TIME_VALIDITY", "ENERGY_GUARDS",
    "MAX_ENERGY_REJECTION_RECORDS", "ENERGY_RESET_MIN_POLLS",
    "ENERGY_RESET_MIN_SECONDS", "ENERGY_REJECT_WARN_INTERVAL_S",
    "ENERGY_QUANTUM_KWH", "ENERGY_GUARD_SAFETY_FACTOR", "BLOCK_STATUS",
    "BLOCK_CONFIG", "BLOCK_PHASE_BOX", "MODEL_CAPABILITIES",
    "DEFAULT_CAPABILITY", "BLOCK_STALENESS_FACTOR",
)


def test_const_contract(snapshot):
    values = {name: getattr(const, name) for name in CONST_NAMES}
    helpers = {
        "decode_enum": {
            repr(args): _outcome(lambda args=args: const.decode_enum(*args[:3], **args[3]))
            for args in (
                (None, const.STATUS_MAP, "status", {}),
                (3, const.STATUS_MAP, "status", {}),
                (99, const.STATUS_MAP, "status", {}),
                (99, const.STATUS_MAP, "status", {"unknown_label": "unknown"}),
            )
        },
        "decode_bitmask": {
            str(value): const.decode_bitmask(value, const.FAULT_BITS, "fault_code")
            for value in (None, 0, 1, 0b1010, 0x3FFFF, 0x40000, 0xFFFFFFFF)
        },
        "get_capabilities": {
            str(model): const.get_capabilities(model)
            for model in (None, "", "A7300P1-E-B-WO", "a011-x", "A022", "B999")
        },
    }
    assert {"values": values, "helpers": helpers} == snapshot


# ── full HA setup: registries and states ─────────────────────────────────────

@pytest.mark.parametrize("language", ["en", "de"])
async def test_registry_and_state_contract(hass, enable_custom_integrations, snapshot, language):
    hass.config.language = language
    fake = FakeController(status=3)
    fake.phase_box = {"auto_phase_switch": 1, "min_switch_interval": 10}
    fake.hw.update(
        power_raw=69, l1_current_raw=301, ambient_temp_raw=1055, current_energy_raw=57,
        lock_status=1, work_mode=1, fault_code=0b10, alarm_code=0b1,
    )
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, ENTRY_ID)})
        assert device == snapshot(name="device")
        registry = er.async_get(hass)
        # unique_id alone is not unique: binary_sensor and switch both use
        # the "auto_phase_switch" suffix.
        entries = sorted(
            er.async_entries_for_config_entry(registry, ENTRY_ID),
            key=lambda e: (e.domain, e.unique_id),
        )
        for entry in entries:
            assert entry == snapshot(name=f"{entry.domain}.{entry.unique_id}-entry")
        for entry in entries:
            if entry.disabled_by is not None:
                registry.async_update_entity(entry.entity_id, disabled_by=None)
        await hass.config_entries.async_reload(ENTRY_ID)
        await hass.async_block_till_done()
        for entry in entries:
            assert hass.states.get(entry.entity_id) == snapshot(
                name=f"{entry.domain}.{entry.unique_id}-state")
    finally:
        await harness.async_unload()


# ── config and options flow ──────────────────────────────────────────────────

class _Probe:
    """Stands in for FoxESSModbusClient during the connection check."""

    instances: list[_Probe] = []
    reply: object = [1]

    def __init__(self, *args) -> None:
        self.args = args
        self.reads: list[tuple] = []
        self.disconnected = False
        _Probe.instances.append(self)

    def read_registers(self, address, count, quiet=False):
        self.reads.append((hex(address), count))
        if isinstance(_Probe.reply, Exception):
            raise _Probe.reply
        return _Probe.reply

    def disconnect(self):
        self.disconnected = True


async def test_config_flow_contract(hass, enable_custom_integrations, snapshot):
    steps = {}
    _Probe.instances = []
    with patch("custom_components.foxess_charger.config_flow.FoxESSModbusClient", _Probe), \
            patch("custom_components.foxess_charger.async_setup_entry", return_value=True):
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        steps["form"] = form

        _Probe.reply = None
        steps["unreachable"] = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"host": "192.0.2.20", "port": 1502, "slave_id": 3})

        _Probe.reply = OSError("probe blew up")
        try:
            await hass.config_entries.flow.async_configure(
                form["flow_id"], {"host": "192.0.2.20", "port": 1502, "slave_id": 3})
            steps["probe_raises"] = "no exception"
        except Exception as err:  # noqa: BLE001
            steps["probe_raises"] = f"{type(err).__name__}: {err}"

        _Probe.reply = [1]
        created = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"host": "192.0.2.20", "port": 1502, "slave_id": 3})
        await hass.async_block_till_done()
        steps["created"] = created
        entry = created["result"]
        steps["entry"] = {
            "unique_id": entry.unique_id, "title": entry.title, "data": dict(entry.data),
            "options": dict(entry.options), "version": entry.version,
            "minor_version": entry.minor_version,
        }
        steps["probes"] = [
            {"args": p.args, "reads": p.reads, "disconnected": p.disconnected}
            for p in _Probe.instances
        ]

        again = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        steps["duplicate"] = await hass.config_entries.flow.async_configure(
            again["flow_id"], {"host": "192.0.2.20", "port": 502, "slave_id": 3})
        other = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        other_slave = await hass.config_entries.flow.async_configure(
            other["flow_id"], {"host": "192.0.2.20", "port": 502, "slave_id": 4})
        steps["same_host_other_slave"] = other_slave["type"]

        def options_result(result):
            # The handler of an options flow is the (random) entry ID.
            return {**result, "handler": "<entry_id>"}

        options = {}
        options["form"] = options_result(
            await hass.config_entries.options.async_init(entry.entry_id))
        for interval in (4, 301, 5, 300, 10):
            flow = await hass.config_entries.options.async_init(entry.entry_id)
            options[str(interval)] = options_result(
                await hass.config_entries.options.async_configure(
                    flow["flow_id"], {"scan_interval": interval}))
            # The next form defaults to whatever is stored now.
            options[f"form_after_{interval}"] = options_result(
                await hass.config_entries.options.async_init(entry.entry_id))
        options["stored"] = dict(entry.options)
        hass.config_entries.async_update_entry(other_slave["result"], options={})
        options["form_without_stored_interval"] = options_result(
            await hass.config_entries.options.async_init(other_slave["result"].entry_id))
        steps["options"] = options
    assert steps == snapshot
    assert steps["created"]["type"] is FlowResultType.CREATE_ENTRY


# ── translations ─────────────────────────────────────────────────────────────

def _flatten(tree, prefix=""):
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _flatten(value, f"{path}.")
        else:
            yield path, value


def test_translation_contract(snapshot):
    """All keys in both files, and every value users see as an entity name
    or state label (those feed entity IDs and history). Config/options flow
    wording is free to change; its keys are not."""
    result = {}
    for lang in ("en", "de"):
        with open(os.path.join(TRANSLATIONS, f"{lang}.json"), encoding="utf-8") as fh:
            flat = dict(_flatten(json.load(fh)))
        result[lang] = {
            "keys": sorted(flat),
            "entity": {k: v for k, v in sorted(flat.items()) if k.startswith("entity.")},
            "issues": {k: v for k, v in sorted(flat.items()) if k.startswith("issues.")},
        }
    assert result == snapshot


# ── Modbus TCP framing ───────────────────────────────────────────────────────

class _EchoSocket:
    """Answers every request like a well-behaved charger and keeps the
    exact bytes sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._pending = b""

    def settimeout(self, _t):
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.append(data.hex())
        tid, unit, pdu = data[:2], data[6:7], data[7:]
        fc = pdu[0]
        if fc == 0x03:
            count = int.from_bytes(pdu[3:5], "big")
            base = int.from_bytes(pdu[1:3], "big")
            regs = [(base + i) & 0xFFFF for i in range(count)]
            if base == 0x101E:
                regs = [0x4137, 0x3330, 0x3050, 0x3100][:count]
            body = bytes([0x03, count * 2]) + b"".join(r.to_bytes(2, "big") for r in regs)
        elif fc == 0x06:
            body = pdu
        else:
            body = pdu[:5]
        self._pending += tid + b"\x00\x00" + (len(body) + 1).to_bytes(2, "big") + unit + body

    def recv(self, n: int) -> bytes:
        chunk, self._pending = self._pending[:n], self._pending[n:]
        return chunk

    def close(self):
        pass


def test_modbus_frame_contract(snapshot):
    sock = _EchoSocket()
    with patch(
        "custom_components.foxess_charger.modbus_client.socket.create_connection",
        return_value=sock,
    ):
        client = FoxESSModbusClient("192.0.2.30", 502, 7)
        results = {
            "status_block": client.read_registers(0x1000, 30),
            "config_block": client.read_registers_strict(0x3000, 7),
            "uint32": client.read_uint32(0x1016),
            "ascii": client.read_ascii(0x101E, 4),
            "write_power": client.write_holding_register(0x3002, 73),
            "write_stop": client.write_holding_register(0x4001, 2),
            "write_lock_strict": client.write_register_strict(0x4000, 1),
            "write_every_command_register": [
                client.write_holding_register(address, 0)
                for address in (0x4000, 0x4001, 0x4002, 0x4003)
            ],
            "write_config_registers": [
                client.write_holding_register(address, 1)
                for address in (0x3000, 0x3005, 0x300A, 0x3FFF, 0x4004)
            ],
        }
        client._tid = 0xFFFD  # transaction IDs wrap after 0xFFFE
        results["wrap"] = [client.read_registers(0x1003, 1) for _ in range(3)]
        client.disconnect()
    results["decode_ascii"] = {
        "padded": decode_ascii([0x4137, 0x3330, 0x3000, 0x0000]),
        "spaces": decode_ascii([0x2041, 0x4200]),
        "empty": decode_ascii([]),
    }
    assert {"frames": sock.sent, "results": results} == snapshot
