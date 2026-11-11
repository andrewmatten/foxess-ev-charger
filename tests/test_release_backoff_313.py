"""Fault-latch auto-release backoff (3.1.3).

Replaces the 3-per-24 h cap, which left a 4th soft fault on a hot day
latched for about 21 h. Every releasable cause (comms, fault, stop_failed)
releases after a healthy hold of base * 2**n, at most 2 h; n counts the
releases of the current streak, which ends after 2 h without a latch. Only a
hard latch needs a human.
"""
from __future__ import annotations

import time

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


# ── release history survives a restart (S4) ──────────────────────────────
async def restarted_hold(state, **kw):
    """Hold of a soft fault on a controller restored from ``state``."""
    c, io = await make(state, charging=False, **kw)
    await soft_fault(c, io)
    return await measure_hold(c, io)


async def released_state():
    """The state saved by the write that released a first soft-fault latch."""
    states = []

    async def persist(state):
        states.append(dict(state))

    c, io = await make(saved(enabled=False), charging=False, persist=persist)
    await soft_fault(c, io)
    await measure_hold(c, io)
    assert not states[-1]["safety_latched"]
    return states[-1]


async def test_release_history_survives_a_restart():
    assert_holds([await restarted_hold(await released_state())], [2 * FAULT_RELEASE_S])


@pytest.mark.parametrize("down, expected", [(3600, 2 * FAULT_RELEASE_S),
                                            (CEILING + 1, FAULT_RELEASE_S)])  # saved times are rounded
async def test_release_history_ages_by_wall_clock_across_a_restart(down, expected):
    state = await released_state()
    later = time.time() + down
    assert_holds([await restarted_hold(state, wall_clock=lambda: later)], [expected])


@pytest.mark.parametrize("bad", [
    "x", 5, None, {"t": 1.0}, [None], ["1"], [True], [float("nan")], [float("inf")],
    [[1.0]], [{"t": 1.0}],
])
async def test_malformed_release_history_is_ignored(bad):
    hold = await restarted_hold(saved(enabled=False, latch_releases=bad))
    assert_holds([hold], [FAULT_RELEASE_S])
    if isinstance(bad, list):                      # good entries beside bad ones still count
        state = saved(enabled=False, latch_releases=[*bad, time.time() - 100])
        assert_holds([await restarted_hold(state)], [2 * FAULT_RELEASE_S])


async def test_stale_and_future_releases_are_dropped():
    now = time.time()
    stale_and_future = [now - CEILING, now - 86400, now + 600, now + 86400]
    hold = await restarted_hold(saved(enabled=False, latch_releases=stale_and_future))
    assert_holds([hold], [FAULT_RELEASE_S])
    state = saved(enabled=False, latch_releases=[*stale_and_future, now - 60])
    assert_holds([await restarted_hold(state)], [2 * FAULT_RELEASE_S])


@pytest.mark.parametrize("wall_step", [0.0, -3 * 3600.0, 3 * 3600.0])
async def test_restart_mid_hold_keeps_the_backoff_level(wall_step):
    """A latch restored by a restart keeps the releases of the streak it
    continues, even those 2 h old or more and with the wall clock stepped."""
    ref = {}                                       # wall clock follows the fake clock
    c, io = await make(saved(enabled=False), charging=False,
                       wall_clock=lambda: 1e9 + (ref["io"].clock.now if ref else 1000))
    ref["io"] = io
    for _ in range(4):
        await soft_fault(c, io)
        await measure_hold(c, io, step=30)
    io.clock.now += 7000                           # quiet, but < 2 h: streak continues
    await soft_fault(c, io)
    before = c._release_hold("fault")
    assert before == CEILING
    io.clock.now += 400                            # HA restarts 400 s into the hold
    state, clock = c.export_state(), io.clock
    c2, _ = await make(state, charging=False, wall_clock=lambda: 1e9 + clock.now + wall_step)
    assert c2.latch_cause == "fault"
    assert c2._release_hold("fault") == before


async def test_huge_release_history_stays_bounded():
    state = saved(enabled=False, latch_releases=[time.time() - 1] * 100_000)
    assert_holds([await restarted_hold(state)], [CEILING])


async def test_state_saved_by_3_1_2_loads_with_no_history():
    assert_holds([await restarted_hold(saved(enabled=False))], [FAULT_RELEASE_S])


# ── saved latch cause: corrupt values and rollback to 3.1.1/3.1.2 ─────────
@pytest.mark.parametrize("bad", [["comms"], {"cause": "comms"}, 1, True])
@pytest.mark.parametrize("comms_latch, expected", [(True, "comms"), (False, "hard")])
async def test_corrupt_saved_latch_cause_loads_as_unclassified(bad, comms_latch, expected):
    c, io = await make(saved(enabled=False, safety_latched=True, latch_cause=bad,
                             comms_latch=comms_latch))
    assert c.phase == "faulted" and c.latch_cause == expected



@pytest.mark.parametrize("latch", ["comms", "fault", "hard"])
async def test_saved_state_keeps_comms_latch_for_a_rollback(latch):
    """3.1.1/3.1.2 read only comms_latch: without it a comms latch would come
    back manual-only after a rollback."""
    c, io = await make()
    if latch == "comms":
        await drop_link_during_enable(c, io)
    else:
        io.regs[FAULT_LO] = 1 << 1 if latch == "fault" else 1
        await c.async_poll()
        await drain(c)
    state = c.export_state()
    assert state["latch_cause"] == latch and state["comms_latch"] is (latch == "comms")
    del state["latch_cause"]                       # what 3.1.2 sees
    c2, _ = await make(state)
    assert c2.latch_cause == ("comms" if latch == "comms" else "hard")


async def test_comms_latch_false_when_not_latched():
    c, io = await make()
    assert c.export_state()["comms_latch"] is False


# ── diagnostics: why am I not charging ───────────────────────────────────
async def test_diagnostics_show_seconds_until_auto_release():
    c, io = await comms_latched()
    d = c.diagnostics
    assert d["latch_cause"] == "comms" and d["latch_release_in"] is None   # timer not running
    await refresh(c, io, 1)                        # timer starts
    assert c.diagnostics["latch_release_in"] == COMMS_LATCH_RELEASE_S
    await refresh(c, io, 30)
    assert c.diagnostics["latch_release_in"] == COMMS_LATCH_RELEASE_S - 30
    await measure_hold(c, io)
    assert c.diagnostics["latch_release_in"] is None                     # not latched
    await drop_link_during_enable(c, io)
    await refresh(c, io, 1)
    assert c.diagnostics["latch_release_in"] == 2 * COMMS_LATCH_RELEASE_S  # backed off
    io.regs[FAULT_LO] = 1                          # emergency stop: hard
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    await refresh(c, io, 30)
    await refresh(c, io, 30)
    assert c.diagnostics["latch_cause"] == "hard" and c.diagnostics["latch_release_in"] is None
