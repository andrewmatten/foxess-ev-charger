"""Stateful energy sanitisation around energy_guard.py's pure decisions.

Ported from the 2.4.3 coordinator tests (``_sanitize_energy``) to the
rebuild's EnergyTracker, driven through its public ``apply`` with a manual
clock, and to the polling coordinator where the session-boundary signal is
derived. Covers:

1. The session-boundary cross-reference (the coordinator's own session
   tracking decides whether a decrease of current_energy_raw is a reset).
2. The first-observation absolute bound.
3. The rolling-window sustained-corruption check, its reset after a
   rejection and after a counter reset.
4. Rejections never becoming the new anchor.
5. A full quantised 30-minute trace at rated power is never rejected.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import ENERGY_QUANTUM_KWH
from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.energy import EnergyTracker

from fake_controller import FakeController

RATED_KW = 7.3  # A7300P1-E-B-WO single-phase max


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_tracker(clock: Clock | None = None) -> tuple[EnergyTracker, Clock]:
    clock = clock or Clock()
    # Wall and monotonic clocks move together, so restore()/export() map
    # timestamps 1:1 and a baseline can be seeded at an exact instant.
    return EnergyTracker(clock=clock, wall_clock=clock), clock


def seed(tracker: EnergyTracker, key: str, raw: int, at: float) -> None:
    """Last-known-good baseline recorded at ``at`` (as after a restart)."""
    tracker.restore({key: {"raw": raw, "wall_ts": at}})


def sanitize(tracker: EnergyTracker, key: str, raw: int, *, boundary: bool = False,
             max_power_raw: int = 73) -> int | None:
    """Feeds one reading; returns it if accepted, None if rejected."""
    # The rejection log is capped, so detect a new entry by identity.
    last = tracker.rejections[-1] if tracker.rejections else None
    data = {key: raw, "max_power_raw": max_power_raw}
    tracker.apply(data, session_boundary=boundary)
    if tracker.rejections and tracker.rejections[-1] is not last:
        # Rejected: the published value is the last good one (or absent).
        assert data.get(key) == tracker.last_good(key)
        return None
    assert data[key] == raw
    return raw


# ── 1. session-boundary cross-reference (through the coordinator) ─────────

async def _coordinator(hass, fake: FakeController, clock: Clock) -> FoxESSChargerCoordinator:
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    # Guard on a manual clock so the reset's plausibility window is explicit.
    coordinator.energy = EnergyTracker(clock=clock, wall_clock=clock)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    return coordinator


class TestSessionBoundaryCrossReference:
    """A decrease of current_energy_raw that is not near zero is trusted only
    when the coordinator's own session tracking saw an inactive -> active
    transition on this poll."""

    async def test_decrease_coinciding_with_a_real_session_start_is_accepted(self, hass):
        fake = FakeController(status=1)          # connected, inactive
        fake.hw["current_energy_raw"] = 530
        clock = Clock()
        coordinator = await _coordinator(hass, fake, clock)
        clock.t += 3 * 3600                      # a new session hours later
        fake.hw["status"] = 3                    # transitioning into charging
        fake.hw["current_energy_raw"] = 210      # decrease, not near zero
        await coordinator.async_refresh()
        assert coordinator.data["current_energy_raw"] == 210
        assert coordinator.energy.rejections == []

    async def test_decrease_without_a_session_transition_is_rejected(self, hass):
        fake = FakeController(status=3)          # already charging
        fake.hw["current_energy_raw"] = 530
        clock = Clock()
        coordinator = await _coordinator(hass, fake, clock)
        clock.t += 3 * 3600
        fake.hw["current_energy_raw"] = 210      # unexplained mid-session drop
        await coordinator.async_refresh()
        assert coordinator.data["current_energy_raw"] == 530
        assert len(coordinator.energy.rejections) == 1

    async def test_decrease_to_zero_is_still_accepted_without_a_confirmed_boundary(self, hass):
        fake = FakeController(status=3)
        fake.hw["current_energy_raw"] = 176
        clock = Clock()
        coordinator = await _coordinator(hass, fake, clock)
        clock.t += 13
        fake.hw["current_energy_raw"] = 0
        await coordinator.async_refresh()
        assert coordinator.data["current_energy_raw"] == 0
        assert coordinator.energy.rejections == []


# ── 2. first observation ──────────────────────────────────────────────────

class TestFirstObservationAbsoluteBound:
    def test_corrupt_first_reading_for_a_key_is_rejected(self):
        tracker, _ = make_tracker()
        raw = int(200_000 / ENERGY_QUANTUM_KWH)   # 200,000 kWh lifetime
        assert sanitize(tracker, "total_energy_raw", raw) is None
        assert tracker.rejections[-1]["last_good_kwh"] is None


# ── 3. rolling window ─────────────────────────────────────────────────────

class TestSustainedWindowCorruption:
    def test_real_intermittent_charging_never_trips_the_window(self):
        tracker, clock = make_tracker()
        raw = 1000
        for _ in range(8):
            assert sanitize(tracker, "total_energy_raw", raw) == raw
            raw += 1
            clock.t += 300.0   # one quantum per 5 minutes: well within 7.3 kW

    def test_sustained_one_quantum_per_poll_is_eventually_rejected(self):
        """~13 s polls, one quantum each (~27.7 kW sustained). Each delta
        passes on its own; the rolling window must catch the aggregate."""
        tracker, clock = make_tracker()
        raw, results = 1000, []
        for _ in range(200):
            results.append(sanitize(tracker, "total_energy_raw", raw))
            raw += 1
            clock.t += 13.0
        assert None in results

    def test_window_resets_after_a_rejection_so_it_can_recover(self):
        # The window's contents have no public view; asserting them directly
        # is the only way to pin "restarts empty after a rejection".
        tracker, clock = make_tracker()
        raw = 1000
        for _ in range(200):
            if sanitize(tracker, "total_energy_raw", raw) is None:
                break
            raw += 1
            clock.t += 13.0
        else:
            pytest.fail("window never tripped")
        assert tracker._window["total_energy_raw"] == []


class TestWindowStoresRawObservationsNotDeltas:
    def test_window_entries_are_timestamp_raw_pairs(self):
        # Internal representation, asserted directly (no public view).
        tracker, clock = make_tracker()
        assert sanitize(tracker, "total_energy_raw", 1000) == 1000
        clock.t = 13.0
        assert sanitize(tracker, "total_energy_raw", 1001) == 1001
        assert tracker._window["total_energy_raw"] == [(0.0, 1000), (13.0, 1001)]


class TestSessionBoundaryDoesNotPoisonTheWindow:
    """A counter reset's large negative delta must never sit in the rolling
    window offsetting a later corrupt read."""

    @staticmethod
    def _polls_until_window_trips(tracker: EnergyTracker, clock: Clock, start_raw: int) -> int:
        raw = start_raw
        for i in range(1, 400):
            clock.t += 13.0
            raw += 1
            if sanitize(tracker, "current_energy_raw", raw) is None:
                return i
        pytest.fail("window never tripped")

    def test_session_reset_clears_and_reseeds_the_window(self):
        """In-session history before a reset to zero must not influence the
        window afterwards: the post-reset corruption trips after exactly as
        many polls as on a tracker that started at the reset."""
        tracker, clock = make_tracker()
        for raw in (500, 501, 502, 503):
            assert sanitize(tracker, "current_energy_raw", raw) == raw
            clock.t += 300.0
        clock.t = 2000.0
        # Near-zero reset, no confirmed boundary: accepted on the weak signal.
        assert sanitize(tracker, "current_energy_raw", 0) == 0
        after_reset = self._polls_until_window_trips(tracker, clock, 0)

        fresh, fresh_clock = make_tracker(Clock(2000.0))
        assert sanitize(fresh, "current_energy_raw", 0) == 0
        assert after_reset == self._polls_until_window_trips(fresh, fresh_clock, 0)

    def test_reset_does_not_mask_a_later_corrupt_spike(self):
        tracker, clock = make_tracker()
        seed(tracker, "current_energy_raw", 530, at=0.0)
        assert sanitize(tracker, "current_energy_raw", 0) == 0
        clock.t = 15.0
        assert sanitize(tracker, "current_energy_raw", 1000) is None  # +100 kWh in 15 s
        assert tracker.rejections[-1]["rejected_kwh"] == 100.0


# ── 4. rejections never become the anchor ─────────────────────────────────

class TestRejectionNeverBecomesTheNewAnchor:
    def test_per_poll_rejection_leaves_last_energy_and_window_untouched(self):
        tracker, clock = make_tracker()
        assert sanitize(tracker, "total_energy_raw", 1000) == 1000
        clock.t = 13.0
        assert sanitize(tracker, "total_energy_raw", 65800) is None  # the 6580.0 kWh incident value
        assert tracker.last_good("total_energy_raw") == 1000
        # The window still holds only the t=0 sample: a plausible reading is
        # judged against it and accepted.
        clock.t = 300.0
        assert sanitize(tracker, "total_energy_raw", 1001) == 1001

    def test_two_consecutive_corrupt_reads_both_compare_against_the_same_anchor(self):
        tracker, clock = make_tracker()
        seed(tracker, "total_energy_raw", 1000, at=0.0)
        clock.t = 13.0
        assert sanitize(tracker, "total_energy_raw", 65800) is None
        clock.t = 26.0
        assert sanitize(tracker, "total_energy_raw", 65800) is None
        assert [r["last_good_raw"] for r in tracker.rejections] == [1000, 1000]
        assert tracker.last_good("total_energy_raw") == 1000


# ── 5. a legitimate 30-minute quantised trace ─────────────────────────────

class TestFullThirtyMinuteQuantisedTraceAtRatedPower:
    @pytest.mark.parametrize("phase_offset_kwh", [0.0, 0.025, 0.05, 0.075, 0.099])
    def test_every_legitimate_sample_over_30_minutes_is_accepted(self, phase_offset_kwh):
        tracker, clock = make_tracker()
        poll_interval_s = 10.0
        accumulated_kwh = phase_offset_kwh
        raw = int(accumulated_kwh / ENERGY_QUANTUM_KWH)
        assert sanitize(tracker, "total_energy_raw", raw) == raw
        for i in range(1, int(1800 / poll_interval_s) + 1):
            clock.t = i * poll_interval_s
            accumulated_kwh += RATED_KW * (poll_interval_s / 3600)
            new_raw = int(accumulated_kwh / ENERGY_QUANTUM_KWH)
            assert sanitize(tracker, "total_energy_raw", new_raw) == new_raw, (
                f"poll {i} (t={clock.t}s, phase_offset={phase_offset_kwh}) wrongly "
                f"rejected: {tracker.rejections[-1] if tracker.rejections else None}"
            )


class TestSustainedArtificialCorruptionThenRecovery:
    def test_exactly_one_quantum_every_10s_is_eventually_rejected(self):
        tracker, clock = make_tracker()
        raw = 1000
        assert sanitize(tracker, "total_energy_raw", raw) == raw
        results = []
        for i in range(1, 200):
            clock.t = i * 10.0
            raw += 1
            results.append(sanitize(tracker, "total_energy_raw", raw))
        assert None in results

    def test_recovers_once_the_corruption_stops(self):
        tracker, clock = make_tracker()
        raw = 1000
        assert sanitize(tracker, "total_energy_raw", raw) == raw
        i, rejected = 0, False
        while not rejected and i < 200:
            i += 1
            clock.t = i * 10.0
            raw += 1
            rejected = sanitize(tracker, "total_energy_raw", raw) is None
        assert rejected
        last_good_raw = tracker.last_good("total_energy_raw")
        for _ in range(5):
            i += 1
            clock.t = 2000 + i * 300
            last_good_raw += 1
            assert sanitize(tracker, "total_energy_raw", last_good_raw) == last_good_raw


# ── energy guard with a garbage device maximum ────────────────────────────
# (ported from test_realistic_charger::test_garbage_max_power_raw_does_not_disable_energy_guard)

def test_garbage_max_power_raw_does_not_disable_energy_guard():
    tracker, clock = make_tracker(Clock(60.0))
    seed(tracker, "total_energy_raw", 1000, at=0.0)
    # +5 kWh in 60 s = 300 kW, impossible on a 7.3 kW charger.
    assert sanitize(tracker, "total_energy_raw", 1050, max_power_raw=0xFFFF) is None
