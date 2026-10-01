"""The service-call context must survive the charger's slow confirmation, so
the logbook can attribute switch/power changes (HA drops contexts after 5 s)."""
from __future__ import annotations

import time as real_time
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import Context

from ha_harness import Harness, make_entry
from fake_controller import FakeController

E = "foxess_charger"


class SlowFake(FakeController):
    """Confirmation takes `delay` (fake) seconds."""

    def __init__(self, clock, delay=15.0, **kw):
        super().__init__(**kw)
        self.clock, self.delay = clock, delay

    async def async_enable(self):
        self.clock["offset"] += self.delay
        return await super().async_enable()

    async def async_set_power(self, raw):
        self.clock["offset"] += self.delay
        return await super().async_set_power(raw)


async def _run(hass, enable_custom_integrations, domain, service, entity, data):
    clock = {"offset": 0.0}
    shim = SimpleNamespace(time=lambda: real_time.time() + clock["offset"])
    fake = SlowFake(clock, status=1)
    harness = Harness(hass, make_entry("contextentry01"), fake)
    with patch("homeassistant.helpers.entity.time", shim), \
            patch("homeassistant.helpers.entity.timer", shim.time), \
            patch("custom_components.foxess_charger.time", shim):
        assert await harness.async_setup()
        ctx = Context()
        await hass.services.async_call(
            domain, service, {"entity_id": entity, **data}, blocking=True, context=ctx,
        )
        await hass.async_block_till_done()
        state = hass.states.get(entity)
        await harness.async_unload()
    assert clock["offset"] > 5
    assert state.context.id == ctx.id


async def test_switch_keeps_service_context(hass, enable_custom_integrations):
    await _run(hass, enable_custom_integrations, "switch", "turn_on",
               f"switch.{E}_charging", {})


async def test_power_number_keeps_service_context(hass, enable_custom_integrations):
    await _run(hass, enable_custom_integrations, "number", "set_value",
               f"number.{E}_max_charging_power", {"value": 5.0})
