"""3.2 N1: protective_state() callers carry over valid user safety settings."""
from __future__ import annotations

import pytest
from homeassistant.helpers.storage import Store

from custom_components.foxess_charger.persistence import (
    ChargerStorage,
    session_key,
    state_key,
)

ENTRY = "n1entry01"


def _put(hass_storage, key, data):
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


def _state(**overrides):
    base = {
        "schema": 1,
        "enabled": True,
        "power_raw": 22,
        "current_raw": 100,
        "revision": 7,
        "safety_latched": False,
        "stop_fallback": False,
        "user_time_validity_s": 30,
        "user_default_current_raw": 160,
    }
    base.update(overrides)
    return base


async def test_malformed_controller_state_keeps_user_safety_settings(hass, hass_storage):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state())
    hass_storage[state_key(ENTRY)]["data"]["controller"]["enabled"] = "maybe"

    result = await ChargerStorage(hass, ENTRY).async_load()
    assert result.controller["enabled"] is False
    assert "controller_state_malformed" in result.issues
    assert result.controller["user_time_validity_s"] == 30
    assert result.controller["user_default_current_raw"] == 160


async def test_stale_revision_keeps_user_safety_settings(hass, hass_storage, monkeypatch):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(
        _state(enabled=True, revision=5, user_time_validity_s=45, user_default_current_raw=120)
    )
    real_save = Store.async_save

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    with pytest.raises(OSError):
        await storage.async_save_controller(_state(enabled=False, revision=6))
    monkeypatch.setattr(Store, "async_save", real_save)

    result = await ChargerStorage(hass, ENTRY).async_load()
    assert result.controller["enabled"] is False
    assert "controller_state_stale" in result.issues
    assert result.controller["user_time_validity_s"] == 45
    assert result.controller["user_default_current_raw"] == 120


@pytest.mark.parametrize(
    "legacy_session",
    [
        {
            "session_start_wall": None,
            "session_start_total": None,
            "last_session": None,
            "prev_status": 3,
            "stop_inhibit": True,
            "stop_pending": False,
        },
        {
            "session_start_wall": None,
            "session_start_total": None,
            "last_session": None,
            "prev_status": 3,
            "stop_inhibit": "bad",
            "stop_pending": False,
        },
    ],
)
async def test_legacy_import_paths_keep_user_safety_settings(hass, hass_storage, legacy_session):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state(user_time_validity_s=40, user_default_current_raw=140))
    # Simulate rollback / corrupted legacy file forcing _import_legacy().
    _put(hass_storage, session_key(ENTRY), legacy_session)

    result = await ChargerStorage(hass, ENTRY).async_load()
    assert result.source == "legacy"
    assert result.controller["enabled"] is False
    assert result.controller["user_time_validity_s"] == 40
    assert result.controller["user_default_current_raw"] == 140


async def test_invalid_user_safety_values_are_dropped_on_salvage(hass, hass_storage):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state())
    ctrl = hass_storage[state_key(ENTRY)]["data"]["controller"]
    ctrl["enabled"] = "maybe"
    ctrl["user_time_validity_s"] = 9999
    ctrl["user_default_current_raw"] = True

    result = await ChargerStorage(hass, ENTRY).async_load()
    assert result.controller["enabled"] is False
    assert "user_time_validity_s" not in result.controller
    assert "user_default_current_raw" not in result.controller
