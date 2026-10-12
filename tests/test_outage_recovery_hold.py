"""Outage recovery hold: a link outage longer than the telemetry grace must
not end a session that is still charging when the link comes back.

The outage sets the telemetry protect, which lifts only after two agreeing
valid frames. When the refresh loop reads the first good frame, a leftover
protective zero must not be written if that frame shows the session
charging; firmware 1.8 would finish the session (status 5) and the finish
would revoke the enable. The first test covers that; the second guards the
boundary: bad frames still get the protective zero. The last two bound the
hold: it never leaves the wire without a confirmed write for longer than the
grace plus one retry.
"""
from __future__ import annotations

from custom_components.foxess_charger.const import REG_STATUS, REG_STATUS_BLOCK_START
from custom_components.foxess_charger.transport import TransportError

from rebuild_simulator import CHARGING
from test_controller import POW, STATUS, make
from test_fault_and_stop_fallback import drain, positive_writes
from test_rebuild_contract import FULL, P, rigs  # noqa: F401 - rigs is a fixture


async def test_session_still_charging_when_link_returns_keeps_running(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    assert rig.sim.state == CHARGING and ctl.intent_enabled
    mark = rig.sim.mark()

    rig.sim.connectivity_burst(25)           # link down 25 s, longer than the 20 s grace
    await rig.run(ctl.async_poll())          # HA polls every 10 s: +0, +10, +20
    for _ in range(2):
        await rig.clock.advance(10)
        await rig.run(ctl.async_poll())
    assert ctl.phase == "uncertain"          # protective zero (not delivered: link down)
    assert rig.sim.state == CHARGING

    # Link back at +25. No poll until +36, so the refresh loop (due +30) is
    # the first to read a good frame, which still shows the car charging.
    await rig.clock.advance(16)
    assert any(
        e.kind == "read" and e.address == REG_STATUS_BLOCK_START and e.outcome == "ok"
        for e in rig.sim.log if e.seq >= mark
    ), "the refresh loop must observe the recovery before any poll does"

    for _ in range(6):                       # then polls every 10 s for a minute
        await rig.run(ctl.async_poll())
        await rig.clock.advance(10)

    assert (P, 0) not in rig.sim.wire_writes(mark)
    assert ctl.intent_enabled
    assert rig.sim.state == CHARGING
    assert ctl.phase == "enabled" and rig.sim.holding[P] == FULL


async def test_isolated_good_frame_inside_outage_bad_frames_still_get_zero():
    """While the outage continues, every refresh that reads a bad frame
    still writes the protective zero, also right after an isolated good
    active-session frame (which alone is not recovery)."""
    c, io = await make()                     # enabled at 7.0 kW, charging
    io.zero_pauses = False                   # the car keeps charging through the zeros
    io.fail_reads = True
    await c.async_poll()
    io.clock.now += 25
    await c.async_poll()                     # past the grace: protective zero
    await drain(c)
    assert c.phase == "uncertain" and io.power_writes() == [0]

    for good in (True, False, True, False):
        io.fail_reads = not good
        io.clock.now += 3                    # refresh-loop retry interval
        before = len(io.power_writes())
        await c._op_refresh()
        if good:
            assert io.regs[STATUS] == 3      # the frame showed the session charging
        else:
            assert io.power_writes()[before:] == [0]

    assert c.phase == "uncertain" and c.intent_enabled
    assert positive_writes(io) == []


async def test_hold_is_bounded_when_active_frames_never_agree(rigs):
    """Good frames after the outage all show an active session but
    alternate status 3/4, so the telemetry protect never lifts. Each hold
    must end with a write within the grace plus one retry (~25 s): the
    protective zero then goes out as before the fix."""
    rig = rigs(reported_validity=180, effective_validity=60, zero_pause_works=False)
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    rig.sim.connectivity_burst(25)
    await rig.run(ctl.async_poll())          # polls +0, +10, +20 set the protect
    for _ in range(2):
        await rig.clock.advance(10)
        await rig.run(ctl.async_poll())
    assert ctl.phase == "uncertain"
    mark = rig.sim.mark()

    # Every status-block read that succeeds flips the reported status 3 <-> 4.
    real_read, rig.sim.status_override = rig.sim.read_sync, 3

    def flipping_read(address, count):
        values = real_read(address, count)
        if address <= REG_STATUS < address + count:
            rig.sim.status_override = 7 - rig.sim.status_override
        return values

    rig.sim.read_sync = flipping_read
    await rig.clock.advance(125)             # link back at +25; refresh loop only, no polls

    # Only the refresh loop reads the status block from here on.
    events = [
        (e.t, e.kind) for e in rig.sim.log
        if e.seq >= mark and e.outcome == "ok" and (
            (e.kind == "read" and e.address == REG_STATUS_BLOCK_START)
            or (e.kind == "write" and e.address == P))
    ]
    holds, start = [], None                  # first frame read -> next write
    for t, kind in events:
        if kind == "read":
            start = t if start is None else start
        elif start is not None:
            holds.append(t - start)
            start = None
    if start is not None:
        holds.append(rig.clock() - start)

    assert rig.sim.positive_cap_writes(mark) == []   # the protect never lifted
    first_frame = next(t for t, kind in events if kind == "read")
    first_write = next((t for t, kind in events if kind == "write"), None)
    assert first_write is not None and first_write - first_frame <= 25
    assert (P, 0) in rig.sim.wire_writes(mark)
    assert max(holds) <= 25, holds
    assert ctl.phase == "uncertain" and ctl.intent_enabled


async def test_hold_bound_not_restarted_by_a_zero_lost_on_the_link():
    """Flaky link: on bad frames the write fails too, good frames show an
    active session. Only a confirmed write ends a hold, so a lost zero
    cannot restart it: a protective zero still lands within the grace."""
    c, io = await make()                     # enabled at 7.0 kW, charging
    io.zero_pauses = False                   # frames stay active
    real_write = io.write

    async def write(address, value):
        if io.fail_reads:                    # link down: the write is lost too
            raise TransportError("link down")
        await real_write(address, value)

    io.write = write
    io.fail_reads = True
    await c.async_poll()
    io.clock.now += 25
    await c.async_poll()                     # past the grace: protect set
    await drain(c)
    assert c.phase == "uncertain"

    t0, landed = io.clock.now, []
    for i in range(10):                      # good, bad, good, ... every 3 s
        io.fail_reads = bool(i % 2)
        io.clock.now += 3
        n = len(io.writes)
        await c._op_refresh()
        landed += [io.clock.now - t0 for _, a, v in io.writes[n:] if a == POW and v == 0]
    # First hold at +3; good frames every 6 s, so the bound acts by +29.
    assert landed and landed[0] <= 3 + 20 + 6, landed
