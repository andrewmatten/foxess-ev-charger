"""Behavioural tests for the single charging controller (controller.py).

The fake below implements the RegisterIO contract directly: reads return the
fake's current physical registers, writes may be refused, lost, ignored or
held at a barrier. Its physics model is deliberately small: a nonzero
current/power write while connected (re)starts charging, zero power pauses
unless configured not to, and 0x4001=2 stops unless configured not to.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from custom_components.foxess_charger.transport import CommandRefused, TransportError, UnknownOutcome
from custom_components.foxess_charger.controller import (
    ChargingController,
    ControlError,
)

CUR, POW, CTRL, VALIDITY, FALLBACK = 0x3001, 0x3002, 0x4001, 0x3005, 0x3006
STATUS, CC, POWER, LOCK = 0x1003, 0x1005, 0x100E, 0x100F


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # Let runnable work proceed first; a cancelled sleep never moves time.
        start = self.now
        for _ in range(20):
            await asyncio.sleep(0)
        self.now = max(self.now, start + max(0.0, seconds))


class FakeIO:
    def __init__(self, clock: FakeClock, *, charging: bool = True) -> None:
        self.clock = clock
        self.regs: dict[int, int] = defaultdict(int)
        self.regs.update({CC: 1, VALIDITY: 180, FALLBACK: 80, CUR: 320, POW: 73, 0x1011: 73})
        self.stopped = not charging
        self.zero_pauses = True
        self.stop_works = True
        self.car_draw = True
        self.refuse: set[int] = set()
        self.lose_reply: set[int] = set()
        self.ignore: set[int] = set()
        self.fail_reads = False
        self.status_override: int | None = None
        self.cc_override: int | None = None
        self.hooks: dict[int, object] = {}
        self.writes: list[tuple[float, int, int]] = []
        self.ramp_down_s = 0.0           # car keeps drawing this long after zero
        self._zero_at: float | None = None
        self.closed = False
        self._physics()

    async def read(self, address: int, count: int) -> tuple[int, ...]:
        await asyncio.sleep(0)
        if self.fail_reads:
            raise TransportError("read failed")
        self._physics()
        values = [self.regs[address + i] for i in range(count)]
        for reg, override in ((STATUS, self.status_override), (CC, self.cc_override)):
            if override is not None and address <= reg < address + count:
                values[reg - address] = override
        return tuple(values)

    async def write(self, address: int, value: int) -> None:
        hook = self.hooks.get(address)
        if hook is not None:
            await hook(value)
        await asyncio.sleep(0)
        self.writes.append((self.clock(), address, value))
        if address in self.refuse:
            raise CommandRefused("refused")
        if address not in self.ignore:
            self._apply(address, value)
        if address in self.lose_reply:
            raise UnknownOutcome("reply lost")

    async def close(self) -> None:
        self.closed = True

    def _apply(self, address: int, value: int) -> None:
        if address == CTRL:
            if value == 2 and self.stop_works:
                self.stopped = True
        elif address == 0x4000:
            self.regs[LOCK] = value - 1
        elif address == 0x4002:
            self.regs[0x1010] = value     # phase sequence follows the command
        else:
            self.regs[address] = value
            if address == POW:
                self._zero_at = self.clock() if value == 0 else None
            if address in (CUR, POW) and value > 0:
                self.stopped = False  # implicit resume on a nonzero limit write
        self._physics()

    def _physics(self) -> None:
        r = self.regs
        if r[CC] == 0 or not self.car_draw:
            r[STATUS], r[POWER] = (0 if r[CC] == 0 else 1), 0
        elif self.stopped:
            r[STATUS], r[POWER] = 5, 0
        elif r[POW] == 0 and self.zero_pauses and (
            self._zero_at is None or self.clock() >= self._zero_at + self.ramp_down_s
        ):
            r[STATUS], r[POWER] = 4, 0
        elif r[POW] == 0 and self.zero_pauses:
            r[STATUS], r[POWER] = 3, 70  # car still ramping down
        else:
            r[STATUS], r[POWER] = 3, (r[POW] or 73)

    def power_writes(self) -> list[int]:
        return [v for _, a, v in self.writes if a == POW]


async def snapshot(io) -> dict:
    regs = await io.read(0x1000, 28)
    status, cc = regs[3], regs[5]
    ok_s, ok_c = status in {0, 1, 2, 3, 4, 5, 6, 8, 9}, cc in (0, 1)
    return {
        "status": status if ok_s else None, "status_valid": ok_s,
        "cc_status": cc if ok_c else None, "cc_status_valid": ok_c,
        "power_raw": regs[14], "lock_status": regs[15],
        "fault_code": regs[26] << 16 | regs[27],
    }


def saved(enabled=True, power=70, current=None, **kw):
    return {"schema": 1, "enabled": enabled, "power_raw": power, "current_raw": current,
            "revision": 5, "safety_latched": False, "stop_fallback": False, **kw}


async def make(state=None, *, safety=False, charging=True, io=None, persist=None, **kw):
    clock = FakeClock()
    io = io or FakeIO(clock, charging=charging)
    io.clock = clock
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot,
                           persist=persist, **kw)
    await c.async_initialize(saved() if state is None else state, configure_safety=safety)
    io.writes.clear()
    return c, io


async def spin(predicate, rounds=2000):
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


# ── on/off through the power setpoint ─────────────────────────────────────

async def test_pause_writes_zero_power_and_confirms_without_0x4001():
    c, io = await make()
    result = await c.async_pause()
    assert result.outcome == "confirmed" and result.observed == 0
    assert io.power_writes() == [0]
    assert not any(a == CTRL for _, a, _ in io.writes)
    assert io.regs[POWER] == 0 and c.phase == "paused" and not c.intent_enabled


async def test_enable_resumes_saved_power_and_current_never_start():
    c, io = await make(saved(enabled=False, power=50, current=100), charging=False)
    result = await c.async_enable()
    assert result.outcome == "confirmed" and result.observed == 50
    assert [(a, v) for _, a, v in io.writes] == [(CUR, 100), (POW, 50)]
    assert (CTRL, 1) not in [(a, v) for _, a, v in io.writes]
    assert io.regs[STATUS] == 3 and c.phase == "enabled"


async def test_enable_fails_visibly_when_no_session_starts():
    c, io = await make(saved(enabled=False), charging=False)
    io.car_draw = False
    with pytest.raises(ControlError):
        await c.async_enable()
    # The failed enable must not leave positive authorization armed.
    assert not c.intent_enabled and io.regs[POW] == 0
    io.writes.clear()
    io.car_draw = True               # the car becomes ready later
    await c.async_start()
    for _ in range(300):
        await asyncio.sleep(0)
    await c.async_poll()
    for _ in range(300):
        await asyncio.sleep(0)
    await c.async_close()
    assert not [v for _, a, v in io.writes if a in (CUR, POW) and v > 0]
    assert io.regs[STATUS] != 3 and io.regs[POWER] == 0


async def test_enable_refused_by_charger_revokes_and_zeroes():
    c, io = await make(saved(enabled=False), charging=False)
    io.status_override = 8           # locked: refuses to charge
    with pytest.raises(ControlError):
        await c.async_enable()
    assert not c.intent_enabled and io.regs[POW] == 0


async def test_power_below_minimum_is_a_visible_pause_never_raised():
    c, io = await make()
    result = await c.async_set_power(10)
    assert result.outcome == "confirmed" and result.observed == 0
    assert io.power_writes() == [0]
    assert c.phase == "paused_below_minimum" and c.desired_power_raw == 10
    assert c.intent_enabled


async def test_current_ceiling_written_with_power_when_enabled():
    c, io = await make()
    await c.async_set_current(100)
    assert [(a, v) for _, a, v in io.writes] == [(CUR, 100), (POW, 70)]
    assert io.regs[CUR] == 100


# ── staging while off (stop-then-resume incident: updates while off must not resume) ─

async def test_limits_only_stage_while_paused():
    c, io = await make(saved(enabled=False), charging=False)
    assert (await c.async_set_power(73)).outcome == "staged"
    assert (await c.async_set_current(320)).outcome == "staged"
    assert io.writes == [] and not c.intent_enabled
    assert c.desired_power_raw == 73 and c.desired_current_raw == 320
    await c.async_start()
    await spin(lambda: len(io.writes) >= 1)
    await c.async_close()
    assert all(v == 0 for _, a, v in io.writes if a in (CUR, POW))
    assert io.regs[STATUS] != 3


async def test_implicit_resume_while_off_is_corrected():
    c, io = await make(saved(enabled=False), charging=False)
    io.regs[POW] = 73
    io.stopped = False
    io._physics()                    # firmware/car resumes by itself
    await c.async_poll()
    await spin(lambda: io.power_writes() == [0] and c.phase == "paused")
    assert c.diagnostics["implicit_resume_corrections"] == 1
    assert io.regs[POWER] == 0


# ── Stop fallback ─────────────────────────────────────────────────────────

async def test_zero_ignored_triggers_confirmed_stop_and_latches():
    c, io = await make()
    io.zero_pauses = False
    result = await c.async_pause()
    assert result.outcome == "confirmed"
    assert (CTRL, 2) in [(a, v) for _, a, v in io.writes]
    assert c.phase == "stopped" and c.diagnostics["stop_fallbacks"] == 1
    assert c.export_state()["stop_fallback"] is True


async def test_stop_fallback_suppresses_all_cap_writes_until_explicit_enable():
    c, io = await make()
    io.zero_pauses = False
    await c.async_pause()
    io.writes.clear()
    assert (await c.async_set_power(50)).outcome == "staged"
    assert (await c.async_set_current(100)).outcome == "staged"
    await c.async_start()
    for _ in range(50):
        await asyncio.sleep(0)
    assert not [w for w in io.writes if w[1] in (CUR, POW)]
    assert io.regs[STATUS] == 5
    result = await c.async_enable()
    await c.async_close()
    assert result.outcome == "confirmed" and c.phase == "enabled"
    assert io.regs[POW] == 50 and io.regs[STATUS] == 3
    assert (CTRL, 1) not in [(a, v) for _, a, v in io.writes]


async def test_failed_stop_latches_fault_and_raises():
    c, io = await make()
    io.zero_pauses = False
    io.stop_works = False
    with pytest.raises(ControlError):
        await c.async_pause()
    assert c.phase == "faulted" and c.export_state()["safety_latched"] is True
    await c.async_close()


# ── invalid telemetry (review finding 3) ─────────────────────────────────

async def test_invalid_status_never_confirms_stop():
    c, io = await make()
    io.zero_pauses = False
    io.status_override = 65535       # Stop applies, but status is unknown
    with pytest.raises(ControlError):
        await c.async_pause()
    assert c.phase == "faulted" and not c.export_state()["stop_fallback"]


async def test_invalid_status_never_confirms_pause():
    c, io = await make()
    io.status_override = 65535
    io.stop_works = False
    with pytest.raises(ControlError):
        await c.async_pause()
    assert c.phase == "faulted"


async def test_invalid_connector_forces_protective_zero_and_uncertain():
    c, io = await make()
    io.cc_override = 65535           # neither connected nor unplugged
    await c.async_poll()
    io.clock.now += 25               # sustained past the telemetry grace
    await c.async_poll()
    await spin(lambda: io.power_writes() == [0])
    assert c.phase == "uncertain" and c.intent_enabled
    io.cc_override = None
    await c.async_poll()             # one good frame is not recovery
    for _ in range(300):
        await asyncio.sleep(0)
    assert io.power_writes() == [0] and c.phase == "uncertain"
    io.clock.now += 1.0              # second consistent frame, gap apart
    await c.async_poll()
    await spin(lambda: io.power_writes() == [0, 70])
    assert c.phase == "enabled"


async def test_isolated_good_frame_inside_outage_keeps_protective_zero():
    c, io = await make()
    io.fail_reads = True
    await c.async_poll()
    io.clock.now += 25
    await c.async_poll()
    await spin(lambda: io.power_writes() == [0])
    for good in (True, False, True, False):
        io.fail_reads = not good
        io.clock.now += 2
        await c.async_poll()
        for _ in range(300):
            await asyncio.sleep(0)
    assert c.phase == "uncertain"
    assert not [v for _, a, v in io.writes if a in (CUR, POW) and v > 0]


async def test_read_failure_is_uncertain_not_stopped():
    c, io = await make(saved(enabled=False), charging=False)
    io.fail_reads = True
    await c.async_poll()
    io.clock.now += 25
    await c.async_poll()
    await spin(lambda: c.diagnostics["refresh_failures"] >= 1)
    assert c.phase == "uncertain" and c.export_state()["stop_fallback"] is False


# ── confirmation needs fresh read-back (review finding 4) ─────────────────

async def test_lost_reply_with_matching_readback_confirms():
    c, io = await make()
    io.lose_reply.add(POW)
    result = await c.async_set_power(50)
    assert result.outcome == "confirmed" and io.regs[POW] == 50
    assert c.diagnostics["unknown_outcomes"] == 1


async def test_ack_without_application_never_confirms():
    c, io = await make(confirmation_timeout=10.0, retry_interval=3.0)
    io.ignore.add(POW)               # ACK, but hardware keeps 70
    with pytest.raises(ControlError):
        await c.async_set_power(14)
    assert len(io.power_writes()) >= 3       # retried within the deadline
    assert c.phase == "faulted" and c.export_state()["safety_latched"]
    assert io.power_writes()[-1] == 0        # protective zero attempted


async def test_refusal_is_counted_and_retried():
    c, io = await make()
    io.refuse.add(POW)
    with pytest.raises(ControlError):
        await c.async_set_power(50)
    assert c.diagnostics["refusals"] >= 2


# ── stale write race (review finding 1) ───────────────────────────────────

async def test_old_refresh_cannot_overwrite_newer_lower_power():
    c, io = await make(saved(power=70, current=100))
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(_value):
        if not entered.is_set():
            entered.set()
            await release.wait()

    io.hooks[CUR] = barrier
    await c.async_start()            # refresh reaches the current write, blocks
    await asyncio.wait_for(entered.wait(), 1)
    update = asyncio.create_task(c.async_set_power(14))
    await spin(lambda: c.desired_power_raw == 14)
    release.set()
    result = await update
    await c.async_close()
    writes = io.power_writes()
    assert io.regs[POW] == 14 and result.outcome == "confirmed"
    assert 70 not in writes[writes.index(14):], writes
    assert 70 not in writes, "stale refresh value reached the wire"


async def test_pause_supersedes_queued_positive_work():
    c, io = await make(saved(power=70, current=100))
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(_value):
        if not entered.is_set():
            entered.set()
            await release.wait()

    io.hooks[CUR] = barrier
    raise_power = asyncio.create_task(c.async_set_power(73))
    await asyncio.wait_for(entered.wait(), 1)
    pause = asyncio.create_task(c.async_pause())
    await spin(lambda: not c.intent_enabled)
    release.set()
    assert (await raise_power).outcome == "superseded"
    assert (await pause).outcome == "confirmed"
    assert io.power_writes() == [0] and io.regs[POWER] == 0


# ── cancellation (review finding 2) ───────────────────────────────────────

async def test_cancelled_caller_does_not_abort_or_wedge_the_controller():
    c, io = await make()
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(_value):
        entered.set()
        await release.wait()

    io.hooks[POW] = barrier
    caller = asyncio.create_task(c.async_pause())
    await asyncio.wait_for(entered.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    del io.hooks[POW]
    release.set()
    await spin(lambda: c.phase == "paused")
    assert io.regs[POWER] == 0       # the pause still completed
    assert (await c.async_enable()).outcome == "confirmed"   # lock not stuck
    await c.async_start()
    await spin(lambda: c.diagnostics["refreshes"] >= 1)
    await c.async_close()


# ── refresh loop timing ───────────────────────────────────────────────────

async def _write_times(c, io, count):
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= count)
    await c.async_close()
    return [t for t, a, _ in io.writes if a == POW][:count]


async def test_refresh_every_30s_even_when_validity_reports_180():
    c, io = await make()
    times = await _write_times(c, io, 4)
    assert [round(b - a, 3) for a, b in zip(times, times[1:])] == [30.0] * 3


async def test_refresh_is_half_of_a_short_validity():
    c, io = await make()
    io.regs[VALIDITY] = 20
    times = await _write_times(c, io, 4)
    assert [round(b - a, 3) for a, b in zip(times[1:], times[2:])] == [10.0] * 2


async def test_refresh_keeps_zero_and_current_ceiling_alive():
    c, io = await make(saved(enabled=False, current=100), charging=False)
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= 2)
    await c.async_close()
    assert set(io.power_writes()) == {0} and not [w for w in io.writes if w[1] == CUR]
    c, io = await make(saved(current=100))
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= 2)
    await c.async_close()
    assert [v for _, a, v in io.writes if a == CUR][:2] == [100, 100]


async def test_failed_refresh_retries_at_retry_interval():
    c, io = await make(retry_interval=3.0)
    io.ignore.add(POW)
    io.regs[POW] = 50                # read-back mismatches: refresh fails
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= 3)
    await c.async_close()
    times = [t for t, a, _ in io.writes if a == POW]
    assert round(times[1] - times[0], 3) == 3.0
    assert c.diagnostics["refresh_failures"] >= 2


# ── registers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("address", [0x4003, 0x4001, 0x3002, 0x3001, 0x3007, 0x1003])
async def test_non_allowlisted_registers_never_reach_the_wire(address):
    c, io = await make()
    with pytest.raises(ValueError):
        await c.async_set_register(address, 1)
    assert io.writes == []


async def test_register_write_confirmed_by_readback():
    c, io = await make()
    assert (await c.async_set_register(0x3000, 1)).outcome == "confirmed"
    io.ignore.add(0x3003)
    with pytest.raises(ControlError):
        await c.async_set_register(0x3003, 120)


async def test_lock_confirmed_by_lock_status():
    c, io = await make()
    result = await c.async_set_register(0x4000, 2)
    assert result.outcome == "confirmed" and io.regs[LOCK] == 1


# ── initialization and persistence ────────────────────────────────────────

async def test_saved_off_wins_over_active_session():
    c, io = await make(saved(enabled=False), charging=True)
    assert not c.intent_enabled and c.phase == "paused"
    assert io.regs[POWER] == 0 and io.regs[STATUS] == 4


@pytest.mark.parametrize("hardware", ["finished", "connected_idle", "unplugged"])
async def test_saved_enabled_never_resumes_an_inactive_charger_on_restore(hardware):
    """Firmware 1.8: a nonzero 0x3002 write resumes a finished session, so a
    restored enabled intent needs a fresh active-session observation."""
    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    if hardware == "connected_idle":
        io.stopped, io.car_draw = False, False
    elif hardware == "unplugged":
        io.regs[CC] = 0
    io._physics()
    before = io.regs[STATUS]
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(enabled=True, power=70), configure_safety=False)
    assert not [v for _, a, v in io.writes if a in (CUR, POW) and v > 0]
    assert not c.intent_enabled and c.desired_power_raw == 70
    assert io.regs[STATUS] == before
    io.writes.clear()
    await c.async_start()
    for _ in range(200):
        await asyncio.sleep(0)
    await c.async_close()
    assert not [v for _, a, v in io.writes if a in (CUR, POW) and v > 0]


async def test_malformed_saved_state_starts_paused():
    for bad in ({"schema": 1, "enabled": "yes"}, {"schema": 9}, [], saved(power=True)):
        c, io = await make(bad, charging=True)
        assert not c.intent_enabled and io.regs[POWER] == 0


async def test_first_install_adopts_only_an_active_session():
    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    io.regs[POW] = 14                # live cap 1.4 kW, device max 7.3 kW
    io._physics()
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(None, configure_safety=False)
    assert c.intent_enabled
    # Adopted with the existing live cap, never raised towards the maximum.
    assert c.desired_power_raw == 14
    assert io.power_writes() and set(io.power_writes()) == {14}
    assert io.regs[POWER] == 14
    io2 = FakeIO(clock, charging=False)
    c2 = ChargingController(io2, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c2.async_initialize(None, configure_safety=False)
    assert not c2.intent_enabled


async def test_first_install_adoption_with_unreadable_maximum_writes_zero():
    class NoMaxIO(FakeIO):
        async def read(self, address, count):
            if address <= 0x1011 < address + count and address != 0x1000:
                raise TransportError("unreadable")
            return await super().read(address, count)

    clock = FakeClock()
    io = NoMaxIO(clock, charging=True)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(None, configure_safety=False)
    assert c.desired_power_raw == 0
    assert io.power_writes() and io.power_writes()[0] == 0
    assert io.regs[POWER] == 0


@pytest.mark.parametrize("live", [0, 10, 74])
async def test_first_install_with_invalid_live_cap_adopts_zero(live):
    clock = FakeClock()
    io = FakeIO(clock, charging=True)
    io.regs[POW] = live
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(None, configure_safety=False)
    assert c.desired_power_raw == 0
    assert io.power_writes() and set(io.power_writes()) == {0}


async def test_safety_configuration_written_and_read_back():
    clock = FakeClock()
    io = FakeIO(clock)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(), configure_safety=True)
    assert io.regs[VALIDITY] == 60 and io.regs[FALLBACK] == 60
    assert c.phase == "enabled"
    assert c.diagnostics["refresh_interval"] == 30


async def test_unconfirmed_safety_configuration_blocks_enable():
    clock = FakeClock()
    io = FakeIO(clock)
    io.ignore.add(VALIDITY)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(), configure_safety=True)
    assert c.phase == "faulted" and io.regs[POW] == 0
    with pytest.raises(ControlError):
        await c.async_enable()


async def test_restart_keeps_explicit_off_and_latches():
    c, io = await make()
    io.zero_pauses = False
    await c.async_pause()
    state = c.export_state()
    c2, io2 = await make(state, charging=False)
    assert not c2.intent_enabled and c2.phase == "stopped"
    await c2.async_start()
    for _ in range(50):
        await asyncio.sleep(0)
    await c2.async_close()
    assert not [w for w in io2.writes if w[1] in (CUR, POW)]


async def test_intent_persisted_before_the_write_is_dispatched():
    seen = []
    io_holder = {}

    async def persist(state):
        seen.append((state["enabled"], len(io_holder["io"].writes) if "io" in io_holder else 0))

    clock = FakeClock()
    io = FakeIO(clock)
    io_holder["io"] = io
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot,
                           persist=persist)
    await c.async_initialize(saved(), configure_safety=False)
    io.writes.clear()
    seen.clear()
    await c.async_pause()
    assert seen[0] == (False, 0)


async def test_close_drains_in_flight_work_before_closing_io():
    c, io = await make()
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(_value):
        entered.set()
        await release.wait()

    io.hooks[POW] = barrier
    pause = asyncio.create_task(c.async_pause())
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(c.async_close())
    await asyncio.sleep(0)
    assert not io.closed
    del io.hooks[POW]
    release.set()
    await closing
    assert io.closed and (await pause).outcome == "confirmed"
    with pytest.raises(ControlError):
        await c.async_set_power(50)


async def test_diagnostics_expose_counters_and_phase():
    c, _ = await make()
    diag = c.diagnostics
    for key in ("refreshes", "refresh_failures", "confirmations", "unknown_outcomes",
                "refusals", "stop_fallbacks", "implicit_resume_corrections", "phase"):
        assert key in diag


# ── parent review follow-ups ──────────────────────────────────────────────

async def test_single_lost_read_mid_charge_does_not_interrupt():
    c, io = await make()
    io.fail_reads = True
    await c.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)
    io.fail_reads = False
    await c.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)
    assert 0 not in io.power_writes() and c.phase == "enabled"


async def test_sustained_telemetry_outage_forces_protective_zero():
    c, io = await make()
    io.fail_reads = True
    await c.async_poll()
    io.clock.now += 10
    await c.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)
    assert 0 not in io.power_writes()
    io.clock.now += 15
    await c.async_poll()
    await spin(lambda: 0 in io.power_writes())
    assert c.phase == "uncertain"


async def test_slow_car_ramp_down_does_not_trigger_stop():
    c, io = await make()
    io.ramp_down_s = 15.0
    result = await c.async_pause()
    assert result.outcome == "confirmed" and c.phase == "paused"
    assert not any(a == CTRL for _, a, _ in io.writes)


async def test_enable_while_unplugged_is_staged_and_stays_on():
    c, io = await make(saved(enabled=False), charging=False)
    io.regs[CC] = 0
    result = await c.async_enable()
    assert result.outcome == "staged" and c.intent_enabled


async def test_safety_configuration_retried_until_confirmed():
    clock = FakeClock()
    io = FakeIO(clock)
    io.refuse.add(VALIDITY)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(), configure_safety=True)
    assert c.phase == "faulted"
    assert "safety_config_unconfirmed" in c.diagnostics["protective_reasons"]
    io.refuse.clear()
    await c.async_start()
    await spin(lambda: io.regs[VALIDITY] == 60 and io.regs[FALLBACK] == 60)
    await spin(lambda: c.phase == "enabled")
    assert (await c.async_enable()).outcome == "confirmed"
    await c.async_close()


async def test_queued_command_overtaken_by_pause_reports_superseded():
    c, io = await make(saved(power=70))
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(_value):
        if not entered.is_set():
            entered.set()
            await release.wait()

    io.hooks[POW] = barrier
    await c.async_start()            # refresh holds the lock mid-write
    await asyncio.wait_for(entered.wait(), 1)
    raise_power = asyncio.create_task(c.async_set_power(50))
    await spin(lambda: c.desired_power_raw == 50)
    pause = asyncio.create_task(c.async_pause())
    await spin(lambda: not c.intent_enabled)
    release.set()
    assert (await raise_power).outcome == "superseded"
    assert (await pause).outcome == "confirmed"
    await c.async_close()
    assert 50 not in io.power_writes()


async def test_pause_overtaken_by_staged_change_still_writes_zero():
    c, io = await make()
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(_value):
        if not entered.is_set():
            entered.set()
            await release.wait()

    io.hooks[POW] = barrier
    await c.async_start()
    await asyncio.wait_for(entered.wait(), 1)
    pause = asyncio.create_task(c.async_pause())
    await spin(lambda: not c.intent_enabled)
    assert (await c.async_set_power(50)).outcome == "staged"
    release.set()
    await pause
    await c.async_close()
    assert io.regs[POW] == 0 and io.regs[POWER] == 0



@pytest.mark.parametrize("address,value", [(0x300A, 1), (0x300B, 10)])
async def test_phase_box_registers_confirmed_by_readback(address, value):
    c, io = await make()
    assert (await c.async_set_register(address, value)).outcome == "confirmed"
    assert io.regs[address] == value
    with pytest.raises(ValueError):
        await c.async_set_register(0x300B, 4)


async def test_phase_switching_confirmed_by_phase_sequence():
    c, io = await make()
    result = await c.async_set_register(0x4002, 1)
    assert result.outcome == "confirmed" and io.regs[0x1010] == 1
    with pytest.raises(ValueError):
        await c.async_set_register(0x4002, 3)


async def test_phase_switching_unconfirmable_raises():
    c, io = await make()
    io.ignore.add(0x4002)
    with pytest.raises(ControlError):
        await c.async_set_register(0x4002, 2)
    io.ignore.clear()
    io.fail_reads = True
    with pytest.raises(ControlError):
        await c.async_set_register(0x4002, 1)


async def _failing_persist(_state):
    raise OSError("disk full")


async def test_staged_change_raises_when_it_could_not_be_persisted():
    c, io = await make(saved(enabled=False), charging=False, persist=_failing_persist)
    with pytest.raises(ControlError):
        await c.async_set_power(50)
    with pytest.raises(ControlError):
        await c.async_set_current(100)


async def test_persist_failure_does_not_block_applied_or_protective_work():
    c, io = await make(persist=_failing_persist)
    assert (await c.async_set_power(50)).outcome == "confirmed"
    # The zero still reaches the wire, but the pause is not durable: say so.
    with pytest.raises(ControlError, match="not saved"):
        await c.async_pause()
    assert io.regs[POW] == 0 and io.regs[POWER] == 0
    assert c.diagnostics["persist_failures"] >= 2
    assert c.diagnostics["persist_pending"] is True
    assert c.diagnostics["persist_error"]


async def test_unsaved_pause_is_retried_from_the_refresh_loop():
    fail = {"on": False}
    saved_states = []

    async def persist(state):
        if fail["on"]:
            raise OSError("disk full")
        saved_states.append(dict(state))

    c, io = await make(persist=persist)
    fail["on"] = True
    with pytest.raises(ControlError):
        await c.async_pause()
    assert saved_states[-1]["enabled"] is True     # disk still says enabled
    fail["on"] = False
    await c.async_start()
    await spin(lambda: not c.diagnostics["persist_pending"], rounds=20000)
    await c.async_close()
    assert saved_states[-1]["enabled"] is False
    assert saved_states[-1]["revision"] == c.export_state()["revision"]
    assert c.diagnostics["persist_error"] is None


# ── unplug release, device maximum, persistent mismatch ───────────────────

async def _poll_twice(c, io, gap=1.0):
    await c.async_poll()
    io.clock.now += gap
    await c.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)


async def test_confirmed_unplug_releases_pause_for_a_new_external_session():
    c, io = await make(saved(enabled=False, power=50), charging=False)
    io.regs[CC] = 0
    await _poll_twice(c, io)
    io.writes.clear()
    io.regs[CC] = 1                  # replug; Plug&Charge starts by itself
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    await spin(lambda: c.intent_enabled and io.regs[POW] == 50)
    assert 0 not in io.power_writes() and io.regs[STATUS] == 3


@pytest.mark.parametrize("latch", ["safety_latched", "stop_fallback"])
async def test_unplug_never_releases_a_fault_or_stop_latch(latch):
    c, io = await make(saved(enabled=False, power=50, **{latch: True}), charging=False)
    io.regs[CC] = 0
    await _poll_twice(c, io)
    io.writes.clear()
    io.regs[CC] = 1                  # replug; Plug&Charge starts by itself
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    for _ in range(500):
        await asyncio.sleep(0)
    await c.async_poll()
    await spin(lambda: io.regs[POWER] == 0)
    assert not c.intent_enabled and c.export_state()[latch] is True
    assert not [v for _, a, v in io.writes if a in (CUR, POW) and v > 0]
    assert (await c.async_enable()).outcome == "confirmed"   # explicit way out
    assert c.export_state()[latch] is False


async def test_single_or_invalid_unplug_reading_never_releases_pause():
    c, io = await make(saved(enabled=False), charging=False)
    io.cc_override = 65535
    await _poll_twice(c, io)
    io.cc_override = None
    io.regs[CC] = 0
    await c.async_poll()             # one observation only
    io.regs[CC] = 1
    io.regs[POW] = 73
    io.stopped = False
    await c.async_poll()
    await spin(lambda: io.regs[POWER] == 0)
    assert not c.intent_enabled


async def test_restored_power_above_device_maximum_is_rejected_visibly():
    c, io = await make(saved(power=150), charging=True)
    assert not c.intent_enabled and c.desired_power_raw is None
    assert "maximum" in c.diagnostics["last_error"]
    with pytest.raises(ValueError):
        await c.async_set_power(80)


async def test_persistent_refresh_mismatch_latches_a_fault():
    c, io = await make()
    io.ignore.add(POW)
    io.regs[POW] = 73                # charger keeps running uncapped
    await c.async_start()
    await spin(lambda: c.phase == "faulted", rounds=20000)
    await c.async_close()
    assert c.export_state()["safety_latched"]


async def test_refused_current_refresh_never_starves_the_power_refresh():
    c, io = await make(saved(power=70, current=100))
    io.ignore.add(CUR)
    io.regs[CUR] = 320               # current cap drifted; writes ignored
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= 2, rounds=20000)
    assert io.power_writes()[:2] == [70, 70]
    assert "current_cap_mismatch" in c.diagnostics["degraded_reasons"]
    assert io.regs[POWER] <= 70
    await spin(lambda: c.phase == "faulted", rounds=40000)
    await c.async_close()
    assert c.export_state()["safety_latched"] and io.regs[POW] == 0



async def test_refused_current_write_is_refreshed_with_power_too():
    c, io = await make(saved(power=70, current=100))
    io.refuse.add(CUR)
    io.regs[CUR] = 320
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= 2, rounds=20000)
    await c.async_close()
    assert io.power_writes()[:2] == [70, 70]


async def test_unavailable_current_readback_degrades_then_latches():
    """N3 repro: 0x3001 writes ignored and its read-back unavailable,
    0x3002 fine. Unobservable is unconfirmed, not fine."""
    class NoCurReadIO(FakeIO):
        block_cur = False

        async def read(self, address, count):
            if self.block_cur and address == CUR:
                raise TransportError("current read failed")
            return await super().read(address, count)

    clock = FakeClock()
    io = NoCurReadIO(clock)
    c, io = await make(saved(power=70, current=100), io=io)
    io.ignore.add(CUR)
    io.regs[CUR] = 320
    io.block_cur = True
    await c.async_start()
    await spin(lambda: len(io.power_writes()) >= 1, rounds=20000)
    await spin(lambda: "current_cap_mismatch" in c.diagnostics["degraded_reasons"])
    await spin(lambda: c.phase == "faulted", rounds=60000)
    await c.async_close()
    assert c.export_state()["safety_latched"] and io.regs[POW] == 0
