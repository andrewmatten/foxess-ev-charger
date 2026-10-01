"""Frozen compatibility inventory for the FoxESS EV Charger integration.

Generated from the 2.4.3 code (before the rebuild) by setting the
integration up through HA's config-entry machinery with fake hardware and
reading back what HA registered. Any change here breaks users: entity IDs
and unique IDs feed automations/dashboards/statistics, Work Mode option
strings feed automations, and the three version-1 Store files must stay
readable by 2.4.3 so a rollback keeps its saved limits and Stop intent.

Enum-sensor option lists may only grow at the end (a new firmware status
is additive); every other field must match exactly.
"""
from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.device_trigger import async_get_triggers
from custom_components.foxess_charger.diagnostics import (
    async_get_config_entry_diagnostics,
)

from ha_harness import Harness, make_entry, make_fake

ENTRY_ID = "inventoryentry01"

EXPECTED_ENTITIES = {
    ('binary_sensor', 'auto_phase_switch'): dict(entity_id='binary_sensor.foxess_charger_auto_phase_switch', translation_key='auto_phase_switch', name='Auto Phase Switch', unit=None, device_class=None, entity_category=None, enabled_default=False, capabilities=None),
    ('binary_sensor', 'has_alarm'): dict(entity_id='binary_sensor.foxess_charger_alarm', translation_key='has_alarm', name='Alarm', unit=None, device_class='problem', entity_category=None, enabled_default=True, capabilities=None),
    ('binary_sensor', 'has_fault'): dict(entity_id='binary_sensor.foxess_charger_fault', translation_key='has_fault', name='Fault', unit=None, device_class='problem', entity_category=None, enabled_default=True, capabilities=None),
    ('binary_sensor', 'is_charging'): dict(entity_id='binary_sensor.foxess_charger_charging', translation_key='is_charging', name='Charging', unit=None, device_class='battery_charging', entity_category=None, enabled_default=True, capabilities=None),
    ('binary_sensor', 'is_locked'): dict(entity_id='binary_sensor.foxess_charger_locked', translation_key='is_locked', name='Locked', unit=None, device_class='lock', entity_category=None, enabled_default=True, capabilities=None),
    ('binary_sensor', 'vehicle_connected'): dict(entity_id='binary_sensor.foxess_charger_vehicle_connected', translation_key='vehicle_connected', name='Vehicle Connected', unit=None, device_class='plug', entity_category=None, enabled_default=True, capabilities=None),
    ('number', 'allowed_charge_energy'): dict(entity_id='number.foxess_charger_allowed_charge_energy', translation_key='allowed_charge_energy', name='Allowed Charge Energy', unit='kWh', device_class=None, entity_category=None, enabled_default=True, capabilities={'max': 999, 'min': 0, 'mode': 'auto', 'step': 1}),
    ('number', 'allowed_charge_time'): dict(entity_id='number.foxess_charger_allowed_charge_time', translation_key='allowed_charge_time', name='Allowed Charge Time', unit='min', device_class=None, entity_category=None, enabled_default=True, capabilities={'max': 1440, 'min': 0, 'mode': 'auto', 'step': 1}),
    ('number', 'default_current'): dict(entity_id='number.foxess_charger_default_current_fallback', translation_key='default_current', name='Default Current (Fallback)', unit='A', device_class=None, entity_category=None, enabled_default=True, capabilities={'max': 32.0, 'min': 6.0, 'mode': 'auto', 'step': 0.1}),
    ('number', 'max_charging_current'): dict(entity_id='number.foxess_charger_max_charging_current', translation_key='max_charging_current', name='Max Charging Current', unit='A', device_class=None, entity_category=None, enabled_default=True, capabilities={'max': 32.0, 'min': 6.0, 'mode': 'auto', 'step': 0.1}),
    ('number', 'max_charging_power'): dict(entity_id='number.foxess_charger_max_charging_power', translation_key='max_charging_power', name='Max Charging Power', unit='kW', device_class=None, entity_category=None, enabled_default=True, capabilities={'max': 7.3, 'min': 0.0, 'mode': 'auto', 'step': 0.1}),
    ('number', 'min_switch_interval'): dict(entity_id='number.foxess_charger_min_phase_switch_interval', translation_key='min_switch_interval', name='Min Phase Switch Interval', unit='min', device_class=None, entity_category=None, enabled_default=False, capabilities={'max': 30, 'min': 5, 'mode': 'auto', 'step': 1}),
    ('number', 'time_validity'): dict(entity_id='number.foxess_charger_command_time_validity', translation_key='time_validity', name='Command Time Validity', unit='s', device_class=None, entity_category=None, enabled_default=True, capabilities={'max': 255, 'min': 10, 'mode': 'auto', 'step': 1}),
    ('select', 'phase_sequence'): dict(entity_id='select.foxess_charger_phase_sequence', translation_key='phase_switching_control', name='Phase Sequence', unit=None, device_class=None, entity_category=None, enabled_default=False, capabilities={'options': ['three_phase', 'l2_single_phase', 'l3_single_phase']}),
    ('select', 'work_mode'): dict(entity_id='select.foxess_charger_work_mode', translation_key='work_mode_control', name='Work Mode', unit=None, device_class=None, entity_category=None, enabled_default=True, capabilities={'options': ['Controlled', 'Plug&Charge', 'Locked']}),
    ('sensor', 'alarm_code'): dict(entity_id='sensor.foxess_charger_alarm_code', translation_key='alarm_code', name='Alarm Code', unit=None, device_class=None, entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'ambient_temperature'): dict(entity_id='sensor.foxess_charger_internal_temperature', translation_key='ambient_temperature', name='Internal Temperature', unit='°C', device_class='temperature', entity_category=None, enabled_default=True, capabilities={'state_class': 'measurement'}),
    ('sensor', 'cc_status'): dict(entity_id='sensor.foxess_charger_cc_status', translation_key='cc_status', name='CC Status', unit=None, device_class='enum', entity_category=None, enabled_default=True, capabilities={'options': ['disconnected', 'connected', 'unknown']}),
    ('sensor', 'charging_power'): dict(entity_id='sensor.foxess_charger_charging_power', translation_key='charging_power', name='Charging Power', unit='kW', device_class='power', entity_category=None, enabled_default=True, capabilities={'state_class': 'measurement'}),
    ('sensor', 'cp_status'): dict(entity_id='sensor.foxess_charger_cp_status', translation_key='cp_status', name='CP Status', unit=None, device_class='enum', entity_category=None, enabled_default=True, capabilities={'options': ['fault', '12v_disconnected', '9v_connected', '6v_ready', 'unknown']}),
    ('sensor', 'current_session_energy'): dict(entity_id='sensor.foxess_charger_current_session_energy', translation_key='current_session_energy', name='Current Session Energy', unit='kWh', device_class='energy', entity_category=None, enabled_default=True, capabilities={'state_class': 'total_increasing'}),
    ('sensor', 'device_address'): dict(entity_id='sensor.foxess_charger_device_address', translation_key='device_address', name='Device Address', unit=None, device_class=None, entity_category='diagnostic', enabled_default=True, capabilities=None),
    ('sensor', 'fault_code'): dict(entity_id='sensor.foxess_charger_fault_code', translation_key='fault_code', name='Fault Code', unit=None, device_class=None, entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'id_serial_number'): dict(entity_id='sensor.foxess_charger_serial_number', translation_key='id_serial_number', name='Serial Number', unit=None, device_class=None, entity_category='diagnostic', enabled_default=True, capabilities=None),
    ('sensor', 'l1_current'): dict(entity_id='sensor.foxess_charger_l1_current', translation_key='l1_current', name='L1 Current', unit='A', device_class='current', entity_category=None, enabled_default=True, capabilities={'state_class': 'measurement'}),
    ('sensor', 'l1_voltage'): dict(entity_id='sensor.foxess_charger_l1_voltage', translation_key='l1_voltage', name='L1 Voltage', unit='V', device_class='voltage', entity_category=None, enabled_default=True, capabilities={'state_class': 'measurement'}),
    ('sensor', 'l2_current'): dict(entity_id='sensor.foxess_charger_l2_current', translation_key='l2_current', name='L2 Current', unit='A', device_class='current', entity_category=None, enabled_default=False, capabilities={'state_class': 'measurement'}),
    ('sensor', 'l2_voltage'): dict(entity_id='sensor.foxess_charger_l2_voltage', translation_key='l2_voltage', name='L2 Voltage', unit='V', device_class='voltage', entity_category=None, enabled_default=False, capabilities={'state_class': 'measurement'}),
    ('sensor', 'l3_current'): dict(entity_id='sensor.foxess_charger_l3_current', translation_key='l3_current', name='L3 Current', unit='A', device_class='current', entity_category=None, enabled_default=False, capabilities={'state_class': 'measurement'}),
    ('sensor', 'l3_voltage'): dict(entity_id='sensor.foxess_charger_l3_voltage', translation_key='l3_voltage', name='L3 Voltage', unit='V', device_class='voltage', entity_category=None, enabled_default=False, capabilities={'state_class': 'measurement'}),
    ('sensor', 'last_session_duration'): dict(entity_id='sensor.foxess_charger_last_session_duration', translation_key='last_session_duration', name='Last Session Duration', unit='min', device_class='duration', entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'last_session_energy'): dict(entity_id='sensor.foxess_charger_last_session_energy', translation_key='last_session_energy', name='Last Session Energy', unit='kWh', device_class='energy', entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'lock_status'): dict(entity_id='sensor.foxess_charger_lock_status', translation_key='lock_status', name='Lock Status', unit=None, device_class='enum', entity_category=None, enabled_default=True, capabilities={'options': ['unlocked', 'locked', 'unknown']}),
    ('sensor', 'max_supported_current'): dict(entity_id='sensor.foxess_charger_max_supported_current', translation_key='max_supported_current', name='Max Supported Current', unit='A', device_class='current', entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'max_supported_power'): dict(entity_id='sensor.foxess_charger_max_supported_power', translation_key='max_supported_power', name='Max Supported Power', unit='kW', device_class='power', entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'min_supported_current'): dict(entity_id='sensor.foxess_charger_min_supported_current', translation_key='min_supported_current', name='Min Supported Current', unit='A', device_class='current', entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'min_supported_power'): dict(entity_id='sensor.foxess_charger_min_supported_power', translation_key='min_supported_power', name='Min Supported Power', unit='kW', device_class='power', entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'phase_sequence'): dict(entity_id='sensor.foxess_charger_phase_sequence', translation_key='phase_sequence', name='Phase Sequence', unit=None, device_class='enum', entity_category=None, enabled_default=False, capabilities={'options': ['three_phase', 'l2_single_phase', 'l3_single_phase', 'unknown']}),
    ('sensor', 'port_temperature'): dict(entity_id='sensor.foxess_charger_port_temperature', translation_key='port_temperature', name='Port Temperature', unit='°C', device_class='temperature', entity_category=None, enabled_default=False, capabilities={'state_class': 'measurement'}),
    ('sensor', 'rfid_card'): dict(entity_id='sensor.foxess_charger_rfid_card', translation_key='rfid_card', name='RFID Card', unit=None, device_class=None, entity_category=None, enabled_default=False, capabilities=None),
    ('sensor', 'software_version'): dict(entity_id='sensor.foxess_charger_software_version', translation_key='software_version', name='Software Version', unit=None, device_class=None, entity_category='diagnostic', enabled_default=True, capabilities=None),
    ('sensor', 'status'): dict(entity_id='sensor.foxess_charger_status', translation_key='status', name='Status', unit=None, device_class='enum', entity_category=None, enabled_default=True, capabilities={'options': ['idle', 'connected', 'start', 'charging', 'paused', 'finished', 'fault', 'reserved', 'locked', 'unknown']}),
    ('sensor', 'stop_reason'): dict(entity_id='sensor.foxess_charger_stop_reason', translation_key='stop_reason', name='Stop Reason', unit=None, device_class=None, entity_category=None, enabled_default=True, capabilities=None),
    ('sensor', 'total_energy'): dict(entity_id='sensor.foxess_charger_total_energy', translation_key='total_energy', name='Total Energy', unit='kWh', device_class='energy', entity_category=None, enabled_default=True, capabilities={'state_class': 'total_increasing'}),
    ('sensor', 'transport_errors'): dict(entity_id='sensor.foxess_charger_transport_errors', translation_key='transport_errors', name='Transport Errors', unit=None, device_class=None, entity_category='diagnostic', enabled_default=True, capabilities={'state_class': 'measurement'}),
    ('sensor', 'work_mode_sensor'): dict(entity_id='sensor.foxess_charger_work_mode', translation_key='work_mode', name='Work Mode', unit=None, device_class='enum', entity_category=None, enabled_default=True, capabilities={'options': ['controlled', 'plug_and_charge', 'locked', 'unknown']}),
    ('switch', 'auto_phase_switch'): dict(entity_id='switch.foxess_charger_auto_phase_switch', translation_key='auto_phase_switch', name='Auto Phase Switch', unit=None, device_class=None, entity_category=None, enabled_default=False, capabilities=None),
    ('switch', 'charging'): dict(entity_id='switch.foxess_charger_charging', translation_key='charging', name='Charging', unit=None, device_class=None, entity_category=None, enabled_default=True, capabilities=None),
    ('switch', 'lock'): dict(entity_id='switch.foxess_charger_lock', translation_key='lock', name='Lock', unit=None, device_class=None, entity_category=None, enabled_default=True, capabilities=None),
}

