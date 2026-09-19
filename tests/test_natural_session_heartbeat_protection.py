"""Tests for the P0 fix: naturally-started charging sessions (Plug & Charge
mode, or an RFID card tap) get heartbeat protection too, not only sessions
started via switch.py's async_turn_on.

Before this fix, `_charging_desired` was only ever set True by the charging
switch's own async_turn_on - a session this charger started entirely on its
own (independent of HA) left the heartbeat/poll-driven re-assertion with
nothing telling it to protect that session's setpoints, right up until the
user happened to toggle the switch themselves (which they'd have no reason
to do for a session already running).

FoxESSChargerCoordinator._fetch() now sets _charging_desired True the
moment a *fresh, successful* status-block read shows an active session that
wasn't already flagged - see the comment right after `data["alarm_code"] =
regs[21]` in __init__.py. Deliberately does not fire on a poll where this
specific block's read failed (data carried over via the
seed-from-last-known-good pattern), even if the coordinator's overall
`last_update_success` stays True because other blocks succeeded.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
)
from tests.test_coordinator_batching import make_status_block_registers


def make_client_with_status(status: int) -> MagicMock:
    client = MagicMock()

    def _read_registers(address, count, quiet=False):
        if (address, count) == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT):
            regs = make_status_block_registers()
            regs[3] = status
            return regs
        if (address, count) == (0x3000, 7):
            return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
        return None  # phase-switch-box: expected to fail
    client.read_registers.side_effect = _read_registers
    client.read_ascii.return_value = None
    return client


class TestFetchSetsDesiredFlag:
    def test_fresh_active_status_sets_charging_desired_without_any_switch_call(self, hass):
        """The whole point: nothing here ever touches switch.py."""
        client = make_client_with_status(3)  # charging
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        assert coordinator._charging_desired is False

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is True

    def test_inactive_status_does_not_set_the_flag(self, hass):
        client = make_client_with_status(0)  # idle
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is False

    def test_already_desired_stays_desired_on_a_subsequent_active_poll(self, hass):
        client = make_client_with_status(3)
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._charging_desired = True

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is True

    def test_failed_status_block_read_does_not_set_the_flag_from_stale_data(self, hass):
        """A poll where the status block itself failed to read must not be
        able to newly flag a session as desired - even though the seed-
        from-last-known-good pattern leaves a stale `status` value sitting
        in `data`, and even though the coordinator's overall fetch can
        still "succeed" (other blocks read fine)."""
        client = MagicMock()

        def _read_registers(address, count, quiet=False):
            if (address, count) == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT):
                return None  # status block read fails this poll
            if (address, count) == (0x3000, 7):
                return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
            return None
        client.read_registers.side_effect = _read_registers
        client.read_ascii.return_value = None

        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        # Seed stale data showing an active session, as if a prior
        # successful poll had already happened.
        coordinator.data = {"status": 3}
        assert coordinator._charging_desired is False

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is False

    def test_does_not_clear_an_already_desired_flag(self, hass):
        """Clearing is exclusively switch.py's async_turn_off's job (the
        stop-race fix depends on it happening before the stop write is even
        sent) - _fetch() must never be the one to clear it, even if status
        goes inactive."""
        client = make_client_with_status(0)  # idle/finished
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._charging_desired = True

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is True


class TestUpdateDataWakesHeartbeatOnNaturalStart:
    """_async_update_data() already wakes the heartbeat for a second,
    unrelated reason (a time_validity change - see that block) - these
    fetches report time_validity=30 on a coordinator that starts with
    _last_time_validity=None, which independently triggers a wake. Both
    tests pre-seed _last_time_validity to the value _fetch() will report,
    so that unrelated wake reason can't fire and confound what's being
    tested here."""

    async def test_transition_to_desired_wakes_the_heartbeat(self, hass):
        client = make_client_with_status(3)
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._last_time_validity = 30
        coordinator._wake_heartbeat = MagicMock()

        await coordinator._async_update_data()

        coordinator._wake_heartbeat.assert_called_once()

    async def test_no_transition_does_not_wake_the_heartbeat_for_this_reason(self, hass):
        client = make_client_with_status(0)
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._last_time_validity = 30
        coordinator._wake_heartbeat = MagicMock()

        await coordinator._async_update_data()

        coordinator._wake_heartbeat.assert_not_called()
