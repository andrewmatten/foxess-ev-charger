"""Controlled work mode (0x3000 = 0) alongside Plug&Charge.

Contract:
- Only an explicit enable may open a session. In Controlled mode it may send
  0x4001 Start, after the limits are confirmed, when the limit writes alone
  did not open one. A refused Start is never answered with a Stop.
- With the switch off nothing that could open a session is written: no
  0x3001/0x3002 positive value and no Start, whatever is staged meanwhile.
- A session that appears while the switch is off is brought back to zero,
  including after an unplug/replug (nothing is adopted in Controlled mode).
- Plug&Charge is unchanged: Start is never sent there.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
)
from custom_components.foxess_charger.controller import START_RETRY_S, ControlError

from rebuild_simulator import CHARGING, FINISHED, SimCharger
from test_rebuild_contract import Rig, rigs  # noqa: F401 - fixture

P, C, CTRL = REG_MAX_CHARGING_POWER, REG_MAX_CHARGING_CURRENT, REG_CHARGING_CONTROL
FULL, CAP = 70, 14
CONTROLLED, PLUG_AND_CHARGE = 0, 1


def starts(sim: SimCharger, mark: int = 0) -> int:
    return sum(1 for a, v in sim.wire_writes(mark) if (a, v) == (CTRL, 1))


def stops(sim: SimCharger, mark: int = 0) -> int:
    return sum(1 for a, v in sim.wire_writes(mark) if (a, v) == (CTRL, 2))


def positive_limits(sim: SimCharger, mark: int = 0) -> list[tuple[int, int]]:
    return [(a, v) for a, v in sim.wire_writes(mark) if a in (P, C) and v > 0]


# ── enable ────────────────────────────────────────────────────────────────

async def test_controlled_enable_sends_start_when_limits_alone_do_not(rigs):
    """Firmware that needs the Start command: the case that failed with
    'no active charging session after enable'."""
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert starts(rig.sim) == 1 and stops(rig.sim) == 0
    assert rig.sim.state == CHARGING and rig.sim.power_limit_raw() == CAP
    assert ctl.intent_enabled
    await rig.clock.advance(600)                 # the cap is kept alive as usual
    assert rig.sim.state == CHARGING and rig.sim.power_limit_raw() == CAP
    assert starts(rig.sim) == 1


async def test_controlled_enable_needs_no_start_when_limit_write_opens_session(rigs):
    """Firmware where the limit write itself opens the session."""
    rig = rigs(work_mode=CONTROLLED, implicit_resume=True)
    await rig.boot()
    await rig.enable_at(CAP)
    assert starts(rig.sim) == 0 and stops(rig.sim) == 0
    assert rig.sim.state == CHARGING and rig.sim.power_limit_raw() == CAP


async def test_controlled_refused_start_is_retried_never_compensated_with_stop(rigs):
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    rig.sim.inject_write(CTRL, "refuse", value=1, count=None)
    await rig.run(ctl.async_set_power(CAP))
    with pytest.raises(ControlError, match="no active charging session") as err:
        await rig.run(ctl.async_enable())
    # The message carries what the charger reported and what was tried.
    msg = str(err.value)
    assert "work mode 0 (controlled)" in msg and "status 1 (connected)" in msg
    assert "pilot" in msg and "stop reason" in msg
    assert f"Start sent {starts(rig.sim)}x, {starts(rig.sim)} refused (Modbus exception 3)" in msg
    assert "limits written 1.4 kW, 32.0 A (charger maximum, none saved)" in msg
    assert starts(rig.sim) >= 2                  # retried every START_RETRY_S
    assert stops(rig.sim) == 0
    assert not ctl.intent_enabled and rig.sim.power_limit_raw() == 0
    mark = rig.sim.mark()
    await rig.clock.advance(600)
    assert starts(rig.sim, mark) == 0 and not positive_limits(rig.sim, mark)
    assert START_RETRY_S > 0


async def test_controlled_resume_after_pause_uses_start_again(rigs):
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    assert rig.sim.measured_power_raw() == 0 and not ctl.intent_enabled
    # Firmware that ends the session on a zero limit (status 5) and needs a
    # fresh Start for the next one. (The simulator's own zero-paused state
    # with implicit_resume off can be resumed by nothing at all.)
    rig.sim.state = FINISHED
    result = await rig.run(ctl.async_enable())
    assert result.outcome == "confirmed"
    assert rig.sim.state == CHARGING and starts(rig.sim) == 2 and stops(rig.sim) == 0


async def test_controlled_enable_staged_while_unplugged_starts_on_plug(rigs):
    rig = rigs(work_mode=CONTROLLED, implicit_resume=False)
    ctl = await rig.boot()
    rig.sim.unplug()
    await rig.run(ctl.async_set_power(CAP))
    assert (await rig.run(ctl.async_enable())).outcome == "staged"
    assert starts(rig.sim) == 0
    rig.sim.plug()
    await rig.clock.advance(120)
    assert rig.sim.state == CHARGING and rig.sim.power_limit_raw() == CAP
    assert starts(rig.sim) == 1


# ── switch off: nothing that could open a session goes on the wire ───────

async def test_controlled_switch_off_never_writes_current_power_or_start(rigs):
    rig = rigs(work_mode=CONTROLLED, implicit_resume=True)
    ctl = await rig.boot()
    assert not ctl.intent_enabled
    assert (await rig.run(ctl.async_set_current(240))).outcome == "staged"
    assert (await rig.run(ctl.async_set_power(FULL))).outcome == "staged"
    for _ in range(30):
        await rig.run(ctl.async_poll())
        await rig.clock.advance(10)
    assert not positive_limits(rig.sim) and starts(rig.sim) == 0
    assert C not in [a for a, _ in rig.sim.wire_writes()]
    assert rig.sim.state != CHARGING and rig.sim.measured_power_raw() == 0


async def test_controlled_session_appearing_while_off_is_brought_back_to_zero(rigs):
    rig = rigs(work_mode=CONTROLLED)
    ctl = await rig.boot()
    await rig.run(ctl.async_poll())
    rig.sim.external_start()                     # something else opened a session
    await rig.run(ctl.async_poll())
    await rig.clock.advance(120)
    assert not ctl.intent_enabled
    assert rig.sim.measured_power_raw() == 0 and rig.sim.state != CHARGING


async def test_controlled_replug_adopts_nothing(rigs):
    rig = rigs(work_mode=CONTROLLED)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    await rig.run(ctl.async_poll())              # work mode known to the controller
    rig.sim.unplug()
    await rig.clock.advance(30)
    await rig.run(ctl.async_poll())
    rig.sim.plug()
    await rig.run(ctl.async_poll())
    rig.sim.external_start()
    await rig.run(ctl.async_poll())
    await rig.clock.advance(300)
    assert not ctl.intent_enabled
    assert rig.sim.measured_power_raw() == 0 and rig.sim.state != CHARGING


# ── Plug&Charge is unchanged ──────────────────────────────────────────────

async def test_plug_and_charge_never_sends_start(rigs):
    rig = rigs(work_mode=PLUG_AND_CHARGE, implicit_resume=False)
    ctl = await rig.boot()
    await rig.run(ctl.async_set_power(CAP))
    with pytest.raises(ControlError, match="no active charging session") as err:
        await rig.run(ctl.async_enable())
    assert starts(rig.sim) == 0
    assert "work mode 1 (plug_and_charge)" in str(err.value)
    assert "Start not sent" in str(err.value)


async def test_plug_and_charge_replug_still_adopts_new_session(rigs):
    rig = rigs(work_mode=PLUG_AND_CHARGE)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    assert (await rig.run(ctl.async_pause())).outcome == "confirmed"
    await rig.run(ctl.async_poll())
    rig.sim.unplug()
    await rig.clock.advance(30)
    await rig.run(ctl.async_poll())
    rig.sim.plug()                               # Plug&Charge starts by itself
    await rig.clock.advance(300)
    assert rig.sim.state == CHARGING and ctl.intent_enabled


# ── both limits are written on enable (protocol V1.06, 2.37/2.38) ─────────

@pytest.mark.parametrize("mode", [CONTROLLED, PLUG_AND_CHARGE])
async def test_enable_without_saved_current_writes_charger_maximum_once(rigs, mode):
    rig = rigs(work_mode=mode)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    current_writes = [v for a, v in rig.sim.wire_writes() if a == C]
    assert current_writes == [rig.sim.max_current_raw]
    assert ctl.desired_current_raw is None       # not saved as intent
    mark = rig.sim.mark()
    await rig.clock.advance(600)                 # and never refreshed
    assert C not in [a for a, _ in rig.sim.wire_writes(mark)]
    assert rig.sim.state == CHARGING and rig.sim.power_limit_raw() == CAP


async def test_enable_with_saved_current_writes_that_current(rigs):
    rig = rigs(work_mode=CONTROLLED)
    ctl = await rig.boot()
    await rig.run(ctl.async_set_current(160))
    await rig.enable_at(CAP)
    assert [v for a, v in rig.sim.wire_writes() if a == C][0] == 160
    assert rig.sim.max_current_raw not in [v for a, v in rig.sim.wire_writes() if a == C]


async def test_enable_survives_charger_refusing_the_default_current_write(rigs):
    rig = rigs(work_mode=PLUG_AND_CHARGE)
    ctl = await rig.boot()
    rig.sim.inject_write(C, "refuse", count=None)
    await rig.enable_at(CAP)                     # best effort: not a failure
    assert rig.sim.state == CHARGING and ctl.intent_enabled


@pytest.mark.parametrize("mode", [CONTROLLED, PLUG_AND_CHARGE])
async def test_power_change_while_charging_does_not_rewrite_current(rigs, mode):
    rig = rigs(work_mode=mode)
    ctl = await rig.boot()
    await rig.enable_at(CAP)
    mark = rig.sim.mark()
    await rig.run(ctl.async_set_power(CAP + 6))     # CAP is the minimum; go up
    await rig.clock.advance(60)
    assert C not in [a for a, _ in rig.sim.wire_writes(mark)]
    assert [v for a, v in rig.sim.wire_writes(mark) if a == P and v > 0]   # the new cap did go out
