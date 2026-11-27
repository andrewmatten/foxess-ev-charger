"""3.2 R3: changing the poll interval polls straight away, so a shorter
interval does not leave entities unavailable until the old timer fires."""
from __future__ import annotations

from unittest.mock import patch

from custom_components.foxess_charger.const import DOMAIN

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY = "r3optionsentry1"


async def test_interval_change_polls_now(hass, enable_custom_integrations):
    entry = make_entry(ENTRY)
    entry.add_to_hass(hass)
    h = Harness(hass, entry, FakeController(status=1))
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        with patch.object(coordinator, "async_refresh", wraps=coordinator.async_refresh) as poll:
            hass.config_entries.async_update_entry(entry, options={**entry.options, "scan_interval": 5})
            await hass.async_block_till_done()
            assert poll.call_count == 1
            # Same interval again (e.g. only the phase-box option changed): no extra poll.
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, "phase_box_fitted": True})
            await hass.async_block_till_done()
            assert poll.call_count == 1
    finally:
        await h.async_unload()
