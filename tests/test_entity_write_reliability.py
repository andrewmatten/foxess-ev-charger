"""Tests for the 2.1.3 entity write-reliability changes (switch/number/select).

Covers the two behaviours added on top of the CoordinatorEntity conversion:

1. A failed write (client.write_holding_register returns False) raises
   HomeAssistantError instead of being silently swallowed.
2. After a successful write, once the coordinator has refreshed, a
   read-back that doesn't match what was written logs a warning instead of
   being trusted blindly.

Entities are constructed directly (not through async_setup_entry / a full
config entry) and `async_write_ha_state` is stubbed out - these tests are
about the write-reliability business logic in switch.py/number.py/select.py,
not about exercising HA's state-machine write path (which belongs to HA
core's own test suite, not this integration's).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import REG_CHARGING_CONTROL, REG_WORK_MODE
from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber
from custom_components.foxess_charger.select import FoxESSWorkModeSelect
from custom_components.foxess_charger.switch import FoxESSChargingSwitch


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def make_coordinator(hass, initial_data: dict, client=None) -> FoxESSChargerCoordinator:
    # Task 3: FoxESSChargingSwitch's start/stop writes now go through the
    # coordinator's own command lock (coordinator.client), not a write
    # issued directly against the entity's own client reference - so
    # whichever client a test configures return values on must be the same
    # object the coordinator was constructed with, exactly like production
    # wiring in async_setup_entry (same `client` passed to both). Defaults
    # to a fresh MagicMock() for tests (number.py/select.py, mostly) that
    # never route a write through the coordinator at all.
    coordinator = FoxESSChargerCoordinator(hass, client or MagicMock(), scan_interval=10)
    coordinator.data = dict(initial_data)
    return coordinator


async def test_charging_switch_turn_on_raises_on_failed_write(hass):
    client = MagicMock()
    client.write_holding_register.return_value = False
    coordinator = make_coordinator(hass, {"status": 0}, client=client)
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    client.write_holding_register.assert_called_once_with(REG_CHARGING_CONTROL, 1)


async def test_charging_switch_turn_on_does_not_set_desired_flag_on_failed_write(hass):
    """P0 audit fix: _charging_desired used to be set True *before* the
    write, unconditionally - a failed start command still left the
    heartbeat believing a session it never actually started needed
    protecting. Must stay False when the write fails."""
    client = MagicMock()
    client.write_holding_register.return_value = False
    coordinator = make_coordinator(hass, {"status": 0}, client=client)
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    assert coordinator._charging_desired is False
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    assert coordinator._charging_desired is False


async def test_charging_switch_turn_on_sets_desired_flag_only_after_successful_write(hass):
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"status": 0}, client=client)
    coordinator.async_request_refresh = AsyncMock()
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    assert coordinator._charging_desired is False
    with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
        await entity.async_turn_on()

    assert coordinator._charging_desired is True


async def test_charging_switch_turn_off_clears_flag_on_successful_write(hass):
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"status": 3}, client=client)
    coordinator._charging_desired = True
    coordinator.async_request_refresh = AsyncMock()
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
        await entity.async_turn_off()

    assert coordinator._charging_desired is False


async def test_charging_switch_turn_off_restores_flag_on_failed_write(hass):
    """P0 audit fix: the stop-race fix requires clearing _charging_desired
    before the stop write is even attempted (must not change - see
    async_turn_off's comment). If the stop write itself then fails, the
    charger is still actually running - leaving the flag cleared would
    silently drop all heartbeat protection for a session that never
    stopped. Must be restored to True when the write raises.

    Task 3: restoring on failure now requires a *fresh* status confirmation
    (async_request_refresh) rather than restoring unconditionally - mocked
    here to report the charger as still actively charging, so this test
    keeps exercising the "still active -> restore" branch it was written
    for (see test_command_lock.py's TestFailedStopRestoresProtectionOnly
    IfStillActive for the paired "actually stopped -> don't restore" case).
    """
    client = MagicMock()
    client.write_holding_register.return_value = False
    coordinator = make_coordinator(hass, {"status": 3}, client=client)
    coordinator._charging_desired = True

    async def _refresh_still_active():
        coordinator.data = {"status": 3}
    coordinator.async_request_refresh = AsyncMock(side_effect=_refresh_still_active)

    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()

    assert coordinator._charging_desired is True


async def test_charging_switch_turn_off_increments_generation_before_the_write(hass):
    """Paired with __init__.py's TestGenerationTokenRace - this only
    confirms async_turn_off actually bumps the counter as part of its
    first-actions-before-the-write block; the heartbeat-side consequences
    of that are tested in test_heartbeat_task.py."""
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"status": 3}, client=client)
    coordinator._charging_desired = True
    coordinator.async_request_refresh = AsyncMock()
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    before = coordinator._charging_generation
    with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
        await entity.async_turn_off()

    assert coordinator._charging_generation == before + 1


async def test_charging_switch_turn_on_warns_when_status_never_reflects_it(hass, caplog):
    """Write succeeds, but the post-refresh status never shows an active
    session - a real "accepted but not applied" failure mode worth a clear
    warning, not silent trust in the optimistic patch."""
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"status": 0}, client=client)

    # A real refresh replaces coordinator.data wholesale with a fresh
    # _fetch() result (see __init__.py) - simulate that here rather than
    # mutating the existing dict, so the optimistic patch set moments
    # earlier by async_turn_on can't accidentally satisfy the read-back
    # check on its own.
    async def _refresh_status_never_changed():
        coordinator.data = {"status": 0}

    coordinator.async_request_refresh = AsyncMock(side_effect=_refresh_status_never_changed)
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
        await entity.async_turn_on()

    assert "did not reflect an active session" in caplog.text


async def test_charging_switch_turn_on_no_warning_when_status_matches(hass, caplog):
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"status": 0}, client=client)

    async def _refresh():
        coordinator.data["status"] = 3  # charger actually applied it

    coordinator.async_request_refresh = AsyncMock(side_effect=_refresh)
    entity = FoxESSChargingSwitch(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
        await entity.async_turn_on()

    assert "did not reflect" not in caplog.text


async def test_number_raises_on_failed_write(hass):
    client = MagicMock()
    client.write_holding_register.return_value = False
    # max_charging_current is a REASSERTED_REGISTERS entry, so its write now
    # routes through coordinator.async_send_setpoint_user (coordinator.client),
    # not the entity's own self._client reference - same reasoning as
    # make_coordinator's comment above re: switch.py's start/stop. Both must
    # be the same client object for this test's return_value to take effect.
    coordinator = make_coordinator(hass, {"max_charging_current_raw": 320}, client=client)
    desc = next(d for d in NUMBERS if d.key == "max_charging_current")
    entity = FoxESSNumber(coordinator, client, desc, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(16.0)


async def test_number_warns_on_read_back_mismatch(hass, caplog):
    client = MagicMock()
    client.write_holding_register.return_value = True
    # Same reasoning as test_number_raises_on_failed_write above: the write
    # for this REASSERTED_REGISTERS entry now goes through coordinator.client,
    # so it must be the same object as the entity's client for this test to
    # actually exercise what it claims to.
    coordinator = make_coordinator(hass, {"max_charging_current_raw": 320}, client=client)

    # A real refresh replaces coordinator.data wholesale with a fresh
    # _fetch() result - simulate the charger reporting back the old value
    # (acknowledged the write without actually applying it), rather than
    # mutating the dict the optimistic patch already touched.
    async def _refresh_value_never_changed():
        coordinator.data = {"max_charging_current_raw": 320}

    coordinator.async_request_refresh = AsyncMock(side_effect=_refresh_value_never_changed)
    desc = next(d for d in NUMBERS if d.key == "max_charging_current")
    entity = FoxESSNumber(coordinator, client, desc, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with patch("custom_components.foxess_charger.number.asyncio.sleep", AsyncMock()):
        await entity.async_set_native_value(16.0)

    assert "read-back after 3 refreshes" in caplog.text
    assert coordinator.async_request_refresh.await_count == 3


async def test_number_allows_delayed_read_back(hass, caplog):
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"max_charging_current_raw": 320}, client=client)
    refreshes = 0

    async def _refresh_value_on_second_attempt():
        nonlocal refreshes
        refreshes += 1
        coordinator.data = {"max_charging_current_raw": 160 if refreshes == 2 else 320}

    coordinator.async_request_refresh = AsyncMock(side_effect=_refresh_value_on_second_attempt)
    desc = next(d for d in NUMBERS if d.key == "max_charging_current")
    entity = FoxESSNumber(coordinator, client, desc, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with patch("custom_components.foxess_charger.number.asyncio.sleep", AsyncMock()):
        await entity.async_set_native_value(16.0)

    assert coordinator.async_request_refresh.await_count == 2
    assert "read-back after" not in caplog.text


async def test_number_skips_warning_for_superseded_setpoint(hass, caplog):
    client = MagicMock()
    client.write_holding_register.return_value = True
    coordinator = make_coordinator(hass, {"max_charging_current_raw": 320}, client=client)
    coordinator.async_request_refresh = AsyncMock()
    coordinator.desired_setpoints[REG_MAX_CHARGING_CURRENT] = 200
    desc = next(d for d in NUMBERS if d.key == "max_charging_current")
    entity = FoxESSNumber(coordinator, client, desc, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    await entity._async_verify_read_back(desc, 160)

    assert coordinator.async_request_refresh.await_count == 1
    assert "read-back after" not in caplog.text


async def test_work_mode_select_raises_on_failed_write(hass):
    client = MagicMock()
    client.write_holding_register.return_value = False
    coordinator = make_coordinator(hass, {"work_mode": 0})
    entity = FoxESSWorkModeSelect(coordinator, client, make_entry())
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()

    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("Plug&Charge")

    client.write_holding_register.assert_called_once_with(REG_WORK_MODE, 1)
