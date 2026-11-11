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
    assert len([r for r in lines(caplog, logging.INFO) if "saving works again" in r.getMessage()]) == 1


async def test_intent_save_failure_logs_once_then_recovery_once(caplog):
    from test_comms_latch_release import refresh
    from test_controller import make, saved

    name = "custom_components.foxess_charger.controller"
    broken = [True]

    async def persist(_state):
        if broken[0]:
            raise OSError("disk full")

    c, io = await make(saved(enabled=False), charging=False)
    c._persist = persist
    caplog.set_level(logging.DEBUG, logger=name)
    caplog.clear()
    await c._update(enabled=False)
    for _ in range(10):
        await refresh(c, io, 20)
    saves = [r for r in caplog.records if r.name == name and "persist" in r.getMessage()]
    assert [r.levelno for r in saves] == [logging.ERROR]
    assert c.diagnostics["persist_pending"] and c.diagnostics["persist_failures"] == 11
    caplog.clear()
    broken[0] = False
    await refresh(c, io, 20)
    await refresh(c, io, 20)
    back = [r for r in caplog.records if r.name == name and "sav" in r.getMessage()]
    assert [r.levelno for r in back] == [logging.INFO]
    assert not c.diagnostics["persist_pending"]
