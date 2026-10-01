"""Session-summary persistence and the session_completed event.

Ported from the 2.4.3 coordinator tests to the rebuild: the polling
coordinator over a fake controller, with the real ChargerStorage (HA Store)
behind it, and SessionTracker with manual clocks where exact durations
matter.

1. A restart mid-session must not lose the session's start baseline.
2. The last-completed-session record survives a restart.
3. The restored prev_status does not make the first poll look like a new
   session.
4. session_completed fires once per genuine session end, never on restore.
"""
from __future__ import annotations

import time

import pytest

from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED
from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.persistence import ChargerStorage, session_key
from custom_components.foxess_charger.session import SessionTracker

from fake_controller import FakeController

ENTRY = "sessionentry01"

LAST_SESSION = {
    "ended": "2026-01-01T00:00:00+00:00",
    "duration_min": 42.0,
    "energy_kwh": 12.34,
    "avg_power_kw": 5.0,
    "peak_power_kw": 7.3,
    "stop_reason": "command",
}


def put_session(hass_storage, **fields) -> None:
    data = {"session_start_wall": None, "session_start_total": None,
            "last_session": None, "prev_status": None,
            "stop_inhibit": False, "stop_pending": False}
    data.update(fields)
    hass_storage[session_key(ENTRY)] = {
        "version": 1, "minor_version": 1, "key": session_key(ENTRY), "data": data,
    }


def saved_session(hass_storage) -> dict:
    return hass_storage[session_key(ENTRY)]["data"]


async def make(hass, fake: FakeController, *, store: bool = True,
               entry_id: str | None = ENTRY) -> FoxESSChargerCoordinator:
    storage = None
    loaded_session: dict = {}
    loaded_energy: dict = {}
    if store:
        storage = ChargerStorage(hass, ENTRY)
        loaded = await storage.async_load()
        loaded_session, loaded_energy = loaded.session, loaded.energy
    coordinator = FoxESSChargerCoordinator(hass, fake, 10, storage=storage, entry_id=entry_id)
    coordinator.restore(loaded_session, loaded_energy)
    return coordinator


async def poll(coordinator, fake: FakeController, status: int, **hw) -> dict:
    fake.hw["status"] = status
    fake.hw.update(hw)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    return coordinator.data


class TestPersistingOnSessionTransitions:
    async def test_session_start_triggers_a_save_with_start_baseline(self, hass, hass_storage):
        fake = FakeController(status=1)
        coordinator = await make(hass, fake)
        await poll(coordinator, fake, 1)          # connected, not active
        assert saved_session(hass_storage)["session_start_wall"] is None

        data = await poll(coordinator, fake, 3)   # transitions to charging
        saved = saved_session(hass_storage)
        assert saved["session_start_wall"] is not None
        assert saved["session_start_total"] == data["total_energy_raw"]


class TestRestoringAfterRestart:
    def test_mid_session_restart_preserves_start_baseline(self):
        """Started 5 minutes (wall clock) before the restart: the session's
        duration and energy on completion count from the original start."""
        mono, wall = [100.0], [1_790_000_000.0]
        tracker = SessionTracker(clock=lambda: mono[0], wall_clock=lambda: wall[0])
        tracker.restore({"session_start_wall": wall[0] - 300,
                         "session_start_total": 1000, "prev_status": 3})
        assert tracker.active
        assert tracker.export()["session_start_wall"] == wall[0] - 300
        assert tracker.export()["session_start_total"] == 1000
        data = {"status": 5, "status_valid": True, "total_energy_raw": 1010, "power_raw": 0}
        tracker.update(data, intent_enabled=True)
        assert data["last_session"]["duration_min"] == 5.0
        assert data["last_session"]["energy_kwh"] == 1.0

    async def test_last_completed_session_survives_restart(self, hass, hass_storage):
        put_session(hass_storage, last_session=LAST_SESSION, prev_status=0)
        fake = FakeController(status=0)
        coordinator = await make(hass, fake)
        data = await poll(coordinator, fake, 0)
        assert data["last_session"] == LAST_SESSION

    async def test_no_stored_state_is_a_safe_no_op(self, hass, hass_storage):
        fake = FakeController(status=1)
        coordinator = await make(hass, fake)
        data = await poll(coordinator, fake, 1)
        assert data["session_active"] is False
        assert "last_session" not in data
        assert "session_start" not in data

    async def test_no_store_configured_is_a_safe_no_op(self, hass):
        fake = FakeController(status=1)
        coordinator = await make(hass, fake, store=False)
        await poll(coordinator, fake, 3)
        await poll(coordinator, fake, 5)
        await coordinator.async_flush()           # must not raise


