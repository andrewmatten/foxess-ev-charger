"""Fault latches release themselves (paused) when the cause has cleared.

Incident 2026-10-30: a Modbus timeout during a write latched the charger,
and it stayed latched for ~21 h although the charger was healthy. 3.1.1
released a comms-only latch after COMMS_LATCH_RELEASE_S of healthy link.
Incident 2026-11-06 (twice): the link kept dropping single frames, each one
restarting that timer, so the latch held for hours. Now the latch records
its cause: comms (120 s, doubling per release in the last hour, max 15 min),
self-clearing faults and failed Stops (10 min; failed Stop never 18:00-21:00)
release; hard faults never do; at most MAX_AUTO_RELEASES per 24 h, then
needs_human. A release always leaves charging paused.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import (
    COMMS_HOLD_MAX_S, COMMS_LATCH_RELEASE_S, FAULT_RELEASE_S, MAX_AUTO_RELEASES,
    STOP_FALLBACK_CLEAR_S, ControlError,
)
from custom_components.foxess_charger.transport import TransportError

from test_controller import CTRL, POW, make, saved
from test_fault_and_stop_fallback import drain, positive_writes

FAULT_HI, FAULT_LO = 0x101A, 0x101B
NOON = {"local_hour": lambda: 12}


def cause(c):
    return c.export_state()["latch_cause"]


async def drop_link_during_enable(c, io):
    """The link drops during the positive write of an enable."""
    async def link_drops(value):
        if value > 0:
            io.fail_reads = True
            io.hooks.pop(POW)

    io.hooks[POW] = link_drops
    with pytest.raises(ControlError):
        await c.async_enable()
    assert c.phase == "faulted" and cause(c) == "comms"
    io.fail_reads = False            # link back, charger healthy
    io.writes.clear()


async def comms_latched(**kw):
    c, io = await make(saved(enabled=False), **kw)
    await drop_link_during_enable(c, io)
    assert c.intent_enabled          # latched mid-enable: the enable is still stored
    return c, io


async def refresh(c, io, seconds):
    io.clock.now += seconds
    await c._op_refresh()
    await drain(c)


async def healthy_for(c, io, seconds, step=30):
    await refresh(c, io, 1)          # timer starts
    for _ in range(int(seconds // step)):
        await refresh(c, io, step)


async def test_comms_latch_releases_paused_after_healthy_link():
    c, io = await comms_latched()
    await refresh(c, io, 1)                        # timer starts
    for _ in range(3):
        await refresh(c, io, 30)
    assert c.phase == "faulted"                    # 90 s: not yet
    await refresh(c, io, 30)                       # 120 s healthy
    assert c.phase == "paused" and not c.intent_enabled
    state = c.export_state()
    assert not state["safety_latched"] and state["latch_cause"] is None
    for _ in range(5):                             # nothing resumes by itself
        await c.async_poll()
        await refresh(c, io, 30)
    assert positive_writes(io) == [] and io.regs[POW] == 0
    assert (await c.async_enable()).outcome == "confirmed"   # normal enable works
    assert io.regs[POW] == 70


@pytest.mark.parametrize("code", [1, 1 << 5, (1 << 3) | (1 << 1), 1 << 9])
async def test_hard_fault_latch_never_releases(code):
    """Emergency stop, PE, overcurrent (even with a soft bit), connector."""
    c, io = await make()
    io.regs[FAULT_HI], io.regs[FAULT_LO] = code >> 16, code & 0xFFFF
    await c.async_poll()
    await drain(c)
    assert c.phase == "faulted" and cause(c) == "hard" and c.needs_human
    io.regs[FAULT_HI] = io.regs[FAULT_LO] = 0
    await healthy_for(c, io, 3600)
    assert c.phase == "faulted" and c.needs_human and positive_writes(io) == []


@pytest.mark.parametrize("status, code", [
    (6, 0),                      # fault status, no fault bits
    (None, 1 << 1),              # overvoltage
    (None, (1 << 2) | (1 << 8)), # undervoltage + CP
    (6, 1 << 16),                # ambient temperature (high word)
])
async def test_self_clearing_fault_releases_after_ten_minutes(status, code):
    c, io = await make(saved(enabled=False), charging=False)   # a fault stops output
    io.status_override = status
    io.regs[FAULT_HI], io.regs[FAULT_LO] = code >> 16, code & 0xFFFF
    await c.async_poll()
    await drain(c)
    assert c.phase == "faulted" and cause(c) == "fault" and not c.needs_human
    io.status_override = None
    io.regs[FAULT_HI] = io.regs[FAULT_LO] = 0
    await healthy_for(c, io, FAULT_RELEASE_S - 30)
    assert c.phase == "faulted"
    await refresh(c, io, 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == [] and io.regs[POW] == 0


async def test_soft_fault_still_present_keeps_latch():
    c, io = await make()
    io.regs[FAULT_LO] = 1 << 1                     # overvoltage stays
    await healthy_for(c, io, 2 * FAULT_RELEASE_S)
    assert c.phase == "faulted" and cause(c) == "fault"


async def test_hard_fault_during_comms_latch_makes_it_permanent():
    c, io = await comms_latched()
    io.regs[FAULT_LO] = 1 << 6                     # leakage current
    await c.async_poll()
    await drain(c)
    assert cause(c) == "hard"
    io.regs[FAULT_LO] = 0
    await healthy_for(c, io, 3600)
    assert c.phase == "faulted" and c.needs_human


async def test_failed_stop_releases_after_ten_minutes_outside_the_evening_window():
    hour = [19]
    c, io = await make(local_hour=lambda: hour[0])
    io.zero_pauses, io.stop_works = False, False
    with pytest.raises(ControlError, match="did not stop"):
        await c.async_pause()
    assert c.phase == "faulted" and cause(c) == "stop_failed"
    io.zero_pauses = True                          # charger healthy again
    await healthy_for(c, io, 3 * FAULT_RELEASE_S)
    assert c.phase == "faulted"                    # 18:00-21:00: never
    hour[0] = 21
    await refresh(c, io, 30)
    assert c.phase == "paused" and positive_writes(io) == []


async def test_failed_stop_with_charger_still_drawing_stays_latched():
    c, io = await make(**NOON)
    io.ignore.add(POW)
    io.ignore.add(CTRL)                            # ignores both zero and Stop
    with pytest.raises(ControlError):
        await c.async_pause()
    await drain(c)
    assert c.phase == "faulted" and cause(c) == "stop_failed"
    await healthy_for(c, io, 2 * FAULT_RELEASE_S)
    assert c.phase == "faulted"                    # drawing keeps resetting it


async def test_comms_latch_with_charger_ignoring_zero_and_stop_becomes_stop_failed():
    c, io = await comms_latched(**NOON)
    io.zero_pauses, io.stop_works = False, False   # still drawing despite zero and Stop
    await c.async_poll()                           # correction -> Stop fallback fails
    await drain(c)
    assert cause(c) == "stop_failed"
    io.zero_pauses = True
    for _ in range(10):
        await refresh(c, io, 30)
    assert c.phase == "faulted"                    # 300 s: not the 120 s comms hold
    await healthy_for(c, io, FAULT_RELEASE_S)
    assert c.phase == "paused" and positive_writes(io) == []


async def test_single_invalid_frames_do_not_restart_release_timer():
    """2026-11-06: one lost frame every minute or so kept the latch for hours."""
    c, io = await comms_latched()
    await refresh(c, io, 1)                        # timer starts at T
    for _ in range(4):
        io.clock.now += 10
        io.fail_reads = True                       # one unreadable poll frame
        await c.async_poll()
        io.fail_reads = False
        await refresh(c, io, 20)
    assert c.phase == "paused"                     # T+120 despite four lost frames


async def test_drawing_or_sustained_invalid_frames_reset_release_timer():
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
    io.fail_reads = True                           # ~25 s of invalid frames
    for _ in range(6):
        io.clock.now += 5
        await c.async_poll()
    io.fail_reads = False
    await drain(c)
    await refresh(c, io, 1)                        # telemetry protect still on
    await refresh(c, io, 1)                        # agreeing frame lifts it
    await refresh(c, io, 100)                      # 120 s since the restart
    assert c.phase == "faulted"                    # restarted by the outage
    await refresh(c, io, COMMS_LATCH_RELEASE_S)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == []


async def test_comms_hold_doubles_per_release_within_an_hour_and_caps():
    c, io = await comms_latched()
    holds = []
    for n in range(MAX_AUTO_RELEASES):
        start = io.clock.now
        await refresh(c, io, 1)
        while c.phase == "faulted":
            await refresh(c, io, 10)
        holds.append(io.clock.now - start)
        await drop_link_during_enable(c, io)
    assert holds[0] < 2 * COMMS_LATCH_RELEASE_S
    assert holds[1] >= 2 * COMMS_LATCH_RELEASE_S and holds[2] >= 4 * COMMS_LATCH_RELEASE_S
    # cap reached: the next latch stays, and a human is needed
    assert c.needs_human and c.diagnostics["needs_human"]
    await healthy_for(c, io, 2 * COMMS_HOLD_MAX_S)
    assert c.phase == "faulted" and c.needs_human
    io.clock.now += 86400                          # a day later the cap has lapsed
    await healthy_for(c, io, COMMS_LATCH_RELEASE_S + 30)
    assert c.phase == "paused" and not c.needs_human


@pytest.mark.parametrize("extra", [{}, {"comms_latch": "yes"}, {"latch_cause": "bogus"}])
async def test_restored_latch_with_unknown_cause_stays_latched(extra):
    """A legacy latch may have been earth leakage / contactor / emergency stop:
    unclassified means manual only (Codex review 2026-11-07)."""
    c, io = await make(saved(enabled=False, safety_latched=True, **extra), **NOON)
    assert cause(c) == "hard"
    await healthy_for(c, io, 3600)
    assert c.phase == "faulted" and c.needs_human


async def test_restored_unknown_cause_with_hard_fault_present_stays_latched():
    c, io = await make(saved(enabled=False, safety_latched=True), **NOON)
    io.regs[FAULT_LO] = 1                          # emergency stop
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    await healthy_for(c, io, 3600)
    assert c.phase == "faulted" and cause(c) == "hard"


@pytest.mark.parametrize("extra", [{"comms_latch": True}, {"latch_cause": "comms"}])
async def test_restored_comms_latch_releases_paused(extra):
    c, io = await make(saved(enabled=True, safety_latched=True, **extra))
    assert c.phase == "faulted" and cause(c) == "comms"
    for _ in range(6):
        await refresh(c, io, 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert positive_writes(io) == [] and io.regs[POW] == 0


async def test_latch_cause_round_trips_through_saved_state():
    c, io = await make()
    io.regs[FAULT_LO] = 1
    await c.async_poll()
    await drain(c)
    c2, _ = await make(c.export_state())
    assert c2.latch_cause == "hard" and c2.needs_human


async def test_link_outage_during_pause_latches_releasably():
    """Incident 2026-11-06: the zero write was confirmed, then the link died
    (status reads time out), so the pause was never observed and the Stop
    fallback timed out. Never having heard the charger is a link outage, not
    a failed stop: the latch must release once the link is healthy."""
    c, io = await make()                           # charging at 7.0 kW
    real = io.read
    down = True

    async def read(address, count):
        if down and address != POW:                # setpoint read-back works, status does not
            raise TransportError("status block timed out")
        return await real(address, count)

    io.read = read
    with pytest.raises(ControlError):
        await c.async_pause()
    await drain(c)
    assert c.phase == "faulted" and cause(c) == "comms"
    down = False
    io.writes.clear()
    await refresh(c, io, 1)
    for _ in range(5):
        await refresh(c, io, 30)
    assert c.phase == "paused" and not c.intent_enabled


async def test_quiet_stop_fallback_becomes_a_refreshed_pause():
    c, io = await make()
    io.zero_pauses = False                         # zero ignored, Stop works
    await c.async_pause()
    assert c.phase == "stopped"
    io.writes.clear()
    await healthy_for(c, io, STOP_FALLBACK_CLEAR_S - 30)
    assert c.phase == "stopped" and io.writes == []    # stopped: no cap writes
    await refresh(c, io, 30)
    assert c.phase == "paused" and not c.export_state()["stop_fallback"]
    await refresh(c, io, 30)
    assert io.power_writes()[-1:] == [0] and positive_writes(io) == []
