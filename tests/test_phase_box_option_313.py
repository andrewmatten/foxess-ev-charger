"""The manual phase selector needs a FoxESS phase-switch box, so it is
off unless the user says a box is fitted (option `phase_box_fitted`)."""
from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.foxess_charger.const import DOMAIN, REG_PHASE_SWITCHING
from custom_components.foxess_charger.select import FoxESSPhaseSelect, FoxESSWorkModeSelect

from fake_controller import FakeController
from ha_harness import Harness, make_entry

FLAG = "phase_box_fitted"


async def _setup(hass, options):
    entry = make_entry("phaseboxentry01")
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    h = Harness(hass, entry, FakeController(status=1))
    assert await h.async_setup()
    return h, hass.data[DOMAIN][entry.entry_id]["coordinator"]


def _select(cls, coordinator, entry):
    entity = cls(coordinator, entry)
    entity.async_write_ha_state = lambda: None
    return entity


async def test_option_off_is_unavailable_and_refuses_before_any_write(hass, enable_custom_integrations):
    h, coordinator = await _setup(hass, {})
    try:
        select = _select(FoxESSPhaseSelect, coordinator, h.entry)
        assert select.available is False
        with pytest.raises(HomeAssistantError):
            await select.async_select_option("l2_single_phase")
        assert not [c for c in h.fake.calls if c[0] == "set_register"]
    finally:
        await h.async_unload()


async def test_option_on_is_available_and_writes_once(hass, enable_custom_integrations):
    h, coordinator = await _setup(hass, {FLAG: True})
    try:
        select = _select(FoxESSPhaseSelect, coordinator, h.entry)
        assert select.available is True
        await select.async_select_option("l2_single_phase")
        assert [c for c in h.fake.calls if c[0] == "set_register"] == [
            ("set_register", REG_PHASE_SWITCHING, 1)
        ]
    finally:
        await h.async_unload()


async def test_work_mode_select_ignores_the_option(hass, enable_custom_integrations):
    h, coordinator = await _setup(hass, {})
    try:
        assert _select(FoxESSWorkModeSelect, coordinator, h.entry).available is True
    finally:
        await h.async_unload()


async def test_options_flow_shows_flag_default_off_and_saves_both_keys(hass, enable_custom_integrations):
    h, _ = await _setup(hass, {})
    try:
        form = await hass.config_entries.options.async_init(h.entry.entry_id)
        keys = {str(k): k for k in form["data_schema"].schema}
        assert FLAG in keys and keys[FLAG].default() is False
        done = await hass.config_entries.options.async_configure(
            form["flow_id"], {"scan_interval": 20, FLAG: True}
        )
        assert done["data"] == {"scan_interval": 20, FLAG: True}
    finally:
        await h.async_unload()
