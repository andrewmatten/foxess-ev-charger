"""persistence.py: legacy import, corrupt/missing stores, new-key precedence
and downgrade readability of the legacy projections."""
from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys

import pytest

from custom_components.foxess_charger.persistence import (
    ChargerStorage, energy_key, session_key, setpoints_key, state_key,
)

ENTRY = "persistentry01"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_243 = "4490e4a"


def _put(hass_storage, key, data):
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


def _legacy_session(**overrides):
    base = {
        "session_start_wall": None, "session_start_total": None,
        "last_session": None, "prev_status": 1,
        "stop_inhibit": False, "stop_pending": False,
    }
    base.update(overrides)
    return base


def _state(**overrides):
    base = {
        "schema": 1, "enabled": True, "power_raw": 22, "current_raw": 100,
        "revision": 7, "safety_latched": False, "stop_fallback": False,
    }
    base.update(overrides)
    return base


async def _load(hass):
    return await ChargerStorage(hass, ENTRY).async_load()


async def test_first_install_has_no_saved_state(hass, hass_storage):
    result = await _load(hass)
    assert result.controller is None
    assert result.issues == []


async def test_legacy_import_enabled_with_limits(hass, hass_storage):
    _put(hass_storage, session_key(ENTRY), _legacy_session(prev_status=3))
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 14, "12289": 100}})
    result = await _load(hass)
    assert result.controller == _state(power_raw=14, current_raw=100, revision=0)
    assert result.source == "legacy"


@pytest.mark.parametrize("prev_status", [2, 3, 4])
async def test_legacy_active_session_imports_enabled(hass, hass_storage, prev_status):
    _put(hass_storage, session_key(ENTRY), _legacy_session(prev_status=prev_status))
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 14}})
    assert (await _load(hass)).controller["enabled"] is True


@pytest.mark.parametrize("prev_status", [0, 1, 5, 6, 8, None, "3"])
async def test_legacy_inactive_or_unknown_session_imports_paused(hass, hass_storage, prev_status):
    """A naturally finished 2.4.3 session has no stop flags; a nonzero write
    would resume it, so only a genuinely active session imports enabled."""
    _put(hass_storage, session_key(ENTRY), _legacy_session(prev_status=prev_status))
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 14}})
    result = await _load(hass)
    assert result.controller["enabled"] is False
    assert result.controller["power_raw"] == 14      # the saved limit survives


async def test_legacy_setpoints_without_session_store_import_paused(hass, hass_storage):
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 14}})
    result = await _load(hass)
    assert result.controller["enabled"] is False and result.controller["power_raw"] == 14


@pytest.mark.parametrize(
    ("inhibit", "pending", "fallback"),
    [(True, False, False), (False, True, True), (True, True, True)],
)
async def test_legacy_stop_intent_imports_as_paused(hass, hass_storage, inhibit, pending, fallback):
    _put(hass_storage, session_key(ENTRY), _legacy_session(stop_inhibit=inhibit, stop_pending=pending))
    result = await _load(hass)
    assert result.controller["enabled"] is False
    assert result.controller["stop_fallback"] is fallback


async def test_legacy_session_baseline_and_last_session_imported(hass, hass_storage):
    last = {"ended": "2026-01-02T10:00:00+00:00", "energy_kwh": 5.0, "duration_min": 60.0}
    _put(hass_storage, session_key(ENTRY), _legacy_session(
        session_start_wall=1_790_000_000.0, session_start_total=1234,
        last_session=last, prev_status=3,
    ))
    result = await _load(hass)
    assert result.session == {
        "session_start_wall": 1_790_000_000.0, "session_start_total": 1234,
        "last_session": last, "prev_status": 3,
    }


@pytest.mark.parametrize(
    "session",
    [["not", "a", "dict"], _legacy_session(stop_inhibit="yes"), _legacy_session(stop_pending=1)],
)
async def test_malformed_legacy_session_is_protective(hass, hass_storage, session):
    _put(hass_storage, session_key(ENTRY), session)
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 20}})
    result = await _load(hass)
    assert result.controller["enabled"] is False
    assert result.controller["power_raw"] == 20
    assert "legacy_session_malformed" in result.issues


