"""Config and options flow for FoxESS EV Charger.

Setup asks where the charger is on the LAN and only creates the entry once
the charger has answered a real Modbus read. The options flow sets how often
it is polled. A reconfigure flow points an existing entry at a new host,
port or unit ID, once the charger answers from there.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback

from .const import (
    CONF_HOST,
    CONF_PHASE_BOX_FITTED,
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

_LOGGER = logging.getLogger(__name__)

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
    """One entry per charger, identified by host and Modbus unit ID.

    A reconfigure never changes an entry's unique_id.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {**user_input, CONF_HOST: user_input[CONF_HOST].strip()}
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            slave_id = user_input[CONF_SLAVE_ID]
            await self.async_set_unique_id(f"{host}-{slave_id}")
            self._abort_if_unique_id_configured()
            if self._address_in_use(host, slave_id):
                return self.async_abort(reason="already_configured")
            errors = await self._async_connection_errors(host, port, slave_id)
            if not errors:
                return self.async_create_entry(
                    title=f"FoxESS Charger ({host})",
                    data={CONF_HOST: host, CONF_PORT: port, CONF_SLAVE_ID: slave_id},
                    options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CONNECTION_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move an existing entry to another host, port or unit ID.

        Same connection check as setup, then the entry is updated in place:
        entry_id, unique_id, options and entities are untouched.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {**user_input, CONF_HOST: user_input[CONF_HOST].strip()}
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            slave_id = user_input[CONF_SLAVE_ID]
            # Two entries on one charger would be two writers.
            if self._address_in_use(host, slave_id, exclude=entry.entry_id):
                return self.async_abort(reason="already_configured")
            errors = await self._async_connection_errors(host, port, slave_id)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SLAVE_ID: slave_id,
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONNECTION_SCHEMA, user_input if user_input is not None else dict(entry.data)
            ),
            errors=errors,
        )

    def _address_in_use(self, host: str, slave_id: int, exclude: str | None = None) -> bool:
        """True if another entry already talks to this charger (its current
        address, which a reconfigure may have moved away from its unique_id)."""
        return any(
            e.entry_id != exclude
            and str(e.data.get(CONF_HOST, "")).strip().lower() == host.lower()
            and e.data.get(CONF_SLAVE_ID) == slave_id
            for e in self._async_current_entries(include_ignore=False)
        )

    async def _async_connection_errors(
        self, host: str, port: int, slave_id: int
    ) -> dict[str, str]:
        """Run the connection check and map its outcome to form errors.

        Empty when the charger answered. A charger that stays silent is
        something the user can fix (``cannot_connect``); anything else is
        logged and shown as ``unknown`` instead of escaping the flow.
        """
        try:
            if await self._async_charger_answers(host, port, slave_id):
                return {}
        except Exception:  # noqa: BLE001 - whatever it was, it must land on the form
            _LOGGER.exception(
                "Connection check for the charger at %s:%s (unit %s) failed unexpectedly",
                host, port, slave_id,
            )
            return {"base": "unknown"}
        return {"base": "cannot_connect"}

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
        return FoxESSChargerOptionsFlow()


class FoxESSChargerOptionsFlow(OptionsFlow):
    """Poll interval and whether a phase-switch box is fitted."""

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
                return self.async_create_entry(title="", data={
                    CONF_SCAN_INTERVAL: interval,
                    CONF_PHASE_BOX_FITTED: user_input.get(CONF_PHASE_BOX_FITTED, False),
                })
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        box = self.config_entry.options.get(CONF_PHASE_BOX_FITTED, False)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=current): int,
                vol.Optional(CONF_PHASE_BOX_FITTED, default=box): bool,
            }),
            errors=errors,
        )
