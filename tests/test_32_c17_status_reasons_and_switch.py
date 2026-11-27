"""C17: Charging Status Reason separates status_unreadable from charger_offline,
adds safety_config_unconfirmed (mentioning FoxCloud/read-only vs Modbus TCP),
maps Work Mode Locked to charger_locked, maps status 9 (phase_switching) to charging,
adds paused_below_minimum (switch.charging keeps its 3.1 reading there),
and translates all reasons in strings.json, en.json, and de.json."""
from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.sensor import (
    SENSORS,
    STATUS_REASONS,
    FoxESSChargerSensor,
)
from custom_components.foxess_charger.switch import FoxESSChargingSwitch

from fake_controller import FakeController
from ha_harness import Harness, make_entry

COMP_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "foxess_charger"
)
ENTRY_ID = "c17reasonentry01"


def _reason_sensor(coordinator: FoxESSChargerCoordinator) -> FoxESSChargerSensor:
    desc = next(d for d in SENSORS if d.key == "charging_status_reason")
    return FoxESSChargerSensor(coordinator, desc, MagicMock(entry_id=ENTRY_ID))


async def test_status_unreadable_separated_from_charger_offline(hass):
    fake = FakeController(status=99, cc_status=1)  # status_block_ok=True, status=None
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    await coordinator.async_refresh()
    sensor = _reason_sensor(coordinator)
    assert sensor.native_value == "status_unreadable"

    # Once the status block stops answering, charger_offline takes over (even if needs_human).
    fake.needs_human = True

    async def _dead():
        return {}

    fake.async_poll = _dead
    await coordinator.async_refresh()
    assert sensor.native_value == "charger_offline"


async def test_safety_config_unconfirmed_reported_instead_of_link_uncertain(hass):
    fake = FakeController(status=1, cc_status=1)
    fake.phase = "faulted"
    fake.diagnostics["protective_reasons"] = ["safety_config_unconfirmed"]
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    await coordinator.async_refresh()
    assert _reason_sensor(coordinator).native_value == "safety_config_unconfirmed"


@pytest.mark.parametrize(
    ("status", "work_mode", "phase", "enabled", "expected"),
    [
        (1, 2, "paused", False, "charger_locked"),
        (1, 2, "enabled", True, "charger_locked"),
        (9, 1, "enabled", True, "charging"),
        (4, 1, "paused_below_minimum", True, "paused_below_minimum"),
        (4, 1, "paused", False, "charging_off"),
    ],
)
async def test_locked_work_mode_phase_switching_and_paused_below_minimum_reasons(
    hass, status, work_mode, phase, enabled, expected
):
    fake = FakeController(status=status, cc_status=1)
    fake.hw["work_mode"] = work_mode
    fake.phase = phase
    fake._enabled = enabled
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    await coordinator.async_refresh()
    assert _reason_sensor(coordinator).native_value == expected


async def test_charging_switch_keeps_3_1_reading_in_paused_below_minimum(
    hass, enable_custom_integrations
):
    fake = FakeController(status=4, cc_status=1)  # active vehicle-paused session on wire
    fake._enabled = True
    fake.phase = "paused_below_minimum"
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        switch_state = hass.states.get("switch.foxess_charger_charging")
        reason_state = hass.states.get("sensor.foxess_charger_charging_status_reason")
        # Unchanged from 3.1 (automations key on it): on while the session is open.
        assert switch_state.state == "on"
        assert switch_state.attributes["intent_enabled"] is True
        assert switch_state.attributes["control_phase"] == "paused_below_minimum"
        assert reason_state.state == "paused_below_minimum"

        # Unit-level check across active statuses (2, 3, 4, 9).
        coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
        entity = FoxESSChargingSwitch(coordinator, MagicMock(entry_id=ENTRY_ID))
        for active_status in (2, 3, 4, 9):
            fake.hw["status"] = active_status
            await coordinator.async_refresh()
            assert entity.is_on is True
    finally:
        await harness.async_unload()


def test_status_reason_translations_complete_and_mention_foxcloud_modbus():
    strings = json.loads((COMP_DIR / "strings.json").read_text(encoding="utf-8"))
    en = json.loads((COMP_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
    de = json.loads((COMP_DIR / "translations" / "de.json").read_text(encoding="utf-8"))

    assert strings == en
    for name, doc in (("strings.json", strings), ("en.json", en), ("de.json", de)):
        state_map = doc["entity"]["sensor"]["charging_status_reason"]["state"]
        assert set(state_map) == set(STATUS_REASONS), name
        assert all(isinstance(v, str) and v.strip() for v in state_map.values()), name
        safety_text = state_map["safety_config_unconfirmed"]
        assert "FoxCloud" in safety_text, name
        assert "Modbus" in safety_text, name

    assert "read-only" in en["entity"]["sensor"]["charging_status_reason"]["state"][
        "safety_config_unconfirmed"
    ].lower()
