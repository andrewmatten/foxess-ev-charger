"""3.2 R5-8: blueprint still stops charging on over-import when Max Charging Current is unavailable."""
from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time

from test_solar_surplus_blueprint import (
    NUMBER,
    SWITCH,
    T0,
    Harness,
    _setup,
    install_blueprint,  # noqa: F401
)


@pytest.mark.parametrize("bad_state", [None, "unavailable", "unknown"])
async def test_over_import_stops_charging_once_when_current_number_unavailable(
    hass, install_blueprint, bad_state  # noqa: F811
):
    """If the config register block drops (making Max Charging Current
    unavailable/unknown/missing) while charging is on, sustained high import
    must turn charging off once rather than leaving it drawing."""
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        if bad_state is not None:
            hass.states.async_set(NUMBER, bad_state, {"min": 6, "max": 32})
        h.set_switch("on")
        h.set_grid(200)
        await hass.async_block_till_done()
        await _setup(hass)

        for step in range(1, 4):
            frozen.move_to(T0 + timedelta(minutes=3 * step))
            h.set_grid(5000 + step)
            await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert h.turn_ons == 0
    assert h.number_writes == []
    assert hass.states.get(SWITCH).state == "off"


async def test_over_import_disarms_staged_enable_when_current_number_unavailable(
    hass, install_blueprint  # noqa: F811
):
    """High import while switch is off with a staged intent_enabled and an
    unavailable current number must still disarm the staged enable."""
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        hass.states.async_set(NUMBER, "unavailable", {"min": 6, "max": 32})
        h.set_connected(True)
        h.set_switch("off", intent_enabled=True)
        h.set_grid(200)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(5000)
        await hass.async_block_till_done()

    assert h.turn_offs == 1
    assert hass.states.get(SWITCH).attributes["intent_enabled"] is False
    assert h.number_writes == []


async def test_surplus_never_starts_or_writes_when_current_number_unavailable(
    hass, install_blueprint  # noqa: F811
):
    """With an unavailable current number, surplus must still be guarded and
    never turn charging on or call number.set_value."""
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        hass.states.async_set(NUMBER, "unavailable", {"min": 6, "max": 32})
        h.set_connected(True)
        h.set_switch("off")
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-3001)
        await hass.async_block_till_done()

    assert h.turn_ons == 0
    assert h.turn_offs == 0
    assert h.number_writes == []