@pytest.mark.parametrize(
    "setpoints",
    [
        {"desired_setpoints": {"12290": "abc", "12289": 100}},
        {"desired_setpoints": {"12290": 9999, "12289": 100}},
        {"desired_setpoints": {"12290": True, "12289": 100}},
        {"desired_setpoints": ["x"]},
        "garbage",
    ],
)
async def test_malformed_legacy_limits_are_protective(hass, hass_storage, setpoints):
    _put(hass_storage, session_key(ENTRY), _legacy_session())
    _put(hass_storage, setpoints_key(ENTRY), setpoints)
    result = await _load(hass)
    assert result.controller["enabled"] is False
    assert result.controller["power_raw"] is None
    assert "legacy_setpoints_malformed" in result.issues


async def test_unreadable_store_is_protective(hass, hass_storage, monkeypatch):
    from homeassistant.helpers.storage import Store

    async def _boom(self):
        raise ValueError("corrupt json")

    monkeypatch.setattr(Store, "async_load", _boom)
    result = await _load(hass)
    assert result.controller["enabled"] is False
    assert any(i.endswith("_unreadable") for i in result.issues)


async def test_malformed_energy_entries_dropped_individually(hass, hass_storage):
    _put(hass_storage, energy_key(ENTRY), {
        "total_energy_raw": {"raw": 5000, "wall_ts": 1_790_000_000.0},
        "current_energy_raw": {"raw": True, "wall_ts": 1_790_000_000.0},
        "bogus": {"raw": 1, "wall_ts": 1.0},
    })
    result = await _load(hass)
    assert result.energy == {"total_energy_raw": {"raw": 5000, "wall_ts": 1_790_000_000.0}}


# ── new key round trip / precedence ───────────────────────────────────────

async def _roundtrip_json(hass_storage):
    """What a real restart sees: every Store file through JSON."""
    for key, value in list(hass_storage.items()):
        hass_storage[key] = json.loads(json.dumps(value))


async def test_new_state_round_trips_with_revision_and_latches(hass, hass_storage):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    saved = _state(enabled=False, safety_latched=True, stop_fallback=True, revision=12)
    await storage.async_save_controller(saved)
    await storage.async_save_session(
        {"session_start_wall": 1_790_000_000.5, "session_start_total": 10,
         "last_session": None, "prev_status": 4},
        rearm_required=True,
    )
    await _roundtrip_json(hass_storage)
    result = await _load(hass)
    assert result.source == "state"
    assert result.controller == saved
    assert result.session["rearm_required"] is True
    assert result.issues == []


async def test_malformed_new_state_is_protective_and_salvages_limits(hass, hass_storage):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state())
    hass_storage[state_key(ENTRY)]["data"]["controller"]["enabled"] = "maybe"
    result = await _load(hass)
    assert result.controller["enabled"] is False
    assert result.controller["power_raw"] == 22
    assert "controller_state_malformed" in result.issues


async def test_save_rejects_malformed_controller_state(hass, hass_storage):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    with pytest.raises(ValueError):
        await storage.async_save_controller({"schema": 2})
    assert state_key(ENTRY) not in hass_storage


async def test_save_failure_propagates(hass, hass_storage, monkeypatch):
    from homeassistant.helpers.storage import Store

    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    with pytest.raises(OSError):
        await storage.async_save_controller(_state())


async def test_restore_older_than_last_attempted_save_is_protective(
    hass, hass_storage, monkeypatch
):
    """A pause whose save failed must not be undone by reloading the older
    enabled record that is still on disk."""
    from homeassistant.helpers.storage import Store

    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state(enabled=True, revision=5))
    real_save = Store.async_save

    async def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "async_save", _boom)
    with pytest.raises(OSError):
        await storage.async_save_controller(_state(enabled=False, revision=6))
    monkeypatch.setattr(Store, "async_save", real_save)
    result = await ChargerStorage(hass, ENTRY).async_load()   # entry reload
    assert result.controller["enabled"] is False
    assert result.controller["revision"] > 6
    assert result.controller["power_raw"] == 22                # limits kept
    assert "controller_state_stale" in result.issues


async def test_legacy_changed_by_older_version_wins_over_new_state(hass, hass_storage):
    """Rolled back to 2.4.3, user paused there, upgraded again: 2.4.3's
    newer legacy files must win over our stale new-format state."""
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state(enabled=True, power_raw=22))
    await _roundtrip_json(hass_storage)
    # 2.4.3 writes its own files.
    _put(hass_storage, session_key(ENTRY), _legacy_session(stop_inhibit=True, stop_pending=True))
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 30}})
    result = await _load(hass)
    assert result.source == "legacy"
    assert result.controller["enabled"] is False
    assert result.controller["power_raw"] == 30
    assert result.controller["revision"] == 8


