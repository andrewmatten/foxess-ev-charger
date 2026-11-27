"""3.2 N16: setup fails closed on unreadable live cap and unconfirmed safety config."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ChargingController, ControlError
from custom_components.foxess_charger.transport import TransportError

from test_controller import (
    FALLBACK,
    POW,
    VALIDITY,
    FakeClock,
    FakeIO,
    saved,
    snapshot,
)


async def test_setup_with_unreadable_power_cap_fails_closed():
    """Restoring enabled=True when 0x3002 cannot be read must pause (fail closed),
    not assume the charger held a positive cap."""
    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    real_read = io.read
    fail_cap = [True]

    async def read(address, count):
        if address == POW and fail_cap[0]:
            fail_cap[0] = False
            raise TransportError("0x3002 unreadable")
        return await real_read(address, count)

    io.read = read
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(enabled=True, power=70), configure_safety=False)

    assert not c.intent_enabled
    assert "unreadable" in (c.diagnostics["last_error"] or "")
    assert not [v for _, a, v in io.writes if a == POW and v > 0]
    assert io.regs[POW] == 0


@pytest.mark.parametrize("bad_cap", [0, 10, 65535])
async def test_setup_with_zero_or_invalid_power_cap_fails_closed(bad_cap):
    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    real_read = io.read
    override = [True]

    async def read(address, count):
        if address == POW and override[0]:
            override[0] = False
            return (bad_cap,)
        return await real_read(address, count)

    io.read = read
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(enabled=True, power=70), configure_safety=False)

    assert not c.intent_enabled
    assert not [v for _, a, v in io.writes if a == POW and v > 0]
    assert io.regs[POW] == 0


async def test_setup_confirms_safety_before_first_positive_cap_refresh():
    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(enabled=True, power=50), configure_safety=True)

    addrs = [a for _, a, _ in io.writes]
    assert VALIDITY in addrs and FALLBACK in addrs and POW in addrs
    first_pow = addrs.index(POW)
    assert addrs.index(VALIDITY) < first_pow
    assert addrs.index(FALLBACK) < first_pow
    assert io.regs[POW] == 50


@pytest.mark.parametrize("blocked_reg", [VALIDITY, FALLBACK])
async def test_setup_with_unconfirmed_safety_writes_only_zero_never_positive(blocked_reg):
    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    io.ignore.add(blocked_reg)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(enabled=True, power=50), configure_safety=True)

    assert c.phase == "faulted"
    assert "safety_config_unconfirmed" in c.diagnostics["protective_reasons"]
    assert not [v for _, a, v in io.writes if a == POW and v > 0]
    assert io.regs[POW] == 0
    with pytest.raises(ControlError):
        await c.async_enable()
