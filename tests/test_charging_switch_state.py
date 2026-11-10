"""switch.charging state follows the logical session, including a vehicle
pause: device charging_started/stopped triggers bind to it. (The power-flow
binary sensor stays status-3-only.)

Ported from test_stop_inhibit::test_charging_switch_state_is_session_active_across_vehicle_pause.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.switch import FoxESSChargingSwitch

from fake_controller import FakeController


async def test_charging_switch_state_is_session_active_across_vehicle_pause(hass):
    fake = FakeController(status=1)
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    entity = FoxESSChargingSwitch(coordinator, MagicMock(entry_id="entry"))
    states = []
    for status in (1, 3, 4, 3, 5):
        fake.hw["status"] = status
        await coordinator.async_refresh()
        states.append(entity.is_on)
    assert states == [False, True, True, True, False]


async def test_charging_switch_reads_off_while_latched_or_stopped(hass):
    """A latch or Stop mid-session leaves status 4 and the stored enable:
    the switch must still read off (review S3)."""
    fake = FakeController(status=4)
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    entity = FoxESSChargingSwitch(coordinator, MagicMock(entry_id="entry"))
    await coordinator.async_refresh()
    states = {}
    for phase in ("enabled", "faulted", "stopped"):
        fake.phase = phase
        states[phase] = entity.is_on
    assert fake.intent_enabled
    assert states == {"enabled": True, "faulted": False, "stopped": False}
