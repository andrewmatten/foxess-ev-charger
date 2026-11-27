"""3.2 R6-1 (Astra re-check): a pause that lands while Plug&Charge replug
adoption is reading the live cap wins; adoption never overwrites it."""
from __future__ import annotations

import asyncio

from test_controller import CC, POW, spin
from test_32_r5_5_replug_adoption_live_cap import _fresh_unplugged


async def test_pause_during_adoption_read_is_not_overwritten():
    c, io = await _fresh_unplugged()
    real_read = io.read
    paused = []

    async def read(address: int, count: int):
        if address == POW and not paused:
            paused.append(asyncio.get_running_loop().create_task(c.async_pause()))
            for _ in range(20):
                await asyncio.sleep(0)
        return await real_read(address, count)

    io.read = read
    io.regs[CC] = 1
    io.regs[POW] = 20
    io.stopped = False
    await c.async_poll()
    for _ in range(200):
        await asyncio.sleep(0)
    assert paused
    assert not c.intent_enabled
    assert all(w == 0 for w in io.power_writes())
