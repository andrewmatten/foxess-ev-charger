"""3.2 N10: hard latch survives failed human enable on offline charger and save/restore failures."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.helpers.storage import Store

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.persistence import (
    ATTEMPTED_HARD_KEY,
    ATTEMPTED_REVISION_KEY,
    ChargerStorage,
    state_key,
)

from test_comms_latch_release import FAULT_LO, cause, healthy_for
from test_controller import POW, FakeClock, FakeIO, make, saved
from test_fault_and_stop_fallback import drain, positive_writes


async def _hard_latched(**kw):
    c, io = await make(**kw)
    io.regs[FAULT_LO] = 1 << 6  # leakage current -> hard latch
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    assert cause(c) == "hard" and c.needs_human
    io.writes.clear()
    return c, io


async def test_human_enable_on_offline_charger_keeps_hard_latch():
    """Clearing a hard latch when the charger is offline must not downgrade it
    to an auto-releasing comms latch."""
    c, io = await _hard_latched()
    io.fail_reads = True
    io.refuse.add(POW)

    with pytest.raises(ControlError):
        await c.async_enable(clear_hard=True)
    await drain(c)

    assert cause(c) == "hard"
    assert c.needs_human
    assert c.phase == "faulted"
    assert not c.intent_enabled

    # Link returns healthy: hard latch must still never auto-release.
    io.fail_reads = False
    io.refuse.clear()
    await healthy_for(c, io, 3600)
    assert cause(c) == "hard" and c.needs_human and c.phase == "faulted"
    assert positive_writes(io) == []


async def test_partial_save_failure_preserves_hard_latch_across_cold_restart(
    hass, hass_storage
):
    """If the legacy session save succeeds while the state file write fails
    during a hard latch, a cold restart must still restore a hard latch."""
    entry = "n10entry01"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved())
    c, io = await make(saved(), persist=storage.async_save_controller)

    async def refuse(_data):
        raise OSError("state write failed")

    storage._state_store.async_save = refuse
    io.regs[FAULT_LO] = 1 << 6  # leakage current -> hard latch
    await c.async_poll()
    await drain(c)
    assert c.latch_cause == "hard" and c.needs_human

    # Cold restart: in-memory attempted tracking is gone.
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_REVISION_KEY, None)
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_HARD_KEY, None)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["safety_latched"] is True

    io2 = FakeIO(FakeClock(), charging=False)
    io2.regs[POW] = 0
    c2, io2 = await make(loaded.controller, io=io2)
    assert c2.latch_cause == "hard" and c2.needs_human and c2.phase == "faulted"
    await healthy_for(c2, io2, 3600)
    assert c2.latch_cause == "hard" and c2.needs_human and positive_writes(io2) == []


async def test_total_save_failure_of_hard_latch_survives_entry_reload(
    hass, hass_storage, monkeypatch
):
    """If all store saves fail on a hard latch, a hot entry reload must still
    restore the hard latch rather than downgrading it to a plain pause."""
    entry = "n10entry02"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved())
    c, io = await make(saved(), persist=storage.async_save_controller)

    real_save = Store.async_save

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    io.regs[FAULT_LO] = 1 << 6
    await c.async_poll()
    await drain(c)
    assert c.latch_cause == "hard" and c.diagnostics["persist_pending"]

    monkeypatch.setattr(Store, "async_save", real_save)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert "controller_state_stale" in loaded.issues
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"


async def test_malformed_state_salvage_keeps_hard_latch(hass, hass_storage):
    entry = "n10entry03"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(
        saved(enabled=False, safety_latched=True, latch_cause="hard")
    )
    hass_storage[state_key(entry)]["data"]["controller"]["enabled"] = "corrupt"

    loaded = await ChargerStorage(hass, entry).async_load()
    assert "controller_state_malformed" in loaded.issues
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"


async def test_older_save_completing_does_not_clear_pending_hard_latch():
    """An older in-flight save completing after a newer hard-latch save failed
    must not clear persist_pending."""
    entered, release = asyncio.Event(), asyncio.Event()
    fail_hard = {"on": False}

    async def persist(state):
        if state["safety_latched"]:
            if fail_hard["on"]:
                raise OSError("disk full")
            return
        if not entered.is_set() and state["power_raw"] == 50:
            entered.set()
            await release.wait()

    c, io = await make(persist=persist)
    t_change = asyncio.create_task(c.async_set_power(50))
    await asyncio.wait_for(entered.wait(), 1)

    fail_hard["on"] = True
    io.regs[FAULT_LO] = 1 << 6
    await c.async_poll()
    await drain(c)
    assert c.latch_cause == "hard" and c.diagnostics["persist_pending"] is True

    release.set()
    try:
        await t_change
    except ControlError:
        pass
    assert c.latch_cause == "hard"
    assert c.diagnostics["persist_pending"] is True
