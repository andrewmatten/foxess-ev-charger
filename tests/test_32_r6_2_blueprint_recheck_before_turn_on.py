"""3.2 R6-2 (Astra re-check): if surplus disappears while the blueprint is
staging the minimum current, it does not turn charging on."""
from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time

from test_solar_surplus_blueprint import (
    GRID,
    T0,
    Harness,
    _setup,
    install_blueprint,  # noqa: F401
)


async def test_no_turn_on_when_surplus_vanishes_during_staging(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    real_set = hass.services._services["number"]["set_value"].job.target

    async def slow_set(call):
        await real_set(call)
        # Cloud passes while the charger poll runs: import now.
        hass.states.async_set(GRID, "2000", {"device_class": "power", "unit_of_measurement": "W"})

    hass.services.async_register("number", "set_value", slow_set)
    with freeze_time(T0) as frozen:
        h.set_connected(True)
        h.set_number(10)
        h.set_switch("off", intent_enabled=False)
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)
        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-3001)
        await hass.async_block_till_done()
    assert h.number_writes == [6.0]
    assert h.turn_ons == 0
