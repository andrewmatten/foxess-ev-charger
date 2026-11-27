"""3.2 R5-5: Plug&Charge replug adoption requires a valid live cap read."""
from __future__ import annotations

import asyncio

from custom_components.foxess_charger.const import REG_MAX_POWER
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.transport import TransportError
from test_controller import CC, POW, POWER, STATUS, FakeClock, FakeIO, snapshot, spin


async def _fresh_unplugged(io_factory=None):
    clock = FakeClock()
    io = io_factory(clock) if io_factory else FakeIO(clock, charging=False)
    io.clock = clock
    io.regs[CC] = 0
    io.regs[0x3000] = 1  # Plug&Charge
    io._physics()
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(None, configure_safety=False)
    io.writes.clear()
    await c.async_poll()
    io.clock.now += 1.0
    await c.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)
    io.writes.clear()
    return c, io


async def test_replug_adoption_with_unreadable_live_cap_never_raises_2kw_session_to_max():
    """With no saved power and cached rating 7.3 kW, an active replugged
    session capped at 2 kW whose 0x3002 read times out must fail closed to zero
    rather than adopting and writing 7.3 kW."""
    c, io = await _fresh_unplugged()
    assert c._max_power == 73

    real_read = io.read
    fail_once = [True]

    async def read(address: int, count: int):
        if address == POW and fail_once[0]:
            fail_once[0] = False
            raise TransportError("0x3002 timed out")
        return await real_read(address, count)

    io.read = read
    io.regs[CC] = 1
    io.regs[POW] = 20  # charger currently capped at 2.0 kW
    io.stopped = False

    await c.async_poll()
    await spin(lambda: io.regs[POW] == 0)

    assert not c.intent_enabled
    assert c.desired_power_raw is None
    assert 73 not in io.power_writes()
    assert io.regs[POW] == 0
    assert io.regs[POWER] == 0


async def test_replug_adoption_requires_known_device_max_like_first_install():
    """If 0x1011 (device maximum) is unreadable, replug adoption cannot
    validate the live cap and must fail closed to paused/zero."""
    def make_io(clock):
        io = FakeIO(clock, charging=False)
        real_read = io.read

        async def read(address: int, count: int):
            if address == REG_MAX_POWER:
                raise TransportError("0x1011 unreadable")
            return await real_read(address, count)

        io.read = read
        return io

    c, io = await _fresh_unplugged(io_factory=make_io)
    assert c._max_power is None

    io.regs[CC] = 1
    io.regs[POW] = 50
    io.stopped = False

    await c.async_poll()
    await spin(lambda: io.regs[POW] == 0)

    assert not c.intent_enabled
    assert c.desired_power_raw is None
    assert 50 not in io.power_writes()
    assert io.regs[POWER] == 0


async def test_replug_adoption_keeps_valid_live_cap_without_raising():
    """When both device max and live cap read cleanly, adoption keeps the live
    2 kW cap and never raises it to rated maximum."""
    c, io = await _fresh_unplugged()
    assert c._max_power == 73

    io.regs[CC] = 1
    io.regs[POW] = 20
    io.stopped = False

    await c.async_poll()
    await spin(lambda: c.intent_enabled and c.desired_power_raw == 20)

    assert c.desired_power_raw == 20
    assert 73 not in io.power_writes()
    assert io.regs[POW] == 20 and io.regs[STATUS] == 3
