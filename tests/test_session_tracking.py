"""Logical session boundaries (session.py) and energy baselines (energy.py)."""
from __future__ import annotations

from custom_components.foxess_charger.energy import EnergyTracker
from custom_components.foxess_charger.session import SessionTracker


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make():
    clock, wall = Clock(), Clock()
    done: list[dict] = []
    tracker = SessionTracker(on_complete=done.append, clock=clock, wall_clock=wall)
    return tracker, done, clock, wall


def obs(status, total=1000, power=0, **extra):
    data = {"status": status, "status_valid": status is not None,
            "total_energy_raw": total, "power_raw": power}
    data.update(extra)
    return data


def feed(tracker, clock, seq, intent=True):
    for item in seq:
        clock.t += 10
        tracker.update(item, intent_enabled=intent)


def test_vehicle_pause_and_phase_switch_do_not_end_session():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3), obs(4), obs(9), obs(3), obs(4)])
    assert done == [] and tracker.active


def test_unknown_status_never_completes_session():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3)])
    invalid = {"status": None, "status_valid": False, "total_energy_raw": 1000, "power_raw": 0}
    feed(tracker, clock, [invalid, dict(invalid), obs(3)])
    assert done == [] and tracker.active


def test_known_inactive_status_completes_once_with_lifetime_delta():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3, total=1000), obs(3, total=1030), obs(5, total=1042), obs(5, total=1042)])
    assert len(done) == 1
    assert done[0]["energy_kwh"] == 4.2


def test_user_pause_ends_session_once_and_needs_rearm():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3, total=1000, power=70), obs(3, total=1020, power=70)])
    # Pause confirmed: charger reports vehicle-paused at zero power.
    feed(tracker, clock, [obs(4, total=1021), obs(4, total=1021), obs(4, total=1021)], intent=False)
    assert len(done) == 1
    assert done[0]["energy_kwh"] == 2.1
    assert not tracker.active
    # Still paused-and-connected: no phantom session while off.
    feed(tracker, clock, [obs(3, total=1021, power=70)], intent=False)
    assert len(done) == 1 and not tracker.active


def test_resume_after_user_pause_starts_new_session_without_double_count():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3, total=1000, power=70), obs(3, total=1020, power=70)])
    feed(tracker, clock, [obs(4, total=1020)], intent=False)
    # On-device session counter keeps counting across a zero-power pause;
    # only the lifetime delta is used.
    feed(tracker, clock, [obs(3, total=1020, power=70, current_energy_raw=20),
                          obs(3, total=1035, power=70, current_energy_raw=35),
                          obs(5, total=1035, current_energy_raw=35)])
    assert [d["energy_kwh"] for d in done] == [2.0, 1.5]


def test_explicit_end_for_user_pause_is_idempotent():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3)])
    data = obs(4)
    assert tracker.end_for_user_pause(data) is True
    assert tracker.end_for_user_pause(data) is False
    assert len(done) == 1


def test_unplug_rearms_after_user_pause():
    tracker, done, clock, _ = make()
    feed(tracker, clock, [obs(3)])
    feed(tracker, clock, [obs(4)], intent=False)
    feed(tracker, clock, [obs(0), obs(3, power=70)], intent=False)
    assert tracker.active  # genuinely new (e.g. Plug&Charge) session recorded
    assert len(done) == 1


def test_restored_session_continues_without_reset():
    tracker, done, clock, wall = make()
    wall.t = 5000.0
    tracker.restore({"session_start_wall": 4400.0, "session_start_total": 900,
                     "prev_status": 3, "last_session": None})
    feed(tracker, clock, [obs(3, total=950), obs(5, total=960)])
    assert len(done) == 1
    assert done[0]["energy_kwh"] == 6.0
    assert done[0]["duration_min"] == 10.3


def test_restored_last_session_does_not_fire():
    tracker, done, clock, _ = make()
    tracker.restore({"last_session": {"energy_kwh": 1.0}, "prev_status": 1})
    data = obs(1)
    tracker.update(data, intent_enabled=True)
    assert done == [] and data["last_session"] == {"energy_kwh": 1.0}


def test_energy_baseline_round_trip_rejects_corrupt_first_reading():
    clock, wall = Clock(), Clock()
    first = EnergyTracker(clock=clock, wall_clock=wall)
    data = {"total_energy_raw": 3785, "current_energy_raw": 0}
    first.apply(data, session_boundary=False)
    stored = first.export()
    clock.t += 5
    wall.t += 5
    second = EnergyTracker(clock=clock, wall_clock=wall)
    second.restore(stored)
    bad = {"total_energy_raw": 65800, "current_energy_raw": 0}
    second.apply(bad, session_boundary=False)
    assert bad["total_energy_raw"] == 3785
    assert second.rejections and second.rejections[-1]["rejected_raw"] == 65800


def test_missing_energy_reading_keeps_last_good():
    tracker = EnergyTracker()
    tracker.apply({"total_energy_raw": 10}, session_boundary=False)
    data = {"total_energy_raw": None}
    tracker.apply(data, session_boundary=False)
    assert data["total_energy_raw"] == 10
