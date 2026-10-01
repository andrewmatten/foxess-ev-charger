"""Behaviours the 2.4.3 heartbeat / stop-latch / command-lock tests protected
that had no direct equivalent in the rebuild's contract suites, re-expressed
against the ChargingController public API over SimCharger (virtual clock).

Each test names the legacy test(s) it stands in for; the mapping of every
legacy test is in tests/LEGACY_DISPOSITION.md.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_TIME_VALIDITY,
    REG_WORK_MODE,
)
from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.transport import TransportError

from rebuild_simulator import CHARGING, CONNECTED, FINISHED, SimCharger
from test_rebuild_contract import Rig, settle, start_stop_writes

P, C, CTRL = REG_MAX_CHARGING_POWER, REG_MAX_CHARGING_CURRENT, REG_CHARGING_CONTROL
FULL, CAP = 70, 14


@pytest.fixture
async def rigs():
    made: list[Rig] = []

    def make(**kwargs) -> Rig:
        rig = Rig(**kwargs)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        try:
            await rig.close()
        except Exception:
            pass


def power_values(sim: SimCharger, mark: int = 0) -> list[int]:
    return [v for a, v in sim.wire_writes(mark) if a == P]


# ── cap kept alive whatever the rest of the telemetry does ────────────────
# legacy: test_realistic_charger::test_drift_is_detected_and_resent

async def test_externally_reverted_cap_is_restored_within_one_refresh(rigs):
    rig = rigs(reported_validity=180, effective_validity=180)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    await rig.clock.advance(5)
    rig.sim.holding[P] = rig.sim.max_power_raw   # firmware drops the cap early
    assert rig.sim.power_limit_raw() == rig.sim.max_power_raw
    await rig.clock.advance(30)                  # at most one refresh interval
    assert rig.sim.power_limit_raw() == CAP
    t = rig.clock()
    await rig.clock.advance(300)
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP
    assert ctl.intent_enabled


# legacy: test_realistic_charger::test_heartbeat_keeps_capping_with_stale_config_block,
#         test_heartbeat_task::TestFreshnessGate::test_stale_config_block_still_pushes

async def test_failing_config_block_does_not_stop_cap_refresh(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.fail_reads(0x3000, count=None)       # config block unreadable
    t = rig.clock()
    for _ in range(10):
        await rig.clock.advance(30)
        await rig.run(ctl.async_poll())
    assert ctl.data.get("config_block_ok") is False
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP
    assert rig.sim.measured_power_raw() == CAP
    assert ctl.intent_enabled


# legacy: test_realistic_charger::test_heartbeat_keeps_capping_during_non_fatal_alarm,
#         test_heartbeat_task::TestFreshnessGate::test_active_alarm_still_pushes

async def test_non_fatal_alarm_keeps_cap_refreshed(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.alarm_code = 0b100                   # phase loss: still drawing
    snap = await rig.run(ctl.async_poll())
    assert snap["active_alarms"] == ["phase_loss"]
    t = rig.clock()
    await rig.clock.advance(300)
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP
    assert rig.sim.measured_power_raw() == CAP
    assert ctl.intent_enabled


# legacy: test_realistic_charger::test_hard_fault_sends_stop_instead_of_going_silent,
#         test_heartbeat_task::TestFreshnessGate::test_active_fault_sends_stop_instead_of_a_push
# The protected behaviour is "never go silent on a fault" (silence lets the
# firmware revert the cap to maximum). The rebuild latches the fault and
# keeps refreshing a zero cap instead of sending 0x4001 Stop (CONTRACTS.md C
# allows 0x4001=2 only as the zero-power fallback); a cleared fault never
# resumes by itself, only an explicit enable does (SPEC 6.8).

async def test_hard_fault_mid_session_keeps_refreshing_never_goes_silent(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.raise_fault(1 << 3)                  # overcurrent
    snap = await rig.run(ctl.async_poll())
    assert snap["active_faults"] == ["overcurrent"]
    await settle()
    assert ctl.export_state()["safety_latched"] is True
    assert rig.persisted[-1]["safety_latched"] is True
    assert ctl.phase == "faulted"
    mark = rig.sim.mark()
    await rig.clock.advance(300)
    values = power_values(rig.sim, mark)
    assert len(values) >= 9                      # every <=30 s, not silent
    assert set(values) == {0}
    assert (CTRL, 1) not in rig.sim.wire_writes()
    # The fault clears by itself: still no automatic resume.
    rig.sim.fault_code = 0
    rig.sim.state = CHARGING
    mark = rig.sim.mark()
    await rig.run(ctl.async_poll())
    await rig.clock.advance(300)
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.measured_power_raw() == 0
    assert ctl.export_state()["safety_latched"] is True
    result = await rig.run(ctl.async_enable())   # explicit recovery
    assert result.outcome == "confirmed" and rig.sim.holding[P] == CAP


async def test_fault_code_with_charging_status_latches_zero(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    mark = rig.sim.mark()
    rig.sim.fault_code = 1 << 3                  # fault bit, status still 3
    await rig.clock.advance(120)                 # refresh loop only, no poll
    assert ctl.export_state()["safety_latched"] is True
    values = power_values(rig.sim, mark)
    assert values and values[0] == 0             # first post-fault write is zero
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.holding[P] == 0 and rig.sim.measured_power_raw() == 0


async def test_fault_status_seen_by_refresh_alone_latches(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.raise_fault(1)                       # no poll: refresh loop only
    await rig.clock.advance(120)
    assert ctl.export_state()["safety_latched"] is True
    assert rig.sim.holding[P] == 0


# ── restart / restore ─────────────────────────────────────────────────────
# legacy: test_realistic_charger::test_setup_mid_session_writes_cap_immediately

async def test_restart_mid_session_applies_saved_cap_during_initialize(rigs):
    rig = rigs(state=CHARGING)
    saved = {"schema": 1, "enabled": True, "power_raw": CAP, "current_raw": None,
             "revision": 3, "safety_latched": False, "stop_fallback": False}
    assert rig.sim.power_limit_raw() == rig.sim.max_power_raw
    ctl = rig.new_controller()
    rig.ctl = ctl
    await rig.run(ctl.async_initialize(saved))
    # before the refresh loop has even started
    assert rig.sim.power_limit_raw() == CAP
    assert rig.sim.measured_power_raw() == CAP
    assert ctl.intent_enabled


# legacy: test_natural_session_heartbeat_protection::TestFetchSetsDesiredFlag::
#         test_failed_status_block_read_does_not_set_the_flag_from_stale_data

async def test_first_install_with_unreadable_status_does_not_authorize_charging(rigs):
    rig = rigs(state=CHARGING)
    rig.sim.fail_reads(0x1000, count=None)
    ctl = rig.new_controller()
    rig.ctl = ctl
    await rig.run(ctl.async_initialize(None, configure_safety=False))
    assert not ctl.intent_enabled


# ── natural (Plug&Charge / RFID) sessions ─────────────────────────────────
# legacy: test_natural_session_heartbeat_protection::TestUpdateDataWakesHeartbeatOnNaturalStart::
#         test_transition_to_desired_wakes_the_heartbeat,
#         test_stop_inhibit::TestNaturalStartSuppressedWhileInhibited::
#         test_active_status_sets_desired_normally_once_not_inhibited

async def test_naturally_started_session_is_capped_from_its_first_instant(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    mark = rig.sim.mark()
    rig.sim.state = FINISHED                     # car finished by itself
    await rig.clock.advance(120)
    # Firmware 1.8: a positive heartbeat would restart a finished session.
    # The finish ends the authorization; the heartbeat carries zero.
    assert rig.sim.state == FINISHED
    assert rig.sim.positive_cap_writes(mark) == []
    assert not ctl.intent_enabled and rig.persisted[-1]["enabled"] is False
    t = rig.clock()
    rig.sim.external_start()                     # RFID tap / Plug&Charge
    await rig.clock.advance(300)
    # Still plugged in, so the ended session's pause holds: capped at zero
    # from its first instant, never above the old cap.
    assert rig.sim.max_power_limit(t, rig.clock()) == 0
    assert rig.sim.max_measured_power(t, rig.clock()) == 0
    assert not ctl.intent_enabled


async def test_status_5_seen_as_telemetry_recovery_frames_does_not_resume(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.set_invalid_telemetry()
    await rig.clock.advance(90)                  # beyond grace: protective zero
    assert ctl.phase == "uncertain"
    rig.sim.state = FINISHED                     # session ended meanwhile
    rig.sim.clear_invalid_telemetry()
    mark = rig.sim.mark()
    await rig.run(ctl.async_poll())
    await rig.clock.advance(2)
    await rig.run(ctl.async_poll())
    await rig.clock.advance(120)
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.state == FINISHED and not ctl.intent_enabled


async def test_telemetry_recovery_into_active_session_resumes_cap(rigs):
    rig = rigs(reported_validity=180, effective_validity=60, zero_pause_works=False)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.set_invalid_telemetry()
    await rig.clock.advance(90)
    assert ctl.phase == "uncertain"
    rig.sim.clear_invalid_telemetry()            # still charging (zero ignored)
    await rig.run(ctl.async_poll())
    await rig.clock.advance(2)
    await rig.run(ctl.async_poll())
    await rig.clock.advance(60)
    assert ctl.intent_enabled and ctl.phase == "enabled"
    assert rig.sim.holding[P] == CAP


# legacy: test_stop_inhibit::TestDisconnectClearsInhibit::test_vehicle_unplugging_clears_the_inhibit
# SPEC.md 5.2: after a confirmed unplug (cc_status == 0) a genuinely new
# externally authorised session must be recognisable; a pause alone must
# not be cleared by active telemetry.

async def test_user_pause_then_unplug_replug_allows_new_external_session(rigs):
    rig = rigs(reported_validity=180, effective_validity=60, work_mode=1)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    rig.sim.unplug()
    await rig.clock.advance(30)
    await rig.run(ctl.async_poll())              # cc_status 0 observed
    rig.sim.plug()                               # Plug&Charge starts by itself
    await rig.clock.advance(300)
    assert rig.sim.state == CHARGING
    assert rig.sim.measured_power_raw() > 0


# ── concurrency: pause always wins over in-flight positive work ───────────
# legacy: test_command_lock::TestStartAndStopOverlap::
#         test_a_stop_landing_during_an_in_flight_start_prevents_desired_from_being_set,
#         test_entity_write_reliability::test_concurrent_stop_during_start_confirmation_keeps_stop_authoritative

async def test_pause_during_in_flight_enable_wins(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(FULL))
    gate = rig.sim.hold_writes(P, value=FULL)
    enable = asyncio.ensure_future(ctl.async_enable())
    await asyncio.wait_for(gate.entered.wait(), 1)
    pause = asyncio.ensure_future(ctl.async_pause())
    await settle()
    gate.release()
    assert (await rig.run(pause)).outcome == "confirmed"
    try:
        outcome = (await rig.run(enable)).outcome
    except ControlError:
        outcome = "failed"
    assert outcome != "confirmed"
    mark = rig.sim.mark()
    await rig.clock.advance(180)
    assert not ctl.intent_enabled
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.measured_power_raw() == 0
    assert start_stop_writes(rig.sim) == []


# legacy: test_heartbeat_task::TestGenerationTokenRace::
#         test_stop_landing_before_a_later_register_prevents_that_writes_start

async def test_pause_between_current_and_power_refresh_writes_blocks_positive_power(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_current(160))
    await rig.enable_at(FULL)
    gate = rig.sim.hold_writes(C, value=160)
    await rig.clock.advance(35)                  # refresh: current write in flight
    assert gate.entered.is_set()
    mark = rig.sim.mark()
    pause = asyncio.ensure_future(ctl.async_pause())
    await settle()
    gate.release()
    assert (await rig.run(pause)).outcome == "confirmed"
    assert (P, FULL) not in rig.sim.wire_writes(mark)
    assert power_values(rig.sim, mark) and set(power_values(rig.sim, mark)) == {0}


# ── failed enable leaves no positive output ───────────────────────────────
# legacy: test_entity_write_reliability::test_charging_switch_turn_on_does_not_set_desired_flag_on_failed_write,
#         ::test_failed_prestart_cap_aborts_start, ::test_exception_during_prestart_cap_sends_stop
# The old compensating 0x4001 Stop is replaced by a latched fault with a
# protective zero (SPEC.md 6.8). If that zero itself cannot take effect the
# confirmed-Stop fallback may follow (CONTRACTS.md C); 0x4001 Start never.

@pytest.mark.parametrize("address, mode", [
    (C, "refuse"), (C, "lost_request"), (P, "ignored"), (P, "refuse"),
])
async def test_failed_enable_latches_and_holds_zero_until_explicit_enable(rigs, address, mode):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_current(160))
    await rig.run(ctl.async_set_power(FULL))
    rig.sim.inject_write(address, mode, count=None)
    with pytest.raises(ControlError):
        await rig.run(ctl.async_enable())
    assert ctl.phase == "faulted"
    assert ctl.export_state()["safety_latched"] is True
    mark = rig.sim.mark()
    await rig.clock.advance(300)
    assert [w for w in rig.sim.positive_cap_writes(mark) if w.address == P] == []
    assert rig.sim.measured_power_raw() == 0
    assert (CTRL, 1) not in rig.sim.wire_writes()
    rig.sim.clear_write_rules()
    assert (await rig.run(ctl.async_enable())).outcome == "confirmed"
    assert ctl.phase == "enabled"


# ── refresh loop robustness ───────────────────────────────────────────────

class CrashingIO:
    """RegisterIO over a SimCharger whose next N writes raise an unexpected
    (non-transport) exception."""

    def __init__(self, sim: SimCharger) -> None:
        self.sim = sim
        self.crash_writes = 0

    async def read(self, address, count):
        return await self.sim.read(address, count)

    async def write(self, address, value):
        if self.crash_writes:
            self.crash_writes -= 1
            raise RuntimeError("boom")
        await self.sim.write(address, value)

    async def close(self):
        await self.sim.close()


# legacy: test_heartbeat_task::TestWriteFailureResilience::test_crashing_write_does_not_kill_the_background_task,
#         ::test_failed_write_is_logged_and_does_not_raise,
#         test_heartbeat_task::TestLoopSurvivesUnexpectedErrors::test_a_raising_tick_does_not_end_the_loop

async def test_unexpected_write_exceptions_do_not_end_the_refresh_loop(rigs):
    from custom_components.foxess_charger.controller import ChargingController

    rig = rigs(reported_validity=180, effective_validity=60)
    io = CrashingIO(rig.sim)
    ctl = ChargingController(io, clock=rig.clock, sleep=rig.clock.sleep,
                             confirmation_timeout=10.0, retry_interval=3.0)
    rig.ctl = ctl
    await rig.run(ctl.async_initialize(None))
    await rig.run(ctl.async_start())
    await rig.enable_at(CAP)
    io.crash_writes = 5
    await rig.clock.advance(240)
    assert io.crash_writes == 0
    assert "boom" in (ctl.diagnostics["last_error"] or "")
    t = rig.clock()
    mark = rig.sim.mark()
    await rig.clock.advance(300)
    assert len(power_values(rig.sim, mark)) >= 9  # the loop is still alive
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP


# legacy: test_heartbeat_task::TestLoopSurvivesUnexpectedErrors::
#         test_a_raising_interval_calculation_does_not_end_the_loop

@pytest.mark.parametrize("validity", [0, 0xFFFF])
async def test_nonsensical_validity_register_keeps_refreshing(rigs, validity):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot(configure_safety=False)
    await rig.enable_at(CAP)
    rig.sim.holding[REG_TIME_VALIDITY] = validity
    t = rig.clock()
    await rig.clock.advance(300)
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP
    assert ctl.diagnostics["refresh_interval"] <= 30


# legacy: test_heartbeat_task::TestLoopSurvivesUnexpectedErrors::
#         test_cancellation_still_works_during_the_error_backoff

async def test_close_during_failure_backoff_returns_promptly(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.set_io_down()
    await rig.clock.advance(40)                  # refreshes failing, backing off
    assert ctl.diagnostics["refresh_failures"] >= 1
    await rig.run(ctl.async_close(), max_time=5.0)
    rig.ctl = None
    assert rig.sim.close_count == 1 or rig.sim.closed


# legacy: test_heartbeat_task::TestUnloadCancellation::test_stop_heartbeat_is_a_safe_no_op_when_never_started

async def test_close_without_start_is_safe(rigs):
    rig = rigs()
    ctl = rig.new_controller()
    await rig.run(ctl.async_initialize(None, configure_safety=False))
    await rig.run(ctl.async_close())
    assert rig.sim.closed


# ── validity change reschedules the refresh ───────────────────────────────
# legacy: test_heartbeat_task::TestRescheduleOnTimeValidityChange::
#         test_mid_wait_change_wakes_early_and_reschedules

async def test_shortened_validity_takes_effect_before_the_cap_can_lapse(rigs):
    rig = rigs(reported_validity=60, effective_validity=60)
    ctl = await rig.boot(configure_safety=False)
    await rig.enable_at(CAP)
    await rig.clock.advance(31)                  # mid-way through a 30 s wait
    assert (await rig.run(ctl.async_set_register(REG_TIME_VALIDITY, 10))).outcome == "confirmed"
    t = rig.clock()
    await rig.clock.advance(120)
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP


# ── restored limits outside the detected model's range ────────────────────
# legacy: test_setpoint_persistence::TestValidatingAgainstDetectedCapabilities::
#         test_out_of_range_power_with_no_safe_default_is_discarded (and siblings)

async def test_restored_power_above_model_maximum_never_runs_uncapped_or_silently(rigs):
    rig = rigs(state=CHARGING)                   # A7300: 7.3 kW maximum
    saved = {"schema": 1, "enabled": True, "power_raw": 150, "current_raw": None,
             "revision": 3, "safety_latched": False, "stop_fallback": False}
    ctl = rig.new_controller()
    rig.ctl = ctl
    await rig.run(ctl.async_initialize(saved))
    await rig.run(ctl.async_start())
    t = rig.clock()
    await rig.clock.advance(120)
    # Either the value is rejected visibly or the output is protective; it
    # must never be reported as a healthy enabled state at maximum output.
    assert not (ctl.phase == "enabled" and rig.sim.max_measured_power(t, rig.clock()) >= rig.sim.max_power_raw)


async def test_enable_staged_while_unplugged_starts_capped_on_plug(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    rig.sim.unplug()
    await rig.run(ctl.async_set_power(CAP))
    assert (await rig.run(ctl.async_enable())).outcome == "staged"
    await rig.clock.advance(120)                 # stays authorized while unplugged
    assert ctl.intent_enabled
    t = rig.clock()
    rig.sim.plug()
    await rig.clock.advance(60)
    assert rig.sim.state == CHARGING and ctl.intent_enabled
    assert rig.sim.max_power_limit(t + 1, rig.clock()) <= CAP


async def test_set_power_right_after_natural_finish_stages_and_revokes(rigs):
    """Solar-surplus modulation writes set_power often: one landing just after a
    natural finish must not restart the finished session."""
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.state = FINISHED                     # not caused by us
    mark = rig.sim.mark()
    result = await rig.run(ctl.async_set_power(50))
    assert result.outcome == "staged"
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.state == FINISHED
    assert not ctl.intent_enabled and ctl.desired_power_raw == 50
    assert rig.persisted[-1]["enabled"] is False
    assert rig.persisted[-1]["power_raw"] == 50
    await rig.clock.advance(120)
    assert rig.sim.positive_cap_writes(mark) == [] and rig.sim.state == FINISHED


async def test_set_power_with_invalid_fresh_read_stages_without_positive_write(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.set_invalid_telemetry()              # stale "active" must not count
    mark = rig.sim.mark()
    result = await rig.run(ctl.async_set_power(50))
    assert result.outcome == "staged"
    assert rig.sim.positive_cap_writes(mark) == []
    assert ctl.intent_enabled and ctl.desired_power_raw == 50


@pytest.mark.parametrize("physical", ["finished", "active"])
async def test_staged_limit_is_never_refreshed_on_stale_evidence(rigs, physical):
    """N1a: set_power staged after an invalid fresh read, then a
    refresh inside the telemetry grace. Finished: no positive write at all
    (it would restart the session). Active: never the staged value."""
    rig = rigs(reported_validity=20, effective_validity=20)
    ctl = await rig.boot(configure_safety=False)  # 10 s refresh interval
    await rig.enable_at(CAP)
    if physical == "finished":
        rig.sim.state = FINISHED
    rig.sim.set_invalid_telemetry()
    mark = rig.sim.mark()
    assert (await rig.run(ctl.async_set_power(50))).outcome == "staged"
    await rig.clock.advance(15)                  # >= 1 refresh, inside grace
    values = [w.value for w in rig.sim.positive_cap_writes(mark)]
    assert 50 not in values
    if physical == "finished":
        assert values == [] and rig.sim.state == FINISHED
    else:
        assert all(v <= CAP for v in values)
        assert rig.sim.max_power_limit(rig.clock() - 15, rig.clock()) <= CAP


async def test_unreadable_fault_register_is_invalid_telemetry(rigs):
    """N6: status 3 but 0x101A-0x101B unreadable. The observation is
    invalid: grace, then protective zero; positive refreshes never coexist
    with an unobservable fault register beyond the grace."""
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.fault_code = 1 << 3                  # hidden behind the failed read
    rig.sim.fail_reads(0x101A, count=None)
    t0 = rig.clock()
    await rig.clock.advance(60)
    late = [w for w in rig.sim.positive_cap_writes() if w.t > t0 + 25]
    assert late == []
    assert ctl.phase == "uncertain" and rig.sim.holding[P] == 0
