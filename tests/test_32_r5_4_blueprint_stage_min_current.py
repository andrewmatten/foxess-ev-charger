"""3.2 R5-4: solar blueprint stages entity_min on turn-on unless desired already equals it."""
from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time

from test_solar_surplus_blueprint import (
    NUMBER,
    T0,
    Harness,
    _setup,
    install_blueprint,  # noqa: F401
)


async def test_turn_on_from_off_stages_min_when_display_is_6a_but_desired_is_none(
    hass, install_blueprint  # noqa: F811
):
    """When no current is saved (desired is None) and the hardware happens to
    read 6 A, the blueprint must still call number.set_value(6.0) before
    switch.turn_on so explicit enable does not write rated current (e.g. 32 A)."""
    order: list[str] = []
    h = Harness(hass)

    async def _set_value(call):
        value = float(call.data["value"])
        order.append("number")
        h.number_writes.append(value)
        hass.states.async_set(
            NUMBER,
            str(value),
            {"min": 6, "max": 32, "step": 0.1, "desired": value, "observed": value},
        )

    async def _turn_on(call):
        order.append("switch_on")
        h.turn_ons += 1
        h.set_switch("on", intent_enabled=True)

    async def _turn_off(call):
        order.append("switch_off")
        h.turn_offs += 1
        h.set_switch("off", intent_enabled=False)

    hass.services.async_register("number", "set_value", _set_value)
    hass.services.async_register("switch", "turn_on", _turn_on)
    hass.services.async_register("switch", "turn_off", _turn_off)

    with freeze_time(T0) as frozen:
        hass.states.async_set(
            NUMBER,
            "6.0",
            {"min": 6, "max": 32, "step": 0.1, "desired": None, "observed": 6.0},
        )
        h.set_switch("off")
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-3001)
        await hass.async_block_till_done()

    assert order == ["number", "switch_on"]
    assert h.number_writes == [6.0]
    assert h.turn_ons == 1


async def test_turn_on_from_off_skips_number_write_when_desired_already_equals_min(
    hass, install_blueprint  # noqa: F811
):
    """When desired is already saved at entity_min, no redundant number.set_value
    is needed before switch.turn_on."""
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        hass.states.async_set(
            NUMBER,
            "6.0",
            {"min": 6, "max": 32, "step": 0.1, "desired": 6.0, "observed": 6.0},
        )
        h.set_switch("off")
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-3001)
        await hass.async_block_till_done()

    assert h.number_writes == []
    assert h.turn_ons == 1


async def test_turn_on_from_off_stages_min_when_desired_differs_from_min(
    hass, install_blueprint  # noqa: F811
):
    """When desired is saved above entity_min, number.set_value(entity_min) is
    called before switch.turn_on."""
    h = Harness(hass)
    await h.async_register()

    with freeze_time(T0) as frozen:
        hass.states.async_set(
            NUMBER,
            "6.0",
            {"min": 6, "max": 32, "step": 0.1, "desired": 12.0, "observed": 6.0},
        )
        h.set_switch("off")
        h.set_grid(-3000)
        await hass.async_block_till_done()
        await _setup(hass)

        frozen.move_to(T0 + timedelta(minutes=3))
        h.set_grid(-3001)
        await hass.async_block_till_done()

    assert h.number_writes == [6.0]
    assert h.turn_ons == 1
