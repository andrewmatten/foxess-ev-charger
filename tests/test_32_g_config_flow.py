"""3.2 package G: config flow reconfigure, error handling and HA 2024.12 floor.

None of these tests talk to a charger: the connection check is either
patched at ``_async_charger_answers`` or driven through a fake client.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxess_charger.const import (
    CONF_PHASE_BOX_FITTED,
    CONF_SLAVE_ID,
    DOMAIN,
)

from ha_harness import make_entry

PROBE = "custom_components.foxess_charger.config_flow.FoxESSChargerConfigFlow._async_charger_answers"
CLIENT = "custom_components.foxess_charger.config_flow.FoxESSModbusClient"
SETUP = "custom_components.foxess_charger.async_setup_entry"

ENTRY = "pkggrentry01"
# Address, port and unit ID of the entries made by ha_harness.make_entry.
CURRENT = {CONF_HOST: "192.0.2.10", CONF_PORT: 502, CONF_SLAVE_ID: 1}
NEW = {CONF_HOST: "192.0.2.11", CONF_PORT: 1502, CONF_SLAVE_ID: 7}
# What an entry set up before 3.2 was identified by: host and unit ID only.
LEGACY_UNIQUE_ID = "192.0.2.10-1"


def _suggested(result) -> dict:
    """What a form pre-fills, keyed by field."""
    return {
        key.schema: (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }


def _defaults(result) -> dict:
    """The default of each field, keyed by field (the options form style)."""
    return {key.schema: key.default() if callable(key.default) else key.default
            for key in result["data_schema"].schema}


class _Client:
    """Stands in for FoxESSModbusClient during the connection check."""

    instances: list[_Client] = []
    reply: object = [1]

    def __init__(self, *args) -> None:
        self.args = args
        self.disconnected = False
        _Client.instances.append(self)

    def read_registers(self, address, count, quiet=False):
        if isinstance(_Client.reply, Exception):
            raise _Client.reply
        return _Client.reply

    def disconnect(self) -> None:
        self.disconnected = True


def _legacy_entry() -> MockConfigEntry:
    """An entry as a version before 3.2 stored it."""
    return MockConfigEntry(
        domain=DOMAIN, data=dict(CURRENT), options={"scan_interval": 10},
        entry_id=ENTRY, title="FoxESS Charger (192.0.2.10)", version=1,
        unique_id=LEGACY_UNIQUE_ID,
    )


async def _start_reconfigure(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


async def _add(hass, data):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(PROBE, return_value=True), patch(SETUP, return_value=True):
        return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def test_host_whitespace_is_stripped(hass, enable_custom_integrations):
    result = await _add(hass, {CONF_HOST: "  192.0.2.10\t", CONF_PORT: 502, CONF_SLAVE_ID: 1})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_HOST] == "192.0.2.10"
    assert entry.title == "FoxESS Charger (192.0.2.10)"
    assert entry.unique_id == "192.0.2.10-1"


async def test_same_charger_is_refused(hass, enable_custom_integrations):
    first = await _add(hass, {CONF_HOST: "192.0.2.10", CONF_PORT: 502, CONF_SLAVE_ID: 1})
    assert first["result"].unique_id == "192.0.2.10-1"
    again = await _add(hass, {CONF_HOST: " 192.0.2.10 ", CONF_PORT: 502, CONF_SLAVE_ID: 1})
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"


async def test_error_keeps_entered_values(hass, enable_custom_integrations):
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert form["type"] is FlowResultType.FORM
    assert not any(value is not None for value in _suggested(form).values())
    typed = {CONF_HOST: "192.0.2.10", CONF_PORT: 1502, CONF_SLAVE_ID: 7}
    with patch(PROBE, return_value=False):
        retry = await hass.config_entries.flow.async_configure(form["flow_id"], typed)
    assert retry["type"] is FlowResultType.FORM
    assert retry["step_id"] == "user"
    assert retry["errors"] == {"base": "cannot_connect"}
    assert _suggested(retry) == typed
    # The fields still validate what they did.
    with pytest.raises(vol.Invalid):
        retry["data_schema"]({CONF_HOST: "192.0.2.10", CONF_PORT: 0, CONF_SLAVE_ID: 7})
    # A retry with the kept values succeeds.
    with patch(PROBE, return_value=True), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(retry["flow_id"], typed)
    assert done["type"] is FlowResultType.CREATE_ENTRY


async def test_unexpected_probe_error_is_unknown(hass, enable_custom_integrations, caplog):
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    typed = {CONF_HOST: "192.0.2.10", CONF_PORT: 502, CONF_SLAVE_ID: 1}
    _Client.instances = []
    _Client.reply = OSError("probe blew up")
    with patch(CLIENT, _Client):
        retry = await hass.config_entries.flow.async_configure(form["flow_id"], typed)
    assert retry["type"] is FlowResultType.FORM
    assert retry["errors"] == {"base": "unknown"}
    # The failed probe was closed and the fault logged with its traceback.
    assert [client.disconnected for client in _Client.instances] == [True]
    assert any(
        record.name == "custom_components.foxess_charger.config_flow" and record.exc_info
        for record in caplog.records
    )
    # Still a live flow with the typed values kept, so the user can retry.
    assert _suggested(retry) == typed
    _Client.reply = [1]
    with patch(CLIENT, _Client), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(retry["flow_id"], typed)
    assert done["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_updates_connection_and_keeps_identity(
    hass, enable_custom_integrations
):
    entry = _legacy_entry()
    entry.add_to_hass(hass)
    # An enabled entity, as if the user had turned it on.
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{ENTRY}_power", config_entry=entry,
        suggested_object_id="foxess_charger_power",
    )
    form = await _start_reconfigure(hass, entry)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "reconfigure"
    # Opens pre-filled with what is stored now.
    assert _suggested(form) == CURRENT
    # Setup and unload are faked: HA reloads a loaded entry after a
    # reconfigure, and this test has no charger behind the entry.
    with patch(PROBE, return_value=True), patch(
        "custom_components.foxess_charger.async_unload_entry", return_value=True
    ), patch(SETUP, return_value=True):
        done = await hass.config_entries.flow.async_configure(form["flow_id"], dict(NEW))
        await hass.async_block_till_done()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    assert dict(entry.data) == NEW
    # Same entry, same identity: nothing else moved.
    assert entry.entry_id == ENTRY
    assert entry.unique_id == LEGACY_UNIQUE_ID
    assert dict(entry.options) == {"scan_interval": 10}
    assert entry.title == "FoxESS Charger (192.0.2.10)"
    registry = er.async_get(hass)
    entity = registry.async_get_entity_id("sensor", DOMAIN, f"{ENTRY}_power")
    assert entity is not None
    assert registry.async_get(entity).config_entry_id == ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_error_keeps_entry_and_entered_values(
    hass, enable_custom_integrations
):
    entry = _legacy_entry()
    entry.add_to_hass(hass)
    form = await _start_reconfigure(hass, entry)
    with patch(PROBE, side_effect=RuntimeError("boom")):
        retry = await hass.config_entries.flow.async_configure(form["flow_id"], dict(NEW))
    assert retry["type"] is FlowResultType.FORM
    assert retry["errors"] == {"base": "unknown"}
    assert _suggested(retry) == NEW
    with patch(PROBE, return_value=False):
        retry = await hass.config_entries.flow.async_configure(retry["flow_id"], dict(NEW))
    assert retry["type"] is FlowResultType.FORM
    assert retry["errors"] == {"base": "cannot_connect"}
    assert _suggested(retry) == NEW
    # Nothing was written while the charger did not answer.
    assert dict(entry.data) == CURRENT
    assert entry.unique_id == LEGACY_UNIQUE_ID


async def test_options_flow_works_without_constructor(hass, enable_custom_integrations):
    entry = make_entry(ENTRY)
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "init"
    # Defaults come from the entry through the HA-provided config_entry.
    assert _defaults(form) == {"scan_interval": 10, "phase_box_fitted": False}
    too_low = await hass.config_entries.options.async_configure(
        form["flow_id"], {"scan_interval": 4, CONF_PHASE_BOX_FITTED: True}
    )
    assert too_low["type"] is FlowResultType.FORM
    assert too_low["errors"] == {"base": "scan_interval_too_low"}
    done = await hass.config_entries.options.async_configure(
        too_low["flow_id"], {"scan_interval": 25, CONF_PHASE_BOX_FITTED: True}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {"scan_interval": 25, "phase_box_fitted": True}
    again = await hass.config_entries.options.async_init(entry.entry_id)
    assert _defaults(again) == {"scan_interval": 25, "phase_box_fitted": True}
