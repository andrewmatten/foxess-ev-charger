"""C4: SessionTracker.is_boundary sees intent_enabled so resuming after a user
pause accepts the session-energy counter reset on the first poll."""
from __future__ import annotations

from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.energy import EnergyTracker
from custom_components.foxess_charger.session import SessionTracker

from fake_controller import FakeController


class Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def test_resume_after_user_pause_accepts_session_energy_reset(hass):
    clock = Clock()
    fake = FakeController(status=3)
    fake.hw["current_energy_raw"] = 50
    fake.hw["total_energy_raw"] = 1050
    fake.hw["power_raw"] = 70

    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    coordinator.energy = EnergyTracker(clock=clock, wall_clock=clock)
    await coordinator.async_refresh()
    assert coordinator.data["current_energy_raw"] == 50
    assert coordinator.sessions.active

    # User pauses charging: logical session ends and rearm_required is set.
    clock.t += 60
    fake._enabled = False
    fake.hw["status"] = 4
    fake.hw["power_raw"] = 0
    await coordinator.async_user_paused()
    assert not coordinator.sessions.active
    assert coordinator.sessions.rearm_required is True

    # User resumes later; charger opens a new session and resets current_energy_raw
    # (already at 0.5 kWh by this poll, above NEAR_ZERO_RAW_UNITS).
    clock.t += 600
    fake._enabled = True
    fake.hw["status"] = 3
    fake.hw["power_raw"] = 70
    fake.hw["current_energy_raw"] = 5
    fake.hw["total_energy_raw"] = 1055
    await coordinator.async_refresh()

    assert coordinator.energy.rejections == []
    assert coordinator.data["current_energy_raw"] == 5
    assert coordinator.sessions.active
    assert coordinator.sessions.start_current == 5


def test_is_boundary_matches_update_conditions():
    tracker = SessionTracker()
    tracker.rearm_required = True

    active_obs = {"status": 3, "status_valid": True, "power_raw": 70}
    assert tracker.is_boundary(active_obs, intent_enabled=False) is False
    assert tracker.is_boundary(active_obs, intent_enabled=True) is True

    tracker.rearm_required = False
    paused_obs = {"status": 4, "status_valid": True, "power_raw": 0}
    assert tracker.is_boundary(paused_obs, intent_enabled=False) is False
    assert tracker.is_boundary(paused_obs, intent_enabled=True) is True
