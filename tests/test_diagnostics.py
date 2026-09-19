"""Tests for diagnostics.py (added 2.2.0).

Builds a real coordinator (mocked client, like test_coordinator_batching.py)
rather than mocking the coordinator itself, so this also exercises the real
block_health_snapshot() output - not just diagnostics.py's own plumbing.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.diagnostics import REDACTED

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    BLOCK_CONFIG, BLOCK_PHASE_BOX, BLOCK_STATUS, DOMAIN,
)
from custom_components.foxess_charger.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.test_coordinator_batching import make_mock_client


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.data = {"host": "192.0.2.1", "port": 502, "slave_id": 1}
    entry.options = {"scan_interval": 10}
    return entry


async def test_diagnostics_redacts_host_and_rfid_card(hass):
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = coordinator._fetch()
    coordinator.data["rfid_card"] = 0xDEADBEEF
    entry = make_entry()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "client": client,
    }

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"]["host"] == REDACTED
    assert result["coordinator_data"]["rfid_card"] == REDACTED
    # Non-redacted fields must survive untouched.
    assert result["entry"]["data"]["port"] == 502
    assert result["coordinator_data"]["total_energy_raw"] == 3785


async def test_diagnostics_redacts_serial_number(hass):
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = coordinator._fetch()
    coordinator.data["id_serial_number"] = "ABC123DEF456"
    entry = make_entry()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "client": client,
    }

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["coordinator_data"]["id_serial_number"] == REDACTED
    # Non-redacted fields must survive untouched.
    assert result["coordinator_data"]["total_energy_raw"] == 3785


async def test_diagnostics_includes_model_and_capabilities(hass):
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = coordinator._fetch()
    coordinator.data["id_model_code"] = "A022-XYZ"
    entry = make_entry()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "client": client,
    }

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["model"] == "A022-XYZ"
    assert result["detected_capabilities"] == {"max_power_kw": 22.0, "max_current_a": 32.0}


async def test_diagnostics_includes_transport_counters(hass):
    client = make_mock_client()
    client.txid_mismatches = 3
    client.short_reads = 1
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = coordinator._fetch()
    entry = make_entry()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "client": client,
    }

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["transport_counters"]["txid_mismatches"] == 3
    assert result["transport_counters"]["short_reads"] == 1


async def test_diagnostics_includes_block_health(hass):
    """Status/config blocks succeeded, phase-switch-box never did (expected
    on the single-phase mock hardware) - block_health must reflect that."""
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.data = coordinator._fetch()
    entry = make_entry()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "client": client,
    }

    result = await async_get_config_entry_diagnostics(hass, entry)

    health = result["block_health"]
    assert set(health) == {BLOCK_STATUS, BLOCK_CONFIG, BLOCK_PHASE_BOX}
    assert health[BLOCK_STATUS]["fresh"] is True
    assert health[BLOCK_CONFIG]["fresh"] is True
    assert health[BLOCK_PHASE_BOX]["fresh"] is False
    assert health[BLOCK_PHASE_BOX]["seconds_since_last_success"] is None
