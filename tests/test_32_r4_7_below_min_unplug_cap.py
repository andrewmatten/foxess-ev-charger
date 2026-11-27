"""3.2 R4-7: _track_unplug must not release < 1.4 kW power caps on unplug/replug."""
from __future__ import annotations

from test_controller import CC, POW, make, saved
from test_fault_and_stop_fallback import drain
from test_rebuild_contract import CAP, rigs  # noqa: F401 - rigs is a fixture

PLUG_AND_CHARGE = 1


async def test_below_minimum_cap_not_released_on_unplug_in_plug_and_charge(rigs):
    """When Max Charging Power is below 1.4 kW, unplugging must keep 0x3002=0
    refreshed rather than releasing the pause and letting Plug&Charge start
    uncapped on replug."""
    rig = rigs(work_mode=PLUG_AND_CHARGE)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert (await rig.run(ctl.async_set_power(10))).outcome == "confirmed"
    assert ctl.phase == "paused_below_minimum"

    rig.sim.unplug()
    await rig.run(ctl.async_poll())
    await rig.clock.advance(2)
    await rig.run(ctl.async_poll())
    await rig.clock.advance(90)

    assert not ctl._released
    assert rig.sim.power_limit_raw() == 0

    rig.sim.plug()
    assert rig.sim.power_limit_raw() == 0
    assert rig.sim.measured_power_raw() == 0


async def test_below_minimum_replug_at_connected_status_1_rearms_before_charging():
    """If a pause was already released while unplugged and the user then lowers
    power below 1.4 kW, the first poll (even at cc=1, status=1 before status 3)
    must immediately re-arm zero."""
    c, io = await make(saved(enabled=False, power=50), charging=False)
    io.regs[0x3000] = PLUG_AND_CHARGE
    io.regs[CC] = 0
    await c.async_poll()
    io.clock.now += 1.0
    await c.async_poll()
    await drain(c)
    assert c._released is True

    # Simulate 0x3002 reverted to rated max while released; user lowers to 1.0 kW.
    io.regs[POW] = 73
    assert (await c.async_set_power(10)).outcome == "staged"

    # Replug seen first at status=1 (connected, before Plug&Charge enters status 3).
    io.regs[CC] = 1
    io.car_draw = False
    io.stopped = False
    io.writes.clear()
    await c.async_poll()
    await drain(c)

    assert not c._released
    assert io.power_writes() == [0]
    assert io.regs[POW] == 0


async def test_paused_below_min_with_stop_fallback_clears_fallback_without_releasing():
    """Unplugging with stop_fallback and a < 1.4 kW cap still clears
    stop_fallback to a plain pause, while keeping _released=False and asserting 0."""
    c, io = await make(saved(enabled=False, power=0, stop_fallback=True), charging=False)
    io.regs[0x3000] = PLUG_AND_CHARGE
    io.regs[CC] = 0
    io.regs[POW] = 73

    await c.async_poll()
    io.clock.now += 1.0
    await c.async_poll()
    await drain(c)

    assert c.export_state()["stop_fallback"] is False
    assert not c._released
    assert io.regs[POW] == 0
