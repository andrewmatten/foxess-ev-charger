"""Energy-baseline persistence: the first live energy reading after a restart
is checked against a persisted last-known-good value rather than trusted.

Ported from the 2.4.3 coordinator tests to the rebuild: EnergyTracker
(restore/export/dirty), ChargerStorage (Store validation of the legacy
``_energy_baseline`` file) and the HA unload path (flush).
"""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.storage import Store

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.energy import EnergyTracker
from custom_components.foxess_charger.persistence import ChargerStorage, energy_key

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY = "energyentry01"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def tracker_at(mono: float, wall: float) -> tuple[EnergyTracker, Clock, Clock]:
    m, w = Clock(mono), Clock(wall)
    return EnergyTracker(clock=m, wall_clock=w), m, w


def feed(tracker: EnergyTracker, key: str, raw: int) -> int | None:
    last = tracker.rejections[-1] if tracker.rejections else None
    data = {key: raw, "max_power_raw": 73}
    tracker.apply(data, session_boundary=False)
    return None if tracker.rejections and tracker.rejections[-1] is not last else data[key]


class TestRestoreConvertsWallClockToMonotonic:
    def test_restored_baseline_lands_in_last_energy(self):
        tracker, _mono, _wall = tracker_at(mono=5000.0, wall=1_790_000_000.0)
        wall_ts = 1_790_000_000.0 - 120          # 2 minutes before "now"
        tracker.restore({"total_energy_raw": {"raw": 3785, "wall_ts": wall_ts}})
        assert tracker.last_good("total_energy_raw") == 3785
        # The synthetic monotonic timestamp is 120 s in the past: exporting
        # maps it back to the same wall-clock time.
        assert tracker.export()["total_energy_raw"]["wall_ts"] == pytest.approx(wall_ts)
        # And rates are judged over those 120 s: 0.3 kWh in 120 s is
        # plausible (9 kW with margin), the same step over ~0 s would not be.
        assert feed(tracker, "total_energy_raw", 3788) == 3788


class TestKnownIncidentValueRejectedAfterRestartWithPersistedBaseline:
    def test_65800_is_rejected_as_the_first_live_reading(self):
        tracker, _m, _w = tracker_at(mono=5000.0, wall=1_790_000_000.0)
        tracker.restore({"total_energy_raw": {"raw": 3785, "wall_ts": 1_790_000_000.0 - 60}})
        assert feed(tracker, "total_energy_raw", 65800) is None
        assert tracker.rejections[-1]["last_good_raw"] == 3785
        assert len(tracker.rejections) == 1

    def test_without_a_persisted_baseline_the_same_value_would_be_accepted(self):
        """Documents the residual gap for a genuinely new install: nothing
        to restore, so only the absolute ceiling applies."""
        tracker, _m, _w = tracker_at(mono=5000.0, wall=1_790_000_000.0)
        tracker.restore({})
        assert feed(tracker, "total_energy_raw", 65800) == 65800


class TestMalformedStorageDiscardedSafely:
    @staticmethod
    async def _load(hass, hass_storage, stored) -> dict:
        hass_storage[energy_key(ENTRY)] = {
            "version": 1, "minor_version": 1, "key": energy_key(ENTRY), "data": stored,
        }
        return (await ChargerStorage(hass, ENTRY).async_load()).energy

    async def test_non_dict_entry_is_discarded(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage, {"total_energy_raw": "not-a-dict"})
        assert "total_energy_raw" not in energy

    async def test_negative_raw_is_discarded(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage,
                                  {"total_energy_raw": {"raw": -5, "wall_ts": 1_790_000_000.0}})
        assert "total_energy_raw" not in energy

    async def test_non_numeric_wall_ts_is_discarded(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage,
                                  {"total_energy_raw": {"raw": 100, "wall_ts": "yesterday"}})
        assert "total_energy_raw" not in energy

    async def test_boolean_raw_is_rejected_despite_bool_being_an_int_subclass(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage,
                                  {"total_energy_raw": {"raw": True, "wall_ts": 1_790_000_000.0}})
        assert "total_energy_raw" not in energy

    async def test_one_bad_key_does_not_discard_a_good_sibling_key(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage, {
            "total_energy_raw": {"raw": -5, "wall_ts": 1_790_000_000.0},
            "current_energy_raw": {"raw": 100, "wall_ts": 1_790_000_000.0},
        })
        assert "total_energy_raw" not in energy
        assert energy["current_energy_raw"] == {"raw": 100, "wall_ts": 1_790_000_000.0}


class TestDebouncedDirtyFlagSaving:
    def test_unchanged_repeated_readings_do_not_mark_dirty(self):
        tracker, mono, _w = tracker_at(mono=0.0, wall=0.0)
        assert feed(tracker, "total_energy_raw", 1000) == 1000
        tracker.dirty = False                     # as after the first save
        mono.t = 13.0
        assert feed(tracker, "total_energy_raw", 1000) == 1000
        assert tracker.dirty is False

    def test_a_changed_value_marks_dirty(self):
        tracker, mono, _w = tracker_at(mono=0.0, wall=0.0)
        assert feed(tracker, "total_energy_raw", 1000) == 1000
        tracker.dirty = False
        mono.t = 13.0
        assert feed(tracker, "total_energy_raw", 1001) == 1001
        assert tracker.dirty is True


class TestSessionBoundaryAndUnloadFlushPromptly:
    def test_session_boundary_marks_dirty_even_if_value_unchanged(self):
        tracker, mono, _w = tracker_at(mono=0.0, wall=0.0)
        data = {"current_energy_raw": 0, "max_power_raw": 73}
        tracker.apply(data, session_boundary=False)
        tracker.dirty = False
        mono.t = 13.0
        data = {"current_energy_raw": 0, "max_power_raw": 73}
        tracker.apply(data, session_boundary=True)   # inactive -> active
        assert data["current_energy_raw"] == 0
        assert tracker.dirty is True

    async def test_unload_flushes_unconditionally(
        self, hass, hass_storage, enable_custom_integrations, monkeypatch,
    ):
        fake = FakeController(status=3)
        fake.hw["total_energy_raw"] = 12344
        h = Harness(hass, make_entry(ENTRY), fake)
        assert await h.async_setup()
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]

        real_save = Store.async_save

        async def _failing(self, data):
            raise OSError("disk busy")

        # The per-poll save of the new baseline fails, leaving it unsaved...
        monkeypatch.setattr(Store, "async_save", _failing)
        fake.hw["total_energy_raw"] = 12345
        await coordinator.async_refresh()
        assert hass_storage[energy_key(ENTRY)]["data"]["total_energy_raw"]["raw"] == 12344
        # ...and unload must persist it regardless.
        monkeypatch.setattr(Store, "async_save", real_save)
        await h.async_unload()
        assert h.entry.state is ConfigEntryState.NOT_LOADED
        assert hass_storage[energy_key(ENTRY)]["data"]["total_energy_raw"]["raw"] == 12345
