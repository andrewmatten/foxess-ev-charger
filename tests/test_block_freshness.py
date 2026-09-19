"""Tests for the 2.2.0 per-block freshness change.

Before this, any one register block failing 3 polls in a row raised
UpdateFailed, and HA's CoordinatorEntity base class turned that into blanket
`available=False` for *every* entity in the integration - including ones
whose own block read fine (e.g. the status batch failing would also hide
the config-block-backed Work Mode select, and vice versa).

Now each block (BLOCK_STATUS/BLOCK_CONFIG/BLOCK_PHASE_BOX) tracks its own
last-successful-read timestamp, and FoxESSBlockAvailabilityMixin.available
checks the specific block an entity depends on, on top of (not instead of)
the coordinator-wide last_update_success flag - so:

- one stale block + others fresh -> only that block's entities unavailable
- total connection loss (last_update_success=False) -> everything
  unavailable, same as before
- a stale block recovering -> its entities become available again on the
  next successful read of it
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    BLOCK_CONFIG,
    BLOCK_PHASE_BOX,
    BLOCK_STATUS,
)
from custom_components.foxess_charger.select import FoxESSWorkModeSelect
from custom_components.foxess_charger.switch import (
    FoxESSAutoPhaseSwitchSwitch,
    FoxESSChargingSwitch,
)
from tests.test_coordinator_batching import make_mock_client


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


class TestBlockIsFresh:
    def test_never_succeeded_block_is_not_fresh(self, hass):
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)
        assert coordinator.block_is_fresh(BLOCK_STATUS) is False

    def test_none_block_is_always_fresh(self, hass):
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)
        assert coordinator.block_is_fresh(None) is True

    def test_successful_fetch_marks_status_and_config_fresh_but_not_phase_box(self, hass):
        # make_mock_client's phase-switch-box read always returns None -
        # "expected to fail on single-phase hardware" (see
        # test_coordinator_batching.py) - a real, permanent per-block
        # failure, distinct from a transient one.
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)

        coordinator._fetch()

        assert coordinator.block_is_fresh(BLOCK_STATUS) is True
        assert coordinator.block_is_fresh(BLOCK_CONFIG) is True
        assert coordinator.block_is_fresh(BLOCK_PHASE_BOX) is False

    def test_stale_block_becomes_fresh_again_once_it_succeeds(self, hass):
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator._fetch()
        assert coordinator.block_is_fresh(BLOCK_PHASE_BOX) is False

        # Simulate the phase-switch-box block starting to answer (e.g. the
        # accessory was connected, or a transient fault cleared) while
        # everything else behaves as before.
        base_side_effect = client.read_registers.side_effect

        def _read_registers(address, count, quiet=False):
            if (address, count) == (0x300A, 2):
                return [1, 5]
            return base_side_effect(address, count, quiet=quiet)

        client.read_registers.side_effect = _read_registers
        coordinator._fetch()

        assert coordinator.block_is_fresh(BLOCK_PHASE_BOX) is True

    def test_threshold_scales_with_scan_interval(self, hass, monkeypatch):
        """Staleness threshold is BLOCK_STALENESS_FACTOR x scan_interval, not
        a hardcoded constant - a longer scan interval must not make an
        entity flap unavailable between its own normal polls."""
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=60)
        coordinator._fetch()

        clock = {"t": 0.0}
        monkeypatch.setattr(
            "custom_components.foxess_charger.time.monotonic", lambda: clock["t"]
        )
        # Re-mark success at t=0 explicitly (fetch above used the real clock).
        coordinator._mark_block_success(BLOCK_STATUS)

        clock["t"] = 60 * 3 - 1  # just under 3x the 60s scan interval
        assert coordinator.block_is_fresh(BLOCK_STATUS) is True

        clock["t"] = 60 * 3 + 1  # just past it
        assert coordinator.block_is_fresh(BLOCK_STATUS) is False


class TestEntityAvailability:
    """Entity-level check: FoxESSBlockAvailabilityMixin combines the
    coordinator-wide flag with the entity's own block freshness."""

    def test_stale_block_only_disables_its_own_entities(self, hass):
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = coordinator._fetch()
        # Sanity: this scenario's whole point is one fresh, one stale block.
        assert coordinator.last_update_success is True

        charging_switch = FoxESSChargingSwitch(coordinator, client, make_entry())  # BLOCK_STATUS
        work_mode_select = FoxESSWorkModeSelect(coordinator, client, make_entry())  # BLOCK_CONFIG
        phase_switch = FoxESSAutoPhaseSwitchSwitch(coordinator, client, make_entry())  # BLOCK_PHASE_BOX

        assert charging_switch.available is True
        assert work_mode_select.available is True
        assert phase_switch.available is False

    def test_total_connection_loss_disables_everything(self, hass):
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = coordinator._fetch()
        # Simulate what _async_update_data does when the connection itself
        # is down: UpdateFailed propagates up through the base class, which
        # sets this to False.
        coordinator.last_update_success = False

        charging_switch = FoxESSChargingSwitch(coordinator, client, make_entry())
        work_mode_select = FoxESSWorkModeSelect(coordinator, client, make_entry())

        assert charging_switch.available is False
        assert work_mode_select.available is False

    def test_stale_block_recovering_makes_its_entities_available_again(self, hass):
        client = make_mock_client()
        coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)
        coordinator.data = coordinator._fetch()

        phase_switch = FoxESSAutoPhaseSwitchSwitch(coordinator, client, make_entry())
        assert phase_switch.available is False

        base_side_effect = client.read_registers.side_effect

        def _read_registers(address, count, quiet=False):
            if (address, count) == (0x300A, 2):
                return [1, 5]
            return base_side_effect(address, count, quiet=quiet)

        client.read_registers.side_effect = _read_registers
        coordinator.data = coordinator._fetch()

        assert phase_switch.available is True
