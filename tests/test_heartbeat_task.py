"""Tests for the background heartbeat task added alongside the adaptive
interval math from `get_heartbeat_interval()` (const.py, commit 7bcda26).

That earlier commit made the re-assertion *interval* adaptive but left the
check running only inside `FoxESSChargerCoordinator._fetch()`, which only
ever runs once per poll cycle (~10-13s, DEFAULT_SCAN_INTERVAL). If Command
Time Validity (0x3005) were ever configured at its own documented minimum
(10s), the required heartbeat (5s) can't be met by something that only
checks once per poll.

This adds a real `asyncio.Task` on the coordinator (`async_start_heartbeat`/
`async_stop_heartbeat`/`_heartbeat_loop`/`_heartbeat_tick` in __init__.py),
decoupled from the poll cycle entirely. Background heartbeat task design
adapted from a third-party PR by github.com/loadrunner42 (PR #2 on
andrewmatten/foxess-ev-charger), in turn based on evcc-io/evcc's
foxess-evc.go driver convention - see CHANGELOG.md and __init__.py's
_heartbeat_loop docstring.

`tests/test_heartbeat_interval.py` already covers the pure interval math
(`get_heartbeat_interval()` itself) - these tests are about the *task*
actually using that value correctly, not re-testing the pure function.
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.foxess_charger as integration
from custom_components.foxess_charger import (
    FoxESSChargerCoordinator,
    async_unload_entry,
)
from custom_components.foxess_charger.const import (
    BLOCK_CONFIG,
    BLOCK_STATUS,
    DOMAIN,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    get_heartbeat_interval,
)
from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber
from custom_components.foxess_charger.switch import FoxESSChargingSwitch


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def make_coordinator(hass, client, initial_data: dict) -> FoxESSChargerCoordinator:
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = dict(initial_data)
    return coordinator


async def _wait_until(predicate, timeout: float = 1.0, interval: float = 0.005) -> None:
    """Polls `predicate` until it's truthy, instead of a fixed sleep - keeps
    these tests fast on a quiet machine and not flaky on a loaded one."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


class TestIntervalUsage:
    """The task must feed the *current* time_validity through
    get_heartbeat_interval() on every iteration, not just once at startup."""

    @pytest.mark.parametrize("time_validity", [10, 60, 180])
    async def test_loop_computes_interval_from_current_time_validity(
        self, hass, monkeypatch, time_validity,
    ):
        calls = []

        def _spy(tv):
            calls.append(tv)
            return 0.01  # keep the loop fast regardless of the real math

        monkeypatch.setattr(integration, "get_heartbeat_interval", _spy)

        coordinator = make_coordinator(hass, MagicMock(), {"time_validity": time_validity})

        await coordinator.async_start_heartbeat()
        try:
            await _wait_until(lambda: len(calls) >= 2)
        finally:
            await coordinator.async_stop_heartbeat()

        assert all(c == time_validity for c in calls)


class TestRescheduleOnTimeValidityChange:
    async def test_mid_wait_change_wakes_early_and_reschedules(self, hass, monkeypatch):
        """Starts a wait computed from time_validity=180 (90s, per the real
        get_heartbeat_interval - comfortably longer than this test's real
        budget), then changes time_validity and wakes the loop the same way
        _async_update_data does. The loop must recompute against the new
        value on its very next iteration, not finish out the 90s wait."""
        calls = []

        def _spy(tv):
            calls.append(tv)
            return get_heartbeat_interval(tv)

        monkeypatch.setattr(integration, "get_heartbeat_interval", _spy)

        coordinator = make_coordinator(hass, MagicMock(), {"time_validity": 180})

        await coordinator.async_start_heartbeat()
        await asyncio.sleep(0)  # let the loop reach its first wait
        assert calls == [180]

        coordinator.data = {"time_validity": 10}
        coordinator._wake_heartbeat()

        try:
            await _wait_until(lambda: len(calls) >= 2)
        finally:
            await coordinator.async_stop_heartbeat()

        assert calls == [180, 10]


