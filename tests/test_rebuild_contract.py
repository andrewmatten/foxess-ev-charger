"""Black-box contract tests for the rebuilt ChargingController (CONTRACTS.md
section C, SPEC.md section 8), driven through SimCharger on a virtual clock.

Only the public API is used: constructor, async_* methods, CommandResult,
ControlError, export_state(), intent_enabled, desired_power_raw, phase.
Everything else is judged on the simulated device (what reached the wire,
what the charger actually enforced and when).

Skips cleanly until custom_components.foxess_charger.controller exists.
"""
from __future__ import annotations

import asyncio

import pytest

controller_mod = pytest.importorskip("custom_components.foxess_charger.controller")
ChargingController = controller_mod.ChargingController
ControlError = controller_mod.ControlError

from custom_components.foxess_charger.const import (  # noqa: E402
    REG_CHARGING_CONTROL,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
)

from rebuild_simulator import (  # noqa: E402
    CHARGING,
    CONNECTED,
    FINISHED,
    ZERO_PAUSED,
    SimCharger,
    VirtualClock,
)

P, C, CTRL = REG_MAX_CHARGING_POWER, REG_MAX_CHARGING_CURRENT, REG_CHARGING_CONTROL
FULL, CAP = 70, 14   # 7.0 kW, 1.4 kW


class Rig:
    def __init__(self, **sim_kwargs) -> None:
        self.clock = VirtualClock()
        sim_kwargs.setdefault("state", CONNECTED)
        self.sim = SimCharger(clock=self.clock, **sim_kwargs)
        self.persisted: list[dict] = []
        self.persist_fails = False
        self.ctl = None

    async def _persist(self, state: dict) -> None:
        if self.persist_fails:
            raise OSError("disk full")
        self.persisted.append(dict(state))

    def new_controller(self):
        return ChargingController(
            self.sim, persist=self._persist, clock=self.clock, sleep=self.clock.sleep,
            confirmation_timeout=10.0, retry_interval=3.0,
        )

    async def run(self, awaitable, max_time: float = 120.0):
        return await self.clock.run(awaitable, max_time=max_time)

    async def boot(self, saved=None, *, configure_safety=True):
        self.ctl = self.new_controller()
        await self.run(self.ctl.async_initialize(saved, configure_safety=configure_safety))
        await self.run(self.ctl.async_start())
        return self.ctl

    async def restart(self, *, configure_safety=True):
        saved = self.ctl.export_state()
        await self.run(self.ctl.async_close())
        self.sim.simulate_process_restart()
        return await self.boot(saved, configure_safety=configure_safety)

    async def enable_at(self, raw: int):
        await self.run(self.ctl.async_set_power(raw))
        result = await self.run(self.ctl.async_enable())
        assert result.outcome == "confirmed"
        return result

    async def close(self):
        if self.ctl is not None:
            await self.run(self.ctl.async_close())


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


async def test_slow_charger_replies_do_not_break_initialize_enable_or_refresh(rigs):
    """Slow-reply incident: a charger that takes several seconds to
    answer must not break initialize, enable or the background refresh
    loop. SimCharger's `reply_latency` stands in for that slow charger,
    advancing virtual time by 3 s on every single read/write; this is the
    controller-level counterpart to the transport-level fix (see
    CHANGELOG) - each control write here still gets its own full
    confirmation_timeout window rather than sharing one between the
    registers a single operation writes."""
    rig = rigs(reply_latency=3.0)
    ctl = await rig.boot()
    result = await rig.enable_at(CAP)
    assert result.outcome == "confirmed"
    assert rig.sim.state == CHARGING
    assert ctl.intent_enabled

    t = rig.clock()
    await rig.clock.advance(120)
    assert rig.sim.max_power_limit(t, t + 120) == CAP
    assert ctl.intent_enabled


def start_stop_writes(sim: SimCharger, mark: int = 0) -> list[tuple[int, int]]:
    return [w for w in sim.wire_writes(mark) if w[0] == CTRL and w[1] in (1, 2)]


async def settle(n: int = 50) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ── refused-Start: enable without 0x4001, no stop loop ───────────────────────────

