"""Config-entry diagnostics: redaction, model/capabilities, transport
counters and per-block health.

Ported from the 2.4.3 tests to a real config-entry setup (HA harness) with a
fake controller, so the real coordinator's block_health_snapshot() and the
controller's diagnostics feed the output.
"""
from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED

from custom_components.foxess_charger.const import (
    BLOCK_CONFIG, BLOCK_PHASE_BOX, BLOCK_STATUS, DOMAIN,
)
from custom_components.foxess_charger.diagnostics import (
    async_get_config_entry_diagnostics,
)

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY = "diagentry01"


async def diagnostics(hass, fake: FakeController) -> dict:
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        return await async_get_config_entry_diagnostics(hass, h.entry)
    finally:
        await h.async_unload()


async def test_diagnostics_redacts_host_and_rfid_card(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["rfid_card"] = 0xDEADBEEF
    result = await diagnostics(hass, fake)
    assert result["entry"]["data"]["host"] == REDACTED
    assert result["coordinator_data"]["rfid_card"] == REDACTED
    # Non-redacted fields survive untouched.
    assert result["entry"]["data"]["port"] == 502
    assert result["coordinator_data"]["total_energy_raw"] == 1000


async def test_diagnostics_redacts_serial_number(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    result = await diagnostics(hass, fake)   # fake reports serial "SERIAL"
    assert result["coordinator_data"]["id_serial_number"] == REDACTED
    assert result["coordinator_data"]["total_energy_raw"] == 1000


async def test_diagnostics_includes_model_and_capabilities(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    orig_poll = fake.async_poll

    async def _poll():
        snap = await orig_poll()
        snap["id_model_code"] = "A022-XYZ"
        return snap

    fake.async_poll = _poll
    result = await diagnostics(hass, fake)
    assert result["model"] == "A022-XYZ"
    assert result["detected_capabilities"] == {"max_power_kw": 22.0, "max_current_a": 32.0}


async def test_diagnostics_includes_transport_counters(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.diagnostics = {"txid_mismatches": 3, "short_reads": 1}
    result = await diagnostics(hass, fake)
    assert result["transport_counters"]["txid_mismatches"] == 3
    assert result["transport_counters"]["short_reads"] == 1


async def test_diagnostics_includes_block_health(hass, enable_custom_integrations):
    """Status/config succeeded, the phase-switch box never did (absent on
    the single-phase fake)."""
    result = await diagnostics(hass, FakeController(status=1))
    health = result["block_health"]
    assert set(health) == {BLOCK_STATUS, BLOCK_CONFIG, BLOCK_PHASE_BOX}
    assert health[BLOCK_STATUS]["fresh"] is True
    assert health[BLOCK_CONFIG]["fresh"] is True
    assert health[BLOCK_PHASE_BOX]["fresh"] is False
    assert health[BLOCK_PHASE_BOX]["seconds_since_last_success"] is None