class TestPrevStatusRestorationBug:
    async def test_restart_mid_session_preserves_baseline_not_reset(self, hass, hass_storage):
        start_wall = time.time() - 300
        put_session(hass_storage, session_start_wall=start_wall,
                    session_start_total=1000, prev_status=3)
        fake = FakeController(status=3)
        coordinator = await make(hass, fake)
        data = await poll(coordinator, fake, 3, total_energy_raw=1009)   # still charging
        assert "session_start" not in data
        assert data["session_active"] is True
        assert saved_session(hass_storage)["session_start_wall"] == start_wall
        assert saved_session(hass_storage)["session_start_total"] == 1000

        data = await poll(coordinator, fake, 5, total_energy_raw=1010)
        assert data["last_session"]["energy_kwh"] == 1.0
        assert data["last_session"]["duration_min"] == pytest.approx(5.0, abs=0.1)

    async def test_restart_while_inactive_creates_no_phantom_session(self, hass, hass_storage):
        put_session(hass_storage, prev_status=0)
        fake = FakeController(status=0)
        coordinator = await make(hass, fake)
        data = await poll(coordinator, fake, 0)
        assert data["session_active"] is False
        assert "session_start" not in data

    async def test_genuine_new_session_after_restoration_still_starts_normally(
        self, hass, hass_storage,
    ):
        put_session(hass_storage, prev_status=0)
        fake = FakeController(status=0)
        coordinator = await make(hass, fake)
        data = await poll(coordinator, fake, 0)
        assert data["session_active"] is False
        data = await poll(coordinator, fake, 3)   # genuine start
        assert data["session_active"] is True
        assert data["session_start"]
        assert saved_session(hass_storage)["session_start_wall"] is not None

    async def test_prev_status_is_included_in_persisted_payload(self, hass, hass_storage):
        fake = FakeController(status=3)
        coordinator = await make(hass, fake)
        await poll(coordinator, fake, 3)
        assert saved_session(hass_storage)["prev_status"] == 3


class TestSessionCompletedEvent:
    @staticmethod
    def _listen(hass) -> list:
        events: list = []
        hass.bus.async_listen(EVENT_SESSION_COMPLETED, events.append)
        return events

    async def test_genuine_session_end_fires_the_event(self, hass):
        events = self._listen(hass)
        fake = FakeController(status=1)
        coordinator = await make(hass, fake, store=False)
        await poll(coordinator, fake, 3)
        await poll(coordinator, fake, 5)
        await hass.async_block_till_done()
        assert len(events) == 1
        assert events[0].data["entry_id"] == ENTRY

    async def test_restoring_persisted_last_session_does_not_fire(self, hass, hass_storage):
        put_session(hass_storage, last_session={"duration_min": 42.0}, prev_status=0)
        events = self._listen(hass)
        fake = FakeController(status=0)
        coordinator = await make(hass, fake)
        data = await poll(coordinator, fake, 0)
        assert data["last_session"] == {"duration_min": 42.0}
        await hass.async_block_till_done()
        assert events == []

    async def test_two_consecutive_sessions_with_identical_duration_both_fire(self, hass):
        events = self._listen(hass)
        fake = FakeController(status=1)
        coordinator = await make(hass, fake, store=False)
        for _ in range(2):
            await poll(coordinator, fake, 3)
            await poll(coordinator, fake, 5)
        await hass.async_block_till_done()
        assert len(events) == 2

    async def test_no_event_without_entry_id(self, hass):
        events = self._listen(hass)
        fake = FakeController(status=1)
        coordinator = await make(hass, fake, store=False, entry_id=None)
        await poll(coordinator, fake, 3)
        await poll(coordinator, fake, 5)
        await hass.async_block_till_done()
        assert events == []
