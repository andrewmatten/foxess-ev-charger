"""3.2 N6: serialize ChargerStorage._async_write_all() so concurrent saves stay consistent."""
from __future__ import annotations

import asyncio

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.persistence import ChargerStorage


def _state(**overrides):
    base = {
        "schema": 1,
        "enabled": True,
        "power_raw": 70,
        "current_raw": 100,
        "revision": 5,
        "safety_latched": False,
        "stop_fallback": False,
        "latch_cause": None,
    }
    base.update(overrides)
    return base


async def test_concurrent_session_and_controller_saves_preserve_hard_latch(hass, hass_storage):
    """A session save yielding before writing the legacy session file must not
    interleave with a concurrent hard-latch save and overwrite the projection."""
    entry = "n6entry01"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(_state(enabled=True, revision=5))

    entered, release = asyncio.Event(), asyncio.Event()
    real_save = storage._session_store.async_save

    async def delayed_session_save(data):
        if data.get("stop_inhibit") is False and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_save(data)

    storage._session_store.async_save = delayed_session_save

    t_sess = asyncio.create_task(
        storage.async_save_session({"prev_status": 3}, rearm_required=False)
    )
    await asyncio.wait_for(entered.wait(), 1)

    t_ctrl = asyncio.create_task(
        storage.async_save_controller(
            _state(enabled=False, safety_latched=True, latch_cause="hard", revision=6)
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)

    release.set()
    await asyncio.gather(t_sess, t_ctrl)

    # Cold restart: in-memory attempted revision is gone.
    hass.data.pop(f"{DOMAIN}_attempted_revision", None)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.source == "state"
    assert loaded.controller["enabled"] is False
    assert loaded.controller["safety_latched"] is True
    assert loaded.controller["latch_cause"] == "hard"
    assert loaded.controller["revision"] == 6


async def test_concurrent_state_write_does_not_leave_mismatched_fingerprint(hass, hass_storage):
    """A session save yielding before writing the state file must not overwrite
    a newer controller save's state with an old legacy_fingerprint."""
    entry = "n6entry02"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(_state(enabled=True, revision=5))

    entered, release = asyncio.Event(), asyncio.Event()
    real_save = storage._state_store.async_save

    async def delayed_state_save(data):
        if data["legacy_fingerprint"]["session"]["stop_inhibit"] is False and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_save(data)

    storage._state_store.async_save = delayed_state_save

    t_sess = asyncio.create_task(
        storage.async_save_session({"prev_status": 3}, rearm_required=True)
    )
    await asyncio.wait_for(entered.wait(), 1)

    t_ctrl = asyncio.create_task(
        storage.async_save_controller(_state(enabled=False, revision=6))
    )
    for _ in range(10):
        await asyncio.sleep(0)

    release.set()
    await asyncio.gather(t_sess, t_ctrl)

    hass.data.pop(f"{DOMAIN}_attempted_revision", None)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.source == "state"
    assert loaded.controller["enabled"] is False
    assert loaded.controller["revision"] == 6
    assert loaded.session["rearm_required"] is True
