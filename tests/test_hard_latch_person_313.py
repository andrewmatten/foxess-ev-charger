"""A hard latch needs a person (3.1.3, owner's decision).

An automation's switch.turn_on (service context without a user_id) must not
clear a hard latch; a person's (context with a user_id) may. Soft latches
(comms, fault, stop_failed) still clear on any explicit enable. A hard latch
that appeared after an enable was called is never cleared by that enable.
"""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError

from custom_components.foxess_charger.controller import ControlError

from fake_controller import FakeController
from ha_harness import Harness, make_entry
from test_comms_latch_release import FAULT_LO, cause, drop_link_during_enable
from test_controller import POW, make, saved
from test_fault_and_stop_fallback import drain, positive_writes

E = "foxess_charger"


async def hard_latched(**kw):
    """Leakage current shows for one frame, then clears: hard latch, charger healthy."""
    c, io = await make(**kw)
    io.regs[FAULT_LO] = 1 << 6
    await c.async_poll()
    await drain(c)
    io.regs[FAULT_LO] = 0
    assert cause(c) == "hard" and c.needs_human
    io.writes.clear()
    return c, io


async def test_automation_enable_refused_under_hard_latch():
    saves = []

    async def persist(state):
        saves.append(dict(state))

    c, io = await hard_latched(persist=persist)
    before, n_saves = c.export_state(), len(saves)
    with pytest.raises(ControlError, match="yourself"):
        await c.async_enable()
    await drain(c)
    assert io.writes == [] and len(saves) == n_saves
    assert c.export_state() == before and cause(c) == "hard" and c.phase == "faulted"


async def test_person_enable_clears_hard_latch_and_charging_resumes():
    c, io = await hard_latched()
    assert (await c.async_enable(clear_hard=True)).outcome == "confirmed"
    assert c.phase == "enabled" and cause(c) is None and io.regs[POW] == 70


async def test_person_enable_with_fault_present_relatches():
    c, io = await hard_latched()
    io.regs[FAULT_LO] = 1 << 6                     # still there
    with pytest.raises(ControlError):
        await c.async_enable(clear_hard=True)
    await drain(c)
    assert cause(c) == "hard" and positive_writes(io) == []


@pytest.mark.parametrize("latch", ["comms", "fault", "stop_failed"])
async def test_automation_enable_still_clears_soft_latches(latch):
    c, io = await make(saved(enabled=False) if latch == "comms" else None)
    if latch == "comms":
        await drop_link_during_enable(c, io)
    elif latch == "fault":
        io.regs[FAULT_LO] = 1 << 1                 # overvoltage, one frame
        await c.async_poll()
        await drain(c)
        io.regs[FAULT_LO] = 0
    else:
        io.zero_pauses, io.stop_works = False, False
        with pytest.raises(ControlError):
            await c.async_pause()
        await drain(c)
        io.zero_pauses, io.stop_works = True, True
    assert cause(c) == latch
    assert (await c.async_enable()).outcome == "confirmed"
    assert c.phase == "enabled" and io.regs[POW] == 70


@pytest.mark.parametrize("clear_hard", [False, True])
async def test_enable_in_flight_never_clears_a_newer_hard_latch(clear_hard):
    """The enable waits for the lock (to read the device maximum) while a poll
    sees a one-frame leakage fault and latches hard: the enable must not
    clear a latch that did not exist when it was called."""
    c, io = await make(saved(enabled=False, power=None))
    real_read, started = io.read, []
    kw = {"clear_hard": True} if clear_hard else {}   # an automation passes nothing

    async def read(address, count):
        if address == 0x1000 and not started:      # the poll's status block
            started.append(asyncio.ensure_future(c.async_enable(**kw)))
            for _ in range(5):
                await asyncio.sleep(0)             # enable now waits for the lock
            io.regs[FAULT_LO] = 1 << 6
            regs = await real_read(address, count)
            io.regs[FAULT_LO] = 0                  # one frame only
            return regs
        return await real_read(address, count)

    io.read = read
    await c.async_poll()
    with pytest.raises(ControlError):
        await started[0]
    await drain(c)
    assert cause(c) == "hard" and c.phase == "faulted" and positive_writes(io) == []


async def test_automation_enable_racing_the_latch_zero_write_is_refused():
    """Review probe H2c inverted: an automation enable that lands while the
    new hard latch is still writing its zero is refused."""
    c, io = await make(saved(enabled=False))
    io.regs[FAULT_LO] = 1 << 6                     # seen by this poll only
    started = []

    async def hook(value):
        if value == 0 and not started:
            started.append(asyncio.ensure_future(c.async_enable()))
            io.regs[FAULT_LO] = 0
            await asyncio.sleep(0)

    io.hooks[POW] = hook
    await c.async_poll()
    await drain(c)
    with pytest.raises(ControlError):
        await started[0]
    await drain(c)
    assert cause(c) == "hard" and c.phase == "faulted" and positive_writes(io) == []


# ── through the switch, with a real service-call context ─────────────────

class Fake(FakeController):
    async def async_enable(self, *, clear_hard=False):
        self.clear_hard = clear_hard
        if self.needs_human and not clear_hard:
            raise ControlError("refused (fake)")
        return await super().async_enable()


@pytest.mark.parametrize("person, published, expected", [
    (False, True, False),                           # automation: Context() has no user
    (True, True, True),                             # person who saw the latch
    (True, False, False),                           # person, latch not yet shown
])
async def test_switch_passes_clear_hard_only_for_a_person(
    hass, enable_custom_integrations, hass_admin_user, person, published, expected,
):
    context = Context(user_id=hass_admin_user.id) if person else Context()
    fake = Fake(status=4)
    harness = Harness(hass, make_entry("hardlatchentry01"), fake)
    assert await harness.async_setup()
    try:
        fake.needs_human = published
        await hass.data[E][harness.entry.entry_id]["coordinator"].async_refresh()
        fake.needs_human = True
        call = hass.services.async_call(
            "switch", "turn_on", {"entity_id": f"switch.{E}_charging"},
            blocking=True, context=context,
        )
        if expected:
            await call
        else:
            with pytest.raises(HomeAssistantError):
                await call
        assert fake.clear_hard is expected
    finally:
        await harness.async_unload()
