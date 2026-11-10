"""A controller ValueError from a number's set path is a clean HomeAssistantError."""
from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from test_ha_adapters import E, _call, setup  # noqa: F401  (fixture reuse)


@pytest.mark.parametrize("fail", ["set_power", "set_current", "set_register"])
async def test_value_error_becomes_homeassistant_error(hass, setup, fail):  # noqa: F811
    h = await setup()

    async def reject(*_args):
        raise ValueError("out of range (fake)")

    entity = {"set_power": "max_charging_power", "set_current": "max_charging_current",
              "set_register": "command_time_validity"}[fail]
    setattr(h.fake, "async_" + fail, reject)
    value = {"set_power": 2.0, "set_current": 10, "set_register": 60}[fail]
    with pytest.raises(HomeAssistantError, match="could not set"):
        await _call(hass, "number", "set_value", f"number.{E}_{entity}", value=value)
