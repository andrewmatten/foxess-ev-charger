"""End-to-end through Home Assistant with the real ChargingController over a
SimCharger (real time, short timeouts): config-entry setup, entity services
and unload, judged on what reached the simulated charger.

Stands in for the 2.4.3 entity/setup tests that exercised the old
coordinator's own write paths (see tests/LEGACY_DISPOSITION.md).
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError

import custom_components.foxess_charger as integration
from custom_components.foxess_charger.const import (
    DOMAIN, REG_CHARGING_CONTROL, REG_MAX_CHARGING_POWER,
)
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.persistence import session_key, setpoints_key

from ha_harness import make_entry
from rebuild_simulator import CHARGING, FINISHED, SimCharger

ENTRY = "realctlentry01"
E = "foxess_charger"
P, CTRL = REG_MAX_CHARGING_POWER, REG_CHARGING_CONTROL
CAP = 14


def put(hass_storage, key, data) -> None:
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


def legacy_enabled_with_cap(hass_storage, power_raw: int = CAP) -> None:
    """A 2.4.3 install that was charging with a saved power cap."""
    put(hass_storage, session_key(ENTRY), {
        "session_start_wall": None, "session_start_total": None, "last_session": None,
        "prev_status": 3, "stop_inhibit": False, "stop_pending": False,
    })
    put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {str(P): power_raw}})


@pytest.fixture
async def setup_real(hass, enable_custom_integrations):
    entries = []

    async def _setup(sim: SimCharger):
        async def factory(hass_, entry, *, persist=None):
            return ChargingController(
                sim, persist=persist, confirmation_timeout=1.0, retry_interval=0.2,
                pause_confirmation_timeout=3.0,
            )

        stack = patch.object(integration, "async_create_controller", factory)
        stack.start()
        entry = make_entry(ENTRY)
        entry.add_to_hass(hass)
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append((entry, stack))
        return ok, entry

    yield _setup
    for entry, stack in entries:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
        stack.stop()


def new_sim(**kw) -> SimCharger:
    return SimCharger(clock=time.monotonic, **kw)


# legacy: test_realistic_charger::test_setup_mid_session_writes_cap_immediately
async def test_setup_mid_session_applies_saved_cap(hass, hass_storage, setup_real):
    legacy_enabled_with_cap(hass_storage)
    sim = new_sim(state=CHARGING)
    assert sim.power_limit_raw() == sim.max_power_raw
    ok, _ = await setup_real(sim)
    assert ok
    assert sim.power_limit_raw() == CAP
    assert sim.measured_power_raw() == CAP
    assert hass.states.get(f"switch.{E}_charging").state == "on"


# legacy: test_command_lock::TestReassertedRegisterSkippedWhenNotCharging::
#         test_the_incident_scenario_end_to_end_cannot_resume_charging
async def test_full_power_number_while_paused_never_resumes(hass, hass_storage, setup_real):
    legacy_enabled_with_cap(hass_storage)
    sim = new_sim(state=CHARGING)
    ok, _ = await setup_real(sim)
    assert ok
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": f"switch.{E}_charging"}, blocking=True,
    )
    assert sim.measured_power_raw() == 0
    mark = sim.mark()
    # The unconditional end-of-window automation write at full power.
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": f"number.{E}_max_charging_power", "value": 7.3}, blocking=True,
    )
    coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
    await coordinator.async_refresh()
    assert sim.positive_cap_writes(mark) == []
    assert sim.measured_power_raw() == 0
    assert hass.states.get(f"switch.{E}_charging").state == "off"
    state = hass.states.get(f"number.{E}_max_charging_power")
    assert state.state == "7.3"                       # the saved intent is shown
    assert state.attributes["confirmed"] is False


# legacy: test_entity_write_reliability::test_charging_switch_turn_on_raises_on_failed_write
async def test_turn_on_that_cannot_be_confirmed_raises(hass, hass_storage, setup_real):
    sim = new_sim()                                   # connected, idle
    ok, _ = await setup_real(sim)
    assert ok
    sim.inject_write(P, "ignored", count=None)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": f"switch.{E}_charging"}, blocking=True,
        )
    assert (CTRL, 1) not in sim.wire_writes()
    assert sim.measured_power_raw() == 0
    assert hass.states.get(f"switch.{E}_charging").state != "on"


@pytest.mark.parametrize("session_store", [True, False])
async def test_finished_243_install_is_not_resumed_by_upgrade(
    hass, hass_storage, setup_real, session_store
):
    """2.4.3 after natural completion: prev_status 5, no stop flags, saved cap."""
    if session_store:
        put(hass_storage, session_key(ENTRY), {
            "session_start_wall": None, "session_start_total": None, "last_session": None,
            "prev_status": 5, "stop_inhibit": False, "stop_pending": False,
        })
    put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {str(P): CAP}})
    sim = new_sim(state=FINISHED)
    ok, _ = await setup_real(sim)
    assert ok
    await hass.data[DOMAIN][ENTRY]["coordinator"].async_refresh()
    assert sim.positive_cap_writes() == []
    assert sim.status == 5 and sim.measured_power_raw() == 0
    assert hass.states.get(f"switch.{E}_charging").state == "off"
