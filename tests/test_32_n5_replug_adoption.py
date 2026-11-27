"""3.2 N5: Plug&Charge replug adoption requires known Work Mode and valid power_raw."""
from __future__ import annotations

import asyncio

from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.transport import TransportError

from test_controller import (
    CC,
    POW,
    POWER,
    STATUS,
    FakeClock,
    FakeIO,
    make,
    saved,
    snapshot,
    spin,
)


async def _poll_twice(c, io, gap=1.0):
    await c.async_poll()
    io.clock.now += gap
    await c.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)


async def _fresh_unplugged(work_mode=1, io_factory=None):
    clock = FakeClock()
    io = io_factory(clock) if io_factory else FakeIO(clock, charging=False)
    io.clock = clock
    io.regs[CC] = 0
    if work_mode is not None:
        io.regs[0x3000] = work_mode
    io._physics()
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(None, configure_safety=False)
    io.writes.clear()
    assert not c.intent_enabled and c.desired_power_raw is None
    await _poll_twice(c, io)
    io.writes.clear()
    return c, io


async def test_replug_adoption_populates_power_raw_and_survives_restart():
    c, io = await _fresh_unplugged(work_mode=1)
    io.regs[CC] = 1
    io.regs[POW] = 50
    io.stopped = False
    await c.async_poll()
    await spin(lambda: c.intent_enabled and io.regs[POW] == 50)
    assert c.desired_power_raw == 50

    # Restart mid-session: must keep charging, not pause for missing power_raw.
    state = c.export_state()
    clock2 = FakeClock()
    io2 = FakeIO(clock2, charging=True)
    io2.regs[0x3000] = 1
    io2.regs[POW] = 50
    io2._physics()
    c2 = ChargingController(io2, clock=clock2, sleep=clock2.sleep, read_snapshot=snapshot)
    await c2.async_initialize(state, configure_safety=False)
    assert c2.intent_enabled
    assert c2.desired_power_raw == 50
    assert 0 not in io2.power_writes()
    assert io2.regs[STATUS] == 3


async def test_replug_adoption_falls_back_to_device_max_when_live_cap_unreadable():
    c, io = await _fresh_unplugged(work_mode=1)
    real_read = io.read
    fail_once = [True]

    async def read(address, count):
        if address == POW and fail_once[0]:
            fail_once[0] = False
            raise TransportError("0x3002 unreadable")
        return await real_read(address, count)

    io.read = read
    io.regs[CC] = 1
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    await spin(lambda: c.intent_enabled and c.desired_power_raw == 73)
    assert io.regs[POW] == 73 and io.regs[STATUS] == 3


async def test_replug_adoption_fails_closed_when_cap_and_max_unreadable():
    def make_io(clock):
        io = FakeIO(clock, charging=False)
        real_read = io.read
        io.block_cap = True

        async def read(address, count):
            if address == 0x1011:
                raise TransportError("0x1011 unreadable")
            if address == POW and io.block_cap:
                raise TransportError("0x3002 unreadable")
            return await real_read(address, count)

        io.read = read
        return io

    c, io = await _fresh_unplugged(work_mode=1, io_factory=make_io)
    io.regs[CC] = 1
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    io.block_cap = False
    await spin(lambda: io.regs[POW] == 0)
    assert not c.intent_enabled
    assert c.desired_power_raw is None
    assert io.regs[POWER] == 0


async def test_replug_does_not_adopt_when_work_mode_unreadable():
    c, io = await make(saved(enabled=False, power=50), charging=False)
    io.regs[CC] = 0
    await _poll_twice(c, io)
    io.writes.clear()

    real_read = io.read

    async def read(address, count):
        if address == 0x3000:
            raise TransportError("0x3000 unreadable")
        return await real_read(address, count)

    io.read = read
    io.regs[CC] = 1
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    await spin(lambda: io.regs[POW] == 0)
    assert not c.intent_enabled
    assert io.regs[POWER] == 0


async def test_replug_re_reads_work_mode_and_refuses_controlled_mode():
    c, io = await make(saved(enabled=False, power=50), charging=False)
    io.regs[0x3000] = 0  # Controlled; _work_mode is still None (snapshot omits 0x3000)
    io.regs[CC] = 0
    await _poll_twice(c, io)
    io.writes.clear()

    io.regs[CC] = 1
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    await spin(lambda: io.regs[POW] == 0)
    assert not c.intent_enabled
    assert io.regs[POWER] == 0
