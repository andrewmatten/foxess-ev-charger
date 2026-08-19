"""Config flow for FoxESS EV Charger."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback

from .const import (
    DOMAIN, CONF_HOST, CONF_PORT, CONF_SLAVE_ID,
    DEFAULT_PORT, DEFAULT_SLAVE_ID, DEFAULT_SCAN_INTERVAL,
)
from .modbus_client import FoxESSModbusClient

_LOGGER = logging.getLogger(__name__)


class FoxESSChargerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}-{user_input[CONF_SLAVE_ID]}"
            )
            self._abort_if_unique_id_configured()

            # Validate the connection now, not just after the entry exists -
            # a bad host/port/slave ID would otherwise only surface later as
            # silently-unavailable entities with no clear error message.
            client = FoxESSModbusClient(
                user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_SLAVE_ID]
            )
            try:
                regs = await self.hass.async_add_executor_job(
                    client.read_registers, 0x1000, 1  # Device Address - cheapest real read
                )
            finally:
                await self.hass.async_add_executor_job(client.disconnect)

            if regs is None:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"FoxESS Charger ({user_input[CONF_HOST]})",
                    data={
                        CONF_HOST:     user_input[CONF_HOST],
                        CONF_PORT:     user_input[CONF_PORT],
                        CONF_SLAVE_ID: user_input[CONF_SLAVE_ID],
                    },
                    options={"scan_interval": DEFAULT_SCAN_INTERVAL},
                )

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema({
                vol.Required(CONF_HOST):                         str,
                vol.Required(CONF_PORT,     default=DEFAULT_PORT): int,
                vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): int,
            }),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FoxESSChargerOptionsFlow(config_entry)


class FoxESSChargerOptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    @property
    def config_entry(self) -> ConfigEntry:
        return self._entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        current_interval = self._entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)

        if user_input is not None:
            interval = user_input.get("scan_interval", DEFAULT_SCAN_INTERVAL)
            if interval < 5:
                errors["base"] = "scan_interval_too_low"
            elif interval > 300:
                errors["base"] = "scan_interval_too_high"
            else:
                return self.async_create_entry(title="", data={"scan_interval": interval})

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema({
                vol.Required("scan_interval", default=current_interval): int,
            }),
        )
