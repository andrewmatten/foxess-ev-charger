"""3.2 R2 (Opus review): a person's turn-on clears a hard latch only if the
charging switch was already showing it; a latch that landed unseen is not
cleared by a tap meant for an ordinary pause. After the refusal the switch
shows the latch, and a second tap clears it."""
from __future__ import annotations

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError

from ha_harness import Harness, make_entry
from test_32_c18_error_refresh_and_live_human import ENTRY_ID, _LatchOnErrorController

SW = "switch.foxess_charger_charging"


async def test_unseen_hard_latch_is_not_cleared_by_a_tap(
    hass, enable_custom_integrations, hass_admin_user
):
    fake = _LatchOnErrorController(status=4)
    fake._enabled = False
    fake.phase = "paused"
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        assert hass.states.get(SW).attributes["needs_human"] is False
        # Hard latch lands in the background; the switch has not shown it yet.
        fake.safety_latched, fake.latch_cause, fake.needs_human = True, "hard", True
        fake.phase = "faulted"
        user = Context(user_id=hass_admin_user.id)
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": SW}, blocking=True, context=user)
        assert fake.last_clear_hard is False
        await hass.async_block_till_done()
        assert hass.states.get(SW).attributes["needs_human"] is True   # now visible
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": SW}, blocking=True,
            context=Context(user_id=hass_admin_user.id))
        assert fake.last_clear_hard is True
    finally:
        await harness.async_unload()
