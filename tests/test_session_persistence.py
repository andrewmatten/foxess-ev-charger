"""Tests for the 2.2.0 session-summary persistence change.

Session start/end/duration/energy/peak-power tracking already lived in the
coordinator's instance attributes and coordinator.data - none of it survived
an HA restart. This adds a Store-backed save/restore path:

1. A restart mid-session must not lose the session's start timestamp/
   baseline - the eventual "session ended" computation (duration, energy
   delta) needs both to still be correct.
2. The last-completed-session record must survive a restart too, so Last
   Session Energy/Duration don't go blank just because HA restarted.

The Store itself is mocked (AsyncMock) rather than hitting real disk -
these tests are about the coordinator's own save/restore logic, not HA
core's Store implementation.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from custom_components.foxess_charger import FoxESSChargerCoordinator
from tests.test_coordinator_batching import make_mock_client, make_status_block_registers


def make_store(loaded: dict | None = None) -> MagicMock:
    store = MagicMock()
    store.async_load = AsyncMock(return_value=loaded)
    store.async_save = AsyncMock()
    return store


class TestPersistingOnSessionTransitions:
    async def test_session_start_triggers_a_save_with_start_baseline(self, hass):
        client = make_mock_client()
        store = make_store()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, store=store)

        # First fetch: status=connected (1), not yet active - establishes
        # _prev_status so the next fetch's transition into "charging" (3)
        # is actually detected as a start.
        coordinator.data = coordinator._fetch()
        store.async_save.assert_not_called()

        # Second fetch: charger transitions to charging.
        client.read_registers.side_effect = None

        def _charging_regs(address, count, quiet=False):
            from custom_components.foxess_charger.const import (
                REG_STATUS_BLOCK_COUNT, REG_STATUS_BLOCK_START,
            )
            from tests.test_coordinator_batching import make_status_block_registers
            if (address, count) == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT):
                regs = make_status_block_registers()
                regs[3] = 3  # status = charging
                return regs
            if (address, count) == (0x3000, 7):
                return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
            return None

        client.read_registers.side_effect = _charging_regs
        data = coordinator._fetch()
        assert coordinator._session_state_dirty is True

        await coordinator._async_persist_session_state()

        store.async_save.assert_called_once()
        saved = store.async_save.call_args.args[0]
        assert saved["session_start_wall"] is not None
        assert saved["session_start_total"] == data["total_energy_raw"]


class TestRestoringAfterRestart:
    async def test_mid_session_restart_preserves_start_baseline(self, hass):
        """A session started 5 minutes ago (wall-clock) before the
        (simulated) restart - the coordinator must recompute a monotonic
        start offset so duration math stays correct once the session ends."""
        start_wall = time.time() - 300  # started 5 minutes ago
        store = make_store({
            "session_start_wall": start_wall,
            "session_start_total": 1000,
            "last_session": None,
        })
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, store=store
        )

        await coordinator.async_load_session_state()

        assert coordinator._session_start_total == 1000
        assert coordinator._session_start_wall == start_wall
        # Recomputed monotonic start must be ~300s in the past.
        elapsed = time.monotonic() - coordinator._session_start_ts
        assert 295 <= elapsed <= 305

    async def test_last_completed_session_survives_restart(self, hass):
        persisted_last_session = {
            "ended": "2026-01-01T00:00:00+00:00",
            "duration_min": 42.0,
            "energy_kwh": 12.34,
            "avg_power_kw": 5.0,
            "peak_power_kw": 7.3,
            "stop_reason": "command",
        }
        store = make_store({
            "session_start_wall": None,
            "session_start_total": None,
            "last_session": persisted_last_session,
        })
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, store=store)

        await coordinator.async_load_session_state()
        # Simulate the first fetch after restart (self.data is still None).
        data = coordinator._fetch()

        assert data["last_session"] == persisted_last_session

    async def test_no_stored_state_is_a_safe_no_op(self, hass):
        """Fresh install / never-charged - Store.async_load() returns None,
        must not crash and must leave everything at its normal defaults."""
        store = make_store(loaded=None)
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10, store=store)

        await coordinator.async_load_session_state()

        assert coordinator._session_start_ts is None
        assert coordinator._last_completed_session is None

    async def test_no_store_configured_is_a_safe_no_op(self, hass):
        """store=None (the default) - most existing tests construct the
        coordinator this way - persistence is simply skipped."""
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)

        await coordinator.async_load_session_state()  # must not raise
        await coordinator._async_persist_session_state()  # must not raise


class TestPrevStatusRestorationBug:
    """2026-09 (second audit): _prev_status used to be restored as None
    after a restart, regardless of what was actually persisted - so the
    very first post-restart poll always looked like a brand new transition
    into an active status, clobbering the just-restored start baseline back
    to "now" even though the session had actually been running for a while.
    """

    async def _fetch_with_status(self, client, status: int) -> dict:
        from custom_components.foxess_charger.const import (
            REG_STATUS_BLOCK_COUNT, REG_STATUS_BLOCK_START,
        )
        regs = make_status_block_registers()
        regs[3] = status

        def _side_effect(address, count, quiet=False):
            if (address, count) == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT):
                return regs
            if (address, count) == (0x3000, 7):
                return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
            return None

        client.read_registers.side_effect = _side_effect

    async def test_restart_mid_session_preserves_baseline_not_reset(self, hass):
        """prev_status is restored as the last-persisted active status
        (charging=3) - the first poll after restart, which still reports an
        active status, must NOT be read as a fresh session start."""
        start_wall = time.time() - 300  # started 5 minutes ago
        store = make_store({
            "session_start_wall": start_wall,
            "session_start_total": 1000,
            "last_session": None,
            "prev_status": 3,  # charging - was active at last persist
        })
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, store=store)

        await coordinator.async_load_session_state()
        assert coordinator._prev_status == 3
        restored_start_ts = coordinator._session_start_ts
        restored_start_total = coordinator._session_start_total

        await self._fetch_with_status(client, status=3)  # still charging
        coordinator.data = coordinator._fetch()

        # Baseline must be untouched - NOT reset to "just now".
        assert coordinator._session_start_ts == restored_start_ts
        assert coordinator._session_start_total == restored_start_total
        assert "session_start" not in coordinator.data

    async def test_restart_while_inactive_creates_no_phantom_session(self, hass):
        """prev_status restored as an inactive status (idle=0) - the first
        poll after restart, also inactive, must not start a session."""
        store = make_store({
            "session_start_wall": None,
            "session_start_total": None,
            "last_session": None,
            "prev_status": 0,  # idle
        })
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, store=store)

        await coordinator.async_load_session_state()
        assert coordinator._prev_status == 0

        await self._fetch_with_status(client, status=0)  # still idle
        coordinator.data = coordinator._fetch()

        assert coordinator._session_start_ts is None
        assert "session_start" not in coordinator.data

    async def test_genuine_new_session_after_restoration_still_starts_normally(self, hass):
        """Restored while inactive, then a REAL later transition into an
        active status must still be recognised as a new session start - the
        restoration fix must not suppress future genuine transitions."""
        store = make_store({
            "session_start_wall": None,
            "session_start_total": None,
            "last_session": None,
            "prev_status": 0,  # idle
        })
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, store=store)

        await coordinator.async_load_session_state()

        await self._fetch_with_status(client, status=0)  # still idle
        coordinator.data = coordinator._fetch()
        assert coordinator._session_start_ts is None

        await self._fetch_with_status(client, status=3)  # genuine start
        data = coordinator._fetch()

        assert coordinator._session_start_ts is not None
        assert data["session_start"]
        assert coordinator._session_state_dirty is True

    async def test_prev_status_is_included_in_persisted_payload(self, hass):
        client = make_mock_client()
        store = make_store()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10, store=store)
        coordinator._prev_status = 3

        await coordinator._async_persist_session_state()

        saved = store.async_save.call_args.args[0]
        assert saved["prev_status"] == 3


class TestSessionCompletedEvent:
    """2026-09 (second audit): device_trigger.py's session_completed trigger
    used to key off any change in the Last Session Duration sensor's state -
    indistinguishable from a value merely being restored/written for the
    first time this process. The coordinator now fires a real internal event
    only at the exact moment a genuine session ends (see _track_session)."""

    async def _fetch_with_status(self, client, status: int) -> dict:
        from custom_components.foxess_charger.const import (
            REG_STATUS_BLOCK_COUNT, REG_STATUS_BLOCK_START,
        )
        regs = make_status_block_registers()
        regs[3] = status

        def _side_effect(address, count, quiet=False):
            if (address, count) == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT):
                return regs
            if (address, count) == (0x3000, 7):
                return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
            return None

        client.read_registers.side_effect = _side_effect

    async def test_genuine_session_end_fires_the_event(self, hass):
        from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED

        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(
            hass, client, scan_interval=10, entry_id="test_entry",
        )
        events = []
        hass.bus.async_listen(EVENT_SESSION_COMPLETED, lambda e: events.append(e))

        await self._fetch_with_status(client, status=3)  # start
        coordinator.data = coordinator._fetch()
        await self._fetch_with_status(client, status=5)  # finished - real end
        coordinator.data = coordinator._fetch()
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0].data["entry_id"] == "test_entry"

    async def test_restoring_persisted_last_session_does_not_fire(self, hass):
        """Seeding coordinator.data["last_session"] from storage (see
        _fetch()'s re-seed-on-restart branch) never goes through
        _track_session()'s transition logic, so it must never fire the
        event - simulated here by fetching while status stays inactive the
        whole time, with a last-completed-session already on hand."""
        from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED

        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(
            hass, client, scan_interval=10, entry_id="test_entry",
        )
        coordinator._last_completed_session = {"duration_min": 42.0}
        events = []
        hass.bus.async_listen(EVENT_SESSION_COMPLETED, lambda e: events.append(e))

        await self._fetch_with_status(client, status=0)  # idle, no transition
        coordinator.data = coordinator._fetch()
        assert coordinator.data["last_session"] == {"duration_min": 42.0}
        await hass.async_block_till_done()

        assert events == []

    async def test_two_consecutive_sessions_with_identical_duration_both_fire(self, hass):
        """The bug a plain state-diff trigger had: two real sessions with
        the same (rounded) duration produce no state change at all. Keying
        off the transition itself (not a value comparison) means both still
        fire here."""
        from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED

        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(
            hass, client, scan_interval=10, entry_id="test_entry",
        )
        events = []
        hass.bus.async_listen(EVENT_SESSION_COMPLETED, lambda e: events.append(e))

        for _ in range(2):
            await self._fetch_with_status(client, status=3)  # start
            coordinator.data = coordinator._fetch()
            await self._fetch_with_status(client, status=5)  # end
            coordinator.data = coordinator._fetch()
        await hass.async_block_till_done()

        assert len(events) == 2

    async def test_no_event_without_entry_id(self, hass):
        """Most existing tests construct the coordinator without entry_id
        (e.g. calling _fetch() directly, not through async_setup_entry) -
        must not raise, and must simply skip firing."""
        from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED

        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        events = []
        hass.bus.async_listen(EVENT_SESSION_COMPLETED, lambda e: events.append(e))

        await self._fetch_with_status(client, status=3)
        coordinator.data = coordinator._fetch()
        await self._fetch_with_status(client, status=5)
        coordinator.data = coordinator._fetch()
        await hass.async_block_till_done()

        assert events == []
