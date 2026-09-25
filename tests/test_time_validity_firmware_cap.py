"""Regression tests for the 2026-09-24 ~91s power sawtooth.

Live evidence (HA history 2026-09-24 19:00-19:08): 0x3005 Command Time
Validity reads 180s, so get_heartbeat_interval() produced a 90s heartbeat -
but the charger reverted 0x3001/0x3002 to maximum ~60s after each write
(surges every ~91s lasting ~40s; a setpoint write at 19:05:08 was followed by
a surge ~65s later, not 90s). The firmware evidently caps the window at its
documented 60s maximum regardless of the register value, so the heartbeat
must never be computed from more than 60s.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

import custom_components.foxess_charger as integration
from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    DEFAULT_TIME_VALIDITY,
    REG_MAX_CHARGING_POWER,
    get_heartbeat_interval,
)

FIRMWARE_REVERT_AFTER_S = 60  # observed real charger behaviour


# ── Unit: interval math ──────────────────────────────────────────────────────

def test_live_value_180_is_capped_to_the_firmware_window():
    assert get_heartbeat_interval(180) <= 30


@pytest.mark.parametrize("tv,expected", [(60, 30), (20, 10), (10, 5), (180, 30), (3600, 30)])
def test_sensible_values(tv, expected):
    assert get_heartbeat_interval(tv) == expected


@pytest.mark.parametrize("tv", [None, 0, -5, "garbage", float("nan")])
def test_missing_or_garbage_uses_safe_default(tv):
    assert get_heartbeat_interval(tv) == DEFAULT_TIME_VALIDITY / 2


# ── Behavioural: fake charger with a 60s firmware revert ────────────────────

async def test_limit_never_reverts_over_five_minutes_with_0x3005_at_180(hass, monkeypatch):
    """Runs the real _heartbeat_loop on a frozen/virtual clock against a fake
    charger that reverts the power limit to max 60s after the last write,
    whatever 0x3005 says. The limit must hold for the whole 5 minutes."""
    with freeze_time("2026-09-24 09:00:00") as clock:
        start = clock.time_to_freeze.timestamp()
        now = lambda: clock.time_to_freeze.timestamp() - start  # noqa: E731
        last_write = {"t": 0.0}  # session start: setpoint just written
        reverted_at: list[float] = []

        def write(register, value):
            last_write["t"] = now()
            return True

        client = MagicMock()
        client.write_holding_register.side_effect = write

        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 3, "time_validity": 180}
        coordinator.desired_setpoints = {REG_MAX_CHARGING_POWER: 14}
        coordinator._charging_desired = True
        monkeypatch.setattr(coordinator, "block_is_fresh", lambda block: True)

        async def fake_wait_for(aw, timeout):
            # Virtual time: the wait always runs its full timeout.
            aw.close()
            if now() - last_write["t"] + timeout > FIRMWARE_REVERT_AFTER_S:
                reverted_at.append(last_write["t"] + FIRMWARE_REVERT_AFTER_S)
            clock.tick(timeout)
            if now() >= 300:
                raise asyncio.CancelledError
            raise asyncio.TimeoutError

        monkeypatch.setattr(integration.asyncio, "wait_for", fake_wait_for)

        with pytest.raises(asyncio.CancelledError):
            await coordinator._heartbeat_loop()

    assert client.write_holding_register.call_count > 0
    assert reverted_at == [], f"charger reverted to max at t={reverted_at}s"
