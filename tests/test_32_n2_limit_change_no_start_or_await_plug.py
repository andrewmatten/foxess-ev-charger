"""3.2 N2: a non-enabling limit change must never send Start or arm _await_plug."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_MAX_CHARGING_POWER,
)
from custom_components.foxess_charger.controller import ControlError

from rebuild_simulator import CHARGING, CONNECTED, FINISHED
from test_rebuild_contract import CAP, rigs  # noqa: F401 - fixture

P, CTRL = REG_MAX_CHARGING_POWER, REG_CHARGING_CONTROL
CONTROLLED, PLUG_AND_CHARGE = 0, 1


def _starts(sim, mark: int = 0) -> int:
    return sum(1 for a, v in sim.wire_writes(mark) if (a, v) == (CTRL, 1))


async def test_controlled_limit_change_finishing_mid_write_never_sends_start(rigs):
    """Car finishes between _preflight and the write: must revoke, never send Start."""
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert rig.sim.state == CHARGING

    real_write = rig.sim.write
    tripped = False

    async def finish_during_write(address, value):
        nonlocal tripped
        if not tripped and address == P and value == CAP + 6:
            tripped = True
            rig.sim.state = FINISHED
        return await real_write(address, value)

    rig.sim.write = finish_during_write
    mark = rig.sim.mark()
    with pytest.raises(ControlError):
        await rig.run(ctl.async_set_power(CAP + 6))

    assert _starts(rig.sim, mark) == 0
    assert not ctl.intent_enabled
    assert not ctl._await_plug
    assert rig.sim.state != CHARGING
    assert rig.sim.holding[P] == 0
    await rig.clock.advance(60)
    assert _starts(rig.sim, mark) == 0
    assert rig.sim.state != CHARGING


@pytest.mark.parametrize("work_mode", [CONTROLLED, PLUG_AND_CHARGE])
async def test_limit_change_unplug_mid_write_never_arms_await_plug(rigs, work_mode):
    """Car unplugs between _preflight and _confirm_session: must revoke, not stage."""
    rig = rigs(work_mode=work_mode, implicit_resume=False)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert rig.sim.state == CHARGING

    real_write = rig.sim.write
    tripped = False

    async def unplug_during_write(address, value):
        nonlocal tripped
        if not tripped and address == P and value == CAP + 6:
            tripped = True
            rig.sim.unplug()
        return await real_write(address, value)

    rig.sim.write = unplug_during_write
    mark = rig.sim.mark()
    with pytest.raises(ControlError):
        await rig.run(ctl.async_set_power(CAP + 6))

    assert not ctl.intent_enabled
    assert not ctl._await_plug
    assert _starts(rig.sim, mark) == 0
    assert rig.sim.holding[P] == 0

    if work_mode == CONTROLLED:
        # Plugging a car in later must not auto-start charging.
        replug_mark = rig.sim.mark()
        rig.sim.plug()
        await rig.run(ctl.async_poll())
        await rig.clock.advance(60)
        assert _starts(rig.sim, replug_mark) == 0
        assert not rig.sim.positive_cap_writes(replug_mark)
        assert rig.sim.state == CONNECTED
        assert not ctl.intent_enabled
