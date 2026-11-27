"""3.2 R4-8: populate _max_power from poll snapshots (and enable fallback) if startup read failed."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import REG_MAX_POWER
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.transport import TransportError

from test_controller import FakeClock, FakeIO, saved
from test_rebuild_contract import rigs  # noqa: F401 - rigs is a fixture


async def test_poll_populates_max_power_after_startup_read_failure(rigs):
    """If the single 0x1011 read in async_initialize fails, the next poll's
    status snapshot must populate _max_power so async_set_power enforces the
    device ceiling."""
    rig = rigs(max_power_raw=73)
    real_read = rig.sim.read
    fail_once = [True]

    async def read(address: int, count: int):
        if address == REG_MAX_POWER and fail_once[0]:
            fail_once[0] = False
            raise TransportError("0x1011 timeout at startup")
        return await real_read(address, count)

    rig.sim.read = read
    ctl = await rig.boot(saved=saved(enabled=False, power=50))
    assert ctl._max_power is None

    await rig.run(ctl.async_poll())
    assert ctl._max_power == 73
    with pytest.raises(ValueError, match="above device maximum 73"):
        await rig.run(ctl.async_set_power(80))


async def test_poll_ignores_invalid_max_power_raw_and_keeps_valid_value():
    """Out-of-range or sentinel max_power_raw values in snapshots must not be
    accepted or overwrite an already-populated device maximum."""
    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    real_read = io.read

    async def read(address: int, count: int):
        if address == REG_MAX_POWER:
            raise TransportError("0x1011 timeout at startup")
        return await real_read(address, count)

    io.read = read
    snap_max = {"value": 65535}

    async def snap(_io) -> dict:
        return {
            "status": 1, "status_valid": True,
            "cc_status": 1, "cc_status_valid": True,
            "power_raw": 0, "lock_status": 0, "fault_code": 0,
            "max_power_raw": snap_max["value"],
        }

    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snap)
    await c.async_initialize(saved(enabled=False, power=50), configure_safety=False)
    assert c._max_power is None

    for bad in (0, 10, 331, 65535, True, None):
        snap_max["value"] = bad
        await c.async_poll()
        assert c._max_power is None

    snap_max["value"] = 220
    await c.async_poll()
    assert c._max_power == 220

    # Subsequent corrupt reading must not overwrite the populated ceiling.
    snap_max["value"] = 14
    await c.async_poll()
    assert c._max_power == 220

    with pytest.raises(ValueError, match="above device maximum 220"):
        await c.async_set_power(230)


async def test_enable_without_saved_power_caches_max_power_after_startup_failure(rigs):
    """When async_initialize missed 0x1011 and async_enable reads 0x1011 as its
    fallback power, it also caches _max_power."""
    rig = rigs(max_power_raw=73)
    real_read = rig.sim.read
    fail_once = [True]

    async def read(address: int, count: int):
        if address == REG_MAX_POWER and fail_once[0]:
            fail_once[0] = False
            raise TransportError("0x1011 timeout at startup")
        return await real_read(address, count)

    rig.sim.read = read
    ctl = await rig.boot(saved=saved(enabled=False, power=None))
    assert ctl._max_power is None

    res = await rig.run(ctl.async_enable())
    assert res.outcome == "confirmed"
    assert ctl._max_power == 73
    with pytest.raises(ValueError, match="above device maximum 73"):
        await rig.run(ctl.async_set_power(80))
