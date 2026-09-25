"""Unit tests for energy_guard.decide_energy_reading.

Pure-function tests: decide_energy_reading() has zero imports from
homeassistant.* and touches no coordinator/hass state, so these need no HA
test harness or stubbing of any kind - just plain arguments in, a
EnergyGuardDecision out. See energy_guard.py's module docstring for the
incident history this guard exists to catch.

Superseded the pre-2.1.3 version of this file, which built a coordinator
via `object.__new__` to skip DataUpdateCoordinator.__init__ (no real HA test
infrastructure existed anywhere in this project at the time). That approach
is gone now that both a pure decision function and a real HA test harness
(see tests/conftest.py, requirements_test.txt) exist.

Run with: pytest tests/test_energy_guard.py -v
"""
from __future__ import annotations

from custom_components.foxess_charger.energy_guard import (
    ENERGY_ABS_MAX_KWH,
    ENERGY_GUARD_SAFETY_FACTOR,
    ENERGY_QUANTUM_KWH,
    ENERGY_WINDOW_SECONDS,
    check_cumulative_window,
    decide_energy_reading,
)

RATED_KW = 7.3  # A7300P1-E-B-WO single-phase max, matches const.py's floor


class TestNormalOperation:
    def test_single_quantum_step_after_normal_poll_interval(self):
        """The exact bug this patch fixes: a real 0.1 kWh step at a ~13s poll
        interval used to be rejected outright (old formula allowed ~0.0527
        kWh at this interval, less than one quantum)."""
        decision = decide_energy_reading(
            "total_energy_raw", raw=3786, prev=3785, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert decision.accepted
        assert decision.value == 3786

    def test_several_unchanged_polls_then_one_step(self):
        """Flat (delta=0) polls are accepted and DO advance the timestamp
        (agreed behaviour - the quantum floor makes this safe regardless of
        the real charging rate, since max_power_kw is a fixed worst-case
        bound, not the live draw)."""
        ts = 0.0
        for _ in range(3):
            decision = decide_energy_reading(
                "total_energy_raw", raw=3785, prev=3785, prev_ts=ts, now=ts + 13.0,
                max_power_kw=RATED_KW, allow_decrease=False,
            )
            assert decision.accepted
            assert decision.value == 3785
            ts += 13.0

        # Now the real step arrives, still only ~13s after the last poll.
        decision = decide_energy_reading(
            "total_energy_raw", raw=3786, prev=3785, prev_ts=ts, now=ts + 13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert decision.accepted
        assert decision.value == 3786

    def test_current_energy_reset_to_zero_between_sessions(self):
        """current_energy_raw explicitly allows decrease (session boundary)."""
        decision = decide_energy_reading(
            "current_energy_raw", raw=0, prev=176, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=True,
        )
        assert decision.accepted
        assert decision.value == 0

    def test_total_energy_decrease_is_rejected(self):
        """total_energy_raw is a lifetime counter - any decrease is implausible."""
        decision = decide_energy_reading(
            "total_energy_raw", raw=3785, prev=3786, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert not decision.accepted
        assert decision.value is None

    def test_first_ever_reading_for_a_key_is_always_accepted(self):
        """No last-known-good yet (prev/prev_ts both None) - nothing to
        compare against, so the first read is trusted outright."""
        decision = decide_energy_reading(
            "total_energy_raw", raw=999999, prev=None, prev_ts=None, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert decision.accepted
        assert decision.value == 999999


class TestAnomalyDetection:
    def test_the_original_incident_is_still_rejected(self):
        """The guard's actual original purpose: the two real 2026-08/09
        incidents that produced raw=65800 (6580.0 kWh) in a single poll.
        This must still fail hard under the new formula."""
        decision = decide_energy_reading(
            "total_energy_raw", raw=65800, prev=3785, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert not decision.accepted

    def test_large_multi_kwh_spike_at_longer_interval_still_rejected(self):
        """Even at a generously long elapsed time, a multi-kWh jump in one
        poll is not something 7.3 kW charging can produce and must fail."""
        # 100 kWh in 5 minutes implies 1200 kW - nowhere near plausible.
        decision = decide_energy_reading(
            "total_energy_raw", raw=3785 + 1000, prev=3785, prev_ts=0.0, now=300.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert not decision.accepted


class TestMissedPollNeverRejectsARealReading:
    """A 2-tick (0.2 kWh) jump within ~26s (one missed ~13s poll) is not a
    false-rejection risk: at the 7.3kW rated max, at most ~0.053kWh can be
    delivered in 26s, plus at most one pending quantum (<0.1kWh) already
    accumulated - so a genuinely real jump that size is physically capped
    below 2 raw units regardless of elapsed time. Rejecting it is correct
    anomaly detection, not a gap. SAFETY_FACTOR=1.5 (>1) guarantees the
    guard's allowance always exceeds this physical maximum, so no real
    reading can ever be rejected at any elapsed time - only genuinely
    implausible ones."""

    def test_two_tick_jump_after_one_missed_poll_is_correctly_rejected(self):
        decision = decide_energy_reading(
            "total_energy_raw", raw=3787, prev=3785, prev_ts=0.0, now=26.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert not decision.accepted, "a 0.2kWh jump in 26s at 7.3kW is physically impossible"

    def test_two_tick_jump_accepted_once_physically_achievable(self):
        """At 40s, up to ~0.081kWh + pending quantum could genuinely have
        accumulated - the formula accepts it, correctly."""
        decision = decide_energy_reading(
            "total_energy_raw", raw=3787, prev=3785, prev_ts=0.0, now=40.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert decision.accepted
        assert decision.value == 3787


def test_constants_match_agreed_values():
    """Pins the agreed-upon constants so an accidental edit elsewhere is
    caught here rather than silently changing guard behaviour."""
    assert ENERGY_QUANTUM_KWH == 0.1
    assert ENERGY_GUARD_SAFETY_FACTOR == 1.5


class TestFirstObservationSanityCheck:
    """2026-09 (second audit): a first-ever reading for a key used to be
    accepted outright with no plausibility check at all - a corrupt first
    read became the new trusted baseline forever. The absolute
    ENERGY_ABS_MAX_KWH backstop now applies even when prev is None."""

    def test_plausible_first_reading_is_still_accepted_outright(self):
        """Unchanged behaviour for the common case: no prior reading to
        compare a rate against, but well within the absolute bound."""
        decision = decide_energy_reading(
            "total_energy_raw", raw=999999, prev=None, prev_ts=None, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert decision.accepted
        assert decision.value == 999999

    def test_wildly_corrupt_first_reading_is_rejected(self):
        """A garbage Modbus response right at startup (e.g. way past what
        any home EV charger could ever have delivered lifetime) must not
        become the trusted baseline just because there's nothing yet to
        compare it against."""
        raw = int((ENERGY_ABS_MAX_KWH * 2) / ENERGY_QUANTUM_KWH)
        decision = decide_energy_reading(
            "total_energy_raw", raw=raw, prev=None, prev_ts=None, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert not decision.accepted
        assert decision.value is None

    def test_absolute_bound_also_applies_with_a_prior_reading(self):
        """Not just a first-observation special case - the same backstop
        catches an absurd value even when there IS a last-known-good to
        compare against (belt and braces alongside the rate-based check)."""
        raw = int((ENERGY_ABS_MAX_KWH * 2) / ENERGY_QUANTUM_KWH)
        decision = decide_energy_reading(
            "total_energy_raw", raw=raw, prev=3785, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=False,
        )
        assert not decision.accepted


class TestSessionBoundaryDecrease:
    """2026-09 (second audit): current_energy_raw's allow_decrease=True used
    to accept ANY decrease unconditionally. A decrease is now only accepted
    when it actually looks like a session-boundary reset."""

    def test_decrease_to_zero_without_confirmed_boundary_is_still_accepted(self):
        """The weak signal alone (new value at/near zero) is enough - this
        is the existing/common case and must keep working unchanged."""
        decision = decide_energy_reading(
            "current_energy_raw", raw=0, prev=176, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=True, session_boundary=False,
        )
        assert decision.accepted
        assert decision.value == 0

    def test_decrease_confirmed_by_coordinator_session_state_is_accepted(self):
        """The strong signal: the coordinator's own session-tracking state
        confirms a real boundary occurred, even though the new value isn't
        itself at/near zero (e.g. the session started mid-count from
        whatever total_energy happened to read)."""
        decision = decide_energy_reading(
            "current_energy_raw", raw=2, prev=530, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=True, session_boundary=True,
        )
        assert decision.accepted
        assert decision.value == 2

    def test_implausible_nonzero_boundary_value_is_rejected(self):
        decision = decide_energy_reading(
            "current_energy_raw", raw=210, prev=530, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=True, session_boundary=True,
        )
        assert not decision.accepted

    def test_unexplained_mid_session_decrease_is_rejected(self):
        """5.3kWh -> 2.1kWh with no session boundary and not near zero -
        must be rejected like any other implausible reading, not waved
        through just because this counter is allowed to reset sometimes."""
        decision = decide_energy_reading(
            "current_energy_raw", raw=21, prev=53, prev_ts=0.0, now=13.0,
            max_power_kw=RATED_KW, allow_decrease=True, session_boundary=False,
        )
        assert not decision.accepted
        assert decision.value is None


class TestCumulativeWindowCheck:
    """2026-09 (second audit): a sustained corruption of exactly one
    register quantum per poll passes decide_energy_reading() every single
    time (the per-poll floor unconditionally allows one quantum), but sums
    to a physically impossible rate over a longer window."""

    def test_real_charging_at_rated_power_over_the_window_is_plausible(self):
        # 7.3kW for the full 1800s window = 3.65 kWh - well within bound.
        window_kwh = RATED_KW * (ENERGY_WINDOW_SECONDS / 3600)
        assert not check_cumulative_window(window_kwh, ENERGY_WINDOW_SECONDS, RATED_KW)

    def test_sustained_one_quantum_per_poll_corruption_is_rejected(self):
        """~13s polls, one 0.1kWh quantum accepted every single time, for
        the full window - each individual delta is fine on its own, but the
        implied sustained rate (~27.7kW) is nearly 4x this hardware's rated
        7.3kW max."""
        poll_interval_s = 13.0
        num_polls = int(ENERGY_WINDOW_SECONDS / poll_interval_s)
        total_kwh = num_polls * ENERGY_QUANTUM_KWH
        window_elapsed = num_polls * poll_interval_s
        assert check_cumulative_window(total_kwh, window_elapsed, RATED_KW)

    def test_zero_elapsed_window_is_never_flagged(self):
        """Guards the division - a not-yet-populated window must not be
        treated as an instant infinite rate."""
        assert not check_cumulative_window(0.1, 0.0, RATED_KW)


class TestCumulativeWindowQuantisationFloor:
    def test_two_genuine_quantum_ticks_in_55s_are_not_flagged(self):
        """The exact bug this task fixes: two real 0.1kWh register ticks
        landing within ~55s (5 samples at ~13-14s apart) at 7.3kW must never
        be flagged, even though the old formula (no quantum floor) rejected
        this. 0.2 kWh over 55s implies ~13kW - impossible at this hardware's
        7.3kW rating - which is exactly why the guard needs the same
        one-quantum floor the per-poll check already has: one physically
        real register tick can land in any single poll no matter how short
        the window, independent of rate."""
        assert not check_cumulative_window(0.2, 55.0, RATED_KW)

    def test_old_formula_would_have_rejected_this_same_case(self):
        """Documents the bug being fixed - the un-floored formula rejects
        the exact case above."""
        old_formula_max = RATED_KW * (55.0 / 3600) * ENERGY_GUARD_SAFETY_FACTOR
        assert 0.2 > old_formula_max

    def test_sustained_corruption_is_still_rejected_with_the_floor_added(self):
        """The floor is a one-time +0.1kWh addition - at the 1800s window
        scale, sustained one-quantum-per-poll corruption (~27.7kW implied,
        see the existing TestCumulativeWindowCheck case) still exceeds the
        floored threshold by a wide margin."""
        poll_interval_s = 13.0
        num_polls = int(ENERGY_WINDOW_SECONDS / poll_interval_s)
        total_kwh = num_polls * ENERGY_QUANTUM_KWH
        window_elapsed = num_polls * poll_interval_s
        assert check_cumulative_window(total_kwh, window_elapsed, RATED_KW)
