"""Persistent state for the FoxESS integration (HA Store API only).

Four Store files per config entry:

- ``foxess_charger_{entry_id}_state`` (new, version 1): the controller's
  saved intent (``controller``), session-tracker extras (``session``) and a
  fingerprint of the legacy files as last written by this version.
- ``foxess_charger_{entry_id}_session`` / ``_setpoints`` /
  ``_energy_baseline`` (legacy, version 1): kept in exactly the shape 2.4.3
  reads, so rolling back to it keeps the saved limits, the pause intent and
  the energy baseline.

Precedence on load: the new file wins while the legacy files still match the
fingerprint it recorded. If they differ, an older version has run since and
its (newer) view is imported instead. Anything malformed restores a
protective paused intent rather than guessing.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    ENERGY_GUARDS,
    MAX_POWER_RAW,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    USER_TIME_VALIDITY_RANGE,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
CONTROLLER_SCHEMA = 1

SESSION_FIELDS = (
    "session_start_wall", "session_start_total", "last_session",
    "prev_status", "stop_inhibit", "stop_pending",
)
# Plausibility bounds for restored limits, in register units (0.1 kW /
# 0.1 A). Model-specific bounds are the controller's job; these only reject
# values no supported charger could ever hold.
POWER_RAW_MAX = MAX_POWER_RAW
# 2.4.3 prev_status values that mean a session was genuinely running.
LEGACY_ACTIVE_STATUS = (2, 3, 4)
CURRENT_RAW_RANGE = (60, 320)
TIME_VALIDITY_RANGE = USER_TIME_VALIDITY_RANGE
# Internal keys kept under hass.data[DOMAIN] (outliving entry reloads so a
# stale on-disk record cannot undo a failed save; cleared by async_remove_all).
ATTEMPTED_REVISION_KEY = "_attempted_revision"
ATTEMPTED_HARD_KEY = "_attempted_hard"


def state_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_state"


def session_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_session"


def setpoints_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_setpoints"


def energy_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_energy_baseline"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value)
    )


def valid_controller_state(state: Any) -> bool:
    """Whether ``state`` has the controller saved-state v1 shape."""
    if not isinstance(state, dict) or state.get("schema") != CONTROLLER_SCHEMA:
        return False
    if not isinstance(state.get("enabled"), bool):
        return False
    for key in ("safety_latched", "stop_fallback"):
        if not isinstance(state.get(key), bool):
            return False
    revision = state.get("revision")
    if not _is_int(revision) or revision < 0:
        return False
    power = state.get("power_raw")
    if power is not None and not (_is_int(power) and 0 <= power <= POWER_RAW_MAX):
        return False
    current = state.get("current_raw")
    lo, hi = CURRENT_RAW_RANGE
    if current is not None and not (_is_int(current) and lo <= current <= hi):
        return False
    return True


def protective_state(
    power_raw: int | None = None,
    current_raw: int | None = None,
    revision: int = 0,
    *,
    safety_latched: bool = False,
    latch_cause: str | None = None,
    user_time_validity_s: int | None = None,
    user_default_current_raw: int | None = None,
) -> dict:
    """Paused intent that keeps whichever limits are still trustworthy."""
    out: dict[str, Any] = {
        "schema": CONTROLLER_SCHEMA,
        "enabled": False,
        "power_raw": power_raw,
        "current_raw": current_raw,
        "revision": revision,
        "safety_latched": safety_latched,
        "stop_fallback": False,
    }
    if safety_latched and latch_cause is not None:
        out["latch_cause"] = latch_cause
    if user_time_validity_s is not None:
        out["user_time_validity_s"] = user_time_validity_s
    if user_default_current_raw is not None:
        out["user_default_current_raw"] = user_default_current_raw
    return out


@dataclass
class LoadResult:
    """What setup needs from storage before the controller starts."""

    controller: dict | None          # None = first install, nothing saved
    session: dict = field(default_factory=dict)
    energy: dict[str, dict] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    source: str = "none"             # "state", "legacy", "none"


class ChargerStorage:
    """Owns every Store for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._state_store = Store(hass, STORE_VERSION, state_key(entry_id))
        self._session_store = Store(hass, STORE_VERSION, session_key(entry_id))
        self._setpoints_store = Store(hass, STORE_VERSION, setpoints_key(entry_id))
        self._energy_store = Store(hass, STORE_VERSION, energy_key(entry_id))
        self._controller: dict | None = None
        self._session_fields: dict = {f: None for f in SESSION_FIELDS[:4]}
        self._session_extra: dict = {"rearm_required": False}
        self._written: dict[str, Any] = {}
        self._write_lock = asyncio.Lock()
        # Highest controller revision this HA process tried to save for the
        # entry. It outlives the entry (reloads), so a reload can tell that
        # the record on disk is older than intent the controller already
        # acted on (e.g. a pause whose save failed).
        domain_data = hass.data.setdefault(DOMAIN, {})
        self._attempted: dict[str, int] = domain_data.setdefault(
            ATTEMPTED_REVISION_KEY, {}
        )
        self._attempted_hard: dict[str, bool] = domain_data.setdefault(
            ATTEMPTED_HARD_KEY, {}
        )
        self._entry_id = entry_id

    # ── loading ──────────────────────────────────────────────────────────

    async def _load(self, store: Store, name: str, issues: list[str]) -> tuple[bool, Any]:
        """(ok, data). A Store that cannot be read is reported, not raised."""
        try:
            return True, await store.async_load()
        except Exception as err:  # noqa: BLE001 - any read failure is protective
            _LOGGER.warning("Could not read FoxESS %s storage: %s", name, err)
            issues.append(f"{name}_unreadable")
            return False, None

    async def async_load(self) -> LoadResult:
        issues: list[str] = []
        ok_state, state = await self._load(self._state_store, "state", issues)
        ok_sess, session = await self._load(self._session_store, "session", issues)
        ok_set, setpoints = await self._load(self._setpoints_store, "setpoints", issues)
        _ok_en, energy = await self._load(self._energy_store, "energy", issues)

        result = LoadResult(controller=None, issues=issues)
        result.energy = self._validate_energy(energy, issues)
        result.session = self._validate_session(session, issues)

        legacy_now = {"session": session, "setpoints": setpoints}
        use_state = (
            ok_state and ok_sess and ok_set
            and state is not None
            and isinstance(state, dict)
            and (
                state.get("legacy_fingerprint") == legacy_now
                # Legacy files removed entirely: nothing newer to import.
                or (session is None and setpoints is None)
            )
        )
        if use_state:
            result.source = "state"
            controller = state.get("controller")
            if valid_controller_state(controller):
                result.controller = dict(controller)
            else:
                issues.append("controller_state_malformed")
                hard = (
                    not isinstance(controller, dict)
                    or controller.get("safety_latched") is not False
                    or self._session_stop_pending_or_bad(session, ok_sess)
                )
                result.controller = protective_state(
                    *self._salvage_limits(controller, setpoints),
                    safety_latched=hard,
                    latch_cause="hard" if hard else None,
                    **self._salvage_user_safety(controller),
                )
            extra = state.get("session")
            if isinstance(extra, dict) and isinstance(extra.get("rearm_required"), bool):
                result.session["rearm_required"] = extra["rearm_required"]
        elif not ok_state or (state is not None and not isinstance(state, dict)):
            issues.append("controller_state_malformed")
            result.source = "state"
            result.controller = protective_state(
                *self._salvage_limits(None, setpoints),
                safety_latched=True,
                latch_cause="hard",
            )
        elif session is None and setpoints is None and state is None and ok_sess and ok_set:
            result.source = "none"
        else:
            result.source = "legacy"
            result.controller = self._import_legacy(
                session, setpoints, ok_sess and ok_set, state, issues,
                session_readable=ok_sess,
            )

        attempted = self._attempted.get(self._entry_id)
        if (
            result.controller is not None and attempted is not None
            and result.controller["revision"] < attempted
        ):
            issues.append("controller_state_stale")
            hard = (
                self._is_hard_latch(result.controller)
                or bool(self._attempted_hard.get(self._entry_id))
            )
            result.controller = protective_state(
                *self._salvage_limits(result.controller, setpoints),
                attempted + 1,
                safety_latched=hard,
                latch_cause="hard" if hard else None,
                **self._salvage_user_safety(result.controller),
            )

        if issues:
            _LOGGER.warning("FoxESS storage restored with issues: %s", ", ".join(issues))
        self._controller = dict(result.controller) if result.controller else None
        for key in self._session_fields:
            self._session_fields[key] = result.session.get(key)
        self._session_extra["rearm_required"] = bool(result.session.get("rearm_required", False))
        return result

    @staticmethod
    def _session_stop_pending_or_bad(session: Any, ok_sess: bool) -> bool:
        if not ok_sess:
            return True
        if session is None:
            return False
        if not isinstance(session, dict):
            return True
        return session.get("stop_pending", False) is not False

    @staticmethod
    def _is_hard_latch(controller: Any) -> bool:
        """True if ``controller`` represents a hard (or unclassified) fault latch."""
        if not isinstance(controller, dict) or controller.get("safety_latched") is not True:
            return False
        cause = controller.get("latch_cause")
        if cause in ("comms", "fault", "stop_failed"):
            return False
        if cause == "hard":
            return True
        return controller.get("comms_latch") is not True

    @staticmethod
    def _salvage_user_safety(controller: Any) -> dict[str, int]:
        """Carry over valid user-set 0x3005/0x3006 values (re-validated by controller)."""
        out: dict[str, int] = {}
        if not isinstance(controller, dict):
            return out
        tv = controller.get("user_time_validity_s")
        t_lo, t_hi = TIME_VALIDITY_RANGE
        if _is_int(tv) and t_lo <= tv <= t_hi:
            out["user_time_validity_s"] = tv
        dc = controller.get("user_default_current_raw")
        c_lo, c_hi = CURRENT_RAW_RANGE
        if _is_int(dc) and c_lo <= dc <= c_hi:
            out["user_default_current_raw"] = dc
        return out

    @staticmethod
    def _salvage_limits(controller: Any, setpoints: Any) -> tuple[int | None, int | None]:
        power = current = None
        if isinstance(controller, dict):
            p, c = controller.get("power_raw"), controller.get("current_raw")
            if _is_int(p) and 0 <= p <= POWER_RAW_MAX:
                power = p
            if _is_int(c) and CURRENT_RAW_RANGE[0] <= c <= CURRENT_RAW_RANGE[1]:
                current = c
        if power is None or current is None:
            lp, lc, _bad = ChargerStorage._legacy_limits(setpoints)
            power = power if power is not None else lp
            current = current if current is not None else lc
        return power, current

    @staticmethod
    def _legacy_limits(setpoints: Any) -> tuple[int | None, int | None, bool]:
        """(power_raw, current_raw, malformed) from the legacy setpoints file."""
        if setpoints is None:
            return None, None, False
        if not isinstance(setpoints, dict):
            return None, None, True
        desired = setpoints.get("desired_setpoints")
        if desired is None:
            return None, None, False
        if not isinstance(desired, dict):
            return None, None, True
        malformed = False
        power = desired.get(str(REG_MAX_CHARGING_POWER))
        current = desired.get(str(REG_MAX_CHARGING_CURRENT))
        if power is not None and not (_is_int(power) and 0 <= power <= POWER_RAW_MAX):
            power, malformed = None, True
        lo, hi = CURRENT_RAW_RANGE
        if current is not None and not (_is_int(current) and lo <= current <= hi):
            current, malformed = None, True
        return power, current, malformed

    def _import_legacy(self, session: Any, setpoints: Any, readable: bool,
                       state: Any, issues: list[str], *, session_readable: bool = True) -> dict:
        power, current, bad_limits = self._legacy_limits(setpoints)
        if bad_limits:
            issues.append("legacy_setpoints_malformed")
        stop_inhibit = stop_pending = False
        bad_session = False
        if session is not None:
            if not isinstance(session, dict):
                bad_session = True
            else:
                stop_inhibit = session.get("stop_inhibit", False)
                stop_pending = session.get("stop_pending", False)
                if not isinstance(stop_inhibit, bool) or not isinstance(stop_pending, bool):
                    bad_session = True
        if bad_session:
            issues.append("legacy_session_malformed")
        src_ctrl = state.get("controller") if isinstance(state, dict) else None
        user_safety = self._salvage_user_safety(src_ctrl)
        hard = self._is_hard_latch(src_ctrl)
        # A partial save (legacy written with stop_pending=True while the new
        # state file failed or crashed) cannot be told apart from a hard latch,
        # so fail closed: import as an unclassified latch (loads as hard).
        partial_latch = state is not None and (
            bool(stop_pending) or bad_session or not session_readable
        )
        revision = 0
        if valid_controller_state(src_ctrl):
            revision = src_ctrl["revision"] + 1
        if bad_session or bad_limits or not readable:
            latched = hard or partial_latch
            return protective_state(
                power,
                current,
                revision,
                safety_latched=latched,
                latch_cause="hard" if latched else None,
                **user_safety,
            )
        # 2.4.3 leaves both stop flags false after a session ends naturally,
        # so their absence says nothing about intent. Only a session 2.4.3
        # last saw running imports as enabled; finished, idle, unknown or a
        # missing session store import paused (a nonzero 0x3002 write would
        # resume a finished session).
        prev = session.get("prev_status") if isinstance(session, dict) else None
        active = _is_int(prev) and prev in LEGACY_ACTIVE_STATUS
        latched = hard or partial_latch
        # When an authoritative 3.x state file already exists, a legacy
        # fingerprint mismatch cannot be distinguished from an interrupted save
        # or a rollback-and-reupgrade; fail closed to paused rather than
        # resurrecting an unconfirmed enable or limit increase.
        paused_on_mismatch = state is not None
        out = {
            "schema": CONTROLLER_SCHEMA,
            "enabled": (
                False
                if (latched or paused_on_mismatch)
                else (active and not (stop_inhibit or stop_pending))
            ),
            "power_raw": power,
            "current_raw": current,
            "revision": revision,
            "safety_latched": latched,
            # A legacy pending Stop was sent via 0x4001: the charger may be
            # in its Stop state, which only a deliberate resume should leave.
            "stop_fallback": bool(stop_pending),
            **user_safety,
        }
        if hard:
            out["latch_cause"] = "hard"
        return out

    @staticmethod
    def _validate_session(session: Any, issues: list[str]) -> dict:
        out: dict = {}
        if not isinstance(session, dict):
            return out
        wall, total = session.get("session_start_wall"), session.get("session_start_total")
        if wall is not None or total is not None:
            if _is_number(wall) and _is_int(total) and total >= 0:
                out["session_start_wall"] = float(wall)
                out["session_start_total"] = total
            else:
                issues.append("session_baseline_malformed")
        last = session.get("last_session")
        if isinstance(last, dict):
            out["last_session"] = last
        elif last is not None:
            issues.append("last_session_malformed")
        prev = session.get("prev_status")
        if _is_int(prev):
            out["prev_status"] = prev
        return out

    @staticmethod
    def _validate_energy(stored: Any, issues: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not isinstance(stored, dict):
            if stored is not None:
                issues.append("energy_baseline_malformed")
            return out
        for key, entry in stored.items():
            if key not in ENERGY_GUARDS or not isinstance(entry, dict):
                continue
            raw, wall = entry.get("raw"), entry.get("wall_ts")
            if not (_is_int(raw) and raw >= 0 and _is_number(wall)):
                issues.append(f"energy_baseline_malformed:{key}")
                continue
            offset = entry.get("offset", 0)
            if not _is_int(offset):
                issues.append(f"energy_baseline_malformed:{key}")
                continue
            out[key] = {"raw": raw, "wall_ts": float(wall)}
            if offset:
                out[key]["offset"] = offset
        return out

    # ── saving ───────────────────────────────────────────────────────────

    @classmethod
    def _latch_rank(cls, controller: dict) -> int:
        if not controller.get("safety_latched"):
            return -1
        if cls._is_hard_latch(controller):
            return 3
        return {"comms": 0, "fault": 1, "stop_failed": 2}.get(
            controller.get("latch_cause"), 3
        )

    @classmethod
    def _is_more_protective(cls, candidate: dict, previous: dict | None) -> bool:
        """True if ``candidate`` adds pause/latch protection over ``previous``
        without relaxing any protective flag."""
        cand_paused = (
            not candidate["enabled"]
            or candidate["safety_latched"]
            or candidate["stop_fallback"]
        )
        if previous is None:
            return cand_paused
        prev_rank, cand_rank = cls._latch_rank(previous), cls._latch_rank(candidate)
        if cand_rank < prev_rank:
            return False
        if previous["stop_fallback"] and not candidate["stop_fallback"]:
            return False
        if not previous["enabled"] and candidate["enabled"]:
            return False
        return (
            cand_rank > prev_rank
            or (candidate["stop_fallback"] and not previous["stop_fallback"])
            or (previous["enabled"] and not candidate["enabled"])
        )

    @classmethod
    def _raises_output(cls, candidate: dict, previous: dict) -> bool:
        """True if ``candidate`` relaxes protection or raises a saved limit."""
        if candidate["enabled"] and not previous["enabled"]:
            return True
        if cls._latch_rank(candidate) < cls._latch_rank(previous):
            return True
        if previous["stop_fallback"] and not candidate["stop_fallback"]:
            return True
        for key in ("power_raw", "current_raw"):
            prev_v, cand_v = previous.get(key), candidate.get(key)
            if prev_v is not None and (cand_v is None or cand_v > prev_v):
                return True
        return False

    def _effective_controller(self) -> tuple[dict | None, bool]:
        """Controller state to persist, hardened if a newer save never landed."""
        controller = dict(self._controller) if self._controller is not None else None
        cur_rev = controller["revision"] if controller is not None else -1
        stale = self._attempted.get(self._entry_id, -1) > cur_rev
        if controller is not None and stale:
            controller["enabled"] = False
            if (
                self._is_hard_latch(controller)
                or self._attempted_hard.get(self._entry_id)
            ):
                controller["safety_latched"] = True
                controller["latch_cause"] = "hard"
                controller["comms_latch"] = False
        return controller, stale

    def _legacy_session_payload(
        self, controller: dict | None = None, *, force_paused: bool = False,
    ) -> dict:
        c = self._controller if controller is None else controller
        # A fault latch or Stop fallback leaves `enabled` set but must not
        # charge, so it is projected as paused too. The legacy files are
        # written first, so this also protects a restart after the new state
        # file failed to save (the fingerprint mismatch imports this view).
        paused = force_paused or (
            c is not None and (
                not c["enabled"] or c["safety_latched"] or c["stop_fallback"]
            )
        )
        payload = dict(self._session_fields)
        # 2.4.3 reads stop_pending as "a Stop is outstanding" and keeps
        # stopping until the charger reports inactive - the protective
        # reading of a paused intent if it is ever loaded after a rollback.
        payload["stop_inhibit"] = paused
        payload["stop_pending"] = paused
        return payload

    def _legacy_setpoints_payload(self, controller: dict | None = None) -> dict | None:
        c = self._controller if controller is None else controller
        if c is None:
            return None
        desired = {}
        if c.get("current_raw") is not None:
            desired[str(REG_MAX_CHARGING_CURRENT)] = c["current_raw"]
        if c.get("power_raw") is not None:
            desired[str(REG_MAX_CHARGING_POWER)] = c["power_raw"]
        return {"desired_setpoints": desired}

    async def _save(self, name: str, store: Store, payload: Any) -> None:
        if payload is None or self._written.get(name) == payload:
            return
        await store.async_save(payload)
        self._written[name] = payload

    async def _async_write_locked(self, *, state_first: bool = False) -> None:
        # Protective saves write legacy files first: if the new file then fails
        # to save, the next load sees a fingerprint mismatch and imports the
        # (newer, more protective) legacy projection instead of an older
        # controller state. Output-raising saves write the authoritative state
        # first so a failed state write never leaves an uncommitted higher
        # limit or unpaused flag in the legacy files; if a subsequent legacy
        # write fails, the fingerprint mismatch imports the older lower/paused
        # legacy files and restores paused. Caller holds self._write_lock so
        # concurrent controller and session saves cannot interleave and leave
        # a mismatched legacy_fingerprint on disk.
        controller, stale = self._effective_controller()
        session = self._legacy_session_payload(
            controller, force_paused=stale and controller is None,
        )
        setpoints = self._legacy_setpoints_payload(controller)
        session_extra = dict(self._session_extra)
        state_payload = None if controller is None else {
            "controller": controller,
            "session": session_extra,
            "legacy_fingerprint": {"session": session, "setpoints": setpoints},
        }
        if state_first and not stale and state_payload is not None:
            await self._save("state", self._state_store, state_payload)
            await self._save("session", self._session_store, session)
            await self._save("setpoints", self._setpoints_store, setpoints)
            return
        await self._save("session", self._session_store, session)
        await self._save("setpoints", self._setpoints_store, setpoints)
        if state_payload is None:
            return
        await self._save("state", self._state_store, state_payload)

    async def _async_write_all(self) -> None:
        async with self._write_lock:
            await self._async_write_locked()

    async def async_save_controller(self, state: dict) -> None:
        """Persist callback handed to the controller. Raises on failure so
        the controller never treats unsaved intent as staged."""
        if not valid_controller_state(state):
            raise ValueError(f"refusing to persist malformed controller state: {state!r}")
        self._attempted[self._entry_id] = max(
            self._attempted.get(self._entry_id, -1), state["revision"]
        )
        if self._is_hard_latch(state):
            self._attempted_hard[self._entry_id] = True
        async with self._write_lock:
            if (
                self._controller is not None
                and state["revision"] < self._controller["revision"]
            ):
                return
            previous = self._controller
            self._controller = dict(state)
            state_first = previous is not None and self._raises_output(
                self._controller, previous
            )
            try:
                await self._async_write_locked(state_first=state_first)
            except BaseException:
                if not self._is_more_protective(self._controller, previous):
                    self._controller = previous
                raise
            if (
                not self._is_hard_latch(self._controller)
                and self._attempted.get(self._entry_id, -1) <= self._controller["revision"]
            ):
                self._attempted_hard.pop(self._entry_id, None)

    async def async_save_session(self, fields: dict, *, rearm_required: bool) -> None:
        async with self._write_lock:
            for key in self._session_fields:
                self._session_fields[key] = fields.get(key)
            self._session_extra["rearm_required"] = rearm_required
            await self._async_write_locked()

    async def async_save_energy(self, baselines: dict[str, dict]) -> None:
        async with self._write_lock:
            await self._save("energy", self._energy_store, baselines)

    async def async_remove_all(self) -> None:
        """Remove all four stores and per-entry revision tracking.

        Called when the config entry is deleted so no orphaned storage files
        or in-memory revision guards remain. Tries every store even if one
        removal fails, then re-raises the first error.
        """
        stores = (
            ("state", self._state_store),
            ("session", self._session_store),
            ("setpoints", self._setpoints_store),
            ("energy", self._energy_store),
        )
        first_err: Exception | None = None
        async with self._write_lock:
            for name, store in stores:
                try:
                    await store.async_remove()
                except Exception as err:  # noqa: BLE001 - clean up remaining stores
                    _LOGGER.warning("Could not remove FoxESS %s storage: %s", name, err)
                    if first_err is None:
                        first_err = err
            self._written.clear()
            self._controller = None
            self._session_fields = {f: None for f in SESSION_FIELDS[:4]}
            self._session_extra = {"rearm_required": False}
            self._attempted.pop(self._entry_id, None)
            self._attempted_hard.pop(self._entry_id, None)
        if first_err is not None:
            raise first_err
