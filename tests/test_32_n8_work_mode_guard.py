"""3.2 N8: Work Mode write requires a valid unplugged frame and updates _work_mode."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import REG_WORK_MODE
from custom_components.foxess_charger.controller import ControlError
from custom_components.foxess_charger.transport import TransportError

from test_controller import CC, make, saved


async def test_work_mode_write_refused_when_status_unreadable():
    c, io = await make(saved(enabled=False), charging=False)
    io.regs[CC] = 0
    real_read = io.read

    async def fail_status(address, count):
        if address == 0x1000:
            raise TransportError("status unreadable")
        return await real_read(address, count)

    io.read = fail_status
    with pytest.raises(ControlError, match="no car is connected"):
        await c.async_set_register(REG_WORK_MODE, 1)
    assert not [w for w in io.writes if w[1] == REG_WORK_MODE]


@pytest.mark.parametrize(
    "overrides",
    [
        {"cc_override": 65535},
        {"status_override": 65535},
        {"fault_unknown": True},
    ],
)
async def test_work_mode_write_refused_on_invalid_frame(overrides):
    c, io = await make(saved(enabled=False), charging=False)
    io.regs[CC] = 0
    if "cc_override" in overrides:
        io.cc_override = overrides["cc_override"]
    if "status_override" in overrides:
        io.status_override = overrides["status_override"]
    if overrides.get("fault_unknown"):
        io.regs[0x101A] = io.regs[0x101B] = 0xFFFF

    with pytest.raises(ControlError, match="no car is connected"):
        await c.async_set_register(REG_WORK_MODE, 1)
    assert not [w for w in io.writes if w[1] == REG_WORK_MODE]


async def test_work_mode_write_updates_cached_work_mode():
    c, io = await make(saved(enabled=False), charging=False)
    io.regs[CC] = 0
    io.regs[REG_WORK_MODE] = 1
    c._work_mode = 1

    res = await c.async_set_register(REG_WORK_MODE, 0)
    assert res.outcome == "confirmed"
    assert io.regs[REG_WORK_MODE] == 0
    assert c._work_mode == 0

    res = await c.async_set_register(REG_WORK_MODE, 1)
    assert res.outcome == "confirmed"
    assert io.regs[REG_WORK_MODE] == 1
    assert c._work_mode == 1
