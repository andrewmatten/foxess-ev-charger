"""3.2 C19: a limit staged while paused shows confirmed once charging is
turned on and the charger holds it (not only after this entity's own call)."""
from __future__ import annotations

from test_ha_adapters import E, _call, setup  # noqa: F401 - fixture


async def test_staged_limit_reads_confirmed_after_turn_on(hass, setup):  # noqa: F811
    from fake_controller import FakeController

    h = await setup(FakeController(status=3))
    await _call(hass, "switch", "turn_off", f"switch.{E}_charging")
    await _call(hass, "number", "set_value", f"number.{E}_max_charging_power", value=2.5)
    assert hass.states.get(f"number.{E}_max_charging_power").attributes["confirmed"] is False
    await _call(hass, "switch", "turn_on", f"switch.{E}_charging")
    h.fake.hw["max_charging_power_raw"] = 25      # the charger now holds it
    await hass.data["foxess_charger"][h.entry.entry_id]["coordinator"].async_refresh()
    state = hass.states.get(f"number.{E}_max_charging_power")
    assert state.attributes["observed"] == 2.5
    assert state.attributes["confirmed"] is True
