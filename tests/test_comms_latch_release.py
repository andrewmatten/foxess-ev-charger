"""A fault latch caused only by a lost link releases itself (paused).

Incident 2026-10-30: a Modbus timeout during a write latched the charger,
and it stayed latched for ~21 h although the charger was healthy. A latch
whose only cause was an unconfirmed write/read-back now clears after
COMMS_LATCH_RELEASE_S of healthy link (valid, fault-free, not drawing, zero
confirmed), with charging still paused. Hard faults, a failed Stop and an
unknown restored cause stay latched until an explicit enable.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import COMMS_LATCH_RELEASE_S, ControlError

from test_controller import POW, make, saved
from test_fault_and_stop_fallback import drain, positive_writes


async def comms_latched():
    """Paused charger; the link drops during the positive write of an enable."""
    c, io = await make(saved(enabled=False))

    async def link_drops(value):
        if value > 0:
            io.fail_reads = True
            io.hooks.pop(POW)

    io.hooks[POW] = link_drops
    with pytest.raises(ControlError):
        await c.async_enable()
    assert c.phase == "faulted" and c.export_state()["comms_latch"] is True
    assert c.intent_enabled          # latched mid-enable: the enable is still stored
    io.fail_reads = False            # link back, charger healthy
    io.writes.clear()
    return c, io


async def refresh(c, io, seconds):
    io.clock.now += seconds
    await c._op_refresh()
    await drain(c)


async def test_comms_latch_releases_paused_after_healthy_link():
    c, io = await comms_latched()
    await refresh(c, io, 1)                        # timer starts
    for _ in range(3):
        await refresh(c, io, 30)
    assert c.phase == "faulted"                    # 90 s: not yet
    await refresh(c, io, 30)                       # 120 s healthy
    assert c.phase == "paused" and not c.intent_enabled
    state = c.export_state()
    assert not state["safety_latched"] and not state["comms_latch"]
    for _ in range(5):                             # nothing resumes by itself
        await c.async_poll()
        await refresh(c, io, 30)
    assert positive_writes(io) == [] and io.regs[POW] == 0
    assert (await c.async_enable()).outcome == "confirmed"   # normal enable works
    assert io.regs[POW] == 70


async def test_status_fault_latch_does_not_release():
    c, io = await make()
    io.status_override = 6
    await c.async_poll()
    await drain(c)
    assert c.phase == "faulted" and c.export_state()["comms_latch"] is False
    io.status_override = None
    for _ in range(10):
        await refresh(c, io, 30)
    assert c.phase == "faulted" and positive_writes(io) == []


async def test_fault_during_comms_latch_makes_it_permanent():
    c, io = await comms_latched()
    io.status_override = 6
    await c.async_poll()
    await drain(c)
    assert c.export_state()["comms_latch"] is False
    io.status_override = None
    for _ in range(10):
        await refresh(c, io, 30)
    assert c.phase == "faulted"


async def test_failed_stop_latch_does_not_release():
    c, io = await make()
    io.zero_pauses, io.stop_works = False, False
    with pytest.raises(ControlError, match="did not stop"):
        await c.async_pause()
    assert c.phase == "faulted" and c.export_state()["comms_latch"] is False
    io.zero_pauses = True                          # charger healthy again
    for _ in range(10):
        await refresh(c, io, 30)
    assert c.phase == "faulted" and positive_writes(io) == []


async def test_comms_latch_with_charger_ignoring_zero_and_stop_does_not_release():
    c, io = await comms_latched()
    io.zero_pauses, io.stop_works = False, False   # still drawing despite zero and Stop
    await c.async_poll()                           # correction -> Stop fallback fails
    await drain(c)
    assert c.export_state()["comms_latch"] is False
    io.zero_pauses = True
    for _ in range(10):
        await refresh(c, io, 30)
    assert c.phase == "faulted" and positive_writes(io) == []


async def test_drawing_or_invalid_read_resets_release_timer():
    c, io = await comms_latched()
    await refresh(c, io, 1)                        # timer starts at T
    await refresh(c, io, 60)
    io.clock.now += 10
    io.status_override = 3                         # one frame shows charging
    await c.async_poll()
    io.status_override = None
    await drain(c)
    await refresh(c, io, 60)                       # T+130: would release without reset
    assert c.phase == "faulted"                    # timer restarted here
    io.clock.now += 10
    io.fail_reads = True                           # one unreadable frame
    await c.async_poll()
    io.fail_reads = False
    await refresh(c, io, 110)                      # 120 s since the restart
    assert c.phase == "faulted"                    # restarted again
    await refresh(c, io, COMMS_LATCH_RELEASE_S)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == []


@pytest.mark.parametrize("cause", [{}, {"comms_latch": "yes"}])
async def test_restored_latch_with_unknown_cause_stays_latched(cause):
    c, io = await make(saved(enabled=False, safety_latched=True, **cause))
    assert c.export_state()["comms_latch"] is False
    for _ in range(10):
        await refresh(c, io, 30)
    assert c.phase == "faulted"


async def test_restored_comms_latch_releases_paused():
    c, io = await make(saved(enabled=True, safety_latched=True, comms_latch=True))
    assert c.phase == "faulted" and c.export_state()["comms_latch"] is True
    for _ in range(6):
        await refresh(c, io, 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == [] and io.regs[POW] == 0
