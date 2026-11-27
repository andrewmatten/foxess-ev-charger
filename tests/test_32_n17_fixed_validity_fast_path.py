"""3.2 N17: fixed-validity models (0x3005 exception 3) skip confirmation retries."""
from __future__ import annotations

from custom_components.foxess_charger.controller import ChargingController
from test_controller import (
    FALLBACK,
    POW,
    VALIDITY,
    FakeClock,
    FakeIO,
    saved,
    snapshot,
)


async def test_fixed_validity_exception_3_takes_fast_path():
    clock = FakeClock()
    t0 = clock.now
    io = FakeIO(clock)
    io.refuse.add(VALIDITY)
    io.refuse_code = 3
    io.regs[VALIDITY] = 180

    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(), configure_safety=True)

    assert clock.now == t0
    assert [v for _, a, v in io.writes if a == VALIDITY] == [60]
    assert io.regs[FALLBACK] == 60
    assert c.phase == "enabled"
    assert c.diagnostics["reported_time_validity"] == 180
    assert c.diagnostics["refresh_interval"] == 20


async def test_other_validity_refusal_still_retries_until_timeout():
    clock = FakeClock()
    t0 = clock.now
    io = FakeIO(clock)
    io.refuse.add(VALIDITY)
    io.refuse_code = 6  # device busy: transient, should retry

    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(), configure_safety=True)

    assert clock.now >= t0 + 10.0
    assert len([w for w in io.writes if w[1] == VALIDITY]) >= 3
    assert c.phase == "faulted"


async def test_exception_3_with_implausible_held_validity_fails_fast():
    clock = FakeClock()
    t0 = clock.now
    io = FakeIO(clock)
    io.refuse.add(VALIDITY)
    io.refuse_code = 3
    io.regs[VALIDITY] = 3600

    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(), configure_safety=True)

    assert clock.now == t0
    assert [v for _, a, v in io.writes if a == VALIDITY] == [60]
    assert c.phase == "faulted"
    assert io.regs[POW] == 0
