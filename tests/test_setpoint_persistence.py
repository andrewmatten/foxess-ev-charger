"""Tests for the P0 fix: desired_setpoints (the current/power limits the
integration re-asserts once charging starts, since the charger resets
0x3001/0x3002 to its maximum at every session boundary) surviving a HA
restart.

Before this fix, desired_setpoints was in-memory only - a restart forgot
any user-set limit, and the heartbeat/poll-driven re-assertion would then
have nothing to re-apply once the charger reset those registers to its own
maximum at the next session boundary. Same failure mode desired_setpoints
exists to prevent in the first place, just deferred to "after the next
restart" instead of "after the next session".

Mirrors tests/test_session_persistence.py's approach: the Store is mocked
(AsyncMock) rather than hitting real disk - these tests are about the
coordinator's own save/restore/validate logic, not HA core's Store
implementation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
)
from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber


def make_setpoints_store(loaded: dict | None = None) -> MagicMock:
    store = MagicMock()
    store.async_load = AsyncMock(return_value=loaded)
    store.async_save = AsyncMock()
    return store


class TestRestoringAfterRestart:
    async def test_restores_desired_setpoints_from_store(self, hass):
        store = make_setpoints_store({
            "desired_setpoints": {
                str(REG_MAX_CHARGING_CURRENT): 160,
                str(REG_MAX_CHARGING_POWER): 50,
            },
        })
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )

        await coordinator.async_load_desired_setpoints()

        # Restored keys must be real ints (JSON round-trips them as
        # strings), matching how every other write site keys this dict.
        assert coordinator.desired_setpoints == {
            REG_MAX_CHARGING_CURRENT: 160,
            REG_MAX_CHARGING_POWER: 50,
        }
        assert all(isinstance(k, int) for k in coordinator.desired_setpoints)

    async def test_no_stored_state_is_a_safe_no_op(self, hass):
        store = make_setpoints_store(loaded=None)
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )

        await coordinator.async_load_desired_setpoints()

        assert coordinator.desired_setpoints == {}

    async def test_no_store_configured_is_a_safe_no_op(self, hass):
        """setpoints_store=None (the default) - most existing tests
        construct the coordinator this way - persistence is simply
        skipped."""
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)

        await coordinator.async_load_desired_setpoints()  # must not raise
        await coordinator._async_persist_desired_setpoints()  # must not raise
        await coordinator.async_validate_desired_setpoints()  # must not raise

        assert coordinator.desired_setpoints == {}


class TestPersistingOnWrite:
    async def test_dirty_flag_triggers_a_save_on_the_next_update_cycle(self, hass):
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}
        coordinator._setpoints_dirty = True

        # _async_update_data() persists the coordinator's own desired_
        # setpoints dict when it's marked dirty; the actual _fetch() result
        # (which needs a working client) isn't the point of this test, so
        # stub it out directly rather than wiring up a full mock client.
        coordinator._fetch = MagicMock(return_value={})

        await coordinator._async_update_data()

        store.async_save.assert_called_once_with(
            {"desired_setpoints": {str(REG_MAX_CHARGING_CURRENT): 160}}
        )
        assert coordinator._setpoints_dirty is False

    async def test_not_dirty_does_not_trigger_a_save(self, hass):
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator._fetch = MagicMock(return_value={})

        await coordinator._async_update_data()

        store.async_save.assert_not_called()


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


class TestNumberEntityMarksDirty:
    async def test_setting_a_reasserted_register_marks_setpoints_dirty(self, hass):
        client = MagicMock()
        client.write_holding_register.return_value = True
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = {"max_charging_current_raw": 100}
        coordinator.async_request_refresh = AsyncMock()

        desc = next(d for d in NUMBERS if d.key == "max_charging_current")
        entity = FoxESSNumber(coordinator, client, desc, make_entry())
        entity.hass = hass
        entity.async_write_ha_state = MagicMock()

        assert coordinator._setpoints_dirty is False

        with patch("custom_components.foxess_charger.number.asyncio.sleep", AsyncMock()):
            await entity.async_set_native_value(16.0)  # raw=160

        assert coordinator._setpoints_dirty is True
        assert coordinator.desired_setpoints[REG_MAX_CHARGING_CURRENT] == 160


class TestValidatingAgainstDetectedCapabilities:
    """A value restored from storage may be stale relative to *this
    specific* charger (e.g. saved against a different/lower-capability
    unit) - it must be bounds-checked against the currently detected
    model's capabilities before being trusted, not just accepted because it
    round-tripped through the Store correctly."""

    async def test_in_range_restored_value_is_left_alone(self, hass):
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator.data = {"id_model_code": "A7300P1-E-B-WO", "default_current_raw": 100}
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 160}  # 16A, in 6-32A range

        await coordinator.async_validate_desired_setpoints()

        assert coordinator.desired_setpoints == {REG_MAX_CHARGING_CURRENT: 160}
        store.async_save.assert_not_called()

    async def test_out_of_range_current_falls_back_to_device_default_not_maximum(self, hass):
        """A7300 max current is 32A (raw 320) - 500 (50A) is out of range
        for this detected model. Must fall back to the charger's own
        tracked default (REG_DEFAULT_CURRENT/default_current_raw), never to
        the model's maximum - that's the exact "silently starts at full
        output" failure mode this whole feature exists to prevent."""
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator.data = {"id_model_code": "A7300P1-E-B-WO", "default_current_raw": 100}
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 500}

        await coordinator.async_validate_desired_setpoints()

        assert coordinator.desired_setpoints[REG_MAX_CHARGING_CURRENT] == 100
        store.async_save.assert_called_once()

    async def test_out_of_range_power_with_no_safe_default_is_discarded(self, hass):
        """REG_MAX_CHARGING_POWER has no equivalent "default" register in
        this protocol - an out-of-range value must be dropped rather than
        guessed at or silently pushed through as the model's maximum."""
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator.data = {"id_model_code": "A7300P1-E-B-WO", "default_current_raw": 100}
        # A7300 max power is 7.3kW (raw 73) - 999 is nonsense for this model.
        coordinator.desired_setpoints = {REG_MAX_CHARGING_POWER: 999}

        await coordinator.async_validate_desired_setpoints()

        assert REG_MAX_CHARGING_POWER not in coordinator.desired_setpoints
        store.async_save.assert_called_once()

    async def test_out_of_range_current_with_no_default_available_is_discarded(self, hass):
        """If the charger's own default_current_raw hasn't been read yet
        (or is itself somehow out of range), there is no safe fallback to
        use - discard rather than write something unverified."""
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator.data = {"id_model_code": "A7300P1-E-B-WO"}  # no default_current_raw yet
        coordinator.desired_setpoints = {REG_MAX_CHARGING_CURRENT: 500}

        await coordinator.async_validate_desired_setpoints()

        assert REG_MAX_CHARGING_CURRENT not in coordinator.desired_setpoints

    async def test_no_desired_setpoints_is_a_no_op(self, hass):
        store = make_setpoints_store()
        coordinator = FoxESSChargerCoordinator(
            hass, MagicMock(), scan_interval=10, setpoints_store=store,
        )
        coordinator.data = {"id_model_code": "A7300P1-E-B-WO"}

        await coordinator.async_validate_desired_setpoints()  # must not raise

        store.async_save.assert_not_called()
