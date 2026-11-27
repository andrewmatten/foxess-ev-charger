"""3.2 R4-5: telemetry recovery hold must not bypass CAP_ACK_LIMIT_S past firmware cap expiry."""
from __future__ import annotations

from custom_components.foxess_charger.const import REG_STATUS
from custom_components.foxess_charger.controller import CAP_ACK_LIMIT_S
from custom_components.foxess_charger.transport import TransportError

from test_controller import POW, make, saved
from test_rebuild_contract import CAP, P, rigs  # noqa: F401 - rigs is a fixture


async def test_telemetry_recovery_hold_refused_past_cap_ack_limit_prevents_expiry(rigs):
    """After a 1.4 kW cap is acked at t=0 and a link drop from t=19..46 sets
    _REASON_TELEMETRY while zero writes fail, flapping valid frames starting at
    t=47 must NOT hold zero writes past CAP_ACK_LIMIT_S (45 s) and let the
    charger revert to rated maximum at t=60 s."""
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    t0 = rig.clock()

    # Advance to +19 s (before the 20 s refresh), then drop the link for 27 s
    # (until +46 s) so _REASON_TELEMETRY sets at +41 s while 0x3002=0 fails.
    await rig.clock.advance(19)
    rig.sim.connectivity_burst(27)
    await rig.clock.advance(27)
    assert "telemetry_uncertain" in ctl.diagnostics["protective_reasons"]

    # Link flaps back at +46 s with alternating active status 3 <-> 4 so
    # _REASON_TELEMETRY never lifts; advance past the 60 s firmware expiry.
    real_read, rig.sim.status_override = rig.sim.read_sync, 3

    def flipping_read(address, count):
        values = real_read(address, count)
        if address <= REG_STATUS < address + count:
            rig.sim.status_override = 7 - rig.sim.status_override
        return values

    rig.sim.read_sync = flipping_read
    mark = rig.sim.mark()
    await rig.clock.advance(24)  # to t0 + 70 s, past the 60 s expiry

    assert (P, 0) in rig.sim.wire_writes(mark)
    assert rig.sim.max_power_limit(t0, rig.clock()) <= CAP
    assert rig.sim.max_measured_power(t0, rig.clock()) <= CAP


async def test_active_telemetry_hold_ends_immediately_at_cap_ack_limit():
    """A recovery hold begun before CAP_ACK_LIMIT_S must still end as soon as
    CAP_ACK_LIMIT_S elapses since _cap_at, rather than waiting the full 20 s
    telemetry grace."""
    c, io = await make(saved(power=14))
    t0 = io.clock.now  # _cap_at == t0 from initial refresh
    io.zero_pauses = False
    real_write = io.write

    async def write(address, value):
        if io.fail_reads:
            raise TransportError("link down")
        await real_write(address, value)

    io.write = write

    # Outage t+20..t+41 sets _REASON_TELEMETRY while the zero write fails.
    io.fail_reads = True
    io.clock.now = t0 + 20
    await c._op_refresh()
    io.clock.now = t0 + 41
    await c._op_refresh()
    assert "telemetry_uncertain" in c.diagnostics["protective_reasons"]
    assert io.power_writes() == []

    # At t+43 (< 45 s): one valid frame enters the hold (no zero yet).
    io.fail_reads = False
    io.clock.now = t0 + 43
    await c._op_refresh()
    assert io.power_writes() == []

    # Dropped poll resets _good_first so _REASON_TELEMETRY stays active.
    io.fail_reads = True
    io.clock.now = t0 + 44
    await c.async_poll()

    # At t+46 (>= CAP_ACK_LIMIT_S, only 3 s into the 20 s hold): hold must be
    # refused and protective zero written immediately.
    io.fail_reads = False
    io.clock.now = t0 + CAP_ACK_LIMIT_S + 1
    await c._op_refresh()
    assert io.power_writes() == [0]
    assert io.regs[POW] == 0
