"""3.1.3 energy-counter regressions: a lone near-zero glitch mid-session is
not a counter reset, and an all-ones filler is never a first baseline."""
from __future__ import annotations

from custom_components.foxess_charger.energy import EnergyTracker

CUR = "current_energy_raw"
TOT = "total_energy_raw"
POLL = 13.0


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(key: str = CUR, raw: int = 53):
    clock = Clock()
    tracker = EnergyTracker(clock=clock, wall_clock=clock)
    tracker.restore({key: {"raw": raw, "wall_ts": clock.t - POLL}})
    return tracker, clock


def poll(tracker, clock, raw, key: str = CUR, **kw):
    clock.t += POLL
    data = {key: raw, "max_power_raw": 73}
    tracker.apply(data, session_boundary=kw.get("boundary", False))
    return data.get(key)


class TestSingleNearZeroGlitch:
    def test_one_zero_poll_mid_session_is_ignored(self):
        tracker, clock = make(raw=53)
        assert poll(tracker, clock, 0) == 53       # glitch: previous good kept
        assert poll(tracker, clock, 54) == 54      # session carries on
        assert poll(tracker, clock, 55) == 55

    def test_two_glitch_polls_then_normal_is_still_ignored(self):
        tracker, clock = make(raw=53)
        assert poll(tracker, clock, 1) == 53
        assert poll(tracker, clock, 0) == 53
        assert poll(tracker, clock, 54) == 54

    def test_real_reset_is_accepted_on_third_consecutive_poll(self):
        tracker, clock = make(raw=53)
        assert poll(tracker, clock, 0) == 53
        assert poll(tracker, clock, 0) == 53
        assert poll(tracker, clock, 1) == 1        # confirmed; never goes negative
        assert poll(tracker, clock, 1) == 1
        assert poll(tracker, clock, 2) == 2

    def test_session_boundary_accepts_zero_immediately(self):
        tracker, clock = make(raw=53)
        assert poll(tracker, clock, 0, boundary=True) == 0


class TestFillerBaseline:
    def test_all_ones_32bit_first_read_is_not_a_baseline(self):
        for key in (TOT, CUR):
            tracker, clock = make(key=key)
            tracker._last.clear()
            data = {key: 0xFFFFFFFF, "max_power_raw": 73}
            tracker.apply(data, session_boundary=False)
            assert key not in data and tracker.last_good(key) is None