@pytest.mark.parametrize("initial", [CONNECTED, FINISHED])
async def test_enable_completes_by_power_without_start_or_stop(rigs, initial):
    rig = rigs(state=initial)
    ctl = await rig.boot()
    staged = await rig.run(ctl.async_set_power(CAP))
    assert staged.outcome == "staged"
    assert rig.sim.positive_cap_writes() == []

    result = await rig.run(ctl.async_enable())
    assert result.outcome == "confirmed"
    assert start_stop_writes(rig.sim) == []
    assert rig.sim.refused_starts == 0
    assert rig.sim.state == CHARGING
    t = rig.clock()
    await rig.clock.advance(120)
    assert rig.sim.max_power_limit(t, t + 120) == CAP
    assert start_stop_writes(rig.sim) == []
    assert ctl.intent_enabled


# ── stop-then-resume: off + staging + active-looking status never resumes ──────────

@pytest.mark.parametrize("external_resume", [False, True])
async def test_off_then_staging_never_sends_positive_power(rigs, external_resume):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    mark = rig.sim.mark()

    assert (await rig.run(ctl.async_set_power(FULL))).outcome == "staged"
    assert (await rig.run(ctl.async_set_current(320))).outcome == "staged"
    if external_resume:
        rig.sim.car_demand_raw = FULL
        # the device is pushed back to charging behind HA's back
        rig.sim.state = CHARGING
    await rig.clock.advance(300)
    assert rig.sim.positive_cap_writes(mark) == []
    assert not ctl.intent_enabled
    assert ctl.desired_power_raw == FULL

    ctl = await rig.restart()
    await rig.clock.advance(300)
    assert rig.sim.positive_cap_writes(mark) == []
    assert not ctl.intent_enabled
    assert ctl.export_state()["enabled"] is False


# ── sawtooth: cap survives reported/effective validity mismatch ──────────

@pytest.mark.parametrize("reported, effective", [(60, 60), (180, 180), (180, 60)])
@pytest.mark.parametrize("configure_safety", [False, True])
async def test_cap_held_for_ten_minutes_with_lost_replies(rigs, reported, effective, configure_safety):
    rig = rigs(reported_validity=reported, effective_validity=effective)
    ctl = await rig.boot(configure_safety=configure_safety)
    await rig.enable_at(CAP)
    t0 = rig.clock()
    for mode in ("lost_reply", "lost_request", "lost_reply", "lost_request"):
        rig.sim.inject_write(P, mode)
        await rig.clock.advance(150)
    await rig.clock.advance(60)
    assert rig.clock() - t0 >= 660
    assert rig.sim.max_power_limit(t0, rig.clock()) == CAP
    assert rig.sim.max_measured_power(t0, rig.clock()) == CAP
    assert rig.sim.measured_power_raw() == CAP
    assert ctl.intent_enabled
    assert start_stop_writes(rig.sim) == []


# ── stale-write race ──────────────────────────────────────────────────────

