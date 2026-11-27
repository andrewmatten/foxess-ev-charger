"""C14: Assign CONFIG and DIAGNOSTIC entity categories without changing entity IDs."""
from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS
from custom_components.foxess_charger.number import NUMBERS
from custom_components.foxess_charger.select import FoxESSPhaseSelect, FoxESSWorkModeSelect
from custom_components.foxess_charger.sensor import SENSORS
from custom_components.foxess_charger.switch import (
    FoxESSAutoPhaseSwitchSwitch,
    FoxESSChargingSwitch,
    FoxESSLockSwitch,
)

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "c14catentry01"

EXPECTED_CONFIG = {
    ("select", "work_mode"),
    ("select", "phase_sequence"),
    ("switch", "lock"),
    ("switch", "auto_phase_switch"),
    ("number", "time_validity"),
    ("number", "default_current"),
    ("number", "min_switch_interval"),
    ("number", "allowed_charge_time"),
    ("number", "allowed_charge_energy"),
}

EXPECTED_DIAGNOSTIC = {
    ("sensor", "software_version"),
    ("sensor", "device_address"),
    ("sensor", "id_serial_number"),
    ("sensor", "charging_status_reason"),
    ("sensor", "transport_errors"),
    ("sensor", "cp_status"),
    ("sensor", "cc_status"),
    ("sensor", "lock_status"),
    ("sensor", "work_mode_sensor"),
    ("sensor", "max_supported_power"),
    ("sensor", "min_supported_power"),
    ("sensor", "max_supported_current"),
    ("sensor", "min_supported_current"),
    ("sensor", "alarm_code"),
    ("sensor", "fault_code"),
}

EXPECTED_ENTITY_IDS = {
    ("select", "work_mode"): "select.foxess_charger_work_mode",
    ("select", "phase_sequence"): "select.foxess_charger_phase_sequence",
    ("switch", "charging"): "switch.foxess_charger_charging",
    ("switch", "lock"): "switch.foxess_charger_lock",
    ("switch", "auto_phase_switch"): "switch.foxess_charger_auto_phase_switch",
    ("number", "max_charging_current"): "number.foxess_charger_max_charging_current",
    ("number", "max_charging_power"): "number.foxess_charger_max_charging_power",
    ("number", "allowed_charge_time"): "number.foxess_charger_allowed_charge_time",
    ("number", "allowed_charge_energy"): "number.foxess_charger_allowed_charge_energy",
    ("number", "time_validity"): "number.foxess_charger_command_time_validity",
    ("number", "default_current"): "number.foxess_charger_default_current_fallback",
    ("number", "min_switch_interval"): "number.foxess_charger_min_phase_switch_interval",
    ("sensor", "cp_status"): "sensor.foxess_charger_cp_status",
    ("sensor", "cc_status"): "sensor.foxess_charger_cc_status",
    ("sensor", "lock_status"): "sensor.foxess_charger_lock_status",
    ("sensor", "work_mode_sensor"): "sensor.foxess_charger_work_mode",
    ("sensor", "max_supported_power"): "sensor.foxess_charger_max_supported_power",
    ("sensor", "min_supported_power"): "sensor.foxess_charger_min_supported_power",
    ("sensor", "max_supported_current"): "sensor.foxess_charger_max_supported_current",
    ("sensor", "min_supported_current"): "sensor.foxess_charger_min_supported_current",
    ("sensor", "alarm_code"): "sensor.foxess_charger_alarm_code",
    ("sensor", "fault_code"): "sensor.foxess_charger_fault_code",
}


def test_entity_descriptions_have_expected_categories():
    actual: dict[tuple[str, str], EntityCategory | None] = {}
    for desc in SENSORS:
        actual[("sensor", desc.key)] = desc.entity_category
    for desc in BINARY_SENSORS:
        actual[("binary_sensor", desc.key)] = desc.entity_category
    for desc in NUMBERS:
        actual[("number", desc.key)] = desc.entity_category
    for cls in (FoxESSWorkModeSelect, FoxESSPhaseSelect):
        actual[("select", cls.entity_description.key)] = cls.entity_description.entity_category
    for cls in (FoxESSChargingSwitch, FoxESSLockSwitch, FoxESSAutoPhaseSwitchSwitch):
        actual[("switch", cls.entity_description.key)] = cls.entity_description.entity_category

    config_keys = {k for k, v in actual.items() if v is EntityCategory.CONFIG}
    diag_keys = {k for k, v in actual.items() if v is EntityCategory.DIAGNOSTIC}
    assert config_keys == EXPECTED_CONFIG
    assert diag_keys == EXPECTED_DIAGNOSTIC
    # Primary controls and live telemetry stay uncategorized (None).
    assert actual[("switch", "charging")] is None
    assert actual[("number", "max_charging_current")] is None
    assert actual[("number", "max_charging_power")] is None
    assert actual[("sensor", "status")] is None
    assert actual[("sensor", "charging_power")] is None


async def test_ha_registry_categories_and_unchanged_entity_ids(
    hass, enable_custom_integrations
):
    fake = FakeController(status=1)
    fake.phase_box = {"auto_phase_switch": 0, "min_switch_interval": 5}
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        registry = er.async_get(hass)
        by_key = {}
        for entry in er.async_entries_for_config_entry(registry, ENTRY_ID):
            suffix = entry.unique_id[len(ENTRY_ID) + 1 :]
            by_key[(entry.domain, suffix)] = entry

        for key, entry in by_key.items():
            if key in EXPECTED_CONFIG:
                assert entry.entity_category is EntityCategory.CONFIG, key
            elif key in EXPECTED_DIAGNOSTIC:
                assert entry.entity_category is EntityCategory.DIAGNOSTIC, key
            else:
                assert entry.entity_category is None, key

        for key, expected_entity_id in EXPECTED_ENTITY_IDS.items():
            assert by_key[key].entity_id == expected_entity_id, key
    finally:
        await harness.async_unload()
