"""Diagnostics support for FoxESS EV Charger.

Standard HA config-entry diagnostics: `async_get_config_entry_diagnostics`
is auto-discovered by HA core when it exists in the integration's root
module - no registration needed beyond this file existing. Surfaces model/
firmware/detected capabilities, the per-block polling health added in
2.2.0 (see FoxESSChargerCoordinator.block_health_snapshot), and the
Modbus transport counters from the 2.1.3 transport work - the kind of
information otherwise only available by asking a user to dig through logs.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, DOMAIN, get_capabilities

# CONF_HOST: the charger's LAN IP - not useful for remote troubleshooting
# and not something to hand out in a shared diagnostics download.
# rfid_card: the last-seen RFID card's raw ID - see sensor.py's own
# entity_registry_enabled_default=False on the RFID Card sensor for the
# same privacy reasoning.
# id_serial_number: the charger's hardware serial - a real-world device
# identifier, no reason to include it in a diagnostics download shared
# outside this household.
TO_REDACT = {CONF_HOST, "rfid_card", "id_serial_number"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Returns diagnostics for a config entry."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    client = stored["client"]

    coordinator_data = dict(coordinator.data or {})
    model = coordinator_data.get("id_model_code")

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "model": model,
        "firmware_version": coordinator_data.get("software_version"),
        "detected_capabilities": get_capabilities(model),
        "block_health": coordinator.block_health_snapshot(),
        "transport_counters": {
            "txid_mismatches":       client.txid_mismatches,
            "short_reads":           client.short_reads,
            "connection_errors":     client.connection_errors,
            "unit_id_mismatches":    client.unit_id_mismatches,
            "malformed_headers":     client.malformed_headers,
            "write_echo_mismatches": client.write_echo_mismatches,
        },
        "last_update_success": coordinator.last_update_success,
        "coordinator_data": async_redact_data(coordinator_data, TO_REDACT),
    }
