"""C1: Fault 0xFFFFFFFF and alarm 0xFFFF sentinels are unknown (None) outside the controller."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.helpers import device_registry as dr

from custom_components.foxess_charger import protocol
from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS
from custom_components.foxess_charger.const import DOMAIN, REG_ALARM_CODE, REG_FAULT_CODE
from custom_components.foxess_charger.device_trigger import (
    async_attach_trigger,
    async_get_triggers,
)
from custom_components.foxess_charger.sensor import SENSORS, FoxESSChargerSensor

from fake_controller import FakeController
from ha_harness import Harness, make_entry
from test_protocol import RegisterMap

ENTRY_ID = "c1sentinelentry"


def _binary(key: str, data: dict) -> bool | None:
    return next(d for d in BINARY_SENSORS if d.key == key).value_fn(data)


def _sensor(key: str, data: dict) -> FoxESSChargerSensor:
    desc = next(d for d in SENSORS if d.key == key)
    entity = object.__new__(FoxESSChargerSensor)
    entity.entity_description = desc
    entity.coordinator = SimpleNamespace(data=data)
    return entity


async def test_protocol_normalizes_fault_and_alarm_sentinels_without_warnings(caplog):
    io = RegisterMap()
    io.regs[REG_ALARM_CODE] = 0xFFFF
    io.regs[REG_FAULT_CODE] = 0xFFFF
    io.regs[REG_FAULT_CODE + 1] = 0xFFFF

    with caplog.at_level(logging.WARNING):
        snap = await protocol.async_read_snapshot(io)

    assert snap["fault_code"] is None
    assert snap["alarm_code"] is None
    assert snap["active_faults"] == []
    assert snap["active_alarms"] == []
    assert "Unrecognized bit" not in caplog.text

    # Only one word 0xFFFF (0x0000FFFF) is a real bitmask, not the 32-bit sentinel.
    io.regs[REG_FAULT_CODE] = 0x0000
    io.regs[REG_FAULT_CODE + 1] = 0xFFFF
    snap2 = await protocol.async_read_snapshot(io)
    assert snap2["fault_code"] == 0xFFFF
    assert len(snap2["active_faults"]) == 16


def test_binary_sensors_treat_sentinels_as_unknown_unless_status_is_fault():
    assert _binary("has_fault", {"status": 3, "fault_code": 0xFFFFFFFF}) is None
    assert _binary("has_fault", {"status": None, "fault_code": 0xFFFFFFFF}) is None
    assert _binary("has_fault", {"status": 6, "fault_code": 0xFFFFFFFF}) is True
    assert _binary("has_alarm", {"alarm_code": 0xFFFF}) is None
    assert _binary("has_alarm", {"alarm_code": 1}) is True
    assert _binary("has_alarm", {"alarm_code": 0}) is False


def test_code_sensors_treat_sentinels_as_unknown():
    raw = {
        "alarm_code": 0xFFFF,
        "active_alarms": ["card_reader"],
        "fault_code": 0xFFFFFFFF,
        "active_faults": ["emergency_stop"],
    }
    alarm = _sensor("alarm_code", raw)
    fault = _sensor("fault_code", raw)
    assert alarm.native_value is None
    assert alarm.extra_state_attributes == {"active_alarms": []}
    assert fault.native_value is None
    assert fault.extra_state_attributes == {"active_faults": []}


async def test_sentinel_frame_does_not_fire_fault_or_alarm_device_triggers(
    hass, enable_custom_integrations
):
    fake = FakeController(status=3)
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, ENTRY_ID)})
        assert device is not None
        triggers = await async_get_triggers(hass, device.id)
        by_type = {t["type"]: t for t in triggers}

        fault_calls = AsyncMock()
        alarm_calls = AsyncMock()
        unsub_fault = await async_attach_trigger(
            hass, by_type["fault"], fault_calls, {"trigger_data": {"id": "f"}}
        )
        unsub_alarm = await async_attach_trigger(
            hass, by_type["alarm"], alarm_calls, {"trigger_data": {"id": "a"}}
        )
        try:
            coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
            fake.hw["fault_code"] = 0xFFFFFFFF
            fake.hw["alarm_code"] = 0xFFFF
            await coordinator.async_refresh()
            await hass.async_block_till_done()

            assert hass.states.get("binary_sensor.foxess_charger_fault").state == "unknown"
            assert hass.states.get("binary_sensor.foxess_charger_alarm").state == "unknown"
            fault_state = hass.states.get("sensor.foxess_charger_fault_code")
            alarm_state = hass.states.get("sensor.foxess_charger_alarm_code")
            assert fault_state.state == "unknown"
            assert fault_state.attributes["active_faults"] == []
            assert alarm_state.state == "unknown"
            assert alarm_state.attributes["active_alarms"] == []
            fault_calls.assert_not_called()
            alarm_calls.assert_not_called()

            # Genuine fault and alarm bits still fire their triggers.
            fake.hw["fault_code"] = 1
            fake.hw["alarm_code"] = 1
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            fault_calls.assert_called_once()
            alarm_calls.assert_called_once()
        finally:
            unsub_fault()
            unsub_alarm()
    finally:
        await harness.async_unload()
