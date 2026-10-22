"""Hardware counter reset recovery for the lifetime energy counter (3.0.7).

Live incident: on 14 Oct 2026 the charger's ``total_energy_raw`` went from
8013 (801.3 kWh) to 1427 after a reboot and stayed there. The guard rejected
every poll forever (one WARNING per ~13 s, sensor frozen at 801.3 kWh, session
energy 0). A lower value seen consistently is now accepted as a counter reset
with a persisted offset; isolated low reads are still rejected.
"""
from __future__ import annotations

import logging

import pytest

from custom_components.foxess_charger.const import (
    ENERGY_REJECT_WARN_INTERVAL_S, ENERGY_RESET_MIN_POLLS, ENERGY_RESET_MIN_SECONDS,
)
from custom_components.foxess_charger.energy import EnergyTracker
from custom_components.foxess_charger.persistence import ChargerStorage, energy_key
from custom_components.foxess_charger.session import SessionTracker

KEY = "total_energy_raw"
POLL = 13.0
WALL0 = 1_790_000_000.0
ENTRY = "energyresetentry"


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def tracker_with_baseline(raw: int = 8013, *, age: float = 60.0):
    clock = Clock(WALL0)
    tracker = EnergyTracker(clock=clock, wall_clock=clock)
    tracker.restore({KEY: {"raw": raw, "wall_ts": WALL0 - age}})
    return tracker, clock


def poll(tracker: EnergyTracker, clock: Clock, raw: int, *, dt: float = POLL, **extra) -> dict:
    clock.t += dt
    data = {KEY: raw, "max_power_raw": 73, **extra}
    tracker.apply(data, session_boundary=False)
    return data


def feed_low(tracker, clock, raw=1427, seconds=ENERGY_RESET_MIN_SECONDS + 60) -> list[int]:
    out = []
    end = clock.t + seconds
    while clock.t < end:
        out.append(poll(tracker, clock, raw)[KEY])
    return out


