"""The single charging controller: the only code allowed to write the charger.

On/off is expressed through the power setpoint (0x3002): paused means 0,
enabled means the saved positive power. 0x4001 Start is never sent. 0x4001
Stop is sent only as a fallback when a zero setpoint fails to stop a session,
after which every cap/power write is suppressed until an explicit enable.

Everything the controller wants the hardware to do is derived from one frozen
``Intent`` record. Each change bumps its revision and is persisted before any
write is dispatched. Immediately before every write (under one lock, with no
await in between) the value about to be sent is compared with the value the
*latest* intent derives; if they differ the work is dropped as superseded, so
an older value can never follow a newer one onto the wire.

A write counts as applied only after a fresh read of the register issued
after the write. Status/power evidence comes from fresh reads too and is
validated explicitly: unknown enum values never prove a stop or an unplug.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from .const import (
    REG_ALLOWED_CHARGE_ENERGY,
    REG_ALLOWED_CHARGE_TIME,
    REG_CHARGING_CONTROL,
    REG_DEFAULT_CURRENT,
    REG_FAULT_CODE,
    REG_AUTO_PHASE_SWITCH,
    REG_LOCK_CONTROL,
    REG_LOCK_STATUS,
    REG_MIN_SWITCH_INTERVAL,
    REG_PHASE_SEQUENCE,
    REG_PHASE_SWITCHING,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_MAX_POWER,
    REG_STATUS_BLOCK_START,
    REG_TIME_VALIDITY,
    REG_WORK_MODE,
    WORK_MODE_MAP,
)

from .protocol import SnapshotReader
from .transport import CommandRefused, TransportError, UnknownOutcome

_LOGGER = logging.getLogger(__name__)

SCHEMA = 1
MIN_ENABLE_POWER_RAW = 14          # 1.4 kW: below this the wire value is 0
MAX_POWER_RAW = 1000               # sanity bound; entities apply model ranges
MIN_CURRENT_RAW, MAX_CURRENT_RAW = 60, 320
LOW_POWER_RAW = 1                  # measured <= 0.1 kW counts as not drawing
# The firmware reverts setpoints ~60 s after a write even when 0x3005 reports
# a longer validity, so the effective validity is capped here.
FIRMWARE_EXPIRY_S = 60
MAX_REFRESH_S = 30
SAFETY_VALIDITY_S = 60
SAFETY_FALLBACK_CURRENT_RAW = 60   # 6 A
STOP_COMMAND = 2
OBSERVATION_GAP_S = 1.0            # pause needs two agreeing reads this far apart
POLL_STEP_S = 0.5

VALID_STATUS = {0, 1, 2, 3, 4, 5, 6, 8, 9}
PAUSED_STATUS = {0, 1, 4, 5}       # acceptable after a zero setpoint
STOPPED_STATUS = {0, 1, 5}         # acceptable after 0x4001 Stop
SESSION_STATUS = {2, 3, 4, 9}      # enable succeeded (9 = phase switching)
DRAWING_STATUS = {2, 3}
REFUSING_STATUS = {6, 8}           # fault, locked
FAULT_STATUS = 6
FAULT_OFFSET = REG_FAULT_CODE - REG_STATUS_BLOCK_START
STATUS_READ_COUNT = 28             # 0x1000..0x101B (fault code at 0x101A)

# (min, max) raw values accepted by async_set_register.
REGISTER_ALLOWLIST: dict[int, tuple[int, int]] = {
    REG_WORK_MODE: (0, 2),
    REG_ALLOWED_CHARGE_TIME: (0, 0xFFFF),
    REG_ALLOWED_CHARGE_ENERGY: (0, 0xFFFF),
    REG_TIME_VALIDITY: (1, 3600),
    REG_DEFAULT_CURRENT: (MIN_CURRENT_RAW, MAX_CURRENT_RAW),
    REG_LOCK_CONTROL: (1, 2),
    REG_AUTO_PHASE_SWITCH: (0, 1),
    REG_MIN_SWITCH_INTERVAL: (5, 30),
    REG_PHASE_SWITCHING: (0, 2),
}

# Write-only command registers: (status register proving it, expected value).
WRITE_ONLY_CONFIRMATION: dict[int, tuple[int, Callable[[int], int]]] = {
    REG_LOCK_CONTROL: (REG_LOCK_STATUS, lambda v: v - 1),   # 1 unlock -> 0, 2 lock -> 1
    REG_PHASE_SWITCHING: (REG_PHASE_SEQUENCE, lambda v: v),
}

# Only Plug&Charge (1) can start a session: this integration never sends 0x4001 Start.
WORK_MODE_ERRORS = {
    0: "Charger is in Controlled mode, which this integration does not support. "
       "Set the charger Work Mode to Plug&Charge.",
    2: "Charger Work Mode is Locked, so it will not start charging. "
       "Set the charger Work Mode to Plug&Charge.",
}

_REASON_TELEMETRY = "telemetry_uncertain"
_REASON_SAFETY = "safety_config_unconfirmed"
_DEGRADED_CURRENT = "current_cap_mismatch"


class ControlError(Exception):
    """A command could not be confirmed on the charger."""


class _Superseded(Exception):
    """Newer intent changed the value this work was about to write."""


@dataclass(frozen=True)
class Intent:
    enabled: bool = False
    power_raw: int | None = None
    current_raw: int | None = None
    revision: int = 0
    safety_latched: bool = False
    stop_fallback: bool = False


@dataclass(frozen=True)
class CommandResult:
    outcome: str  # "confirmed" | "staged" | "superseded"
    revision: int
    observed: int | None = None


@dataclass(frozen=True)
class _Evidence:
    status: int | None
    cc_status: int | None
    power_raw: int | None
    lock_status: int | None
    at: float
    fault_code: int | None = None       # None = not read

    @property
    def valid(self) -> bool:
        return (
            self.status in VALID_STATUS
            and self.cc_status in (0, 1)
            and isinstance(self.power_raw, int)
            and isinstance(self.fault_code, int)
            and 0 <= self.power_raw <= MAX_POWER_RAW
        )

    @property
    def drawing(self) -> bool:
        return self.valid and (
            self.status in DRAWING_STATUS or self.power_raw > LOW_POWER_RAW
        )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_saved(saved: Any) -> Intent | None:
    """Validated Intent from persisted state, or None if malformed."""
    if not isinstance(saved, dict) or saved.get("schema") != SCHEMA:
        return None
    flags = [saved.get(k) for k in ("enabled", "safety_latched", "stop_fallback")]
    if not all(isinstance(f, bool) for f in flags):
        return None
    power, current, rev = (saved.get(k) for k in ("power_raw", "current_raw", "revision"))
    if power is not None and not (_is_int(power) and 0 <= power <= MAX_POWER_RAW):
        return None
    if current is not None and not (
        _is_int(current) and MIN_CURRENT_RAW <= current <= MAX_CURRENT_RAW
    ):
        return None
    if not (_is_int(rev) and rev >= 0):
        return None
    return Intent(flags[0], power, current, rev, flags[1], flags[2])


class ChargingController:
    """Sole writer to the charger. All methods run on the event loop."""

    def __init__(
        self,
        io,
        *,
        persist: Callable[[dict], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        confirmation_timeout: float = 10.0,
        start_confirmation_timeout: float = 45.0,
        starting_timeout: float = 60.0,
        retry_interval: float = 3.0,
        pause_confirmation_timeout: float = 30.0,
        telemetry_grace: float = 20.0,
        read_snapshot: Callable[[Any], Awaitable[dict]] | None = None,
    ) -> None:
        if read_snapshot is None:
            reader = SnapshotReader(io, clock=clock)
            read_snapshot = lambda _io: reader.async_read()  # noqa: E731
        self._io = io
        self._persist = persist
        self._clock = clock
        self._sleep = sleep
        self._timeout = confirmation_timeout
        # Firmware 1.8 takes 11-19 s from the nonzero write to a visible session.
        self._start_timeout = start_confirmation_timeout
        # Status 2 confirms a start but must progress within this bound.
        self._starting_timeout = starting_timeout
        self._starting_since: float | None = None
        self._retry = retry_interval
        # A car may keep drawing for a while after zero power; Stop only after this.
        self._pause_timeout = pause_confirmation_timeout
        # Single lost reads must not interrupt a charge; keep below the ~60 s expiry.
        self._grace = telemetry_grace
        self._bad_since: float | None = None
        self._bad_count = 0
        self._good_first: _Evidence | None = None   # recovery candidate
        self._next_safety_try = 0.0
        self._persisted = True
        # Latest intent not yet durable; retried from the refresh loop.
        self._persist_pending = False
        self._persist_error: str | None = None
        # Pause released by a confirmed unplug: see _track_unplug.
        self._released = False
        self._unplug_first: float | None = None
        self._max_power: int | None = None       # device capability (0x1011)
        self._work_mode: int | None = None       # last decoded 0x3000, kept across failed reads
        self._mismatch_since: float | None = None
        self._mismatch_count = 0
        self._wake = asyncio.Event()             # refresh interval shortened
        self._read_snapshot = read_snapshot
        self._intent = Intent()
        self._lock = asyncio.Lock()
        self._protect: set[str] = set()   # reasons forcing wire zero
        self._degraded: set[str] = set()  # visible, not (yet) protective
        self._initialized = False
        self._closing = False
        self._busy = 0
        # Session provenance for positive writes (see _note_session).
        self._last_obs: _Evidence | None = None  # latest valid observation
        self._await_plug = False                 # explicit enable staged unplugged
        self._commands = 0                       # explicit commands in flight
        # (power, current, revision) last authorized for background refresh.
        self._authorized: tuple | None = None
        self._tasks: set[asyncio.Task] = set()
        self._correction: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._validity: int | None = None
        self._last_error: str | None = None
        self.data: dict = {}
        self._counters = dict.fromkeys(
            (
                "refreshes", "refresh_failures", "confirmations",
                "unknown_outcomes", "refusals", "stop_fallbacks",
                "implicit_resume_corrections", "superseded", "persist_failures",
            ),
            0,
        )

    # ── read-only views ──────────────────────────────────────────────────
    @property
    def intent_enabled(self) -> bool:
        return self._intent.enabled

    @property
    def desired_power_raw(self) -> int | None:
        return self._intent.power_raw

    @property
    def desired_current_raw(self) -> int | None:
        return self._intent.current_raw

    @property
    def phase(self) -> str:
        i = self._intent
        if not self._initialized:
            return "initializing"
        if i.safety_latched or _REASON_SAFETY in self._protect:
            return "faulted"
        if i.stop_fallback:
            return "stopped"
        if _REASON_TELEMETRY in self._protect:
            return "uncertain"
        if self._busy:
            return "applying"
        if not i.enabled:
            return "paused"
        if i.power_raw is not None and i.power_raw < MIN_ENABLE_POWER_RAW:
            return "paused_below_minimum"
        return "enabled"

    @property
    def diagnostics(self) -> dict:
        transport = getattr(self._io, "counters", None)
        return {
            **(dict(transport) if isinstance(transport, dict) else {}),
            **self._counters,
            "phase": self.phase,
            "revision": self._intent.revision,
            "safety_latched": self._intent.safety_latched,
            "stop_fallback": self._intent.stop_fallback,
            "protective_reasons": sorted(self._protect),
            "degraded_reasons": sorted(self._degraded),
            "authorized": self._authorized,
            "reported_time_validity": self._validity,
            "refresh_interval": self._refresh_interval(),
            "last_error": self._last_error,
            "persist_pending": self._persist_pending,
            "persist_error": self._persist_error,
        }

    def export_state(self) -> dict:
        i = self._intent
        return {
            "schema": SCHEMA, "enabled": i.enabled, "power_raw": i.power_raw,
            "current_raw": i.current_raw, "revision": i.revision,
            "safety_latched": i.safety_latched, "stop_fallback": i.stop_fallback,
        }

    # ── derived wire values ──────────────────────────────────────────────
    def _explicit_zero(self) -> bool:
        """Intent itself asks for no charging (as opposed to a protective zero)."""
        i = self._intent
        return (
            not i.enabled
            or i.safety_latched
            or (i.power_raw is not None and i.power_raw < MIN_ENABLE_POWER_RAW)
        )

    def _wire(self) -> tuple[int | None, int | None]:
        """(power, current) the latest intent wants on the wire; None = don't write."""
        i = self._intent
        if i.stop_fallback or self._released:
            return None, None
        if self._explicit_zero() or self._protect:
            return 0, None
        return i.power_raw, i.current_raw

    def _refresh_interval(self) -> float:
        v = self._validity
        if not _is_int(v) or v <= 0:
            return min(self._retry, MAX_REFRESH_S)
        return min(min(v, FIRMWARE_EXPIRY_S) / 2, MAX_REFRESH_S)

    # ── intent changes ───────────────────────────────────────────────────
    async def _update(self, **changes) -> Intent:
        self._intent = replace(
            self._intent, revision=self._intent.revision + 1, **changes
        )
        self._persisted = await self._save_intent()
        return self._intent

    async def _save_intent(self) -> bool:
        """Persist the current intent; on failure mark it pending so the
        refresh loop keeps retrying. Never blocks protective writes."""
        if self._persist is None:
            return True
        try:
            await self._persist(self.export_state())
        except Exception as err:  # noqa: BLE001 - never block protective writes
            self._persist_pending = True
            self._persist_error = f"intent revision {self._intent.revision} not saved: {err}"
            self._counters["persist_failures"] += 1
            _LOGGER.exception("Could not persist charging intent")
            return False
        self._persist_pending, self._persist_error = False, None
        return True

    def _require_ready(self) -> None:
        if self._closing:
            raise ControlError("controller is closed")
        if not self._initialized:
            raise ControlError("controller is not initialized")

    # ── task ownership ───────────────────────────────────────────────────
    def _spawn(self, factory: Callable[[], Awaitable[Any]]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(factory())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    async def _run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run controller work so that cancelling the caller cannot abort it."""
        return await asyncio.shield(self._spawn(factory))

    # ── wire primitives (lock must be held) ──────────────────────────────
    def _check(self, wire: tuple | None) -> None:
        if wire is not None and self._wire() != wire:
            raise _Superseded

    async def _read(self, address: int, count: int = 1) -> tuple[int, ...] | None:
        try:
            regs = await self._io.read(address, count)
        except Exception:  # noqa: BLE001 - any read failure is a missing observation
            return None
        return tuple(regs) if regs is not None and len(regs) >= count else None

    async def _read_status(self) -> _Evidence:
        """One contiguous read of 0x1000..0x101B: status, connector, power,
        lock and the fault code come from the same frame. If the block (and
        with it the fault register) cannot be read, the observation is
        invalid and goes through the telemetry grace like any other."""
        regs = await self._read(REG_STATUS_BLOCK_START, STATUS_READ_COUNT)
        if regs is None:
            return _Evidence(None, None, None, None, self._clock())
        code = regs[FAULT_OFFSET] << 16 | regs[FAULT_OFFSET + 1]
        return _Evidence(regs[3], regs[5], regs[14], regs[15], self._clock(), code)

    async def _send(self, address: int, value: int, wire: tuple | None) -> None:
        """Dispatch one write; the latest-intent check is the last step before I/O."""
        self._check(wire)
        try:
            await self._io.write(address, value)
        except UnknownOutcome:
            self._counters["unknown_outcomes"] += 1
        except asyncio.CancelledError:
            # The write may still have applied; the next operation reads back.
            self._counters["unknown_outcomes"] += 1
            raise
        except CommandRefused:
            self._counters["refusals"] += 1
        except Exception as err:  # noqa: BLE001 - outcome unknown; read-back decides
            self._last_error = f"write 0x{address:04X}: {err}"

    async def _write_once(self, address: int, value: int, wire: tuple | None) -> bool:
        await self._send(address, value, wire)
        regs = await self._read(address)
        return regs is not None and regs[0] == value

    async def _write_confirmed(
        self, address: int, value: int, wire: tuple | None, deadline: float
    ) -> None:
        while not await self._write_once(address, value, wire):
            if self._clock() >= deadline:
                raise ControlError(f"0x{address:04X}={value} not confirmed by read-back")
            await self._sleep(min(self._retry, max(0.0, deadline - self._clock())))

    def _note_telemetry(self, ev: _Evidence) -> bool:
        """Track telemetry validity; True if the protective state changed.

        Protective zero starts only after telemetry has been invalid for at
        least ``telemetry_grace`` across two or more consecutive observations.
        It ends only after two consecutive valid observations that agree on
        status and connector, at least OBSERVATION_GAP_S apart: one good
        frame inside an unstable burst is not recovery.
        """
        before = _REASON_TELEMETRY in self._protect
        if ev.valid:
            self._bad_since, self._bad_count = None, 0
            first = self._good_first
            if not before:
                self._good_first = None
            elif (
                first is not None
                and (first.status, first.cc_status) == (ev.status, ev.cc_status)
            ):
                if ev.at - first.at >= OBSERVATION_GAP_S:
                    self._good_first = None
                    self._protect.discard(_REASON_TELEMETRY)
            else:
                self._good_first = ev
        else:
            self._good_first = None
            self._bad_count += 1
            if self._bad_since is None:
                self._bad_since = ev.at
            if self._bad_count >= 2 and ev.at - self._bad_since >= self._grace:
                self._protect.add(_REASON_TELEMETRY)
        return before != (_REASON_TELEMETRY in self._protect)

    async def _note_fault(self, ev: _Evidence, fault_code: Any = None) -> bool:
        """Latch on a hard fault (status 6 or a nonzero fault code in a valid
        observation). The latch makes the wire value zero, which the refresh
        loop keeps re-sending; clearing the fault does not resume charging,
        only an explicit enable does. True if the latch was newly set."""
        faulted = ev.valid and (
            ev.status == FAULT_STATUS or (_is_int(fault_code) and fault_code != 0)
        )
        if not faulted or self._intent.safety_latched:
            return False
        self._last_error = f"charger fault (status {ev.status}, fault code {fault_code})"
        _LOGGER.error("FoxESS %s; output latched to zero", self._last_error)
        await self._update(safety_latched=True)
        return True

    def _session_active(self, ev: _Evidence | None) -> bool:
        return (
            ev is not None and ev.valid and ev.cc_status == 1
            and ev.status in SESSION_STATUS
        )

    async def _note_session(self, ev: _Evidence) -> bool:
        """Enabled intent ends with the session it authorized.

        A valid non-active observation (finished, idle, unplugged, ...) that
        no explicit command is responsible for revokes the enable: charging
        again needs an explicit enable (or a new session after unplug, see
        _track_unplug). An enable staged while unplugged is applied on plug.
        True if the intent was revoked."""
        if not ev.valid:
            return False
        self._last_obs = ev
        i = self._intent
        if not i.enabled or self._explicit_zero() or i.stop_fallback or self._commands:
            return False
        if self._session_active(ev):
            self._await_plug = False
            return False
        if self._await_plug:
            if ev.cc_status == 1:
                self._await_plug = False
                self._spawn(self._op_apply)   # the staged explicit enable
            return False
        self._last_error = f"session ended (status {ev.status}); enable revoked"
        _LOGGER.info("FoxESS %s", self._last_error)
        await self._update(enabled=False)
        return True

    async def _note_starting(self, ev: _Evidence) -> bool:
        """Watchdog (lock held): an enabled session stuck in status 2 without
        reaching charging or drawing power is revoked with a confirmed
        zero. True if revoked."""
        i = self._intent
        if not ev.valid or not i.enabled or self._explicit_zero() or i.stop_fallback:
            if ev.valid:
                self._starting_since = None
            return False
        if ev.status != 2 or ev.power_raw > LOW_POWER_RAW:
            self._starting_since = None
            return False
        if self._starting_since is None:
            self._starting_since = ev.at
            return False
        if ev.at - self._starting_since <= self._starting_timeout:
            return False
        self._starting_since = None
        await self._revoke()
        self._last_error = (
            f"charger stuck starting (status 2) for over {self._starting_timeout:.0f} s; "
            "enable revoked"
        )
        _LOGGER.error("FoxESS %s", self._last_error)
        return True

    async def _track_unplug(self, ev: _Evidence) -> None:
        """Release a user pause on a confirmed unplug (SPEC 5.2).

        An ordinary user pause belongs to the vehicle that was plugged in.
        A fault latch or a Stop fallback is not released this way: only an
        explicit enable leaves those. Two valid cc_status == 0 observations at least OBSERVATION_GAP_S apart
        release it: intent stays off, but zero is no longer asserted and
        resumes are no longer corrected. The next valid session observed
        after replug (e.g. Plug&Charge/RFID) is adopted as enabled with the
        saved power. Invalid or unknown cc_status never counts as unplugged,
        and while the cable stays plugged a paused intent is never resumed.
        """
        i = self._intent
        user_pause = self._explicit_zero() and not (i.safety_latched or i.stop_fallback)
        if not user_pause or not self._initialized:
            self._released, self._unplug_first = False, None
            return
        if self._released:
            if ev.valid and ev.cc_status == 1 and ev.status in SESSION_STATUS:
                self._released = False
                await self._update(enabled=True)
                self._spawn(self._op_apply)
            return
        if ev.valid and ev.cc_status == 0:
            if self._unplug_first is None:
                self._unplug_first = ev.at
            elif ev.at - self._unplug_first >= OBSERVATION_GAP_S:
                self._released, self._unplug_first = True, None
        else:
            self._unplug_first = None

    def _note_readback(self, mismatched: bool) -> None:
        """Track refresh read-back mismatches (the charger ignoring our cap)."""
        if not mismatched:
            self._mismatch_since, self._mismatch_count = None, 0
            return
        self._mismatch_count += 1
        if self._mismatch_since is None:
            self._mismatch_since = self._clock()

    def _mismatch_persistent(self) -> bool:
        window = min(self._validity, FIRMWARE_EXPIRY_S) if (
            _is_int(self._validity) and self._validity > 0) else FIRMWARE_EXPIRY_S
        return (
            self._mismatch_count >= 2
            and self._clock() - self._mismatch_since >= window
        )

    def _set_validity(self, value: int | None) -> None:
        before = self._refresh_interval()
        self._validity = value
        if self._refresh_interval() < before:
            self._wake.set()

    async def _await_evidence(self, ok, wire, deadline) -> _Evidence | None:
        """Poll fresh status until ``ok(ev)`` twice, OBSERVATION_GAP_S apart."""
        first: float | None = None
        while True:
            self._check(wire)
            ev = await self._read_status()
            if ok(ev):
                if first is not None and ev.at - first >= OBSERVATION_GAP_S:
                    return ev
                first = ev.at if first is None else first
            else:
                first = None
            if self._clock() >= deadline:
                return None
            await self._sleep(POLL_STEP_S)

    # ── operations (each runs as an owned task) ──────────────────────────
    async def _op_apply(self, revision: int | None = None) -> CommandResult:
        """Apply the latest intent. A command passes the revision it created: if
        intent moved on since, the latest intent is still applied (a newer
        staged change may not write anything itself) but the command reports
        superseded, never confirmed for a value it did not request."""
        async with self._lock:
            self._busy += 1
            try:
                overtaken = revision is not None and self._intent.revision != revision
                result = await self._apply_locked()
                if overtaken:
                    self._counters["superseded"] += 1
                    return CommandResult("superseded", self._intent.revision)
                return result
            except _Superseded:
                self._counters["superseded"] += 1
                return CommandResult("superseded", self._intent.revision)
            except ControlError as err:
                self._last_error = str(err)
                raise
            finally:
                self._busy -= 1

    async def _apply_locked(self) -> CommandResult:
        intent, wire = self._intent, self._wire()
        power, current = wire
        if intent.stop_fallback:
            ev = await self._read_status()
            if ev.drawing:
                await self._stop_sequence()
            return CommandResult("confirmed", intent.revision, None)
        try:
            # Each register gets its own fresh confirmation_timeout window
            # rather than splitting one between them - a slow transaction on
            # the first write must not eat into the second's confirmation
            # budget too.
            if current is not None:
                await self._write_confirmed(
                    REG_MAX_CHARGING_CURRENT, current, wire, self._clock() + self._timeout)
            if power is not None:
                await self._write_confirmed(
                    REG_MAX_CHARGING_POWER, power, wire, self._clock() + self._timeout)
        except ControlError:
            await self._latch_fault()
            raise
        if power == 0 and self._explicit_zero():
            ok = lambda ev: (  # noqa: E731
                ev.valid and ev.status in PAUSED_STATUS and ev.power_raw <= LOW_POWER_RAW
            )
            pause_deadline = self._clock() + self._pause_timeout
            if await self._await_evidence(ok, wire, pause_deadline) is None:
                await self._stop_sequence()
                return CommandResult("confirmed", self._intent.revision, None)
        elif power != 0:
            try:
                started = await self._confirm_session(
                    wire, self._clock() + self._start_timeout
                )
            except ControlError:
                await self._revoke()
                raise
            if not started:
                self._await_plug = True
                return CommandResult("staged", intent.revision, power)  # unplugged
            self._await_plug = False
            self._authorized = (power, current, intent.revision)
            ev = self._last_obs
            if ev is not None and ev.status == 2 and self._starting_since is None:
                self._starting_since = ev.at    # watchdog counts from confirmation
        self._counters["confirmations"] += 1
        return CommandResult("confirmed", intent.revision, power)

    async def _confirm_session(self, wire, deadline) -> bool:
        """True once a session is seen; False if the cable is definitely unplugged."""
        while True:
            self._check(wire)
            ev = await self._read_status()
            if ev.valid:
                self._last_obs = ev
            if ev.valid and ev.status in REFUSING_STATUS:
                raise ControlError(f"charger refuses to charge (status {ev.status})")
            if ev.valid and ev.cc_status == 0:
                return False
            if ev.valid and ev.cc_status == 1 and ev.status in SESSION_STATUS:
                return True
            if self._clock() >= deadline:
                raise ControlError(WORK_MODE_ERRORS.get(
                    self._work_mode, "no active charging session after enable"))
            await self._sleep(POLL_STEP_S)

    async def _stop_sequence(self) -> None:
        """Zero power did not stop charging: 0x4001 Stop, confirmed, then latch."""
        self._counters["stop_fallbacks"] += 1
        deadline = self._clock() + self._timeout
        stopped = lambda ev: (  # noqa: E731
            ev.valid and ev.status in STOPPED_STATUS and ev.power_raw <= LOW_POWER_RAW
        )
        while True:
            if not self._explicit_zero():
                raise _Superseded  # user re-enabled meanwhile: do not stop
            await self._send(REG_CHARGING_CONTROL, STOP_COMMAND, None)
            step_deadline = min(deadline, self._clock() + self._retry)
            if await self._await_evidence(stopped, None, step_deadline) is not None:
                await self._update(stop_fallback=True)
                return
            if self._clock() >= deadline:
                await self._update(safety_latched=True)
                raise ControlError("charging did not stop after zero power and Stop")

    async def _revoke(self) -> None:
        """A positive command failed: withdraw the authorization and put a
        confirmed zero on the wire, so nothing starts later by itself."""
        await self._update(enabled=False)
        try:
            await self._write_confirmed(
                REG_MAX_CHARGING_POWER, 0, None, self._clock() + self._timeout
            )
        except ControlError:
            await self._latch_fault()

    async def _latch_fault(self) -> None:
        """Unconfirmable write: latch the fault and make one protective zero attempt."""
        if not self._intent.safety_latched:
            await self._update(safety_latched=True)
        if not self._intent.stop_fallback:
            try:
                await self._write_once(REG_MAX_CHARGING_POWER, 0, None)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Protective zero write failed")

    async def _configure_safety(self, deadline: float) -> None:
        """Write 60 s validity and 6 A fallback with read-back (lock held).

        `deadline` is an overall ceiling, not a budget split between the two
        registers: each gets its own fresh confirmation_timeout window
        (still capped by `deadline`, so the single-shot periodic-refresh
        caller passing `self._clock()` still gets exactly one attempt per
        register, no retries).
        """
        self._next_safety_try = self._clock() + self._refresh_interval()
        try:
            await self._write_confirmed(
                REG_TIME_VALIDITY, SAFETY_VALIDITY_S, None,
                min(deadline, self._clock() + self._timeout))
            await self._write_confirmed(
                REG_DEFAULT_CURRENT, SAFETY_FALLBACK_CURRENT_RAW, None,
                min(deadline, self._clock() + self._timeout))
        except ControlError as err:
            self._last_error = f"safety configuration: {err}"
            _LOGGER.error("Safety configuration unconfirmed; enable blocked, will retry")
            return
        self._protect.discard(_REASON_SAFETY)

    async def _op_refresh(self) -> bool:
        async with self._lock:
            if self._persist_pending:
                await self._save_intent()
            if _REASON_SAFETY in self._protect and self._clock() >= self._next_safety_try:
                await self._configure_safety(self._clock())  # one attempt per interval
            ev = await self._read_status()
            self._note_telemetry(ev)
            await self._note_fault(ev, ev.fault_code)
            await self._note_session(ev)
            await self._note_starting(ev)
            await self._track_unplug(ev)
            regs = await self._read(REG_TIME_VALIDITY)
            if regs is not None:
                self._set_validity(regs[0])
            wire = self._wire()
            power, current = wire
            held = False
            if power and self._session_active(ev):
                # A fresh valid active-session observation authorizes the
                # latest revision for background refresh.
                self._authorized = (power, current, self._intent.revision)
            elif power and not ev.valid:
                # Inside the telemetry grace: hold. Re-sending even the last
                # authorized cap would restart a session that finished
                # unobserved (firmware 1.8), and a staged revision was never
                # authorized. The cap written last stays in force (grace +
                # refresh interval < firmware expiry); protective zero follows
                # if the outage persists.
                wire, power, current, held = None, None, None, True
            elif power:
                # Valid but no active session: zero, never positive.
                wire, power, current = None, 0, None
            if not power:
                self._authorized = None
            try:
                # Every address is refreshed even if an earlier one failed:
                # a refused or ignored 0x3001 must never keep the
                # authoritative 0x3002 cap from being re-sent.
                ok, mismatched, seen = True, False, False
                for address, value in ((REG_MAX_CHARGING_CURRENT, current),
                                       (REG_MAX_CHARGING_POWER, power)):
                    if value is None:
                        continue
                    await self._send(address, value, wire)
                    back = await self._read(address)
                    good = back is not None and back[0] == value
                    ok = ok and good
                    if address == REG_MAX_CHARGING_CURRENT:
                        # An unreadable current ceiling is unconfirmed, not
                        # fine: same degraded state and latch window as a
                        # wrong read-back.
                        seen = True
                        mismatched = mismatched or not good
                        if good:
                            self._degraded.discard(_DEGRADED_CURRENT)
                        else:
                            self._degraded.add(_DEGRADED_CURRENT)
                    elif back is not None:
                        seen = True
                        mismatched = mismatched or not good
                if seen:
                    self._note_readback(mismatched)
                if current is None:
                    self._degraded.discard(_DEGRADED_CURRENT)
            except _Superseded:
                self._counters["superseded"] += 1
                ok = True  # the newer command writes its own value
            if held:
                ok = False           # retry soon rather than a full interval
            if self._mismatch_count and self._mismatch_persistent():
                self._last_error = "charger keeps ignoring the setpoint"
                self._note_readback(False)
                await self._latch_fault()
        self._counters["refreshes" if ok else "refresh_failures"] += 1
        self._maybe_correct(ev)
        return ok

    def _maybe_correct(self, ev: _Evidence) -> None:
        """Charging while intent says off: re-assert zero (and Stop if needed)."""
        if self._released or not (ev.drawing and self._explicit_zero()):
            return
        if self._correction is not None and not self._correction.done():
            return
        self._counters["implicit_resume_corrections"] += 1
        self._correction = self._spawn(self._op_apply)

    # ── public API ───────────────────────────────────────────────────────
    async def async_initialize(self, saved: dict | None = None, *, configure_safety: bool = True) -> None:
        if saved is None:
            # First install: authorize only a session the hardware shows is active.
            ev = await self._read_status()
            active = ev.valid and ev.cc_status == 1 and ev.status in SESSION_STATUS
            self._intent = Intent(enabled=active)
        else:
            parsed = _parse_saved(saved)
            if parsed is None:
                _LOGGER.warning("Saved charging intent is malformed; starting paused")
                self._last_error = "malformed saved state"
                parsed = Intent(enabled=False, revision=0)
            self._intent = parsed
            if parsed.enabled:
                # Restored intent is not proof the charger should run now: a
                # nonzero 0x3002 write resumes a finished session. Keep it
                # enabled only if the hardware shows a session running.
                ev = await self._read_status()
                if not (ev.valid and ev.cc_status == 1 and ev.status in SESSION_STATUS):
                    self._last_error = "restored enabled intent without an active session; paused"
                    _LOGGER.warning(self._last_error)
                    self._intent = replace(self._intent, enabled=False)
        if configure_safety:
            self._protect.add(_REASON_SAFETY)
            async with self._lock:
                # Two registers, each wanting its own confirmation_timeout
                # window (see _configure_safety) - the outer ceiling passed
                # here must cover both, not just one.
                await self._configure_safety(self._clock() + self._timeout * 2)
        regs = await self._read(REG_MAX_POWER)
        if regs is not None and MIN_ENABLE_POWER_RAW <= regs[0] <= MAX_POWER_RAW:
            self._max_power = regs[0]
        if saved is None and self._intent.enabled:
            # Adopting a running session with no saved limits: keep the cap
            # it is already running under, if valid, so adoption never
            # raises it; otherwise write an explicit zero.
            live = await self._read(REG_MAX_CHARGING_POWER)
            cap = live[0] if live is not None else None
            valid = (
                self._max_power is not None and _is_int(cap)
                and MIN_ENABLE_POWER_RAW <= cap <= self._max_power
            )
            self._intent = replace(self._intent, power_raw=cap if valid else 0)
        p = self._intent.power_raw
        if self._max_power is not None and p is not None and p > self._max_power:
            # Never clamp silently or run uncapped: pause and show why.
            self._last_error = (
                f"restored power {p} above device maximum {self._max_power}; paused"
            )
            _LOGGER.warning(self._last_error)
            self._intent = replace(self._intent, enabled=False, power_raw=None)
        self._initialized = True
        await self._update()
        await self._run(self._op_refresh)
        if self._correction is not None:
            try:
                await asyncio.shield(self._correction)
            except ControlError as err:
                _LOGGER.error("Could not re-assert saved pause: %s", err)

    async def async_poll(self) -> dict:
        self._require_ready()
        return await self._run(self._op_poll)

    async def _op_poll(self) -> dict:
        async with self._lock:
            try:
                snap = dict(await self._read_snapshot(self._io))
            except Exception:  # noqa: BLE001 - failed mandatory block
                snap = {}
            self.data = snap
            if snap.get("work_mode") in WORK_MODE_MAP:
                self._work_mode = snap["work_mode"]
            ev = _Evidence(
                snap.get("status") if snap.get("status_valid", True) else None,
                snap.get("cc_status") if snap.get("cc_status_valid", True) else None,
                snap.get("power_raw"), snap.get("lock_status"), self._clock(),
                snap.get("fault_code"),
            )
            changed = self._note_telemetry(ev)
            changed = await self._note_fault(ev, snap.get("fault_code")) or changed
            changed = await self._note_session(ev) or changed
            changed = await self._note_starting(ev) or changed
            await self._track_unplug(ev)
        if changed:
            self._spawn(self._op_refresh)  # write the protective/restored value now
        self._maybe_correct(ev)
        return self.data

    async def async_set_power(self, raw: int) -> CommandResult:
        self._require_ready()
        if not (_is_int(raw) and 0 <= raw <= MAX_POWER_RAW):
            raise ValueError(f"invalid power {raw!r}")
        if self._max_power is not None and raw > self._max_power:
            raise ValueError(f"power {raw} above device maximum {self._max_power}")
        return await self._change(power_raw=raw)

    async def async_set_current(self, raw: int) -> CommandResult:
        self._require_ready()
        if not (_is_int(raw) and MIN_CURRENT_RAW <= raw <= MAX_CURRENT_RAW):
            raise ValueError(f"invalid current {raw!r}")
        return await self._change(current_raw=raw)

    async def _command(self, factory):
        """Run an explicit command; while it is in flight, observations do
        not revoke the enable it is establishing."""
        self._commands += 1
        try:
            return await factory()
        finally:
            self._commands -= 1

    async def _change(self, **changes) -> CommandResult:
        """A limit change is not an authorization: while enabled it writes a
        positive value only if a fresh observation shows an active session.
        Otherwise it is staged, and an observed finish revokes the enable."""
        intent = await self._update(**changes)
        if intent.enabled and not (intent.stop_fallback or intent.safety_latched):
            if self._wire()[0] and not await self._run(self._op_observe):
                intent = self._intent
            else:
                return await self._run(lambda: self._op_apply(intent.revision))
        if not self._persisted:  # a staged value lives only in storage
            raise ControlError("staged value could not be saved")
        return CommandResult("staged", intent.revision)

    async def _op_observe(self) -> bool:
        """Fresh status read; True if positive output is authorized by it."""
        async with self._lock:
            ev = await self._read_status()
            self._note_telemetry(ev)
            if await self._note_session(ev):
                self._spawn(self._op_refresh)
            # Only this fresh read may authorize; never a stale one.
            return self._session_active(ev)

    async def async_enable(self) -> CommandResult:
        """Explicit resume; also the only way out of a Stop fallback or fault latch."""
        self._require_ready()
        if _REASON_SAFETY in self._protect:
            raise ControlError("safety configuration unconfirmed; enable blocked")
        power = self._intent.power_raw
        if power is None:
            regs = await self._run(lambda: self._locked_read(REG_MAX_POWER))
            if regs is None or not (MIN_ENABLE_POWER_RAW <= regs[0] <= MAX_POWER_RAW):
                raise ControlError("no saved power and device maximum unreadable")
            power = regs[0]
        async def enable() -> CommandResult:
            intent = await self._update(
                enabled=True, power_raw=power, safety_latched=False, stop_fallback=False
            )
            return await self._run(lambda: self._op_apply(intent.revision))

        return await self._command(enable)

    async def _locked_read(self, address: int):
        async with self._lock:
            return await self._read(address)

    async def async_pause(self) -> CommandResult:
        self._require_ready()
        intent = await self._update(enabled=False)
        result = await self._run(lambda: self._op_apply(intent.revision))
        if self._persist_pending:
            # The zero is on the wire, but a restart could restore the older
            # enabled record: report the pause as not durable.
            raise ControlError("pause applied but not saved")
        return result

    async def async_set_register(self, address: int, value: int) -> CommandResult:
        self._require_ready()
        bounds = REGISTER_ALLOWLIST.get(address)
        if bounds is None or not _is_int(value) or not bounds[0] <= value <= bounds[1]:
            raise ValueError(f"register 0x{address:04X}={value!r} is not writable here")
        return await self._run(lambda: self._op_register(address, value))

    async def _op_register(self, address: int, value: int) -> CommandResult:
        async with self._lock:
            deadline = self._clock() + self._timeout
            proof = WRITE_ONLY_CONFIRMATION.get(address)
            if proof is None:
                await self._write_confirmed(address, value, None, deadline)
                observed = value
            else:  # write-only: confirm through the status register it drives
                status_reg, expected = proof
                observed = expected(value)
                while True:
                    await self._send(address, value, None)
                    regs = await self._read(status_reg)
                    if regs is not None and regs[0] == observed:
                        break
                    if self._clock() >= deadline:
                        raise ControlError(f"0x{address:04X}={value} not confirmed")
                    await self._sleep(min(self._retry, max(0.0, deadline - self._clock())))
            if address == REG_TIME_VALIDITY:
                self._set_validity(value)
            self._counters["confirmations"] += 1
            return CommandResult("confirmed", self._intent.revision, observed)

    async def async_start(self) -> None:
        """Start the background refresh loop."""
        self._require_ready()
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while not self._closing:
            started = self._clock()
            try:
                ok = await self._run(self._op_refresh)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                _LOGGER.exception("Setpoint refresh failed")
                self._counters["refresh_failures"] += 1
                ok = False
            while (remaining := started + (
                self._refresh_interval() if ok else self._retry) - self._clock()) > 0:
                if not await self._sleep_or_wake(remaining):
                    break

    async def _sleep_or_wake(self, seconds: float) -> bool:
        """Sleep; True if woken early because the refresh interval shrank."""
        self._wake.clear()
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sleeper, waker):
                t.cancel()
        return waker.done() and not waker.cancelled() and not sleeper.done()

    async def async_close(self) -> None:
        """Stop the loop, let in-flight work drain, then close I/O."""
        self._closing = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._io.close()
