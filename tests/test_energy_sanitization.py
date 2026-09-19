"""Coordinator-level tests for the 2026-09 (second audit) energy
sanitization gaps: the stateful wiring around energy_guard.py's pure
functions, which tests/test_energy_guard.py itself can't exercise (it never
touches FoxESSChargerCoordinator at all).

Covers:
1. The coordinator's own session-tracking state (_prev_status vs. the
   current poll's status) cross-referenced into decide_energy_reading()'s
   session_boundary param - see FoxESSChargerCoordinator._sanitize_energy.
2. The rolling-window sustained-corruption check (check_cumulative_window),
   wired via self._energy_window.

Uses _sanitize_energy() directly (not a full _fetch()) - it's the coordinator
method these gaps actually live in, and calling it directly avoids having to
fabricate a full register block for every poll.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import ENERGY_QUANTUM_KWH

RATED_KW = 7.3  # A7300P1-E-B-WO single-phase max


def make_coordinator(hass) -> FoxESSChargerCoordinator:
    return FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)


class TestSessionBoundaryCrossReference:
    """current_energy_raw's allow_decrease no longer accepts any decrease
    unconditionally - a decrease is trusted when the coordinator's own
    _prev_status -> data["status"] transition confirms a real session
    boundary just occurred, even if the new value isn't itself near zero."""

    def test_decrease_coinciding_with_a_real_session_start_is_accepted(self, hass):
        coordinator = make_coordinator(hass)
        coordinator._prev_status = 1  # connected (inactive)
        coordinator._last_energy["current_energy_raw"] = (530, 0.0)

        result = coordinator._sanitize_energy(
            "current_energy_raw", 210,  # decrease, not near zero
            {"status": 3, "max_power_raw": 73},  # transitioning into charging
        )

        assert result == 210

    def test_decrease_without_a_session_transition_is_rejected(self, hass):
        coordinator = make_coordinator(hass)
        coordinator._prev_status = 3  # already charging - no transition
        coordinator._last_energy["current_energy_raw"] = (530, 0.0)

        result = coordinator._sanitize_energy(
            "current_energy_raw", 210,  # unexplained mid-session drop
            {"status": 3, "max_power_raw": 73},  # still charging, same status
        )

        assert result is None
        assert len(coordinator.energy_rejections) == 1

    def test_decrease_to_zero_is_still_accepted_without_a_confirmed_boundary(self, hass):
        """The weak signal (near-zero) alone remains sufficient - unchanged
        common-case behaviour."""
        coordinator = make_coordinator(hass)
        coordinator._prev_status = 3  # no transition detected
        coordinator._last_energy["current_energy_raw"] = (176, 0.0)

        result = coordinator._sanitize_energy(
            "current_energy_raw", 0, {"status": 3, "max_power_raw": 73},
        )

        assert result == 0


class TestFirstObservationAbsoluteBound:
    def test_corrupt_first_reading_for_a_key_is_rejected(self, hass):
        coordinator = make_coordinator(hass)
        # 200,000 kWh - no home EV charger has ever delivered this lifetime.
        raw = int(200_000 / ENERGY_QUANTUM_KWH)

        result = coordinator._sanitize_energy(
            "total_energy_raw", raw, {"status": 1, "max_power_raw": 73},
        )

        assert result is None
        assert coordinator.energy_rejections[-1]["last_good_kwh"] is None


