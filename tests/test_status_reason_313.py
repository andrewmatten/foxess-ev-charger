"""One diagnostic sensor answers "why is the car not charging right now?"."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.sensor import FoxESSChargerSensor, SENSORS

from fake_controller import FakeController

KEY = "charging_status_reason"


def _sensor(coordinator):
    desc = next(d for d in SENSORS if d.key == KEY)
    return FoxESSChargerSensor(coordinator, desc, MagicMock(entry_id="entry"))


def _fake(*, status=1, cc=1, enabled=True, phase="enabled", cause=None,
          human=False, release=None):
    fake = FakeController(status=status, cc_status=cc)
    fake._enabled, fake.phase = enabled, phase
    fake.safety_latched, fake.latch_cause = cause is not None, cause
    fake.needs_human = human
    if release is not None:
        fake.diagnostics["latch_release_in"] = release
    return fake


CASES = [
    ("charger_offline", dict(status=99)),
    ("charger_offline", dict(status=99, human=True)),   # offline wins
    ("needs_attention", dict(cause="hard", human=True, phase="faulted")),
    ("stop_failed", dict(cause="stop_failed", phase="faulted")),
    ("charger_fault", dict(cause="fault", phase="faulted")),
    ("charger_fault", dict(cause="unknown", phase="faulted")),
    ("link_problem", dict(cause="comms", phase="faulted")),
    ("link_uncertain", dict(phase="uncertain")),
    ("car_not_connected", dict(status=0, cc=0, phase="paused", enabled=False)),
    ("charger_locked", dict(status=8)),
    ("charging_off", dict(enabled=False, phase="paused")),
    ("charging_off", dict(phase="paused_below_minimum")),
    ("waiting_for_car", dict(status=1)),
    ("waiting_for_car", dict(status=4)),
    ("waiting_for_car", dict(status=5)),
    ("starting", dict(status=2)),
    ("charging", dict(status=3)),
]


@pytest.mark.parametrize("expected,setup", CASES)
async def test_reason_mapping(hass, expected, setup):
    coordinator = FoxESSChargerCoordinator(hass, _fake(**setup), 10)
    await coordinator.async_refresh()
    entity = _sensor(coordinator)
    assert entity.native_value == expected
    assert expected in entity.entity_description.options


async def test_attributes_and_auto_retry(hass):
    coordinator = FoxESSChargerCoordinator(
        hass, _fake(cause="comms", phase="faulted", release=42), 10)
    await coordinator.async_refresh()
    attrs = _sensor(coordinator).extra_state_attributes
    assert attrs == {"latch_cause": "comms", "needs_human": False, "auto_retry_in": 42}


async def test_auto_retry_omitted_when_unknown(hass):
    coordinator = FoxESSChargerCoordinator(hass, _fake(), 10)
    await coordinator.async_refresh()
    assert "auto_retry_in" not in _sensor(coordinator).extra_state_attributes


async def test_unavailable_without_data(hass):
    fake = _fake()
    fake.fail_poll = 1
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    await coordinator.async_refresh()
    entity = _sensor(coordinator)
    assert entity.available is False
    assert entity.native_value is None
    assert entity.extra_state_attributes == {}


async def _offline_after_good_poll(hass):
    fake = _fake(status=3)
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    await coordinator.async_refresh()
    entity = _sensor(coordinator)
    assert entity.native_value == "charging"

    async def dead_link():  # what the real controller returns when the status read fails
        return {}
    real_poll, fake.async_poll = fake.async_poll, dead_link
    return fake, coordinator, entity, real_poll


async def test_dead_link_reads_offline_and_stays_available(hass):
    _, coordinator, entity, _ = await _offline_after_good_poll(hass)
    for _poll in range(4):  # from the first failed poll, past the point the coordinator gives up
        await coordinator.async_refresh()
        assert entity.native_value == "charger_offline"
        assert entity.available is True
        entity.extra_state_attributes  # must not raise


async def test_offline_recovers_on_first_good_poll(hass):
    fake, coordinator, entity, real_poll = await _offline_after_good_poll(hass)
    await coordinator.async_refresh()
    assert entity.native_value == "charger_offline"
    fake.async_poll = real_poll
    await coordinator.async_refresh()
    assert entity.native_value == "charging"
    assert entity.available is True