EXPECTED_WORK_MODE_OPTIONS = ["Controlled", "Plug&Charge", "Locked"]

EXPECTED_TRIGGER_TYPES = [
    "alarm", "charging_started", "charging_stopped", "fault",
    "session_completed", "vehicle_plugged_in",
]

EXPECTED_DIAGNOSTICS_KEYS = {
    "block_health", "coordinator_data", "detected_capabilities", "entry",
    "firmware_version", "last_update_success", "model", "transport_counters",
}

EXPECTED_ENTRY_DATA_KEYS = {"host", "port", "slave_id"}
EXPECTED_ENTRY_OPTIONS_KEYS = {"scan_interval"}

SESSION_KEY = f"{DOMAIN}_{ENTRY_ID}_session"
SETPOINTS_KEY = f"{DOMAIN}_{ENTRY_ID}_setpoints"
ENERGY_KEY = f"{DOMAIN}_{ENTRY_ID}_energy_baseline"
SESSION_FIELDS = {
    "session_start_wall", "session_start_total", "last_session",
    "prev_status", "stop_inhibit", "stop_pending",
}
ENERGY_COUNTERS = {"total_energy_raw", "current_energy_raw"}


async def _setup(hass):
    harness = Harness(hass, make_entry(ENTRY_ID), make_fake(idle=True))
    assert await harness.async_setup()
    return harness


