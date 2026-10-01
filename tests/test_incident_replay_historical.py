"""Incident replays against the genuine historical integration code.

Each historical version is extracted from git (or, for the live
patch, from the exported live baseline) into a temporary top-level package
and driven through SimCharger via LegacyClient. The replay asserts the
*safe* outcome, so it is marked xfail(strict) on the version that shipped
the defect: a strict xfail that starts passing means the replay no longer
reproduces the incident and the test itself must be revisited.

These replays touch version-internal attributes (desired_setpoints, entity
construction) because that is the only way to drive the old code; they
test the old code, not the rebuild contract.

Sources are looked up via FOXESS_HISTORY_REPO / FOXESS_LIVE_BASELINE, falling
back to the recovery workspace layout; tests skip if unavailable.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
)

from rebuild_simulator import (
    CHARGING,
    CONNECTED,
    FINISHED,
    MAX_POWER_RAW,
    LegacyClient,
    SimCharger,
)

_HERE = Path(__file__).resolve()
_WORKSPACE = _HERE.parents[3] if len(_HERE.parents) > 3 else _HERE.parent
REPO = Path(os.environ.get("FOXESS_HISTORY_REPO", _WORKSPACE / "repo"))
LIVE = Path(os.environ.get("FOXESS_LIVE_BASELINE", _WORKSPACE / "baselines" / "live"))

V240 = "692a536"          # 2.4.0 - battery drain possible
V241 = "beaa7ad"          # 2.4.1 - 90 s heartbeat with 180 s validity
V243_PUBLISHED = "4490e4a"
LIVE_PATCH = "live"       # 2.4.3 + Start-first patch

MODULES = (
    "__init__", "binary_sensor", "config_flow", "const", "device_trigger",
    "diagnostics", "energy_guard", "modbus_client", "number", "select",
    "sensor", "switch",
)

_loaded: dict[str, object] = {}


def _source(ref: str, module: str) -> str | None:
    if ref == LIVE_PATCH:
        path = LIVE / f"{module}.py"
        return path.read_text() if path.exists() else None
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "show", f"{ref}:{module}.py"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def load_version(ref: str, root: Path):
    """Import the integration as it was at `ref` under a unique name."""
    if ref in _loaded:
        return _loaded[ref]
    name = f"foxess_hist_{ref}"
    # HA's frame helper identifies the calling integration by a
    # ".../custom_components/<name>/" path component.
    root = root / "custom_components"
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    for module in MODULES:
        text = _source(ref, module)
        if text is None:
            if module == "__init__":
                pytest.skip(f"historical source for {ref} unavailable")
            continue
        (pkg / f"{module}.py").write_text(text)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    package = importlib.import_module(name)
    for module in ("number", "switch", "const"):
        importlib.import_module(f"{name}.{module}")
    _loaded[ref] = package
    return package


@pytest.fixture(scope="module")
def hist_root(tmp_path_factory):
    return tmp_path_factory.mktemp("foxess_history")


def _entry():
    entry = MagicMock()
    entry.entry_id = "replay"
    return entry


def _entity(entity, hass):
    entity.hass = hass
    entity.entity_id = "replay.entity"
    entity.async_write_ha_state = MagicMock()
    return entity


def _power_number(pkg, coordinator, client, hass):
    desc = next(d for d in pkg.number.NUMBERS if d.register == REG_MAX_CHARGING_POWER)
    return _entity(pkg.number.FoxESSNumber(coordinator, client, desc, _entry()), hass)


def xfail_on(bad: str, reason: str):
    return lambda ref: pytest.param(
        ref, marks=pytest.mark.xfail(strict=True, reason=reason) if ref == bad else (),
    )


# ── stop-then-resume: limit write while intentionally stopped resumes charging ──────

STOP_RESUME = xfail_on(V240, "2.4.0 writes Max Charging Power while stopped (stop-then-resume incident)")


@pytest.mark.parametrize("ref", [STOP_RESUME(V240), STOP_RESUME(V243_PUBLISHED)])
async def test_stop_resume_power_change_after_stop_never_resumes(hass, hist_root, ref):
    pkg = load_version(ref, hist_root)
    sim = SimCharger(state=CHARGING, reported_validity=180, effective_validity=60)
    client = LegacyClient(sim)
    coordinator = pkg.FoxESSChargerCoordinator(hass, client, scan_interval=10)
    await coordinator.async_refresh()
    assert coordinator.last_update_success

    switch = _entity(pkg.switch.FoxESSChargingSwitch(coordinator, client, _entry()), hass)
    await switch.async_turn_off()
    assert sim.state == FINISHED
    mark = sim.mark()

    # the automation's end-of-window power update
    number = _power_number(pkg, coordinator, client, hass)
    try:
        await number.async_set_native_value(7.0)
    except Exception:  # a refusal is fine; resuming is not
        pass
    await coordinator.async_refresh()
    await coordinator._heartbeat_tick()
    await coordinator.async_refresh()
    await coordinator._heartbeat_tick()

    assert sim.positive_cap_writes(mark) == []
    assert sim.state == FINISHED
    assert sim.measured_power_raw() == 0


# ── 1.4 / 7 kW sawtooth: 90 s heartbeat vs 60 s effective expiry ──────────
# Runs the version's real heartbeat task with its own interval maths; only
# time is compressed (1 simulated second = SCALE real seconds).

SCALE = 0.002
SAW = xfail_on(V241, "2.4.1 heartbeats every 90 s against a 60 s firmware expiry")


@pytest.mark.parametrize("ref", [SAW(V241), SAW(V243_PUBLISHED)])
async def test_sawtooth_cap_holds_for_ten_minutes(hass, hist_root, monkeypatch, ref):
    pkg = load_version(ref, hist_root)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    sim = SimCharger(
        clock=lambda: (loop.time() - t0) / SCALE,
        state=CHARGING, reported_validity=180, effective_validity=60,
    )
    client = LegacyClient(sim)
    original = pkg.get_heartbeat_interval
    monkeypatch.setattr(pkg, "get_heartbeat_interval", lambda tv: original(tv) * SCALE)
    for const_name in ("SETPOINT_REASSERT_MIN_INTERVAL", "FAILED_WRITE_RETRY_S"):
        if hasattr(pkg, const_name):
            monkeypatch.setattr(pkg, const_name, getattr(pkg, const_name) * SCALE)

    coordinator = pkg.FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.desired_setpoints = {REG_MAX_CHARGING_POWER: 14}
    await coordinator.async_refresh()
    await coordinator.async_start_heartbeat()
    try:
        # first cap lands on the startup tick or after one interval
        deadline = loop.time() + 120 * SCALE
        while not sim.writes_since(0, REG_MAX_CHARGING_POWER) and loop.time() < deadline:
            await asyncio.sleep(SCALE)
        first = sim.writes_since(0, REG_MAX_CHARGING_POWER)
        assert first, "heartbeat never wrote the cap"
        t_cap = first[0].t
        # HA keeps polling (every 10 s) so the device never sees silence
        while sim.now() < t_cap + 660:
            await asyncio.sleep(10 * SCALE)
            await hass.async_add_executor_job(client.read_registers, 0x1000, 30)
    finally:
        await coordinator.async_stop_heartbeat()

    assert sim.max_power_limit(t_cap + 1, t_cap + 650) == 14
    assert sim.max_measured_power(t_cap + 1, t_cap + 650) <= 14


# ── refused-Start: pre-Start cap write starts charging, Start refused, Stop ──────

REFUSED_START = xfail_on(V243_PUBLISHED, "published 2.4.3 writes caps before Start (refused-Start incident)")


@pytest.mark.parametrize("ref", [REFUSED_START(V243_PUBLISHED), REFUSED_START(LIVE_PATCH)])
async def test_refused_start_enable_is_not_killed_by_refused_start(hass, hist_root, monkeypatch, ref):
    pkg = load_version(ref, hist_root)
    if hasattr(pkg, "POST_START_SETTLE_S"):
        monkeypatch.setattr(pkg, "POST_START_SETTLE_S", 0)
    sim = SimCharger(state=CONNECTED, reported_validity=180, effective_validity=60)
    client = LegacyClient(sim)
    coordinator = pkg.FoxESSChargerCoordinator(hass, client, scan_interval=10)
    coordinator.desired_setpoints = {REG_MAX_CHARGING_POWER: 14}
    await coordinator.async_refresh()

    switch = _entity(pkg.switch.FoxESSChargingSwitch(coordinator, client, _entry()), hass)
    try:
        await switch.async_turn_on()
    except Exception:
        pass  # outcome judged on the device below

    assert (REG_CHARGING_CONTROL, 2) not in sim.wire_writes()
    assert sim.state == CHARGING
    await coordinator._heartbeat_tick()
    assert sim.power_limit_raw() == 14