class TestSustainedWindowCorruption:
    """A sustained one-register-quantum-per-poll corruption passes the
    per-poll decision every single time (the per-poll floor unconditionally
    allows one quantum), but the coordinator's rolling window must catch the
    implied sustained rate once enough samples have accumulated."""

    def test_real_intermittent_charging_never_trips_the_window(self, hass, monkeypatch):
        """Realistic pattern: several genuine, well-spaced quantum steps at
        a rate the charger could actually sustain - must never be flagged."""
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        raw = 1000
        for _ in range(8):
            result = coordinator._sanitize_energy(
                "total_energy_raw", raw, {"status": 3, "max_power_raw": 73},
            )
            assert result == raw
            raw += 1        # one quantum
            mono_ts[0] += 300.0  # 5 minutes apart - well within rated 7.3kW capability

    def test_sustained_one_quantum_per_poll_is_eventually_rejected(self, hass, monkeypatch):
        """~13s polls, one quantum accepted every single time - the implied
        sustained rate (~27.7kW) is nearly 4x this hardware's rated 7.3kW
        max. Each individual delta passes on its own; the rolling window
        must catch the aggregate."""
        coordinator = make_coordinator(hass)
        raw = 1000
        mono_ts = [0.0]

        def fake_monotonic():
            return mono_ts[0]

        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", fake_monotonic,
        )

        results = []
        for _ in range(200):
            result = coordinator._sanitize_energy(
                "total_energy_raw", raw, {"status": 3, "max_power_raw": 73},
            )
            results.append(result)
            raw += 1
            mono_ts[0] += 13.0

        # Every individual delta is one quantum - the per-poll check alone
        # would accept every single one. The rolling window must have
        # rejected at least one poll once it accumulated enough samples.
        assert None in results

    def test_window_resets_after_a_rejection_so_it_can_recover(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        raw = 1000
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )

        rejected_at = None
        for i in range(200):
            result = coordinator._sanitize_energy(
                "total_energy_raw", raw, {"status": 3, "max_power_raw": 73},
            )
            if result is None and rejected_at is None:
                rejected_at = i
                assert coordinator._energy_window["total_energy_raw"] == []
                break
            raw += 1
            mono_ts[0] += 13.0

        assert rejected_at is not None


