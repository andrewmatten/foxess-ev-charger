"""3.2 N9 / R5-6: align saved power cap at 220 (rollback-safe) while allowing >220 hardware reads."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger import const, controller, persistence
from custom_components.foxess_charger.controller import ChargingController, _parse_saved
from custom_components.foxess_charger.persistence import (
    ChargerStorage,
    valid_controller_state,
)

from test_controller import POW, FakeClock, FakeIO, make, saved, snapshot


def test_controller_and_persistence_share_max_power_bound():
    assert controller.SAVED_POWER_RAW_MAX == persistence.POWER_RAW_MAX == const.SAVED_POWER_RAW_MAX == 220
    assert controller.MAX_POWER_RAW == const.MAX_POWER_RAW >= 250


async def test_22kw_unit_above_220_enables_and_persists(hass, hass_storage):
    """A 22 kW three-phase unit reporting 0x1011 slightly above 220 (e.g. 225)
    must succeed on first enable and persist a rollback-safe cap (220)."""
    entry = "n9entry01"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()

    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    io.regs[0x1011] = 225
    io.regs[POW] = 225
    io._physics()

    c = ChargingController(
        io,
        clock=clock,
        sleep=clock.sleep,
        read_snapshot=snapshot,
        persist=storage.async_save_controller,
    )
    await c.async_initialize(saved(enabled=False, power=None), configure_safety=False)
    io.writes.clear()

    result = await c.async_enable()
    assert result.outcome == "confirmed"
    assert c.desired_power_raw == 220
    assert io.regs[POW] == 220

    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.issues == []
    assert loaded.controller["power_raw"] == 220
    assert loaded.controller["enabled"] is True


async def test_power_above_shared_max_rejected_by_both():
    bad = const.SAVED_POWER_RAW_MAX + 1
    assert not valid_controller_state(saved(power=bad))
    assert _parse_saved(saved(power=bad)) is None
    c, _ = await make()
    with pytest.raises(ValueError):
        await c.async_set_power(bad)
