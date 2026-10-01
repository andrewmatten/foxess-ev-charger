"""Per-block freshness: each register block (status/config/phase box) has
its own last-successful-read time, and FoxESSBlockAvailabilityMixin makes an
entity available only while its own block is fresh, on top of (not instead
of) the coordinator-wide last_update_success flag:

- one stale block + others fresh -> only that block's entities unavailable
- total connection loss (last_update_success=False) -> everything unavailable
- a stale block recovering -> its entities available again on the next
  successful read of it

Ported from the 2.4.3 coordinator tests to the rebuild's polling
coordinator, fed by a fake controller in the snapshot format.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger.const import (
    BLOCK_CONFIG,
    BLOCK_PHASE_BOX,
    BLOCK_STATUS,
)
from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator
from custom_components.foxess_charger.select import FoxESSWorkModeSelect
from custom_components.foxess_charger.switch import (
    FoxESSAutoPhaseSwitchSwitch,
    FoxESSChargingSwitch,
)

from fake_controller import FakeController


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


async def polled(hass, fake: FakeController | None = None, scan_interval: int = 10):
    """Coordinator after one poll. The fake's phase box is absent by default
    (single-phase hardware): a real, permanent per-block failure."""
    fake = fake or FakeController(status=1)
    coordinator = FoxESSChargerCoordinator(hass, fake, scan_interval)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    return coordinator, fake


class TestBlockIsFresh:
    def test_never_succeeded_block_is_not_fresh(self, hass):
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)
        assert coordinator.block_is_fresh(BLOCK_STATUS) is False

    def test_none_block_is_always_fresh(self, hass):
        coordinator = FoxESSChargerCoordinator(hass, MagicMock(), scan_interval=10)
        assert coordinator.block_is_fresh(None) is True

    async def test_successful_fetch_marks_status_and_config_fresh_but_not_phase_box(self, hass):
        coordinator, _ = await polled(hass)
        assert coordinator.block_is_fresh(BLOCK_STATUS) is True
        assert coordinator.block_is_fresh(BLOCK_CONFIG) is True
        assert coordinator.block_is_fresh(BLOCK_PHASE_BOX) is False

    async def test_stale_block_becomes_fresh_again_once_it_succeeds(self, hass):
        coordinator, fake = await polled(hass)
        assert coordinator.block_is_fresh(BLOCK_PHASE_BOX) is False
        # The phase-switch box starts answering (accessory connected, or a
        # transient fault cleared) while everything else behaves as before.
        fake.phase_box = {"auto_phase_switch": 1, "min_switch_interval": 5}
        await coordinator.async_refresh()
        assert coordinator.block_is_fresh(BLOCK_PHASE_BOX) is True

    async def test_threshold_scales_with_scan_interval(self, hass, monkeypatch):
        """Staleness threshold is BLOCK_STALENESS_FACTOR x scan_interval, not
        a hardcoded constant."""
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "custom_components.foxess_charger.coordinator.time.monotonic",
            lambda: clock["t"],
        )
        coordinator, _ = await polled(hass, scan_interval=60)   # success at t=0
        clock["t"] = 60 * 3 - 1
        assert coordinator.block_is_fresh(BLOCK_STATUS) is True
        clock["t"] = 60 * 3 + 1
        assert coordinator.block_is_fresh(BLOCK_STATUS) is False


class TestEntityAvailability:
    async def test_stale_block_only_disables_its_own_entities(self, hass):
        coordinator, _ = await polled(hass)
        charging_switch = FoxESSChargingSwitch(coordinator, make_entry())          # BLOCK_STATUS
        work_mode_select = FoxESSWorkModeSelect(coordinator, make_entry())         # BLOCK_CONFIG
        phase_switch = FoxESSAutoPhaseSwitchSwitch(coordinator, make_entry())      # BLOCK_PHASE_BOX
        assert charging_switch.available is True
        assert work_mode_select.available is True
        assert phase_switch.available is False

    async def test_total_connection_loss_disables_everything(self, hass):
        coordinator, fake = await polled(hass)
        charging_switch = FoxESSChargingSwitch(coordinator, make_entry())
        work_mode_select = FoxESSWorkModeSelect(coordinator, make_entry())
        # The status block is mandatory: failing it the configured number of
        # polls in a row makes the whole update fail.
        fake.fail_poll = 10
        for _ in range(3):
            await coordinator.async_refresh()
        assert coordinator.last_update_success is False
        assert charging_switch.available is False
        assert work_mode_select.available is False

    async def test_stale_block_recovering_makes_its_entities_available_again(self, hass):
        coordinator, fake = await polled(hass)
        phase_switch = FoxESSAutoPhaseSwitchSwitch(coordinator, make_entry())
        assert phase_switch.available is False
        fake.phase_box = {"auto_phase_switch": 1, "min_switch_interval": 5}
        await coordinator.async_refresh()
        assert phase_switch.available is True
