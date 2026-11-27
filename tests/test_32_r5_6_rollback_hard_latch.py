"""3.2 R5-6: saved 3.2 state stays rollback-safe so 3.1.4 never loses a hard latch."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys

import pytest

from custom_components.foxess_charger.const import SAVED_POWER_RAW_MAX
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.persistence import ChargerStorage, valid_controller_state
from test_comms_latch_release import FAULT_LO
from test_controller import CC, POW, FakeClock, FakeIO, saved, snapshot, spin
from test_fault_and_stop_fallback import drain

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_314 = "ad777ab"


def _import_release_314_persistence(tmp_path):
    """Materialise 3.1.4 (ad777ab) persistence.py from git as package `foxess_314`."""
    try:
        check = subprocess.run(
            ["git", "-C", REPO, "cat-file", "-e", f"{RELEASE_314}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if check.returncode != 0:
        return None
    root = tmp_path / "custom_components"
    pkg = root / "foxess_314"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name in ("energy_guard.py", "const.py", "persistence.py"):
        source = subprocess.run(
            ["git", "-C", REPO, "show", f"{RELEASE_314}:custom_components/foxess_charger/{name}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        (pkg / name).write_text(source, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("foxess_314.persistence")
    finally:
        sys.path.remove(str(root))


async def test_22kw_unit_above_220_preserves_hard_latch_on_rollback_to_314(
    hass, hass_storage, tmp_path
):
    """A 22 kW unit reporting 0x1011=225 that enables and later hits a hard
    fault must save a power_raw <= 220 so rolling back to 3.1.4 keeps
    safety_latched=True and latch_cause='hard' instead of wiping the latch."""
    entry = "r5_6_rollback"
    storage = ChargerStorage(hass, entry)
    await storage.async_load()

    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    io.regs[0x1011] = 225
    io.regs[POW] = 225
    io._physics()

    c = ChargingController(
        io,
        clock=clock,
        sleep=clock.sleep,
        read_snapshot=snapshot,
        persist=storage.async_save_controller,
    )
    await c.async_initialize(saved(enabled=False, power=None), configure_safety=False)
    io.writes.clear()

    res = await c.async_enable()
    assert res.outcome == "confirmed"
    assert c.desired_power_raw == 220

    io.regs[FAULT_LO] = 1 << 6  # leakage current -> hard latch
    await c.async_poll()
    await drain(c)
    assert c.needs_human and c.latch_cause == "hard"
    assert c.export_state()["power_raw"] <= 220

    # Round-trip all stores through JSON as a real HA restart does.
    for key, value in list(hass_storage.items()):
        hass_storage[key] = json.loads(json.dumps(value))

    old_persistence = _import_release_314_persistence(tmp_path)
    if old_persistence is not None:
        try:
            loaded_314 = await old_persistence.ChargerStorage(hass, entry).async_load()
            assert loaded_314.issues == []
            assert loaded_314.controller["power_raw"] == 220
            assert loaded_314.controller["safety_latched"] is True
            assert loaded_314.controller["latch_cause"] == "hard"
        finally:
            for mod in [m for m in sys.modules if m == "foxess_314" or m.startswith("foxess_314.")]:
                sys.modules.pop(mod, None)


async def test_first_install_and_replug_adoption_clamp_above_220_to_220():
    """Adopting a running 22 kW session whose 0x1011/0x3002 reads 225 clamps
    saved power_raw to 220 (rollback-safe, never raises the cap)."""
    # First-install adoption
    clock1 = FakeClock()
    io1 = FakeIO(clock1, charging=True)
    io1.regs[0x1011] = 225
    io1.regs[POW] = 225
    io1._physics()
    c1 = ChargingController(io1, clock=clock1, sleep=clock1.sleep, read_snapshot=snapshot)
    await c1.async_initialize(None, configure_safety=False)
    assert c1.intent_enabled
    assert c1.desired_power_raw == SAVED_POWER_RAW_MAX == 220

    # Plug&Charge replug adoption
    clock2 = FakeClock()
    io2 = FakeIO(clock2, charging=False)
    io2.regs[CC] = 0
    io2.regs[0x3000] = 1
    io2.regs[0x1011] = 225
    io2.regs[POW] = 225
    io2._physics()
    c2 = ChargingController(io2, clock=clock2, sleep=clock2.sleep, read_snapshot=snapshot)
    await c2.async_initialize(None, configure_safety=False)
    await c2.async_poll()
    clock2.now += 1.0
    await c2.async_poll()
    for _ in range(50):
        await asyncio.sleep(0)

    io2.regs[CC] = 1
    io2.regs[POW] = 225
    io2.stopped = False
    await c2.async_poll()
    await spin(lambda: c2.intent_enabled and c2.desired_power_raw == 220)
    assert c2.desired_power_raw == 220


async def test_power_raw_above_220_never_accepted_for_storage():
    assert not valid_controller_state(saved(power=225))
    clock = FakeClock()
    io = FakeIO(clock, charging=False)
    io.regs[0x1011] = 225
    c = ChargingController(io, clock=clock, sleep=clock.sleep, read_snapshot=snapshot)
    await c.async_initialize(saved(enabled=False, power=200), configure_safety=False)
    with pytest.raises(ValueError):
        await c.async_set_power(225)
