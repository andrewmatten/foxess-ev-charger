"""C9: Default Current (Fallback) number max follows the detected model's max_current_a."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "c9defaultcurrent"


def _make_default_current(data: dict) -> FoxESSNumber:
    desc = next(d for d in NUMBERS if d.key == "default_current")
    coordinator = MagicMock()
    coordinator.data = data
    return FoxESSNumber(coordinator, desc, MagicMock(entry_id=ENTRY_ID))


def test_default_current_max_follows_model_capability():
    assert _make_default_current({}).native_max_value == 32.0
    assert _make_default_current({"id_model_code": "A7300P1-E-B-WO"}).native_max_value == 32.0
    assert _make_default_current({"id_model_code": "A022-XYZ"}).native_max_value == 32.0
    assert _make_default_current({"id_model_code": "A011-XYZ"}).native_max_value == 16.0


async def test_default_current_max_is_16a_on_a011_in_ha(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    orig_poll = fake.async_poll

    async def _poll():
        snap = await orig_poll()
        snap["id_model_code"] = "A011KP1-E-2"
        return snap

    fake.async_poll = _poll
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        state = hass.states.get("number.foxess_charger_default_current_fallback")
        assert state.attributes["max"] == 16.0
    finally:
        await harness.async_unload()
