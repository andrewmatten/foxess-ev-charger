"""HA-level tests for the rebuilt adapters, driven through real config-entry
setup, entity services and unload, with a fake controller behind them."""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError

from custom_components.foxess_charger.const import (
    DOMAIN, EVENT_SESSION_COMPLETED, REG_ALLOWED_CHARGE_ENERGY, REG_ALLOWED_CHARGE_TIME,
    REG_AUTO_PHASE_SWITCH, REG_DEFAULT_CURRENT, REG_LOCK_CONTROL, REG_MIN_SWITCH_INTERVAL,
    REG_PHASE_SWITCHING, REG_TIME_VALIDITY, REG_WORK_MODE,
)
from custom_components.foxess_charger.persistence import state_key

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "adaptersentry01"
E = "foxess_charger"


@pytest.fixture
async def setup(hass, enable_custom_integrations):
    harnesses = []

    async def _setup(fake: FakeController | None = None) -> Harness:
        h = Harness(hass, make_entry(ENTRY_ID), fake or FakeController(status=1))
        assert await h.async_setup()
        harnesses.append(h)
        return h

    yield _setup
    for h in harnesses:
        if h.entry.state is ConfigEntryState.LOADED:
            await h.async_unload()


async def _call(hass, domain, service, entity, **data):
    await hass.services.async_call(
        domain, service, {"entity_id": entity, **data}, blocking=True,
    )


# ── setup / unload ─────────────────────────────────────────────────────────

async def test_setup_initializes_starts_and_unload_closes(hass, setup):
    h = await setup()
    fake = h.fake
    assert h.entry.state is ConfigEntryState.LOADED
    assert fake.calls[0] == ("initialize", None)  # first install
    assert fake.started
    assert hass.states.get(f"sensor.{E}_status").state == "connected"
    await h.async_unload()
    assert h.entry.state is ConfigEntryState.NOT_LOADED
    assert fake.closed
    assert ENTRY_ID not in hass.data.get(DOMAIN, {})


async def test_failed_initialize_retries_setup_and_closes(hass, enable_custom_integrations):
    fake = FakeController()
    fake.fail_initialize = True
    h = Harness(hass, make_entry(ENTRY_ID), fake)
    assert not await h.async_setup()
    assert h.entry.state is ConfigEntryState.SETUP_RETRY
    assert fake.closed
    assert not fake.started
    h._stack.close()


async def test_unreachable_at_startup_retries_setup_and_closes(hass, enable_custom_integrations):
    fake = FakeController()
    fake.fail_poll = 10
    h = Harness(hass, make_entry(ENTRY_ID), fake)
    assert not await h.async_setup()
    assert h.entry.state is ConfigEntryState.SETUP_RETRY
    assert fake.closed
    h._stack.close()


async def test_status_block_failures_make_entities_unavailable_after_three(hass, setup):
    h = await setup()
    coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
    h.fake.fail_poll = 2
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    h.fake.fail_poll = 1
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert hass.states.get(f"sensor.{E}_status").state == "unavailable"


async def test_failed_config_block_only_ages_out_its_own_entities(hass, setup):
    h = await setup()
    coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
    h.fake.config_ok = False
    await coordinator.async_refresh()
    # Last good value still shown until the block goes stale.
    assert hass.states.get(f"number.{E}_command_time_validity").state == "60.0"
    assert not coordinator.block_is_fresh("phase_box")
    assert coordinator.block_is_fresh("config")


# ── charging switch ───────────────────────────────────────────────────────

async def test_turn_on_routes_to_enable(hass, setup):
    h = await setup()
    await _call(hass, "switch", "turn_on", f"switch.{E}_charging")
    assert ("enable",) in h.fake.calls
    assert hass.states.get(f"switch.{E}_charging").state == "on"


async def test_turn_off_routes_to_pause(hass, setup):
    h = await setup(FakeController(status=3))
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    assert ("pause",) in h.fake.calls
    state = hass.states.get(f"switch.{E}_charging")
    assert state.state == "off"
    assert state.attributes["intent_enabled"] is False


async def test_control_error_surfaces_as_homeassistant_error(hass, setup):
    h = await setup(FakeController(status=1, cc_status=0))
    with pytest.raises(HomeAssistantError):
        await _call(hass, "switch", "turn_on", f"switch.{E}_charging")
    h.fake.fail_next = "pause"
    with pytest.raises(HomeAssistantError):
        await _call(hass, "switch", "turn_off", f"switch.{E}_charging")


