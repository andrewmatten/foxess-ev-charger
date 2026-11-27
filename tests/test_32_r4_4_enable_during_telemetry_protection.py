"""3.2 R4-4: async_enable during telemetry protection must run preflight and session confirmation."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from rebuild_simulator import CHARGING
from test_controller import CC, POW, STATUS, FakeClock, FakeIO, make, saved
from test_fault_and_stop_fallback import drain, positive_writes
from test_rebuild_contract import CAP, CTRL, rigs  # noqa: F401 - rigs is a fixture


async def _arm_telemetry_protect(c, io) -> None:
    io.status_override = 65535
    await c.async_poll()
    io.clock.now += 25
    await c.async_poll()
    await drain(c)
    assert "telemetry_uncertain" in c.diagnostics["protective_reasons"]
    io.writes.clear()


async def test_enable_while_telemetry_invalid_raises_and_revokes_never_confirms():
    """Calling async_enable while telemetry is still invalid after the grace
    must fail and revoke rather than writing 0 and returning confirmed."""
    c, io = await make(saved(enabled=False, power=70), charging=False)
    await _arm_telemetry_protect(c, io)

    with pytest.raises(ControlError):
        await c.async_enable()
    await drain(c)

    assert not c.intent_enabled
    assert io.regs[POW] == 0
    assert positive_writes(io) == []


async def test_enable_while_telemetry_protect_active_recovers_and_starts_controlled_mode(rigs):
    """When telemetry has just recovered after an outage, async_enable must wait
    for two-frame telemetry recovery in _preflight, write the positive cap, and
    send Start in Controlled mode."""
    rig = rigs(work_mode=0, implicit_resume=False)
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))

    rig.sim.set_invalid_telemetry(status=65535, cc=None)
    await rig.run(ctl.async_poll())
    await rig.clock.advance(25)
    await rig.run(ctl.async_poll())
    assert "telemetry_uncertain" in ctl.diagnostics["protective_reasons"]

    # Link recovers; _REASON_TELEMETRY is still active until two agreeing frames.
    rig.sim.clear_invalid_telemetry()
    mark = rig.sim.mark()
    result = await rig.run(ctl.async_enable())

    assert result.outcome == "confirmed"
    assert result.observed == CAP
    assert ctl.intent_enabled and ctl.phase == "enabled"
    assert rig.sim.state == CHARGING
    assert rig.sim.power_limit_raw() == CAP
    assert (CTRL, 1) in rig.sim.wire_writes(mark)

    # An immediate post-command poll (as switch.py runs) keeps the session on.
    await rig.run(ctl.async_poll())
    assert ctl.intent_enabled and rig.sim.state == CHARGING


async def test_enable_while_telemetry_protect_active_unplugged_stages_await_plug():
    """Enabling unplugged right after a telemetry outage must stage with
    _await_plug=True instead of writing 0 and returning confirmed."""
    c, io = await make(saved(enabled=False, power=70), charging=False)
    io.regs[CC] = 0
    await _arm_telemetry_protect(c, io)

    io.status_override = None
    result = await c.async_enable()

    assert result.outcome == "staged"
    assert result.observed == 70
    assert c.intent_enabled and c._await_plug

    await c.async_poll()
    assert c.intent_enabled and c._await_plug


async def test_human_enable_clear_hard_during_telemetry_protect_keeps_or_clears_properly():
    """With clear_hard=True while telemetry protection is active: invalid
    telemetry must fail and restore hard; recovered telemetry must complete
    preflight and clear the hard latch cleanly (never misclassify as overtaken)."""
    io = FakeIO(FakeClock(), charging=False)
    io.regs[POW] = 0
    c, io = await make(
        saved(enabled=False, power=70, safety_latched=True, latch_cause="hard"),
        io=io,
    )
    await _arm_telemetry_protect(c, io)

    # Still invalid telemetry: must fail and preserve the hard latch.
    with pytest.raises(ControlError, match="hard fault latch restored"):
        await c.async_enable(clear_hard=True)
    await drain(c)
    assert c.needs_human and c.latch_cause == "hard" and not c.intent_enabled

    # Telemetry recovers while _REASON_TELEMETRY is still active: must recover
    # through _preflight and start cleanly without silently re-latching hard.
    io.status_override = None
    assert "telemetry_uncertain" in c.diagnostics["protective_reasons"]
    result = await c.async_enable(clear_hard=True)

    assert result.outcome == "confirmed" and result.observed == 70
    assert not c.needs_human and c.latch_cause is None
    assert c.intent_enabled and io.regs[POW] == 70 and io.regs[STATUS] == 3
