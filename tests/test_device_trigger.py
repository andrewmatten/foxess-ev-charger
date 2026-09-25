"""Tests for device_trigger.py (added 2.2.0).

Uses HA's standard device-trigger test approach: real device/entity
registry entries (via pytest-homeassistant-custom-component's `hass`
fixture and MockConfigEntry), rather than mocking the registries - the
same pattern HA core's own component test suites use for
test_device_trigger.py files.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.device_trigger import (
    async_attach_trigger,
    async_get_triggers,
)


async def _setup_device_and_entities(hass, keys: list[str]):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )

    entity_registry = er.async_get(hass)
    domain_by_key = {
        "vehicle_connected": "binary_sensor", "is_charging": "binary_sensor",
        "charging": "switch",
        "has_fault": "binary_sensor", "has_alarm": "binary_sensor",
        "last_session_duration": "sensor",
    }
    for key in keys:
        entity_registry.async_get_or_create(
            domain_by_key[key], DOMAIN, f"{entry.entry_id}_{key}",
            config_entry=entry, device_id=device.id,
        )

    return entry, device


async def test_get_triggers_returns_all_types_when_all_entities_present(hass):
    entry, device = await _setup_device_and_entities(hass, [
        "vehicle_connected", "is_charging", "charging", "has_fault", "has_alarm",
        "last_session_duration",
    ])

    triggers = await async_get_triggers(hass, device.id)

    types = {t["type"] for t in triggers}
    assert types == {
        "vehicle_plugged_in", "charging_started", "charging_stopped",
        "fault", "alarm", "session_completed",
    }
    for trigger in triggers:
        assert trigger["platform"] == "device"
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == device.id


async def test_get_triggers_omits_types_for_missing_entities(hass):
    """A device with only the Fault binary sensor registered (e.g. other
    entities disabled/removed) must only offer "fault" (plus the always-
    offered session_completed, which doesn't depend on any entity)."""
    entry, device = await _setup_device_and_entities(hass, ["has_fault"])

    triggers = await async_get_triggers(hass, device.id)

    assert {t["type"] for t in triggers} == {"fault", "session_completed"}


async def test_get_triggers_omits_disabled_entities(hass):
    """2026-09 (second audit): a disabled entity is never written to the
    state machine, so a state trigger attached to it would silently never
    fire - it must not even be offered."""
    from homeassistant.helpers import entity_registry as er

    entry, device = await _setup_device_and_entities(hass, [
        "vehicle_connected", "is_charging", "charging",
    ])
    entity_registry = er.async_get(hass)
    entry_entity = entity_registry.async_get_or_create(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_vehicle_connected",
        config_entry=entry, device_id=device.id,
    )
    entity_registry.async_update_entity(
        entry_entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER,
    )

    triggers = await async_get_triggers(hass, device.id)

    types = {t["type"] for t in triggers}
    assert "vehicle_plugged_in" not in types
    assert {"charging_started", "charging_stopped", "session_completed"} <= types


async def test_get_triggers_always_offers_session_completed(hass):
    """session_completed doesn't depend on any entity's state (see
    device_trigger.py's module docstring) - offered whenever the device
    belongs to this integration at all."""
    entry, device = await _setup_device_and_entities(hass, ["has_fault"])

    triggers = await async_get_triggers(hass, device.id)

    session_completed = next(t for t in triggers if t["type"] == "session_completed")
    assert session_completed["platform"] == "device"
    assert session_completed["domain"] == DOMAIN
    assert session_completed["device_id"] == device.id
    assert "entity_id" not in session_completed


async def test_get_triggers_ignores_entities_from_other_integrations(hass):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry, device = await _setup_device_and_entities(hass, ["is_charging", "charging"])
    entity_registry = er.async_get(hass)
    other_entry = MockConfigEntry(domain="other_integration", data={})
    other_entry.add_to_hass(hass)
    entity_registry.async_get_or_create(
        "binary_sensor", "other_integration", "unrelated_unique_id",
        config_entry=other_entry, device_id=device.id,
    )

    triggers = await async_get_triggers(hass, device.id)

    assert {t["type"] for t in triggers} == {
        "charging_started", "charging_stopped", "session_completed",
    }


class TestAttachTrigger:
    async def test_charging_started_attaches_a_to_on_state_trigger(self, hass):
        with patch(
            "custom_components.foxess_charger.device_trigger.state_trigger.async_validate_trigger_config",
            AsyncMock(side_effect=lambda hass, cfg: cfg),
        ) as mock_validate, patch(
            "custom_components.foxess_charger.device_trigger.state_trigger.async_attach_trigger",
            AsyncMock(return_value=lambda: None),
        ) as mock_attach:
            await async_attach_trigger(
                hass,
                {"entity_id": "switch.foo_charging", "type": "charging_started"},
                action=AsyncMock(),
                trigger_info={},
            )

        validated_cfg = mock_validate.call_args.args[1]
        assert validated_cfg["platform"] == "state"
        assert validated_cfg["entity_id"] == "switch.foo_charging"
        assert validated_cfg["to"] == "on"
        mock_attach.assert_called_once()

    async def test_charging_stopped_attaches_a_to_off_state_trigger(self, hass):
        with patch(
            "custom_components.foxess_charger.device_trigger.state_trigger.async_validate_trigger_config",
            AsyncMock(side_effect=lambda hass, cfg: cfg),
        ) as mock_validate, patch(
            "custom_components.foxess_charger.device_trigger.state_trigger.async_attach_trigger",
            AsyncMock(return_value=lambda: None),
        ):
            await async_attach_trigger(
                hass,
                {"entity_id": "switch.foo_charging", "type": "charging_stopped"},
                action=AsyncMock(),
                trigger_info={},
            )

        validated_cfg = mock_validate.call_args.args[1]
        assert validated_cfg["to"] == "off"

    async def test_state_based_trigger_without_entity_id_is_rejected(self, hass):
        with pytest.raises(vol.Invalid):
            await async_attach_trigger(
                hass, {"type": "charging_started"}, action=AsyncMock(), trigger_info={},
            )


class TestSessionCompletedTrigger:
    """2026-09 (second audit): session_completed is keyed off
    EVENT_SESSION_COMPLETED (an internal event fired by
    FoxESSChargerCoordinator._track_session, see __init__.py/const.py) - not
    a state trigger at all. See device_trigger.py's module docstring for why
    a plain state trigger on Last Session Duration was wrong."""

    async def test_attaches_a_listener_for_the_session_completed_event(self, hass):
        from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED

        entry, device = await _setup_device_and_entities(hass, ["is_charging", "charging"])
        action = AsyncMock()

        remove = await async_attach_trigger(
            hass,
            {
                "device_id":  device.id,
                "domain":     DOMAIN,
                "platform":   "device",
                "type":       "session_completed",
            },
            action=action,
            trigger_info={"trigger_data": {"id": "0"}},
        )

        hass.bus.async_fire(EVENT_SESSION_COMPLETED, {"entry_id": entry.entry_id})
        await hass.async_block_till_done()

        action.assert_called_once()
        remove()

    async def test_ignores_events_for_a_different_device(self, hass):
        """A multi-charger install's event for device A must not fire
        device B's trigger."""
        from custom_components.foxess_charger.const import EVENT_SESSION_COMPLETED

        entry, device = await _setup_device_and_entities(hass, ["is_charging", "charging"])
        action = AsyncMock()

        remove = await async_attach_trigger(
            hass,
            {"device_id": device.id, "domain": DOMAIN, "platform": "device",
             "type": "session_completed"},
            action=action,
            trigger_info={"trigger_data": {"id": "0"}},
        )

        hass.bus.async_fire(EVENT_SESSION_COMPLETED, {"entry_id": "some_other_entry"})
        await hass.async_block_till_done()

        action.assert_not_called()
        remove()

    async def test_restoration_never_fires(self, hass):
        """No event = no fire. EVENT_SESSION_COMPLETED is only ever fired
        from _track_session()'s genuine active -> inactive transition
        branch - restoring persisted state (see __init__.py's
        async_load_session_state/_fetch()) never calls that code path at
        all, so there is nothing here to guard against beyond "don't fire
        without a matching event" - already covered by the two tests above.
        This test just documents the property explicitly: attaching the
        trigger by itself, with no event fired, must not call the action."""
        entry, device = await _setup_device_and_entities(hass, ["is_charging", "charging"])
        action = AsyncMock()

        remove = await async_attach_trigger(
            hass,
            {"device_id": device.id, "domain": DOMAIN, "platform": "device",
             "type": "session_completed"},
            action=action,
            trigger_info={"trigger_data": {"id": "0"}},
        )
        await hass.async_block_till_done()

        action.assert_not_called()
        remove()
