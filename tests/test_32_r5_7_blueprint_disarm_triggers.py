"""3.2 R5-7: blueprint disarms staged enable immediately on switch/connection changes; accurate Plug&Charge changelog."""
from __future__ import annotations

import pathlib

from freezegun import freeze_time
from homeassistant.setup import async_setup_component

from test_solar_surplus_blueprint import (
    GRID,
    NUMBER,
    SWITCH,
    T0,
    Harness,
    _setup,
    install_blueprint,  # noqa: F401
)

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"


async def test_car_disconnect_disarms_staged_enable_with_steady_grid(
    hass, install_blueprint  # noqa: F811
):
    """When the car disconnects while an enable is staged and the grid reading
    stays steady, the blueprint disarms immediately without waiting for a grid
    change or minute tick."""
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0):
        h.set_connected(True)
        h.set_number(16)
        h.set_switch("off", intent_enabled=True)
        h.set_grid(100)
        await hass.async_block_till_done()
        await _setup(hass)

        # Grid never changes and time does not advance: only vehicle_connected drops.
        h.set_connected(False)
        await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert hass.states.get(SWITCH).attributes["intent_enabled"] is False
    assert h.turn_ons == 0


async def test_enable_staged_while_car_away_is_disarmed_with_steady_grid(
    hass, install_blueprint  # noqa: F811
):
    """When intent_enabled turns True on an off switch while the car is already
    away and the grid is steady, the blueprint disarms immediately."""
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0):
        h.set_connected(False)
        h.set_number(16)
        h.set_switch("off", intent_enabled=False)
        h.set_grid(100)
        await hass.async_block_till_done()
        await _setup(hass)

        h.set_switch("off", intent_enabled=True)
        await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert hass.states.get(SWITCH).attributes["intent_enabled"] is False
    assert h.turn_ons == 0


async def test_status_sensor_transition_to_finished_disarms_staged_enable_with_steady_grid(
    hass, install_blueprint  # noqa: F811
):
    """Using the Status enum sensor as vehicle_connected disarms a staged
    enable immediately on transition to finished with a steady grid."""
    status_entity = "sensor.charger_status"
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0):
        h.set_number(16)
        h.set_switch("off", intent_enabled=True)
        hass.states.async_set(status_entity, "connected", {"device_class": "enum"})
        h.set_grid(100)
        await hass.async_block_till_done()
        await _setup(hass, {"vehicle_connected": status_entity})

        hass.states.async_set(status_entity, "finished", {"device_class": "enum"})
        await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert hass.states.get(SWITCH).attributes["intent_enabled"] is False


async def test_omitted_vehicle_connected_loads_and_disarms_on_switch_intent(
    hass, install_blueprint  # noqa: F811
):
    """Leaving vehicle_connected omitted must still load cleanly and disarm a
    staged enable when charging_switch changes."""
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0):
        h.set_number(16)
        h.set_switch("off", intent_enabled=False)
        h.set_grid(100)
        await hass.async_block_till_done()
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "alias": "surplus_omitted_vehicle",
                    "use_blueprint": {
                        "path": "foxess_charger/solar_surplus_charging.yaml",
                        "input": {
                            "grid_power_sensor": GRID,
                            "max_current_number": NUMBER,
                            "charging_switch": SWITCH,
                        },
                    },
                }
            },
        )
        await hass.async_block_till_done()

        h.set_switch("off", intent_enabled=True)
        await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert hass.states.get(SWITCH).attributes["intent_enabled"] is False


def test_changelog_narrows_plug_and_charge_staged_plugin_claim():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "never runs at its own maximum first" not in text
    assert "first poll" in text