class TestImmediatePush:
    """Waking the loop must actually cause a write, not just prove the
    event was set."""

    async def test_charging_start_pushes_the_desired_setpoint_immediately(self, hass):
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {"status": 0})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._mark_block_success(BLOCK_CONFIG)
        # P0 fix: _heartbeat_tick now also requires BLOCK_STATUS to be
        # fresh (see TestFreshnessGate below) - marked fresh here too so
        # these tests keep exercising what they were written for (the
        # immediate-push/write-failure behavior) rather than incidentally
        # tripping over the new gate.
        coordinator._mark_block_success(BLOCK_STATUS)
        coordinator.async_request_refresh = AsyncMock()

        await coordinator.async_start_heartbeat()
        try:
            entity = FoxESSChargingSwitch(coordinator, client, make_entry())
            entity.hass = hass
            entity.async_write_ha_state = MagicMock()

            with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
                await entity.async_turn_on()

            await _wait_until(
                lambda: any(
                    c.args == (REG_MAX_CHARGING_CURRENT, 160)
                    for c in client.write_holding_register.call_args_list
                )
            )
        finally:
            await coordinator.async_stop_heartbeat()

    async def test_setpoint_change_pushes_immediately_while_charging(self, hass):
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {
            "status": 3, "max_charging_current_raw": 100,
        })
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        # P0 fix: _heartbeat_tick now also requires BLOCK_STATUS to be
        # fresh (see TestFreshnessGate below) - marked fresh here too so
        # these tests keep exercising what they were written for (the
        # immediate-push/write-failure behavior) rather than incidentally
        # tripping over the new gate.
        coordinator._mark_block_success(BLOCK_STATUS)
        coordinator.async_request_refresh = AsyncMock()

        await coordinator.async_start_heartbeat()
        try:
            desc = next(d for d in NUMBERS if d.key == "max_charging_current")
            entity = FoxESSNumber(coordinator, client, desc, make_entry())
            entity.hass = hass
            entity.async_write_ha_state = MagicMock()

            with patch("custom_components.foxess_charger.number.asyncio.sleep", AsyncMock()):
                await entity.async_set_native_value(16.0)  # raw=160

            # The number entity's own write plus the heartbeat's immediate
            # push both land on this register/value - two writes, not one.
            await _wait_until(
                lambda: sum(
                    1 for c in client.write_holding_register.call_args_list
                    if c.args == (REG_MAX_CHARGING_CURRENT, 160)
                ) >= 2
            )
        finally:
            await coordinator.async_stop_heartbeat()


class TestStopRace:
    async def test_turn_off_clears_desired_flag_before_a_tick_can_repush(self, hass):
        """Confirms the ordering guarantee: async_turn_off clears
        _charging_desired as its very first statement (before the stop
        write is even sent), so any heartbeat tick that runs at or after
        that point sees the flag already cleared and does not re-push the
        charge-limit registers - which per this firmware's documented
        behavior is itself an implicit 'resume charging' command."""
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {"status": 3})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        # P0 fix: _heartbeat_tick now also requires BLOCK_STATUS to be
        # fresh (see TestFreshnessGate below) - marked fresh here too so
        # these tests keep exercising what they were written for (the
        # immediate-push/write-failure behavior) rather than incidentally
        # tripping over the new gate.
        coordinator._mark_block_success(BLOCK_STATUS)
        coordinator.async_request_refresh = AsyncMock()

        entity = FoxESSChargingSwitch(coordinator, client, make_entry())
        entity.hass = hass
        entity.async_write_ha_state = MagicMock()

        with patch("custom_components.foxess_charger.switch.asyncio.sleep", AsyncMock()):
            await entity.async_turn_off()

        assert coordinator._charging_desired is False
        client.write_holding_register.reset_mock()

        # A tick landing right after must be a no-op now.
        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_not_called()


