"""FoxESS EV Charger integration.

Setup wires four pieces together: persistence (HA Store), the charging
controller (sole owner of the Modbus connection and every write), the
polling coordinator and the entity platforms. Entities never touch the
transport; every hardware change goes through the controller's API.
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
import time  # noqa: F401 - kept on module for test_service_context clock patch
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID, DEFAULT_SCAN_INTERVAL, DOMAIN, PLATFORMS,
)
from .coordinator import FoxESSChargerCoordinator
from .persistence import ChargerStorage
from .protocol import format_firmware_version

_LOGGER = logging.getLogger(__name__)

# Device registry identity. The model is replaced by the one the charger
# reports (0x101E) once it has been read.
MANUFACTURER = "FoxESS"
DEVICE_NAME = "FoxESS Charger"
DEFAULT_MODEL = "A7300P1-E-B-WO"

__all__ = [
    "FoxESSChargerCoordinator", "FoxESSBlockAvailabilityMixin", "build_device_info",
    "async_create_controller", "async_remove_entry", "async_setup_entry", "async_unload_entry",
]


async def async_create_controller(hass: HomeAssistant, entry: ConfigEntry, *, persist=None):
    """The single factory for the hardware stack (client -> transport ->
    controller). Imported lazily so the adapters load without it."""
    from .controller import ChargingController
    from .modbus_client import PROTOCOL_MESSAGE_INTERVAL, FoxESSModbusClient
    from .transport import ModbusRegisterIO

    client = FoxESSModbusClient(
        entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_SLAVE_ID],
        min_interval=PROTOCOL_MESSAGE_INTERVAL,
    )
    return ChargingController(ModbusRegisterIO(client), persist=persist)


def build_device_info(entry: ConfigEntry, coordinator: FoxESSChargerCoordinator) -> DeviceInfo:
    """One device per config entry, identified by the entry ID. Model, serial
    number and firmware version come from the charger once read."""
    raw_data = getattr(coordinator, "data", None)
    data = raw_data if isinstance(raw_data, Mapping) else {}
    reported_model = data.get("id_model_code")
    serial = data.get("id_serial_number")
    sw_version = format_firmware_version(data.get("software_version"))
    info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        model=(
            reported_model
            if isinstance(reported_model, str) and reported_model
            else DEFAULT_MODEL
        ),
        name=DEVICE_NAME,
    )
    if isinstance(serial, str) and serial:
        info["serial_number"] = serial
    if sw_version is not None:
        info["sw_version"] = sw_version
    return info


class FoxESSServiceContextMixin:
    """Preserve the service-call context across slow charger confirmation
    using public Entity.async_set_context() rather than mutating private
    Entity context attributes.
    """

    _pending_service_context: Context | None = None
    _applying_service_context: Context | None = None

    @callback
    def async_set_context(self, context: Context) -> None:
        super().async_set_context(context)
        self._pending_service_context = context

    def _take_service_context(self) -> Context | None:
        context = self._pending_service_context
        self._pending_service_context = None
        return context

    @callback
    def _restore_service_context(self, context: Context | None) -> None:
        if context is not None:
            super().async_set_context(context)

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._applying_service_context is not None:
            super().async_set_context(self._applying_service_context)
        super()._handle_coordinator_update()


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
    """Load saved state, start the controller, then the poller and entities."""
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
        hass, controller, entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        storage=storage, entry_id=entry.entry_id,
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
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # The controller is already writing heartbeats: never leave it
        # running unowned, or a later setup would add a second writer.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await _async_close_quietly(controller)
        try:
            await coordinator.async_shutdown()
        except Exception:  # noqa: BLE001 - must not mask the setup failure
            _LOGGER.exception("Error shutting down FoxESS coordinator")
        coordinator.async_clear_work_mode_issue()
        coordinator.async_clear_needs_human_issue()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_apply_options))
    return True


async def _async_apply_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply options live: a reload would stop the power-cap refresh and the
    charger would fall back to its own maximum. The select reads its option
    from the entry, so listeners are notified to refresh availability."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.update_interval = timedelta(
        seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    coordinator.async_update_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the entities and the work-mode repair issue, stop polling,
    drain and close the controller, then save session/energy state."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    coordinator = runtime["coordinator"]
    await coordinator.async_shutdown()
    await runtime["controller"].async_close()
    await coordinator.async_flush()
    # Cleared last: a poll still in flight during shutdown could raise them again.
    coordinator.async_clear_work_mode_issue()
    coordinator.async_clear_needs_human_issue()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry was deleted: remove its stored intent, session and energy files."""
    try:
        await ChargerStorage(hass, entry.entry_id).async_remove_all()
    except Exception:  # noqa: BLE001 - removal must not fail on leftover files
        _LOGGER.exception("Could not remove FoxESS charger storage")
