"""3.1.3 heartbeat: a power-cap refresh that never reached the charger must not
count as delivered, or the 60 s firmware validity lapses while the controller
still reports "enabled" (reviewer probe: writes at +29 s and +59 s lost, peak
7.3 kW against a 2.5 kW cap)."""
from __future__ import annotations

import pytest

from rebuild_simulator import CHARGING
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
