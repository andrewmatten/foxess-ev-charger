"""Fault handling and the Stop fallback.

Covers the 0x4001 Stop fallback when a zero write cannot be confirmed, an
unreadable power-cap read-back reaching the mismatch latch, faults latching
despite unknown fields in the same frame, bounded saves that cannot hold off
the protective zero, fault latches surviving a failed save plus a cold
restart, unknown telemetry reported as unknown on the entities, an enabled
record without a power cap restoring paused, a failed platform setup closing
the controller, real control counters in diagnostics, and rejected energy
without a baseline reading unknown. Each test states the safe behaviour
required.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

import pytest

import custom_components.foxess_charger as integration
from custom_components.foxess_charger import binary_sensor as B
from custom_components.foxess_charger import sensor as N
from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.controller import ChargingController, ControlError
from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.energy import EnergyTracker
from custom_components.foxess_charger.persistence import ChargerStorage
from custom_components.foxess_charger.transport import TransportError

from ha_harness import make_entry
from rebuild_simulator import CHARGING, SimCharger
from test_controller import CC, CTRL, CUR, POW, POWER, STATUS, FakeClock, FakeIO, make, saved

FAULT_HI, FAULT_LO = 0x101A, 0x101B


async def drain(c) -> None:
    """Let spawned controller work (corrections, refreshes) finish."""
    for _ in range(50):
        pending = [t for t in c._tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def positive_writes(io):
    return [(a, v) for _, a, v in io.writes if a in (CUR, POW) and v > 0]


def unreadable_power_cap(io) -> None:
    """Single-register reads of 0x3002 fail; the status block still reads."""
    real = io.read

    async def read(address, count):
        if address == POW:
            raise TransportError("0x3002 read failed")
        return await real(address, count)

    io.read = read


# ── Stop is tried when the zero write itself cannot be confirmed ─────

async def test_pause_uses_stop_when_zero_write_is_ignored():
    c, io = await make()                       # charging at 7.0 kW
    io.ignore.add(POW)                         # charger ignores 0x3002 writes
    with pytest.raises(ControlError):          # the zero was never confirmed
        await c.async_pause()
    await drain(c)
    assert (CTRL, 2) in [(a, v) for _, a, v in io.writes]
    assert io.regs[POWER] == 0 and io.regs[STATUS] == 5
    assert not c.intent_enabled


async def test_no_stop_when_unconfirmed_zero_left_nothing_drawing():
    c, io = await make()
    io.ignore.add(POW)
    io.car_draw = False                        # car already stopped drawing
    with pytest.raises(ControlError):
        await c.async_pause()
    assert (CTRL, 2) not in [(a, v) for _, a, v in io.writes]


# ── an unreadable power-cap read-back reaches the mismatch latch ─────

async def test_unreadable_power_cap_readback_latches_and_stops():
    c, io = await make(saved(power=14))
    assert io.regs[POW] == 14
    io.regs[POW] = 73                          # charger reverted to full power
    io.ignore.add(POW)                         # and ignores further cap writes
    unreadable_power_cap(io)
    for _ in range(5):
        io.clock.now += 30
        await c._op_refresh()
    await drain(c)
    assert c.diagnostics["safety_latched"] and c.phase in ("faulted", "stopped")
    assert io.regs[POWER] == 0                 # stopped via the 0x4001 Stop fallback


# ── a known fault is not masked by an unknown field in its frame ─────

async def test_fault_latches_even_when_connector_field_is_unknown():
    c, io = await make()
    io.cc_override = 65535
    io.regs[FAULT_LO] = 1
    with pytest.raises(ControlError):
        await c.async_set_power(50)
    assert c.diagnostics["safety_latched"]
    io.cc_override, io.regs[FAULT_LO] = None, 0   # telemetry and fault recover
    io.clock.now += 5
    await c._op_refresh()
    assert c.diagnostics["safety_latched"]
    assert positive_writes(io) == [] and io.regs[POW] == 0


async def test_status_fault_latches_even_when_power_field_is_unknown():
    c, io = await make()
    io.status_override = 6
    io.regs[POWER] = 0xFFFF                    # implausible measured power
    real = io._physics
    io._physics = lambda: (real(), io.regs.__setitem__(POWER, 0xFFFF))
    await c._op_refresh()
    assert c.diagnostics["safety_latched"]


async def test_all_ones_fault_register_is_unknown_not_a_fault():
    """0xFFFF in both fault registers is the unknown sentinel (as 65535 is for
    status and connector): it goes through the telemetry grace instead."""
    c, io = await make()
    io.status_override = 65535
    io.regs[FAULT_HI] = io.regs[FAULT_LO] = 0xFFFF
    await c._op_refresh()
    assert not c.diagnostics["safety_latched"]


# ── a stalled save cannot hold off the protective zero ───────────────

async def test_fault_zero_reaches_wire_before_a_stalled_save_completes():
    gate, entered = asyncio.Event(), asyncio.Event()
    blocked = False

    async def persist(_state):
        if blocked:
            entered.set()
            await gate.wait()

    c, io = await make(persist=persist)
    blocked = True
    io.regs[FAULT_LO] = 1
    task = asyncio.create_task(c.async_poll())
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(20):
        await asyncio.sleep(0)
    zero_before_save = io.regs[POW] == 0 and io.regs[POWER] == 0
    gate.set()
    await task
    await drain(c)
    assert zero_before_save


async def test_hung_save_is_bounded_and_retried():
    hung = False

    async def persist(_state):
        if hung:
            await asyncio.Event().wait()       # never completes

    c, io = await make(persist=persist, persist_timeout=0.05)
    hung = True
    io.regs[FAULT_LO] = 1
    await asyncio.wait_for(c.async_poll(), 2)
    await asyncio.wait_for(drain(c), 2)
    assert c.diagnostics["safety_latched"] and c.diagnostics["persist_pending"]
    assert io.regs[POW] == 0
    io.clock.now += 30
    await asyncio.wait_for(c._op_refresh(), 2)   # heartbeat not held hostage
    hung = False
    io.clock.now += 30
    await c._op_refresh()
    assert not c.diagnostics["persist_pending"]


# ── an automatic fault latch survives a failed save + cold restart ───

async def test_fault_latch_survives_failed_state_save_and_cold_restart(hass, hass_storage):
    entry = "faultlatchentry"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(saved())          # enabled, 7.0 kW
    c, io = await make(saved(), persist=storage.async_save_controller)

    async def refuse(_data):
        raise OSError("disk state write refused")

    storage._state_store.async_save = refuse             # only the new file fails
    io.regs[FAULT_LO] = 1
    await c.async_poll()
    await drain(c)
    assert c.diagnostics["safety_latched"]

    # Cold restart: the in-memory attempted-revision guard is gone.
    hass.data.pop(f"{DOMAIN}_attempted_revision", None)
    restored = (await ChargerStorage(hass, entry).async_load()).controller
    assert not restored["enabled"] or restored["safety_latched"]

    # The car still reports a zero-paused session (status 4) after restart.
    clock = FakeClock()
    io2 = FakeIO(clock)
    io2.regs[POW] = 0
    c2, io2 = await make(restored, io=io2)
    assert not c2.intent_enabled
    assert positive_writes(io2) == [] and io2.regs[POW] == 0


# ── unknown fields are unknown, never a false edge ───────────────

def _binary(key, data):
    return next(d for d in B.BINARY_SENSORS if d.key == key).value_fn(data)


def test_unknown_connector_status_and_lock_are_unknown():
    assert _binary("vehicle_connected",
                   {"cc_status": None, "cc_status_valid": False, "cc_status_raw": 65535}) is None
    assert _binary("is_charging",
                   {"status": None, "status_valid": False, "status_raw": 65535}) is None
    assert _binary("is_locked", {"lock_status": 65535}) is None
    assert _binary("vehicle_connected", {}) is None


def test_known_values_unchanged():
    assert _binary("vehicle_connected", {"cc_status": 1, "cc_status_valid": True}) is True
    assert _binary("vehicle_connected", {"cc_status": 0, "cc_status_valid": True}) is False
    assert _binary("is_charging", {"status": 3, "status_valid": True}) is True
    assert _binary("is_charging", {"status": 4, "status_valid": True}) is False
    assert _binary("is_locked", {"lock_status": 0}) is True      # on = unlocked
    assert _binary("is_locked", {"lock_status": 1}) is False


def test_fault_entity_reports_status_6_and_unknown_fault_register():
    assert _binary("has_fault", {"status": 6, "status_valid": True, "fault_code": 0}) is True
    assert _binary("has_fault", {"status": 3, "status_valid": True, "fault_code": 4}) is True
    assert _binary("has_fault", {"status": 3, "status_valid": True, "fault_code": 0}) is False
    assert _binary("has_fault", {"status": 3, "status_valid": True}) is None
    assert _binary("has_alarm", {"alarm_code": 0}) is False
    assert _binary("has_alarm", {}) is None


# ── enabled record without a power cap restores paused ───────────────

async def test_enabled_restore_without_power_cap_restores_paused():
    c, io = await make(saved(power=None))
    assert not c.intent_enabled and c.phase == "paused"
    for _ in range(3):
        io.clock.now += 30
        await c._op_refresh()
    await drain(c)
    assert positive_writes(io) == []
    assert io.regs[POW] == 0                  # paused intent puts zero on the wire


# ── failed platform setup closes the started controller ──────────────

async def test_failed_platform_forward_closes_controller(hass, enable_custom_integrations):
    made = []

    class Tracked(ChargingController):
        closed = False

        async def async_close(self):
            self.closed = True
            await super().async_close()

    async def factory(hass_, entry, *, persist=None):
        c = Tracked(SimCharger(state=CHARGING), persist=persist,
                    confirmation_timeout=1.0, retry_interval=0.2)
        made.append(c)
        return c

    async def forward(*_args, **_kw):
        raise RuntimeError("platform setup failed")

    entry = make_entry("platformfailentry")
    entry.add_to_hass(hass)
    with patch.object(integration, "async_create_controller", factory), \
            patch.object(hass.config_entries, "async_forward_entry_setups", forward):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert made and all(c.closed for c in made)
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


# ── control diagnostics are real counters, never fabricated zero ─────

async def test_transport_errors_attributes_report_real_control_counters():
    c, io = await make()
    await c._op_refresh()
    unreadable_power_cap(io)
    await c._op_refresh()
    diag = c.diagnostics
    assert diag["refreshes"] > 0 and diag["refresh_failures"] > 0
    coordinator = object.__new__(FoxESSChargerCoordinator)
    coordinator.controller = c
    coordinator.energy = types.SimpleNamespace(rejections=[])
    data = {}
    coordinator._add_controller_view(data)
    attrs = next(d for d in N.SENSORS if d.key == "transport_errors").attrs_fn(data)
    assert attrs["refreshes"] == diag["refreshes"]
    assert attrs["refresh_failures"] == diag["refresh_failures"]
    assert attrs["stop_fallbacks"] == diag["stop_fallbacks"]
    # 2.4.3 counters this controller has no producer for: unknown, not 0.
    for legacy in ("setpoint_reasserts", "heartbeat_write_failures",
                   "stop_write_failures", "stop_retries", "stop_pending"):
        assert attrs[legacy] is None


# ── rejected energy with no baseline is unknown, not zero ────────────

def test_rejected_energy_without_baseline_is_unknown():
    tracker = EnergyTracker()
    data = {"status": 3, "cc_status": 1, "max_power_raw": 73,
            "total_energy_raw": 0xFFFFFFFF, "current_energy_raw": 0xFFFFFFFF}
    tracker.apply(data, session_boundary=False)
    values = []
    for key in ("total_energy", "current_session_energy"):
        sensor = object.__new__(N.FoxESSChargerSensor)
        sensor.coordinator = types.SimpleNamespace(data=data)
        sensor.entity_description = next(d for d in N.SENSORS if d.key == key)
        values.append(sensor.native_value)
    assert values == [None, None]
