"""C3: Status and CC Status sensors decode from status_raw / cc_status_raw so
unrecognised register codes report the 'unknown' ('Unrecognized') enum state."""
from __future__ import annotations

import logging

import pytest

from custom_components.foxess_charger import protocol
from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.sensor import SENSORS

from fake_controller import FakeController
from ha_harness import Harness, make_entry
from test_protocol import RegisterMap

ENTRY_ID = "c3statusentry01"


def _value_fn(key: str):
    return next(d for d in SENSORS if d.key == key).value_fn


@pytest.mark.parametrize("raw", [7, 99, 0xFFFF])
async def test_status_sensor_decodes_unrecognized_raw_codes_from_snapshot(raw, caplog):
    io = RegisterMap()
    io.regs[0x1003] = raw
    snap = await protocol.async_read_snapshot(io)
    assert snap["status"] is None
    assert snap["status_raw"] == raw

    with caplog.at_level(logging.WARNING):
        state = _value_fn("status")(snap)

    assert state == "unknown"
    assert f"Unrecognized status raw value: {raw}" in caplog.text


@pytest.mark.parametrize("raw", [2, 99, 0xFFFF])
async def test_cc_status_sensor_decodes_unrecognized_raw_codes_from_snapshot(raw, caplog):
    io = RegisterMap()
    io.regs[0x1005] = raw
    snap = await protocol.async_read_snapshot(io)
    assert snap["cc_status"] is None
    assert snap["cc_status_raw"] == raw

    with caplog.at_level(logging.WARNING):
        state = _value_fn("cc_status")(snap)

    assert state == "unknown"
    assert f"Unrecognized cc_status raw value: {raw}" in caplog.text


def test_missing_reading_still_reports_none():
    assert _value_fn("status")({}) is None
    assert _value_fn("cc_status")({}) is None


async def test_status_and_cc_status_entities_report_unknown_option_and_survive_transient_drop(
    hass, enable_custom_integrations
):
    fake = FakeController(status=99, cc_status=2)
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        assert hass.states.get("sensor.foxess_charger_status").state == "unknown"
        assert hass.states.get("sensor.foxess_charger_cc_status").state == "unknown"

        # One dropped status block read preserves status_raw / cc_status_raw.
        fake.fail_poll = 1
        await hass.data[DOMAIN][ENTRY_ID]["coordinator"].async_refresh()
        assert hass.states.get("sensor.foxess_charger_status").state == "unknown"
        assert hass.states.get("sensor.foxess_charger_cc_status").state == "unknown"
    finally:
        await harness.async_unload()
