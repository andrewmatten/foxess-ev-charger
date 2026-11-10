"""Latch regressions fixed in 3.1.3 (adversarial review, 2026-11).

Each test reproduces a way the charger was left stuck off, or a Stop that
worked was recorded as failed, with no human around to clear it.
"""
from __future__ import annotations

import time

import pytest

from custom_components.foxess_charger import controller as controller_module
from custom_components.foxess_charger.controller import FAULT_RELEASE_S, ControlError

from custom_components.foxess_charger.transport import TransportError

from test_comms_latch_release import FAULT_HI, FAULT_LO, cause, healthy_for, refresh
from test_controller import CTRL, POW, STATUS, make
from test_fault_and_stop_fallback import drain, positive_writes


async def test_failed_stop_releases_after_ten_minutes_at_any_hour(monkeypatch):
    """The 18:00-21:00 no-release window is gone: stuck off all evening is a failure too."""
    evening = time.struct_time((2026, 11, 6, 19, 0, 0, 4, 310, 0))
    monkeypatch.setattr(controller_module.time, "localtime", lambda *a: evening)
    c, io = await make()
    io.zero_pauses, io.stop_works = False, False   # heard, drawing through zero and Stop
    with pytest.raises(ControlError, match="did not stop"):
        await c.async_pause()
    assert c.phase == "faulted" and cause(c) == "stop_failed"
    io.zero_pauses = True                          # charger stops
    await healthy_for(c, io, FAULT_RELEASE_S + 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert not hasattr(controller_module, "NO_RELEASE_HOURS")


async def test_replay_6_nov_lost_zero_then_successful_stop_recovers_unattended():
    """Only the 0x3002 exchange fails for a while during a pause; 0x4001 Stop
    works and the charger sits at status 5. That Stop succeeded: it must not
    be recorded as stop_failed, and the controller must reach a plain pause."""
    c, io = await make()
    real_read = io.read
    bad = [True]

    async def read(address, count):
        if bad[0] and address == POW:
            raise TransportError("read-back lost")
        return await real_read(address, count)

    async def lost(value):
        if bad[0]:
            raise TransportError("write lost")

    io.read, io.hooks[POW] = read, lost
    with pytest.raises(ControlError):
        await c.async_pause()
    bad[0] = False                                 # ~10 s later the link is fine
    await drain(c)
    assert any(a == CTRL for _, a, _ in io.writes) and io.regs[STATUS] == 5
    state = c.export_state()
    assert state["stop_fallback"] and cause(c) != "stop_failed"
    start = io.clock.now
    await refresh(c, io, 1)
    while c.phase != "paused" and io.clock.now - start < 600:
        await refresh(c, io, 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == []


async def test_pause_during_soft_fault_does_not_escalate_to_stop_failed():
    """Status 6 with no power (overvoltage) is not drawing: a pause confirms
    without the Stop sequence, and the fault latch releases once it clears."""
    c, io = await make()
    io.status_override = 6
    io.regs[FAULT_LO] = 1 << 1                     # overvoltage, self-clearing
    io.car_draw = False
    await c.async_poll()
    await drain(c)
    assert cause(c) == "fault"
    await c.async_pause()                          # an automation turns the switch off
    assert cause(c) == "fault"
    assert [v for _, a, v in io.writes if a == CTRL] == []
    io.status_override = None                      # fault clears
    io.regs[FAULT_LO] = 0
    io.car_draw = True
    await healthy_for(c, io, FAULT_RELEASE_S - 30)
    assert c.phase == "faulted"
    await refresh(c, io, 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == []


async def test_fault_status_still_drawing_after_zero_and_stop_is_stop_failed():
    c, io = await make()
    io.status_override = 6                         # fault status but power still 7 kW
    io.zero_pauses, io.stop_works = False, False
    with pytest.raises(ControlError, match="did not stop"):
        await c.async_pause()
    assert c.phase == "faulted" and cause(c) == "stop_failed"
