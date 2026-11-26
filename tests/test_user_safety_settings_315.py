"""hacs/default#14564 review: setup must not reset Command Time Validity
(0x3005) or Default Current (0x3006) that the user chose through the number
entities. The values the user set are saved and re-asserted on every setup;
60 s / 6 A are written only when the user never set one."""
from __future__ import annotations

from custom_components.foxess_charger.controller import ChargingController
from tests.test_controller import FALLBACK, VALIDITY, FakeClock, FakeIO, saved, snapshot


async def _boot(state, io=None):
    clock = FakeClock()
    io = io or FakeIO(clock)
    io.clock = clock
    saves = []

    async def persist(data):
        saves.append(data)

    c = ChargingController(io, clock=clock, sleep=clock.sleep,
                           read_snapshot=snapshot, persist=persist)
    await c.async_initialize(state, configure_safety=True)
    return c, io, saves


async def test_user_default_current_survives_restart():
    c, io, saves = await _boot(saved())
    assert io.regs[FALLBACK] == 60
    await c.async_set_register(FALLBACK, 160)          # user picks 16 A
    assert io.regs[FALLBACK] == 160
    state = saves[-1]
    io.regs[FALLBACK] = 160
    c2, io2, _ = await _boot(state, io)                # HA restart / reload
    assert io2.regs[FALLBACK] == 160                   # not put back to 6 A
    assert c2.phase == "enabled"


async def test_user_time_validity_survives_restart():
    c, io, saves = await _boot(saved())
    await c.async_set_register(VALIDITY, 30)
    c2, io2, _ = await _boot(saves[-1], io)
    assert io2.regs[VALIDITY] == 30
    assert c2.diagnostics["refresh_interval"] == 10


async def test_never_set_still_gets_safe_defaults():
    clock = FakeClock()
    io = FakeIO(clock)
    io.regs[FALLBACK], io.regs[VALIDITY] = 320, 180     # factory / app values
    c, io, _ = await _boot(saved(), io)
    assert io.regs[FALLBACK] == 60 and io.regs[VALIDITY] == 60


async def test_malformed_saved_settings_fall_back_to_safe_defaults():
    state = saved(user_default_current_raw=9999, user_time_validity_s="x")
    c, io, _ = await _boot(state)
    assert io.regs[FALLBACK] == 60 and io.regs[VALIDITY] == 60
    assert c.phase == "enabled"


async def test_first_install_writes_safe_defaults():
    c, io, saves = await _boot(None)
    assert io.regs[FALLBACK] == 60 and io.regs[VALIDITY] == 60
