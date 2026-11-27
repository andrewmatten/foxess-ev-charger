"""3.2 R1-L2: restoring a hard latch after a failed/overtaken human enable updates _last_error."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.transport import TransportError
from test_controller import POW, FakeClock, FakeIO, make, saved
from test_fault_and_stop_fallback import drain


async def _restored_hard_latched():
    io = FakeIO(FakeClock(), charging=False)
    io.regs[POW] = 0
    c, io = await make(
        saved(enabled=False, power=70, safety_latched=True, latch_cause="hard"),
        io=io,
    )
    assert c.needs_human and c.latch_cause == "hard"
    assert c.diagnostics["last_error"] is None
    return c, io


async def test_failed_offline_human_enable_updates_last_error_for_restored_hard_latch():
    """When human enable fails preflight on an offline charger, _last_error
    explains that the hard fault latch was restored."""
    c, io = await _restored_hard_latched()
    io.fail_reads = True

    with pytest.raises(ControlError, match="hard fault latch restored"):
        await c.async_enable(clear_hard=True)
    await drain(c)

    assert c.needs_human and c.latch_cause == "hard"
    assert "hard fault latch restored" in (c.diagnostics["last_error"] or "")


async def test_overtaken_human_enable_before_preflight_updates_last_error():
    """When a concurrent pause overtakes a human enable before _apply_locked
    runs preflight, restoring the hard latch updates _last_error."""
    c, _io = await _restored_hard_latched()

    await c._lock.acquire()
    t_enable = asyncio.create_task(c.async_enable(clear_hard=True))
    for _ in range(5):
        await asyncio.sleep(0)

    t_pause = asyncio.create_task(c.async_pause())
    for _ in range(5):
        await asyncio.sleep(0)
    c._lock.release()

    res = await t_enable
    await t_pause
    await drain(c)

    assert res.outcome == "superseded"
    assert c.needs_human and c.latch_cause == "hard"
    assert "hard fault latch restored" in (c.diagnostics["last_error"] or "")


async def test_overtaken_human_enable_during_preflight_updates_last_error():
    """When a concurrent pause overtakes a human enable mid-preflight,
    restoring the hard latch updates _last_error."""
    c, io = await _restored_hard_latched()
    real_read = io.read
    triggered: list[asyncio.Task] = []

    async def read(address: int, count: int):
        if address == 0x1000 and not triggered:
            t_pause = asyncio.create_task(c.async_pause())
            triggered.append(t_pause)
            for _ in range(5):
                await asyncio.sleep(0)
            raise TransportError("transient read drop")
        return await real_read(address, count)

    io.read = read
    res = await c.async_enable(clear_hard=True)
    await triggered[0]
    await drain(c)

    assert res.outcome == "superseded"
    assert c.needs_human and c.latch_cause == "hard"
    assert "hard fault latch restored" in (c.diagnostics["last_error"] or "")
