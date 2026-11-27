"""3.2 R1-M2: missing 0x3001 acks retry and flag degraded without zeroing 0x3002 power."""
from __future__ import annotations

from custom_components.foxess_charger.controller import CAP_ACK_LIMIT_S
from test_controller import CUR, POW, POWER, make, saved


async def test_refused_current_writes_keep_charging_with_power_refreshed():
    """A unit that refuses 0x3001 writes while holding the current value keeps
    charging with 0x3002 refreshed, retrying 0x3001 and flagging degraded."""
    c, io = await make(saved(power=70, current=100), retry_interval=3.0)
    io.refuse.add(CUR)

    for _ in range(8):
        io.clock.now += 15
        ok = await c._op_refresh()
        assert not ok
        assert c.phase == "enabled"
        assert "current_cap_mismatch" in c.diagnostics["degraded_reasons"]
        assert io.regs[POW] == 70
        assert io.regs[POWER] == 70

    assert 0 not in io.power_writes()
    assert len(io.power_writes()) == 8
    assert all(v == 70 for v in io.power_writes())
    assert c.intent_enabled and not c.export_state()["safety_latched"]


async def test_unacked_current_never_zeroes_power_while_power_ack_guard_still_fires():
    """Missing 0x3001 acks alone do not trigger protective zero, clear degraded
    once acknowledged, and leave the 0x3002 cap-ack guard intact."""
    c, io = await make(saved(power=70, current=100), retry_interval=3.0)
    io.lose_reply.add(CUR)
    io.clock.now += CAP_ACK_LIMIT_S + 20

    assert not await c._op_refresh()
    assert 0 not in io.power_writes()
    assert io.regs[POW] == 70
    assert c.phase == "enabled"
    assert "current_cap_mismatch" in c.diagnostics["degraded_reasons"]

    # Once 0x3001 acknowledges again, degraded clears cleanly.
    io.lose_reply.discard(CUR)
    io.clock.now += 3
    assert await c._op_refresh()
    assert "current_cap_mismatch" not in c.diagnostics["degraded_reasons"]
    assert c.phase == "enabled"

    # The 0x3002 power cap-ack guard still writes protective zero on timeout.
    io.writes.clear()
    io.lose_reply.add(POW)
    io.clock.now += CAP_ACK_LIMIT_S + 1
    assert not await c._op_refresh()
    assert 0 in io.power_writes()
    assert io.regs[POW] == 0
    assert c.phase == "uncertain"
