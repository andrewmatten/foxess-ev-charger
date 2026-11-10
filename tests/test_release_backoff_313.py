"""Fault-latch auto-release backoff (3.1.3).

Replaces the 3-per-24 h cap, which left a 4th soft fault on a hot day
latched for about 21 h. Every releasable cause (comms, fault, stop_failed)
releases after a healthy hold of base * 2**n, at most 2 h; n counts the
releases of the current streak, which ends after 2 h without a latch. Only a
hard latch needs a human.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import (
    COMMS_LATCH_RELEASE_S, FAULT_RELEASE_S, ControlError,
)

from test_comms_latch_release import (
    FAULT_LO, comms_latched, drop_link_during_enable, healthy_for, refresh,
)
from test_controller import POW, make, saved
from test_fault_and_stop_fallback import drain, positive_writes

CEILING = 7200          # the 2 h hold ceiling and streak memory


async def soft_fault(c, io):
    """A self-clearing fault (overvoltage) shows for one frame, then clears."""
    io.regs[FAULT_LO] = 1 << 1
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    assert c.phase == "faulted" and c.latch_cause == "fault"


async def measure_hold(c, io, step=10):
    """Healthy seconds until the latch releases; fails past the ceiling."""
    start, writes = io.clock.now, len(positive_writes(io))
    await refresh(c, io, 1)                        # timer starts
    while c.phase == "faulted":
        assert not c.needs_human and not c.diagnostics["needs_human"]
        assert io.clock.now - start <= CEILING + 2 * step, "latched past the 2 h ceiling"
        await refresh(c, io, step)
    assert c.phase == "paused" and len(positive_writes(io)) == writes
    return io.clock.now - start


def assert_holds(measured, expected, step=10):
    for got, want in zip(measured, expected, strict=True):
        assert want + 1 <= got < want + 1 + step, (measured, expected)


async def test_back_to_back_fault_holds_double_to_the_ceiling():
    c, io = await make(saved(enabled=False), charging=False)
    holds = []
    for _ in range(7):
        await soft_fault(c, io)
        holds.append(await measure_hold(c, io))
    assert_holds(holds, [600, 1200, 2400, 4800, 7200, 7200, 7200])


async def test_back_to_back_comms_holds_double_to_the_ceiling():
    c, io = await comms_latched()
    holds = []
    for _ in range(8):
        holds.append(await measure_hold(c, io))
        await drop_link_during_enable(c, io)
    assert_holds(holds, [120, 240, 480, 960, 1920, 3840, 7200, 7200])


async def test_stop_failed_backs_off_from_the_fault_base():
    c, io = await make()
    holds = []
    for _ in range(2):
        io.zero_pauses, io.stop_works = False, False   # still drawing despite zero and Stop
        with pytest.raises(ControlError):
            await c.async_pause()
        await drain(c)
        assert c.latch_cause == "stop_failed" and not c.needs_human
        io.zero_pauses, io.stop_works = True, True
        holds.append(await measure_hold(c, io))
        assert (await c.async_enable()).outcome == "confirmed"
    assert_holds(holds, [FAULT_RELEASE_S, 2 * FAULT_RELEASE_S])


async def test_fourth_and_later_soft_faults_in_a_day_still_release():
    """Probe inverted: the 4th soft fault an hour apart used to stick ~21 h."""
    c, io = await make(saved(enabled=False), charging=False)
    for _ in range(8):
        await soft_fault(c, io)
        await measure_hold(c, io)
        io.clock.now += 3600
    assert c.phase == "paused" and not c.needs_human


@pytest.mark.parametrize("quiet, expected", [(CEILING - 60, 2 * FAULT_RELEASE_S),
                                             (CEILING, FAULT_RELEASE_S)])
async def test_streak_decays_after_two_quiet_hours(quiet, expected):
    c, io = await make(saved(enabled=False), charging=False)
    await soft_fault(c, io)
    await measure_hold(c, io)
    io.clock.now += quiet
    await soft_fault(c, io)
    assert_holds([await measure_hold(c, io)], [expected])


async def test_comms_streak_decays_after_two_quiet_hours():
    c, io = await comms_latched()
    for _ in range(4):
        await measure_hold(c, io)
        await drop_link_during_enable(c, io)
    await measure_hold(c, io)
    io.clock.now += CEILING
    await drop_link_during_enable(c, io)
    assert_holds([await measure_hold(c, io)], [COMMS_LATCH_RELEASE_S])


async def test_flapping_load_fault_keeps_recovering_within_the_backoff():
    """A soft fault that shows for 5 s after 20 min of charging, all day, with an
    automation enabling after every release: each latch releases by itself
    (never past 2 h, never sooner than the backoff), and charging resumes."""
    c, io = await make(saved(enabled=False))
    holds = []
    for _ in range(10):
        await soft_fault(c, io)
        io.clock.now += 5
        holds.append(await measure_hold(c, io, step=30))
        assert (await c.async_enable()).outcome == "confirmed" and io.regs[POW] == 70
        for _ in range(40):                        # 20 min charging
            await refresh(c, io, 30)
            await c.async_poll()
        assert c.phase == "enabled"
    assert_holds(holds, [600, 1200, 2400, 4800] + [7200] * 6, step=30)
    io.clock.now += CEILING                        # flapping stopped: 2 quiet hours
    await soft_fault(c, io)
    assert_holds([await measure_hold(c, io)], [FAULT_RELEASE_S])


@pytest.mark.parametrize("latch", ["fault", "comms", "stop_failed"])
async def test_needs_human_only_for_hard(latch):
    c, io = await make(saved(enabled=False))
    for _ in range(3):                             # a streak of releases first
        await soft_fault(c, io)
        await measure_hold(c, io)
    if latch == "fault":
        await soft_fault(c, io)
    elif latch == "comms":
        await drop_link_during_enable(c, io)
    else:
        assert (await c.async_enable()).outcome == "confirmed"
        io.zero_pauses, io.stop_works = False, False
        with pytest.raises(ControlError):
            await c.async_pause()
        await drain(c)
    assert c.latch_cause == latch
    for _ in range(3):                             # any amount of time and history
        assert not c.needs_human and not c.diagnostics["needs_human"]
        io.clock.now += 86400
    io.regs[FAULT_LO] = 1 << 6                     # leakage current: hard
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    assert c.latch_cause == "hard" and c.needs_human and c.diagnostics["needs_human"]


async def test_hard_latch_never_releases_after_a_streak():
    c, io = await make(saved(enabled=False), charging=False)
    for _ in range(3):
        await soft_fault(c, io)
        await measure_hold(c, io)
    io.regs[FAULT_LO] = 1                          # emergency stop
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    for _ in range(3):
        await healthy_for(c, io, CEILING, step=60)
        assert c.phase == "faulted" and c.needs_human
        io.clock.now += 86400
    assert positive_writes(io) == []