async def test_entity_registry_inventory(hass, enable_custom_integrations):
    harness = await _setup(hass)
    try:
        registry = er.async_get(hass)
        actual = {}
        for entry in er.async_entries_for_config_entry(registry, ENTRY_ID):
            assert entry.unique_id.startswith(f"{ENTRY_ID}_")
            suffix = entry.unique_id[len(ENTRY_ID) + 1:]
            actual[(entry.domain, suffix)] = dict(
                entity_id=entry.entity_id,
                translation_key=entry.translation_key,
                name=entry.original_name,
                unit=entry.unit_of_measurement,
                device_class=entry.original_device_class,
                entity_category=(
                    entry.entity_category.value if entry.entity_category else None
                ),
                enabled_default=entry.disabled_by is None,
                capabilities=dict(entry.capabilities) if entry.capabilities else None,
            )
        assert set(actual) == set(EXPECTED_ENTITIES)
        for key, expected in EXPECTED_ENTITIES.items():
            got = dict(actual[key])
            want = dict(expected)
            got_caps, want_caps = got.pop("capabilities"), want.pop("capabilities")
            assert got == want, key
            if (
                key[0] == "sensor" and want_caps and "options" in want_caps
            ):
                # Enum sensors: existing options keep their order; the
                # trailing "unknown" may be preceded by newly-added states.
                old = [o for o in want_caps["options"] if o != "unknown"]
                assert got_caps["options"][: len(old)] == old, key
                assert got_caps["options"][-1] == "unknown", key
                assert {k: v for k, v in got_caps.items() if k != "options"} == {
                    k: v for k, v in want_caps.items() if k != "options"
                }, key
            else:
                assert got_caps == want_caps, key
        assert actual[("select", "work_mode")]["capabilities"]["options"] == (
            EXPECTED_WORK_MODE_OPTIONS
        )
    finally:
        await harness.async_unload()


