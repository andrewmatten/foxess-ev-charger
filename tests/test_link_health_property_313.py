"""Randomised properties of the fault-latch release logic (3.1.3).

For each fixed seed a random sequence of events (healthy/lost/faulted/drawing
frames, enables, failed Stops, link drops, long pauses, restarts through
export_state) runs on the fake clock; after every step the safety invariants
are checked, and at the end a long healthy stretch checks liveness. Failure
messages carry the seed and step so a case can be replayed with ``run_seed``.
"""
from __future__ import annotations

import logging
import random

import pytest

from custom_components.foxess_charger.controller import (
    COMMS_LATCH_RELEASE_S, FAULT_RELEASE_S, LATCH_RANK, RELEASE_HOLD_MAX_S,
    ChargingController, ControlError,
)

from test_comms_latch_release import FAULT_LO, drop_link_during_enable, refresh
from test_controller import FakeClock, FakeIO, make, saved, snapshot
from test_fault_and_stop_fallback import drain

SEEDS = range(200)
STEPS = 40
BASE = {"comms": COMMS_LATCH_RELEASE_S, "fault": FAULT_RELEASE_S, "stop_failed": FAULT_RELEASE_S}
KINDS = ["healthy"] * 8 + ["poll"] * 3 + ["lost"] * 3 + ["soft"] * 3 + ["drawing"] * 2 + [
    "hard", "pause", "pause", "enable", "enable", "enable", "drop_link", "drop_link", "stop_fail", "stop_fail", "restart", "restart"]
STATS: dict = {}


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


async def new_controller(io, clock, state):
    wall = lambda: 1e9 + clock.now  # noqa: E731 - same epoch on both sides of a restart
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot,
                           wall_clock=wall)
    await c.async_initialize(state, configure_safety=False)
    io.writes.clear()
    return c


async def observe(c, io, how, dt):
    """One observation (refresh or poll) after dt seconds; transport errors are part of the test."""
    io.clock.now += dt
    try:
        await (c.async_poll() if how == "poll" else c._op_refresh())
    except Exception:  # noqa: BLE001
        pass
    await drain(c)


