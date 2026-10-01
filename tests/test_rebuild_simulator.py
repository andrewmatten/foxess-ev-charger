"""The simulator itself must behave as specified, or every contract test
driven through it proves nothing."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_DEFAULT_CURRENT,
    REG_ID_MODEL_CODE,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
    REG_TIME_VALIDITY,
)

from rebuild_simulator import (
    CHARGING,
    CONNECTED,
    FINISHED,
    IDLE,
    MAX_CURRENT_RAW,
    MAX_POWER_RAW,
    CommandRefused,
    LegacyClient,
    SimCharger,
    TransportError,
    UnknownOutcome,
    VirtualClock,
)

P, C, CTRL = REG_MAX_CHARGING_POWER, REG_MAX_CHARGING_CURRENT, REG_CHARGING_CONTROL


async def status_of(sim: SimCharger) -> tuple[int, int, int]:
    """(status, cc_status, measured power raw) via a fresh wire read."""
    regs = await sim.read(REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)
    return regs[3], regs[5], regs[14]


async def power_reg(sim: SimCharger) -> int:
    return (await sim.read(P, 1))[0]


def test_exceptions_share_transport_error_base():
    assert issubclass(CommandRefused, TransportError)
    assert issubclass(UnknownOutcome, TransportError)


# ── validity / expiry ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "reported, effective", [(60, 60), (180, 180), (180, 60)],
)
async def test_setpoint_expires_at_effective_not_reported_validity(reported, effective):
    sim = SimCharger(reported_validity=reported, effective_validity=effective, state=CHARGING)
    assert (await sim.read(REG_TIME_VALIDITY, 1))[0] == reported
    await sim.write(P, 14)
    await sim.write(C, 100)
    sim.advance(effective - 0.5)
    assert await power_reg(sim) == 14
    assert (await sim.read(C, 1))[0] == 100
    sim.advance(1.0)
    assert await power_reg(sim) == MAX_POWER_RAW
    assert (await sim.read(C, 1))[0] == MAX_CURRENT_RAW


async def test_refresh_restarts_expiry_window():
    sim = SimCharger(reported_validity=180, effective_validity=60, state=CHARGING)
    for _ in range(20):
        await sim.write(P, 14)
        sim.advance(30)
    t_end = sim.now()
    assert sim.max_power_limit(t_end - 600, t_end) == 14
    assert sim.max_measured_power(t_end - 600, t_end) == 14


async def test_writing_validity_shortens_effective_expiry():
    sim = SimCharger(reported_validity=180, effective_validity=180, state=CHARGING)
    await sim.write(REG_TIME_VALIDITY, 60)
    await sim.write(P, 14)
    sim.advance(61)
    assert await power_reg(sim) == MAX_POWER_RAW


async def test_sawtooth_is_visible_in_timeline_with_90s_refresh():
    sim = SimCharger(reported_validity=180, effective_validity=60, state=CHARGING)
    t0 = sim.now()
    for _ in range(6):
        await sim.write(P, 14)
        for _ in range(9):  # HA keeps polling every 10 s
            sim.advance(10)
            await sim.read(REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)
    assert not sim.fallback_active
    assert sim.max_measured_power(t0, sim.now()) == MAX_POWER_RAW


# ── zero pause / implicit resume / Start refusal ──────────────────────────

async def test_zero_power_pauses_and_positive_power_resumes():
    sim = SimCharger(state=CHARGING)
    await sim.write(P, 0)
    status, cc, power = await status_of(sim)
    assert (status, cc, power) == (5, 1, 0)
    await sim.write(P, 30)
    status, _, power = await status_of(sim)
    assert (status, power) == (3, 30)


async def test_zero_pause_ignored_keeps_charging_though_register_reads_zero():
    sim = SimCharger(state=CHARGING, zero_pause_works=False)
    await sim.write(P, 0)
    assert await power_reg(sim) == 0
    status, _, power = await status_of(sim)
    assert status == 3 and power == MAX_POWER_RAW


async def test_zero_pause_lapses_when_not_refreshed():
    # The setpoint reverts to the device max, but the finished session stays
    # finished: only a fresh nonzero write resumes it (firmware 1.8).
    sim = SimCharger(state=CHARGING, reported_validity=60)
    await sim.write(P, 0)
    sim.advance(30)
    await sim.read(REG_STATUS_BLOCK_START, 1)  # keep the link alive
    sim.advance(31)
    status, _, power = await status_of(sim)
    assert status == 5 and power == 0
    assert await power_reg(sim) == MAX_POWER_RAW


@pytest.mark.parametrize("start_state", [CONNECTED, FINISHED])
@pytest.mark.parametrize("register, value", [(P, 14), (C, 60)])
async def test_nonzero_limit_write_implicitly_resumes(start_state, register, value):
    sim = SimCharger(state=start_state)
    await sim.write(register, value)
    assert sim.state == CHARGING
    assert sim.implicit_resumes == 1


async def test_limit_write_does_not_start_without_cable():
    sim = SimCharger(state=IDLE)
    await sim.write(P, 14)
    assert sim.state == IDLE


async def test_redundant_start_refused_with_exception_03():
    sim = SimCharger(state=CONNECTED)
    await sim.write(P, 14)  # implicit resume
    with pytest.raises(CommandRefused) as err:
        await sim.write(CTRL, 1)
    assert err.value.code == 3
    assert sim.state == CHARGING
    assert sim.refused_starts == 1


async def test_start_from_connected_and_stop():
    sim = SimCharger(state=CONNECTED, start_delay=2)
    await sim.write(CTRL, 1)
    assert (await status_of(sim))[0] == 2
    sim.advance(2)
    assert (await status_of(sim))[0] == 3
    await sim.write(CTRL, 2)
    status, _, power = await status_of(sim)
    assert (status, power) == (5, 0)


# ── Stop outcomes ─────────────────────────────────────────────────────────

async def test_stop_refused():
    sim = SimCharger(state=CHARGING)
    sim.inject_write(CTRL, "refuse", value=2, count=None)
    for _ in range(2):
        with pytest.raises(CommandRefused):
            await sim.write(CTRL, 2)
    assert sim.state == CHARGING


async def test_stop_lost_reply_is_applied():
    sim = SimCharger(state=CHARGING)
    sim.inject_write(CTRL, "lost_reply", value=2)
    with pytest.raises(UnknownOutcome):
        await sim.write(CTRL, 2)
    assert (await status_of(sim))[0] == 5


async def test_lost_request_is_not_applied():
    sim = SimCharger(state=CHARGING)
    sim.inject_write(P, "lost_request")
    with pytest.raises(UnknownOutcome):
        await sim.write(P, 14)
    assert await power_reg(sim) == MAX_POWER_RAW


async def test_stop_delayed_applies_after_ack():
    sim = SimCharger(state=CHARGING)
    sim.inject_write(CTRL, "delayed", value=2, delay=4)
    await sim.write(CTRL, 2)
    assert (await status_of(sim))[0] == 3
    sim.advance(4)
    assert (await status_of(sim))[0] == 5


async def test_ignored_write_is_acknowledged_but_never_applied():
    sim = SimCharger(state=CHARGING)
    sim.inject_write(P, "ignored")
    await sim.write(P, 14)
    sim.advance(10)
    assert await power_reg(sim) == MAX_POWER_RAW
    assert sim.writes[-1].acknowledged and sim.writes[-1].applied_at is None


async def test_stop_while_not_running_is_refused():
    for start_state in (CONNECTED, FINISHED):
        sim = SimCharger(state=start_state)
        with pytest.raises(CommandRefused) as err:
            await sim.write(CTRL, 2)
        assert err.value.code == 3


async def test_stop_after_status_5_zero_pause_is_refused():
    """Firmware 1.8: a zero-paused session reports finished (5) and a Stop
    on it is refused with exception 0x03, like any finished session."""
    sim = SimCharger(state=CHARGING)
    await sim.write(P, 0)
    assert sim.status == 5
    with pytest.raises(CommandRefused) as err:
        await sim.write(CTRL, 2)
    assert err.value.code == 3


async def test_setpoint_expiry_while_finished_does_not_resume():
    """Stopping a session (0x4001=2) leaves it finished; letting the power
    setpoint lapse back to the device max on its own must not restart
    charging (firmware 1.8) -- only a fresh nonzero write does."""
    sim = SimCharger(state=CHARGING, reported_validity=60)
    await sim.write(CTRL, 2)
    assert (await status_of(sim))[0] == 5
    sim.advance(61)  # setpoint expiry lapses with nobody polling
    status, _, power = await status_of(sim)
    assert status == 5 and power == 0
    assert await power_reg(sim) == MAX_POWER_RAW
    sim.advance(120)
    status, _, power = await status_of(sim)
    assert status == 5 and power == 0


async def test_nonzero_cap_after_stop_restarts_session():
    sim = SimCharger(state=CHARGING)
    await sim.write(CTRL, 2)
    await sim.write(P, 14)
    assert sim.state == CHARGING


async def test_out_of_range_writes_refused():
    sim = SimCharger(state=CHARGING)
    for addr, value in [(C, 10), (P, 999), (0x4003, 1), (0x300A, 1), (0x2000, 1)]:
        with pytest.raises(CommandRefused):
            await sim.write(addr, value)


# ── fallback on silence ───────────────────────────────────────────────────

async def test_fallback_current_applies_when_ha_goes_silent():
    sim = SimCharger(state=CHARGING, reported_validity=60, fallback_current_raw=60)
    await sim.write(P, 0)  # zero-paused
    assert (await status_of(sim))[2] == 0
    sim.advance(61)  # no request reaches the device
    assert sim.fallback_active
    status, _, power = await status_of(sim)
    # charging again at the 6 A fallback, not stopped
    assert status == 3
    assert power == 13
    assert sim.fallback_active  # a read alone does not restore control
    await sim.write(P, 0)
    assert not sim.fallback_active
    assert (await status_of(sim))[2] == 0


async def test_io_down_counts_as_silence():
    sim = SimCharger(state=CHARGING, reported_validity=60, fallback_current_raw=60)
    await sim.write(P, 0)
    sim.set_io_down()
    for _ in range(4):
        sim.advance(20)
        with pytest.raises(TransportError):
            await sim.read(REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)
    assert sim.fallback_active
    assert sim.measured_power_raw() == 13


async def test_regular_polling_prevents_fallback():
    sim = SimCharger(state=CHARGING, reported_validity=60)
    for _ in range(10):
        sim.advance(20)
        await sim.read(REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)
    assert not sim.fallback_active


# ── telemetry / cable / vehicle ───────────────────────────────────────────

async def test_invalid_telemetry_injection():
    sim = SimCharger(state=FINISHED)
    sim.set_invalid_telemetry()
    status, cc, _ = await status_of(sim)
    assert (status, cc) == (0xFFFF, 0xFFFF)
    sim.clear_invalid_telemetry()
    assert (await status_of(sim))[:2] == (5, 1)


async def test_unplug_and_plug():
    sim = SimCharger(state=CHARGING)
    await sim.write(P, 14)
    sim.unplug()
    status, cc, _ = await status_of(sim)
    assert (status, cc) == (0, 0)
    assert await power_reg(sim) == MAX_POWER_RAW
    sim.plug()
    assert (await status_of(sim))[:2] == (1, 1)


async def test_plug_and_charge_mode_autostarts():
    sim = SimCharger(state=IDLE, work_mode=1)
    sim.plug()
    assert (await status_of(sim))[0] == 3


async def test_car_pause_and_status_9():
    sim = SimCharger(state=CHARGING)
    sim.car_pause()
    status, _, power = await status_of(sim)
    assert (status, power) == (4, 0)
    await sim.write(P, 30)  # car pause is not lifted by a limit write
    assert (await status_of(sim))[0] == 4
    sim.car_resume()
    sim.begin_phase_switch()
    assert (await status_of(sim))[0] == 9
    sim.end_phase_switch()
    assert (await status_of(sim))[0] == 3


async def test_energy_accumulates_from_measured_power():
    sim = SimCharger(state=CHARGING)
    await sim.write(P, 70)
    before = (await sim.read(0x1016, 2))
    for _ in range(4):
        sim.advance(15)
        await sim.write(P, 70)
    after = (await sim.read(0x1016, 2))
    delta = ((after[0] << 16) | after[1]) - ((before[0] << 16) | before[1])
    assert delta == 1  # 7.0 kW for 60 s = 0.117 kWh -> one 0.1 kWh tick


async def test_identity_and_unknown_address_reads():
    sim = SimCharger()
    client = LegacyClient(sim)
    assert client.read_ascii(REG_ID_MODEL_CODE, 4) == "A7300P1-"
    with pytest.raises(CommandRefused):
        await sim.read(0x300A, 2)
    assert client.read_registers(0x300A, 2) is None


async def test_read_failure_injection_and_no_client_cache():
    sim = SimCharger(state=CHARGING)
    sim.fail_reads(REG_MAX_CHARGING_POWER - 2)
    with pytest.raises(TransportError):
        await sim.read(REG_MAX_CHARGING_POWER - 2, 7)
    sim.inject_write(P, "ignored")
    await sim.write(P, 14)
    # nothing remembers the acknowledged 14
    assert (await sim.read(REG_MAX_CHARGING_POWER - 2, 7))[2] == MAX_POWER_RAW


# ── ordering, gates, lifecycle ────────────────────────────────────────────

async def test_hold_gate_keeps_write_in_flight_until_released():
    sim = SimCharger(state=CHARGING)
    gate = sim.hold_writes(P)
    task = asyncio.ensure_future(sim.write(P, 70))
    await gate.entered.wait()
    await sim.write(C, 100)  # not held
    assert await power_reg(sim) == MAX_POWER_RAW
    gate.release()
    await task
    assert await power_reg(sim) == 70
    assert sim.wire_writes() == [(C, 100), (P, 70)]


async def test_write_log_orders_and_marks():
    sim = SimCharger(state=CHARGING)
    await sim.write(P, 70)
    mark = sim.mark()
    await sim.write(P, 14)
    await sim.write(REG_DEFAULT_CURRENT, 60)
    assert sim.wire_writes(mark) == [(P, 14), (REG_DEFAULT_CURRENT, 60)]
    assert [w.value for w in sim.positive_cap_writes(mark)] == [14]


async def test_close_then_restart_keeps_device_state():
    sim = SimCharger(state=CHARGING)
    await sim.write(P, 14)
    await sim.close()
    with pytest.raises(TransportError):
        await sim.read(P, 1)
    io = sim.simulate_process_restart()
    assert await power_reg(io) == 14
    assert io.state == CHARGING


async def test_legacy_client_maps_failures_to_false():
    sim = SimCharger(state=CHARGING)
    client = LegacyClient(sim)
    assert client.write_holding_register(CTRL, 1) is False  # refused start
    sim.inject_write(P, "lost_reply")
    assert client.write_holding_register(P, 14) is False
    assert sim.holding[P] == 14  # yet it was applied


async def test_virtual_clock_run_advances_to_sleepers():
    clock = VirtualClock()
    sim = SimCharger(clock=clock, state=CHARGING, reported_validity=60)
    order = []

    async def worker():
        await sim.write(P, 14)
        await clock.sleep(30)
        order.append(("refresh", clock()))
        await sim.write(P, 14)
        await clock.sleep(30)
        return await power_reg(sim)

    t0 = clock()
    assert await clock.run(worker()) == 14
    assert order == [("refresh", t0 + 30)]
    await clock.advance(61)
    assert await power_reg(sim) == MAX_POWER_RAW


async def test_cancelled_caller_does_not_recall_in_flight_write():
    sim = SimCharger(state=CHARGING)
    gate = sim.hold_writes(P)
    task = asyncio.ensure_future(sim.write(P, 14))
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release()
    for _ in range(5):
        await asyncio.sleep(0)
    assert await power_reg(sim) == 14


async def test_public_observers_see_time_based_transitions():
    sim = SimCharger(state=CHARGING, reported_validity=60)
    await sim.write(P, 0)
    sim.clock.now += 61  # no sync call in between
    assert sim.fallback_active
    assert sim.state == CHARGING
    sim.state = FINISHED
    assert sim.measured_power_raw() == 0
