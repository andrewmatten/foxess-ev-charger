"""Switch platform: charging on/off, the connector lock and automatic phase
switching.

Every switch acts through the controller. A command that cannot be confirmed
on the charger is raised to the caller as a HomeAssistantError; otherwise the
switch refreshes the observation so its state follows the charger.
"""
from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import (
    FoxESSBlockAvailabilityMixin,
    FoxESSChargerCoordinator,
    FoxESSServiceContextMixin,
    build_device_info,
)
from .adapter_api import CONFIRMED, ControlError
from .const import (
    BLOCK_PHASE_BOX,
    BLOCK_STATUS,
    DOMAIN,
    REG_AUTO_PHASE_SWITCH,
    REG_LOCK_CONTROL,
    SESSION_ACTIVE_STATUSES,
)
from .session import STATUS_PHASE_SWITCHING, valid_status

# Values for the 0x4000 lock command register.
LOCK_COMMAND = 2
UNLOCK_COMMAND = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the charging, lock and auto phase switch entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [switch(coordinator, entry)
         for switch in (FoxESSChargingSwitch, FoxESSLockSwitch, FoxESSAutoPhaseSwitchSwitch)]
    )


_UNSET = object()


class _ControllerSwitch(
    FoxESSServiceContextMixin, FoxESSBlockAvailabilityMixin, CoordinatorEntity, SwitchEntity,
):
    """Shared plumbing: identity, and running a controller command."""

    _attr_has_entity_name = True
    entity_description: SwitchEntityDescription

    def __init__(self, coordinator: FoxESSChargerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self.entity_description.key}"
        self._attr_device_info = build_device_info(entry, coordinator)

    async def _async_command(
        self, command: Awaitable[Any], action: str, *, context: Any = _UNSET,
    ) -> Any:
        """Await a controller command, then refresh and publish the state.
        ``action`` completes the error message ("failed to <action>")."""
        ctx = self._take_service_context() if context is _UNSET else context
        try:
            result = await command
        except ControlError as err:
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(f"FoxESS: failed to {action}: {err}") from err
        self._applying_service_context = ctx
        try:
            self._restore_service_context(ctx)
            await self.coordinator.async_refresh()
            self._restore_service_context(ctx)
            self.async_write_ha_state()
        finally:
            self._applying_service_context = None
        return result


class FoxESSChargingSwitch(_ControllerSwitch):
    """On while charging is enabled and a session is in progress, including
    a pause requested by the vehicle. Off when the user paused charging, a
    power limit below the 1.4 kW minimum, a fault latch or Stop holds output
    at zero, or no session is running; unknown while the status cannot be read."""

    entity_description = SwitchEntityDescription(
        key="charging",
        translation_key="charging",
        icon="mdi:ev-plug-type2",
    )
    _block = BLOCK_STATUS

    @property
    def is_on(self) -> bool | None:
        controller = self.coordinator.controller
        # paused_below_minimum keeps the 3.1 reading (on while the session is
        # open): automations key on it; Charging Status Reason explains it.
        if not controller.intent_enabled or controller.phase in ("faulted", "stopped"):
            return False
        status = valid_status(self.coordinator.data or {})
        if status is None:
            return None
        return status in SESSION_ACTIVE_STATUSES or status == STATUS_PHASE_SWITCHING

    @property
    def extra_state_attributes(self) -> dict:
        controller = self.coordinator.controller
        return {
            "intent_enabled": controller.intent_enabled, "control_phase": controller.phase,
            "latch_cause": controller.latch_cause, "needs_human": controller.needs_human,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        # A hard latch needs a person (a call with a user, not an automation)
        # who could see it: only the needs_human this switch last showed
        # counts. A latch that landed since then is not cleared; the refused
        # command refreshes the state, so the person sees it and can retry.
        context = self._take_service_context()
        person = context is not None and context.user_id is not None
        controller = self.coordinator.controller
        shown = self.hass.states.get(self.entity_id) if self.hass else None
        clear_hard = person and shown is not None \
            and shown.attributes.get("needs_human") is True
        await self._async_command(
            controller.async_enable(clear_hard=clear_hard),
            "start charging",
            context=context,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        result = await self._async_command(
            self.coordinator.controller.async_pause(), "pause charging",
        )
        # A confirmed user pause closes the logical session.
        if getattr(result, "outcome", None) == CONFIRMED:
            await self.coordinator.async_user_paused()


class FoxESSLockSwitch(_ControllerSwitch):
    """Connector lock: on = locked (register 0x100F reads 1)."""

    entity_description = SwitchEntityDescription(
        key="lock",
        translation_key="lock",
        icon="mdi:lock",
        entity_category=EntityCategory.CONFIG,
    )
    _block = BLOCK_STATUS

    @property
    def is_on(self) -> bool | None:
        lock = (self.coordinator.data or {}).get("lock_status")
        if lock not in (0, 1):  # e.g. 65535: unknown, not unlocked
            return None
        return lock == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_LOCK_CONTROL, LOCK_COMMAND),
            "lock the connector",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_LOCK_CONTROL, UNLOCK_COMMAND),
            "unlock the connector",
        )


class FoxESSAutoPhaseSwitchSwitch(_ControllerSwitch):
    """Automatic phase switching (0x300A): the charger's own function on
    three-phase models. The manual phase command (0x4002) is what needs the box."""

    entity_description = SwitchEntityDescription(
        key="auto_phase_switch",
        translation_key="auto_phase_switch",
        icon="mdi:auto-fix",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    )
    _block = BLOCK_PHASE_BOX

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get("auto_phase_switch")
        if val not in (0, 1) or isinstance(val, bool):
            return None
        return val == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_AUTO_PHASE_SWITCH, 1),
            "enable auto phase switch",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_command(
            self.coordinator.controller.async_set_register(REG_AUTO_PHASE_SWITCH, 0),
            "disable auto phase switch",
        )
