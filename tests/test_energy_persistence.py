"""Tests for energy-baseline persistence (Task 2): the first live energy
reading after a restart must be checked against a persisted last-known-good
value, not accepted outright just because self._last_energy is empty again.

Covers: restore converts wall-clock to a synthetic monotonic baseline;
the known 65800/6580.0kWh incident value is rejected when it arrives as the
first live reading after a restart with a persisted normal baseline; malformed
storage is discarded safely per-key; debounced dirty-flag saving; prompt
persistence at session boundaries and on unload.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.foxess_charger import (
    FoxESSChargerCoordinator, async_unload_entry, DOMAIN,
)


def make_coordinator(hass, energy_store=None) -> FoxESSChargerCoordinator:
    return FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, energy_store=energy_store)


class FakeStore:
    def __init__(self, initial=None):
        self._data = initial
        self.saved: list[dict] = []

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data
        self.saved.append(data)


class TestRestoreConvertsWallClockToMonotonic:
    async def test_restored_baseline_lands_in_last_energy(self, hass):
        wall_ts = time.time() - 120  # 2 minutes ago
        store = FakeStore({"total_energy_raw": {"raw": 3785, "wall_ts": wall_ts}})
        coordinator = make_coordinator(hass, energy_store=store)

        await coordinator.async_load_energy_state()

        assert "total_energy_raw" in coordinator._last_energy
        raw, synthetic_ts = coordinator._last_energy["total_energy_raw"]
        assert raw == 3785
        # synthetic_ts should be ~120s before "now" on the monotonic clock.
        assert abs((time.monotonic() - synthetic_ts) - 120) < 5


class TestKnownIncidentValueRejectedAfterRestartWithPersistedBaseline:
    async def test_65800_is_rejected_as_the_first_live_reading(self, hass, monkeypatch):
        """The actual bug: without a persisted baseline, raw=65800 sails
        through as a trusted first-ever reading (only the 100,000kWh
        absolute ceiling applies, and 6580.0kWh is nowhere near it). With a
        realistic persisted baseline restored, the same first live reading
        must go through the normal rate-based check and be rejected."""
        wall_ts = time.time() - 60  # HA was down for ~60s
        store = FakeStore({"total_energy_raw": {"raw": 3785, "wall_ts": wall_ts}})
        coordinator = make_coordinator(hass, energy_store=store)
        await coordinator.async_load_energy_state()

        mono_ts = [time.monotonic()]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )

        result = coordinator._sanitize_energy(
            "total_energy_raw", 65800, {"status": 1, "max_power_raw": 73},
        )
        assert result is None
        assert coordinator.energy_rejections[-1]["last_good_raw"] == 3785  # rejection compared against the restored baseline
        assert len(coordinator.energy_rejections) == 1

    async def test_without_a_persisted_baseline_the_same_value_would_be_accepted(self, hass):
        """Contrast case, documenting why this task exists: a genuinely
        fresh install (no Store data yet) has nothing to restore, so the
        first live reading still only gets the weak absolute-ceiling check
        - 6580.0kWh is unremarkable for that check alone."""
        coordinator = make_coordinator(hass, energy_store=FakeStore(None))
        await coordinator.async_load_energy_state()
        result = coordinator._sanitize_energy(
            "total_energy_raw", 65800, {"status": 1, "max_power_raw": 73},
        )
        assert result == 65800  # accepted - documents the residual gap for a genuinely new install


class TestMalformedStorageDiscardedSafely:
    async def test_non_dict_entry_is_discarded(self, hass):
        store = FakeStore({"total_energy_raw": "not-a-dict"})
        coordinator = make_coordinator(hass, energy_store=store)
        await coordinator.async_load_energy_state()
        assert "total_energy_raw" not in coordinator._last_energy

    async def test_negative_raw_is_discarded(self, hass):
        store = FakeStore({"total_energy_raw": {"raw": -5, "wall_ts": time.time()}})
        coordinator = make_coordinator(hass, energy_store=store)
        await coordinator.async_load_energy_state()
        assert "total_energy_raw" not in coordinator._last_energy

    async def test_non_numeric_wall_ts_is_discarded(self, hass):
        store = FakeStore({"total_energy_raw": {"raw": 100, "wall_ts": "yesterday"}})
        coordinator = make_coordinator(hass, energy_store=store)
        await coordinator.async_load_energy_state()
        assert "total_energy_raw" not in coordinator._last_energy

    async def test_boolean_raw_is_rejected_despite_bool_being_an_int_subclass(self, hass):
        store = FakeStore({"total_energy_raw": {"raw": True, "wall_ts": time.time()}})
        coordinator = make_coordinator(hass, energy_store=store)
        await coordinator.async_load_energy_state()
        assert "total_energy_raw" not in coordinator._last_energy

    async def test_one_bad_key_does_not_discard_a_good_sibling_key(self, hass):
        store = FakeStore({
            "total_energy_raw": {"raw": -5, "wall_ts": time.time()},
            "current_energy_raw": {"raw": 100, "wall_ts": time.time()},
        })
        coordinator = make_coordinator(hass, energy_store=store)
        await coordinator.async_load_energy_state()
        assert "total_energy_raw" not in coordinator._last_energy
        assert "current_energy_raw" in coordinator._last_energy


class TestDebouncedDirtyFlagSaving:
    async def test_unchanged_repeated_readings_do_not_mark_dirty(self, hass, monkeypatch):
        coordinator = make_coordinator(hass, energy_store=FakeStore(None))
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._sanitize_energy("total_energy_raw", 1000, {"status": 3, "max_power_raw": 73})
        coordinator._energy_state_dirty = False  # reset after the first (always-dirty) read
        mono_ts[0] = 13.0
        coordinator._sanitize_energy("total_energy_raw", 1000, {"status": 3, "max_power_raw": 73})  # unchanged
        assert coordinator._energy_state_dirty is False

    async def test_a_changed_value_marks_dirty(self, hass, monkeypatch):
        coordinator = make_coordinator(hass, energy_store=FakeStore(None))
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._sanitize_energy("total_energy_raw", 1000, {"status": 3, "max_power_raw": 73})
        coordinator._energy_state_dirty = False
        mono_ts[0] = 13.0
        coordinator._sanitize_energy("total_energy_raw", 1001, {"status": 3, "max_power_raw": 73})
        assert coordinator._energy_state_dirty is True


class TestSessionBoundaryAndUnloadFlushPromptly:
    async def test_session_boundary_marks_dirty_even_if_value_unchanged(self, hass, monkeypatch):
        coordinator = make_coordinator(hass, energy_store=FakeStore(None))
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._prev_status = 1  # inactive
        coordinator._energy_state_dirty = False
        coordinator._sanitize_energy(
            "current_energy_raw", 0, {"status": 3, "max_power_raw": 73},  # status transitioning to active
        )
        assert coordinator._energy_state_dirty is True

    async def test_unload_flushes_unconditionally(self, hass, monkeypatch):
        store = FakeStore(None)
        client = MagicMock()
        client.disconnect = MagicMock()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, energy_store=store)
        coordinator._last_energy["total_energy_raw"] = (12345, time.monotonic())
        coordinator._heartbeat_task = None

        hass.data.setdefault(DOMAIN, {})["fake-entry"] = {"coordinator": coordinator, "client": client}
        entry = MagicMock()
        entry.entry_id = "fake-entry"

        async def fake_unload_platforms(entry, platforms):
            return True
        monkeypatch.setattr(hass.config_entries, "async_unload_platforms", fake_unload_platforms)

        await async_unload_entry(hass, entry)

        assert store.saved, "energy state was never persisted on unload"
        assert store.saved[-1]["total_energy_raw"]["raw"] == 12345
