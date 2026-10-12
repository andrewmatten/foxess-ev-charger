"""Explicit-start safety: preflight status, telemetry grace and fault latch.

A positive explicit start must not reach the wire before a fresh, valid,
fault-free status frame has been read. Every observation made while
confirming the start counts toward the telemetry grace, and a fault seen on
any command observation latches and puts zero on the wire at once, exactly
as background refresh/polling would.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.transport import TransportError

from test_controller import CUR, POW, make, saved

STATUS_BLOCK = 0x1000
FAULT_LO = 0x101B
GRACE = 20.0          # controller default telemetry_grace
POLL_STEP = 0.5


def status_block_fails(io) -> None:
    """Status frame unreadable (io.status_down) while registers still work."""
    io.status_down = False
    real = io.read

    async def read(address, count):
        if address == STATUS_BLOCK and io.status_down:
            raise TransportError("status block read failed")
        return await real(address, count)

    io.read = read


def on_positive_power(io, action) -> None:
    async def hook(value):
        if value > 0:
            action()
    io.hooks[POW] = hook


def positive_writes(io):
    return [(t, a, v) for t, a, v in io.writes if a in (CUR, POW) and v > 0]


def power_write_times(io, value):
    return [t for t, a, v in io.writes if a == POW and v == value]


# ── no positive write without a valid status frame ──────────────────

async def test_enable_with_unreadable_status_never_writes_positive():
    c, io = await make(saved(enabled=False, current=320), charging=False)
    status_block_fails(io)
    io.status_down = True
    start = io.clock.now
    with pytest.raises(ControlError):
        await c.async_enable()
    assert positive_writes(io) == []
    assert io.regs[POW] == 0 and not c.intent_enabled
    assert io.clock.now - start <= GRACE + POLL_STEP


async def test_status_lost_after_start_write_zeroes_within_telemetry_grace():
    c, io = await make(saved(enabled=False), charging=False)
    status_block_fails(io)
    on_positive_power(io, lambda: setattr(io, "status_down", True))
    with pytest.raises(ControlError):
        await c.async_enable()
    started = power_write_times(io, 70)[0]
    zeroed = power_write_times(io, 0)
    assert zeroed and zeroed[0] - started <= GRACE + POLL_STEP
    assert io.regs[POW] == 0 and not c.intent_enabled


async def test_single_lost_status_read_at_preflight_still_enables():
    c, io = await make(saved(enabled=False), charging=False)
    status_block_fails(io)
    io.status_down = True
    real = io.read

    async def read(address, count):
        try:
            return await real(address, count)
        finally:
            io.status_down = False             # only the first frame is lost

    io.read = read
    result = await c.async_enable()
    assert result.outcome == "confirmed" and c.phase == "enabled"


# ── a fault on a command observation latches and zeroes at once ─────

async def test_enable_onto_a_faulted_charger_latches_and_never_writes_positive():
    c, io = await make(saved(enabled=False), charging=False)
    io.regs[FAULT_LO] = 1
    with pytest.raises(ControlError):
        await c.async_enable()
    assert c.diagnostics["safety_latched"] and c.phase == "faulted"
    assert positive_writes(io) == []
    assert io.regs[POW] == 0


async def test_fault_on_start_confirmation_frame_latches_and_zeroes_same_frame():
    c, io = await make(saved(enabled=False), charging=False)
    on_positive_power(io, lambda: io.regs.__setitem__(FAULT_LO, 1))
    with pytest.raises(ControlError):
        await c.async_enable()
    assert c.diagnostics["safety_latched"] and c.phase == "faulted"
    started = power_write_times(io, 70)[0]
    # No sleep (no further poll) between the faulted frame and the zero.
    assert power_write_times(io, 0)[0] == started
    assert io.regs[POW] == 0


async def test_limit_change_observing_a_fault_latches_instead_of_writing():
    c, io = await make()                       # enabled, charging at 7.0 kW
    io.regs[FAULT_LO] = 1
    with pytest.raises(ControlError):
        await c.async_set_power(50)
    assert c.diagnostics["safety_latched"] and c.phase == "faulted"
    assert (POW, 50) not in [(a, v) for _, a, v in io.writes]
    assert io.regs[POW] == 0
