"""Long-run lossy-link simulation (seeded, deterministic, fake clock).

A controller runs for simulated hours over a lossy link while HA polls every
10 s and the refresh runs on its real schedule. Checked every simulated second:

- safety/truthfulness: never phase "enabled" with a positive cap register
  while more than 60 s have passed since a positive cap write last reached
  the charger (its firmware then falls back to its own maximum);
- liveness on a mostly-up link: not latched at the end, little latched time,
  mostly enabled.

Run as a script for the per-mode numbers:
``PYTHONPATH=.:tests .venv/bin/python tests/test_lossy_link_sim_313.py``
"""
from __future__ import annotations

import asyncio
import logging
import random

import pytest

from custom_components.foxess_charger.transport import TransportError, UnknownOutcome
from test_controller import POW, make

EXPIRY = 60.0
HOUR = 3600
SEEDS = range(5)


async def run(mode: str, p: float, seed: int, seconds: int = HOUR) -> dict:
    rng = random.Random(seed)
    c, io = await make(retry_interval=3.0)
    real_read, real_write = io.read, io.write
    last_cap, bad_until, refresh_read = [io.clock.now], [0.0], [False]

    def lossy() -> bool:
        if mode == "iid":
            return rng.random() < p
        if mode == "burst":            # link dead for 1-6 s, on average every 1/p s
            return io.clock.now < bad_until[0]
        if refresh_read[0]:            # "refresh_first": only the refresh's status read
            refresh_read[0] = False
            return True
        return False

    async def read(address, count):
        if lossy():
            raise TransportError("lost")
        return await real_read(address, count)

    async def write(address, value):
        if lossy():
            if rng.random() < 0.5:
                raise TransportError("lost before")
            await real_write(address, value)
            if address == POW and value > 0:
                last_cap[0] = io.clock.now
            raise UnknownOutcome("reply lost")
        await real_write(address, value)
        if address == POW and value > 0:
            last_cap[0] = io.clock.now

    io.read, io.write = read, write
    s = dict(exposed=0, lapsed=0, max_gap=0.0, latched=0, enabled=0)
    due, start = io.clock.now, io.clock.now
    while io.clock.now < start + seconds:
        now = io.clock.now
        if mode == "burst" and now >= bad_until[0] and rng.random() < p:
            bad_until[0] = now + rng.uniform(1, 6)
        if int(now - start) % 10 == 0:
            try:
                await c.async_poll()
            except Exception:  # noqa: BLE001 - a failed poll is part of the link
                pass
        if now >= due:
            refresh_read[0] = mode == "refresh_first"
            try:
                ok = await c._op_refresh()
            except Exception:  # noqa: BLE001
                ok = False
            refresh_read[0] = False
            due = max(io.clock.now, now) + (c._refresh_interval() if ok else c._retry)
        for _ in range(5):
            await asyncio.sleep(0)
        phase = c.phase
        s["lapsed"] += io.regs[POW] > 0 and io.clock.now - last_cap[0] > EXPIRY  # any phase
        if phase == "enabled" and io.regs[POW] > 0:
            s["enabled"] += 1
            gap = io.clock.now - last_cap[0]
            s["max_gap"] = max(s["max_gap"], gap)
            s["exposed"] += gap > EXPIRY
        elif phase == "faulted":
            s["latched"] += 1
        io.clock.now = max(io.clock.now, now + 1)
    s["end"] = c.phase
    await c.async_close()
    return s


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# Safety must hold at every loss rate where a cap or a zero can still get through.
@pytest.mark.parametrize("mode, p", [
    ("iid", 0.05), ("iid", 0.2), ("iid", 0.4),
    ("burst", 0.02), ("burst", 0.05), ("burst", 0.1),
    ("refresh_first", 1.0),
])
async def test_never_enabled_with_a_lapsed_cap(mode, p):
    for seed in SEEDS:
        s = await run(mode, p, seed)
        assert s["exposed"] == 0, (mode, p, seed, s)


# Liveness bounds, from measured numbers (10 seeds, run the module as a
# script): enabled >= 3580 s/h at iid 5 %/20 % and bursts 2 %/5 %, never
# latched. A latch would also end with charging paused until an explicit
# enable, so the enabled share catches it too. The latched-seconds bound
# depends on the current comms-latch release timing (COMMS_LATCH_RELEASE_S,
# doubled per release): retune it if that changes.
@pytest.mark.parametrize("mode, p", [("iid", 0.05), ("iid", 0.2), ("burst", 0.02), ("burst", 0.05)])
async def test_mostly_up_link_stays_enabled(mode, p):
    for seed in SEEDS:
        s = await run(mode, p, seed)
        assert s["end"] == "enabled", (mode, p, seed, s)
        assert s["latched"] <= 60, (mode, p, seed, s)
        assert s["enabled"] >= 0.97 * HOUR, (mode, p, seed, s)


async def test_refresh_status_always_lost_never_latches():
    """By design no cap can be refreshed here (every refresh frame is invalid,
    so positive writes are never authorized): the controller cycles through
    the protective zero instead of running on a lapsed cap, and never latches."""
    for seed in SEEDS:
        s = await run("refresh_first", 1.0, seed)
        assert s["latched"] == 0 and s["end"] != "faulted", (seed, s)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)

    async def main():
        for mode, ps in (("iid", (0.05, 0.2, 0.4, 0.6, 0.8, 0.95)), ("burst", (0.02, 0.05, 0.1)),
                         ("refresh_first", (1.0,))):
            for p in ps:
                runs = [await run(mode, p, seed) for seed in range(10)]
                print(f"{mode:13s} p={p:<4} enabled min {min(r['enabled'] for r in runs):5d}s"
                      f" latched max {max(r['latched'] for r in runs):5d}s"
                      f" exposed max {max(r['exposed'] for r in runs):5d}s"
                      f" lapsed max {max(r['lapsed'] for r in runs):5d}s"
                      f" max gap {max(r['max_gap'] for r in runs):4.0f}s"
                      f" ends {sorted({r['end'] for r in runs})}")

    asyncio.run(main())
