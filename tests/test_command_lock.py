"""Deterministic concurrency tests for Task 3's command lock: proves the
actual ordering guarantees, not just that a lock object exists. Uses a fake
Modbus client whose write_holding_register can be told to block mid-call
(via threading.Event, since writes run on real HA executor-pool threads, not
the event loop) so the test can force a specific interleaving instead of
hoping the scheduler cooperates.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL, REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER,
    REG_TIME_VALIDITY, BLOCK_CONFIG, BLOCK_STATUS,
)
from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber


class BlockableFakeClient:
    """write_holding_register blocks on `gate` if `hold_next` is set for
    the matching register, and signals `entered` the instant it starts
    blocking - lets the test know execution genuinely reached the write
    (and therefore is holding the lock) before proceeding."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []
        self.hold_next: set[int] = set()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def write_holding_register(self, register: int, value: int) -> bool:
        if register in self.hold_next:
            self.hold_next.discard(register)
            self.entered.set()
            self.gate.wait(timeout=5)
            self.gate.clear()
        self.writes.append((register, value))
        return True


def make_charging_coordinator(hass, client) -> FoxESSChargerCoordinator:
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = {
        "status": 3,  # charging
        "active_faults": [], "active_alarms": [],
        "max_charging_current_raw": 100,
    }
    coordinator._mark_block_success(BLOCK_CONFIG)
    coordinator._mark_block_success(BLOCK_STATUS)
    coordinator._charging_desired = True
    coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
    return coordinator


class TestHeartbeatThenStop:
    async def test_heartbeat_write_finishes_before_stop_is_written(self, hass):
        client = BlockableFakeClient()
        client.hold_next.add(REG_MAX_CHARGING_CURRENT)
        coordinator = make_charging_coordinator(hass, client)

        heartbeat_task = asyncio.ensure_future(coordinator._heartbeat_tick())
        await hass.async_add_executor_job(client.entered.wait, 5)
        assert client.entered.is_set(), "heartbeat write never started - test setup is wrong"

        stop_task = asyncio.ensure_future(coordinator.async_send_stop())
        await asyncio.sleep(0.05)  # let stop_task genuinely start waiting on the lock
        client.gate.set()  # release the heartbeat's blocked write

        await heartbeat_task
        await stop_task

        assert client.writes == [(REG_MAX_CHARGING_CURRENT, 160), (REG_CHARGING_CONTROL, 2)]


class TestStopThenQueuedHeartbeat:
    async def test_stale_heartbeat_write_aborts_after_stop_holds_the_lock_first(self, hass):
        """Simulates the real TOCTOU: a heartbeat tick that had already
        captured `generation` and passed its top-level gates BEFORE a stop
        happened, but reaches the lock AFTER the stop already changed
        _charging_desired/_charging_generation. Calls the internal
        _async_send_setpoint directly with a pre-captured stale generation,
        rather than the full _heartbeat_tick() (whose own top-level gate
        would trivially short-circuit on _charging_desired=False before
        ever reaching the lock - the property actually being tested here
        is the re-check *inside* the lock, not the outer gate)."""
        client = BlockableFakeClient()
        client.hold_next.add(REG_CHARGING_CONTROL)
        coordinator = make_charging_coordinator(hass, client)
        stale_generation = coordinator._charging_generation

        stop_task = asyncio.ensure_future(coordinator.async_send_stop())
        await hass.async_add_executor_job(client.entered.wait, 5)
        assert client.entered.is_set()

        # Stop already holds the lock. Now issue the "stale" heartbeat
        # write - it must queue behind the lock, then see the post-stop
        # state once it gets in.
        coordinator._charging_desired = False
        coordinator._charging_generation += 1
        setpoint_task = asyncio.ensure_future(
            coordinator._async_send_setpoint(REG_MAX_CHARGING_CURRENT, 160, stale_generation)
        )
        await asyncio.sleep(0.05)
        client.gate.set()

        await stop_task
        setpoint_result = await setpoint_task

        assert setpoint_result is False
        assert client.writes == [(REG_CHARGING_CONTROL, 2)]


class TestPollDrivenWriterIsGone:
    def test_fetch_never_calls_write_holding_register(self, hass):
        """_reassert_setpoints() is deleted - _fetch() must never write to
        the charger at all, only read. Regression guard against it (or
        anything like it) being reintroduced."""
        client = MagicMock()
        client.read_registers.return_value = None
        client.read_ascii.return_value = None
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}

        try:
            coordinator._fetch()
        except Exception:
            pass  # a real read failure raising UpdateFailed is fine/expected here
        client.write_holding_register.assert_not_called()

    def test_reassert_setpoints_method_no_longer_exists(self):
        assert not hasattr(FoxESSChargerCoordinator, "_reassert_setpoints")


