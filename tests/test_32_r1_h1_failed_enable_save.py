"""3.2 R1-H1: failed enable/unlatch save must not leak via later session save or late write."""
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
)
from test_controller import make, saved


def _cold_restart(hass) -> None:
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_REVISION_KEY, None)
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_HARD_KEY, None)


async def test_failed_enable_save_then_session_save_stays_paused_after_cold_restart(
    hass, hass_storage, monkeypatch
):
    """If an enable save fails, a later unrelated session save must not write
    the rejected enabled state to disk and resurrect it on a cold restart."""
    entry = "r1h1enable"
    initial = saved(enabled=False, power=70)
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(initial)
    c, _io = await make(initial, charging=False, persist=storage.async_save_controller)

    real_save = Store.async_save

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    with pytest.raises(ControlError):
        await c.async_enable()
    assert not c.intent_enabled
    assert c.diagnostics["persist_pending"] is True

    # Storage recovers and an unrelated session save runs before any refresh.
    monkeypatch.setattr(Store, "async_save", real_save)
    await storage.async_save_session({"prev_status": 3}, rearm_required=False)

    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["enabled"] is False

    # Refresh persists the bumped rollback revision cleanly too.
    await c._op_refresh()
    assert c.diagnostics["persist_pending"] is False
    _cold_restart(hass)
    loaded2 = await ChargerStorage(hass, entry).async_load()
    assert loaded2.controller["enabled"] is False
    assert loaded2.controller["revision"] == c.export_state()["revision"]


async def test_failed_hard_latch_clear_save_keeps_hard_latch_across_session_save_and_restart(
    hass, hass_storage, monkeypatch
):
    """A failed human enable that tried to clear a hard latch must not lose
    the hard latch on a subsequent session save and cold restart."""
    entry = "r1h1hard"
    initial = saved(enabled=False, power=70, safety_latched=True, latch_cause="hard")
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(initial)
    c, _io = await make(initial, charging=False, persist=storage.async_save_controller)
    assert c.needs_human and c.latch_cause == "hard"

    real_save = Store.async_save

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    with pytest.raises(ControlError):
        await c.async_enable(clear_hard=True)
    assert not c.intent_enabled and c.needs_human and c.latch_cause == "hard"
    assert c.diagnostics["persist_pending"] is True

    # Partial recovery + unrelated session save must still carry the hard latch.
    monkeypatch.setattr(Store, "async_save", real_save)
    await storage.async_save_session({"prev_status": 3}, rearm_required=False)

    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["enabled"] is False
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"


async def test_timed_out_enable_save_landing_late_cannot_resurrect_enable(
    hass, hass_storage
):
    """If an enable save times out in the controller and its underlying write
    lands late, the bumped rollback save keeps disk paused across a restart."""
    entry = "r1h1timeout"
    initial = saved(enabled=False, power=70)
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(initial)

    release = asyncio.Event()
    late_tasks: list[asyncio.Task] = []
    late_rev: list[int] = []

    async def persist(state: dict) -> None:
        if state["enabled"] and not late_tasks:
            late_rev.append(state["revision"])

            async def _delayed_write() -> None:
                await release.wait()
                await storage.async_save_controller(state)

            task = asyncio.create_task(_delayed_write())
            late_tasks.append(task)
            await asyncio.shield(task)
            return
        await storage.async_save_controller(state)

    c, _io = await make(
        initial, charging=False, persist=persist, persist_timeout=0.05
    )
    with pytest.raises(ControlError):
        await c.async_enable()
    assert not c.intent_enabled
    assert c.export_state()["revision"] > late_rev[0]

    # Now the timed-out enable write lands late.
    release.set()
    await late_tasks[0]
    await c._op_refresh()

    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["enabled"] is False
    assert loaded.controller["revision"] > late_rev[0]
