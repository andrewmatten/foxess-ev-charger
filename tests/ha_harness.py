"""Full Home Assistant setup harness for the FoxESS integration tests.

Sets the integration up through ``hass.config_entries`` (the same path HA
uses at runtime) with the hardware replaced by a fake:

- Integration versions that build their hardware access through
  ``async_create_controller`` (the rebuild) get a fake controller object
  implementing the controller's public API (see tests/fake_controller.py).
- Older versions that construct ``FoxESSModbusClient`` directly get the
  register-level ``FakeCharger`` instead.

Either way nothing touches a network socket.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.foxess_charger as integration
from custom_components.foxess_charger.const import (
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID, DOMAIN,
)

ENTRY_DATA = {CONF_HOST: "192.0.2.10", CONF_PORT: 502, CONF_SLAVE_ID: 1}
ENTRY_OPTIONS = {"scan_interval": 10}


def uses_controller() -> bool:
    return hasattr(integration, "async_create_controller")


def make_entry(entry_id: str = "inventoryentry01") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, data=dict(ENTRY_DATA), options=dict(ENTRY_OPTIONS),
        entry_id=entry_id, title="FoxESS Charger (192.0.2.10)", version=1,
    )


class Harness:
    """Holds the patches and fakes for one set-up config entry."""

    def __init__(self, hass, entry: MockConfigEntry, fake) -> None:
        self.hass = hass
        self.entry = entry
        self.fake = fake
        self._stack = ExitStack()

    async def async_setup(self) -> bool:
        if uses_controller():
            fake = self.fake

            async def _factory(hass, entry, *, persist=None):
                fake.persist = persist
                return fake

            self._stack.enter_context(
                patch.object(integration, "async_create_controller", _factory)
            )
        else:
            fake = self.fake
            self._stack.enter_context(
                patch.object(integration, "FoxESSModbusClient", lambda *a, **k: fake)
            )
        if self.entry.entry_id not in {
            e.entry_id for e in self.hass.config_entries.async_entries(DOMAIN)
        }:
            self.entry.add_to_hass(self.hass)
        ok = await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()
        return ok

    async def async_unload(self) -> None:
        try:
            await self.hass.config_entries.async_unload(self.entry.entry_id)
            await self.hass.async_block_till_done()
        finally:
            self._stack.close()


def make_fake(*, idle: bool = True):
    """Hardware fake matching the integration version under test."""
    if uses_controller():
        from fake_controller import FakeController

        return FakeController(status=1 if idle else 3)
    from fake_charger import FakeCharger

    fake = FakeCharger()
    if idle:
        fake.status = 1
    return fake
