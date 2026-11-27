"""3.2 N15: Stop fallback must only confirm a pause on valid frames."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import CTRL, POW, POWER, STATUS, make


async def _in_stop_fallback():
    c, io = await make()
    io.zero_pauses = False
    res = await c.async_pause()
    assert res.outcome == "confirmed"
    assert c.phase == "stopped" and c.export_state()["stop_fallback"] is True
    io.writes.clear()
    return c, io


async def test_stop_fallback_pause_does_not_confirm_when_reads_fail():
    c, io = await _in_stop_fallback()
    io.fail_reads = True

    with pytest.raises(ControlError):
        await c.async_pause()


@pytest.mark.parametrize(
    "kind",
    ["bad_status", "bad_cc", "unknown_fault"],
)
async def test_stop_fallback_pause_does_not_confirm_on_invalid_telemetry(kind):
    c, io = await _in_stop_fallback()
    if kind == "bad_status":
        io.status_override = 65535
    elif kind == "bad_cc":
        io.cc_override = 65535
    else:
        io.regs[0x101A] = io.regs[0x101B] = 0xFFFF

    with pytest.raises(ControlError):
        await c.async_pause()


async def test_stop_fallback_pause_confirms_on_valid_stopped_frames():
    c, io = await _in_stop_fallback()
    res = await c.async_pause()
    assert res.outcome == "confirmed"
    assert not any(a == CTRL for _, a, _ in io.writes)
    assert io.regs[STATUS] == 5 and io.regs[POWER] == 0


async def test_stop_fallback_pause_sends_stop_when_drawing():
    c, io = await _in_stop_fallback()
    io.regs[POW] = 73
    io.stopped = False
    io._physics()

    res = await c.async_pause()
    assert res.outcome == "confirmed"
    assert (CTRL, 2) in [(a, v) for _, a, v in io.writes]
    assert io.regs[STATUS] == 5 and io.regs[POWER] == 0
