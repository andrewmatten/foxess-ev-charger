"""Config flow rejects a port or Modbus unit ID that cannot work."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.foxess_charger.const import DOMAIN

PROBE = "custom_components.foxess_charger.config_flow.FoxESSChargerConfigFlow._async_charger_answers"


async def _submit(hass, **overrides):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    data = {"host": "192.0.2.10", "port": 502, "slave_id": 1, **overrides}
    with patch(PROBE, return_value=True), patch(
        "custom_components.foxess_charger.async_setup_entry", return_value=True
    ):
        return await hass.config_entries.flow.async_configure(result["flow_id"], data)


@pytest.mark.parametrize(
    "bad", [{"slave_id": 256}, {"slave_id": 300}, {"slave_id": -1}, {"port": 0}, {"port": 65536}]
)
async def test_impossible_port_or_unit_id_is_rejected(hass, enable_custom_integrations, bad):
    with pytest.raises(InvalidData):
        await _submit(hass, **bad)


@pytest.mark.parametrize("ok", [{"slave_id": 0}, {"slave_id": 255}, {"port": 1}, {"port": 65535}])
async def test_limits_are_accepted(hass, enable_custom_integrations, ok):
    result = await _submit(hass, **ok)
    assert result["type"] is FlowResultType.CREATE_ENTRY
