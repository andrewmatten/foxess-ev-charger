"""3.2 R6-3 (Astra re-check): a saved power above the device's rating is
clamped to the rating on enable, never commanded as is."""
from __future__ import annotations

from dataclasses import replace

from test_controller import make, saved


async def test_saved_power_above_rating_is_clamped_on_enable():
    c, io = await make(saved(enabled=False, power=70), charging=True)
    # Saved 22 kW while the rating was unknown (startup read failed); the
    # 7.3 kW rating is learned later from a poll.
    c._intent = replace(c._intent, power_raw=220)
    c._max_power = 73
    await c.async_enable()
    assert io.power_writes() and max(io.power_writes()) <= 73
    assert c.desired_power_raw == 73
