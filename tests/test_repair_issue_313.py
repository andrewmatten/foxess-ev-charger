"""A person is needed (hard latch): Home Assistant shows a Repairs issue."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_charger import coordinator as coordinator_module
from custom_components.foxess_charger.const import DOMAIN

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY = "needshumanentry1"
ISSUE = f"needs_human_{ENTRY}"


def issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)


async def test_issue_follows_needs_human_and_unload(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        assert issue(hass) is None
        fake.needs_human = True
        await coordinator.async_refresh()
        found = issue(hass)
        assert found is not None
        assert found.is_fixable is False
        assert found.severity == ir.IssueSeverity.ERROR
        assert found.translation_key == "needs_human"
        fake.needs_human = False
        await coordinator.async_refresh()
        assert issue(hass) is None
        fake.needs_human = True
        await coordinator.async_refresh()
        assert issue(hass) is not None
    finally:
        await h.async_unload()
    assert issue(hass) is None


async def test_no_repeated_registry_writes(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        with patch.object(coordinator_module.ir, "async_create_issue") as create, \
                patch.object(coordinator_module.ir, "async_delete_issue") as delete:
            for _ in range(3):
                await coordinator.async_refresh()
            assert (create.call_count, delete.call_count) == (0, 0)
            fake.needs_human = True
            for _ in range(3):
                await coordinator.async_refresh()
            assert (create.call_count, delete.call_count) == (1, 0)
            fake.needs_human = False
            for _ in range(3):
                await coordinator.async_refresh()
            assert (create.call_count, delete.call_count) == (1, 1)
    finally:
        await h.async_unload()


async def test_registry_failure_does_not_break_polling(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        fake.needs_human = True
        with patch.object(coordinator_module.ir, "async_create_issue", side_effect=RuntimeError):
            await coordinator.async_refresh()
        assert coordinator.last_update_success is True
    finally:
        await h.async_unload()
