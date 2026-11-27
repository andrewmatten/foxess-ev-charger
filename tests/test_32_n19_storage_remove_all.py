"""3.2 N19: ChargerStorage.async_remove_all() and attempted tracking under hass.data[DOMAIN]."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.persistence import (
    ATTEMPTED_HARD_KEY,
    ATTEMPTED_REVISION_KEY,
    ChargerStorage,
    energy_key,
    session_key,
    setpoints_key,
    state_key,
)


def _state(**overrides):
    base = {
        "schema": 1,
        "enabled": False,
        "power_raw": 70,
        "current_raw": 160,
        "revision": 5,
        "safety_latched": True,
        "latch_cause": "hard",
        "stop_fallback": False,
    }
    base.update(overrides)
    return base


async def _populate(hass, entry: str) -> ChargerStorage:
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(_state())
    await storage.async_save_session(
        {
            "session_start_wall": 1_790_000_000.0,
            "session_start_total": 100,
            "last_session": None,
            "prev_status": 3,
        },
        rearm_required=False,
    )
    await storage.async_save_energy(
        {"total_energy_raw": {"raw": 100, "wall_ts": 1_790_000_000.0}}
    )
    return storage


async def test_remove_all_deletes_all_four_stores_and_tracking_for_entry_only(
    hass, hass_storage
):
    """Removing one entry deletes all four of its Store files and in-memory
    attempted tracking without touching any other entry."""
    entry_a = "n19entry_a"
    entry_b = "n19entry_b"
    await _populate(hass, entry_a)
    await _populate(hass, entry_b)

    for entry in (entry_a, entry_b):
        for key_fn in (state_key, session_key, setpoints_key, energy_key):
            assert key_fn(entry) in hass_storage
        assert entry in hass.data[DOMAIN][ATTEMPTED_REVISION_KEY]
        assert entry in hass.data[DOMAIN][ATTEMPTED_HARD_KEY]

    # Fresh instance, as async_remove_entry will construct after unload.
    await ChargerStorage(hass, entry_a).async_remove_all()

    for key_fn in (state_key, session_key, setpoints_key, energy_key):
        assert key_fn(entry_a) not in hass_storage
        assert key_fn(entry_b) in hass_storage
    assert entry_a not in hass.data[DOMAIN][ATTEMPTED_REVISION_KEY]
    assert entry_a not in hass.data[DOMAIN][ATTEMPTED_HARD_KEY]
    assert entry_b in hass.data[DOMAIN][ATTEMPTED_REVISION_KEY]
    assert entry_b in hass.data[DOMAIN][ATTEMPTED_HARD_KEY]

    reloaded = await ChargerStorage(hass, entry_a).async_load()
    assert reloaded.source == "none"
    assert reloaded.controller is None
    assert reloaded.energy == {}
    assert reloaded.issues == []


async def test_remove_all_succeeds_when_stores_absent(hass, hass_storage):
    """Removing an entry whose stores were never written does not raise."""
    storage = ChargerStorage(hass, "n19neverwritten")
    await storage.async_remove_all()
    assert "n19neverwritten" not in hass.data[DOMAIN][ATTEMPTED_REVISION_KEY]
    assert "n19neverwritten" not in hass.data[DOMAIN][ATTEMPTED_HARD_KEY]


async def test_remove_all_continues_after_store_error_and_reraises(
    hass, hass_storage
):
    """If one store removal fails, the remaining stores and in-memory tracking
    are still cleaned up before the error is re-raised."""
    entry = "n19partialerr"
    await _populate(hass, entry)

    remover = ChargerStorage(hass, entry)

    async def _boom():
        raise OSError("disk error")

    remover._session_store.async_remove = _boom

    with pytest.raises(OSError, match="disk error"):
        await remover.async_remove_all()

    assert state_key(entry) not in hass_storage
    assert setpoints_key(entry) not in hass_storage
    assert energy_key(entry) not in hass_storage
    assert session_key(entry) in hass_storage
    assert entry not in hass.data[DOMAIN][ATTEMPTED_REVISION_KEY]
    assert entry not in hass.data[DOMAIN][ATTEMPTED_HARD_KEY]


async def test_attempted_tracking_lives_under_domain_key(hass, hass_storage):
    """Attempted revision/hard tracking stays under hass.data[DOMAIN] instead
    of leaking top-level keys into hass.data."""
    entry = "n19domain"
    await _populate(hass, entry)

    assert f"{DOMAIN}_attempted_revision" not in hass.data
    assert f"{DOMAIN}_attempted_hard" not in hass.data
    assert hass.data[DOMAIN][ATTEMPTED_REVISION_KEY][entry] == 5
    assert hass.data[DOMAIN][ATTEMPTED_HARD_KEY][entry] is True