class TestUnloadWaitsForInFlightTransaction:
    async def test_stop_heartbeat_blocks_until_a_held_lock_is_released(self, hass):
        client = BlockableFakeClient()
        client.hold_next.add(REG_MAX_CHARGING_CURRENT)
        coordinator = make_charging_coordinator(hass, client)

        write_task = asyncio.ensure_future(
            coordinator._async_send_setpoint(REG_MAX_CHARGING_CURRENT, 160, coordinator._charging_generation)
        )
        await hass.async_add_executor_job(client.entered.wait, 5)

        stop_heartbeat_task = asyncio.ensure_future(coordinator.async_stop_heartbeat())
        await asyncio.sleep(0.05)
        assert not stop_heartbeat_task.done(), "async_stop_heartbeat must not return while the lock is held"

        client.gate.set()
        await write_task
        await stop_heartbeat_task
        assert stop_heartbeat_task.done()
        assert coordinator._shutting_down is True


class TestUserSetpointWriteThenStop:
    """number.py's user-initiated setpoint writes (max_charging_current/
    max_charging_power) route through async_set_desired_setpoint, sharing
    _command_lock with start/stop/heartbeat - the same race class closed for
    those three entry points, closed here for the fourth."""

    async def test_setpoint_write_finishes_before_a_concurrent_stop_is_written(self, hass):
        client = BlockableFakeClient()
        client.hold_next.add(REG_MAX_CHARGING_CURRENT)
        coordinator = make_charging_coordinator(hass, client)

        setpoint_task = asyncio.ensure_future(
            coordinator.async_set_desired_setpoint(REG_MAX_CHARGING_CURRENT, 160)
        )
        await hass.async_add_executor_job(client.entered.wait, 5)
        assert client.entered.is_set(), "setpoint write never started - test setup is wrong"

        stop_task = asyncio.ensure_future(coordinator.async_send_stop())
        await asyncio.sleep(0.05)  # let stop_task genuinely start waiting on the lock
        client.gate.set()  # release the blocked setpoint write

        setpoint_result = await setpoint_task
        stop_result = await stop_task

        assert setpoint_result is True, (
            "the write was already in flight (desired was still True when "
            "it passed its gate check) before the concurrent stop even "
            "requested the lock - it must complete, not be aborted mid-flight"
        )
        assert stop_result is True
        assert client.writes == [(REG_MAX_CHARGING_CURRENT, 160), (REG_CHARGING_CONTROL, 2)]


class TestStopThenUserSetpointWrite:
    """2026-09-18 real incident fix: a setpoint write queued behind a stop
    that already holds the lock must NOT apply once the lock frees - it
    must see the post-stop _charging_desired=False and skip the physical
    write entirely (still recording the value for next session). The old
    behaviour (apply anyway, exempt from the desired-state gate) was
    exactly the mechanism that let an automation's unconditional
    number.set_value silently resume a session that had just been stopped
    for home-battery protection - writing these registers is itself an
    implicit "resume charging" on this firmware."""

    async def test_setpoint_write_is_saved_but_never_reaches_the_wire_after_a_stop_that_holds_the_lock_first(self, hass):
        client = BlockableFakeClient()
        client.hold_next.add(REG_CHARGING_CONTROL)
        coordinator = make_charging_coordinator(hass, client)

        stop_task = asyncio.ensure_future(coordinator.async_send_stop())
        await hass.async_add_executor_job(client.entered.wait, 5)
        assert client.entered.is_set(), "stop write never started - test setup is wrong"

        # Stop already holds the lock, and (as switch.py's async_turn_off
        # does as its own first, lock-free action) has already cleared
        # _charging_desired before even trying to acquire it.
        coordinator._charging_desired = False
        setpoint_task = asyncio.ensure_future(
            coordinator.async_set_desired_setpoint(REG_MAX_CHARGING_CURRENT, 160)
        )
        await asyncio.sleep(0.05)
        client.gate.set()  # release the blocked stop write

        stop_result = await stop_task
        setpoint_result = await setpoint_task

        assert stop_result is True
        assert setpoint_result is None, (
            "a setpoint write must be skipped (not an error, not applied) "
            "once _charging_desired is False - this is the exact "
            "2026-09-18 incident mechanism, now closed"
        )
        assert client.writes == [(REG_CHARGING_CONTROL, 2)], (
            "the setpoint must never reach the charger after a stop"
        )
        assert coordinator.desired_setpoints[REG_MAX_CHARGING_CURRENT] == 160, (
            "the value must still be recorded, ready for the next session"
        )


