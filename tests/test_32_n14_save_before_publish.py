"""3.2 N14: save-before-publish so refresh never writes an unsaved higher cap or enable."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import CUR, POW, make, saved


async def test_refresh_never_writes_power_increase_before_save_succeeds():
    """While a power increase's persist is still in flight (and about to fail),
    a concurrent refresh must keep writing the last durably saved cap."""
    entered, release = asyncio.Event(), asyncio.Event()
    fail_high = {"on": True}

    async def persist(state):
        if state["power_raw"] == 70:
            entered.set()
            await release.wait()
            if fail_high["on"]:
                raise OSError("disk full")

    c, io = await make(saved(power=14), persist=persist)
    assert io.regs[POW] == 14

    task = asyncio.create_task(c.async_set_power(70))
    await asyncio.wait_for(entered.wait(), 1)

    # Refresh runs before save completes: must still see and write 14, never 70.
    assert c.desired_power_raw == 14
    await c._op_refresh()
    assert 70 not in io.power_writes()
    assert io.regs[POW] == 14

    release.set()
    with pytest.raises(ControlError):
        await task
    assert c.desired_power_raw == 14
    assert 70 not in io.power_writes()
    assert io.regs[POW] == 14


async def test_refresh_never_writes_current_increase_before_save_succeeds():
    """While a current increase's persist is in flight, refresh must keep
    writing the last durably saved current."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(state):
        if state["current_raw"] == 200 and not entered.is_set():
            entered.set()
            await release.wait()

    c, io = await make(saved(power=70, current=100), persist=persist)
    assert io.regs[CUR] == 100

    task = asyncio.create_task(c.async_set_current(200))
    await asyncio.wait_for(entered.wait(), 1)

    assert c.desired_current_raw == 100
    await c._op_refresh()
    cur_writes = [v for _, a, v in io.writes if a == CUR]
    assert 200 not in cur_writes
    assert io.regs[CUR] == 100

    release.set()
    res = await task
    assert res.outcome == "confirmed"
    assert c.desired_current_raw == 200
    assert io.regs[CUR] == 200


async def test_refresh_never_writes_positive_during_enable_save():
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(state):
        if state["enabled"]:
            entered.set()
            await release.wait()
            raise OSError("disk full")

    c, io = await make(saved(enabled=False, power=70), charging=False, persist=persist)
    task = asyncio.create_task(c.async_enable())
    await asyncio.wait_for(entered.wait(), 1)

    assert not c.intent_enabled
    await c._op_refresh()
    assert not [v for _, a, v in io.writes if a in (CUR, POW) and v > 0]
    assert io.regs[POW] == 0

    release.set()
    with pytest.raises(ControlError):
        await task
    assert not c.intent_enabled
    assert io.regs[POW] == 0


async def test_pause_during_in_flight_increase_save_supersedes_and_persists_pause():
    entered, release = asyncio.Event(), asyncio.Event()
    saved_states: list[dict] = []

    async def persist(state):
        if state["power_raw"] == 70 and not entered.is_set():
            entered.set()
            await release.wait()
        saved_states.append(dict(state))

    c, io = await make(saved(power=14), persist=persist)
    task = asyncio.create_task(c.async_set_power(70))
    await asyncio.wait_for(entered.wait(), 1)

    assert (await c.async_pause()).outcome == "confirmed"
    release.set()
    res = await task

    assert res.outcome == "superseded"
    assert not c.intent_enabled
    assert c.desired_power_raw == 14
    assert saved_states[-1]["enabled"] is False
    assert 70 not in io.power_writes()
