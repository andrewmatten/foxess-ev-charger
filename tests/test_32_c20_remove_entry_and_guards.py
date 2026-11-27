"""3.2 C20: deleting the entry removes its storage; firmware and board
temperature sensors show unknown for a missing or sentinel reading."""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.foxess_charger import async_remove_entry
from custom_components.foxess_charger.persistence import (
    energy_key,
    session_key,
    setpoints_key,
    state_key,
)
from custom_components.foxess_charger.sensor import _board_celsius, _firmware
from test_32_n19_storage_remove_all import _populate


async def test_remove_entry_deletes_its_storage_only(hass, hass_storage):
    await _populate(hass, "c20_a")
    await _populate(hass, "c20_b")
    await async_remove_entry(hass, SimpleNamespace(entry_id="c20_a"))
    for key_fn in (state_key, session_key, setpoints_key, energy_key):
        assert key_fn("c20_a") not in hass_storage
        assert key_fn("c20_b") in hass_storage


def test_firmware_unknown_for_missing_or_sentinel():
    assert _firmware({}) is None
    assert _firmware({"software_version": None}) is None
    assert _firmware({"software_version": 0xFFFF}) is None
    assert _firmware({"software_version": 0x010A}) == "1.10"


def test_board_temperature_unknown_for_missing_or_sentinel():
    assert _board_celsius({}) is None
    assert _board_celsius({"ambient_temp_raw": None}) is None
    assert _board_celsius({"ambient_temp_raw": 65535}) is None
    assert _board_celsius({"ambient_temp_raw": 736}) == 23.6
