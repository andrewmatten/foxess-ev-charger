"""3.2 R5-3: durable hard latch stays persisted until healthy preflight succeeds."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.const import DOMAIN, REG_STATUS_BLOCK_START
from custom_components.foxess_charger.controller import ChargingController, ControlError
from custom_components.foxess_charger.persistence import (
    ATTEMPTED_HARD_KEY,
    ATTEMPTED_REVISION_KEY,
    ChargerStorage,
)
from test_comms_latch_release import FAULT_LO
from test_controller import POW, FakeClock, FakeIO, make, saved, snapshot
from test_fault_and_stop_fallback import drain, positive_writes


def _cold_restart(hass) -> None:
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_REVISION_KEY, None)
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_HARD_KEY, None)


async def test_restart_mid_preflight_during_human_ack_preserves_hard_latch(
    hass, hass_storage
):
    """If HA stops while async_enable(clear_hard=True) is still waiting on the
    fresh charger preflight, storage must still hold the hard latch so restart
    keeps needs_human=True and blocks automations."""
    entry = "r5_3_mid_preflight"
    initial = saved(enabled=False, power=70, safety_latched=True, latch_cause="hard")
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(initial)

    io = FakeIO(FakeClock(), charging=False)
    io.regs[POW] = 0
    c, io = await make(initial, io=io, persist=storage.async_save_controller)
    assert c.needs_human and c.latch_cause == "hard"

    entered, release = asyncio.Event(), asyncio.Event()
    real_read = io.read

    async def blocked_preflight_read(address: int, count: int):
        if address == REG_STATUS_BLOCK_START and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_read(address, count)

    io.read = blocked_preflight_read

    t_enable = asyncio.create_task(c.async_enable(clear_hard=True))
    await asyncio.wait_for(entered.wait(), 1)

    # Simulate HA stopping before preflight finishes: cold restart from disk.
    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"
    assert loaded.controller["enabled"] is False

    clock2 = FakeClock()
    io2 = FakeIO(clock2, charging=False)
    io2.regs[POW] = 0
    c2 = ChargingController(io2, clock=clock2, sleep=clock2.sleep, read_snapshot=snapshot)
    await c2.async_initialize(loaded.controller, configure_safety=False)
    assert c2.needs_human and c2.latch_cause == "hard"
    with pytest.raises(ControlError, match="yourself"):
        await c2.async_enable()
    assert positive_writes(io2) == []

    # Clean up the original in-flight task cleanly.
    release.set()
    res = await t_enable
    assert res.outcome == "confirmed"
    await drain(c)


async def test_human_ack_clears_durable_hard_latch_after_healthy_preflight_before_wire_writes(
    hass, hass_storage
):
    """Once preflight sees a healthy frame, clearing the hard latch is durably
    saved before any positive limit write hits the wire."""
    entry = "r5_3_order"
    initial = saved(enabled=False, power=70, safety_latched=True, latch_cause="hard")
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(initial)

    events: list[str] = []

    async def recording_persist(state: dict) -> None:
        if not state["safety_latched"]:
            events.append("save_unlatched")
        await storage.async_save_controller(state)

    io = FakeIO(FakeClock(), charging=False)
    io.regs[POW] = 0
    c, io = await make(initial, io=io, persist=recording_persist)

    real_read = io.read
    real_write = io.write

    async def traced_read(address: int, count: int):
        if address == REG_STATUS_BLOCK_START and "preflight_read" not in events:
            events.append("preflight_read")
        return await real_read(address, count)

    async def traced_write(address: int, value: int):
        if address == POW and value > 0 and "positive_write" not in events:
            events.append("positive_write")
        return await real_write(address, value)

    io.read = traced_read
    io.write = traced_write

    res = await c.async_enable(clear_hard=True)
    assert res.outcome == "confirmed"
    assert events == ["preflight_read", "save_unlatched", "positive_write"]
    assert not c.needs_human and c.intent_enabled


async def test_human_ack_while_fault_still_active_never_saves_unlatched():
    """If the fault is still present during preflight, persist is never called
    with safety_latched=False."""
    saved_states: list[dict] = []

    async def persist(state: dict) -> None:
        saved_states.append(dict(state))

    io = FakeIO(FakeClock(), charging=False)
    io.regs[POW] = 0
    c, io = await make(
        saved(enabled=False, power=70, safety_latched=True, latch_cause="hard"),
        io=io,
        persist=persist,
    )
    saved_states.clear()
    io.regs[FAULT_LO] = 1 << 6

    with pytest.raises(ControlError, match="hard fault latch restored"):
        await c.async_enable(clear_hard=True)
    await drain(c)

    assert all(s["safety_latched"] is True for s in saved_states)
    assert c.needs_human and c.latch_cause == "hard"
    assert positive_writes(io) == []