async def test_no_integration_services_and_trigger_types(hass, enable_custom_integrations):
    harness = await _setup(hass)
    try:
        assert hass.services.async_services().get(DOMAIN, {}) == {}
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, ENTRY_ID)})
        assert device is not None
        assert device.manufacturer == "FoxESS"
        assert device.name == "FoxESS Charger"
        triggers = await async_get_triggers(hass, device.id)
        assert sorted(t["type"] for t in triggers) == EXPECTED_TRIGGER_TYPES
    finally:
        await harness.async_unload()


async def test_diagnostics_top_level_keys(hass, enable_custom_integrations):
    harness = await _setup(hass)
    try:
        diag = await async_get_config_entry_diagnostics(hass, harness.entry)
        assert set(diag) == EXPECTED_DIAGNOSTICS_KEYS
        assert diag["entry"]["data"]["host"] == "**REDACTED**"
    finally:
        await harness.async_unload()


async def test_config_flow_entry_shape(hass, enable_custom_integrations):
    class _Probe:
        def __init__(self, *args, **kwargs):
            pass

        def read_registers(self, address, count, quiet=False):
            return [1] * count

        def disconnect(self):
            pass

    with patch("custom_components.foxess_charger.config_flow.FoxESSModbusClient", _Probe), \
            patch("custom_components.foxess_charger.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.0.2.20", "port": 502, "slave_id": 1}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(result["data"]) == EXPECTED_ENTRY_DATA_KEYS
    assert set(result["options"]) == EXPECTED_ENTRY_OPTIONS_KEYS
    entry = result["result"]
    assert entry.version == 1
    assert entry.unique_id == "192.0.2.20-1"


