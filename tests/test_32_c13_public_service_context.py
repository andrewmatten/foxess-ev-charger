"""C13: Service-call context and hard-latch person check use public HA APIs
(async_set_context) without mutating Entity._context_set or reading Entity._context."""
from __future__ import annotations

import pathlib
import time as real_time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError

from custom_components.foxess_charger.adapter_api import ControlError
from custom_components.foxess_charger.const import DOMAIN

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "c13contextentry"
COMP_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "foxess_charger"
)


def test_integration_does_not_access_private_entity_context_internals():
    for py_file in COMP_DIR.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "_context_set" not in text, py_file.name
        assert "._context" not in text, py_file.name


class _HardLatchFake(FakeController):
    def __init__(self, clock, delay=15.0, **kw):
        super().__init__(**kw)
        self.clock = clock
        self.delay = delay
        self.last_clear_hard = None

    async def async_enable(self, *, clear_hard=False):
        self.clock["offset"] += self.delay
        self.last_clear_hard = clear_hard
        if self.needs_human and not clear_hard:
            raise ControlError("hard latch requires a person (fake)")
        self.needs_human = False
        self.latch_cause = None
        return await super().async_enable(clear_hard=clear_hard)


async def test_public_context_preserves_slow_call_without_stamping_later_polls_or_leaking_person(
    hass, enable_custom_integrations, hass_admin_user
):
    clock = {"offset": 0.0}
    shim = SimpleNamespace(time=lambda: real_time.time() + clock["offset"])
    fake = _HardLatchFake(clock, delay=15.0, status=4)
    fake.needs_human = True
    fake._enabled = False

    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    with patch("homeassistant.helpers.entity.time", shim), patch(
        "homeassistant.helpers.entity.timer", shim.time
    ):
        assert await harness.async_setup()
        try:
            # 1. Automation turn-on (no user_id) under hard latch is refused.
            auto_ctx = Context()
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": "switch.foxess_charger_charging"},
                    blocking=True,
                    context=auto_ctx,
                )
            assert fake.last_clear_hard is False

            # 2. Person turn-on (with user_id) succeeds and keeps its context
            #    across the 15 s confirmation (> HA's 5 s default expiry).
            user_ctx = Context(user_id=hass_admin_user.id)
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": "switch.foxess_charger_charging"},
                blocking=True,
                context=user_ctx,
            )
            await hass.async_block_till_done()
            assert fake.last_clear_hard is True
            state = hass.states.get("switch.foxess_charger_charging")
            assert state.state == "on"
            assert state.context.id == user_ctx.id

            # 3. 30 s later a fault trips on a background poll: must NOT
            #    inherit user_ctx (unlike the old 60 s _context_set hack) and
            #    must NOT allow an automation to clear the new hard latch.
            clock["offset"] += 30.0
            fake.needs_human = True
            fake.phase = "faulted"
            coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            fault_state = hass.states.get("switch.foxess_charger_charging")
            assert fault_state.state == "off"
            assert fault_state.context.id != user_ctx.id

            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": "switch.foxess_charger_charging"},
                    blocking=True,
                    context=Context(),
                )
            assert fake.last_clear_hard is False
        finally:
            await harness.async_unload()
