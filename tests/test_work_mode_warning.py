"""Only Plug&Charge works (the integration never sends 0x4001 Start), so a
charger in Controlled or Locked mode gets a specific enable error and a
Repairs issue instead of the generic "no active charging session"."""
from __future__ import annotations

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_charger.const import DOMAIN
from custom_components.foxess_charger.controller import ChargingController, ControlError

from fake_controller import FakeController
from ha_harness import Harness, make_entry
from test_controller import FakeClock, FakeIO, saved, snapshot

ENTRY = "workmodeentry01"
ISSUE = f"wrong_work_mode_{ENTRY}"
GENERIC = "no active charging session after enable"


def issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)


async def failed_enable(work_mode, *, poll=True):
    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    io.car_draw = False
    io.regs[0x3000] = work_mode

    async def read_snapshot(io_):
        return {**await snapshot(io_), "work_mode": io_.regs[0x3000]}

    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=read_snapshot)
    await c.async_initialize(saved(enabled=False), configure_safety=False)
    if poll:
        await c.async_poll()
    with pytest.raises(ControlError) as err:
        await c.async_enable()
    return str(err.value)


async def test_controlled_mode_gets_specific_message():
    msg = await failed_enable(0)
    assert "Controlled mode" in msg and "Plug&Charge" in msg


async def test_locked_mode_gets_its_own_message():
    msg = await failed_enable(2)
    assert "Locked" in msg and "Plug&Charge" in msg
    assert msg != await failed_enable(0)


async def test_plug_and_charge_keeps_generic_message():
    assert await failed_enable(1) == GENERIC


async def test_unknown_work_mode_keeps_generic_message():
    assert await failed_enable(0, poll=False) == GENERIC
    assert await failed_enable(7) == GENERIC


async def test_failed_config_read_keeps_last_known_mode():
    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    io.car_draw = False
    modes = iter([0, None])

    async def read_snapshot(io_):
        snap = await snapshot(io_)
        mode = next(modes)
        return snap if mode is None else {**snap, "work_mode": mode}

    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=read_snapshot)
    await c.async_initialize(saved(enabled=False), configure_safety=False)
    await c.async_poll()
    await c.async_poll()
    with pytest.raises(ControlError, match="Controlled mode"):
        await c.async_enable()


@pytest.mark.parametrize("mode", [0, 2])
async def test_issue_created_for_wrong_mode(hass, enable_custom_integrations, mode):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = mode
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        found = issue(hass)
        assert found is not None
        assert found.is_fixable is False
        assert found.severity == ir.IssueSeverity.WARNING
        assert found.translation_key == "wrong_work_mode"
    finally:
        await h.async_unload()


async def test_issue_cleared_when_switched_to_plug_and_charge(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = 0
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        assert issue(hass) is not None
        fake.hw["work_mode"] = 1
        await hass.data[DOMAIN][ENTRY]["coordinator"].async_refresh()
        assert issue(hass) is None
        fake.hw["work_mode"] = 2
        await hass.data[DOMAIN][ENTRY]["coordinator"].async_refresh()
        assert issue(hass) is not None
    finally:
        await h.async_unload()


async def test_no_issue_for_plug_and_charge(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = 1
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        assert issue(hass) is None
    finally:
        await h.async_unload()


async def test_failed_config_read_neither_creates_nor_clears(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = 0
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        fake.config_ok = False
        fake.hw["work_mode"] = 1
        await coordinator.async_refresh()
        assert issue(hass) is not None          # not cleared by a failed read
        fake.hw["work_mode"] = 1
        fake.config_ok = True
        await coordinator.async_refresh()
        assert issue(hass) is None
        fake.config_ok = False
        fake.hw["work_mode"] = 0
        await coordinator.async_refresh()
        assert issue(hass) is None              # not created by a failed read
        fake.config_ok = True
        await coordinator.async_refresh()
        assert issue(hass) is not None
    finally:
        await h.async_unload()


async def test_issue_only_touched_on_change(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = 0
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        ir.async_delete_issue(hass, DOMAIN, ISSUE)
        await hass.data[DOMAIN][ENTRY]["coordinator"].async_refresh()
        assert issue(hass) is None
    finally:
        await h.async_unload()


async def test_issue_removed_on_unload(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = 0
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    assert issue(hass) is not None
    await h.async_unload()
    assert issue(hass) is None


def test_issue_strings_exist():
    import json
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "custom_components", "foxess_charger", "translations", "en.json")
    with open(path, encoding="utf-8") as fh:
        strings = json.load(fh)["issues"]["wrong_work_mode"]
    assert strings["title"] and "{mode}" in strings["description"]


async def test_issue_mode_placeholder_follows_controlled_locked_change(hass, enable_custom_integrations):
    fake = FakeController(status=1)
    fake.hw["work_mode"] = 0
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        coordinator = hass.data[DOMAIN][ENTRY]["coordinator"]
        assert "controlled" in issue(hass).translation_placeholders["mode"].lower()
        fake.hw["work_mode"] = 2
        await coordinator.async_refresh()
        assert "locked" in issue(hass).translation_placeholders["mode"].lower()
        fake.hw["work_mode"] = 0
        await coordinator.async_refresh()
        assert "controlled" in issue(hass).translation_placeholders["mode"].lower()
    finally:
        await h.async_unload()
