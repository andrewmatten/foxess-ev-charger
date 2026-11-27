"""C18: Failed switch and number commands request a coordinator refresh before
re-raising HomeAssistantError, and FoxESSChargingSwitch.async_turn_on reads
controller.needs_human live rather than possibly stale coordinator.data."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_charger.adapter_api import ControlError
from custom_components.foxess_charger.const import DOMAIN

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "c18refresh01"


class _LatchOnErrorController(FakeController):
    """Simulates a command failing because the controller observed and latched a fault."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.last_clear_hard: bool | None = None

    async def async_enable(self, *, clear_hard: bool = False):
        self.last_clear_hard = clear_hard
        if self.fail_next == "enable":
            self.fail_next = None
            self.safety_latched = True
            self.latch_cause = "hard"
            self.needs_human = True
            self.phase = "faulted"
            raise ControlError("hard fault latched during enable (fake)")
        if self.needs_human and not clear_hard:
            raise ControlError("needs a person (fake)")
        self.safety_latched = False
        self.latch_cause = None
        self.needs_human = False
        self.phase = "enabled"
        return await super().async_enable(clear_hard=clear_hard)

    async def async_set_power(self, raw: int):
        if self.fail_next == "set_power":
            self.fail_next = None
            self.safety_latched = True
            self.latch_cause = "comms"
            self.phase = "faulted"
            raise ControlError("comms latch during set_power (fake)")
        return await super().async_set_power(raw)


async def test_failed_switch_command_requests_refresh_and_publishes_new_latch(
    hass, enable_custom_integrations
):
    fake = _LatchOnErrorController(status=1)
    fake._enabled = False
    fake.phase = "paused"
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
        assert coordinator.data["needs_human"] is False
        fake.fail_next = "enable"

        with patch.object(
            coordinator,
            "async_request_refresh",
            wraps=coordinator.async_request_refresh,
        ) as spy_refresh:
            with pytest.raises(HomeAssistantError, match="failed to start charging"):
                await hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": "switch.foxess_charger_charging"},
                    blocking=True,
                )
            assert spy_refresh.await_count == 1

        await hass.async_block_till_done()
        assert coordinator.data["needs_human"] is True
        assert (
            hass.states.get("sensor.foxess_charger_charging_status_reason").state
            == "needs_attention"
        )
        assert (
            ir.async_get(hass).async_get_issue(DOMAIN, f"needs_human_{ENTRY_ID}")
            is not None
        )
    finally:
        await harness.async_unload()


async def test_failed_number_command_requests_refresh_before_reraising(
    hass, enable_custom_integrations
):
    fake = _LatchOnErrorController(status=3)
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
        fake.fail_next = "set_power"

        with patch.object(
            coordinator,
            "async_request_refresh",
            wraps=coordinator.async_request_refresh,
        ) as spy_refresh:
            with pytest.raises(
                HomeAssistantError, match="could not set max_charging_power"
            ):
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {
                        "entity_id": "number.foxess_charger_max_charging_power",
                        "value": 3.5,
                    },
                    blocking=True,
                )
            assert spy_refresh.await_count == 1

        await hass.async_block_till_done()
        assert coordinator.data["latch_cause"] == "comms"
        assert (
            hass.states.get("sensor.foxess_charger_charging_status_reason").state
            == "link_problem"
        )

        # Lock/other switch failure also requests a coordinator refresh.
        fake.fail_next = "set_register"
        mock_req = AsyncMock()
        with patch.object(coordinator, "async_request_refresh", mock_req):
            with pytest.raises(HomeAssistantError, match="failed to lock"):
                await hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": "switch.foxess_charger_lock"},
                    blocking=True,
                )
            assert mock_req.await_count == 1
    finally:
        await harness.async_unload()


async def test_turn_on_reads_needs_human_live_from_controller_when_coordinator_data_stale(
    hass, enable_custom_integrations, hass_admin_user
):
    fake = _LatchOnErrorController(status=4)
    fake._enabled = False
    fake.phase = "paused"
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY_ID]["coordinator"]
        assert coordinator.data["needs_human"] is False

        # Background controller refresh latches hard between coordinator polls:
        # coordinator.data["needs_human"] is still False, while controller.needs_human is True.
        fake.safety_latched = True
        fake.latch_cause = "hard"
        fake.needs_human = True
        fake.phase = "faulted"

        # 1. Automation (no user_id) is still refused.
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": "switch.foxess_charger_charging"},
                blocking=True,
                context=Context(),
            )
        assert fake.last_clear_hard is False

        # Reset coordinator.data["needs_human"] to stale False to isolate the live read check.
        coordinator.data["needs_human"] = False

        # 2. A human user turn-on succeeds even though coordinator.data was stale.
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.foxess_charger_charging"},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
        assert fake.last_clear_hard is True
        assert hass.states.get("switch.foxess_charger_charging").state == "on"
    finally:
        await harness.async_unload()
