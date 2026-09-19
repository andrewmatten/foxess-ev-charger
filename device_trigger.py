"""Provides device automations (triggers) for FoxESS EV Charger.

Five of the six trigger types are a thin wrapper around HA's generic state
trigger platform (homeassistant.components.homeassistant.triggers.state) -
the same pattern HA core's own binary_sensor/device_trigger.py uses (that
file is the canonical reference this was modeled on). Each is just a state
transition of an entity this integration already exposes:

    vehicle_plugged_in  -> binary_sensor "vehicle_connected"    to "on"
    charging_started    -> binary_sensor "is_charging"          to "on"
    charging_stopped    -> binary_sensor "is_charging"          to "off"
    fault                -> binary_sensor "has_fault"            to "on"
    alarm                -> binary_sensor "has_alarm"            to "on"

session_completed is different (see async_attach_trigger below): it used to
be keyed on any change of the "Last Session Duration" sensor's value, on the
theory that FoxESSChargerCoordinator._track_session() only recomputes that
sensor at the exact moment a session ends, so any change in its value *is*
the signal a session just completed. That had two real bugs, found in the
2026-09 second audit:

1. A state RESTORED at HA startup looks identical to a genuine completion -
   the sensor's very first state write this process (whatever value
   restoration/persistence populated it with) is indistinguishable from a
   real "just finished charging" transition to a plain state trigger.
2. Two genuinely back-to-back sessions with an identical (rounded) duration
   produce no state change at all, so the trigger would silently never fire
   for the second one.

Fixed by keying session_completed off EVENT_SESSION_COMPLETED (const.py) -
an actual internal event FoxESSChargerCoordinator._track_session() fires
only at the exact instant it detects a genuine active -> inactive
transition, never as a side effect of restoring/seeding persisted state.
Neither bug above can occur against a real event: restoration never calls
_track_session()'s transition logic at all (see __init__.py's _fetch()),
and the event fires once per real transition regardless of what value it
carries, so two identical-duration sessions each still fire their own event.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import state as state_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITY_ID, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_SESSION_COMPLETED

TRIGGER_TYPES = {
    "vehicle_plugged_in", "charging_started", "charging_stopped",
    "session_completed", "fault", "alarm",
}

# State-based trigger types only - session_completed is handled entirely
# separately (see async_attach_trigger/async_get_triggers below), so it
# deliberately has no entry in either of these two tables.
_TRIGGER_ENTITY_KEY: dict[str, str] = {
    "vehicle_plugged_in": "vehicle_connected",
    "charging_started":   "is_charging",
    "charging_stopped":   "is_charging",
    "fault":              "has_fault",
    "alarm":              "has_alarm",
}
_TRIGGER_TO_STATE: dict[str, str] = {
    "vehicle_plugged_in": "on",
    "charging_started":   "on",
    "charging_stopped":   "off",
    "fault":              "on",
    "alarm":              "on",
}

# entity_id is only meaningful for the five state-based trigger types -
# session_completed doesn't bind to any entity (see module docstring), so
# it's Optional here rather than Required; async_attach_trigger validates
# it's actually present for the types that need it.
TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Optional(CONF_ENTITY_ID): cv.entity_id_or_uuid,
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
    }
)


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict]:
    """Lists device triggers available for a FoxESS EV Charger device.

    The five state-based triggers only apply if their backing entity exists
    AND isn't disabled - a disabled entity is never written to the state
    machine, so a state trigger attached to it would silently never fire.
    Matched by unique_id (`{config_entry_id}_{key}`) rather than entity_id,
    since unique_id is stable and doesn't depend on whether the user has
    renamed the entity.

    session_completed doesn't depend on any entity's state at all (see
    module docstring) - offered whenever the device belongs to this
    integration at all, regardless of which of its entities are
    enabled/disabled.
    """
    entity_registry = er.async_get(hass)
    device_entries = [
        entry for entry in er.async_entries_for_device(entity_registry, device_id)
        if entry.platform == DOMAIN
    ]
    if not device_entries:
        return []

    triggers: list[dict] = []
    for trigger_type, key in _TRIGGER_ENTITY_KEY.items():
        entry = next(
            (
                e for e in device_entries
                if e.unique_id == f"{e.config_entry_id}_{key}" and e.disabled_by is None
            ),
            None,
        )
        if entry is None:
            continue
        triggers.append({
            "platform":  "device",
            "device_id": device_id,
            "domain":    DOMAIN,
            "entity_id": entry.id,
            "type":      trigger_type,
        })

    triggers.append({
        "platform":  "device",
        "device_id": device_id,
        "domain":    DOMAIN,
        "type":      "session_completed",
    })

    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attaches the trigger matching the requested trigger type."""
    trigger_type = config[CONF_TYPE]

    if trigger_type == "session_completed":
        return _attach_session_completed_trigger(hass, config, action, trigger_info)

    if CONF_ENTITY_ID not in config:
        raise vol.Invalid(f"entity_id is required for trigger type {trigger_type!r}")

    state_config = {
        state_trigger.CONF_PLATFORM:  "state",
        state_trigger.CONF_ENTITY_ID: config[CONF_ENTITY_ID],
        state_trigger.CONF_TO:        _TRIGGER_TO_STATE[trigger_type],
    }
    state_config = await state_trigger.async_validate_trigger_config(hass, state_config)
    return await state_trigger.async_attach_trigger(
        hass, state_config, action, trigger_info, platform_type="device"
    )


@callback
def _attach_session_completed_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attaches a plain event-bus listener for EVENT_SESSION_COMPLETED,
    filtered to this specific device.

    A device automation config only carries device_id, not the underlying
    config entry - resolved once here via the device registry (this
    integration's device identifier is (DOMAIN, entry_id), a 1:1 mapping)
    rather than requiring the coordinator to know its own device_id. Uses a
    direct hass.bus.async_listen() rather than the generic
    homeassistant.components.homeassistant.triggers.event helper: that
    helper's event_type/event_data matching goes through cv.template(),
    which needs a real Jinja template-validation context set up the way a
    normal automation config flow provides it - overkill (and awkward to
    invoke correctly from here) for a fixed, non-templated event type and a
    single literal device filter.
    """
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(config[CONF_DEVICE_ID])
    entry_id = (
        next((eid for domain, eid in device.identifiers if domain == DOMAIN), None)
        if device else None
    )

    trigger_data = trigger_info["trigger_data"]
    job = HassJob(action, f"{DOMAIN} session_completed device trigger")

    @callback
    def handle_event(event) -> None:
        if event.data.get("entry_id") != entry_id:
            return
        hass.async_run_hass_job(
            job,
            {
                "trigger": {
                    **trigger_data,
                    "platform":    "device",
                    "event":       event,
                    "description": "FoxESS EV Charger device - session_completed",
                }
            },
            event.context,
        )

    return hass.bus.async_listen(EVENT_SESSION_COMPLETED, handle_event)
