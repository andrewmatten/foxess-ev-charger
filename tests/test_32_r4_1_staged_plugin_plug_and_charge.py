"""3.2 R4-1: staged unplugged enable applies staged caps immediately on Plug&Charge plug-in."""
from __future__ import annotations

from rebuild_simulator import CHARGING
from test_rebuild_contract import CAP, FULL, C, P, rigs, settle  # noqa: F401 - rigs is a fixture

PLUG_AND_CHARGE = 1


async def test_staged_unplugged_enable_applies_caps_immediately_on_plugin_in_plug_and_charge(rigs):
    """After an unplugged staged enable sits past 0x3001 expiry and refresh zeroes
    0x3002, plugging in under Plug&Charge (immediate status 3) must spawn
    _op_apply(enabling=True) on the first poll instead of waiting for a refresh."""
    rig = rigs(work_mode=PLUG_AND_CHARGE)
    ctl = await rig.boot()
    rig.sim.unplug()
    await rig.run(ctl.async_set_power(CAP))
    await rig.run(ctl.async_set_current(60))
    assert (await rig.run(ctl.async_enable())).outcome == "staged"
    assert ctl.intent_enabled and ctl._await_plug

    # Wait past the 60 s firmware expiry: refresh writes 0x3002=0 while 0x3001
    # reverts to the charger's 32 A maximum. Next refresh is still 10 s away.
    await rig.clock.advance(70)
    assert rig.sim.current_limit_raw() == rig.sim.max_current_raw
    assert rig.sim.power_limit_raw() == 0

    # Plug in under Plug&Charge: charger enters CHARGING immediately at max power
    # if its new session clears the unplugged zero cap.
    rig.sim.plug()
    rig.sim.holding[P] = rig.sim.max_power_raw
    assert rig.sim.state == CHARGING and rig.sim.measured_power_raw() == rig.sim.max_power_raw

    # A single poll (0 s elapsed, well before the next refresh) must apply both
    # staged limits immediately via _op_apply(enabling=True).
    await rig.run(ctl.async_poll())
    await settle()

    assert ctl.intent_enabled and not ctl._await_plug
    assert ctl._commands == 0
    assert rig.sim.power_limit_raw() == CAP
    assert rig.sim.current_limit_raw() == 60
    assert rig.sim.holding[P] == CAP
    assert rig.sim.holding[C] == 60
    assert rig.sim.measured_power_raw() <= CAP


async def test_staged_unplugged_lowered_cap_applied_on_plugin_without_waiting_for_refresh(rigs):
    """Lowering power while an unplugged enable is staged leaves the higher limit
    on the wire until plug-in; Plug&Charge plug-in must apply the lower limit on
    the first poll rather than drawing at the higher cap until the next refresh."""
    rig = rigs(work_mode=PLUG_AND_CHARGE)
    ctl = await rig.boot()
    rig.sim.unplug()
    await rig.run(ctl.async_set_power(FULL))
    assert (await rig.run(ctl.async_enable())).outcome == "staged"
    assert rig.sim.holding[P] == FULL

    # Before the 20 s refresh runs, lower the staged power cap to 1.4 kW.
    await rig.clock.advance(5)
    assert (await rig.run(ctl.async_set_power(CAP))).outcome == "staged"
    assert rig.sim.holding[P] == FULL

    rig.sim.plug()
    assert rig.sim.state == CHARGING and rig.sim.measured_power_raw() == FULL

    await rig.run(ctl.async_poll())
    await settle()

    assert ctl.intent_enabled and not ctl._await_plug
    assert ctl._commands == 0
    assert rig.sim.power_limit_raw() == CAP
    assert rig.sim.measured_power_raw() == CAP
