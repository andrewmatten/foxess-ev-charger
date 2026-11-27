"""3.2 R1-M1: a protective (lower) limit change while enabled stays in memory when save fails."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import CC, CUR, POW, FakeClock, FakeIO, make, saved
from test_fault_and_stop_fallback import drain


async def _awaiting_plug_setup(*, power: int, current: int):
    fail = {"on": False}
    persisted: list[dict] = []

    async def persist(state: dict) -> None:
        if fail["on"]:
            raise OSError("disk full")
        persisted.append(dict(state))

    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    io.regs[CC] = 0
    c, io = await make(
        saved(enabled=False, power=power, current=current),
        io=io,
        persist=persist,
    )
    res = await c.async_enable()
    assert res.outcome == "staged"
    assert c.intent_enabled and c._await_plug
    io.writes.clear()
    return c, io, fail, persisted


async def test_failed_save_of_lower_power_while_awaiting_plug_stays_and_applies():
    """Lowering Max Charging Power while enabled/awaiting plug-in must stay in
    memory on a failed save, retry on refresh, and apply the lower cap on plug-in."""
    c, io, fail, persisted = await _awaiting_plug_setup(power=70, current=320)
    fail["on"] = True

    with pytest.raises(ControlError, match="could not be saved"):
        await c.async_set_power(20)

    assert c.desired_power_raw == 20
    assert c.export_state()["power_raw"] == 20
    assert c.diagnostics["persist_pending"] is True

    # Storage recovers: refresh persists 20, and plug-in applies 20 (never 70).
    fail["on"] = False
    await c._op_refresh()
    assert c.diagnostics["persist_pending"] is False
    assert persisted[-1]["power_raw"] == 20

    io.writes.clear()
    io.regs[CC] = 1
    io.stopped = True
    await c.async_poll()
    await drain(c)
    assert io.regs[POW] == 20
    assert 70 not in io.power_writes()


async def test_failed_save_of_lower_current_while_awaiting_plug_stays_and_applies():
    """Lowering Max Charging Current while enabled/awaiting plug-in must stay
    in memory on a failed save, retry on refresh, and apply on plug-in."""
    c, io, fail, persisted = await _awaiting_plug_setup(power=70, current=320)
    fail["on"] = True

    with pytest.raises(ControlError, match="could not be saved"):
        await c.async_set_current(100)

    assert c.desired_current_raw == 100
    assert c.export_state()["current_raw"] == 100
    assert c.diagnostics["persist_pending"] is True

    fail["on"] = False
    await c._op_refresh()
    assert c.diagnostics["persist_pending"] is False
    assert persisted[-1]["current_raw"] == 100

    io.writes.clear()
    io.regs[CC] = 1
    io.stopped = True
    await c.async_poll()
    await drain(c)
    assert io.regs[CUR] == 100
    assert 320 not in [v for _, a, v in io.writes if a == CUR]


async def test_failed_save_of_higher_limit_while_awaiting_plug_rolls_back():
    """Raising power or current while enabled/awaiting plug-in must not stick
    in memory when persistence fails."""
    c, io, fail, persisted = await _awaiting_plug_setup(power=20, current=100)
    fail["on"] = True

    with pytest.raises(ControlError):
        await c.async_set_power(70)
    with pytest.raises(ControlError):
        await c.async_set_current(320)

    assert c.desired_power_raw == 20
    assert c.desired_current_raw == 100

    fail["on"] = False
    await c._op_refresh()
    assert persisted[-1]["power_raw"] == 20
    assert persisted[-1]["current_raw"] == 100

    io.writes.clear()
    io.regs[CC] = 1
    io.stopped = True
    await c.async_poll()
    await drain(c)
    assert io.regs[POW] == 20
    assert io.regs[CUR] == 100


async def test_failed_save_of_change_while_paused_still_rolls_back():
    """While paused (not enabled), failed staged changes still roll back."""
    fail = {"on": False}

    async def persist(_state: dict) -> None:
        if fail["on"]:
            raise OSError("disk full")

    c, _io = await make(
        saved(enabled=False, power=20, current=100), charging=False, persist=persist
    )
    fail["on"] = True

    with pytest.raises(ControlError, match="could not be saved"):
        await c.async_set_power(70)
    with pytest.raises(ControlError, match="could not be saved"):
        await c.async_set_current(320)

    assert c.desired_power_raw == 20
    assert c.desired_current_raw == 100