async def run_seed(seed: int) -> dict:
    rng = random.Random(seed)
    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    c = await new_controller(io, clock, saved(enabled=False))
    st = dict(causes=set(), releases=0, restarts=0, final_latched=None)
    since = None            # lower bound for when continuous health began (see step 4)
    prev_cause = None

    def where(i):
        return f"seed={seed} step={i}"

    async def step(kind):
        """Run one step; returns (observing, healthy)."""
        if kind in ("healthy", "poll"):
            await observe(c, io, "poll" if kind == "poll" else "refresh", rng.choice([1, 5, 10, 30, 60, 120]))
            return True, True
        if kind == "lost":
            io.fail_reads = True
            await observe(c, io, rng.choice(["poll", "refresh"]), rng.choice([1, 10, 30]))
            io.fail_reads = False
            return True, False
        if kind in ("soft", "hard"):
            io.regs[FAULT_LO] = 1 << (1 if kind == "soft" else 6)
            await observe(c, io, rng.choice(["poll", "refresh"]), rng.choice([1, 10]))
            io.regs[FAULT_LO] = 0
            return True, False
        if kind == "drawing":
            io.zero_pauses, io.stopped = False, False   # the car draws despite a zero cap
            await observe(c, io, rng.choice(["poll", "refresh"]), rng.choice([1, 10, 30]))
            io.zero_pauses = True
            await drain(c)
            return True, False
        if kind == "pause":
            io.clock.now += rng.choice([30, 300, 900, 3000, 7000, 9000])
            return False, True
        if c.latch_cause is not None:        # the rest need an unlatched controller
            return False, True
        try:
            if kind == "enable":
                await c.async_enable()
            elif kind == "drop_link":
                await drop_link_during_enable(c, io)
            elif kind == "stop_fail" and c.phase == "enabled":
                io.zero_pauses = io.stop_works = False
                try:
                    await c.async_pause()
                finally:
                    io.zero_pauses = io.stop_works = True
        except ControlError:
            pass
        await drain(c)
        return False, True

    for i in range(STEPS):
        kind = rng.choice(KINDS)
        was, was_cause = c.latch_cause is not None, c.latch_cause
        t0 = clock.now
        if kind == "restart":
            await drain(c)
            state = c.export_state()
            c = await new_controller(io, clock, state)
            st["restarts"] += 1
            now = c.latch_cause
            assert now == was_cause, f"{where(i)}: restart changed latch {was_cause} -> {now}"  # 7
            if now is not None:
                assert now != "hard" or was_cause == "hard", where(i)
                assert c._release_hold(now) <= RELEASE_HOLD_MAX_S, f"{where(i)}: hold too long"
            since = clock.now if now is not None else None   # initialisation reads the charger too
            continue
        observing, healthy = await step(kind)
        now_cause, t = c.latch_cause, clock.now
        latched = now_cause is not None

        # 1: zero on the wire, never enabled
        if latched:
            assert (c._wire()[0] or 0) <= 0, f"{where(i)}: positive wire target while latched"
            assert c.phase != "enabled", f"{where(i)}: enabled while latched"
        # 2: needs_human exactly for hard
        assert c.needs_human == (now_cause == "hard"), f"{where(i)}: needs_human wrong ({now_cause})"
        assert c.diagnostics["needs_human"] == c.needs_human, where(i)
        if was and latched:                                     # 3
            assert LATCH_RANK[now_cause] >= LATCH_RANK[was_cause], \
                f"{where(i)}: cause went {was_cause} -> {now_cause}"
        if was and not latched:                                 # release
            st["releases"] += 1
            assert was_cause != "hard", f"{where(i)}: hard latch released"
            assert healthy and observing, f"{where(i)}: released on an unhealthy/unobserved step ({kind})"
            assert since is not None and t - since >= BASE[was_cause], \
                f"{where(i)}: {was_cause} released after {None if since is None else t - since} s"
        # 6
        rel = c.diagnostics["latch_release_in"]
        if not latched or now_cause == "hard":
            assert rel is None, f"{where(i)}: latch_release_in={rel} with cause {now_cause}"
        elif rel is not None:
            assert 0 <= rel <= RELEASE_HOLD_MAX_S, f"{where(i)}: latch_release_in={rel}"
        if latched and now_cause != prev_cause:
            st["causes"].add(now_cause)
            st.setdefault("latches", {}).setdefault(now_cause, 0)
            st["latches"][now_cause] += 1

        # Lower bound of when continuous health began, for check 4. An unhealthy step
        # (e.g. the zeroing correction after a drawing frame) may itself end with a
        # healthy observation, so the bound restarts at the step's start (t0), not its end.
        if not latched or not was or now_cause != was_cause:
            since = None
        elif observing and kind != "lost" and (not healthy or since is None):
            since = t0
        prev_cause = now_cause

    # 5: a long healthy stretch
    cause = c.latch_cause
    io.fail_reads, io.zero_pauses, io.stop_works, io.regs[FAULT_LO] = False, True, True, 0
    margin = RELEASE_HOLD_MAX_S + c._refresh_interval() + 90
    start = clock.now
    while clock.now - start < margin and (c.latch_cause not in (None, "hard")):
        await refresh(c, io, 45)
    st["final_latched"] = cause
    if cause == "hard":
        assert c.latch_cause == "hard" and c.needs_human, where("end")
    elif cause is not None:
        assert c.latch_cause is None and c.phase == "paused", \
            f"{where('end')}: still {c.phase}/{c.latch_cause} after {clock.now - start} s healthy"
    return st


async def run_all():
    if not STATS:
        for seed in SEEDS:
            STATS[seed] = await run_seed(seed)
    return STATS


async def test_latch_properties_hold_for_random_histories():
    await run_all()


async def test_generator_exercises_every_cause_and_release():
    res = await run_all()
    latches = {k: sum(r.get("latches", {}).get(k, 0) for r in res.values()) for k in LATCH_RANK}
    releases = sum(r["releases"] for r in res.values())
    restarts = sum(r["restarts"] for r in res.values())
    finals = sum(r["final_latched"] in ("comms", "fault", "stop_failed") for r in res.values())
    assert all(n > 0 for n in latches.values()), latches
    assert releases > 0 and restarts > 0 and finals > 0
