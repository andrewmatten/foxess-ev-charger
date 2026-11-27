"""3.2 R4-3: unreadable legacy stores must not use stale state, and corrupt state fails closed to hard latch."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.persistence import (
    ATTEMPTED_HARD_KEY,
    ATTEMPTED_REVISION_KEY,
    ChargerStorage,
    state_key,
)


def _state(**overrides):
    base = {
        "schema": 1,
        "enabled": True,
        "power_raw": 22,
        "current_raw": 100,
        "revision": 7,
        "safety_latched": False,
        "stop_fallback": False,
    }
    base.update(overrides)
    return base


def _cold_restart(hass) -> None:
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_REVISION_KEY, None)
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_HARD_KEY, None)


async def test_unreadable_legacy_stores_never_restore_stale_enabled_state(hass, hass_storage):
    """If both legacy stores fail to read on startup, async_load must not treat
    them as deleted and restore a stale enabled=True state store."""
    entry = "r4_3_unreadable_legacy"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(_state(enabled=True, revision=7))
    _cold_restart(hass)

    reloaded = ChargerStorage(hass, entry)

    async def _boom():
        raise OSError("corrupt json")

    reloaded._session_store.async_load = _boom
    reloaded._setpoints_store.async_load = _boom

    result = await reloaded.async_load()
    assert result.controller["enabled"] is False
    assert result.controller["safety_latched"] is True
    assert result.controller["latch_cause"] == "hard"
    assert "session_unreadable" in result.issues
    assert "setpoints_unreadable" in result.issues


async def test_unreadable_state_store_preserves_hard_latch(hass, hass_storage):
    """A crash mid-write that corrupts the state store must fail closed to a
    hard latch instead of restoring safety_latched=False."""
    entry = "r4_3_unreadable_state"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(
        _state(enabled=False, safety_latched=True, latch_cause="hard", revision=8)
    )
    _cold_restart(hass)

    reloaded = ChargerStorage(hass, entry)

    async def _boom():
        raise ValueError("truncated json")

    reloaded._state_store.async_load = _boom

    result = await reloaded.async_load()
    assert result.controller["enabled"] is False
    assert result.controller["safety_latched"] is True
    assert result.controller["latch_cause"] == "hard"
    assert result.controller["power_raw"] == 22
    assert "state_unreadable" in result.issues
    assert "controller_state_malformed" in result.issues


@pytest.mark.parametrize("corruption", ["state_non_dict", "controller_non_dict", "latch_corrupt"])
async def test_corrupt_state_or_controller_restores_hard_latch(hass, hass_storage, corruption):
    """Non-dict state, non-dict controller, or corrupted safety_latched must all
    fail closed to a hard latch across restarts."""
    entry = f"r4_3_{corruption}"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(
        _state(enabled=False, safety_latched=True, latch_cause="hard", revision=8)
    )
    _cold_restart(hass)

    if corruption == "state_non_dict":
        hass_storage[state_key(entry)]["data"] = "truncated"
    elif corruption == "controller_non_dict":
        hass_storage[state_key(entry)]["data"]["controller"] = "corrupt"
    else:
        hass_storage[state_key(entry)]["data"]["controller"]["safety_latched"] = "yes"

    result = await ChargerStorage(hass, entry).async_load()
    assert result.controller["enabled"] is False
    assert result.controller["safety_latched"] is True
    assert result.controller["latch_cause"] == "hard"
    assert "controller_state_malformed" in result.issues


async def test_malformed_controller_with_stop_pending_session_restores_hard_latch(
    hass, hass_storage
):
    """When controller is malformed and the legacy session has stop_pending=True,
    startup cannot prove the charger was unlatched and must restore a hard latch."""
    entry = "r4_3_stop_pending"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(
        _state(enabled=False, safety_latched=False, revision=8)
    )
    _cold_restart(hass)

    hass_storage[state_key(entry)]["data"]["controller"]["enabled"] = "maybe"
    result = await ChargerStorage(hass, entry).async_load()
    assert result.controller["enabled"] is False
    assert result.controller["safety_latched"] is True
    assert result.controller["latch_cause"] == "hard"
    assert "controller_state_malformed" in result.issues