async def test_vehicle_pause_keeps_switch_on_while_user_pause_turns_it_off(hass, setup):
    h = await setup(FakeController(status=3))
    coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
    h.fake.hw["status"] = 4
    await coordinator.async_refresh()
    assert hass.states.get(f"switch.{E}_charging").state == "on"
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    assert hass.states.get(f"switch.{E}_charging").state == "off"


# ── numbers ───────────────────────────────────────────────────────────────

async def test_power_staged_while_paused_reports_desired_vs_observed(hass, setup):
    h = await setup(FakeController(status=3))
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    await _call(hass, "number", "set_value", f"number.{E}_max_charging_power", value=2.5)
    assert ("set_power", 25) in h.fake.calls
    state = hass.states.get(f"number.{E}_max_charging_power")
    assert state.state == "2.5"
    assert state.attributes["desired"] == 2.5
    assert state.attributes["observed"] == 7.3
    assert state.attributes["last_outcome"] == "staged"
    assert state.attributes["confirmed"] is False


async def test_power_confirmed_while_enabled(hass, setup):
    h = await setup(FakeController(status=3))
    await _call(hass, "number", "set_value", f"number.{E}_max_charging_power", value=3.2)
    state = hass.states.get(f"number.{E}_max_charging_power")
    assert state.attributes["last_outcome"] == "confirmed"
    assert state.attributes["confirmed"] is True
    assert state.attributes["observed"] == 3.2


async def test_current_routes_to_set_current_and_stages(hass, setup):
    h = await setup(FakeController(status=3))
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    await _call(hass, "number", "set_value", f"number.{E}_max_charging_current", value=10)
    assert ("set_current", 100) in h.fake.calls
    state = hass.states.get(f"number.{E}_max_charging_current")
    assert state.state == "10.0"
    assert state.attributes["confirmed"] is False


async def test_number_control_error_is_homeassistant_error(hass, setup):
    h = await setup()
    h.fake.fail_next = "set_power"
    with pytest.raises(HomeAssistantError):
        await _call(hass, "number", "set_value", f"number.{E}_max_charging_power", value=2.0)


@pytest.mark.parametrize(
    ("entity", "value", "register", "raw"),
    [
        ("allowed_charge_time", 90, REG_ALLOWED_CHARGE_TIME, 90),
        ("allowed_charge_energy", 20, REG_ALLOWED_CHARGE_ENERGY, 20),
        ("command_time_validity", 60, REG_TIME_VALIDITY, 60),
        ("default_current_fallback", 6.0, REG_DEFAULT_CURRENT, 60),
        ("min_phase_switch_interval", 10, REG_MIN_SWITCH_INTERVAL, 10),
    ],
)
async def test_other_numbers_route_to_set_register(hass, setup, entity, value, register, raw):
    from homeassistant.helpers import entity_registry as er

    fake = FakeController()
    fake.phase_box = {"auto_phase_switch": 0, "min_switch_interval": 5}
    h = await setup(fake)
    registry = er.async_get(hass)
    entity_id = f"number.{E}_{entity}"
    if registry.async_get(entity_id).disabled_by is not None:
        registry.async_update_entity(entity_id, disabled_by=None)
        await hass.config_entries.async_reload(ENTRY_ID)
        await hass.async_block_till_done()
        h.fake = hass.data[DOMAIN][ENTRY_ID]["controller"]
    await _call(hass, "number", "set_value", entity_id, value=value)
    assert ("set_register", register, raw) in h.fake.calls


# ── selects and other switches ────────────────────────────────────────────

async def test_work_mode_select_routes_to_set_register(hass, setup):
    h = await setup()
    await _call(hass, "select", "select_option", f"select.{E}_work_mode", option="Plug&Charge")
    assert ("set_register", REG_WORK_MODE, 1) in h.fake.calls
    assert hass.states.get(f"select.{E}_work_mode").state == "Plug&Charge"


async def test_work_mode_select_error(hass, setup):
    h = await setup()
    h.fake.fail_next = "set_register"
    with pytest.raises(HomeAssistantError):
        await _call(hass, "select", "select_option", f"select.{E}_work_mode", option="Locked")


async def test_lock_switch_routes_lock_and_unlock(hass, setup):
    h = await setup()
    await _call(hass, "switch", "turn_on", f"switch.{E}_lock")
    assert ("set_register", REG_LOCK_CONTROL, 2) in h.fake.calls
    assert hass.states.get(f"switch.{E}_lock").state == "on"
    await _call(hass, "switch", "turn_off", f"switch.{E}_lock")
    assert ("set_register", REG_LOCK_CONTROL, 1) in h.fake.calls


