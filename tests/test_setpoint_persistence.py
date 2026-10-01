"""Saved charging limits (and the legacy stop intent) surviving a restart.

Ported from the 2.4.3 coordinator tests (``desired_setpoints`` Store) to the
rebuild: ChargerStorage imports the legacy ``_setpoints``/``_session`` files
into the controller's saved state and keeps writing them in the 2.4.3 shape;
the number entities stage/persist through the controller.

Deliberate change (SPEC.md 6.1): an out-of-range restored limit is dropped
and restoration becomes a protective pause; it is never replaced by the
device's default current (or its maximum).
"""
from __future__ import annotations

import pytest
from homeassistant.helpers.storage import Store

from custom_components.foxess_charger.const import (
    DOMAIN, REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER,
)
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.persistence import (
    ChargerStorage, session_key, setpoints_key, state_key,
)

from fake_controller import FakeController
from ha_harness import Harness, make_entry
from rebuild_simulator import CONNECTED, SimCharger

ENTRY = "setpointentry01"
CUR, POW = str(REG_MAX_CHARGING_CURRENT), str(REG_MAX_CHARGING_POWER)


def put(hass_storage, key, data) -> None:
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


def legacy_session(**overrides) -> dict:
    base = {"session_start_wall": None, "session_start_total": None, "last_session": None,
            "prev_status": 1, "stop_inhibit": False, "stop_pending": False}
    base.update(overrides)
    return base


async def load(hass, hass_storage, setpoints=None, session=None):
    if session is not None:
        put(hass_storage, session_key(ENTRY), session)
    if setpoints is not None:
        put(hass_storage, setpoints_key(ENTRY), setpoints)
    return await ChargerStorage(hass, ENTRY).async_load()


class TestRestoringAfterRestart:
    async def test_restores_desired_setpoints_from_store(self, hass, hass_storage):
        # JSON round-trips register keys as strings; restored values are ints.
        result = await load(hass, hass_storage, {"desired_setpoints": {CUR: 160, POW: 50}},
                            legacy_session())
        assert result.controller["current_raw"] == 160
        assert result.controller["power_raw"] == 50
        assert type(result.controller["current_raw"]) is int
        assert type(result.controller["power_raw"]) is int

    async def test_no_stored_state_is_a_safe_no_op(self, hass, hass_storage):
        result = await load(hass, hass_storage)
        assert result.controller is None       # first install: nothing restored
        assert result.issues == []

    async def test_no_store_configured_is_a_safe_no_op(self):
        """A controller without a persist callback still stages limits."""
        sim = SimCharger(state=CONNECTED)
        ctl = ChargingController(sim, persist=None)
        await ctl.async_initialize(None, configure_safety=False)
        assert (await ctl.async_set_current(160)).outcome == "staged"
        assert ctl.desired_current_raw == 160
        await ctl.async_close()


class TestPersistingOnWrite:
    async def test_dirty_flag_triggers_a_save_on_the_next_update_cycle(
        self, hass, hass_storage, enable_custom_integrations,
    ):
        """A user limit is written to the legacy setpoints file (2.4.3
        shape) as part of the command, before any charger write."""
        h = Harness(hass, make_entry(ENTRY), FakeController(status=1))
        assert await h.async_setup()
        try:
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": "number.foxess_charger_max_charging_current", "value": 16.0},
                blocking=True,
            )
            assert hass_storage[setpoints_key(ENTRY)]["data"] == {
                "desired_setpoints": {CUR: 160},
            }
            assert hass_storage[state_key(ENTRY)]["data"]["controller"]["current_raw"] == 160
        finally:
            await h.async_unload()

    async def test_not_dirty_does_not_trigger_a_save(self, hass, hass_storage, monkeypatch):
        saves: list[str] = []
        real = Store.async_save

        async def _counting(self, data):
            saves.append(self.key)
            await real(self, data)

        monkeypatch.setattr(Store, "async_save", _counting)
        storage = ChargerStorage(hass, ENTRY)
        await storage.async_load()
        state = {"schema": 1, "enabled": True, "power_raw": 22, "current_raw": 100,
                 "revision": 1, "safety_latched": False, "stop_fallback": False}
        await storage.async_save_controller(state)
        first = list(saves)
        assert first                                   # something was written
        await storage.async_save_controller(dict(state))
        assert saves == first                          # unchanged: nothing rewritten