class TestLiveScenarioRecoversByItself:
    def test_stored_8013_then_repeated_1427_is_accepted_with_offset(self):
        tracker, clock = tracker_with_baseline(8013)
        exposed = feed_low(tracker, clock)
        # Held at the last good value through the window, never going backwards.
        assert min(exposed) == 8013 and exposed[0] == 8013
        assert exposed == sorted(exposed)
        # After the window the sensor carries on from 801.3 (not 142.7).
        assert exposed[-1] == 8013
        assert tracker.last_good(KEY) == 8013
        assert tracker.export()[KEY]["offset"] == 8013 - 1427

    def test_counting_up_from_the_new_base_continues_from_the_old_total(self):
        tracker, clock = tracker_with_baseline(8013)
        feed_low(tracker, clock, 1427)
        assert poll(tracker, clock, 1437, dt=600)[KEY] == 8023   # 802.3 kWh
        assert poll(tracker, clock, 1437)[KEY] == 8023

    def test_exposed_value_never_drops_while_the_reset_is_being_confirmed(self):
        tracker, clock = tracker_with_baseline(8013)
        seen = []
        for i in range(ENERGY_RESET_MIN_POLLS * 4):
            seen.append(poll(tracker, clock, 1427 + (i // 40))[KEY])
        assert all(v >= 8013 for v in seen)
        assert seen == sorted(seen)

    def test_reset_is_accepted_only_after_both_polls_and_time(self):
        tracker, clock = tracker_with_baseline(8013)
        # Many polls but too short a time (fast polling) -> not yet.
        for _ in range(ENERGY_RESET_MIN_POLLS + 5):
            poll(tracker, clock, 1427, dt=1.0)
        assert "offset" not in tracker.export()[KEY]
        # Enough time but too few polls (very slow polling) -> not yet.
        tracker, clock = tracker_with_baseline(8013)
        poll(tracker, clock, 1427, dt=1.0)
        poll(tracker, clock, 1427, dt=ENERGY_RESET_MIN_SECONDS + 1)
        assert "offset" not in tracker.export()[KEY]

    def test_exactly_one_acceptance_warning(self, caplog):
        tracker, clock = tracker_with_baseline(8013)
        with caplog.at_level(logging.WARNING):
            feed_low(tracker, clock, seconds=ENERGY_RESET_MIN_SECONDS * 3)
        accepted = [r for r in caplog.records if "counter reset accepted" in r.getMessage()]
        assert len(accepted) == 1

    def test_acceptance_marks_baseline_dirty_so_the_offset_is_saved(self):
        tracker, clock = tracker_with_baseline(8013)
        tracker.dirty = False
        feed_low(tracker, clock)
        assert tracker.dirty is True


class TestIsolatedLowReadsStillRejected:
    def test_single_glitch_is_rejected_and_normal_counting_continues(self):
        tracker, clock = tracker_with_baseline(8013)
        assert poll(tracker, clock, 8013)[KEY] == 8013
        assert poll(tracker, clock, 1427)[KEY] == 8013
        assert len(tracker.rejections) == 1
        assert poll(tracker, clock, 8014)[KEY] == 8014
        assert "offset" not in tracker.export()[KEY]

    def test_scattered_low_reads_never_accumulate_into_a_reset(self):
        tracker, clock = tracker_with_baseline(8013)
        for _ in range(ENERGY_RESET_MIN_POLLS * 3):
            assert poll(tracker, clock, 1427)[KEY] == 8013 or False   # low
            assert poll(tracker, clock, 8013)[KEY] == 8013            # normal read clears it
        assert "offset" not in tracker.export()[KEY]

    def test_low_values_that_wander_downward_do_not_count_as_consistent(self):
        tracker, clock = tracker_with_baseline(8013)
        raw = 3000
        end = clock.t + ENERGY_RESET_MIN_SECONDS * 2
        while clock.t < end:
            assert poll(tracker, clock, raw)[KEY] == 8013
            raw = max(raw - 7, 100)      # falling every poll: not a clean new base
        assert "offset" not in tracker.export()[KEY]

    def test_low_values_with_an_implausible_rise_restart_the_window(self):
        tracker, clock = tracker_with_baseline(8013)
        feed_low(tracker, clock, 1427, seconds=ENERGY_RESET_MIN_SECONDS - 100)
        poll(tracker, clock, 5000)       # 357 kWh in 13 s: not plausible counting
        feed_low(tracker, clock, 5000, seconds=ENERGY_RESET_MIN_SECONDS - 100)
        assert "offset" not in tracker.export()[KEY]

    def test_decrease_of_current_energy_counter_is_unaffected(self):
        """allow_decrease counters keep their session-boundary rules."""
        clock = Clock(WALL0)
        tracker = EnergyTracker(clock=clock, wall_clock=clock)
        tracker.restore({"current_energy_raw": {"raw": 530, "wall_ts": WALL0 - 60}})
        for _ in range(ENERGY_RESET_MIN_POLLS * 2):
            clock.t += ENERGY_RESET_MIN_SECONDS
            data = {"current_energy_raw": 210, "max_power_raw": 73}
            tracker.apply(data, session_boundary=False)
            assert data["current_energy_raw"] == 530


class TestGuardsStillWorkOnOffsetAdjustedValues:
    def _after_reset(self):
        tracker, clock = tracker_with_baseline(8013)
        feed_low(tracker, clock, 1427)
        assert tracker.export()[KEY]["offset"] == 6586
        return tracker, clock

    def test_implausible_jump_is_rejected_on_the_adjusted_value(self):
        tracker, clock = self._after_reset()
        assert poll(tracker, clock, 1427 + 5000)[KEY] == 8013      # 500 kWh in 13 s
        assert len(tracker.rejections) >= 1
        assert tracker.rejections[-1]["rejected_raw"] == 8013 + 5000

    def test_a_second_low_glitch_after_the_reset_is_rejected(self):
        tracker, clock = self._after_reset()
        assert poll(tracker, clock, 1437, dt=600)[KEY] == 8023
        assert poll(tracker, clock, 10)[KEY] == 8023

    def test_a_second_genuine_reset_stacks_on_the_first_offset(self):
        tracker, clock = self._after_reset()
        assert poll(tracker, clock, 1437, dt=600)[KEY] == 8023
        feed_low(tracker, clock, 20)
        assert poll(tracker, clock, 30, dt=600)[KEY] == 8023 + 10
        assert tracker.export()[KEY]["offset"] == 8023 - 20


class TestRejectionWarningRateLimit:
    def test_one_warning_per_key_per_hour_but_every_rejection_is_recorded(self, caplog):
        tracker, clock = tracker_with_baseline(8013)
        with caplog.at_level(logging.DEBUG):
            for _ in range(5):
                poll(tracker, clock, 100_000_000)       # corrupt: never a reset
            warnings = [r for r in caplog.records
                        if r.levelno == logging.WARNING and "Rejected implausible" in r.getMessage()]
            assert len(warnings) == 1
            debug = [r for r in caplog.records
                     if r.levelno == logging.DEBUG and "Rejected implausible" in r.getMessage()]
            assert len(debug) == 4
            assert len(tracker.rejections) == 5
            # After the interval the next rejection warns again.
            clock.t += ENERGY_REJECT_WARN_INTERVAL_S
            poll(tracker, clock, 100_000_000)
            warnings = [r for r in caplog.records
                        if r.levelno == logging.WARNING and "Rejected implausible" in r.getMessage()]
            assert len(warnings) == 2

    def test_limit_is_per_key(self, caplog):
        clock = Clock(WALL0)
        tracker = EnergyTracker(clock=clock, wall_clock=clock)
        tracker.restore({KEY: {"raw": 8013, "wall_ts": WALL0 - 60},
                         "current_energy_raw": {"raw": 100, "wall_ts": WALL0 - 60}})
        with caplog.at_level(logging.WARNING):
            clock.t += POLL
            tracker.apply({KEY: 100_000_000, "current_energy_raw": 100_000_000,
                           "max_power_raw": 73}, session_boundary=False)
        warnings = [r for r in caplog.records if "Rejected implausible" in r.getMessage()]
        assert len(warnings) == 2


class TestOffsetPersistence:
    def test_export_restore_round_trip_keeps_the_offset(self):
        tracker, clock = tracker_with_baseline(8013)
        feed_low(tracker, clock)
        stored = tracker.export()
        assert stored[KEY]["offset"] == 6586

        reborn_clock = Clock(WALL0 + 10_000)
        reborn = EnergyTracker(clock=reborn_clock, wall_clock=reborn_clock)
        reborn.restore(stored)
        # Charger still reports the raw 1427-based count: continues from 801.3.
        reborn_clock.t += POLL
        data = {KEY: 1431, "max_power_raw": 73}
        reborn.apply(data, session_boundary=False)
        assert data[KEY] == 8017
        assert reborn.rejections == []

    def test_export_without_an_offset_keeps_the_legacy_schema(self):
        tracker, clock = tracker_with_baseline(8013)
        poll(tracker, clock, 8013)
        assert set(tracker.export()[KEY]) == {"raw", "wall_ts"}

    def test_stored_baseline_without_offset_restores_as_zero(self):
        tracker, clock = tracker_with_baseline(8013)
        assert poll(tracker, clock, 8014)[KEY] == 8014

    @staticmethod
    async def _load(hass, hass_storage, stored) -> dict:
        hass_storage[energy_key(ENTRY)] = {
            "version": 1, "minor_version": 1, "key": energy_key(ENTRY), "data": stored,
        }
        return (await ChargerStorage(hass, ENTRY).async_load()).energy

    async def test_storage_keeps_a_valid_offset(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage,
                                  {KEY: {"raw": 8013, "wall_ts": WALL0, "offset": 6586}})
        assert energy[KEY] == {"raw": 8013, "wall_ts": WALL0, "offset": 6586}

    async def test_storage_without_offset_is_unchanged(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage, {KEY: {"raw": 8013, "wall_ts": WALL0}})
        assert energy[KEY] == {"raw": 8013, "wall_ts": WALL0}

    async def test_storage_discards_a_malformed_offset(self, hass, hass_storage):
        energy = await self._load(hass, hass_storage,
                                  {KEY: {"raw": 8013, "wall_ts": WALL0, "offset": "lots"}})
        assert KEY not in energy


class TestSessionEnergyAfterReset:
    """Sessions read the offset-adjusted total (EnergyTracker.apply rewrites
    data['total_energy_raw'] before SessionTracker.update runs), so a session
    that spans or follows a reset reports the real energy."""

    @staticmethod
    def _run(tracker, clock, sessions, raw_total, raw_current, status, power=0):
        clock.t += 60
        data = {KEY: raw_total, "current_energy_raw": raw_current, "status": status,
                "status_valid": True, "power_raw": power, "max_power_raw": 73}
        tracker.apply(data, session_boundary=sessions.is_boundary(data))
        sessions.update(data, intent_enabled=True)
        return data

    def test_session_after_a_reset_reports_the_energy_delivered(self):
        tracker, clock = tracker_with_baseline(8013)
        feed_low(tracker, clock, 1427)
        done: list[dict] = []
        sessions = SessionTracker(on_complete=done.append, clock=clock, wall_clock=clock)
        self._run(tracker, clock, sessions, 1427, 0, status=1)
        self._run(tracker, clock, sessions, 1427, 0, status=3, power=70)
        for i in range(1, 16):          # 1.5 kWh in 15 minutes
            self._run(tracker, clock, sessions, 1427 + i, i, status=3, power=60)
        self._run(tracker, clock, sessions, 1442, 15, status=1)
        assert len(done) == 1
        assert done[0]["energy_kwh"] == pytest.approx(1.5)

    def test_session_during_confirmation_falls_back_to_the_session_counter(self):
        """Total is held at 801.3 while the reset is being confirmed (or if the
        register is stuck after it): the per-session counter is used, not 0."""
        tracker, clock = tracker_with_baseline(8013)
        done: list[dict] = []
        sessions = SessionTracker(on_complete=done.append, clock=clock, wall_clock=clock)
        self._run(tracker, clock, sessions, 1427, 0, status=1)
        self._run(tracker, clock, sessions, 1427, 0, status=3, power=70)
        for i in range(1, 11):          # 2.0 kWh in 10 minutes
            self._run(tracker, clock, sessions, 1427, i * 2, status=3, power=60)
        self._run(tracker, clock, sessions, 1427, 20, status=1)
        assert len(done) == 1
        assert done[0]["energy_kwh"] == pytest.approx(2.0)
