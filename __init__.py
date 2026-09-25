"""FoxESS EV Charger integration."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, PLATFORMS,
    CONF_HOST, CONF_PORT, CONF_SLAVE_ID,
    DEFAULT_SCAN_INTERVAL,
    FAULT_BITS, ALARM_BITS, decode_bitmask,
    REG_TOTAL_ENERGY, REG_CURRENT_ENERGY, REG_FAULT_CODE, REG_RFID_CARD,
    REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT,
    REG_CHARGING_CONTROL,
    SESSION_ACTIVE_STATUSES, STOP_REASON_MAP,
    MAX_STATUS_READ_FAILURES, SETPOINT_REASSERT_MIN_INTERVAL, get_heartbeat_interval,
    DEFAULT_TIME_VALIDITY,
    REASSERTED_DATA_KEYS,
    ENERGY_GUARDS, MAX_ENERGY_REJECTION_RECORDS,
    ENERGY_QUANTUM_KWH,
    BLOCK_STATUS, BLOCK_CONFIG, BLOCK_PHASE_BOX, BLOCK_STALENESS_FACTOR,
    get_capabilities, get_setpoint_bounds,
    REG_MAX_CHARGING_CURRENT,
    EVENT_SESSION_COMPLETED,
)
from .modbus_client import FoxESSModbusClient
from .energy_guard import (
    decide_energy_reading, check_cumulative_window, ENERGY_WINDOW_SECONDS,
    NEAR_ZERO_RAW_UNITS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "A7300P1-E-B-WO"

# Don't judge a rolling energy-rate window (see _sanitize_energy) until it
# has enough samples spanning a meaningful stretch of time - avoids a false
# positive on the very first couple of polls after (re)start, before the
# window has accumulated anything statistically meaningful.
ENERGY_WINDOW_MIN_SAMPLES = 5

# ── Session persistence (added 2.2.0) ─────────────────────────────────────────
# "Persistent" here means surviving an HA restart, not just a coordinator
# poll cycle: an in-progress session's start time/baseline used to live only
# in the coordinator's instance attributes (_session_start_ts et al.), and
# last_session only in coordinator.data - both gone the moment HA restarts,
# even mid-session. Uses HA's standard Store helper (a small integration-
# local JSON file under .storage/) rather than entry.data, since this is
# runtime state that changes on every charging session, not configuration.
SESSION_STORAGE_VERSION = 1


def _session_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_session"


# ── Desired-setpoint persistence (P0 fix, added post-2.3.0) ───────────────────
# desired_setpoints (see FoxESSChargerCoordinator.__init__ below) used to be
# in-memory only - a HA restart forgot any user-set current/power limit, and
# the heartbeat/poll-driven re-assertion would then have nothing to
# re-apply once the charger reset those registers to its own maximum at the
# next session boundary (the exact failure mode desired_setpoints exists to
# prevent in the first place, just deferred to "after the next restart"
# instead of "after the next session"). A separate Store from the session
# one above: different lifecycle (written on every setpoint write, not just
# session start/end) and no reason to couple the two schemas together.
SETPOINTS_STORAGE_VERSION = 1


def _setpoints_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_setpoints"


# ── Energy baseline persistence (this task) ───────────────────────────────────
# The energy plausibility guard's first-reading protection (see energy_guard.py's
# ENERGY_ABS_MAX_KWH) only catches a wildly-corrupt value - the known real
# incident (raw=65800, 6580.0kWh) is nowhere near that absolute ceiling and
# sailed through unchallenged as the new trusted baseline on every restart,
# because self._last_energy is in-memory only and empty again after every
# process restart. Persisting the last-known-good raw value/timestamp lets the
# first live reading after a restart go through the normal rate-based check
# instead, the same way _session_start_wall/_session_start_total already do
# for session tracking (see async_load_session_state below).
ENERGY_STORAGE_VERSION = 1


def _energy_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_energy_baseline"


def build_device_info(entry: ConfigEntry, coordinator: "FoxESSChargerCoordinator") -> DeviceInfo:
    """Builds DeviceInfo with the model read from the charger (0x101E) when
    available, falling back to the default single-phase model string only
    if the device hasn't answered yet. Single source of truth instead of
    the same literal repeated in every platform file."""
    model = (coordinator.data or {}).get("id_model_code") or DEFAULT_MODEL
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FoxESS Charger",
        manufacturer="FoxESS",
        model=model,
    )


class FoxESSBlockAvailabilityMixin:
    """Mixin for CoordinatorEntity subclasses whose data comes from one
    specific batched register block (BLOCK_STATUS/BLOCK_CONFIG/
    BLOCK_PHASE_BOX in const.py).

    Must appear before CoordinatorEntity in the MRO (see each platform's
    entity class bases) so `super().available` resolves to
    CoordinatorEntity.available (the coordinator-wide last_update_success
    flag) - that check is still required first: on a genuine total
    connection loss the coordinator raises UpdateFailed and every entity
    must go unavailable, regardless of any individual block's freshness.
    On top of that, this also requires the entity's own block to still be
    fresh, so one stale block (e.g. the phase-switch-box probe that never
    succeeds on single-phase hardware) only takes down the entities that
    actually depend on it.

    `_block` names which block this entity depends on. None (the default)
    means "not tied to a specific block" - e.g. the transport-diagnostics
    sensor, whose counters live on the client and update regardless of
    which register blocks succeeded - and is always considered fresh.
    """
    _block: str | None = None

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.block_is_fresh(self._block)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host      = entry.data[CONF_HOST]
    port      = entry.data[CONF_PORT]
    slave_id  = entry.data[CONF_SLAVE_ID]
    scan_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)

    client      = FoxESSModbusClient(host, port, slave_id)
    store       = Store(hass, SESSION_STORAGE_VERSION, _session_storage_key(entry.entry_id))
    setpoints_store = Store(
        hass, SETPOINTS_STORAGE_VERSION, _setpoints_storage_key(entry.entry_id)
    )
    energy_store = Store(hass, ENERGY_STORAGE_VERSION, _energy_storage_key(entry.entry_id))
    coordinator = FoxESSChargerCoordinator(
        hass, client, scan_interval, store=store, setpoints_store=setpoints_store,
        energy_store=energy_store, entry_id=entry.entry_id,
    )

    # Must happen before the first refresh: an in-progress session's start
    # timestamp/baseline (and the last-completed-session record) need to be
    # in place before _fetch() runs its first status-transition check,
    # otherwise a session already underway at restart is indistinguishable
    # from one that just started.
    await coordinator.async_load_session_state()
    # Restored here too, but only bounds-checked *after* the first refresh
    # below - id_model_code isn't known yet at this point, and validating a
    # restored current/power limit against the wrong (default/fallback)
    # capability table is worse than not validating it yet.
    await coordinator.async_load_desired_setpoints()
    # Must also happen before the first refresh - the very first live
    # _sanitize_energy() call needs self._last_energy already populated so it
    # hits the normal rate-based check instead of the weak first-observation
    # path (see the ENERGY_STORAGE_VERSION comment above and
    # async_load_energy_state's docstring for the full reasoning).
    await coordinator.async_load_energy_state()
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_validate_desired_setpoints()
    await coordinator.async_start_heartbeat()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client":      client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        # Cancel+await the heartbeat before disconnecting - a heartbeat tick
        # trying to write through an already-disconnected client is a
        # wasted, confusing error, not a real failure worth logging.
        await data["coordinator"].async_stop_heartbeat()
        # Unconditional flush (bypassing the dirty-flag debounce), so a
        # clean shutdown never loses the most recent baseline even if it
        # hasn't been picked up by the next _async_update_data cycle yet.
        await data["coordinator"].async_flush_energy_state()
        await hass.async_add_executor_job(data["client"].disconnect)
    return unload_ok


class FoxESSChargerCoordinator(DataUpdateCoordinator):
    """Coordinator: pollt alle Modbus-Register des Chargers."""

    def __init__(self, hass: HomeAssistant, client: FoxESSModbusClient,
                 scan_interval: int, store: Store | None = None,
                 setpoints_store: Store | None = None,
                 energy_store: Store | None = None,
                 entry_id: str | None = None) -> None:
        self.client = client

        # This device's config entry ID - only used to scope
        # EVENT_SESSION_COMPLETED to the right device on a multi-charger
        # install (see _track_session below and device_trigger.py). None in
        # most tests, which construct the coordinator directly without going
        # through async_setup_entry - the event is simply never fired in
        # that case, same "skipped, not broken" pattern as store=None above.
        self.entry_id = entry_id

        # HA Store for session persistence (see async_load_session_state/
        # _async_persist_session_state below). None in most tests, which
        # exercise _fetch()/session tracking directly without going through
        # async_setup_entry - persistence is simply skipped in that case,
        # same as if this were a fresh install with nothing stored yet.
        self._store = store

        # HA Store for desired_setpoints persistence (see
        # async_load_desired_setpoints/_async_persist_desired_setpoints/
        # async_validate_desired_setpoints below) - same "None in most
        # tests" reasoning as _store above.
        self._setpoints_store = setpoints_store
        # Set whenever desired_setpoints changes and needs saving - mirrors
        # _session_state_dirty's pattern (see _async_update_data), so a
        # setpoint write doesn't need its own dedicated round trip to disk
        # timed independently of the poll cycle.
        self._setpoints_dirty: bool = False

        # HA Store for energy-baseline persistence (see
        # async_load_energy_state/_async_persist_energy_state/
        # async_flush_energy_state below) - same "None in most tests"
        # reasoning as _store/_setpoints_store above.
        self._energy_store = energy_store
        # Set whenever an accepted energy reading's raw value actually
        # changes, or a session boundary occurs (see _sanitize_energy) -
        # mirrors _session_state_dirty's debounce pattern (see
        # _async_update_data), so the baseline isn't written to disk on
        # every single unchanged poll.
        self._energy_state_dirty: bool = False

        # Per-counter last-known-good tracking for the energy plausibility
        # guard. Keyed by data key, value is (raw, monotonic_ts).
        self._last_energy: dict[str, tuple[int, float]] = {}

        # Per-counter rolling window of recently *accepted* raw observations
        # (see _sanitize_energy / energy_guard.check_cumulative_window) -
        # keyed by data key, value is a list of (monotonic_ts, raw_value)
        # pairs pruned to ENERGY_WINDOW_SECONDS. Catches a sustained
        # corruption of exactly one register quantum per poll, which
        # individually always passes decide_energy_reading()'s per-poll floor.
        # Stores raw values (not deltas) so session-boundary resets can clear
        # it without leaving a large negative delta in the window.
        self._energy_window: dict[str, list[tuple[float, int]]] = {}

        # Bounded record of every rejected energy read. A rejected value is
        # otherwise just dropped - and if one ever gets through (or got
        # through before the guard existed), the corrupted value is baked
        # into the recorder's cumulative sum permanently with no way to
        # reconstruct what it should have been. Keeping old/attempted/delta
        # means a correction can be computed later instead of guessed at.
        self.energy_rejections: list[dict] = []

        # Consecutive failures of the core status block. See _fetch().
        self._status_read_failures = 0

        # Per-block last-successful-read timestamp (monotonic clock), keyed
        # by BLOCK_STATUS/BLOCK_CONFIG/BLOCK_PHASE_BOX. See block_is_fresh()
        # and FoxESSBlockAvailabilityMixin above - this is what lets one
        # failed block's entities go unavailable without taking every other
        # entity down with them.
        self._block_last_success: dict[str, float] = {}
        self._block_success_count: dict[str, int] = {}

        # Desired setpoints the integration should hold across session
        # boundaries: {register: raw_value}. Populated by number entities on
        # write. The charger resets 0x3001/0x3002 to its maximum at the end
        # of every session (per spec), so without this every new session
        # silently starts at full 32 A / 7.3 kW.
        # Persisted via _setpoints_store (see async_load_desired_setpoints/
        # _async_persist_desired_setpoints/async_validate_desired_setpoints
        # below) so a HA restart doesn't forget the intent too - it used to
        # be in-memory only, which was the same "silently starts at full
        # output" failure mode as no re-assertion at all, just deferred to
        # the next restart instead of the next session.
        self.desired_setpoints: dict[int, int] = {}
        self.setpoint_reasserts = 0
        # 2.4.3: times a poll during an active, desired session read back a
        # re-asserted register different from desired_setpoints - i.e. the
        # charger reverted the cap on its own (the 2026-09-24 91s surge was
        # invisible to HA because nothing compared the two).
        self.setpoint_drift_events = 0
        # 2.4.3: heartbeat writes that failed during an active session, and
        # the flag that makes the loop retry at SETPOINT_REASSERT_MIN_INTERVAL
        # instead of waiting a full interval (one missed write at a 30s
        # interval lands exactly on the firmware's ~60s revert).
        self.heartbeat_write_failures = 0
        self._heartbeat_retry_pending = False
        self.stop_write_failures = 0
        self.stop_retry_count = 0

        # ── Background heartbeat task ────────────────────────────────────
        # Independent of the poll cycle entirely - see _heartbeat_loop()
        # below for why a poll-driven re-assertion (only triggered by
        # _fetch()'s ~10-13s cadence) can't by itself guarantee a write
        # lands inside the charger's own Command Time Validity window if
        # that window is ever configured near its documented minimum (10s).
        # (A poll-driven writer used to live directly inside _fetch() -
        # _reassert_setpoints() - but it ran unguarded on a stale snapshot
        # with no serialization against switch.py's stop path; removed in
        # favor of this heartbeat task plus the command lock below, whose
        # existing wake-on-start behavior already covers what it was for.)
        self._charging_desired: bool = False
        # Incremented by switch.py's async_turn_off as one of its very
        # first actions (same ordering/reasoning as clearing
        # _charging_desired - see that method's comment). Lets
        # _async_send_setpoint detect "a stop was issued since this write
        # was queued/started" across the await gap around the command lock
        # and the executor job, which checking _charging_desired alone
        # cannot do (TOCTOU: the flag could still read True at the instant
        # it's checked, then flip to False before the write actually
        # lands) - see _async_send_setpoint for the full explanation.
        self._charging_generation: int = 0
        # Serializes every charger-control write (start/stop/heartbeat
        # setpoint push) so at most one is ever in flight against the
        # physical charger at a time - see _async_send_setpoint,
        # async_send_start, async_send_stop below.
        self._command_lock: asyncio.Lock = asyncio.Lock()
        # Set by async_stop_heartbeat (called from async_unload_entry) before
        # draining the lock - blocks any new write from starting once shutdown
        # has begun, even one that's still waiting on the lock when it's set.
        self._shutting_down: bool = False
        # Real incident, 2026-09-18: an automation's unconditional
        # end-of-window number.set_value landed while charging was
        # intentionally stopped (battery-protection pause), and this
        # firmware treats a setpoint write as an implicit "resume
        # charging". The charger resumed on its own; _fetch()'s
        # natural-start detection below then saw an active status and (by
        # existing design, for the legitimate Plug & Charge case) set
        # _charging_desired=True, and the heartbeat sustained the
        # unwanted session for the next 52 minutes.
        #
        # Set True as one of async_turn_off's first actions (same
        # ordering as clearing _charging_desired). While True, an active
        # status observed by natural-start detection is NOT treated as
        # legitimate - it does not set _charging_desired and does not
        # wake the heartbeat, so an unexpected resume gets no protection
        # to sustain it. This is deliberately passive, not corrective:
        # deciding whether to proactively re-stop an unwanted resume is
        # home-battery-protection *policy*, which lives in the
        # ex5_charging_controller automation, not in this integration.
        #
        # Cleared on: (a) a genuinely successful explicit start
        # (async_send_start, inside the lock, alongside setting
        # _charging_desired=True - see there), or (b) the vehicle
        # physically disconnecting (cc_status != 1 in _fetch()) - once
        # unplugged, whatever charges next is unambiguously a new,
        # unrelated session, and natural-start detection can't fire
        # anyway while disconnected (status can't read active), so
        # clearing at disconnect vs. at the following reconnect is
        # behaviourally identical and simpler.
        #
        # Persisted alongside prev_status in the session Store (see
        # _async_persist_session_state/async_load_session_state) so a
        # Core restart mid-inhibit doesn't forget it and treat the very
        # next active poll as legitimate.
        self._stop_inhibit: bool = False
        # A Stop is complete only after a fresh status read confirms inactivity.
        self._stop_pending: bool = False
        self._start_confirmation_pending: bool = False
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_wake_event: asyncio.Event = asyncio.Event()
        # Last time_validity seen by _async_update_data, so it can tell
        # "changed" from "same value read again" - see that method.
        self._last_time_validity: int | float | None = None

        # Session tracking. The charger stores none of this - current_energy
        # (0x1018) resets each session and nothing survives it.
        self._session_start_ts: float | None = None       # monotonic - for in-process duration math
        self._session_start_wall: float | None = None      # wall-clock - the only one worth persisting
        self._session_start_total: int | None = None
        self._session_peak_power_raw: int = 0
        self._prev_status: int | None = None

        # The last-completed session record, kept on the coordinator itself
        # (not just inside the `data` dict last_session/_track_session()
        # sets each poll) so it has a stable home to persist from/restore
        # into independent of the data dict's per-poll churn.
        self._last_completed_session: dict | None = None

        # Set whenever a session starts or ends (see _track_session()) -
        # _async_update_data() persists to the Store only when this is set,
        # rather than writing to disk on every single poll.
        self._session_state_dirty: bool = False

        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> dict:
        was_charging_desired = self._charging_desired
        try:
            data = await self.hass.async_add_executor_job(self._fetch)
        except UpdateFailed:
            # Raised deliberately by _fetch when the charger has gone away -
            # pass it through rather than re-wrapping it in a second layer.
            raise
        except Exception as err:
            raise UpdateFailed(f"Modbus error: {err}") from err

        # _fetch() (see the status block branch below) sets _charging_desired
        # True the moment a fresh, successful status read shows an active
        # session that wasn't already flagged as desired - covers sessions
        # started via Plug & Charge or an RFID card tap, independent of HA,
        # which switch.py's async_turn_on never sees. Waking the heartbeat
        # here (not inside _fetch(), which runs off the event loop in an
        # executor job - _wake_heartbeat() is only safe to call from the
        # loop) gives that session immediate heartbeat protection rather
        # than waiting up to a full heartbeat interval for the next
        # scheduled tick to notice.
        if self._charging_desired and not was_charging_desired:
            _LOGGER.info(
                "Charging is active but wasn't started via this switch "
                "(Plug & Charge / RFID card, or already underway at "
                "startup) - heartbeat now protecting this session"
            )
            self._wake_heartbeat()

        # Command Time Validity (0x3005) can change at any time - via the
        # Command Time Validity number entity, or just because the charger
        # itself reports a different live value. The heartbeat loop only
        # reads it once per iteration (at the top of its sleep), so a
        # change made while it's mid-wait needs to wake it early rather
        # than have it finish out a sleep duration computed from the old
        # value. This runs on the event loop (unlike _fetch() above, which
        # ran in an executor job) so a direct event.set() is safe here.
        self._check_setpoint_drift(data)

        new_time_validity = data.get("time_validity")
        if new_time_validity != self._last_time_validity:
            self._last_time_validity = new_time_validity
            self._wake_heartbeat()

        if self._session_state_dirty:
            # Store.async_save is a coroutine - _fetch() itself runs
            # synchronously in an executor job and can't await it directly,
            # so the dirty flag it sets is picked up here instead, back on
            # the event loop.
            await self.async_flush_session_state()

        if self._setpoints_dirty:
            # Set by number.py's async_set_native_value on a successful
            # write to a re-assertable register - persisted here (on the
            # next update cycle, which async_set_native_value itself
            # triggers via async_request_refresh shortly after) rather than
            # from the entity directly, mirroring _session_state_dirty's
            # pattern above.
            await self.async_flush_desired_setpoints()

        if self._energy_state_dirty:
            # Set by _sanitize_energy on an accepted-and-changed reading or a
            # session boundary - persisted here on the next update cycle,
            # same debounce pattern as _session_state_dirty/_setpoints_dirty
            # above.
            await self._async_persist_energy_state()
            self._energy_state_dirty = False

        return data

    def _check_setpoint_drift(self, data: dict) -> None:
        """2.4.3: compares each re-asserted register as just polled against
        desired_setpoints while a session is active and desired. A mismatch
        means the charger dropped the cap on its own (e.g. its Command Time
        Validity lapsed) - warn, count it, and wake the heartbeat to re-send
        now rather than at its next scheduled tick. Only trusts a config
        block read from this very poll."""
        if not self._charging_desired or data.get("status") not in SESSION_ACTIVE_STATUSES:
            return
        if not self.block_is_fresh(BLOCK_CONFIG):
            return
        drifted = False
        for register, desired in self.desired_setpoints.items():
            key = REASSERTED_DATA_KEYS.get(register)
            actual = data.get(key) if key else None
            if actual is not None and actual != desired:
                drifted = True
                _LOGGER.warning(
                    "Setpoint drift on 0x%04X: charger reports %d, desired %d "
                    "- the charger dropped the cap on its own; re-sending now",
                    register, actual, desired,
                )
        if drifted:
            self.setpoint_drift_events += 1
            data["diag_setpoint_drift_events"] = self.setpoint_drift_events
            self._wake_heartbeat()

    # ── Session persistence ───────────────────────────────────────────────────

    async def async_load_session_state(self) -> None:
        """Restores in-progress-session/last-completed-session state from
        the Store. Must be awaited before the first refresh (see
        async_setup_entry) - see the module-level SESSION_STORAGE_VERSION
        comment for why this exists at all.
        """
        if self._store is None:
            return
        stored = await self._store.async_load()
        if not stored:
            return

        self._last_completed_session = stored.get("last_session")

        # 2026-09 (second audit): without this, _prev_status stays None
        # after a restart, so _track_session()'s very first post-restart
        # poll sees `None not in SESSION_ACTIVE_STATUSES` (True) and reads
        # an already-active session as a brand new transition into
        # "charging" - clobbering the session_start_wall/session_start_total
        # baseline just restored above, moments before it's even used, and
        # incorrectly resetting the session's start time/energy baseline.
        # Restoring the actual last-persisted status (rather than leaving it
        # None) lets that first poll correctly recognise "already active, no
        # transition" instead. Precision beyond active/inactive doesn't
        # matter here - _track_session only ever compares set membership
        # (SESSION_ACTIVE_STATUSES), and this is only persisted (see
        # _async_persist_session_state) at the same moments a transition
        # would already have been recorded, so it's never stale across an
        # active/inactive boundary.
        self._prev_status = stored.get("prev_status")
        # See _stop_inhibit's own comment (near __init__'s declaration) for
        # why this needs to survive a restart: a Core restart mid-inhibit
        # must not treat the very next active poll as a legitimate start.
        self._stop_inhibit = bool(stored.get("stop_inhibit", False))
        self._stop_pending = bool(stored.get("stop_pending", False))
        if self._stop_pending:
            self._charging_desired = False
            self._stop_inhibit = True

        start_wall  = stored.get("session_start_wall")
        start_total = stored.get("session_start_total")
        if start_wall is not None and start_total is not None:
            # Monotonic clocks don't survive a process restart - they're
            # relative to an arbitrary epoch that resets every time. What
            # does survive is the *elapsed* wall-clock time, which is used
            # to back-date a synthetic monotonic start so duration math in
            # _track_session() keeps working unchanged once the session
            # eventually ends. This is an approximation (wall-clock time can
            # jump, e.g. NTP correction) but is more than good enough for a
            # session-duration display.
            elapsed_wall = max(time.time() - start_wall, 0.0)
            self._session_start_ts    = time.monotonic() - elapsed_wall
            self._session_start_wall  = start_wall
            self._session_start_total = start_total
            # Peak power tracked so far genuinely doesn't survive a restart
            # (never persisted - see _track_session()) - tracking resumes
            # from 0 and will under-report the peak for whatever portion of
            # the session happened before the restart. Out of scope: the
            # task this was built against only requires the start
            # timestamp/baseline and the last-completed-session record to
            # survive a restart, not peak power specifically.
            self._session_peak_power_raw = 0
            _LOGGER.info(
                "Restored in-progress charging session from storage "
                "(started ~%.0fs ago)", elapsed_wall,
            )

    async def _async_persist_session_state(self) -> None:
        if self._store is None:
            return
        await self._store.async_save({
            "session_start_wall":  self._session_start_wall,
            "session_start_total": self._session_start_total,
            "last_session":        self._last_completed_session,
            "prev_status":         self._prev_status,
            "stop_inhibit":        self._stop_inhibit,
            "stop_pending":        self._stop_pending,
        })

    async def async_flush_session_state(self) -> None:
        """Persists the current session/inhibit state before teardown or
        return from a user action; retain dirty if it changes mid-save."""
        snapshot = (
            self._session_start_wall, self._session_start_total,
            self._last_completed_session, self._prev_status,
            self._stop_inhibit, self._stop_pending,
        )
        await self._async_persist_session_state()
        current = (
            self._session_start_wall, self._session_start_total,
            self._last_completed_session, self._prev_status,
            self._stop_inhibit, self._stop_pending,
        )
        if current == snapshot:
            self._session_state_dirty = False

    # ── Desired-setpoint persistence ────────────────────────────────────────

    async def async_load_desired_setpoints(self) -> None:
        """Restores desired_setpoints from the Store. Must be awaited before
        the first refresh (see async_setup_entry), same timing requirement
        as async_load_session_state - the background heartbeat/poll-driven
        re-assertion must have something to re-apply from the moment
        charging is next detected as active, not just from whenever the
        user next happens to touch a number entity.

        Restored values are deliberately *not* bounds-checked here - the
        detected model (id_model_code) isn't known until the first
        successful poll. See async_validate_desired_setpoints, called from
        async_setup_entry right after the first refresh completes.
        """
        if self._setpoints_store is None:
            return
        stored = await self._setpoints_store.async_load()
        if not stored:
            return
        restored = stored.get("desired_setpoints") or {}
        # JSON object keys are always strings - convert back to the int
        # register addresses desired_setpoints is keyed by everywhere else.
        try:
            self.desired_setpoints = {int(k): v for k, v in restored.items()}
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Discarding malformed restored desired_setpoints: %r", restored,
            )

    async def _async_persist_desired_setpoints(self) -> None:
        if self._setpoints_store is None:
            return
        await self._setpoints_store.async_save({
            "desired_setpoints": {str(k): v for k, v in self.desired_setpoints.items()},
        })

    async def async_flush_desired_setpoints(self) -> None:
        """Persists the current staged setpoints and clears dirty only if
        nothing changed while the storage write was in flight."""
        snapshot = dict(self.desired_setpoints)
        await self._async_persist_desired_setpoints()
        if self.desired_setpoints == snapshot:
            self._setpoints_dirty = False

    async def async_validate_desired_setpoints(self) -> None:
        """Bounds-checks desired_setpoints against the currently detected
        device's capabilities. Run once from async_setup_entry right after
        the first refresh, so id_model_code has had a chance to be read.

        A value restored from storage may not be valid for *this specific*
        charger - e.g. it was saved against a different, lower-capability
        unit, or MODEL_CAPABILITIES' figures for this model were corrected
        since it was saved - so it can't be blindly trusted just because it
        round-tripped through the Store correctly.

        An out-of-range value is never silently pushed through as-is, and
        never replaced with "the charger's maximum" either - the latter is
        the exact failure mode desired_setpoints exists to prevent (a
        session silently starting at full output), and doing it here would
        just move that failure from "after a restart with nothing restored"
        to "after a restart with something restored but wrong". Instead:
        - REG_MAX_CHARGING_CURRENT falls back to the charger's own tracked
          default (REG_DEFAULT_CURRENT / default_current_raw), which this
          integration already reads every poll and which is guaranteed to
          be a real, in-range value for whatever hardware is actually
          attached right now.
        - REG_MAX_CHARGING_POWER has no equivalent "default" register in
          this protocol to fall back to, so an out-of-range entry is
          dropped instead of guessed at. The current-limit register (which
          does have a safe fallback) still bounds the session on its own;
          dropping the power entry just means nothing re-asserts a power
          cap until the user sets one again, which is a strictly safer
          failure mode than writing a made-up number.
        """
        if not self.desired_setpoints:
            return
        data = self.data or {}
        model = data.get("id_model_code")
        default_current = data.get("default_current_raw")
        changed = False

        for register, value in list(self.desired_setpoints.items()):
            bounds = get_setpoint_bounds(register, model)
            if bounds is None:
                continue
            lo, hi = bounds
            if not isinstance(value, int) or isinstance(value, bool):
                _LOGGER.warning(
                    "Discarding malformed restored desired setpoint 0x%04X=%r",
                    register, value,
                )
                del self.desired_setpoints[register]
                changed = True
                continue
            if lo <= value <= hi:
                continue
            changed = True
            if register == REG_MAX_CHARGING_CURRENT and default_current is not None \
                    and lo <= default_current <= hi:
                _LOGGER.warning(
                    "Restored desired setpoint 0x%04X=%d is out of range "
                    "for detected model %s (valid %d-%d) - falling back to "
                    "the charger's own default current (%d), not its "
                    "maximum",
                    register, value, model, lo, hi, default_current,
                )
                self.desired_setpoints[register] = default_current
            else:
                _LOGGER.warning(
                    "Restored desired setpoint 0x%04X=%d is out of range "
                    "for detected model %s (valid %d-%d) and no safe "
                    "fallback value is available - discarding it rather "
                    "than risk an out-of-range write or falling back to "
                    "the charger's maximum",
                    register, value, model, lo, hi,
                )
                del self.desired_setpoints[register]

        if changed:
            await self._async_persist_desired_setpoints()

    # ── Energy baseline persistence ─────────────────────────────────────────

    async def async_load_energy_state(self) -> None:
        """Restores each counter's last-known-good (raw, wall-clock-ts) from
        the Store, converting the wall-clock timestamp into a synthetic
        monotonic baseline the same way async_load_session_state() does for
        _session_start_ts - monotonic clocks don't survive a process restart,
        wall-clock elapsed time does. Must be awaited before the first refresh
        (see async_setup_entry) - the very first live _sanitize_energy() call
        after a restart needs prev already populated to go through the normal
        rate-based check instead of the weak first-observation-only path (see
        energy_guard.ENERGY_ABS_MAX_KWH and this module's ENERGY_STORAGE_VERSION
        comment for why).

        Malformed/invalid stored entries are discarded per-key rather than
        aborting the whole restore - a corrupt entry for one counter must not
        also lose a good one for the other.
        """
        if self._energy_store is None:
            return
        stored = await self._energy_store.async_load()
        if not stored:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        for key, entry in stored.items():
            if key not in ENERGY_GUARDS:
                continue
            if not isinstance(entry, dict):
                continue
            raw = entry.get("raw")
            wall_ts = entry.get("wall_ts")
            # bool is a subclass of int in Python - True/False round-tripping
            # through the json-backed Store as booleans must not be silently
            # accepted as raw values 1/0.
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                _LOGGER.warning("Discarding malformed persisted energy baseline for %s: raw=%r", key, raw)
                continue
            if not isinstance(wall_ts, (int, float)) or isinstance(wall_ts, bool):
                _LOGGER.warning("Discarding malformed persisted energy baseline for %s: wall_ts=%r", key, wall_ts)
                continue
            elapsed_wall = max(now_wall - wall_ts, 0.0)
            synthetic_ts = now_mono - elapsed_wall
            self._last_energy[key] = (raw, synthetic_ts)
            _LOGGER.info(
                "Restored energy baseline for %s: raw=%d (~%.0fs old)", key, raw, elapsed_wall,
            )

    async def _async_persist_energy_state(self) -> None:
        if self._energy_store is None:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        await self._energy_store.async_save({
            key: {"raw": raw, "wall_ts": now_wall - (now_mono - ts)}
            for key, (raw, ts) in self._last_energy.items()
        })

    async def async_flush_energy_state(self) -> None:
        """Unconditional persist, bypassing the dirty-flag debounce - called
        from async_unload_entry right before the client disconnects, so a
        clean shutdown never loses the most recent baseline even if it hasn't
        been picked up by the next _async_update_data cycle yet."""
        await self._async_persist_energy_state()

    def _mark_block_success(self, block: str) -> None:
        self._block_last_success[block] = time.monotonic()
        self._block_success_count[block] = self._block_success_count.get(block, 0) + 1

    def block_success_count(self, block: str) -> int:
        """Count successful reads for block freshness confirmation."""
        return self._block_success_count.get(block, 0)

    def block_is_fresh(self, block: str | None) -> bool:
        """Whether `block`'s last successful read is still within its
        staleness window (BLOCK_STALENESS_FACTOR x the scan interval).

        `block=None` (not tied to a specific register block) is always
        fresh - see FoxESSBlockAvailabilityMixin. A block that has never
        succeeded (e.g. the phase-switch-box probe on single-phase
        hardware, which is *expected* to always fail) has no entry at all
        and is therefore never fresh, not "fresh until proven otherwise".
        """
        if block is None:
            return True
        last_success = self._block_last_success.get(block)
        if last_success is None:
            return False
        threshold = self.update_interval.total_seconds() * BLOCK_STALENESS_FACTOR
        return (time.monotonic() - last_success) <= threshold

    def block_health_snapshot(self) -> dict[str, dict]:
        """Per-block freshness snapshot, for diagnostics.py.

        Exposed as a method (rather than diagnostics reaching into
        `_block_last_success` directly) so the coordinator stays the single
        place that knows how block health is computed.
        """
        now = time.monotonic()
        snapshot = {}
        for block in (BLOCK_STATUS, BLOCK_CONFIG, BLOCK_PHASE_BOX):
            last_success = self._block_last_success.get(block)
            snapshot[block] = {
                "seconds_since_last_success": (
                    round(now - last_success, 1) if last_success is not None else None
                ),
                "fresh": self.block_is_fresh(block),
            }
        return snapshot

    # ── Background heartbeat task ─────────────────────────────────────────────
    # Background heartbeat task design adapted from a third-party PR by
    # github.com/loadrunner42 (PR #2 on andrewmatten/foxess-ev-charger), in
    # turn based on evcc-io/evcc's foxess-evc.go driver convention - see
    # CHANGELOG.md. (Distinct from get_heartbeat_interval() in const.py,
    # whose interval math was already adopted from the same PR in a prior
    # commit - this is the actual task that uses it.)
    #
    # A poll-driven setpoint writer used to run only inside _fetch(), which is
    # driven by the poll cycle (~10-13s per DEFAULT_SCAN_INTERVAL). If
    # Command Time Validity (0x3005) were ever set to its own documented
    # minimum (10s), the required heartbeat (5s, per get_heartbeat_interval)
    # can't be met by something that only checks once per poll. This task
    # runs independently on its own schedule instead, so the guarantee holds
    # regardless of how slow or irregular the poll cycle is.

    async def async_start_heartbeat(self) -> None:
        """Starts the background heartbeat task. Called from
        async_setup_entry after the first refresh succeeds (so self.data is
        populated - the loop's first interval calculation needs a real
        time_validity to work with, not just the DEFAULT_TIME_VALIDITY
        placeholder)."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._last_time_validity = (self.data or {}).get("time_validity")
        # If HA (re)started mid-session, the charger already reports an
        # active status even though no switch.async_turn_on happened this
        # process - the heartbeat must protect that session too, not only
        # ones started after this task exists.
        #
        # `and not self._stop_inhibit` - found in review, 2026-09-18: without
        # this, a Core restart during an active _stop_inhibit (persisted
        # specifically so a restart wouldn't forget an intentional stop -
        # see that flag's own comment) re-armed _charging_desired here
        # unconditionally from the live status, reopening the exact
        # incident this whole fix exists to close: restart while an
        # unexpected implicit-resume is ongoing would have the heartbeat
        # sustain it right back, exactly as if the inhibit had never been
        # persisted at all. async_load_session_state() (which restores
        # _stop_inhibit) always runs before this method - see
        # async_setup_entry.
        self._charging_desired = (
            (self.data or {}).get("status") in SESSION_ACTIVE_STATUSES
            and not self._stop_inhibit
        )
        self._heartbeat_task = self.hass.loop.create_task(
            self._heartbeat_loop(), name=f"{DOMAIN}_heartbeat"
        )

    async def async_stop_heartbeat(self) -> None:
        """Cancels the background heartbeat task and waits for it to
        actually finish, so no tick can still be in flight (e.g. mid-write)
        once this returns. Called from async_unload_entry, before the
        client disconnects.

        Also flips _shutting_down and drains the command lock - cancelling
        the heartbeat task only guarantees *that* task isn't mid-write;
        switch.py's async_turn_on/async_turn_off run as their own separate
        tasks off the event loop's service-call dispatch and could still be
        holding (or about to acquire) the same lock independently.
        """
        self._shutting_down = True
        self._charging_generation += 1
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Drains any write that's still mid-flight (holding the lock) - by
        # the time this acquire succeeds, nothing is touching self.client
        # anymore, so async_unload_entry's subsequent client.disconnect() is
        # safe.
        async with self._command_lock:
            pass

    def _wake_heartbeat(self) -> None:
        """Wakes the heartbeat loop early instead of leaving it to finish
        out its current sleep. Only safe to call from the event loop -
        every current call site (switch.py/number.py entity methods,
        _async_update_data above) runs there; _fetch() itself does not and
        must never call this directly."""
        self._heartbeat_wake_event.set()

    async def _heartbeat_loop(self) -> None:
        # Every iteration is wrapped, because this loop *is* the safety
        # guarantee: if it ever raised, the task would end silently and the
        # charger would revert 0x3001/0x3002 to maximum at the end of the
        # current Command Time Validity window, with nothing left running
        # to notice or say so - and it would stay dead for every subsequent
        # session too, until HA itself restarted. _heartbeat_tick() already
        # guards its own register writes individually (see there); this is
        # the outer net for everything else in the iteration, including the
        # interval calculation and the wait itself.
        while True:
            try:
                interval = get_heartbeat_interval((self.data or {}).get("time_validity"))
                if self._heartbeat_retry_pending:
                    interval = SETPOINT_REASSERT_MIN_INTERVAL
                try:
                    await asyncio.wait_for(
                        self._heartbeat_wake_event.wait(), timeout=interval
                    )
                except asyncio.TimeoutError:
                    pass
                finally:
                    # Cleared whether the wait timed out (normal schedule) or
                    # was woken early - either way the reason for this tick has
                    # now been consumed, and clearing before wait() is checked
                    # again next loop is what lets a *future* wake actually be
                    # noticed instead of instantly resolving on stale state.
                    self._heartbeat_wake_event.clear()
                await self._heartbeat_tick()
            except asyncio.CancelledError:
                # Deliberate shutdown (async_stop_heartbeat on unload) - must
                # propagate, or the task would never actually stop and the
                # await in async_stop_heartbeat would hang forever.
                raise
            except Exception:
                _LOGGER.exception(
                    "FoxESS heartbeat iteration failed; the loop is still "
                    "running and will retry"
                )
                # Backs off to the conservative default interval rather than
                # retrying at SETPOINT_REASSERT_MIN_INTERVAL: a persistent
                # failure here (a bad time_validity type, say) would
                # otherwise spin at 3s intervals writing a full traceback
                # each time. Still short enough to resume normal heartbeats
                # promptly once whatever broke has recovered.
                await asyncio.sleep(DEFAULT_TIME_VALIDITY / 2)

    async def _async_send_setpoint(self, register: int, value: int, generation: int) -> bool:
        """Writes one setpoint register, holding self._command_lock for the
        full check-then-write. Every gate _heartbeat_tick used to check once,
        outside any lock, is re-checked here again, immediately before the
        write, *inside* the lock - a queued call that reaches the lock after a
        stop already ran must see the post-stop state and abort, not the state
        that was true when it started waiting.
        """
        async with self._command_lock:
            if self._shutting_down:
                return False
            if self._charging_generation != generation or not self._charging_desired:
                return False
            data = self.data or {}
            if data.get("status") not in SESSION_ACTIVE_STATUSES:
                return False
            # 2.4.3: no block-freshness or alarm gate here any more. Going
            # silent is not a safe failure - the charger reverts to maximum
            # when the heartbeat stops. A stale block or a non-fatal alarm
            # keeps the cap in place; a hard fault is handled by
            # _heartbeat_tick sending a stop instead.
            if data.get("active_faults"):
                return False
            try:
                success = await self.hass.async_add_executor_job(
                    self.client.write_holding_register, register, value
                )
            except Exception:
                _LOGGER.exception("Command write crashed writing 0x%04X", register)
                return False
            if success:
                data_key = REASSERTED_DATA_KEYS.get(register)
                if data_key:
                    data[data_key] = value
                self.setpoint_reasserts += 1
            else:
                _LOGGER.warning(
                    "Command write failed for 0x%04X (desired %d)", register, value,
                )
            return success

    async def async_send_start(self) -> bool:
        """Sends the start-charging command, holding self._command_lock for
        the duration - serialized against any in-flight stop/heartbeat
        write, same as _async_send_setpoint above.

        Sets _charging_desired=True on success, INSIDE this same locked
        section, rather than leaving that to switch.py after this returns.
        _stop_inhibit/_stop_pending clear only after a fresh active status
        confirms the start. Closes a real race: switch.py's
        async_turn_off clears _charging_desired and bumps the generation
        as its own first two (synchronous, lock-free) statements, so a
        concurrent turn_off's Task can run those the moment this method's
        executor-job await yields control - i.e. potentially *before* this
        method's own write even completes. If the desired-flag flip then
        happened back in switch.py, after this method already returned, it
        would unconditionally stomp turn_off's False back to True with no
        way to tell a stop had just been requested. Checking the
        generation is unchanged since entering the lock closes that gap:
        if it moved, a stop is already queued right behind this one and
        will send the authoritative final command shortly - don't re-arm
        desired state a stop is about to contradict.
        """
        generation_before = self._charging_generation
        async with self._command_lock:
            if self._shutting_down:
                return False
            if self._charging_generation != generation_before:
                return False
            # Program staged current/power limits before the explicit Start
            # command. This is the strongest ordering the protocol permits;
            # on this firmware a setpoint write can itself resume charging,
            # so the protocol provides no provable zero-draw interval before
            # the cap takes effect. A failed cap write aborts explicit Start.
            for register, value in list(self.desired_setpoints.items()):
                try:
                    cap_ok = await self.hass.async_add_executor_job(
                        self.client.write_holding_register, register, value
                    )
                except Exception:
                    _LOGGER.exception(
                        "Pre-start setpoint write raised for 0x%04X; Start aborted",
                        register,
                    )
                    await self._async_stop_after_uncertain_start()
                    return False
                if not cap_ok:
                    _LOGGER.error(
                        "Pre-start setpoint write failed for 0x%04X; Start aborted",
                        register,
                    )
                    await self._async_stop_after_uncertain_start()
                    return False
                self.setpoint_reasserts += 1
                if self._charging_generation != generation_before:
                    return False
            try:
                success = await self.hass.async_add_executor_job(
                    self.client.write_holding_register, REG_CHARGING_CONTROL, 1
                )
            except Exception:
                _LOGGER.exception(
                    "Start command raised; charger state is uncertain, issuing Stop"
                )
                await self._async_stop_after_uncertain_start()
                return False
            if not success:
                _LOGGER.error(
                    "Start command failed; charger state is uncertain, issuing Stop"
                )
                await self._async_stop_after_uncertain_start()
                return False
            if success and self._charging_generation == generation_before:
                self._charging_desired = True
                return True
            return False

    async def _async_stop_after_uncertain_start(self) -> None:
        """A failed/uncertain pre-start cap or Start write may have taken
        effect before its response was lost. Issue Stop under the already-
        held command lock and retain the retry latch until confirmed."""
        self._charging_desired = False
        self._charging_generation += 1
        self._stop_inhibit = True
        self._stop_pending = True
        self._heartbeat_retry_pending = True
        self._session_state_dirty = True
        try:
            success = await self.hass.async_add_executor_job(
                self.client.write_holding_register, REG_CHARGING_CONTROL, 2
            )
        except Exception:
            _LOGGER.exception(
                "Stop after uncertain pre-start setpoint failure raised; retry remains pending"
            )
            success = False
        if not success:
            self.stop_write_failures += 1
            _LOGGER.error(
                "Stop after uncertain pre-start setpoint failure was not confirmed; "
                "charging may remain active and its cap may expire"
            )
        self._wake_heartbeat()
        try:
            await self.async_flush_session_state()
        except Exception:
            _LOGGER.exception(
                "Could not persist pending Stop after uncertain pre-start setpoint"
            )

    async def async_send_stop(self) -> bool:
        """Sends the stop-charging command, holding self._command_lock for
        the duration. Deliberately does NOT check _shutting_down/generation/
        desired - stop's entire purpose is to change that state, and it must
        always be allowed through as long as the lock itself is available."""
        async with self._command_lock:
            try:
                return await self.hass.async_add_executor_job(
                    self.client.write_holding_register, REG_CHARGING_CONTROL, 2
                )
            except Exception:
                _LOGGER.exception("FoxESS Stop command raised; stop remains pending")
                return False

    async def async_set_desired_setpoint(self, register: int, value: int) -> bool | None:
        """Single entry point for a user-initiated setpoint change
        (number.py, for the two REASSERTED_REGISTERS only). Always records
        the desired value first - it's the source of truth for the *next*
        session regardless of whether a physical write happens right now -
        then only submits the physical write when charging is genuinely,
        currently active, decided fresh *inside* the command lock,
        atomically with the write itself.

        Real incident, 2026-09-18: writing 0x3001/0x3002 is itself an
        implicit "resume charging" on this firmware (see switch.py's
        async_turn_off comment). An automation's unconditional
        number.set_value landed while charging was intentionally paused
        (_charging_desired already False) and silently resumed a session
        the heartbeat then sustained for 52 minutes. The physical write is
        now unconditionally skipped whenever charging isn't desired - the
        saved value gets pushed automatically the moment charging next
        actually starts (async_send_start wakes the heartbeat via
        switch.py, which re-asserts every entry in desired_setpoints, same
        mechanism a fresh session already relies on).

        Returns None when the write was correctly skipped (not an error -
        callers must not treat this as a failure), True/False for whether
        an attempted physical write itself succeeded. This distinction is
        why this method doesn't reuse _async_send_setpoint's gate stack
        directly (that method's plain bool return can't distinguish
        "genuinely failed" from "correctly not attempted", which the
        heartbeat's own call site never needed to tell apart but a
        user-facing write does - number.py must raise on a real failure
        and stay silent on a deliberate skip).
        """
        self.desired_setpoints[register] = value
        self._setpoints_dirty = True
        # Wakes the heartbeat to push this value soon rather than waiting
        # for its next scheduled tick - safe to call here, this method
        # only ever runs on the event loop (an entity service-call
        # handler awaits it directly). A no-op if charging isn't actually
        # active right now - the heartbeat's own gates decide that
        # independently, same as the immediately-following gated write
        # below.
        self._wake_heartbeat()

        async with self._command_lock:
            if self._shutting_down:
                return None
            if not self._charging_desired:
                return None
            data = self.data or {}
            if data.get("status") not in SESSION_ACTIVE_STATUSES:
                return None
            if not self.block_is_fresh(BLOCK_CONFIG) or not self.block_is_fresh(BLOCK_STATUS):
                return None
            if data.get("active_faults") or data.get("active_alarms"):
                return None
            try:
                success = await self.hass.async_add_executor_job(
                    self.client.write_holding_register, register, value
                )
            except Exception:
                _LOGGER.exception("Command write crashed writing 0x%04X", register)
                return False
            if success:
                data_key = REASSERTED_DATA_KEYS.get(register)
                if data_key:
                    data[data_key] = value
                self.setpoint_reasserts += 1
            else:
                _LOGGER.warning(
                    "Command write failed for 0x%04X (desired %d)", register, value,
                )
            return success

    async def _heartbeat_tick(self) -> None:
        """One heartbeat attempt: proactively re-pushes desired_setpoints so
        the charger's Command Time Validity window never lapses - runs on
        its own schedule rather than only when a drift-triggered poll-driven
        writer happens to fire during a poll (removed - see
        _async_send_setpoint/the command-lock comment near _charging_desired
        above).

        Several independent gates, checked fresh every tick since any of
        them can change between ticks. Failing one is a normal, expected
        condition (e.g. the car unplugged since the last tick) - not an
        error - so this returns silently rather than logging anything.

        The top-level gates here are a cheap early-exit before ever touching
        the lock - no behavior change from before. The per-register
        generation re-check that used to live inline here now lives inside
        _async_send_setpoint, run again fresh *inside* the lock, which is
        strictly stronger than before: previously the re-check happened
        outside any lock, so a stop could still land in the gap between the
        check and the write starting; now the check and the write are atomic
        with respect to the lock.
        """
        self._heartbeat_retry_pending = False
        if self._start_confirmation_pending:
            return
        if self._stop_pending:
            # While Stop is unconfirmed, retry it at the short heartbeat
            # cadence. A cap write could implicitly resume charging.
            self.stop_retry_count += 1
            success = await self.async_send_stop()
            if not success:
                self.stop_write_failures += 1
                _LOGGER.error(
                    "FoxESS Stop retry failed; charging may remain active and its cap may expire"
                )
            self._heartbeat_retry_pending = True
            return
        if not self._charging_desired:
            return
        if not self.desired_setpoints:
            return
        data = self.data or {}
        if data.get("status") not in SESSION_ACTIVE_STATUSES:
            return
        # 2.4.3: block freshness and active alarms no longer silence the
        # heartbeat. Every earlier gate here failed *unsafe*: the charger
        # reverts 0x3001/0x3002 to maximum once writes stop, so "stale
        # config block" or "phase_loss alarm" used to mean "uncapped". Keep
        # capping on the last known status instead. A hard fault is the one
        # case where re-asserting a limit is the wrong response - stop the
        # session outright rather than either go silent or keep it going.
        if data.get("active_faults"):
            _LOGGER.warning(
                "Charger reports active fault(s) %s during a session - "
                "sending stop instead of re-asserting the charge limit",
                data["active_faults"],
            )
            self._charging_desired = False
            self._charging_generation += 1
            self._stop_pending = True
            self._stop_inhibit = True
            self._session_state_dirty = True
            if not await self.async_send_stop():
                self.stop_write_failures += 1
            self._heartbeat_retry_pending = True
            return

        generation = self._charging_generation
        # list(...) snapshot: number.py's async_set_native_value can mutate
        # desired_setpoints while this loop is suspended at the await below.
        for register, desired in list(self.desired_setpoints.items()):
            ok = await self._async_send_setpoint(register, desired, generation)
            if not ok and self._charging_desired and self._charging_generation == generation:
                # Failed write during a still-desired session: retry at
                # SETPOINT_REASSERT_MIN_INTERVAL, not a full interval.
                self.heartbeat_write_failures += 1
                self._heartbeat_retry_pending = True

    def _fetch(self) -> dict:
        # Start from the last known-good values instead of a blank dict, so a
        # single failed register-block read doesn't wipe out everything else
        # that's still valid (e.g. right after a write, before the charger's
        # ready to answer the follow-up read).
        data: dict = dict(self.data) if self.data else {}

        # On the very first fetch after an HA restart, self.data is still
        # None (nothing has succeeded yet this process), so the seed above
        # is empty - the persisted last-completed-session record (restored
        # by async_load_session_state(), called before the first refresh)
        # needs to be re-seeded here explicitly, otherwise the Last Session
        # Energy/Duration sensors would show unknown until the *next*
        # session completes, discarding a perfectly good historical record
        # for no reason.
        if "last_session" not in data and self._last_completed_session is not None:
            data["last_session"] = self._last_completed_session

        # ── 0x1000–0x101D: Status + Energy/Fault/RFID block (batched) ────────
        # Merged into a single FC03 read as of 2.1.3 - previously 5 separate
        # Modbus round trips per poll (the 22-register status block plus 4
        # individual UINT32 reads for total_energy/current_energy/fault_code/
        # rfid_card). Confirmed safe: unlike 0x3000-0x300B, nothing in this
        # range is phase-switch-box-only, so this cannot reproduce the Round
        # 2 Illegal Data Address bug (see REG_STATUS_BLOCK_START/COUNT in
        # const.py and the 0x300A-0x300B block below, which stays split out).
        regs = self.client.read_registers(REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)
        if regs and len(regs) >= REG_STATUS_BLOCK_COUNT:
            self._status_read_failures = 0
            self._mark_block_success(BLOCK_STATUS)
            data["device_address"]  = regs[0]
            data["software_version"]= regs[1]
            data["stop_reason"]     = regs[2]
            data["status"]          = regs[3]
            data["cp_status"]       = regs[4]
            data["cc_status"]       = regs[5]
            data["port_temp_raw"]   = regs[6]
            data["ambient_temp_raw"]= regs[7]
            data["l1_voltage_raw"]  = regs[8]
            data["l2_voltage_raw"]  = regs[9]
            data["l3_voltage_raw"]  = regs[10]
            data["l1_current_raw"]  = regs[11]
            data["l2_current_raw"]  = regs[12]
            data["l3_current_raw"]  = regs[13]
            data["power_raw"]       = regs[14]
            data["lock_status"]     = regs[15]
            data["phase_sequence"]  = regs[16]
            data["max_power_raw"]   = regs[17]
            data["min_power_raw"]   = regs[18]
            data["max_current_raw"] = regs[19]
            data["min_current_raw"] = regs[20]
            data["alarm_code"]      = regs[21]

            # This charger can start charging on its own - Plug & Charge
            # mode or an RFID card tap - independent of HA. switch.py's
            # async_turn_on is the only other place that sets
            # _charging_desired, so a naturally-started session would
            # otherwise get no heartbeat protection at all until the user
            # happened to toggle the switch themselves. Only set here, never
            # cleared here (clearing is switch.py's async_turn_off's job,
            # first-statement-before-the-stop-write, for the stop-race fix -
            # see that method) - and only inside this specific `if regs and
            # ...` branch, i.e. only on a poll where this exact status read
            # just succeeded, not from data carried over by the
            # seed-from-last-known-good pattern when this block's read
            # failed this cycle. That's deliberately a stricter check than
            # block_is_fresh(BLOCK_STATUS) (which tolerates a few stale
            # polls) - a session should only be newly flagged as desired
            # from a read that is fresh *this instant*, not "fresh enough".
            #
            # `not self._stop_inhibit` - see that flag's own comment near
            # __init__'s declaration: while HA has just intentionally
            # stopped this charger, an active status must NOT be treated
            # as a legitimate Plug & Charge start, or the heartbeat would
            # sustain an unwanted implicit-resume (the 2026-09-18 incident
            # this exists to prevent).
            if self._stop_pending and data["status"] not in SESSION_ACTIVE_STATUSES:
                self._stop_pending = False
                self._heartbeat_retry_pending = False
                self._session_state_dirty = True

            if (
                data["status"] in SESSION_ACTIVE_STATUSES
                and not self._charging_desired
                and not self._stop_inhibit
            ):
                self._charging_desired = True

            # The vehicle physically unplugging (cc_status != 1) ends
            # whatever session/inhibit was in effect unambiguously - see
            # _stop_inhibit's own comment for why clearing here (rather
            # than waiting for the subsequent reconnect) is equivalent and
            # simpler: status can't read active while disconnected, so
            # natural-start detection above can't fire in the gap either
            # way.
            if self._stop_inhibit and data.get("cc_status") is not None \
                    and data["cc_status"] != 1:
                self._stop_inhibit = False
                self._session_state_dirty = True

            # UINT32 fields packed as two consecutive registers each, indexed
            # off the same batch via their addresses from const.py rather
            # than bare literals - these were duplicated as magic numbers
            # until 2.1.2, so fixing the swapped energy registers in const.py
            # alone had no effect on what was actually read.
            for key, addr in [
                ("total_energy_raw",   REG_TOTAL_ENERGY),
                ("current_energy_raw", REG_CURRENT_ENERGY),
                ("fault_code",         REG_FAULT_CODE),
                ("rfid_card",          REG_RFID_CARD),
            ]:
                idx = addr - REG_STATUS_BLOCK_START
                val = (regs[idx] << 16) | regs[idx + 1]
                if key in ENERGY_GUARDS:
                    val = self._sanitize_energy(key, val, data)
                    if val is None:
                        continue
                data[key] = val
        else:
            # The seed-from-last-known-good pattern above is right for one
            # failed block, but on its own it has no upper bound: every read
            # can fail forever and this method still returns a "successful"
            # dict of stale values, so last_update_success stays True and
            # every entity stays `available`. Pulling the charger's power
            # would have left HA displaying yesterday's numbers as current,
            # indefinitely, with nothing but a log warning. The core status
            # block is the liveness signal - if it can't be read N times
            # running, the charger is gone and entities must say so.
            self._status_read_failures += 1
            _LOGGER.warning(
                "Could not read status/energy/fault registers 0x1000–0x101D (%d consecutive)",
                self._status_read_failures,
            )
            if self._status_read_failures >= MAX_STATUS_READ_FAILURES:
                raise UpdateFailed(
                    f"Charger unreachable: core status block failed "
                    f"{self._status_read_failures} polls in a row"
                )

        # ── 0x3000–0x3006: R/W Config Register (single-phase-safe core block) ──
        # Split from the phase-switch-box block below because a single failed
        # register in one read fails the *entire* Modbus request (Illegal Data
        # Address) - on single-phase hardware (e.g. A7300P1-E-B-WO), 0x300A/
        # 0x300B (phase-switch-box only) don't exist in firmware, which was
        # taking down this whole block - including work_mode, max charging
        # current/power, allowed charge time/energy, and time validity - even
        # though those registers are all readable on their own.
        cfg = self.client.read_registers(0x3000, 7)
        if cfg and len(cfg) >= 7:
            self._mark_block_success(BLOCK_CONFIG)
            data["work_mode"]                = cfg[0]
            data["max_charging_current_raw"] = cfg[1]
            data["max_charging_power_raw"]   = cfg[2]
            data["allowed_charge_time"]      = cfg[3]
            data["allowed_charge_energy"]    = cfg[4]
            data["time_validity"]            = cfg[5]
            data["default_current_raw"]      = cfg[6]
        else:
            _LOGGER.warning("Could not read config registers 0x3000–0x3006")

        # ── 0x300A–0x300B: Phase-Switch-Box Register (three-phase only) ────────
        # Expected to fail on single-phase hardware where these registers
        # aren't implemented - that's fine, it's independent of the read above.
        phase_cfg = self.client.read_registers(0x300A, 2, quiet=True)
        if phase_cfg and len(phase_cfg) >= 2:
            self._mark_block_success(BLOCK_PHASE_BOX)
            data["auto_phase_switch"]   = phase_cfg[0]
            data["min_switch_interval"] = phase_cfg[1]
        else:
            _LOGGER.debug("Could not read phase-switch-box registers 0x300A–0x300B (expected on single-phase hardware)")

        # ── 0x101E/0x1022: Id Model Code / Id Serial Number (ASCII, static) ────
        # Read once and cached forever via the seed-from-last-known-good
        # pattern above - these don't change, no need to re-poll every cycle.
        if not data.get("id_model_code"):
            model = self.client.read_ascii(0x101E, 4)
            if model:
                data["id_model_code"] = model
        if not data.get("id_serial_number"):
            serial = self.client.read_ascii(0x1022, 16)
            if serial:
                data["id_serial_number"] = serial

        # ── Fault/Alarm bitmask decode ──────────────────────────────────────────
        # fault_code/alarm_code are bitmasks (Appendix 2/3) - multiple
        # conditions can be active at once. Decode into readable name lists
        # for the sensors' extra_state_attributes instead of a raw integer.
        data["active_faults"] = decode_bitmask(data.get("fault_code"), FAULT_BITS, "fault_code")
        data["active_alarms"] = decode_bitmask(data.get("alarm_code"), ALARM_BITS, "alarm_code")

        self._track_session(data)
        self._add_diagnostics(data)

        return data

    # ── Session tracking ──────────────────────────────────────────────────────

    def _track_session(self, data: dict) -> None:
        """Derives session start/end from status transitions.

        None of this comes from the charger - current_energy (0x1018) resets
        each session, stop_reason (0x1002) is overwritten before anyone sees
        it, and nothing records when a session began or how long it ran.
        """
        status = data.get("status")
        if status is None:
            return

        was_active = self._prev_status in SESSION_ACTIVE_STATUSES
        is_active  = status in SESSION_ACTIVE_STATUSES
        now        = time.monotonic()

        if is_active and not was_active:
            self._session_start_ts       = now
            self._session_start_wall     = time.time()
            self._session_start_total    = data.get("total_energy_raw")
            self._session_peak_power_raw = 0
            data["session_start"] = datetime.now(timezone.utc).isoformat()
            _LOGGER.debug("Charging session started (status=%s)", status)
            # Persist the start baseline now, not just at session end - a
            # restart mid-session must not lose it (see
            # async_load_session_state/_async_persist_session_state).
            self._session_state_dirty = True

        if is_active:
            self._session_peak_power_raw = max(
                self._session_peak_power_raw, data.get("power_raw", 0) or 0
            )

        if was_active and not is_active and self._session_start_ts is not None:
            duration_s = now - self._session_start_ts
            # Prefer the lifetime-counter delta: current_energy has usually
            # already reset by the time we see the status leave the active
            # set, so reading it here would report ~0 for every session.
            energy_raw = None
            end_total  = data.get("total_energy_raw")
            if self._session_start_total is not None and end_total is not None:
                delta = end_total - self._session_start_total
                if delta >= 0:
                    energy_raw = delta
            if energy_raw is None:
                energy_raw = data.get("current_energy_raw", 0) or 0

            hours = duration_s / 3600
            data["last_session"] = {
                "ended":            datetime.now(timezone.utc).isoformat(),
                "duration_min":     round(duration_s / 60, 1),
                "energy_kwh":       round(energy_raw * 0.1, 2),
                "avg_power_kw":     round((energy_raw * 0.1) / hours, 2) if hours > 0 else 0,
                "peak_power_kw":    round(self._session_peak_power_raw * 0.1, 2),
                "stop_reason":      STOP_REASON_MAP.get(data.get("stop_reason", 0), "unknown"),
            }
            _LOGGER.debug("Charging session ended: %s", data["last_session"])
            self._last_completed_session = data["last_session"]
            # Fired only here, at the exact moment a genuine active ->
            # inactive transition is detected - never as a side effect of
            # restoring persisted state (see async_load_session_state/
            # _fetch()'s last_session re-seeding, neither of which calls
            # _track_session at all) and never suppressed by two sessions
            # sharing an identical duration (this fires on the transition
            # itself, not on any value comparison). See device_trigger.py's
            # session_completed trigger, the actual consumer.
            # self.hass.bus.fire() (not async_fire) is the thread-safe
            # variant - _track_session runs inside _fetch(), which itself
            # runs in an executor job off the event loop.
            if self.entry_id is not None:
                self.hass.bus.fire(
                    EVENT_SESSION_COMPLETED,
                    {"entry_id": self.entry_id, **data["last_session"]},
                )
            self._session_start_ts    = None
            self._session_start_wall  = None
            self._session_start_total = None
            # Clear the in-progress start baseline in storage and persist
            # the newly-completed session record.
            self._session_state_dirty = True

        data["session_active"] = is_active
        self._prev_status = status

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def _add_diagnostics(self, data: dict) -> None:
        data["diag_txid_mismatches"]      = self.client.txid_mismatches
        data["diag_short_reads"]          = self.client.short_reads
        data["diag_connection_errors"]    = self.client.connection_errors
        data["diag_malformed_headers"]    = self.client.malformed_headers
        data["diag_unit_id_mismatches"]   = self.client.unit_id_mismatches
        data["diag_write_echo_mismatches"] = self.client.write_echo_mismatches
        data["diag_energy_rejections"]    = len(self.energy_rejections)
        data["diag_setpoint_reasserts"]   = self.setpoint_reasserts
        data["diag_setpoint_drift_events"] = self.setpoint_drift_events
        data["diag_heartbeat_write_failures"] = self.heartbeat_write_failures
        data["diag_stop_pending"] = self._stop_pending
        data["diag_stop_write_failures"] = self.stop_write_failures
        data["diag_stop_retries"] = self.stop_retry_count
        data["energy_rejection_log"]      = self.energy_rejections[-5:]

    # ── Energy plausibility guard ─────────────────────────────────────────────

    def _sanitize_energy(self, key: str, raw: int, data: dict) -> int | None:
        """Guards an energy counter against bad Modbus reads.

        Thin stateful wrapper: owns _last_energy / _energy_window /
        energy_rejections and the datetime/logging side effects, and
        delegates the actual "is this raw value plausible" decisions to
        energy_guard.decide_energy_reading()/check_cumulative_window() -
        pure functions with zero HA dependencies, so those decisions can be
        unit tested directly (see tests/test_energy_guard.py). See
        energy_guard.py's module docstring for the incident history and
        reasoning behind the thresholds themselves.
        """
        allow_decrease = ENERGY_GUARDS[key]["allow_decrease"]
        now = time.monotonic()
        prev_entry = self._last_energy.get(key)
        prev, prev_ts = prev_entry if prev_entry is not None else (None, None)
        # Rated max power for this specific charger (see MODEL_CAPABILITIES
        # in const.py) rather than a hardcoded single-phase 7.3kW - the
        # live max_power_raw reading is preferred when available (it can
        # legitimately be lower, e.g. a derated/current-limited install),
        # falling back to the model's rated max only when that reading is
        # itself missing.
        rated_max_kw = get_capabilities(data.get("id_model_code"))["max_power_kw"]
        # 2.4.3: capped at 1.5x rated - a garbage max_power_raw (e.g. 0xFFFF
        # = 6553.5kW) used to widen the guard until it accepted anything.
        live_max_kw = (data.get("max_power_raw") or rated_max_kw * 10) * 0.1
        max_power_kw = max(min(live_max_kw, rated_max_kw * 1.5), rated_max_kw)

        # 2026-09 (second audit): the coordinator's own session-tracking
        # state already knows when a real session boundary occurs - this is
        # the exact same "just transitioned into an active status" check
        # _track_session() performs moments later in _fetch() (this method
        # runs first, before _track_session() updates self._prev_status), so
        # a current_energy_raw decrease that coincides with a genuine
        # session start is trusted even if the new value isn't itself at/
        # near zero (see decide_energy_reading's session_boundary param).
        session_boundary = (
            self._prev_status not in SESSION_ACTIVE_STATUSES
            and data.get("status") in SESSION_ACTIVE_STATUSES
        )

        decision = decide_energy_reading(
            key, raw, prev, prev_ts, now, max_power_kw, allow_decrease,
            session_boundary=session_boundary,
        )

        if not decision.accepted:
            self._record_energy_rejection(key, prev, raw, decision)
            # Deliberately does not update the timestamp: leaving it means
            # the plausible window keeps widening, so a genuine step change
            # is eventually accepted instead of being rejected forever.
            return None

        # Sustained-corruption check: every individual delta in a rolling
        # window can pass the per-poll decision above (e.g. exactly one
        # register quantum every poll, forever) while the window's SUM is a
        # rate no real charger could sustain. Checked only once the per-poll
        # decision itself accepts - a per-poll rejection already means this
        # reading isn't trusted as a new baseline anyway.
        #
        # 2026-09 (second audit): the window now stores raw observations
        # (timestamp, raw_value) instead of (timestamp, delta_kwh) pairs.
        # This fixes Bug B (session-boundary reset's large negative delta
        # poisoning the window for 30 minutes) structurally: a session-
        # boundary reset is detected at the accept-decision stage, and the
        # window is cleared/reseeded here so the reset's "delta" (which would
        # be a huge negative number) never enters it at all. For
        # non-boundary-reset readings, we derive the cumulative delta from
        # the window's own boundary samples (first/last raw values), which
        # fixes Bug A too: there's no possibility of a delta whose interval
        # began before the window's own earliest remaining timestamp.
        if allow_decrease and (session_boundary or raw <= NEAR_ZERO_RAW_UNITS):
            # A legitimate session-boundary reset (e.g. current_energy_raw ->
            # near-zero) is not itself evidence about the sustained *rate* the
            # guard is checking, and its huge negative delta must never sit in
            # the window offsetting a later corrupt read for up to 30 minutes
            # (see this task's Bug B). Clear and reseed with just the new value.
            # Only applies to counters that allow resets (current_energy_raw),
            # not lifetime counters (total_energy_raw).
            self._energy_window[key] = [(now, raw)]
            if session_boundary:
                # Belt and braces: a session-boundary reset always changes
                # the raw value anyway (so the value-changed check below
                # would catch it too), but persisting promptly at session
                # boundaries is an explicit requirement in its own right -
                # made unconditional here rather than relying on that check.
                self._energy_state_dirty = True
        else:
            window = self._energy_window.setdefault(key, [])
            window.append((now, raw))
            cutoff = now - ENERGY_WINDOW_SECONDS
            while window and window[0][0] < cutoff:
                window.pop(0)

            if len(window) >= ENERGY_WINDOW_MIN_SAMPLES:
                window_elapsed = window[-1][0] - window[0][0]
                window_total_kwh = (window[-1][1] - window[0][1]) * ENERGY_QUANTUM_KWH
                if check_cumulative_window(window_total_kwh, window_elapsed, max_power_kw):
                    _LOGGER.warning(
                        "Rejected %s: sustained rate over the last %.0fs (%.2f "
                        "kWh accepted) implausibly tracks the per-poll "
                        "acceptance edge - likely a repeating one-quantum-per-"
                        "poll corruption, not real charging",
                        key, window_elapsed, window_total_kwh,
                    )
                    self._record_energy_rejection(key, prev, raw, decision)
                    # Reset rather than leave it stuck rejecting forever - same
                    # "recover once the anomaly stops" reasoning as the per-poll
                    # rejection above not updating _last_energy's timestamp.
                    self._energy_window[key] = []
                    return None

        # Only mark dirty when the accepted raw value actually changed from
        # what's already tracked - debounced, so a flat/unchanged poll
        # doesn't trigger a write to disk every single cycle (see
        # _async_update_data's energy_state_dirty persist).
        if prev_entry is None or prev_entry[0] != raw:
            self._energy_state_dirty = True
        self._last_energy[key] = (raw, now)
        return raw

    def _record_energy_rejection(self, key: str, prev: int | None, raw: int, decision) -> None:
        record = {
            "at":                 datetime.now(timezone.utc).isoformat(),
            "key":                key,
            "last_good_raw":      prev,
            "rejected_raw":       raw,
            # `prev` is None for a rejected *first-ever* observation (see
            # energy_guard.ENERGY_ABS_MAX_KWH) - nothing to report a
            # last-known-good value as yet.
            "last_good_kwh":      round(prev * ENERGY_QUANTUM_KWH, 2) if prev is not None else None,
            "rejected_kwh":       round(raw * ENERGY_QUANTUM_KWH, 2),
            "delta_kwh":          decision.delta_kwh,
            "elapsed_s":          round(decision.elapsed_s),
            "max_plausible_kwh":  decision.max_plausible_kwh,
        }
        self.energy_rejections.append(record)
        del self.energy_rejections[:-MAX_ENERGY_REJECTION_RECORDS]
        _LOGGER.warning(
            "Rejected implausible %s read: raw=%d (prev=%s, delta=%.2f kWh "
            "over %.0fs, max plausible=%.2f kWh) - keeping last known-good value",
            key, raw, prev, decision.delta_kwh, decision.elapsed_s,
            decision.max_plausible_kwh,
        )
