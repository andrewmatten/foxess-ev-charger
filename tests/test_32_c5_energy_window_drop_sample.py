"""C5: Rolling-window rejection in EnergyTracker drops only the rejected sample,
preventing the next 4 corrupt +0.1 kWh ticks from leaking into statistics."""
from __future__ import annotations

from test_energy_sanitization import make_tracker, sanitize


def test_rolling_window_rejection_blocks_subsequent_corrupt_ticks():
    tracker, clock = make_tracker()
    # Accept initial baseline plus three +0.1 kWh ticks every 10 s (4 samples).
    for step in range(4):
        clock.t = step * 10.0
        assert sanitize(tracker, "total_energy_raw", 1000 + step) == 1000 + step

    # Sample 5 (+0.4 kWh in 40 s on a 7.3 kW unit) trips the cumulative window.
    clock.t = 40.0
    assert sanitize(tracker, "total_energy_raw", 1004) is None
    assert tracker.last_good("total_energy_raw") == 1003

    # Old code wiped _window to [], letting 4 more +0.1 kWh corrupt ticks in.
    # Keeping the accepted window history rejects every subsequent fast tick.
    for extra in range(1, 5):
        clock.t = 40.0 + extra * 10.0
        candidate = tracker.last_good("total_energy_raw") + 1
        assert sanitize(tracker, "total_energy_raw", candidate) is None
        assert tracker.last_good("total_energy_raw") == 1003


def test_rolling_window_recovers_once_elapsed_time_makes_gain_plausible():
    tracker, clock = make_tracker()
    for step in range(4):
        clock.t = step * 10.0
        assert sanitize(tracker, "total_energy_raw", 1000 + step) == 1000 + step

    clock.t = 40.0
    assert sanitize(tracker, "total_energy_raw", 1004) is None

    # At t=120s, +0.4 kWh over 120s is <= 0.1 + 7.3*(120/3600)*1.5 = 0.465 kWh.
    clock.t = 120.0
    assert sanitize(tracker, "total_energy_raw", 1004) == 1004
