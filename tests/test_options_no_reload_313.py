"""Changing an option applies live: a reload would stop the power-cap
refresh and the charger would fall back to its own maximum."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

from custom_components.foxess_charger.const import DOMAIN

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY = "noreloadentry01"
SELECT = "select.foxess_charger_phase_sequence"


async def test_options_change_applies_without_reload(hass, enable_custom_integrations):
    entry = make_entry(ENTRY)
    entry.add_to_hass(hass)
    # An enabled registry entry, as if the user had turned the entity on.
    er.async_get(hass).async_get_or_create(
        "select", DOMAIN, f"{ENTRY}_phase_sequence", config_entry=entry,
        suggested_object_id="foxess_charger_phase_sequence",
    )
    h = Harness(hass, entry, FakeController(status=1))
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        assert coordinator.update_interval == timedelta(seconds=10)
        assert hass.states.get(SELECT).state == "unavailable"
        with patch("custom_components.foxess_charger.async_unload_entry") as unload, patch(
            "custom_components.foxess_charger.async_setup_entry"
        ) as setup:
            hass.config_entries.async_update_entry(
                entry, options={"scan_interval": 25, "phase_box_fitted": True}
            )
            await hass.async_block_till_done()
        unload.assert_not_called()
        setup.assert_not_called()
        assert hass.data[DOMAIN][ENTRY]["coordinator"] is coordinator
        assert coordinator.update_interval == timedelta(seconds=25)
        assert hass.states.get(SELECT).state != "unavailable"
    finally:
        await h.async_unload()
