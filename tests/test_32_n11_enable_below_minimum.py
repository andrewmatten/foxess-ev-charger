"""3.2 N11: async_enable() refuses when saved power_raw is below 1.4 kW."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.controller import ControlError
from test_controller import POW, make, saved


@pytest.mark.parametrize("low_power", [0, 5, 13])
async def test_enable_refuses_below_minimum_power(low_power):
    c, io = await make(saved(enabled=False, power=low_power), charging=False)
    before = c.export_state()

    with pytest.raises(
        ControlError, match="Max Charging Power is below 1.4 kW, charging cannot start"
    ):
        await c.async_enable()

    assert not c.intent_enabled
    assert c.export_state() == before
    assert io.writes == []


async def test_enable_at_exact_minimum_power_succeeds():
    c, io = await make(saved(enabled=False, power=14), charging=False)
    result = await c.async_enable()
    assert result.outcome == "confirmed"
    assert c.intent_enabled
    assert io.regs[POW] == 14
