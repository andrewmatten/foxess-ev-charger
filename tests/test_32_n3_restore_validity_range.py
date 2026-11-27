"""3.2 N3: restored user Command Time Validity is restricted to 10-255 s."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.persistence import ChargerStorage, state_key

from tests.test_controller import FALLBACK, VALIDITY, FakeClock, FakeIO, saved, snapshot


async def _boot(state):
    clock = FakeClock()
    io = FakeIO(clock)
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(state, configure_safety=True)
    return c, io


@pytest.mark.parametrize("bad_validity", [0, 1, 9, 256, 3600])
async def test_restore_rejects_time_validity_outside_entity_range(bad_validity):
    c, io = await _boot(saved(user_time_validity_s=bad_validity))
    assert io.regs[VALIDITY] == 60
    assert c.export_state()["user_time_validity_s"] is None


@pytest.mark.parametrize("good_validity", [10, 30, 180, 255])
async def test_restore_accepts_time_validity_within_entity_range(good_validity):
    c, io = await _boot(saved(user_time_validity_s=good_validity))
    assert io.regs[VALIDITY] == good_validity
    assert c.export_state()["user_time_validity_s"] == good_validity


@pytest.mark.parametrize("raw, expected", [(59, 60), (60, 60), (320, 320), (321, 60)])
async def test_restore_default_current_stays_6_to_32_a(raw, expected):
    c, io = await _boot(saved(user_default_current_raw=raw))
    assert io.regs[FALLBACK] == expected
    saved_val = c.export_state()["user_default_current_raw"]
    assert saved_val == (raw if 60 <= raw <= 320 else None)


@pytest.mark.parametrize("bad_validity", [5, 300])
async def test_persistence_salvage_drops_out_of_range_time_validity(
    hass, hass_storage, bad_validity
):
    entry = f"n3entry_{bad_validity}"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()
    await storage.async_save_controller(
        saved(user_time_validity_s=bad_validity, user_default_current_raw=160)
    )
    hass_storage[state_key(entry)]["data"]["controller"]["enabled"] = "corrupt"

    result = await ChargerStorage(hass, entry).async_load()
    assert "user_time_validity_s" not in result.controller
    assert result.controller["user_default_current_raw"] == 160
