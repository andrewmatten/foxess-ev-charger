"""3.2 N4: staged plug-in enable must be counted as an in-flight command."""
from __future__ import annotations

import asyncio

from rebuild_simulator import CHARGING
from test_rebuild_contract import CAP, rigs  # noqa: F401 - fixture

CONTROLLED = 0


async def test_queued_refresh_cannot_revoke_staged_enable_on_plugin(rigs):
    """A refresh queued on controller._lock ahead of the newly spawned plug-in
    enable must not revoke intent.enabled before _op_apply(enabling=True) runs."""
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    rig.sim.unplug()
    await rig.run(ctl.async_set_power(CAP))
    assert (await rig.run(ctl.async_enable())).outcome == "staged"
    assert ctl.intent_enabled and ctl._await_plug

    rig.sim.plug()
    real_snap = ctl._read_snapshot
    queued: list[asyncio.Task] = []

    async def snap_with_queued_refresh(io):
        snap = await real_snap(io)
        if not queued:
            queued.append(asyncio.create_task(ctl._op_refresh()))
            for _ in range(5):
                await asyncio.sleep(0)
        return snap

    ctl._read_snapshot = snap_with_queued_refresh
    await rig.run(ctl.async_poll())
    await rig.run(queued[0])
    await rig.clock.advance(30)

    assert ctl.intent_enabled
    assert rig.sim.state == CHARGING
    assert rig.sim.power_limit_raw() == CAP
    assert ctl._commands == 0


async def test_staged_plugin_enable_decrements_command_count_on_failure(rigs):
    """If the spawned plug-in enable fails, _commands returns to 0."""
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    rig.sim.unplug()
    await rig.run(ctl.async_set_power(CAP))
    assert (await rig.run(ctl.async_enable())).outcome == "staged"

    rig.sim.plug()
    rig.sim.status_override = 8  # locked: refuses to charge
    await rig.run(ctl.async_poll())
    await rig.clock.advance(30)

    assert not ctl.intent_enabled
    assert ctl._commands == 0
