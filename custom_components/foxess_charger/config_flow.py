"""Config and options flow for FoxESS EV Charger.

Setup asks where the charger is on the LAN and only creates the entry once
the charger has answered a real Modbus read. The options flow sets how often
it is polled.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SLAVE_ID,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE_ID,
    DOMAIN,
    REG_DEVICE_ADDRESS,
)
from .modbus_client import FoxESSModbusClient

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult

# Poll interval bounds offered in the options flow, in seconds.
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 300

CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
        # The unit ID travels as one byte on the wire (modbus_client._frame).
        vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): vol.All(
            int, vol.Range(min=0, max=255)
        ),
    }
)


class FoxESSChargerConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per charger, identified by host and Modbus unit ID."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            slave_id = user_input[CONF_SLAVE_ID]
            await self.async_set_unique_id(f"{host}-{slave_id}")
            self._abort_if_unique_id_configured()
            if await self._async_charger_answers(host, port, slave_id):
                return self.async_create_entry(
                    title=f"FoxESS Charger ({host})",
                    data={CONF_HOST: host, CONF_PORT: port, CONF_SLAVE_ID: slave_id},
                    options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
                )
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user", data_schema=CONNECTION_SCHEMA, errors=errors,
        )

    async def _async_charger_answers(self, host: str, port: int, slave_id: int) -> bool:
        """Read one register (the device address, the cheapest real read).

        Done before the entry exists so a wrong host, port or unit ID is
        reported on the form instead of surfacing later as entities that
        never become available. The probe connection is always closed.
        """
        probe = FoxESSModbusClient(host, port, slave_id)
        try:
            reply = await self.hass.async_add_executor_job(
                probe.read_registers, REG_DEVICE_ADDRESS, 1
            )
        finally:
            await self.hass.async_add_executor_job(probe.disconnect)
        return reply is not None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FoxESSChargerOptionsFlow(config_entry)


class FoxESSChargerOptionsFlow(OptionsFlow):
    """Poll interval."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    @property
    def config_entry(self) -> ConfigEntry:
        # Served from the entry passed in, which works on HA versions both
        # before and after OptionsFlow gained its own config_entry property.
        return self._entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            if interval < MIN_SCAN_INTERVAL:
                errors["base"] = "scan_interval_too_low"
            elif interval > MAX_SCAN_INTERVAL:
                errors["base"] = "scan_interval_too_high"
            else:
                return self.async_create_entry(title="", data={CONF_SCAN_INTERVAL: interval})
        current = self._entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_SCAN_INTERVAL, default=current): int}),
            errors=errors,
        )
