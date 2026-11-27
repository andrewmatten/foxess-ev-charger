"""3.2 R4-2: async_save_session must not overwrite a failed pause or hard latch."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.persistence import (
    ATTEMPTED_HARD_KEY,
    ATTEMPTED_REVISION_KEY,
    ChargerStorage,
    session_key,
)
from test_comms_latch_release import FAULT_LO
from test_controller import make, saved
from test_fault_and_stop_fallback import drain


def _cold_restart(hass) -> None:
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_REVISION_KEY, None)
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_HARD_KEY, None)


async def test_session_save_after_failed_hard_latch_state_write_preserves_hard_latch(
    hass, hass_storage
):
    """If a hard-latch save writes session (stop_pending=True) then fails on the
    state file, a subsequent session save must persist the hard latch rather than
    overwriting disk with the older enabled/unlatched state."""
    entry = "r4_2_hard"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    c, io = await make(saved(), persist=storage.async_save_controller)

    real_state_save = storage._state_store.async_save
    fail = {"on": True}

    async def flaky_state_save(data):
        if fail["on"]:
            raise OSError("state write failed")
        return await real_state_save(data)

    storage._state_store.async_save = flaky_state_save

    io.regs[FAULT_LO] = 1 << 6  # leakage current -> hard latch
    await c.async_poll()
    await drain(c)
    assert c.latch_cause == "hard" and c.needs_human
    assert c.diagnostics["persist_pending"] is True

    # Storage recovers; coordinator immediately persists dirty session state
    # before the next 20 s refresh retry.
    fail["on"] = False
    await storage.async_save_session({"prev_status": 6}, rearm_required=False)

    assert hass_storage[session_key(entry)]["data"]["stop_pending"] is True
    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"

    # Refresh retry clears persist_pending cleanly too.
    await c._op_refresh()
    assert c.diagnostics["persist_pending"] is False


async def test_session_save_after_failed_pause_state_write_preserves_pause(
    hass, hass_storage
):
    """If async_pause fails on the state write after saving session, the
    immediate coordinator refresh/session save must keep the pause on disk."""
    entry = "r4_2_pause"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    c, _io = await make(saved(), persist=storage.async_save_controller)

    real_state_save = storage._state_store.async_save
    fail = {"on": True}

    async def flaky_state_save(data):
        if fail["on"]:
            raise OSError("state write failed")
        return await real_state_save(data)

    storage._state_store.async_save = flaky_state_save

    with pytest.raises(ControlError, match="not saved"):
        await c.async_pause()
    assert not c.intent_enabled and c.diagnostics["persist_pending"] is True

    fail["on"] = False
    await storage.async_save_session({"prev_status": 4}, rearm_required=False)

    assert hass_storage[session_key(entry)]["data"]["stop_pending"] is True
    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["enabled"] is False


async def test_session_save_after_hard_latch_timeout_before_lock_preserves_hard_latch(
    hass, hass_storage
):
    """Even if a hard-latch save times out waiting for _write_lock before
    updating _controller, a later session save must project paused + hard."""
    entry = "r4_2_timeout"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved(enabled=True, revision=5))

    async with storage._write_lock:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                storage.async_save_controller(
                    saved(enabled=True, safety_latched=True, latch_cause="hard", revision=6)
                ),
                timeout=0.01,
            )

    await storage.async_save_session({"prev_status": 6}, rearm_required=False)

    assert hass_storage[session_key(entry)]["data"]["stop_pending"] is True
    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["enabled"] is False
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"
