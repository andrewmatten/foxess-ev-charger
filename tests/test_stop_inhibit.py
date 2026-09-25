"""Tests for the stop-inhibit latch (2026-09-18 real incident fix).

An automation's unconditional end-of-window number.set_value landed while
charging was intentionally stopped (battery-protection pause). Writing the
charge-limit setpoint registers is itself an implicit "resume charging" on
this firmware - the charger resumed on its own, and _fetch()'s natural-start
detection (built for the legitimate Plug & Charge case) treated it as such,
setting _charging_desired=True and letting the heartbeat sustain the
unwanted session for 52 minutes.

FoxESSChargerCoordinator._stop_inhibit (set by switch.py's async_turn_off,
alongside clearing _charging_desired) suppresses natural-start detection
while an explicit HA-initiated stop is in effect, without proactively
re-stopping anything - deciding whether to correct an unwanted resume is
home-battery-protection *policy*, which lives in the ex5_charging_controller
automation, not this integration. See _stop_inhibit's own comment in
__init__.py for the full reasoning and exactly what clears it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.switch import FoxESSChargingSwitch
from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
)
from tests.test_coordinator_batching import make_status_block_registers


def make_client_with_status_and_cc(status: int, cc_status: int = 1) -> MagicMock:
    client = MagicMock()

    def _read_registers(address, count, quiet=False):
        if (address, count) == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT):
            regs = make_status_block_registers()
            regs[3] = status
            regs[5] = cc_status
            return regs
        if (address, count) == (0x3000, 7):
            return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
        return None  # phase-switch-box: expected to fail
    client.read_registers.side_effect = _read_registers
    client.read_ascii.return_value = None
    return client


class TestNaturalStartSuppressedWhileInhibited:
    def test_active_status_does_not_set_desired_while_inhibited(self, hass):
        """The exact incident mechanism: an unexpected resume while HA
        just intentionally stopped charging must not be mistaken for a
        legitimate Plug & Charge start."""
        client = make_client_with_status_and_cc(status=3, cc_status=1)  # charging, still plugged in
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._stop_inhibit = True

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is False

    def test_active_status_sets_desired_normally_once_not_inhibited(self, hass):
        """Regression guard: this isn't a permanent suppression - ordinary
        Plug & Charge / RFID auto-start detection must keep working exactly
        as before whenever _stop_inhibit is False (the common case)."""
        client = make_client_with_status_and_cc(status=3, cc_status=1)
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        assert coordinator._stop_inhibit is False

        coordinator.data = coordinator._fetch()

        assert coordinator._charging_desired is True


def test_charging_switch_state_is_session_active_across_vehicle_pause(hass):
    """Device charging_started/stopped triggers bind to switch.charging,
    whose status transitions represent a session, including a vehicle pause.
    The power-flow binary sensor remains status-3-only."""
    coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)
    entity = FoxESSChargingSwitch(coordinator, MagicMock(), MagicMock(entry_id="entry"))
    states = []
    for status in (1, 3, 4, 3, 5):
        coordinator.data = {"status": status}
        states.append(entity.is_on)
    assert states == [False, True, True, True, False]


class TestDisconnectClearsInhibit:
    def test_vehicle_unplugging_clears_the_inhibit(self, hass):
        """Once physically unplugged, whatever charges next is
        unambiguously a new, unrelated session - see _stop_inhibit's own
        comment for why clearing at disconnect (rather than waiting for
        the following reconnect) is equivalent and simpler."""
        client = make_client_with_status_and_cc(status=1, cc_status=0)  # connected-but-idle status, unplugged
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._stop_inhibit = True

        coordinator.data = coordinator._fetch()

        assert coordinator._stop_inhibit is False

    def test_staying_plugged_in_does_not_clear_the_inhibit(self, hass):
        client = make_client_with_status_and_cc(status=1, cc_status=1)  # still connected, not charging
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._stop_inhibit = True

        coordinator.data = coordinator._fetch()

        assert coordinator._stop_inhibit is True

    def test_disconnect_marks_session_state_dirty_for_prompt_persistence(self, hass):
        client = make_client_with_status_and_cc(status=1, cc_status=0)
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._stop_inhibit = True
        coordinator._session_state_dirty = False

        coordinator.data = coordinator._fetch()

        assert coordinator._session_state_dirty is True


class FakeStore:
    def __init__(self, initial=None):
        self._data = initial
        self.saved: list[dict] = []

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data
        self.saved.append(data)


class TestRestartMidResumeStaysInhibited:
    """Critical finding from review, 2026-09-18: async_start_heartbeat
    used to unconditionally set _charging_desired from the live status on
    every startup, ignoring a freshly-restored _stop_inhibit - defeating
    the entire point of persisting it. A Core restart while an unexpected
    implicit-resume was ongoing (status active, inhibit persisted True)
    would re-arm the heartbeat right back onto the exact session the
    inhibit exists to leave unprotected. This is the actual restart
    scenario that made persistence necessary in the first place."""

    async def test_active_status_at_startup_does_not_arm_desired_while_inhibited(self, hass):
        client = MagicMock()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 3}  # charging - the unwanted resume, still ongoing
        coordinator._stop_inhibit = True  # as restored by async_load_session_state

        await coordinator.async_start_heartbeat()
        try:
            assert coordinator._charging_desired is False
        finally:
            await coordinator.async_stop_heartbeat()


class TestStopPendingRetry:
    async def test_failed_stop_is_retried_without_any_setpoint_write(self, hass):
        client = make_client_with_status_and_cc(status=3)
        client.write_holding_register.return_value = False
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 3}
        coordinator._stop_pending = True
        coordinator._stop_inhibit = True
        coordinator._charging_desired = False
        coordinator.desired_setpoints = {0x3001: 160}

        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_called_once_with(REG_CHARGING_CONTROL, 2)
        assert coordinator._stop_pending is True
        assert coordinator._heartbeat_retry_pending is True
        assert coordinator.stop_write_failures == 1

    async def test_stop_exception_becomes_retryable_failure(self, hass):
        client = make_client_with_status_and_cc(status=3)
        client.write_holding_register.side_effect = RuntimeError("transport")
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)

        assert await coordinator.async_send_stop() is False

    def test_only_fresh_inactive_status_clears_stop_pending(self, hass):
        coordinator = FoxESSChargerCoordinator(
            hass, make_client_with_status_and_cc(status=1), scan_interval=10,
        )
        coordinator._stop_pending = True

        coordinator._fetch()

        assert coordinator._stop_pending is False

    def test_active_status_does_not_clear_stop_pending(self, hass):
        coordinator = FoxESSChargerCoordinator(
            hass, make_client_with_status_and_cc(status=3), scan_interval=10,
        )
        coordinator._stop_pending = True

        coordinator._fetch()

        assert coordinator._stop_pending is True

    def test_failed_status_read_does_not_clear_stop_pending_from_cached_inactive(self, hass):
        client = make_client_with_status_and_cc(status=1)
        client.read_registers.side_effect = None
        client.read_registers.return_value = None
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 1}
        coordinator._stop_pending = True

        coordinator._fetch()

        assert coordinator._stop_pending is True

    async def test_active_status_at_startup_arms_desired_normally_when_not_inhibited(self, hass):
        """Regression guard: ordinary mid-session restarts (the common
        case this method exists for) must keep working exactly as before."""
        client = MagicMock()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 3}
        assert coordinator._stop_inhibit is False

        await coordinator.async_start_heartbeat()
        try:
            assert coordinator._charging_desired is True
        finally:
            await coordinator.async_stop_heartbeat()


class TestStopInhibitPersistsAcrossARestart:
    """A Core restart mid-inhibit must not forget that charging was
    intentionally stopped - otherwise the very next poll after a restart
    could re-open the exact incident window this latch exists to close."""

    async def test_inhibit_survives_a_restore_round_trip(self, hass):
        store = FakeStore({"stop_inhibit": True})
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, store=store)

        await coordinator.async_load_session_state()

        assert coordinator._stop_inhibit is True

    async def test_absent_key_defaults_to_not_inhibited(self, hass):
        """Malformed/legacy stored state (from before this field existed)
        must not accidentally leave every restart permanently inhibited."""
        store = FakeStore({"prev_status": 3})  # no "stop_inhibit" key at all
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, store=store)

        await coordinator.async_load_session_state()

        assert coordinator._stop_inhibit is False

    async def test_persist_includes_the_current_inhibit_state(self, hass):
        store = FakeStore(None)
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, store=store)
        coordinator._stop_inhibit = True

        await coordinator._async_persist_session_state()

        assert store.saved[-1]["stop_inhibit"] is True

    async def test_pending_stop_is_persisted_and_restored(self, hass):
        store = FakeStore(None)
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, store=store)
        coordinator._stop_pending = True
        coordinator._stop_inhibit = True

        await coordinator._async_persist_session_state()
        restored = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, store=store)
        await restored.async_load_session_state()

        assert store.saved[-1]["stop_pending"] is True
        assert restored._stop_pending is True
        assert restored._charging_desired is False