class TestFreshnessGate:
    async def test_stale_config_block_prevents_a_push(self, hass):
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {"status": 3})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        # Deliberately never marked fresh.

        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_not_called()

    async def test_stale_status_block_prevents_a_push_even_if_config_is_fresh(self, hass):
        """P0 audit fix: the gate used to check BLOCK_CONFIG's freshness
        only - whether charging is currently active/desired is a *status*
        block decision, so a stale status block reporting an old "still
        charging" reading must not be trusted to justify a write just
        because the unrelated config block happens to still be fresh."""
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {"status": 3})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        # BLOCK_STATUS deliberately never marked fresh.

        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_not_called()

    async def test_active_fault_suppresses_a_push_even_with_both_blocks_fresh(self, hass):
        """P0 audit fix: re-asserting a charge-limit setpoint while the
        charger has flagged a fault is not something this feature should
        do blindly."""
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {
            "status": 3, "active_faults": ["overcurrent"], "active_alarms": [],
        })
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        coordinator._mark_block_success(BLOCK_STATUS)

        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_not_called()

    async def test_active_alarm_suppresses_a_push_even_with_both_blocks_fresh(self, hass):
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {
            "status": 3, "active_faults": [], "active_alarms": ["phase_loss"],
        })
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        coordinator._mark_block_success(BLOCK_STATUS)

        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_not_called()

    async def test_no_fault_or_alarm_allows_a_push_with_both_blocks_fresh(self, hass):
        """Sanity check paired with the two tests above: it's specifically
        an active fault/alarm that suppresses the push, not the mere
        presence of the keys or some other side effect of this test setup."""
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {
            "status": 3, "active_faults": [], "active_alarms": [],
        })
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        coordinator._mark_block_success(BLOCK_STATUS)

        await coordinator._heartbeat_tick()

        client.write_holding_register.assert_called_once_with(REG_MAX_CHARGING_CURRENT, 160)


class TestGenerationTokenRace:
    """The stop-race fix (TestStopRace above) closes the gap where a tick
    hasn't started writing yet at all. This covers the narrower TOCTOU gap
    inside an already-in-flight tick: _charging_desired could still read
    True at the very top of _heartbeat_tick, then flip to False (a stop
    lands) while this tick is suspended awaiting the executor job for a
    register write - checking the flag only once, at the top, misses that."""

    async def test_write_that_passed_its_pre_write_check_still_applies_even_if_state_changes_during_the_await(self, hass):
        """Task 3 changed this scenario's correct outcome. Before the
        command lock existed, a stop landing while this write's executor
        job was in flight had to un-apply the write's side effects on
        return, because the write itself could otherwise race an
        independently-dispatched stop write on the wire in either order.

        With the command lock, that physical race is closed structurally:
        a stop's own register write (async_send_stop) must acquire the same
        lock this write is already holding, so it can only land *after*
        this one completes - never interleaved with it. A stop's plain
        attribute mutations (_charging_desired/_charging_generation, set
        directly on the event loop, not gated by the lock - see
        async_turn_off's comment in switch.py) can still happen mid-await,
        as simulated here, but that no longer makes this already-in-flight
        write's own side effects unsafe to apply: the physical write it
        represents already reached the charger, and the queued stop is
        guaranteed to run right after it, so the final commanded state is
        still correct (stopped). See _async_send_setpoint's docstring in
        __init__.py - the re-check now happens once, immediately before the
        write, inside the lock; there is deliberately no second re-check
        after.
        """
        client = MagicMock()
        coordinator = make_coordinator(hass, client, {"status": 3, "max_charging_current_raw": 100})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        coordinator._mark_block_success(BLOCK_STATUS)

        def _slow_write(fn, *args):
            # Simulates a stop's own state mutations landing while this
            # write's executor job is still in flight - only the stop's own
            # *register write* is serialized behind this one by the lock;
            # its plain attribute mutations aren't lock-gated and can still
            # interleave here.
            coordinator._charging_desired = False
            coordinator._charging_generation += 1
            return True

        with patch.object(
            hass, "async_add_executor_job", AsyncMock(side_effect=_slow_write),
        ):
            await coordinator._heartbeat_tick()

        assert coordinator.data.get("max_charging_current_raw") == 160
        assert coordinator.setpoint_reasserts == 1

    async def test_stop_landing_before_a_later_register_prevents_that_writes_start(self, hass):
        """Two registers in desired_setpoints - the stop lands after the
        first write completes, so the second must never even start."""
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {"status": 3})
        coordinator.desired_setpoints = {
            REG_MAX_CHARGING_CURRENT: 160,
            REG_MAX_CHARGING_POWER: 50,
        }
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        coordinator._mark_block_success(BLOCK_STATUS)

        call_count = 0
        real_executor_job = hass.async_add_executor_job

        async def _job(fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Stop lands right after the first register's write starts
                # returning, before the loop reaches the second register.
                coordinator._charging_desired = False
                coordinator._charging_generation += 1
            return await real_executor_job(fn, *args)

        with patch.object(hass, "async_add_executor_job", side_effect=_job):
            await coordinator._heartbeat_tick()

        assert call_count == 1
        assert client.write_holding_register.call_count == 1


class TestDictMutationDuringIteration:
    async def test_desired_setpoints_mutated_mid_tick_does_not_crash(self, hass):
        """number.py's async_set_native_value can mutate desired_setpoints
        from a concurrent write while a heartbeat tick is suspended
        awaiting a register write - the tick must snapshot the dict rather
        than iterate the live one directly."""
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = make_coordinator(hass, client, {"status": 3})
        coordinator.desired_setpoints = {
            REG_MAX_CHARGING_CURRENT: 160,
            REG_MAX_CHARGING_POWER: 50,
        }
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        coordinator._mark_block_success(BLOCK_STATUS)

        real_executor_job = hass.async_add_executor_job
        call_count = 0

        async def _job(fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Mutates the dict (adds a new entry) while the tick's loop
                # is suspended on this await - would raise "dictionary
                # changed size during iteration" if the loop weren't
                # iterating a snapshot.
                coordinator.desired_setpoints[0x9999] = 1
            return await real_executor_job(fn, *args)

        with patch.object(hass, "async_add_executor_job", side_effect=_job):
            await coordinator._heartbeat_tick()  # must not raise

        assert client.write_holding_register.call_count == 2


class TestUnloadCancellation:
    async def test_stop_heartbeat_cancels_and_awaits_the_task(self, hass):
        coordinator = make_coordinator(hass, MagicMock(), {"time_validity": 60})

        await coordinator.async_start_heartbeat()
        task = coordinator._heartbeat_task
        assert task is not None and not task.done()

        await coordinator.async_stop_heartbeat()

        assert task.cancelled() is True
        assert coordinator._heartbeat_task is None

    async def test_stop_heartbeat_is_a_safe_no_op_when_never_started(self, hass):
        coordinator = make_coordinator(hass, MagicMock(), {})
        await coordinator.async_stop_heartbeat()  # must not raise

    async def test_unload_entry_stops_heartbeat_before_disconnecting_client(self, hass):
        call_order = []

        client = MagicMock()
        client.disconnect = MagicMock(side_effect=lambda: call_order.append("disconnect"))
        coordinator = MagicMock()
        coordinator.async_stop_heartbeat = AsyncMock(
            side_effect=lambda: call_order.append("stop_heartbeat")
        )
        # Task 2 (energy baseline persistence) added an unconditional flush
        # between stop_heartbeat and disconnect - MagicMock() doesn't
        # auto-generate awaitable attributes without a spec, so this needs
        # an explicit AsyncMock like async_stop_heartbeat above.
        coordinator.async_flush_energy_state = AsyncMock(
            side_effect=lambda: call_order.append("flush_energy_state")
        )

        entry = MockConfigEntry(domain=DOMAIN, data={})
        entry.add_to_hass(hass)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "coordinator": coordinator, "client": client,
        }

        with patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ):
            result = await async_unload_entry(hass, entry)

        assert result is True
        assert call_order == ["stop_heartbeat", "flush_energy_state", "disconnect"]


