"""C10: Solar surplus blueprint safety and ergonomics fixes:
- never turns on or steps current up while unplugged or after session finished;
- never turns on again while intent_enabled is already true;
- turns off whenever switch is on OR intent_enabled is true (both stop and fail-safe);
- clamps down to entity_min before stopping;
- guards missing/unavailable Max Charging Current entity;
- starts from entity_min when turning on from off;
- filters charger entity selectors to integration: foxess_charger.
"""
from __future__ import annotations

from datetime import timedelta

import yaml
from freezegun import freeze_time

from test_solar_surplus_blueprint import (
    BLUEPRINT_SRC,
    GRID,
    NUMBER,
    SWITCH,
    T0,
    Harness,
    _setup,
    install_blueprint,  # noqa: F401
)


async def test_unplugged_with_surplus_never_turns_on_or_ramps_current(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        h.set_connected(False)
        h.set_number(6)
        h.set_switch("off")
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        for step in range(1, 20):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            h.set_grid(-3000 - step)
            await hass.async_block_till_done()

    assert h.turn_ons == 0
    assert h.number_writes == []


async def test_staged_enable_is_not_re_sent_and_disarms_on_high_import_and_failsafe(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        # Simulate an enable staged while unplugged: switch reads off, intent_enabled=True.
        h.set_connected(True)
        h.set_number(16)
        h.set_switch("off", intent_enabled=True)
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        for step in range(1, 5):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            h.set_grid(-3000 - step)
            await hass.async_block_till_done()

        assert h.turn_ons == 0
        assert h.number_writes == []

        # Sunset / high import must call turn_off even though switch reads off.
        frozen.move_to(T0 + timedelta(minutes=20))
        h.set_grid(5000)
        await hass.async_block_till_done()
        assert h.turn_offs == 1
        assert hass.states.get(SWITCH).attributes["intent_enabled"] is False

        # Subsequent high-import ticks do not spam turn_off once disarmed.
        frozen.move_to(T0 + timedelta(minutes=25))
        h.set_grid(5100)
        await hass.async_block_till_done()
        assert h.turn_offs == 1

        # Re-stage and verify the fail-safe branch also disarms immediately.
        h.set_switch("off", intent_enabled=True)
        frozen.move_to(T0 + timedelta(minutes=25, seconds=5))
        hass.states.async_set(GRID, "unavailable")
        await hass.async_block_till_done()
        assert h.turn_offs == 2
        assert hass.states.get(SWITCH).attributes["intent_enabled"] is False


async def test_finished_status_sensor_blocks_turn_on_until_ready(
    hass, install_blueprint  # noqa: F811
):
    status_entity = "sensor.charger_status"
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        h.set_number(6)
        h.set_switch("off")
        hass.states.async_set(status_entity, "finished", {"device_class": "enum"})
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass, {"vehicle_connected": status_entity})

        for step in range(1, 6):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            h.set_grid(-3000 - step)
            await hass.async_block_till_done()
        assert h.turn_ons == 0

        hass.states.async_set(status_entity, "connected", {"device_class": "enum"})
        frozen.move_to(T0 + timedelta(minutes=25))
        h.set_grid(-3100)
        await hass.async_block_till_done()
        assert h.turn_ons == 1


async def test_lowers_to_entity_min_before_stopping(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        h.set_number(9)
        h.set_switch("on")
        h.set_grid(5000)
        await hass.async_block_till_done()
        await _setup(hass, {"current_step_amps": 4})

        # 9 - 4 = 5 < 6: clamps to 6.0 A instead of stopping immediately.
        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(5001)
        await hass.async_block_till_done()
        assert h.number_writes == [6.0]
        assert h.turn_offs == 0

        # Already at 6.0 A on the next dwell tick: now stops.
        frozen.move_to(T0 + timedelta(minutes=6))
        h.set_grid(5002)
        await hass.async_block_till_done()
        assert h.number_writes == [6.0]
        assert h.turn_offs == 1


async def test_unavailable_or_missing_number_entity_is_guarded(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        # NUMBER entity missing from state machine initially.
        h.set_switch("on")
        h.set_grid(5000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(5001)
        await hass.async_block_till_done()
        assert h.turn_offs == 0
        assert h.number_writes == []

        # NUMBER entity present but unavailable/unknown.
        for bad_state in ("unavailable", "unknown"):
            hass.states.async_set(NUMBER, bad_state, {"min": 6, "max": 32})
            frozen.move_to(frozen.time_to_freeze + timedelta(minutes=3))
            h.set_grid(5010)
            await hass.async_block_till_done()
            frozen.move_to(frozen.time_to_freeze + timedelta(minutes=3))
            h.set_grid(-3000)
            await hass.async_block_till_done()
        assert h.turn_offs == 0
        assert h.number_writes == []


async def test_turn_on_from_off_starts_at_entity_min_then_ramps(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        h.set_number(28)
        h.set_switch("off")
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-3001)
        await hass.async_block_till_done()
        assert h.number_writes == [6.0]
        assert h.turn_ons == 1

        # Next dwell period steps up from 6.0 A by 1 A.
        frozen.move_to(T0 + timedelta(minutes=6))
        h.set_grid(-3002)
        await hass.async_block_till_done()
        assert h.number_writes == [6.0, 7.0]
        assert h.turn_ons == 1


def test_blueprint_selectors_filter_charger_entities_to_foxess_charger():
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))
    doc = yaml.load(BLUEPRINT_SRC.read_text(encoding="utf-8"), Loader=_Loader)
    inputs = doc["blueprint"]["input"]
    assert inputs["max_current_number"]["selector"]["entity"]["integration"] == "foxess_charger"
    assert inputs["charging_switch"]["selector"]["entity"]["integration"] == "foxess_charger"
    filters = inputs["vehicle_connected"]["selector"]["entity"]["filter"]
    assert all(f["integration"] == "foxess_charger" for f in filters)
    assert "integration" not in inputs["grid_power_sensor"]["selector"]["entity"]