class TestStartAndStopOverlap:
    """2026-09-18 start-race fix: async_send_start now captures the
    generation and decides whether to set _charging_desired=True INSIDE
    the lock, immediately after its own write - not in switch.py, after
    the lock was already released. Closes a real race: switch.py's
    async_turn_off clears _charging_desired and bumps the generation as
    its own synchronous, lock-free first actions, so it can run those the
    moment a concurrent start's write yields to the executor - i.e.
    potentially before that start's write even completes. Setting the
    desired flag back in switch.py, after async_send_start returned, would
    unconditionally stomp turn_off's False back to True with no way to
    tell a stop had just been requested."""

    async def test_a_stop_landing_during_an_in_flight_start_prevents_desired_from_being_set(self, hass):
        client = BlockableFakeClient()
        client.hold_next.add(REG_CHARGING_CONTROL)
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 5, "active_faults": [], "active_alarms": []}
        coordinator._charging_desired = False

        start_task = asyncio.ensure_future(coordinator.async_send_start())
        await hass.async_add_executor_job(client.entered.wait, 5)
        assert client.entered.is_set(), "start write never started - test setup is wrong"

        # Simulate switch.py's async_turn_off landing concurrently while
        # the start's write is still in flight.
        coordinator._charging_desired = False
        coordinator._charging_generation += 1
        stop_task = asyncio.ensure_future(coordinator.async_send_stop())

        client.gate.set()  # release the blocked start write
        start_result = await start_task
        stop_result = await stop_task

        assert start_result is False, "a concurrent Stop must cancel the Start result"
        assert stop_result is True
        assert client.writes == [(REG_CHARGING_CONTROL, 1), (REG_CHARGING_CONTROL, 2)]
        assert coordinator._charging_desired is False, (
            "start must not stomp _charging_desired back to True once the "
            "generation proves a stop landed during its in-flight write"
        )

    async def test_start_ack_sets_desired_but_waits_for_status_before_clearing_inhibit(self, hass):
        client = BlockableFakeClient()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 5, "active_faults": [], "active_alarms": []}
        coordinator._charging_desired = False
        coordinator._stop_inhibit = True

        result = await coordinator.async_send_start()

        assert result is True
        assert coordinator._charging_desired is True
        assert coordinator._stop_inhibit is True


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


class TestNumberEntityRoutesReassertedRegistersThroughTheLock:
    """Unit tests against FoxESSNumber itself (not just the coordinator
    method) - confirms async_set_native_value actually calls the new
    coordinator method for the two in-scope registers, and leaves the other
    four entities' write path completely untouched."""

    async def test_reasserted_register_calls_coordinator_method_not_the_direct_client(self, hass):
        client = MagicMock()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"max_charging_current_raw": 100}
        coordinator.async_set_desired_setpoint = AsyncMock(return_value=True)
        coordinator.async_request_refresh = AsyncMock()

        desc = next(d for d in NUMBERS if d.key == "max_charging_current")
        entity = FoxESSNumber(coordinator, client, desc, make_entry())
        entity.hass = hass
        entity.async_write_ha_state = MagicMock()

        with patch("custom_components.foxess_charger.number.asyncio.sleep", AsyncMock()):
            await entity.async_set_native_value(16.0)  # raw=160

        coordinator.async_set_desired_setpoint.assert_awaited_once_with(REG_MAX_CHARGING_CURRENT, 160)
        client.write_holding_register.assert_not_called()

    async def test_non_reasserted_register_still_writes_directly_bypassing_the_coordinator(self, hass):
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"time_validity": 60}
        coordinator.async_set_desired_setpoint = AsyncMock(return_value=True)
        coordinator.async_request_refresh = AsyncMock()

        desc = next(d for d in NUMBERS if d.key == "time_validity")
        entity = FoxESSNumber(coordinator, client, desc, make_entry())
        entity.hass = hass
        entity.async_write_ha_state = MagicMock()

        with patch("custom_components.foxess_charger.number.asyncio.sleep", AsyncMock()):
            await entity.async_set_native_value(30)

        client.write_holding_register.assert_called_once_with(REG_TIME_VALIDITY, 30)
        coordinator.async_set_desired_setpoint.assert_not_called()


class TestReassertedRegisterSkippedWhenNotCharging:
    """2026-09-18 real incident, reproduced and closed: an automation's
    unconditional number.set_value(max_charging_power, 7.3) landing while
    charge_mode was "Paused - Battery Protect" (_charging_desired already
    False) must save the value but never touch the wire - the exact
    mechanism that silently resumed a stopped session for 52 minutes."""

    async def test_setpoint_saved_but_not_written_while_stopped(self, hass):
        client = MagicMock()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"max_charging_power_raw": 73}
        coordinator._charging_desired = False  # the exact incident precondition

        desc = next(d for d in NUMBERS if d.key == "max_charging_power")
        entity = FoxESSNumber(coordinator, client, desc, make_entry())
        entity.hass = hass
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(7.3)  # raw=73

        client.write_holding_register.assert_not_called()
        assert coordinator.desired_setpoints[REG_MAX_CHARGING_POWER] == 73
        assert entity.native_value == 7.3, (
            "the entity must show the saved intent, not the stale live "
            "register value, while the write is deferred"
        )

    async def test_the_incident_scenario_end_to_end_cannot_resume_charging(self, hass):
        """The precise 20:26/21:00 sequence: paused for battery protection,
        then an unconditional end-of-window write at full power - must not
        touch the charger at all."""
        client = MagicMock()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"status": 5, "max_charging_power_raw": 73}  # "finished", not charging
        coordinator._charging_desired = False
        coordinator._stop_inhibit = True

        desc = next(d for d in NUMBERS if d.key == "max_charging_power")
        entity = FoxESSNumber(coordinator, client, desc, make_entry())
        entity.hass = hass
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(7.3)

        client.write_holding_register.assert_not_called()
        assert coordinator._charging_desired is False
        assert coordinator._stop_inhibit is True
