"""3.2 N18: 0x3005/0x3006 saves run outside controller lock and raise on save failure."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import FALLBACK, VALIDITY, make, saved


@pytest.mark.parametrize(
    ("reg", "value", "key"),
    [
        (VALIDITY, 30, "user_time_validity_s"),
        (FALLBACK, 160, "user_default_current_raw"),
    ],
)
async def test_safety_register_save_failure_raises_applied_but_not_saved(reg, value, key):
    fail = {"on": False}
    persisted: list[dict] = []

    async def persist(state):
        if fail["on"]:
            raise OSError("disk full")
        persisted.append(dict(state))

    c, io = await make(saved(), persist=persist)
    fail["on"] = True

    with pytest.raises(ControlError, match="applied but not saved"):
        await c.async_set_register(reg, value)

    # Register took effect on hardware, and refresh retries persistence.
    assert io.regs[reg] == value
    assert c.diagnostics["persist_pending"] is True

    fail["on"] = False
    await c._op_refresh()
    assert not c.diagnostics["persist_pending"]
    assert persisted[-1][key] == value


async def test_safety_register_save_does_not_hold_controller_lock():
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(state):
        if state.get("user_time_validity_s") == 30 and not entered.is_set():
            entered.set()
            await release.wait()

    c, io = await make(saved(), persist=persist)
    task = asyncio.create_task(c.async_set_register(VALIDITY, 30))
    await asyncio.wait_for(entered.wait(), 1)

    assert not c._lock.locked()
    await asyncio.wait_for(c.async_poll(), 1)

    release.set()
    res = await task
    assert res.outcome == "confirmed"
    assert io.regs[VALIDITY] == 30
