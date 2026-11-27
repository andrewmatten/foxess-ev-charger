"""3.2 R2 (Opus review of the blueprint): a staged enable is disarmed as soon
as the car is gone, whatever the grid does; a refused turn-on is not retried
on every grid update."""
from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time

from test_solar_surplus_blueprint import (
    BLUEPRINT_SRC,
    T0,
    Harness,
    _setup,
    install_blueprint,  # noqa: F401
)


async def test_car_gone_with_staged_enable_is_disarmed_inside_the_band(
    hass, install_blueprint  # noqa: F811
):
    h = Harness(hass)
    await h.async_register()
    with freeze_time(T0) as frozen:
        h.set_connected(True)
        h.set_number(16)
        h.set_switch("off", intent_enabled=True)   # car finished / left, enable kept
        h.set_grid(100)                            # house battery: grid near 0 W
        await hass.async_block_till_done()
        await _setup(hass)
        h.set_connected(False)
        frozen.move_to(T0 + timedelta(seconds=30))
        h.set_grid(110)
        await hass.async_block_till_done()
    assert h.turn_offs >= 1
    assert hass.states.get("switch.charger_charging").attributes["intent_enabled"] is False
    assert h.turn_ons == 0


def test_refused_turn_on_is_throttled():
    """The turn-on continues on error and, if charging did not get enabled,
    the run holds for 60 s; with mode single the extra triggers are dropped
    (a behavioural test would need real time: HA waits for the delay)."""
    import yaml

    class _L(yaml.SafeLoader):
        pass

    _L.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))
    bp = yaml.load(BLUEPRINT_SRC.read_text(), Loader=_L)
    assert bp["mode"] == "single" and bp["max_exceeded"] == "silent"

    def walk(node):
        if isinstance(node, dict):
            yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    for node in walk(bp["action"]):
        seq = node.get("sequence")
        if not isinstance(seq, list):
            continue
        for i, step in enumerate(seq):
            if isinstance(step, dict) and step.get("action") == "switch.turn_on":
                assert step.get("continue_on_error") is True
                hold = seq[i + 1]
                assert "intent_enabled" in hold["if"][0]
                assert hold["then"][0]["delay"]["seconds"] >= 60
                return
    raise AssertionError("no switch.turn_on step found")
