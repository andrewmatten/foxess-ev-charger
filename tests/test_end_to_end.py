"""End to end: real HA config-entry setup, real entities, the real
ChargingController, and the simulated charger at register level.

Every other rebuild suite fakes one side of a boundary (entities over a fake
controller, controller over a fake register map). This one wires them all
together so a mismatch between layers shows up as wrong register writes.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState

import custom_components.foxess_charger as integration
from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL, REG_DEFAULT_CURRENT, REG_MAX_CHARGING_POWER, REG_TIME_VALIDITY,
)
from custom_components.foxess_charger.controller import ChargingController

from ha_harness import make_entry
from rebuild_simulator import CHARGING, CONNECTED, SimCharger

E = "foxess_charger"
SWITCH = f"switch.{E}_charging"
POWER = f"number.{E}_max_charging_power"


class Stack:
    """One HA instance with the integration running over a SimCharger. The
    simulator outlives unload/setup, like a real charger across HA restarts."""

    def __init__(self, hass, sim: SimCharger) -> None:
        self.hass = hass
        self.sim = sim
        self.entry = make_entry("endtoendentry01")
        self.controllers: list[ChargingController] = []

    async def _factory(self, hass, entry, *, persist=None):
        if self.sim.closed:  # a real restart opens a fresh connection
            self.sim.simulate_process_restart()
        ctl = ChargingController(
            self.sim, persist=persist, confirmation_timeout=3.0,
            pause_confirmation_timeout=3.0, retry_interval=0.2,
        )
        self.controllers.append(ctl)
        return ctl

    async def start(self) -> None:
        with patch.object(integration, "async_create_controller", self._factory):
            if self.entry.entry_id not in {
                e.entry_id for e in self.hass.config_entries.async_entries("foxess_charger")
            }:
                self.entry.add_to_hass(self.hass)
            assert await self.hass.config_entries.async_setup(self.entry.entry_id)
            await self.hass.async_block_till_done()
        assert self.entry.state is ConfigEntryState.LOADED

    async def stop(self) -> None:
        await self.hass.config_entries.async_unload(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def call(self, domain, service, entity, **data) -> None:
        await self.hass.services.async_call(
            domain, service, {"entity_id": entity, **data}, blocking=True,
        )
        await self.hass.async_block_till_done()

    def state(self, entity: str) -> str:
        return self.hass.states.get(entity).state


@pytest.fixture
async def stack(hass, enable_custom_integrations):
    s = Stack(hass, SimCharger(state=CONNECTED))
    yield s
    if s.entry.state is ConfigEntryState.LOADED:
        await s.stop()


async def test_full_user_flow_over_real_controller(stack):
    sim = stack.sim
    await stack.start()

    # Safety configuration landed on the device at startup.
    assert sim.holding[REG_TIME_VALIDITY] == 60
    assert sim.holding[REG_DEFAULT_CURRENT] == 60
    # First install with no session running: nothing authorises charging.
    assert stack.state(SWITCH) == "off"
    assert sim.positive_cap_writes() == []

    # Raising the limit while off is staged, never sent (stop-then-resume incident).
    mark = sim.mark()
    await stack.call("number", "set_value", POWER, value=7.0)
    assert sim.positive_cap_writes(mark) == []
    assert sim.measured_power_raw() == 0

    # Switch on: charging starts through the power setpoint alone.
    mark = sim.mark()
    await stack.call("switch", "turn_on", SWITCH)
    assert sim.state == CHARGING
    assert sim.holding[REG_MAX_CHARGING_POWER] == 70
    assert stack.state(SWITCH) == "on"

    # Throttle to the 1.4 kW minimum and back up.
    await stack.call("number", "set_value", POWER, value=1.4)
    assert sim.holding[REG_MAX_CHARGING_POWER] == 14
    await stack.call("number", "set_value", POWER, value=7.0)
    assert sim.holding[REG_MAX_CHARGING_POWER] == 70

    # Switch off: zero power pauses it; 0x4001 was never needed.
    await stack.call("switch", "turn_off", SWITCH)
    assert sim.holding[REG_MAX_CHARGING_POWER] == 0
    assert sim.measured_power_raw() == 0
    assert stack.state(SWITCH) == "off"
    assert [w for w in sim.writes_since(mark) if w.address == REG_CHARGING_CONTROL] == []


async def test_pause_survives_ha_restart(stack):
    sim = stack.sim
    await stack.start()
    await stack.call("number", "set_value", POWER, value=7.0)
    await stack.call("switch", "turn_on", SWITCH)
    await stack.call("switch", "turn_off", SWITCH)

    await stack.stop()
    mark = sim.mark()
    await stack.start()

    assert not stack.controllers[-1].intent_enabled
    assert sim.positive_cap_writes(mark) == []
    assert sim.measured_power_raw() == 0
    assert stack.state(SWITCH) == "off"


async def test_charging_resumes_after_ha_restart(stack):
    sim = stack.sim
    await stack.start()
    await stack.call("number", "set_value", POWER, value=3.2)
    await stack.call("switch", "turn_on", SWITCH)

    await stack.stop()
    await stack.start()

    assert stack.controllers[-1].intent_enabled
    assert sim.state == CHARGING
    assert sim.holding[REG_MAX_CHARGING_POWER] == 32
    assert stack.state(SWITCH) == "on"
