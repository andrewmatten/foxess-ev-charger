"""Lossy-link simulation with firmware 1.8 behaviour and real frame latency.

As on the real link and firmware 1.8:

- a lost frame costs the 5 s Modbus transaction deadline in fake time;
- a zero cap ENDS the session (status 5): charging stays off until something
  turns it on again, so a protective zero on a merely lossy link is fatal.

Safety property (must hold): the phase is never ``enabled`` while more than
60 s have passed since a positive cap write was last acknowledged.
Known limit (xfail): the 45 s no-ack limit writes a protective zero, which
ends a firmware 1.8 session on a lossy link.

Run as a script for the per-loss numbers:
``PYTHONPATH=.:tests .venv/bin/python tests/test_lossy_link_fw18_313.py``
"""
from __future__ import annotations

import asyncio
import logging
import random

import pytest

from custom_components.foxess_charger.transport import TransportError, UnknownOutcome
from test_controller import POW, make

EXPIRY, HOUR, LOST_COST = 60.0, 3600, 5.0
SEEDS = range(20)


async def run(p: float, seed: int, seconds: int = HOUR) -> dict:
    rng = random.Random(seed)
    c, io = await make(retry_interval=3.0)
    real_apply = io._apply

    def apply(address, value):
        real_apply(address, value)
        if address == POW and value == 0:
            io.stopped = True          # 1.8: zero ends the session (status 5)
        io._physics()
    io._apply = apply
    real_read, real_write = io.read, io.write
    last_cap = [io.clock.now]

    def lost() -> bool:
        if rng.random() < p:
            io.clock.now += LOST_COST
            return True
        return False

    async def read(address, count):
        if lost():
            raise TransportError("lost")
        return await real_read(address, count)

    async def write(address, value):
        if lost():
            if rng.random() < 0.5:
                raise TransportError("lost before")
            await real_write(address, value)
            raise UnknownOutcome("reply lost")
        await real_write(address, value)
        if address == POW and value > 0:
            last_cap[0] = io.clock.now

    io.read, io.write = read, write
    s = dict(exposed=0)
    due = next_poll = start = io.clock.now
    while io.clock.now < start + seconds:
        now = io.clock.now
        if now >= next_poll:
            next_poll = now + 10
            try:
                await c.async_poll()
            except Exception:  # noqa: BLE001
                pass
        if io.clock.now >= due:
            t0 = io.clock.now
            try:
                ok = await c._op_refresh()
            except Exception:  # noqa: BLE001
                ok = False
            due = t0 + (c._refresh_interval() if ok else c._retry)
        for _ in range(5):
            await asyncio.sleep(0)
        gap = io.clock.now - last_cap[0]
        s["exposed"] += c.phase == "enabled" and gap > EXPIRY
        io.clock.now = max(io.clock.now, now + 1)
    s["charging"] = c.intent_enabled and c.phase == "enabled" and not io.stopped
    await c.async_close()
    return s


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.mark.parametrize("p", [0.05, 0.1, 0.2])
async def test_lossy_link_never_enabled_without_a_recent_cap_ack(p):
    for seed in SEEDS:
        s = await run(p, seed)
        assert s["exposed"] == 0, (seed, s)


@pytest.mark.xfail(strict=True, reason="known limit: the 45 s no-ack limit ends a firmware 1.8 session on a lossy link; see CHANGELOG 3.1.3")
@pytest.mark.parametrize("p", [0.05, 0.1])
async def test_lossy_link_keeps_charging_on_fw18(p):
    for seed in SEEDS:
        s = await run(p, seed)
        assert s["charging"], (seed, s)


async def _report() -> None:
    logging.disable(logging.CRITICAL)
    for p in (0.05, 0.1, 0.2):
        res = [await run(p, seed) for seed in SEEDS]
        print(f"p={p}: ended off {sum(not r['charging'] for r in res)}/{len(res)}, "
              f"exposed {sum(r['exposed'] for r in res)} s")


if __name__ == "__main__":
    asyncio.run(_report())
