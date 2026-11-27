"""3.2 N13: 0x3001 (current) refresh gets the same ack/retry and cap-ack protection as 0x3002."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import CAP_ACK_LIMIT_S
from rebuild_simulator import CHARGING
from test_controller import CUR, POW, make, saved
from test_rebuild_contract import C, FULL, rigs  # noqa: F401 - fixture


@pytest.mark.parametrize("mode", ["transport_error", "lost_request", "lost_reply", "refuse"])
async def test_unacknowledged_current_refresh_retries_at_retry_interval(rigs, mode):
    """An unacknowledged 0x3001 refresh write whose read-back still holds the
    old value must be retried at retry_interval (3 s), not a full 20 s later."""
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.run(ctl.async_set_current(100))
    await rig.enable_at(FULL)
    assert rig.sim.state == CHARGING
    await rig.clock.advance(31)

    mark = rig.sim.mark()
    failures = ctl.diagnostics["refresh_failures"]
    rig.sim.inject_write(C, mode, count=2)
    for _ in range(6):
        await rig.run(ctl.async_poll())
        await rig.clock.advance(10)

    writes = rig.sim.writes_since(mark, C)
    assert [w.mode for w in writes[:3]] == [mode, mode, "ok"]
    gaps = [b.t - a.t for a, b in zip(writes, writes[1:3])]
    assert gaps == [pytest.approx(3.0, abs=0.5)] * 2
    assert ctl.diagnostics["refresh_failures"] - failures >= 2
    assert rig.sim.state == CHARGING and ctl.phase == "enabled"


async def test_sustained_unacknowledged_current_refresh_writes_protective_zero():
    """If 0x3001 goes unacknowledged for CAP_ACK_LIMIT_S while 0x3002 succeeds,
    the controller must write protective 0x3002 = 0 in that same refresh."""
    c, io = await make(saved(power=70, current=100), retry_interval=3.0)
    io.lose_reply.add(CUR)
    io.clock.now += CAP_ACK_LIMIT_S + 1

    ok = await c._op_refresh()
    assert not ok
    assert 0 in io.power_writes()
    assert io.regs[POW] == 0
    assert c.phase == "uncertain"
    assert "current cap" in (c.diagnostics["last_error"] or "")


async def test_power_only_refresh_does_not_trip_current_cap_guard():
    c, io = await make(saved(power=70, current=None), retry_interval=3.0)
    for _ in range(5):
        io.clock.now += 20
        assert await c._op_refresh()
    assert c.phase == "enabled"
    assert not [w for w in io.writes if w[1] == CUR]
