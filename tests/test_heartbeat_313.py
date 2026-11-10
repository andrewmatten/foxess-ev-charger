"""3.1.3 heartbeat: a power-cap refresh that never reached the charger must not
count as delivered, or the 60 s firmware validity lapses while the controller
still reports "enabled" (reviewer probe: writes at +29 s and +59 s lost, peak
7.3 kW against a 2.5 kW cap)."""
from __future__ import annotations

import pytest

from rebuild_simulator import CHARGING
from test_controller import saved
from test_rebuild_contract import P, rigs  # noqa: F401


@pytest.mark.parametrize("mode", ["transport_error", "lost_request", "lost_reply", "refuse"])
@pytest.mark.parametrize("lost", [1, 2])
async def test_unacknowledged_heartbeat_is_retried_not_counted_delivered(rigs, mode, lost):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(25)                       # 2.5 kW cap
    assert rig.sim.state == CHARGING
    await rig.clock.advance(31)                   # let one normal refresh land
    mark, t0 = rig.sim.mark(), rig.clock()
    failures = ctl.diagnostics["refresh_failures"]
    rig.sim.inject_write(P, mode, count=lost)     # the next N cap writes get no ack
    phases = set()
    for _ in range(12):                           # 120 s, HA polling every 10 s
        await rig.run(ctl.async_poll())
        phases.add(ctl.phase)
        await rig.clock.advance(10)
    writes = rig.sim.writes_since(mark, P)
    assert [w.mode for w in writes[:lost + 1]] == [mode] * lost + ["ok"]
    # Each unacknowledged write is retried at the 3 s retry, not a full interval later.
    gaps = [b.t - a.t for a, b in zip(writes, writes[1:lost + 1])]
    assert gaps == [pytest.approx(3.0, abs=0.5)] * lost
    assert ctl.diagnostics["refresh_failures"] - failures >= lost
    assert rig.sim.max_measured_power(t0, rig.clock()) <= 25
    assert phases == {"enabled"} and not ctl.diagnostics["safety_latched"]


async def test_unacknowledged_heartbeats_do_not_latch_a_lossy_link_sooner(rigs):
    """Mismatch tracking stays read-back only: 30 s of unacknowledged cap writes
    whose read-back still shows the cap neither latches nor pauses."""
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(25)
    await rig.clock.advance(31)
    rig.sim.inject_write(P, "lost_reply", count=None)
    await rig.clock.advance(30)
    rig.sim.clear_write_rules()
    await rig.clock.advance(60)
    assert ctl.phase == "enabled" and not ctl.diagnostics["safety_latched"]
    assert rig.sim.state == CHARGING


@pytest.mark.parametrize("validity, interval", [(60, 20.0), (180, 20.0), (30, 10.0), (None, 3.0)])
async def test_refresh_interval_is_a_third_of_the_effective_validity(validity, interval):
    """About 13 attempts (20 s + 3 s retries) fit before the 60 s expiry, not two."""
    from test_controller import make

    c, _ = await make()
    c._validity = validity
    assert c._refresh_interval() == interval
    assert c.diagnostics["refresh_interval"] == interval


async def _drive(c, io, seconds, lose_refresh_status):
    """Poll every 10 s and run the refresh on its real schedule, one simulated
    second at a time; the refresh's own status read is lost while
    ``lose_refresh_status()``. Returns, per second, the phase and the seconds
    since a positive cap write last reached the charger (None while the cap
    register holds zero: nothing can lapse)."""
    from test_controller import POW, TransportError

    real_read, real_write = io.read, io.write
    in_refresh, last_cap = [False], [io.clock.now]

    async def read(address, count):
        if in_refresh[0] and address == 0x1000:
            in_refresh[0] = False
            raise TransportError("lost")
        return await real_read(address, count)

    async def write(address, value):
        await real_write(address, value)
        if address == POW and value > 0:
            last_cap[0] = io.clock.now

    io.read, io.write = read, write
    out, due, start = [], io.clock.now, io.clock.now
    while io.clock.now < start + seconds:
        now = io.clock.now
        if int(now - start) % 10 == 0:
            await c.async_poll()
        if now >= due:
            in_refresh[0] = lose_refresh_status()
            ok = await c._op_refresh()
            in_refresh[0] = False
            due = now + (c._refresh_interval() if ok else c._retry)
        out.append((c.phase, io.clock.now - last_cap[0] if io.regs[POW] else None))
        io.clock.now = now + 1
    return out


async def test_telemetry_hold_cannot_outlast_the_cap_validity():
    """S1: every refresh status read is lost while the 10 s polls stay valid, so
    the telemetry grace never expires and the refresh holds without writing.
    The cap must not lapse with the phase still "enabled": after
    CAP_ACK_LIMIT_S without an acknowledged cap write the controller goes
    protective (zero, "uncertain"), never latched."""
    from test_controller import make

    c, io = await make(retry_interval=3.0)
    trace = await _drive(c, io, 150, lambda: True)
    exposed = [t for t, (ph, age) in enumerate(trace) if ph == "enabled" and (age or 0) > 60]
    assert not exposed, f"enabled with a lapsed cap at {exposed[:3]}..."
    assert "uncertain" in {ph for ph, _ in trace}
    assert 0 in io.power_writes() and not c.diagnostics["safety_latched"]


async def test_protective_cap_timeout_recovers_when_the_link_heals():
    from test_controller import make

    c, io = await make(retry_interval=3.0)
    lose = [True]
    await _drive(c, io, 60, lambda: lose[0])
    lose[0] = False
    trace = await _drive(c, io, 60, lambda: lose[0])
    assert trace[-1][0] == "enabled" and trace[-1][1] is not None and trace[-1][1] <= 20
    assert not c.diagnostics["safety_latched"] and c.intent_enabled


async def test_idle_positive_intent_without_a_session_does_not_go_protective():
    """An enable staged on an unplugged cable wires zero (nothing to protect):
    no acknowledged positive cap ever, yet no protective flapping."""
    from test_controller import CC, make

    c, io = await make(saved(enabled=False), charging=False)
    io.regs[CC] = 0
    assert (await c.async_enable()).outcome == "staged"
    trace = await _drive(c, io, 150, lambda: False)
    assert {ph for ph, _ in trace} == {"enabled"}
