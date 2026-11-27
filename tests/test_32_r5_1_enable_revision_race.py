"""3.2 R5-1: async_enable captures intent revision before awaiting device max read."""
from __future__ import annotations

import asyncio

from custom_components.foxess_charger.const import REG_MAX_POWER
from test_controller import CTRL, POW, POWER, STATUS, make, saved
from test_fault_and_stop_fallback import drain, positive_writes


async def test_zero_power_limit_during_first_enable_max_read_supersedes_enable():
    """A peak-tariff zero power limit landing while async_enable awaits 0x1011
    must supersede the enable rather than being overwritten with rated power."""
    c, io = await make(saved(enabled=False, power=None), charging=False)
    io.regs[0x3000] = 0  # Controlled mode
    io.writes.clear()

    entered, release = asyncio.Event(), asyncio.Event()
    real_read = io.read

    async def read(address: int, count: int):
        if address == REG_MAX_POWER and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_read(address, count)

    io.read = read

    t_enable = asyncio.create_task(c.async_enable())
    await asyncio.wait_for(entered.wait(), 1)

    res_zero = await c.async_set_power(0)
    assert res_zero.outcome == "staged"
    assert c.desired_power_raw == 0

    release.set()
    res_enable = await t_enable
    await drain(c)

    assert res_enable.outcome == "superseded"
    assert not c.intent_enabled
    assert c.desired_power_raw == 0
    assert positive_writes(io) == []
    assert (CTRL, 1) not in [(a, v) for _, a, v in io.writes]
    assert io.regs[STATUS] != 3 and io.regs[POWER] == 0
    assert c.diagnostics["superseded"] >= 1


async def test_pause_during_first_enable_max_read_supersedes_enable():
    """An explicit pause landing while async_enable awaits 0x1011 must supersede
    the older enable and never write rated power or send Start."""
    c, io = await make(saved(enabled=False, power=None), charging=False)
    io.regs[0x3000] = 0  # Controlled mode
    io.writes.clear()

    entered, release = asyncio.Event(), asyncio.Event()
    real_read = io.read

    async def read(address: int, count: int):
        if address == REG_MAX_POWER and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_read(address, count)

    io.read = read

    t_enable = asyncio.create_task(c.async_enable())
    await asyncio.wait_for(entered.wait(), 1)

    t_pause = asyncio.create_task(c.async_pause())
    for _ in range(10):
        await asyncio.sleep(0)

    release.set()
    res_enable = await t_enable
    res_pause = await t_pause
    await drain(c)

    assert res_enable.outcome == "superseded"
    assert res_pause.outcome == "confirmed"
    assert not c.intent_enabled
    assert c.desired_power_raw is None
    assert positive_writes(io) == []
    assert (CTRL, 1) not in [(a, v) for _, a, v in io.writes]
    assert io.regs[POW] == 0 and io.regs[POWER] == 0
