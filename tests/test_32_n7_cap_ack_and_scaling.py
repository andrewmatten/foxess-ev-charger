"""3.2 N7: immediate, uncancellable cap-ack protective zero and short-validity scaling."""
from __future__ import annotations

from custom_components.foxess_charger.controller import CAP_ACK_LIMIT_S, ChargingController
from test_controller import POW, VALIDITY, FakeClock, FakeIO, make, saved, snapshot, spin


async def test_cap_ack_limit_writes_zero_in_same_refresh():
    """Once CAP_ACK_LIMIT_S passes without an acknowledged 0x3002 write, the
    protective zero must go out in that same refresh (not deferred to the retry)."""
    c, io = await make(retry_interval=3.0)
    io.lose_reply.add(POW)
    io.clock.now += CAP_ACK_LIMIT_S + 1
    ok = await c._op_refresh()
    assert not ok
    assert 0 in io.power_writes()
    assert io.regs[POW] == 0
    assert c.phase == "uncertain"


async def test_healthy_polls_cannot_cancel_cap_ack_protective_zero():
    """When writes fail while status reads stay healthy, polls between retries
    must not clear cap-ack protection and let a positive write slip through."""
    c, io = await make(retry_interval=3.0)
    io.refuse.add(POW)  # all writes to 0x3002 fail while status reads stay valid

    io.clock.now += CAP_ACK_LIMIT_S + 1
    await c._op_refresh()
    assert io.power_writes()[-1] == 0
    assert c.phase == "uncertain"

    # Healthy poll during the 3 s retry window (used to set _good_first so the
    # next refresh cleared _REASON_TELEMETRY and wrote positive 70 again).
    io.clock.now += 1.0
    await c.async_poll()
    io.clock.now += 2.0
    before = len(io.power_writes())
    await c._op_refresh()
    retry_writes = io.power_writes()[before:]
    assert retry_writes == [0]
    assert c.phase == "uncertain"


async def test_short_validity_scales_cap_ack_limit_and_telemetry_grace():
    """With a 15 s user validity, both cap-ack limit (~11.25 s) and telemetry
    grace (5 s) scale proportionally so protection acts before expiry."""
    clock = FakeClock()
    io = FakeIO(clock)
    io.regs[VALIDITY] = 15
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(user_time_validity_s=15), configure_safety=True)
    io.writes.clear()

    # Cap-ack limit at 15 s is 11.25 s (< 15 s expiry).
    io.lose_reply.add(POW)
    io.clock.now += 12.0
    await c._op_refresh()
    assert 0 in io.power_writes()
    assert io.regs[POW] == 0
    assert c.phase == "uncertain"

    # Recover, then verify telemetry grace scales to 5 s at 15 s validity.
    io.lose_reply.clear()
    io.clock.now += 1.0
    await c._op_refresh()
    assert c.phase == "enabled" and io.regs[POW] == 70
    io.writes.clear()

    io.fail_reads = True
    await c.async_poll()
    io.clock.now += 5.5  # > 5.0 s scaled grace, well below 20 s default
    await c.async_poll()
    await spin(lambda: 0 in io.power_writes())
    assert c.phase == "uncertain"


async def test_guards_keep_current_values_at_60s_and_180s():
    c, _ = await make()
    for validity in (60, 180, None):
        c._validity = validity
        assert c._cap_ack_limit() == 45.0
        assert c._telemetry_grace() == 20.0
