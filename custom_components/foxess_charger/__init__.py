"""FoxESS EV Charger integration.

Setup wires four pieces together: persistence (HA Store), the charging
controller (sole owner of the Modbus connection and every write), the
polling coordinator and the entity platforms. Entities never touch the
transport; every hardware change goes through the controller's API.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID, DEFAULT_SCAN_INTERVAL, DOMAIN, PLATFORMS,
)
from .coordinator import FoxESSChargerCoordinator
from .persistence import ChargerStorage

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "A7300P1-E-B-WO"

__all__ = [
    "FoxESSChargerCoordinator", "FoxESSBlockAvailabilityMixin", "build_device_info",
    "async_create_controller", "async_setup_entry", "async_unload_entry",
]


async def async_create_controller(hass: HomeAssistant, entry: ConfigEntry, *, persist=None):
    """The single factory for the hardware stack (client -> transport ->
    controller). Imported lazily so the adapters load without it."""
    from .controller import ChargingController
    from .modbus_client import FoxESSModbusClient
    from .transport import ModbusRegisterIO

    client = FoxESSModbusClient(
        entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_SLAVE_ID],
    )
    return ChargingController(ModbusRegisterIO(client), persist=persist)


def build_device_info(entry: ConfigEntry, coordinator: FoxESSChargerCoordinator) -> DeviceInfo:
    """Device identity is the config entry; the model is read from the
    charger when known."""
    model = (coordinator.data or {}).get("id_model_code") or DEFAULT_MODEL
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FoxESS Charger",
        manufacturer="FoxESS",
        model=model,
    )


class FoxESSBlockAvailabilityMixin:
    """Entities tied to one register block are available only while that
    block's reads are fresh (on top of the coordinator's own success flag),
    so a failing optional block does not take unrelated entities down.
    ``_block = None`` means not tied to a block."""

    _block: str | None = None

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.block_is_fresh(self._block)


async def _async_close_quietly(controller) -> None:
    try:
        await controller.async_close()
    except Exception:  # noqa: BLE001 - closing after a failure must not mask it
        _LOGGER.exception("Error closing FoxESS controller")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
    storage = ChargerStorage(hass, entry.entry_id)
    loaded = await storage.async_load()

    controller = await async_create_controller(
        hass, entry, persist=storage.async_save_controller,
    )
    try:
        await controller.async_initialize(loaded.controller)
    except Exception as err:
        await _async_close_quietly(controller)
        raise ConfigEntryNotReady(f"FoxESS controller did not initialize: {err}") from err

    coordinator = FoxESSChargerCoordinator(
        hass, controller, scan_interval, storage=storage, entry_id=entry.entry_id,
    )
    coordinator.restore(loaded.session, loaded.energy)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await _async_close_quietly(controller)
        raise

    try:
        # Establish the new-format state (and legacy projections) now, so a
        # restart never has to re-derive intent from legacy files.
        await storage.async_save_controller(controller.export_state())
    except Exception:  # noqa: BLE001 - controller keeps protecting in memory
        _LOGGER.exception("Could not persist FoxESS controller state at startup")

    try:
        await controller.async_start()
    except Exception as err:
        await _async_close_quietly(controller)
        raise ConfigEntryNotReady(f"FoxESS controller did not start: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "controller": controller,
        "storage": storage,
        "restore_issues": list(loaded.issues),
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data[DOMAIN].pop(entry.entry_id)
        stored["coordinator"].async_clear_work_mode_issue()
        await stored["coordinator"].async_shutdown()
        await stored["controller"].async_close()
        await stored["coordinator"].async_flush()
    return unload_ok