async def test_paused_state_projects_protective_legacy_files(hass, hass_storage):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_controller(_state(enabled=False, power_raw=22, current_raw=100))
    session = hass_storage[session_key(ENTRY)]["data"]
    assert session["stop_inhibit"] is True and session["stop_pending"] is True
    assert hass_storage[setpoints_key(ENTRY)]["data"] == {
        "desired_setpoints": {"12289": 100, "12290": 22},
    }
    await storage.async_save_controller(_state(enabled=True))
    session = hass_storage[session_key(ENTRY)]["data"]
    assert session["stop_inhibit"] is False and session["stop_pending"] is False


# ── downgrade: 2.4.3 itself loads the projections ─────────────────────────

def _import_release_243(tmp_path):
    """Materialise the 2.4.3 integration from git as package `foxess_243`
    (under a custom_components/ directory, as HA's frame helper expects of
    integration code)."""
    root = tmp_path / "custom_components"
    pkg = root / "foxess_243"
    pkg.mkdir(parents=True)
    for name in ("__init__.py", "const.py", "energy_guard.py", "modbus_client.py"):
        source = subprocess.run(
            ["git", "-C", REPO, "show", f"{RELEASE_243}:{name}"],
            check=True, capture_output=True, text=True,
        ).stdout
        (pkg / name).write_text(source)
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("foxess_243")
    finally:
        sys.path.remove(str(root))


async def test_release_243_loads_paused_projection(hass, hass_storage, tmp_path):
    storage = ChargerStorage(hass, ENTRY)
    await storage.async_load()
    await storage.async_save_session(
        {"session_start_wall": 1_790_000_000.0, "session_start_total": 500,
         "last_session": {"energy_kwh": 1.0, "duration_min": 5.0}, "prev_status": 4},
        rearm_required=False,
    )
    await storage.async_save_controller(_state(enabled=False, power_raw=14, current_raw=80))
    await _roundtrip_json(hass_storage)

    old = _import_release_243(tmp_path)
    from homeassistant.helpers.storage import Store

    coordinator = old.FoxESSChargerCoordinator(
        hass, object(), 10,
        store=Store(hass, 1, session_key(ENTRY)),
        setpoints_store=Store(hass, 1, setpoints_key(ENTRY)),
        energy_store=Store(hass, 1, energy_key(ENTRY)),
    )
    await coordinator.async_load_session_state()
    await coordinator.async_load_desired_setpoints()
    # 2.4.3's own restored state (unavoidably its private attributes):
    # paused intent with a Stop still to confirm, and the saved limits.
    assert coordinator._stop_inhibit is True
    assert coordinator._stop_pending is True
    assert coordinator._charging_desired is False
    assert coordinator.desired_setpoints == {0x3002: 14, 0x3001: 80}
    assert coordinator._session_start_total == 500
    assert coordinator._last_completed_session == {"energy_kwh": 1.0, "duration_min": 5.0}
    sys.modules.pop("foxess_243", None)
    for mod in [m for m in sys.modules if m.startswith("foxess_243.")]:
        sys.modules.pop(mod)


async def test_upgrade_from_243_files_through_setup(hass, hass_storage, enable_custom_integrations):
    """Real setup with the three 2.4.3 files present: paused intent and
    limits reach the controller, the session baseline is kept."""
    from fake_controller import FakeController
    from ha_harness import Harness, make_entry

    _put(hass_storage, session_key(ENTRY), _legacy_session(
        session_start_wall=1_790_000_000.0, session_start_total=900, prev_status=3,
        stop_inhibit=True, stop_pending=False,
    ))
    _put(hass_storage, setpoints_key(ENTRY), {"desired_setpoints": {"12290": 14}})
    _put(hass_storage, energy_key(ENTRY), {"total_energy_raw": {"raw": 900, "wall_ts": 1.0}})
    fake = FakeController(status=4)
    fake.hw["total_energy_raw"] = 900
    h = Harness(hass, make_entry(ENTRY), fake)
    assert await h.async_setup()
    try:
        saved = fake.calls[0][1]
        assert saved["enabled"] is False and saved["power_raw"] == 14
        assert state_key(ENTRY) in hass_storage
        assert hass.states.get("number.foxess_charger_max_charging_power").state == "1.4"
    finally:
        await h.async_unload()
    # The legacy files are still in 2.4.3's shape afterwards.
    session = hass_storage[session_key(ENTRY)]["data"]
    assert session["stop_inhibit"] is True
    assert copy.deepcopy(hass_storage[setpoints_key(ENTRY)]["data"]) == {
        "desired_setpoints": {"12290": 14},
    }
