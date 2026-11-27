"""3.2 R5-2: an interrupted save of a limit increase must not resurrect on cold restart."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import DOMAIN, REG_MAX_CHARGING_POWER
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.persistence import (
    ATTEMPTED_HARD_KEY,
    ATTEMPTED_REVISION_KEY,
    ChargerStorage,
    setpoints_key,
)
from test_controller import POW, FakeClock, FakeIO, saved, snapshot


def _cold_restart(hass) -> None:
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_REVISION_KEY, None)
    hass.data.get(DOMAIN, {}).pop(ATTEMPTED_HARD_KEY, None)


async def test_failed_state_write_on_power_increase_does_not_resurrect_on_cold_restart(
    hass, hass_storage
):
    """During an active 2 kW session, raising to 7 kW whose state write fails
    before HA stops must not leave 7 kW in legacy setpoints to be imported and
    applied on cold startup."""
    entry = "r5_2_state_fail"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved(enabled=True, power=20, revision=5))
    await storage.async_save_session({"prev_status": 3}, rearm_required=False)

    async def fail_state(_data):
        raise OSError("state write failed")

    storage._state_store.async_save = fail_state
    with pytest.raises(OSError):
        await storage.async_save_controller(saved(enabled=True, power=70, revision=6))

    # HA stops before any rollback runs: cold restart against charger still at 2 kW.
    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["power_raw"] == 20

    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    io.regs[POW] = 20
    io._physics()
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(loaded.controller, configure_safety=False)
    assert c.desired_power_raw == 20
    assert 70 not in io.power_writes()
    assert io.regs[POW] == 20


async def test_failed_setpoints_write_on_power_increase_fails_safe_on_cold_restart(
    hass, hass_storage
):
    """If the state write succeeds on a 2 kW -> 7 kW increase and the legacy
    setpoints write then fails before HA stops, cold restart must restore paused
    at the older 2 kW limit rather than applying 7 kW."""
    entry = "r5_2_setpoints_fail"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved(enabled=True, power=20, revision=5))
    await storage.async_save_session({"prev_status": 3}, rearm_required=False)

    async def fail_setpoints(_data):
        raise OSError("setpoints write failed")

    storage._setpoints_store.async_save = fail_setpoints
    with pytest.raises(OSError):
        await storage.async_save_controller(saved(enabled=True, power=70, revision=6))

    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.controller["enabled"] is False
    assert loaded.controller["power_raw"] == 20

    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    io.regs[POW] = 20
    io._physics()
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(loaded.controller, configure_safety=False)
    assert not c.intent_enabled
    assert 70 not in io.power_writes()
    assert io.regs[POW] == 0


async def test_fingerprint_mismatch_with_higher_legacy_setpoint_restores_paused(
    hass, hass_storage
):
    """Even if legacy setpoints diverge upward from the last committed state
    while prev_status is active, cold startup must restore paused so refresh
    never raises the live charger cap."""
    entry = "r5_2_mismatch"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved(enabled=True, power=20, revision=5))
    await storage.async_save_session({"prev_status": 3}, rearm_required=False)

    hass_storage[setpoints_key(entry)]["data"] = {
        "desired_setpoints": {str(REG_MAX_CHARGING_POWER): 70}
    }

    _cold_restart(hass)
    loaded = await ChargerStorage(hass, entry).async_load()
    assert loaded.source == "legacy"
    assert loaded.controller["enabled"] is False

    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    io.regs[POW] = 20
    io._physics()
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(loaded.controller, configure_safety=False)
    assert not c.intent_enabled
    assert 70 not in io.power_writes()
    assert io.regs[POW] == 0