class TestSessionBoundaryDoesNotPoisonTheWindow:
    """Bug B: a session-boundary reset's large negative delta must never
    sit in the rolling window offsetting a later corrupt read."""

    def test_session_reset_clears_and_reseeds_the_window(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._prev_status = 3  # already charging
        coordinator._last_energy["current_energy_raw"] = (530, 0.0)
        # Pre-populate the window with some in-session history.
        coordinator._energy_window["current_energy_raw"] = [
            (0.0, 500), (10.0, 510), (20.0, 530),
        ]
        mono_ts[0] = 30.0

        result = coordinator._sanitize_energy(
            "current_energy_raw", 0,  # reset to zero
            {"status": 3, "max_power_raw": 73},
        )
        # session_boundary is only True on an inactive->active transition;
        # _prev_status=3 (already active) means this is NOT a coordinator-
        # confirmed boundary, only the weak near-zero signal - decide_energy_reading
        # still accepts it (near-zero decrease is always accepted), and the
        # window must still be cleared/reseeded because the *result* looks
        # like a reset regardless of which signal justified accepting it.
        assert result == 0
        assert coordinator._energy_window["current_energy_raw"] == [(30.0, 0)]

    def test_reset_does_not_mask_a_later_corrupt_spike(self, hass, monkeypatch):
        """Before this fix: the reset's -53.0kWh delta would sit in the
        window; a +40kWh corrupt spike shortly after would sum to -13kWh,
        never tripping check_cumulative_window (which only checks
        total > max_plausible, never negative sums). After this fix, the
        window was reseeded at the reset, so the spike is judged on its own
        merits by decide_energy_reading's per-poll check and rejected
        outright (100kWh in one poll is nowhere near plausible)."""
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._prev_status = 3
        coordinator._last_energy["current_energy_raw"] = (530, 0.0)
        coordinator._sanitize_energy(
            "current_energy_raw", 0, {"status": 3, "max_power_raw": 73},
        )
        mono_ts[0] = 15.0
        result = coordinator._sanitize_energy(
            "current_energy_raw", 1000,  # +100kWh in 15s - not plausible
            {"status": 3, "max_power_raw": 73},
        )
        assert result is None
        assert coordinator.energy_rejections[-1]["rejected_kwh"] == 100.0


class TestWindowStoresRawObservationsNotDeltas:
    def test_window_entries_are_timestamp_raw_pairs(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._sanitize_energy(
            "total_energy_raw", 1000, {"status": 3, "max_power_raw": 73},
        )
        mono_ts[0] = 13.0
        coordinator._sanitize_energy(
            "total_energy_raw", 1001, {"status": 3, "max_power_raw": 73},
        )
        assert coordinator._energy_window["total_energy_raw"] == [(0.0, 1000), (13.0, 1001)]


class TestRejectionNeverBecomesTheNewAnchor:
    def test_per_poll_rejection_leaves_last_energy_and_window_untouched(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._last_energy["total_energy_raw"] = (1000, 0.0)
        coordinator._energy_window["total_energy_raw"] = [(0.0, 1000)]
        mono_ts[0] = 13.0

        result = coordinator._sanitize_energy(
            "total_energy_raw", 65800,  # the real 6580.0kWh incident value
            {"status": 3, "max_power_raw": 73},
        )
        assert result is None
        assert coordinator._last_energy["total_energy_raw"] == (1000, 0.0)
        assert coordinator._energy_window["total_energy_raw"] == [(0.0, 1000)]

    def test_two_consecutive_corrupt_reads_both_compare_against_the_same_anchor(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        coordinator._last_energy["total_energy_raw"] = (1000, 0.0)
        mono_ts[0] = 13.0
        r1 = coordinator._sanitize_energy("total_energy_raw", 65800, {"status": 3, "max_power_raw": 73})
        mono_ts[0] = 26.0
        r2 = coordinator._sanitize_energy("total_energy_raw", 65800, {"status": 3, "max_power_raw": 73})
        assert r1 is None and r2 is None
        assert coordinator._last_energy["total_energy_raw"] == (1000, 0.0)


class TestFullThirtyMinuteQuantisedTraceAtRatedPower:
    """A real continuous 7.3kW charge, simulated as a continuous energy
    accumulator polled every 10s, register reporting floor(accumulated /
    quantum). Every legitimate sample must be accepted regardless of what
    fraction of a quantum was already banked when polling started - the
    accumulator's starting phase inside the quantum shouldn't matter."""

    @pytest.mark.parametrize("phase_offset_kwh", [0.0, 0.025, 0.05, 0.075, 0.099])
    def test_every_legitimate_sample_over_30_minutes_is_accepted(self, hass, monkeypatch, phase_offset_kwh):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        rated_kw = 7.3
        poll_interval_s = 10.0
        accumulated_kwh = phase_offset_kwh
        raw = int(accumulated_kwh / ENERGY_QUANTUM_KWH)
        coordinator._last_energy["total_energy_raw"] = (raw, 0.0)
        coordinator._energy_window["total_energy_raw"] = [(0.0, raw)]

        num_polls = int(1800 / poll_interval_s)
        for i in range(1, num_polls + 1):
            mono_ts[0] = i * poll_interval_s
            accumulated_kwh += rated_kw * (poll_interval_s / 3600)
            new_raw = int(accumulated_kwh / ENERGY_QUANTUM_KWH)
            result = coordinator._sanitize_energy(
                "total_energy_raw", new_raw, {"status": 3, "max_power_raw": 73},
            )
            assert result == new_raw, (
                f"poll {i} (t={mono_ts[0]}s, phase_offset={phase_offset_kwh}) "
                f"was wrongly rejected: {coordinator.energy_rejections[-1] if coordinator.energy_rejections else 'no rejection'}"
            )


class TestSustainedArtificialCorruptionThenRecovery:
    def test_exactly_one_quantum_every_10s_is_eventually_rejected(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        raw = 1000
        coordinator._last_energy["total_energy_raw"] = (raw, 0.0)
        coordinator._energy_window["total_energy_raw"] = [(0.0, raw)]
        results = []
        for i in range(1, 200):
            mono_ts[0] = i * 10.0
            raw += 1
            results.append(coordinator._sanitize_energy(
                "total_energy_raw", raw, {"status": 3, "max_power_raw": 73},
            ))
        assert None in results

    def test_recovers_once_the_corruption_stops(self, hass, monkeypatch):
        coordinator = make_coordinator(hass)
        mono_ts = [0.0]
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: mono_ts[0],
        )
        raw = 1000
        coordinator._last_energy["total_energy_raw"] = (raw, 0.0)
        coordinator._energy_window["total_energy_raw"] = [(0.0, raw)]
        i = 0
        rejected = False
        while not rejected and i < 200:
            i += 1
            mono_ts[0] = i * 10.0
            raw += 1
            if coordinator._sanitize_energy(
                "total_energy_raw", raw, {"status": 3, "max_power_raw": 73},
            ) is None:
                rejected = True
        assert rejected

        # Corruption stops - space out real, plausible ticks from here on.
        last_good_raw = coordinator._last_energy["total_energy_raw"][0]
        for _ in range(5):
            i += 1
            # Space readings 300+ seconds apart so they're physically plausible
            # and don't re-trigger the cumulative window check
            mono_ts[0] = 2000 + i * 300
            last_good_raw += 1
            result = coordinator._sanitize_energy(
                "total_energy_raw", last_good_raw, {"status": 3, "max_power_raw": 73},
            )
            assert result == last_good_raw