async def test_legacy_store_keys_and_schemas(hass, enable_custom_integrations, hass_storage):
    """A staged power limit and a user pause must land in the three legacy
    version-1 Store files in the shape 2.4.3 reads back."""
    harness = await _setup(hass)
    try:
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.foxess_charger_max_charging_power", "value": 2.5},
            blocking=True,
        )
        await hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": "switch.foxess_charger_charging"}, blocking=True,
        )
        await hass.async_block_till_done()
    finally:
        await harness.async_unload()

    for key in (SESSION_KEY, SETPOINTS_KEY, ENERGY_KEY):
        assert key in hass_storage, key
        assert hass_storage[key]["version"] == 1, key

    session = hass_storage[SESSION_KEY]["data"]
    assert set(session) == SESSION_FIELDS
    assert session["stop_inhibit"] is True
    assert session["stop_pending"] in (True, False)

    setpoints = hass_storage[SETPOINTS_KEY]["data"]
    assert set(setpoints) == {"desired_setpoints"}
    assert setpoints["desired_setpoints"]["12290"] == 25  # 0x3002, 0.1 kW units

    energy = hass_storage[ENERGY_KEY]["data"]
    assert energy and set(energy) <= ENERGY_COUNTERS
    for counter in energy.values():
        assert set(counter) == {"raw", "wall_ts"}
        assert isinstance(counter["raw"], int) and not isinstance(counter["raw"], bool)
        assert isinstance(counter["wall_ts"], float)