class TestWriteFailureResilience:
    async def test_failed_write_is_logged_and_does_not_raise(self, hass, caplog):
        client = MagicMock()
        client.write_holding_register.return_value = False
        coordinator = make_coordinator(hass, client, {"status": 3})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        # P0 fix: _heartbeat_tick now also requires BLOCK_STATUS to be
        # fresh (see TestFreshnessGate below) - marked fresh here too so
        # these tests keep exercising what they were written for (the
        # immediate-push/write-failure behavior) rather than incidentally
        # tripping over the new gate.
        coordinator._mark_block_success(BLOCK_STATUS)

        await coordinator._heartbeat_tick()  # must not raise

        # Task 3: the failure-log message moved into _async_send_setpoint,
        # the primitive now shared by the heartbeat, start, and stop write
        # paths - it's deliberately generic ("Command write failed") rather
        # than heartbeat-specific, since the same code path also serves
        # start/stop. The behavior under test (a failed write is logged,
        # not raised) is unchanged.
        assert "Command write failed" in caplog.text

    async def test_crashing_write_does_not_kill_the_background_task(self, hass, caplog):
        client = MagicMock()
        client.write_holding_register.side_effect = RuntimeError("boom")
        coordinator = make_coordinator(hass, client, {"time_validity": 60, "status": 3})
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._charging_desired = True
        coordinator._mark_block_success(BLOCK_CONFIG)
        # P0 fix: _heartbeat_tick now also requires BLOCK_STATUS to be
        # fresh (see TestFreshnessGate below) - marked fresh here too so
        # these tests keep exercising what they were written for (the
        # immediate-push/write-failure behavior) rather than incidentally
        # tripping over the new gate.
        coordinator._mark_block_success(BLOCK_STATUS)

        await coordinator.async_start_heartbeat()
        try:
            coordinator._wake_heartbeat()
            # Task 3: same rename as the failed-write log message above -
            # this now comes from _async_send_setpoint's shared crash
            # handler ("Command write crashed"), not a heartbeat-specific
            # string. The loop-survives-a-crashing-write behavior under
            # test is unchanged.
            await _wait_until(lambda: "Command write crashed" in caplog.text)

            # The loop itself must still be alive and would still fire on
            # its next scheduled interval.
            assert coordinator._heartbeat_task is not None
            assert not coordinator._heartbeat_task.done()
        finally:
            await coordinator.async_stop_heartbeat()