async def test_disabled_by_default_controls_route_to_set_register(hass, setup):
    from homeassistant.helpers import entity_registry as er

    fake = FakeController()
    fake.phase_box = {"auto_phase_switch": 0, "min_switch_interval": 5}
    await setup(fake)
    registry = er.async_get(hass)
    for entity_id in (f"switch.{E}_auto_phase_switch", f"select.{E}_phase_sequence"):
        registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(ENTRY_ID)
    await hass.async_block_till_done()
    fake = hass.data[DOMAIN][ENTRY_ID]["controller"]
    await _call(hass, "switch", "turn_on", f"switch.{E}_auto_phase_switch")
    await _call(hass, "switch", "turn_off", f"switch.{E}_auto_phase_switch")
    await _call(hass, "select", "select_option", f"select.{E}_phase_sequence",
                option="l2_single_phase")
    assert ("set_register", REG_AUTO_PHASE_SWITCH, 1) in fake.calls
    assert ("set_register", REG_AUTO_PHASE_SWITCH, 0) in fake.calls
    assert ("set_register", REG_PHASE_SWITCHING, 1) in fake.calls


# ── session boundary through HA ───────────────────────────────────────────

async def test_user_pause_completes_session_once_and_fires_event(hass, setup):
    events = []
    hass.bus.async_listen(EVENT_SESSION_COMPLETED, events.append)
    h = await setup(FakeController(status=3))
    coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
    h.fake.hw["total_energy_raw"] = 1001  # one 0.1 kWh register step
    await coordinator.async_refresh()
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    await hass.async_block_till_done()
    for _ in range(3):  # charger sits paused (status 4, zero power)
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["entry_id"] == ENTRY_ID
    assert events[0].data["energy_kwh"] == 0.1
    assert hass.states.get(f"sensor.{E}_last_session_energy").state == "0.1"


async def test_status_9_and_unknown_status_do_not_complete_session(hass, setup):
    events = []
    hass.bus.async_listen(EVENT_SESSION_COMPLETED, events.append)
    h = await setup(FakeController(status=3))
    coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
    for status in (9, 3, 77, 7, 3, 4, 3):
        h.fake.hw["status"] = status
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert events == []
    h.fake.hw["status"] = 5
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_status_sensor_reports_phase_switching(hass, setup):
    h = await setup(FakeController(status=9))
    assert hass.states.get(f"sensor.{E}_status").state == "phase_switching"


# ── storage ───────────────────────────────────────────────────────────────

async def test_controller_state_persisted_under_new_key(hass, setup, hass_storage):
    h = await setup(FakeController(status=3))
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    saved = hass_storage[state_key(ENTRY_ID)]
    assert saved["version"] == 1
    assert saved["data"]["controller"]["enabled"] is False


async def test_restart_restores_paused_intent(hass, setup, hass_storage):
    h = await setup(FakeController(status=3))
    await _call(hass, "number", "set_value", f"number.{E}_max_charging_power", value=2.2)
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    await h.async_unload()
    fake2 = FakeController(status=4)
    h2 = await setup(fake2)
    saved = fake2.calls[0][1]
    assert saved["enabled"] is False and saved["power_raw"] == 22
    assert hass.states.get(f"switch.{E}_charging").state == "off"


async def test_storage_save_failure_does_not_break_setup_or_polling(
    hass, setup, hass_storage, monkeypatch,
):
    from homeassistant.helpers.storage import Store

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    h = await setup(FakeController(status=3))
    coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    # Persisting user intent failed: the command must not report success.
    with pytest.raises((HomeAssistantError, OSError)):
        await _call(hass, "number", "set_value", f"number.{E}_max_charging_power", value=2.0)


async def test_diagnostics_include_controller_view(hass, setup):
    from custom_components.foxess_charger.diagnostics import async_get_config_entry_diagnostics

    h = await setup()
    diag = await async_get_config_entry_diagnostics(hass, h.entry)
    ctrl = diag["coordinator_data"]["controller"]
    assert ctrl["saved_state"]["schema"] == 1
    assert diag["transport_counters"]["txid_mismatches"] == 0
    assert diag["coordinator_data"]["id_serial_number"] == "**REDACTED**"


async def test_limit_maxima_follow_detected_model(hass, setup):
    fake = FakeController()
    orig_poll = fake.async_poll

    async def _poll():
        snap = await orig_poll()
        snap["id_model_code"] = "A022-XYZ"
        return snap

    fake.async_poll = _poll
    await setup(fake)
    assert hass.states.get(f"number.{E}_max_charging_power").attributes["max"] == 22.0
    assert hass.states.get(f"number.{E}_max_charging_current").attributes["max"] == 32.0
    assert hass.states.get(f"number.{E}_command_time_validity").attributes["max"] == 255
