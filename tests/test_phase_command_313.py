"""The manual phase command (0x4002) is one write (review M4, 3.1.3).

No register proves it took effect (on a charger without the phase-switch
box 0x1010 already reads three-phase), so it is sent once, never resent,
and reported staged when the charger acknowledged it.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError

from test_controller import make

PHASE_CMD, PHASE_SEQ, LOCK_CMD = 0x4002, 0x1010, 0x4000


def writes_to(io, address):
    return [v for _, a, v in io.writes if a == address]


@pytest.mark.parametrize("value", [0, 1, 2])
async def test_phase_command_is_one_write_and_staged(value):
    c, io = await make()
    io.ignore.add(PHASE_CMD)                       # acknowledged; 0x1010 does not follow
    start = io.clock.now
    result = await c.async_set_register(PHASE_CMD, value)
    assert result.outcome == "staged" and writes_to(io, PHASE_CMD) == [value]
    assert io.clock.now == start and not c._lock.locked()
    io.writes.clear()
    await c._op_refresh()                          # the heartbeat can run at once
    assert writes_to(io, 0x3002)


@pytest.mark.parametrize("failure", ["lose_reply", "refuse"])
async def test_unacknowledged_phase_command_raises_after_one_write(failure):
    c, io = await make()
    getattr(io, failure).add(PHASE_CMD)
    with pytest.raises(ControlError):
        await c.async_set_register(PHASE_CMD, 1)
    assert writes_to(io, PHASE_CMD) == [1] and not c._lock.locked()


async def test_lock_command_still_confirms_through_the_lock_status():
    c, io = await make()
    io.ignore.add(LOCK_CMD)                        # the lock never moves
    with pytest.raises(ControlError):
        await c.async_set_register(LOCK_CMD, 2)
    assert len(writes_to(io, LOCK_CMD)) > 1
