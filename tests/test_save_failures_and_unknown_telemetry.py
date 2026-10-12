"""Failed saves and unknown telemetry.

Covers a fault stop that survives a failed first save plus a cold restart,
a positive change that could not be saved not being applied, the
per-session energy fallback, and invalid connector-lock telemetry showing as
unknown. Each test states the safe behaviour required.
"""
from __future__ import annotations

import types

import pytest

from custom_components.foxess_charger import switch as SW
from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.persistence import ChargerStorage
from custom_components.foxess_charger.session import SessionTracker

from test_controller import POW, POWER, STATUS, FakeClock, FakeIO, make, saved
from test_fault_and_stop_fallback import FAULT_LO, drain, positive_writes


# ── fault stop survives a failed first (legacy) save + cold restart ─

async def test_fault_stop_survives_first_projection_failure_and_cold_restart(
        hass, hass_storage):
    entry = "faultstopentry"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved())          # enabled, 7.0 kW
    c, io = await make(saved(), persist=storage.async_save_controller)

    async def refuse(_data):
        raise OSError("disk write refused")

    storage._session_store.async_save = refuse           # the first write fails
    io.regs[FAULT_LO] = 1
    await c.async_poll()
    await drain(c)
    assert c.diagnostics["safety_latched"] and io.regs[POW] == 0

    # Cold restart: the in-memory attempted-revision guard is gone and every
    # file still holds the older enabled record.
    hass.data.pop(f"{DOMAIN}_attempted_revision", None)
    restored = (await ChargerStorage(hass, entry).async_load()).controller
    assert restored["enabled"] and not restored["safety_latched"]   # the cut point

    # Fault has cleared; the car is connected and paused under a zero cap.
    io2 = FakeIO(FakeClock())
    io2.regs[POW] = 0
    io2._physics()
    assert io2.regs[STATUS] == 4 and io2.regs[POWER] == 0
    c2, io2 = await make(restored, io=io2)
    for _ in range(3):
        io2.clock.now += 30
        await c2._op_refresh()
    await drain(c2)
    assert not c2.intent_enabled
    assert positive_writes(io2) == [] and io2.regs[POW] == 0

    # A later explicit enable may resume.
    result = await c2.async_enable()
    assert result.outcome == "confirmed" and io2.regs[POW] == 70


async def test_restart_while_charging_keeps_enabled():
    """Control: a restored enable with a positive live cap is kept."""
    c, io = await make(saved())
    assert c.intent_enabled and io.regs[POW] == 70 and io.regs[STATUS] == 3


# ── a positive change that could not be saved is not applied ───────

def _failing_persist():
    state = {"fail": False}

    async def persist(_data):
        if state["fail"]:
            raise OSError("disk write refused")

    return state, persist


async def test_failed_enable_save_cannot_authorize_positive_output():
    state, persist = _failing_persist()
    c, io = await make(saved(enabled=False), persist=persist)
    assert io.regs[POW] == 0
    state["fail"] = True
    with pytest.raises(ControlError):
        await c.async_enable()
    await drain(c)
    for _ in range(3):
        io.clock.now += 30
        await c._op_refresh()
    await drain(c)
    assert not c.intent_enabled
    assert positive_writes(io) == [] and io.regs[POW] == 0


async def test_failed_increase_save_cannot_raise_live_power_cap():
    state, persist = _failing_persist()
    c, io = await make(saved(power=14), persist=persist)
    assert io.regs[POW] == 14
    state["fail"] = True
    with pytest.raises(ControlError):
        await c.async_set_power(70)
    for _ in range(3):
        io.clock.now += 30
        await c._op_refresh()
    await drain(c)
    assert c.intent_enabled and c.desired_power_raw == 14
    assert io.regs[POW] == 14 and 70 not in io.power_writes()


async def test_failed_save_still_allows_decrease_and_pause():
    """Control: protective changes are not held back by a failed save."""
    state, persist = _failing_persist()
    c, io = await make(saved(power=70), persist=persist)
    state["fail"] = True
    await c.async_set_power(14)
    assert io.regs[POW] == 14
    with pytest.raises(ControlError):          # applied, but not durable
        await c.async_pause()
    assert io.regs[POW] == 0 and not c.intent_enabled


# ── logical session energy fallback ────────────────────────────────

def _session(start: dict, end: dict) -> dict:
    now = [100.0]
    done: list[dict] = []
    t = SessionTracker(on_complete=done.append, clock=lambda: now[0],
                       wall_clock=lambda: 1_000_000 + now[0])
    t.update({"status": 3, "status_valid": True, "power_raw": 70, **start},
             intent_enabled=True)
    now[0] += 20 * 60
    t.update({"status": 0, "status_valid": True, "power_raw": 0, **end},
             intent_enabled=True)
    assert len(done) == 1
    return done[0]


def test_session_fallback_subtracts_a_known_start_counter():
    record = _session({"total_energy_raw": None, "current_energy_raw": 50},
                      {"total_energy_raw": None, "current_energy_raw": 60})
    assert record["energy_kwh"] == 1.0
    assert record["avg_power_kw"] == 3.0


def test_session_missing_energy_remains_unknown():
    record = _session({}, {})
    assert record["energy_kwh"] is None and record["avg_power_kw"] is None
    record = _session({"current_energy_raw": None}, {"current_energy_raw": 60})
    assert record["energy_kwh"] is None


def test_session_lifetime_delta_unchanged():
    record = _session({"total_energy_raw": 1000, "current_energy_raw": 50},
                      {"total_energy_raw": 1010, "current_energy_raw": 60})
    assert record["energy_kwh"] == 1.0


# ── invalid connector-lock telemetry is unknown on the switch ──────

def _lock_switch(value):
    sw = object.__new__(SW.FoxESSLockSwitch)
    sw.coordinator = types.SimpleNamespace(data={"lock_status": value})
    return sw.is_on


def test_invalid_connector_lock_control_is_unknown():
    assert _lock_switch(65535) is None
    assert _lock_switch(2) is None
    assert _lock_switch(None) is None
    assert _lock_switch(1) is True
    assert _lock_switch(0) is False