class TestNumberEntityMarksDirty:
    async def test_setting_a_reasserted_register_marks_setpoints_dirty(
        self, hass, hass_storage, enable_custom_integrations,
    ):
        fake = FakeController(status=3)
        h = Harness(hass, make_entry(ENTRY), fake)
        assert await h.async_setup()
        try:
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": "number.foxess_charger_max_charging_current", "value": 16.0},
                blocking=True,
            )
            assert ("set_current", 160) in fake.calls
            assert hass.data[DOMAIN][ENTRY]["controller"].desired_current_raw == 160
            assert hass_storage[setpoints_key(ENTRY)]["data"]["desired_setpoints"][CUR] == 160
        finally:
            await h.async_unload()


class TestValidatingAgainstDetectedCapabilities:
    async def test_in_range_restored_value_is_left_alone(self, hass, hass_storage):
        result = await load(hass, hass_storage, {"desired_setpoints": {CUR: 160}}, legacy_session(prev_status=3))
        assert result.controller["current_raw"] == 160
        assert result.controller["enabled"] is True
        assert result.issues == []

    async def test_out_of_range_current_falls_back_to_device_default_not_maximum(
        self, hass, hass_storage,
    ):
        # SPEC.md 6.1: dropped and protective, never the maximum (nor a guess).
        result = await load(hass, hass_storage, {"desired_setpoints": {CUR: 500}}, legacy_session())
        assert result.controller["current_raw"] is None
        assert result.controller["enabled"] is False
        assert "legacy_setpoints_malformed" in result.issues

    async def test_out_of_range_power_with_no_safe_default_is_discarded(self, hass, hass_storage):
        result = await load(hass, hass_storage, {"desired_setpoints": {POW: 999}}, legacy_session())
        assert result.controller["power_raw"] is None
        assert result.controller["enabled"] is False
        assert "legacy_setpoints_malformed" in result.issues

    async def test_out_of_range_current_with_no_default_available_is_discarded(
        self, hass, hass_storage,
    ):
        result = await load(hass, hass_storage, {"desired_setpoints": {CUR: 500, POW: 30}},
                            legacy_session())
        assert result.controller["current_raw"] is None
        assert result.controller["power_raw"] == 30   # the valid sibling survives
        assert result.controller["enabled"] is False

    async def test_no_desired_setpoints_is_a_no_op(self, hass, hass_storage):
        result = await load(hass, hass_storage, {"desired_setpoints": {}}, legacy_session(prev_status=3))
        assert result.controller["power_raw"] is None
        assert result.controller["current_raw"] is None
        assert result.controller["enabled"] is True
        assert result.issues == []


# ported from test_realistic_charger::test_malformed_restored_setpoint_is_dropped
@pytest.mark.parametrize("bad", ["73", None, 7.3, True, [73]])
async def test_malformed_restored_setpoint_is_dropped(hass, hass_storage, bad):
    result = await load(hass, hass_storage, {"desired_setpoints": {POW: bad}}, legacy_session())
    assert result.controller["power_raw"] is None
    if bad is not None:
        # SPEC.md 6.1: malformed restoration is protective, not ignored.
        assert result.controller["enabled"] is False
        assert "legacy_setpoints_malformed" in result.issues


class TestLegacyStopIntent:
    # ported from test_stop_inhibit::TestStopInhibitPersistsAcrossARestart::
    #             test_absent_key_defaults_to_not_inhibited
    async def test_absent_key_defaults_to_not_inhibited(self, hass, hass_storage):
        result = await load(hass, hass_storage, session={"prev_status": 3})
        assert result.controller["enabled"] is True
        assert result.controller["stop_fallback"] is False
        assert result.issues == []
