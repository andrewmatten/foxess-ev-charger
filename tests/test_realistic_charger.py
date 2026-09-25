"""Audit 2026-09-25 findings, driven against FakeCharger (tests/fake_charger.py)
rather than a MagicMock that obeys any value forever.

The 2.4.2 bug (180s Command Time Validity, firmware still reverting at ~60s)
slipped past 246 tests because nothing modelled the firmware's time-based
revert-to-max. Each test here fails against 2.4.2 and passes on 2.4.3.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.foxess_charger as integration
from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID, DOMAIN,
    REG_CHARGING_CONTROL, REG_MAX_CHARGING_POWER,
)

from fake_charger import FakeCharger, MAX_POWER_RAW

CAPPED_POWER_RAW = 14  # 1.4 kW, what the EX5 automations actually set


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def make_session(hass, fake: FakeCharger) -> FoxESSChargerCoordinator:
    """Coordinator mid-session with a 1.4kW cap desired and first refresh
    done - same order as async_setup_entry, minus the heartbeat task."""
    coordinator = FoxESSChargerCoordinator(hass, fake, scan_interval=10)
    coordinator.desired_setpoints = {REG_MAX_CHARGING_POWER: CAPPED_POWER_RAW}
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    coordinator._charging_desired = True
    return coordinator


# ── Finding 1: drift between polled register and desired setpoint ──────────

async def test_drift_is_detected_and_resent(hass, caplog, monkeypatch):
    fake = FakeCharger()
    coordinator = await make_session(hass, fake)
    # Only a drift-triggered wake can re-send within the test's timeout.
    monkeypatch.setattr(integration, "get_heartbeat_interval", lambda _tv: 30)
    await coordinator._heartbeat_tick()
    assert fake.effective_power_raw() == CAPPED_POWER_RAW
    drift_before = coordinator.setpoint_drift_events
    await coordinator.async_start_heartbeat()
    try:
        await asyncio.sleep(0.05)  # let the startup tick settle
        fake.advance(61)  # firmware reverts to max despite 0x3005=180
        assert fake.effective_power_raw() == MAX_POWER_RAW
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await coordinator.async_refresh()
            await _wait_until(lambda: fake.effective_power_raw() == CAPPED_POWER_RAW)
    finally:
        await coordinator.async_stop_heartbeat()

    assert "drift" in caplog.text.lower()
    assert coordinator.setpoint_drift_events == drift_before + 1
    assert coordinator.data["diag_setpoint_drift_events"] == drift_before + 1


async def test_no_drift_warning_when_not_charging(hass, caplog):
    fake = FakeCharger()
    fake.status = 1  # connected, not charging
    coordinator = await make_session(hass, fake)
    coordinator._charging_desired = False
    with caplog.at_level(logging.WARNING):
        await coordinator.async_refresh()
    assert coordinator.setpoint_drift_events == 0
    assert fake.writes == []


# ── Finding 2: fast retry after a failed heartbeat write ────────────────────

async def test_failed_heartbeat_write_is_retried_fast(hass, monkeypatch):
    fake = FakeCharger()
    coordinator = await make_session(hass, fake)
    # Normal interval long enough that only a fast retry can land in time.
    monkeypatch.setattr(integration, "get_heartbeat_interval", lambda _tv: 30)
    monkeypatch.setattr(integration, "SETPOINT_REASSERT_MIN_INTERVAL", 0.01)
    fake.fail_writes = 1
    await coordinator.async_start_heartbeat()
    try:
        coordinator._wake_heartbeat()
        await _wait_until(lambda: len(fake.writes) >= 1)
    finally:
        await coordinator.async_stop_heartbeat()
    assert len(fake.write_attempts) >= 2
    assert fake.effective_power_raw() == CAPPED_POWER_RAW
    assert coordinator.heartbeat_write_failures == 1


# ── Finding 3: keep capping when blocks go stale / non-fatal alarm ─────────

async def test_heartbeat_keeps_capping_with_stale_config_block(hass):
    fake = FakeCharger()
    coordinator = await make_session(hass, fake)
    coordinator._block_last_success.pop("config")  # config reads failing
    await coordinator._heartbeat_tick()
    assert (0.0, REG_MAX_CHARGING_POWER, CAPPED_POWER_RAW) in fake.writes


async def test_heartbeat_keeps_capping_with_stale_status_block(hass):
    fake = FakeCharger()
    coordinator = await make_session(hass, fake)
    coordinator._block_last_success.pop("status")
    await coordinator._heartbeat_tick()
    assert (0.0, REG_MAX_CHARGING_POWER, CAPPED_POWER_RAW) in fake.writes


async def test_heartbeat_keeps_capping_during_non_fatal_alarm(hass):
    fake = FakeCharger()
    fake.alarm_code = 0b100  # phase_loss - charger keeps drawing current
    coordinator = await make_session(hass, fake)
    assert coordinator.data["active_alarms"] == ["phase_loss"]
    await coordinator._heartbeat_tick()
    assert (0.0, REG_MAX_CHARGING_POWER, CAPPED_POWER_RAW) in fake.writes


async def test_hard_fault_sends_stop_instead_of_going_silent(hass):
    fake = FakeCharger()
    fake.fault_code = 1 << 3  # overcurrent
    coordinator = await make_session(hass, fake)
    assert coordinator.data["active_faults"] == ["overcurrent"]
    await coordinator._heartbeat_tick()
    assert (0.0, REG_CHARGING_CONTROL, 2) in fake.writes
    assert not any(a == REG_MAX_CHARGING_POWER for _, a, _v in fake.writes)
    assert coordinator._charging_desired is False


# ── Finding 4: restart mid-session re-applies the cap immediately ──────────

async def test_setup_mid_session_writes_cap_immediately(
    hass, monkeypatch, enable_custom_integrations,
):
    fake = FakeCharger()
    monkeypatch.setattr(integration, "get_heartbeat_interval", lambda _tv: 30)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "192.0.2.1", CONF_PORT: 502, CONF_SLAVE_ID: 1},
    )
    entry.add_to_hass(hass)

    async def _load(self):
        self.desired_setpoints = {REG_MAX_CHARGING_POWER: CAPPED_POWER_RAW}

    with patch.object(integration, "FoxESSModbusClient", lambda *a: fake), \
         patch.object(FoxESSChargerCoordinator, "async_load_desired_setpoints", _load), \
         patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    try:
        await _wait_until(lambda: fake.effective_power_raw() == CAPPED_POWER_RAW, 0.5)
    finally:
        await coordinator.async_stop_heartbeat()


# ── Finding 5: malformed restored setpoints must not crash setup ───────────

@pytest.mark.parametrize("bad", ["73", None, 7.3, True, [73]])
async def test_malformed_restored_setpoint_is_dropped(hass, bad):
    fake = FakeCharger()
    coordinator = FoxESSChargerCoordinator(hass, fake, scan_interval=10)
    await coordinator.async_refresh()
    coordinator.desired_setpoints = {REG_MAX_CHARGING_POWER: bad}
    await coordinator.async_validate_desired_setpoints()
    assert REG_MAX_CHARGING_POWER not in coordinator.desired_setpoints


# ── Energy guard: garbage max_power_raw must not disable the guard ─────────

async def test_garbage_max_power_raw_does_not_disable_energy_guard(hass):
    import time
    coordinator = FoxESSChargerCoordinator(hass, FakeCharger(), scan_interval=10)
    coordinator._prev_status = 3
    coordinator._last_energy["total_energy_raw"] = (1000, time.monotonic() - 60)
    # +5kWh in 60s = 300kW - impossible on a 7.3kW charger.
    result = coordinator._sanitize_energy(
        "total_energy_raw", 1050, {"status": 3, "max_power_raw": 0xFFFF},
    )
    assert result is None