async def test_in_flight_refresh_cannot_overwrite_newer_lower_power(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    gate = rig.sim.hold_writes(P, value=FULL)
    await rig.clock.advance(35)          # a refresh of 7.0 kW is now in flight
    assert gate.entered.is_set()
    mark = rig.sim.mark()
    lower = asyncio.ensure_future(ctl.async_set_power(CAP))
    await settle()
    gate.release()
    result = await rig.run(lower)
    assert result.outcome == "confirmed"
    await rig.clock.advance(120)
    values = [v for a, v in rig.sim.wire_writes(mark) if a == P]
    first_cap = values.index(CAP)
    assert FULL not in values[first_cap:]
    assert rig.sim.holding[P] == CAP
    assert rig.sim.max_power_limit(rig.clock() - 100, rig.clock()) == CAP


async def test_pause_supersedes_queued_positive_work(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    gate = rig.sim.hold_writes(P, value=FULL)
    await rig.clock.advance(35)
    assert gate.entered.is_set()
    mark = rig.sim.mark()
    queued = asyncio.ensure_future(ctl.async_set_power(50))
    await settle()
    pause = asyncio.ensure_future(ctl.async_pause())
    await settle()
    gate.release()
    assert (await rig.run(pause)).outcome == "confirmed"
    try:
        outcome = (await rig.run(queued)).outcome
    except ControlError:
        outcome = "failed"
    assert outcome != "confirmed"
    await rig.clock.advance(120)
    assert (P, 50) not in rig.sim.wire_writes(mark)
    assert rig.sim.positive_cap_writes(rig.sim.writes_since(mark, P)[-1].seq + 1) == []
    assert rig.sim.measured_power_raw() == 0
    assert not ctl.intent_enabled


# ── cancellation ──────────────────────────────────────────────────────────

async def test_cancelled_enable_leaves_controller_usable(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(FULL))
    gate = rig.sim.hold_writes(P, value=FULL)
    enable = asyncio.ensure_future(ctl.async_enable())
    await asyncio.wait_for(gate.entered.wait(), 1)
    enable.cancel()
    gate.release()
    await settle()
    with pytest.raises(asyncio.CancelledError):
        await enable
    result = await rig.run(ctl.async_pause())
    assert result.outcome == "confirmed"
    t = rig.clock()
    mark = rig.sim.mark()
    await rig.clock.advance(180)
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.max_power_limit(t, rig.clock()) == 0   # zero kept refreshed


async def test_cancelled_pause_keeps_protective_intent(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    gate = rig.sim.hold_writes(P, value=0)
    mark = rig.sim.mark()
    pause = asyncio.ensure_future(ctl.async_pause())
    await asyncio.wait_for(gate.entered.wait(), 1)
    pause.cancel()
    gate.release()
    await settle()
    await rig.clock.advance(180)
    assert not ctl.intent_enabled
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.measured_power_raw() == 0


async def test_close_drains_in_flight_write_before_closing_io(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    gate = rig.sim.hold_writes(P)
    await rig.clock.advance(35)
    assert gate.entered.is_set()
    closing = asyncio.ensure_future(ctl.async_close())
    await settle()
    assert not rig.sim.closed
    gate.release()
    await rig.run(closing)
    rig.ctl = None
    assert rig.sim.closed
    held = rig.sim.writes_since(0, P)[-1]
    close_seq = next(e.seq for e in rig.sim.log if e.kind == "close")
    assert held.seq < close_seq and held.applied_at is not None
    n = len(rig.sim.writes)
    await rig.clock.advance(120)
    assert len(rig.sim.writes) == n


# ── invalid telemetry ─────────────────────────────────────────────────────

async def test_invalid_status_never_confirms_pause(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    mark = rig.sim.mark()
    rig.sim.set_invalid_telemetry(status=0xFFFF, cc=0xFFFF)
    with pytest.raises(ControlError):
        await rig.run(ctl.async_pause())
    assert not ctl.intent_enabled
    assert ctl.phase != "paused"
    rig.sim.clear_invalid_telemetry()
    await rig.clock.advance(120)
    assert rig.sim.positive_cap_writes(mark) == []


async def test_invalid_cable_reading_is_not_an_unplug(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    mark = rig.sim.mark()
    rig.sim.set_invalid_telemetry(status=0xFFFF, cc=0xFFFF)
    await rig.clock.advance(60)
    rig.sim.clear_invalid_telemetry()
    rig.sim.state = CHARGING  # active-looking session appears afterwards
    await rig.clock.advance(120)
    assert not ctl.intent_enabled
    assert rig.sim.positive_cap_writes(mark) == []


async def test_status_9_keeps_session_and_cap(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    rig.sim.begin_phase_switch()
    await rig.clock.advance(40)
    await rig.run(ctl.async_poll())
    assert ctl.intent_enabled
    rig.sim.end_phase_switch()
    t = rig.clock()
    await rig.clock.advance(120)
    assert ctl.intent_enabled
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP
    assert rig.sim.measured_power_raw() == CAP
    assert start_stop_writes(rig.sim) == []


# ── ACK without read-back ─────────────────────────────────────────────────

async def test_ignored_write_never_confirms_enable(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.inject_write(P, "ignored", count=None)
    with pytest.raises(ControlError):
        await rig.run(ctl.async_enable())


async def test_unreadable_device_never_confirms_enable(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.fail_reads(None, count=None)
    with pytest.raises(ControlError):
        await rig.run(ctl.async_enable())


async def test_delay_beyond_confirmation_timeout_fails(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.inject_write(P, "delayed", delay=30, count=None)
    with pytest.raises(ControlError):
        await rig.run(ctl.async_enable())
    mark = rig.sim.mark()
    await rig.clock.advance(180)
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.measured_power_raw() == 0


async def test_slow_hardware_start_is_confirmed(rigs):
    """Measured firmware 1.8 starts take 11-19 s before a session shows."""
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.status_override = 1      # still "connected" while the car wakes
    enable = asyncio.ensure_future(ctl.async_enable())
    await rig.clock.advance(19)
    assert not enable.done()
    rig.sim.status_override = None
    result = await rig.run(enable)
    assert result.outcome == "confirmed" and ctl.intent_enabled


@pytest.mark.parametrize("delay", [19, 60])
async def test_sim_start_delay_is_confirmed_and_capped(rigs, delay):
    """SimCharger's own starting->charging transition. Status 2 (starting)
    is session evidence, so a start slower than the 45 s window is still
    confirmed; the timeout path needs status to stay 1 (tests below)."""
    rig = rigs(start_delay=delay)
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    t = rig.clock()
    result = await rig.run(ctl.async_enable())
    assert result.outcome == "confirmed" and ctl.intent_enabled
    assert rig.sim.status == 2
    await rig.clock.advance(delay + 5)
    assert rig.sim.state == CHARGING and ctl.intent_enabled
    assert rig.sim.max_power_limit(t, rig.clock()) == CAP
    assert rig.sim.measured_power_raw() == CAP


async def test_start_beyond_45_s_times_out_and_revokes(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.status_override = 1      # still "connected" at 46 s
    enable = asyncio.ensure_future(ctl.async_enable())
    await rig.clock.advance(44)
    assert not enable.done()
    await rig.clock.advance(5)
    with pytest.raises(ControlError):
        await rig.run(enable)
    assert not ctl.intent_enabled and rig.sim.holding[P] == 0


async def test_start_that_never_arrives_revokes_positive_authorization(rigs):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.status_override = 1
    with pytest.raises(ControlError):
        await rig.run(ctl.async_enable())
    assert not ctl.intent_enabled
    assert rig.sim.holding[P] == 0
    rig.sim.status_override = None
    mark = rig.sim.mark()
    await rig.clock.advance(300)
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.measured_power_raw() == 0


@pytest.mark.parametrize("mode, delay", [("lost_reply", 0), ("delayed", 2)])
async def test_lost_reply_or_short_delay_confirmed_by_fresh_read(rigs, mode, delay):
    rig = rigs()
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    rig.sim.inject_write(P, mode, delay=delay)
    result = await rig.run(ctl.async_enable())
    assert result.outcome == "confirmed"
    assert rig.sim.holding[P] == CAP


# ── zero pause ignored: confirmed Stop fallback ───────────────────────────

@pytest.mark.parametrize("stop_mode", ["ok", "lost_reply", "delayed"])
async def test_ignored_zero_pause_falls_back_to_stop_and_latches(rigs, stop_mode):
    rig = rigs(zero_pause_works=False)
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    if stop_mode != "ok":
        rig.sim.inject_write(CTRL, stop_mode, value=2, delay=2)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    assert (CTRL, 2) in rig.sim.wire_writes()
    assert (CTRL, 1) not in rig.sim.wire_writes()
    assert rig.sim.state == FINISHED
    assert ctl.export_state()["stop_fallback"] is True
    stop_seq = next(w.seq for w in rig.sim.writes if (w.address, w.value) == (CTRL, 2))

    assert (await rig.run(ctl.async_set_power(50))).outcome == "staged"
    await rig.clock.advance(600)
    assert rig.sim.positive_cap_writes(stop_seq) == []
    assert rig.sim.state == FINISHED

    ctl = await rig.restart()
    await rig.clock.advance(300)
    assert rig.sim.positive_cap_writes(stop_seq) == []
    assert ctl.export_state()["stop_fallback"] is True

    assert (await rig.run(ctl.async_enable())).outcome == "confirmed"
    assert (CTRL, 1) not in rig.sim.wire_writes()
    assert rig.sim.state == CHARGING
    assert ctl.export_state()["stop_fallback"] is False


async def test_refused_stop_fallback_fails_visibly_and_stays_off(rigs):
    rig = rigs(zero_pause_works=False)
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    rig.sim.inject_write(CTRL, "refuse", value=2, count=None)
    mark = rig.sim.mark()
    with pytest.raises(ControlError):
        await rig.run(ctl.async_pause())
    assert not ctl.intent_enabled
    assert ctl.phase != "paused"
    await rig.clock.advance(300)
    assert rig.sim.positive_cap_writes(mark) == []
    assert (CTRL, 1) not in rig.sim.wire_writes()


# ── full session ──────────────────────────────────────────────────────────

async def test_full_session(rigs):
    rig = rigs(reported_validity=180, effective_validity=60)
    ctl = await rig.boot()
    assert (await rig.run(ctl.async_set_power(FULL))).outcome == "staged"
    assert (await rig.run(ctl.async_enable())).outcome == "confirmed"
    await rig.clock.advance(90)
    assert rig.sim.measured_power_raw() == FULL

    assert (await rig.run(ctl.async_set_power(CAP))).outcome == "confirmed"
    t = rig.clock()
    await rig.clock.advance(90)
    assert rig.sim.max_measured_power(t, rig.clock()) == CAP

    rig.sim.inject_write(P, "lost_reply")
    assert (await rig.run(ctl.async_set_power(FULL))).outcome == "confirmed"
    await rig.clock.advance(60)
    assert rig.sim.measured_power_raw() == FULL

    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    t = rig.clock()
    await rig.clock.advance(120)
    assert rig.sim.max_measured_power(t + 1, rig.clock()) == 0

    assert (await rig.run(ctl.async_enable())).outcome == "confirmed"
    await rig.clock.advance(30)
    assert rig.sim.measured_power_raw() == FULL

    ctl = await rig.restart()
    assert ctl.intent_enabled
    assert ctl.desired_power_raw == FULL
    t = rig.clock()
    await rig.clock.advance(180)
    assert rig.sim.max_power_limit(t, rig.clock()) <= FULL
    assert rig.sim.measured_power_raw() == FULL

    assert (await rig.run(ctl.async_set_power(CAP))).outcome == "confirmed"
    t = rig.clock()
    await rig.clock.advance(120)
    assert rig.sim.max_measured_power(t, rig.clock()) == CAP

    assert start_stop_writes(rig.sim) == []
    assert rig.persisted and all(s.get("schema") == 1 for s in rig.persisted)


# ── fallback current is not "stopped" ─────────────────────────────────────

async def test_fallback_current_after_silence_is_not_reported_paused(rigs):
    rig = rigs(reported_validity=60, effective_validity=60)
    ctl = await rig.boot()   # configures 6 A fallback
    await rig.enable_at(FULL)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    mark = rig.sim.mark()

    rig.sim.set_io_down()
    await rig.clock.advance(120)
    assert rig.sim.fallback_active
    assert rig.sim.measured_power_raw() > 0   # drawing at fallback current
    try:
        await rig.run(ctl.async_poll())
    except Exception:
        pass
    assert ctl.phase != "paused"
    assert not ctl.intent_enabled

    rig.sim.set_io_down(False)
    await rig.clock.advance(60)
    t = rig.clock()
    await rig.clock.advance(60)
    assert rig.sim.max_measured_power(t, rig.clock()) == 0
    assert rig.sim.positive_cap_writes(mark) == []
    assert not ctl.intent_enabled


# ── firmware 1.8: zero-pause finishes the session, bursts don't stop it ───

async def test_pause_resume_cycle_reaches_status_5(rigs):
    """A pause (0x3002=0) ends the session (status 5, not the old status 4);
    an explicit resume brings it straight back to charging."""
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    assert rig.sim.state == CHARGING

    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    assert rig.sim.state == ZERO_PAUSED
    assert rig.sim.status == 5
    assert rig.sim.stop_reason == 1
    assert rig.sim.measured_power_raw() == 0

    result = await rig.enable_at(CAP)
    assert result.outcome == "confirmed"
    assert rig.sim.state == CHARGING
    assert rig.sim.measured_power_raw() == CAP


async def test_connectivity_burst_mid_charge_no_protective_zero_or_stop(rigs):
    """A 15 s link outage mid-charge must not be mistaken for a reason to
    zero the power or send a Stop: the controller just retries once the
    link is back, and the device keeps charging throughout."""
    rig = rigs()
    ctl = await rig.boot()
    await rig.enable_at(FULL)
    mark = rig.sim.mark()

    rig.sim.connectivity_burst(15)
    try:
        await rig.run(ctl.async_poll())
    except Exception:
        pass
    await rig.clock.advance(15)
    try:
        await rig.run(ctl.async_poll())
    except Exception:
        pass
    await rig.clock.advance(5)

    assert rig.sim.state == CHARGING
    assert rig.sim.measured_power_raw() == FULL
    assert (P, 0) not in rig.sim.wire_writes(mark)   # no protective zero write
    assert (CTRL, 2) not in rig.sim.wire_writes(mark)  # no Stop
    assert ctl.intent_enabled


async def test_unsaved_pause_then_restart_from_old_record_does_not_resume(rigs):
    rig = rigs(state=CHARGING)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    durable = dict(rig.persisted[-1])
    assert durable["enabled"] is True
    rig.persist_fails = True
    with pytest.raises(ControlError):
        await rig.run(ctl.async_pause())
    assert rig.sim.measured_power_raw() == 0
    await rig.run(ctl.async_close())
    rig.sim.simulate_process_restart()
    rig.persist_fails = False
    mark = rig.sim.mark()
    ctl2 = await rig.boot(durable)
    await rig.clock.advance(120)
    assert rig.sim.positive_cap_writes(mark) == []
    assert rig.sim.measured_power_raw() == 0 and not ctl2.intent_enabled


async def test_documented_boundary_unsaved_and_unconfirmed_pause_then_restart(rigs):
    """Documented limit (C3): the pause save fails AND the
    zero is never applied, then the whole process restarts. The pause
    raised, so no success was ever reported; the charger never stopped.
    The next process sees the older enabled record and an active session,
    so it restores that session under its old cap - never above it.
    Closing this needs an independent write-ahead medium."""
    rig = rigs(state=CHARGING)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    durable = dict(rig.persisted[-1])
    rig.persist_fails = True
    rig.sim.inject_write(P, "ignored", count=None)
    with pytest.raises(ControlError):
        await rig.run(ctl.async_pause())
    assert rig.sim.state == CHARGING             # the zero never applied
    await rig.run(ctl.async_close())
    rig.sim.simulate_process_restart()
    rig.sim.clear_write_rules()
    rig.persist_fails = False
    t = rig.clock()
    ctl2 = await rig.boot(durable)
    await rig.clock.advance(120)
    assert ctl2.intent_enabled                   # the known boundary
    assert rig.sim.max_power_limit(t, rig.clock()) <= CAP
    assert rig.sim.max_measured_power(t, rig.clock()) <= CAP


async def test_status_2_stuck_after_confirmation_is_revoked(rigs):
    """N7: status 2 confirms a start, but not indefinitely."""
    rig = rigs(start_delay=100000)               # never leaves "starting"
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    assert (await rig.run(ctl.async_enable())).outcome == "confirmed"
    await rig.clock.advance(45)
    assert ctl.intent_enabled                    # within starting_timeout
    await rig.clock.advance(60)
    assert not ctl.intent_enabled
    assert rig.sim.holding[P] == 0
    assert "starting" in ctl.diagnostics["last_error"]
    mark = rig.sim.mark()
    await rig.clock.advance(120)
    assert rig.sim.positive_cap_writes(mark) == []