class TestLoopSurvivesUnexpectedErrors:
    """The outer net around every `_heartbeat_loop` iteration.

    `_heartbeat_tick` guards its own register writes (see
    TestWriteFailureHandling above), but everything else in an iteration -
    the interval calculation, the wait, the gate checks - was unguarded: a
    single unexpected exception from any of it would end the task silently.
    That is the worst possible failure mode for this particular loop,
    because the loop *is* the safety guarantee: with it gone the charger
    reverts 0x3001/0x3002 to maximum once the current Command Time Validity
    window lapses, and nothing is left running to notice. It would also
    stay dead for every subsequent session, since nothing restarts it short
    of an HA restart.
    """

    async def test_a_raising_tick_does_not_end_the_loop(self, hass, caplog, monkeypatch):
        ticks = []

        async def _exploding_tick():
            ticks.append(1)
            raise RuntimeError("unexpected")

        coordinator = make_coordinator(hass, MagicMock(), {"time_validity": 60})
        monkeypatch.setattr(coordinator, "_heartbeat_tick", _exploding_tick)
        monkeypatch.setattr(integration, "get_heartbeat_interval", lambda tv: 0.01)

        await coordinator.async_start_heartbeat()
        try:
            await _wait_until(lambda: ticks)
            assert not coordinator._heartbeat_task.done()
            assert "heartbeat iteration failed" in caplog.text
        finally:
            await coordinator.async_stop_heartbeat()

    async def test_a_raising_interval_calculation_does_not_end_the_loop(
        self, hass, caplog, monkeypatch
    ):
        """The interval math sits outside `_heartbeat_tick` entirely, so it
        was never covered by that method's own internal guards."""
        calls = []

        def _exploding_interval(tv):
            calls.append(tv)
            raise TypeError("unsupported operand type(s) for /: 'str' and 'int'")

        coordinator = make_coordinator(hass, MagicMock(), {"time_validity": "180"})
        monkeypatch.setattr(integration, "get_heartbeat_interval", _exploding_interval)

        await coordinator.async_start_heartbeat()
        try:
            await _wait_until(lambda: calls)
            assert not coordinator._heartbeat_task.done()
            assert "heartbeat iteration failed" in caplog.text
        finally:
            await coordinator.async_stop_heartbeat()

    async def test_cancellation_still_works_during_the_error_backoff(
        self, hass, monkeypatch
    ):
        """The guard must re-raise CancelledError rather than swallow it -
        otherwise the loop could never be stopped and
        `async_stop_heartbeat`'s await would hang forever on unload."""
        ticks = []

        async def _exploding_tick():
            ticks.append(1)
            raise RuntimeError("unexpected")

        coordinator = make_coordinator(hass, MagicMock(), {"time_validity": 60})
        monkeypatch.setattr(coordinator, "_heartbeat_tick", _exploding_tick)
        monkeypatch.setattr(integration, "get_heartbeat_interval", lambda tv: 0.01)

        await coordinator.async_start_heartbeat()
        task = coordinator._heartbeat_task
        await _wait_until(lambda: ticks)

        # Now in the long post-error backoff sleep - stopping must still
        # return promptly rather than waiting it out.
        await asyncio.wait_for(coordinator.async_stop_heartbeat(), timeout=5)

        assert task.cancelled() is True
        assert coordinator._heartbeat_task is None
