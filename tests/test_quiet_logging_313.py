"""One log line per outage and per storage failure, not one per poll."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator

from fake_controller import FakeController

LOGGER = "custom_components.foxess_charger.coordinator"


def lines(caplog, level):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == level]


async def test_offline_logs_one_warning_then_one_info_on_recovery(hass, caplog):
    fake = FakeController(status=1)
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    await coordinator.async_refresh()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    fake.fail_poll = 8
    for _ in range(8):
        await coordinator.async_refresh()
    assert len(lines(caplog, logging.WARNING)) == 1
    caplog.clear()
    await coordinator.async_refresh()
    recovered = [r for r in lines(caplog, logging.INFO) if "offline" in r.getMessage()]
    assert len(recovered) == 1
    assert "second" in recovered[0].getMessage()
    # a second outage logs again
    caplog.clear()
    fake.fail_poll = 2
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert len(lines(caplog, logging.WARNING)) == 1


async def test_store_failure_logs_once_then_recovery_once(hass, caplog):
    storage = MagicMock()
    storage.async_save_session = AsyncMock()
    storage.async_save_energy = AsyncMock(side_effect=OSError("disk full"))
    coordinator = FoxESSChargerCoordinator(hass, FakeController(status=1), 10, storage=storage)
    caplog.set_level(logging.DEBUG)
    for _ in range(5):
        coordinator.energy.dirty = True
        await coordinator._async_persist()
    problems = [r for r in lines(caplog, logging.WARNING) + lines(caplog, logging.ERROR)
                if "persist" in r.getMessage()]
    assert len(problems) == 1
    assert "disk full" in problems[0].getMessage()
    caplog.clear()
    storage.async_save_energy.side_effect = None
    coordinator.energy.dirty = True
    await coordinator._async_persist()
    coordinator.energy.dirty = True
    await coordinator._async_persist()
    assert len([r for r in lines(caplog, logging.INFO) if "persist" in r.getMessage()]) == 1
