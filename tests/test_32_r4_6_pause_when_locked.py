"""3.2 R4-6: pausing while charger is locked (status 8) and not drawing confirms cleanly."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import CTRL, POW, make, saved


async def test_pause_while_locked_and_not_drawing_confirms_without_stop_or_fault_latch():
    """Status 8 (locked) with zero power must confirm a pause quickly rather
    than hanging 40 s, sending 0x4001 Stop, and latching stop_failed."""
    c, io = await make()
    io.status_override = 8
    io.car_draw = False
    t0 = io.clock.now

    result = await c.async_pause()

    assert result.outcome == "confirmed" and result.observed == 0
    assert io.clock.now - t0 < 5.0
    assert io.regs[POW] == 0
    assert not any(a == CTRL for _, a, _ in io.writes)
    assert c.phase == "paused" and not c.intent_enabled and c.latch_cause is None


async def test_pause_while_locked_but_still_drawing_triggers_stop_and_latches():
    """Safety guard: if status 8 still reports active power draw, pause must
    still escalate to 0x4001 Stop and latch stop_failed."""
    c, io = await make()
    io.status_override = 8
    io.zero_pauses, io.stop_works = False, False

    with pytest.raises(ControlError, match="did not stop"):
        await c.async_pause()

    assert (CTRL, 2) in [(a, v) for _, a, v in io.writes]
    assert c.phase == "faulted" and c.latch_cause == "stop_failed"


async def test_stop_fallback_pause_confirms_when_locked_and_not_drawing():
    """A pause while already in stop_fallback also confirms when status is 8
    and power is zero."""
    c, io = await make(saved(enabled=False, power=70, stop_fallback=True), charging=False)
    io.status_override = 8
    io.car_draw = False
    t0 = io.clock.now

    result = await c.async_pause()

    assert result.outcome == "confirmed" and result.observed is None
    assert io.clock.now - t0 < 5.0
    assert not any(a == CTRL for _, a, _ in io.writes)
    assert c.phase == "stopped" and c.latch_cause is None
