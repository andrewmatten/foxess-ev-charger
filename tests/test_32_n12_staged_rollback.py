"""3.2 N12: failed staged limit change must roll back in memory and never persist later."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import CUR, POW, make, saved


async def test_failed_staged_power_change_rolls_back_and_never_persists():
    fail = {"on": False}
    persisted: list[dict] = []

    async def persist(state):
        if fail["on"]:
            raise OSError("disk full")
        persisted.append(dict(state))

    c, io = await make(
        saved(enabled=False, power=14, current=100), charging=False, persist=persist
    )
    fail["on"] = True

    with pytest.raises(ControlError, match="could not be saved"):
        await c.async_set_power(70)

    assert c.desired_power_raw == 14
    assert c.export_state()["power_raw"] == 14

    # Disk recovers and refresh runs: the rejected 70 must never be saved or applied.
    fail["on"] = False
    await c._op_refresh()
    assert persisted[-1]["power_raw"] == 14

    assert (await c.async_enable()).outcome == "confirmed"
    assert io.regs[POW] == 14


async def test_failed_staged_current_change_rolls_back_and_never_persists():
    fail = {"on": False}
    persisted: list[dict] = []

    async def persist(state):
        if fail["on"]:
            raise OSError("disk full")
        persisted.append(dict(state))

    c, io = await make(
        saved(enabled=False, power=50, current=100), charging=False, persist=persist
    )
    fail["on"] = True

    with pytest.raises(ControlError, match="could not be saved"):
        await c.async_set_current(320)

    assert c.desired_current_raw == 100
    assert c.export_state()["current_raw"] == 100

    fail["on"] = False
    await c._op_refresh()
    assert persisted[-1]["current_raw"] == 100

    assert (await c.async_enable()).outcome == "confirmed"
    assert io.regs[CUR] == 100
