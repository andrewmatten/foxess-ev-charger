"""Behavioural simulator of a FoxESS A7300P1 charger for the rebuild tests.

`SimCharger` implements the RegisterIO protocol from CONTRACTS.md directly
(`async read(address, count)`, `async write(address, value)`,
`async close()`). Every read is computed from the simulated *device* state at
the moment of the request; there is no client-side cache anywhere in here, so
a controller can never be "confirmed" by its own optimistic value.

Modelled firmware behaviour (items marked ASSUMED are design hypotheses from
SPEC.md, not verified on hardware):

- 0x3001/0x3002 revert to the device maximum `effective_validity` seconds
  after their last write. The *reported* validity (0x3005) and the effective
  expiry are configured independently (60/60, 180/180, 180/60).
- Writing 0x3002=0 while a session is running pauses charging by ending the
  session: status goes to 5 (finished), stop_reason 1, power 0 (measured on
  firmware 1.8; `zero_pause_status` defaults to 5, kept configurable). Or,
  on firmware that ignores it, the write is accepted and ignored
  (`zero_pause_works=False`, ASSUMED failure mode).
- A nonzero 0x3001/0x3002 write while the session is connected/finished/
  zero-paused implicitly resumes charging: status goes to 2 (starting) then
  3 (charging) after `start_delay` seconds, same as an explicit Start
  (observed firmware quirk). The setpoint's own expiry back to the device
  max does NOT resume a finished/paused session — only a fresh nonzero
  write does.
- 0x4001=1 while a session is already running is refused with Modbus
  exception 0x03 (CommandRefused).
- When no request at all has reached the device for `effective_validity`
  seconds, the device falls back to the 0x3006 default current with the
  power limit released until the next 0x3001/0x3002 write (ASSUMED). A
  zero-power pause does not survive that.
- Fault injection per write: refused, lost request, lost reply after apply,
  ACK with delayed apply, ACK and ignored, transport error; per read:
  transport error. Invalid telemetry (e.g. 65535 status/cc) can be forced.
- Writes can be held "in flight" with a gate to build deterministic
  interleavings.

Time comes from a clock callable. `VirtualClock` (default) provides a
deterministic virtual time with an injectable `sleep`; historical replays
may pass any monotonic callable instead.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from custom_components.foxess_charger.const import (
    REG_ALLOWED_CHARGE_ENERGY,
    REG_ALLOWED_CHARGE_TIME,
    REG_AUTO_PHASE_SWITCH,
    REG_CHARGING_CONTROL,
    REG_DEFAULT_CURRENT,
    REG_ID_MODEL_CODE,
    REG_ID_SERIAL_NUMBER,
    REG_LOCK_CONTROL,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_MIN_SWITCH_INTERVAL,
    REG_PHASE_SWITCHING,
    REG_RESTART,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
    REG_TIME_VALIDITY,
    REG_WORK_MODE,
)

# ── Exceptions ────────────────────────────────────────────────────────────
from custom_components.foxess_charger.transport import (  # noqa: E402
    CommandRefused,
    TransportError,
    UnknownOutcome,
)


def _refused(message: str, code: int = 3) -> CommandRefused:
    return CommandRefused(message, code=code)


# ── Device constants ──────────────────────────────────────────────────────
MAX_POWER_RAW = 73        # 7.3 kW in 0.1 kW
MAX_CURRENT_RAW = 320     # 32.0 A in 0.1 A
MIN_CURRENT_RAW = 60      # 6.0 A
NOMINAL_VOLTAGE = 230     # single phase
INVALID = 0xFFFF          # 65535, what a garbled/unsupported read looks like

# Session states (device side). The status register value is derived.
IDLE = "idle"                    # cable unplugged
CONNECTED = "connected"          # cable in, no session running
STARTING = "starting"
CHARGING = "charging"
CAR_PAUSED = "car_paused"        # vehicle suspended draw itself
ZERO_PAUSED = "zero_paused"      # paused by 0x3002 = 0
FINISHED = "finished"            # stopped by 0x4001=2 or session end
FAULT = "fault"
PHASE_SWITCHING = "phase_switching"

STATUS_OF = {
    IDLE: 0, CONNECTED: 1, STARTING: 2, CHARGING: 3, CAR_PAUSED: 4,
    FINISHED: 5, FAULT: 6, PHASE_SWITCHING: 9,
}
# States in which a session is running from the charger's point of view:
# a redundant 0x4001=1 is refused in any of them.
SESSION_RUNNING = {STARTING, CHARGING, CAR_PAUSED, ZERO_PAUSED, PHASE_SWITCHING}
# States where a nonzero limit write implicitly (re)starts charging.
IMPLICIT_RESUME_FROM = {CONNECTED, FINISHED, ZERO_PAUSED}

WRITE_MODES = {
    "ok",            # applied, acknowledged
    "refuse",        # not applied, CommandRefused
    "lost_request",  # not applied, UnknownOutcome
    "lost_reply",    # applied, UnknownOutcome
    "delayed",       # acknowledged now, applied `delay` seconds later
    "ignored",       # acknowledged, never applied
    "transport_error",  # never reached the device, TransportError
}


# ── Virtual clock ─────────────────────────────────────────────────────────
class VirtualClock:
    """Deterministic virtual time. `clock()` returns now; `sleep` is an async
    drop-in for asyncio.sleep that only completes when virtual time is
    advanced past its deadline (via `advance` or `run`)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self._waiters: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float, result: Any = None) -> Any:
        delay = max(0.0, float(delay))
        if delay == 0:
            await asyncio.sleep(0)
            return result
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self.now + delay, next(self._seq), fut))
        await fut
        return result

    @property
    def pending(self) -> int:
        return sum(1 for _, _, f in self._waiters if not f.done())

    def _next_deadline(self) -> float | None:
        while self._waiters and self._waiters[0][2].done():
            heapq.heappop(self._waiters)  # cancelled sleeps
        return self._waiters[0][0] if self._waiters else None

    def _wake_due(self) -> None:
        while self._waiters and self._waiters[0][0] <= self.now:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)

    @staticmethod
    async def _settle(rounds: int = 30) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Advance virtual time by `seconds`, waking every sleeper in
        deadline order and letting the event loop run between wake-ups."""
        target = self.now + seconds
        await self._settle()
        while True:
            nxt = self._next_deadline()
            if nxt is None or nxt > target:
                break
            self.now = max(self.now, nxt)
            self._wake_due()
            await self._settle()
        self.now = target
        await self._settle()

    async def run(self, awaitable: Awaitable, *, max_time: float = 3600.0) -> Any:
        """Run `awaitable` to completion, advancing virtual time to the next
        sleeper whenever the event loop goes idle. Background sleepers are
        woken along the way, exactly as real time would. Raises if the
        awaitable needs more than `max_time` virtual seconds."""
        task = asyncio.ensure_future(awaitable)
        limit = self.now + max_time
        try:
            while not task.done():
                await self._settle()
                if task.done():
                    break
                nxt = self._next_deadline()
                if nxt is None:
                    # Nothing sleeping on virtual time; the task waits on
                    # something else (a gate, a lock). Give it real turns.
                    await asyncio.sleep(0.001)
                    continue
                if nxt > limit:
                    raise AssertionError(
                        f"awaitable still pending after {max_time}s virtual time"
                    )
                self.now = max(self.now, nxt)
                self._wake_due()
        except BaseException:
            if not task.done():
                task.cancel()
            raise
        return task.result()


# ── Records ───────────────────────────────────────────────────────────────
@dataclass
class WriteRecord:
    seq: int
    t: float
    address: int
    value: int
    mode: str
    applied_at: float | None = None
    acknowledged: bool = False
    raised: str | None = None


@dataclass
class LogEntry:
    seq: int
    t: float
    kind: str               # "read" | "write" | "close" | "restart" | "event"
    address: int | None = None
    value: Any = None
    outcome: str = ""


@dataclass
class _Rule:
    address: int | None
    mode: str
    value: int | None = None
    delay: float = 0.0
    remaining: int | None = 1   # None = persistent


class WriteGate:
    """Holds matching writes in flight until released."""

    def __init__(self, address: int | None, value: int | None, count: int | None) -> None:
        self.address = address
        self.value = value
        self.remaining = count
        self.entered = asyncio.Event()
        self._released = asyncio.Event()
        self.held = 0

    def matches(self, address: int, value: int) -> bool:
        if self.remaining is not None and self.remaining <= 0:
            return False
        if self.address is not None and address != self.address:
            return False
        if self.value is not None and value != self.value:
            return False
        return True

    def release(self) -> None:
        self._released.set()


# ── The simulator ─────────────────────────────────────────────────────────
class SimCharger:
    """Simulated charger + RegisterIO endpoint."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        reported_validity: int = 180,
        effective_validity: float | None = None,
        zero_pause_works: bool = True,
        zero_pause_status: int = 5,
        implicit_resume: bool = True,
        fallback_on_silence: bool = True,
        fallback_current_raw: int = 80,
        state: str = CONNECTED,
        car_demand_raw: int = MAX_POWER_RAW,
        max_power_raw: int = MAX_POWER_RAW,
        max_current_raw: int = MAX_CURRENT_RAW,
        work_mode: int = 0,
        start_delay: float = 0.0,
        model_code: str = "A7300P1-E-B-WO",
        serial: str = "SIM0000000000001",
        total_energy_raw: int = 12345,
        reply_latency: float = 0.0,
    ) -> None:
        self.clock = clock if clock is not None else VirtualClock()
        # Virtual seconds each read()/write() waits before touching the
        # register model, standing in for a charger that is slow to answer
        # (the real A7300P1 sometimes takes several seconds). Advanced via
        # the clock's own sleep so a VirtualClock-driven test controls it
        # exactly like any other wait.
        self._reply_latency = reply_latency
        self._lock = threading.RLock()
        self.max_power_raw = max_power_raw
        self.max_current_raw = max_current_raw
        # effective expiry = min(0x3005 as written, firmware cap)
        self.validity_cap = float(
            effective_validity if effective_validity is not None else reported_validity
        )
        self.zero_pause_works = zero_pause_works
        self.zero_pause_status = zero_pause_status
        self.implicit_resume = implicit_resume
        self.fallback_on_silence = fallback_on_silence
        self.car_demand_raw = car_demand_raw
        self.start_delay = start_delay
        self.model_code = model_code
        self.serial = serial

        self.holding: dict[int, int] = {
            REG_WORK_MODE: work_mode,
            REG_MAX_CHARGING_CURRENT: max_current_raw,
            REG_MAX_CHARGING_POWER: max_power_raw,
            REG_ALLOWED_CHARGE_TIME: INVALID,
            REG_ALLOWED_CHARGE_ENERGY: INVALID,
            REG_TIME_VALIDITY: reported_validity,
            REG_DEFAULT_CURRENT: fallback_current_raw,
        }
        self._state = state
        self.stop_reason = 0
        self.alarm_code = 0
        self.fault_code = 0
        self.lock_status = 0
        self.status_override: int | None = None
        self.cc_override: int | None = None
        self._fallback = False
        self._expiry: dict[int, float] = {}        # register -> revert time
        self._start_complete_at: float | None = None
        self._delayed: list[tuple[float, int, int, int, WriteRecord]] = []
        self._last_comm: float | None = self.clock()
        self._last_sync: float = self.clock()
        self.total_energy_wh = float(total_energy_raw) * 100.0
        self.session_energy_wh = 0.0

        self._rules: list[_Rule] = []
        self._read_failures: list[tuple[int | None, int | None]] = []  # (address, remaining)
        self.io_down = False
        self._burst_until: float | None = None
        self._gates: list[WriteGate] = []
        self._seq = itertools.count(1)
        self.writes: list[WriteRecord] = []
        self.log: list[LogEntry] = []
        self.closed = False
        self.close_count = 0
        self.restarts = 0
        self.implicit_resumes = 0
        self.refused_starts = 0
        self.timeline: list[tuple[float, int, int, int]] = []  # t, measured, power limit, status
        self._record_timeline(self.clock())

    # ── clock ─────────────────────────────────────────────────────────────
    def now(self) -> float:
        return self.clock()

    def advance(self, seconds: float) -> None:
        """Synchronous time step for sim-only tests (VirtualClock only)."""
        if not isinstance(self.clock, VirtualClock):
            raise TypeError("advance() requires the default VirtualClock")
        self.clock.now += seconds
        self.sync()

    @property
    def effective_validity(self) -> float:
        reported = self.holding[REG_TIME_VALIDITY]
        if reported <= 0:
            return self.validity_cap
        return min(float(reported), self.validity_cap)

    # ── derived observables ───────────────────────────────────────────────
    @property
    def cable_connected(self) -> bool:
        return self._state != IDLE

    @property
    def status(self) -> int:
        if self._state == ZERO_PAUSED:
            return self.zero_pause_status
        return STATUS_OF[self._state]

    # Public observers bring the device up to "now" first; internal code
    # uses the underscored state directly.
    @property
    def state(self) -> str:
        self.sync()
        return self._state

    @state.setter
    def state(self, new: str) -> None:
        """Force a device state (e.g. something outside HA resumes it)."""
        with self._lock:
            self._sync(self.clock())
            if new == CHARGING and self._state != CHARGING:
                self._enter_charging()
            self._state = new
            self._record_timeline(self.clock())

    @property
    def fallback_active(self) -> bool:
        self.sync()
        return self._fallback

    def measured_power_raw(self) -> int:
        self.sync()
        return self._measured()

    def power_limit_raw(self) -> int:
        self.sync()
        return self._power_limit()

    def _power_limit(self) -> int:
        """Power cap the device is actually enforcing right now."""
        if self._fallback:
            return self.max_power_raw
        reg = self.holding[REG_MAX_CHARGING_POWER]
        if reg == 0 and not self.zero_pause_works:
            return self.max_power_raw
        return reg

    def current_limit_raw(self) -> int:
        if self._fallback:
            return self.holding[REG_DEFAULT_CURRENT]
        return self.holding[REG_MAX_CHARGING_CURRENT]

    def _measured(self) -> int:
        if self._state != CHARGING:
            return 0
        by_current = int(self.current_limit_raw() * NOMINAL_VOLTAGE // 1000)
        return max(0, min(self._power_limit(), by_current, self.car_demand_raw))

    def _record_timeline(self, t: float) -> None:
        entry = (t, self._measured(), self._power_limit(), self.status)
        if self.timeline and self.timeline[-1][1:] == entry[1:]:
            return
        if self.timeline and self.timeline[-1][0] == t:
            self.timeline[-1] = entry
        else:
            self.timeline.append(entry)

    def max_measured_power(self, t0: float, t1: float) -> int:
        """Highest measured draw at any instant in [t0, t1] (exact: the
        timeline records each change at the time it happened)."""
        self.sync()
        return self._max_over(t0, t1, 1)

    def max_power_limit(self, t0: float, t1: float) -> int:
        self.sync()
        return self._max_over(t0, t1, 2)

    def _max_over(self, t0: float, t1: float, idx: int) -> int:
        best = None
        for i, entry in enumerate(self.timeline):
            start = entry[0]
            end = self.timeline[i + 1][0] if i + 1 < len(self.timeline) else float("inf")
            if end <= t0 or start > t1:
                continue
            if end == start:
                continue
            best = entry[idx] if best is None else max(best, entry[idx])
        return 0 if best is None else best

    # ── time evolution ────────────────────────────────────────────────────
    def sync(self) -> None:
        with self._lock:
            self._sync(self.clock())

    def _integrate(self, t: float) -> None:
        dt = t - self._last_sync
        if dt > 0:
            wh = self._measured() * 100.0 * dt / 3600.0
            self.total_energy_wh += wh
            self.session_energy_wh += wh
            self._last_sync = t

    def _next_event(self, now: float) -> tuple[float, str, Any] | None:
        cands: list[tuple[float, str, Any]] = []
        for reg, t in self._expiry.items():
            cands.append((t, "expire", reg))
        if self._start_complete_at is not None:
            cands.append((self._start_complete_at, "started", None))
        for item in self._delayed:
            cands.append((item[0], "delayed", item))
        if (
            self.fallback_on_silence and not self._fallback
            and self._last_comm is not None
        ):
            cands.append((self._last_comm + self.effective_validity, "fallback", None))
        due = [c for c in cands if c[0] <= now]
        if not due:
            return None
        return min(due, key=lambda c: (c[0], c[1]))

    def _sync(self, now: float) -> None:
        while True:
            ev = self._next_event(now)
            if ev is None:
                break
            t, kind, arg = ev
            self._integrate(t)
            if kind == "expire":
                self._expiry.pop(arg, None)
                self._revert_register(arg)
            elif kind == "started":
                self._start_complete_at = None
                if self._state == STARTING:
                    self._enter_charging()
            elif kind == "delayed":
                self._delayed.remove(arg)
                _, _, address, value, rec = arg
                self._apply(address, value, t)
                rec.applied_at = t
            elif kind == "fallback":
                self._fallback = True
                self._expiry.clear()
                self.holding[REG_MAX_CHARGING_CURRENT] = self.max_current_raw
                self.holding[REG_MAX_CHARGING_POWER] = self.max_power_raw
                if self._state == ZERO_PAUSED:
                    self._enter_charging()
                self._event(t, "fallback_active")
            self._record_timeline(t)
        self._integrate(now)
        self._record_timeline(now)

    def _revert_register(self, reg: int) -> None:
        # The setpoint reverting to the device max on expiry never resumes a
        # paused/finished session by itself (measured on firmware 1.8):
        # only a fresh nonzero write does, via `_begin_session`.
        maximum = self.max_power_raw if reg == REG_MAX_CHARGING_POWER else self.max_current_raw
        self.holding[reg] = maximum

    def _enter_charging(self) -> None:
        if self._state in (CONNECTED, FINISHED, IDLE):
            self.session_energy_wh = 0.0
        self._state = CHARGING
        self.stop_reason = 0

    def _begin_session(self, now: float) -> None:
        """Start (or implicitly resume) a session, same firmware sequencing
        whichever trigger caused it: status goes to 2 (starting) and only
        reaches 3 (charging) `start_delay` seconds later."""
        if self.start_delay > 0:
            self._state = STARTING
            self._start_complete_at = now + self.start_delay
        else:
            self._enter_charging()

    def _event(self, t: float, what: str) -> None:
        self.log.append(LogEntry(next(self._seq), t, "event", value=what))

    def _touch(self, now: float) -> None:
        """A request reached the device: the silence timer restarts. An
        active fallback persists until a fresh current/power setpoint is
        written (ASSUMED; reads alone do not restore control)."""
        self._last_comm = now

    def _leave_fallback(self, now: float) -> None:
        if self._fallback:
            self._fallback = False
            self._event(now, "fallback_cleared")

    # ── write semantics ───────────────────────────────────────────────────
    def _validate(self, address: int, value: int) -> None:
        ok_ranges = {
            REG_WORK_MODE: (0, 2),
            REG_MAX_CHARGING_CURRENT: (MIN_CURRENT_RAW, self.max_current_raw),
            REG_MAX_CHARGING_POWER: (0, self.max_power_raw),
            REG_ALLOWED_CHARGE_TIME: (0, 0xFFFF),
            REG_ALLOWED_CHARGE_ENERGY: (0, 0xFFFF),
            REG_TIME_VALIDITY: (1, 0xFFFF),
            REG_DEFAULT_CURRENT: (MIN_CURRENT_RAW, self.max_current_raw),
            REG_LOCK_CONTROL: (0, 2),
            REG_CHARGING_CONTROL: (0, 2),
        }
        if address in (REG_AUTO_PHASE_SWITCH, REG_MIN_SWITCH_INTERVAL, REG_PHASE_SWITCHING):
            raise _refused(f"0x{address:04X} unsupported on single-phase", code=2)
        if address == REG_RESTART:
            if value != 0xA5A5:
                raise _refused("bad restart value")
            return
        if address not in ok_ranges:
            raise _refused(f"illegal data address 0x{address:04X}", code=2)
        lo, hi = ok_ranges[address]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise _refused(f"illegal value {value!r} for 0x{address:04X}")
        if address == REG_CHARGING_CONTROL and value == 1:
            if self._state in SESSION_RUNNING or not self.cable_connected:
                self.refused_starts += 1
                raise _refused("Start refused: session already running / no cable")
        if address == REG_CHARGING_CONTROL and value == 2:
            # A zero pause reported as status 5 is finished to the firmware.
            if self._state not in SESSION_RUNNING or self.status == 5:
                raise _refused("Stop refused: no session running")

    def _apply(self, address: int, value: int, now: float) -> None:
        if address in (REG_MAX_CHARGING_POWER, REG_MAX_CHARGING_CURRENT):
            self._leave_fallback(now)
        if address == REG_MAX_CHARGING_POWER:
            self.holding[address] = value
            self._expiry[address] = now + self.effective_validity
            if value == 0:
                if self.zero_pause_works and self._state in (CHARGING, STARTING):
                    self._state = ZERO_PAUSED
                    self.stop_reason = 1
                    self._start_complete_at = None
            elif self.implicit_resume and self._state in IMPLICIT_RESUME_FROM:
                if self._state != ZERO_PAUSED:
                    self.implicit_resumes += 1
                self._begin_session(now)
        elif address == REG_MAX_CHARGING_CURRENT:
            self.holding[address] = value
            self._expiry[address] = now + self.effective_validity
            if self.implicit_resume and self._state in (CONNECTED, FINISHED):
                self.implicit_resumes += 1
                self._begin_session(now)
        elif address == REG_CHARGING_CONTROL:
            if value == 1:
                if self._state in SESSION_RUNNING or not self.cable_connected:
                    return  # a delayed Start that lost the race
                self._begin_session(now)
            elif value == 2:
                if self._state in SESSION_RUNNING:
                    self._state = FINISHED
                    self.stop_reason = 1
                    self._start_complete_at = None
        elif address == REG_LOCK_CONTROL:
            if value == 2:
                self.lock_status = 1
            elif value == 1:
                self.lock_status = 0
        elif address == REG_RESTART:
            self._event(now, "device_restart")
        else:
            self.holding[address] = value
            if address == REG_WORK_MODE and value == 2 and self._state in SESSION_RUNNING:
                self._state = FINISHED

    # ── fault injection API ───────────────────────────────────────────────
    def inject_write(
        self, address: int | None, mode: str, *, value: int | None = None,
        delay: float = 0.0, count: int | None = 1,
    ) -> None:
        """Queue a write behaviour for matching writes. `count=None` makes
        it persistent (e.g. a Stop that is always refused)."""
        if mode not in WRITE_MODES:
            raise ValueError(mode)
        self._rules.append(_Rule(address, mode, value, delay, count))

    def clear_write_rules(self) -> None:
        self._rules.clear()

    def fail_reads(self, address: int | None = None, count: int | None = 1) -> None:
        """Make reads covering `address` raise TransportError (count=None:
        until clear_read_failures)."""
        self._read_failures.append((address, count))

    def clear_read_failures(self) -> None:
        self._read_failures.clear()

    def set_io_down(self, down: bool = True) -> None:
        """Nothing reaches the device (network loss): every request raises
        TransportError and the device sees silence."""
        self.io_down = down

    def connectivity_burst(self, seconds: float) -> None:
        """Simulate a transient link outage: every read/write raises
        TransportError until `seconds` of virtual time have passed, then
        clears itself automatically (no manual `set_io_down(False)` needed).
        Time only advances via later `advance`/`sim.clock` calls, exactly
        like `set_io_down`."""
        self._burst_until = self.clock() + seconds

    def hold_writes(
        self, address: int | None = None, *, value: int | None = None, count: int | None = 1,
    ) -> WriteGate:
        """Hold matching writes in flight (not yet applied) until the
        returned gate is released."""
        gate = WriteGate(address, value, count)
        self._gates.append(gate)
        return gate

    def set_invalid_telemetry(self, status: int | None = INVALID, cc: int | None = INVALID) -> None:
        self.status_override = status
        self.cc_override = cc

    def clear_invalid_telemetry(self) -> None:
        self.status_override = None
        self.cc_override = None

    # ── physical/vehicle actions ──────────────────────────────────────────
    def unplug(self) -> None:
        with self._lock:
            self._sync(self.clock())
            self._state = IDLE
            self.stop_reason = 7
            self._expiry.clear()
            self._start_complete_at = None
            self.holding[REG_MAX_CHARGING_POWER] = self.max_power_raw
            self.holding[REG_MAX_CHARGING_CURRENT] = self.max_current_raw
            self._record_timeline(self.clock())

    def plug(self) -> None:
        with self._lock:
            self._sync(self.clock())
            self._state = CONNECTED
            if self.holding[REG_WORK_MODE] == 1:  # Plug&Charge starts by itself
                self._enter_charging()
            self._record_timeline(self.clock())

    def car_pause(self) -> None:
        self._set_state_if(CHARGING, CAR_PAUSED)

    def car_resume(self) -> None:
        self._set_state_if(CAR_PAUSED, CHARGING)

    def begin_phase_switch(self) -> None:
        self._set_state_if(CHARGING, PHASE_SWITCHING)

    def end_phase_switch(self) -> None:
        self._set_state_if(PHASE_SWITCHING, CHARGING)

    def external_start(self) -> None:
        """Something other than HA starts a session (RFID, app, button)."""
        with self._lock:
            self._sync(self.clock())
            if self.cable_connected and self._state not in SESSION_RUNNING:
                self._enter_charging()
            self._record_timeline(self.clock())

    def raise_fault(self, bits: int = 1) -> None:
        with self._lock:
            self._sync(self.clock())
            self.fault_code = bits
            self._state = FAULT
            self._record_timeline(self.clock())

    def _set_state_if(self, expected: str, new: str) -> None:
        with self._lock:
            self._sync(self.clock())
            if self._state != expected:
                raise AssertionError(f"sim state is {self._state}, expected {expected}")
            self._state = new
            self._record_timeline(self.clock())

    # ── process lifecycle ─────────────────────────────────────────────────
    def simulate_process_restart(self) -> "SimCharger":
        """HA restarted: the old client is gone, the device keeps its state.
        Returns this sim, reopened, to hand to the new controller."""
        self.closed = False
        self.restarts += 1
        self.log.append(LogEntry(next(self._seq), self.clock(), "restart"))
        return self

    # ── register image ────────────────────────────────────────────────────
    def _status_block(self) -> list[int]:
        regs = [0] * REG_STATUS_BLOCK_COUNT
        status = self.status if self.status_override is None else self.status_override
        cc = (1 if self.cable_connected else 0) if self.cc_override is None else self.cc_override
        measured = self._measured()
        regs[0] = 1
        regs[1] = 0x0108
        regs[2] = self.stop_reason
        regs[3] = status
        regs[4] = 1 if not self.cable_connected else (3 if self._state == CHARGING else 2)
        regs[5] = cc
        regs[6] = regs[7] = 750
        regs[8] = NOMINAL_VOLTAGE * 10
        regs[11] = int(measured * 10000 // NOMINAL_VOLTAGE) if measured else 0
        regs[14] = measured
        regs[15] = self.lock_status
        regs[17] = self.max_power_raw
        regs[19] = self.max_current_raw
        regs[20] = MIN_CURRENT_RAW
        regs[21] = self.alarm_code
        total = int(self.total_energy_wh // 100)
        session = int(self.session_energy_wh // 100)
        regs[22], regs[23] = (total >> 16) & 0xFFFF, total & 0xFFFF
        regs[24], regs[25] = (session >> 16) & 0xFFFF, session & 0xFFFF
        regs[26], regs[27] = (self.fault_code >> 16) & 0xFFFF, self.fault_code & 0xFFFF
        return regs

    @staticmethod
    def _ascii(text: str, count: int) -> list[int]:
        raw = text.encode("ascii")[: count * 2].ljust(count * 2, b"\x00")
        return [(raw[i] << 8) | raw[i + 1] for i in range(0, count * 2, 2)]

    def _register_image(self) -> dict[int, int]:
        image: dict[int, int] = {}
        for i, v in enumerate(self._status_block()):
            image[REG_STATUS_BLOCK_START + i] = v
        for i, v in enumerate(self._ascii(self.model_code, 4)):
            image[REG_ID_MODEL_CODE + i] = v
        for i, v in enumerate(self._ascii(self.serial, 16)):
            image[REG_ID_SERIAL_NUMBER + i] = v
        for reg, v in self.holding.items():
            image[reg] = v
        return image

    # ── core request handling (sync; thread safe) ─────────────────────────
    def _check_link(self, kind: str, address: int, value: Any, log: bool = True) -> None:
        in_burst = self._burst_until is not None and self.clock() < self._burst_until
        reason = "closed" if self.closed else "io_down" if (self.io_down or in_burst) else None
        if reason is None:
            return
        if log:
            self.log.append(LogEntry(next(self._seq), self.clock(), kind, address, value, reason))
        raise TransportError(f"request not delivered ({reason})")

    def read_sync(self, address: int, count: int) -> tuple[int, ...]:
        with self._lock:
            self._check_link("read", address, count)
            for i, (addr, remaining) in enumerate(self._read_failures):
                if addr is None or address <= addr < address + count:
                    if remaining is not None:
                        if remaining <= 1:
                            self._read_failures.pop(i)
                        else:
                            self._read_failures[i] = (addr, remaining - 1)
                    self.log.append(LogEntry(next(self._seq), self.clock(), "read", address, count, "failed"))
                    raise TransportError(f"read 0x{address:04X} timed out")
            now = self.clock()
            self._sync(now)
            self._touch(now)
            image = self._register_image()
            try:
                values = tuple(image[address + i] for i in range(count))
            except KeyError:
                self.log.append(LogEntry(next(self._seq), now, "read", address, count, "refused"))
                raise _refused(f"illegal data address 0x{address:04X}", code=2) from None
            self.log.append(LogEntry(next(self._seq), now, "read", address, count, "ok"))
            return values

    def _take_rule(self, address: int, value: int) -> _Rule | None:
        for i, rule in enumerate(self._rules):
            if rule.address is not None and rule.address != address:
                continue
            if rule.value is not None and rule.value != value:
                continue
            if rule.remaining is not None:
                rule.remaining -= 1
                if rule.remaining <= 0:
                    self._rules.pop(i)
            return rule
        return None

    def write_sync(self, address: int, value: int, *, delivered: bool = False) -> None:
        with self._lock:
            now = self.clock()
            rule = self._take_rule(address, value)
            mode = rule.mode if rule else "ok"
            rec = WriteRecord(next(self._seq), now, address, value, mode)
            self.writes.append(rec)
            self.log.append(LogEntry(rec.seq, now, "write", address, value, mode))
            try:
                if not delivered:
                    self._check_link("write", address, value, log=False)
            except TransportError:
                rec.raised = "TransportError"
                self.log[-1].outcome = "closed" if self.closed else "io_down"
                raise
            if mode == "transport_error":
                rec.raised = "TransportError"
                raise TransportError(f"write 0x{address:04X}: connection reset")
            self._sync(now)
            if mode == "lost_request":
                # the frame was sent but never processed: silence continues
                rec.raised = "UnknownOutcome"
                raise UnknownOutcome(f"write 0x{address:04X}: no reply")
            self._touch(now)
            if mode == "refuse":
                rec.raised = "CommandRefused"
                raise _refused(f"write 0x{address:04X} refused (injected)")
            try:
                self._validate(address, value)
            except TransportError:
                rec.raised = "CommandRefused"
                rec.mode = "refused_by_device"
                raise
            if mode == "ignored":
                rec.acknowledged = True
                return
            if mode == "delayed":
                rec.acknowledged = True
                self._delayed.append((now + (rule.delay if rule else 0.0), rec.seq, address, value, rec))
                return
            self._apply(address, value, now)
            rec.applied_at = now
            self._record_timeline(now)
            if mode == "lost_reply":
                rec.raised = "UnknownOutcome"
                raise UnknownOutcome(f"write 0x{address:04X}: reply lost")
            rec.acknowledged = True

    # ── RegisterIO ────────────────────────────────────────────────────────
    async def _reply_delay(self) -> None:
        if not self._reply_latency:
            await asyncio.sleep(0)
            return
        sleep = getattr(self.clock, "sleep", None)
        if sleep is not None:
            await sleep(self._reply_latency)  # advances in step with VirtualClock
        else:
            await asyncio.sleep(self._reply_latency)

    async def read(self, address: int, count: int) -> tuple[int, ...]:
        await self._reply_delay()
        return self.read_sync(address, count)

    async def write(self, address: int, value: int) -> None:
        await self._reply_delay()
        gate = next((g for g in self._gates if g.matches(address, value)), None)
        if gate is None:
            self.write_sync(address, value)
            return
        # The frame is on the wire from here on: cancelling the caller
        # cannot recall it, it still lands when the gate opens.
        with self._lock:
            self._check_link("write", address, value)
        if gate.remaining is not None:
            gate.remaining -= 1
        gate.held += 1
        gate.entered.set()
        inner = asyncio.ensure_future(self._held_write(gate, address, value))
        inner.add_done_callback(lambda f: f.cancelled() or f.exception())
        await asyncio.shield(inner)

    async def _held_write(self, gate: WriteGate, address: int, value: int) -> None:
        await gate._released.wait()
        self.write_sync(address, value, delivered=True)

    async def close(self) -> None:
        with self._lock:
            self.closed = True
            self.close_count += 1
            self.log.append(LogEntry(next(self._seq), self.clock(), "close"))

    # ── assertion helpers ─────────────────────────────────────────────────
    def mark(self) -> int:
        """Sequence number to slice logs from."""
        return next(self._seq)

    def writes_since(self, mark: int = 0, address: int | None = None) -> list[WriteRecord]:
        return [
            w for w in self.writes
            if w.seq >= mark and (address is None or w.address == address)
        ]

    def wire_writes(self, mark: int = 0) -> list[tuple[int, int]]:
        """(address, value) of every write that reached the wire, in order."""
        return [
            (w.address, w.value) for w in self.writes_since(mark)
            if w.raised != "TransportError"
        ]

    def positive_cap_writes(self, mark: int = 0) -> list[WriteRecord]:
        """Writes that could implicitly resume charging on this firmware."""
        return [
            w for w in self.writes_since(mark)
            if w.raised != "TransportError"
            and (
                (w.address == REG_MAX_CHARGING_POWER and w.value > 0)
                or w.address == REG_MAX_CHARGING_CURRENT
            )
        ]


class LegacyClient:
    """Synchronous FoxESSModbusClient-shaped view of a SimCharger, for
    driving historical (pre-rebuild) coordinator code. Failures are mapped
    to the old client's conventions: reads return None, writes return
    False; neither distinguishes refusal from a lost reply."""

    def __init__(self, sim: SimCharger) -> None:
        self.sim = sim
        self.txid_mismatches = self.short_reads = self.connection_errors = 0
        self.malformed_headers = self.unit_id_mismatches = 0
        self.write_echo_mismatches = 0

    def read_registers(self, address: int, count: int, quiet: bool = False):
        try:
            return list(self.sim.read_sync(address, count))
        except TransportError:
            return None

    def read_ascii(self, address: int, reg_count: int):
        regs = self.read_registers(address, reg_count, quiet=True)
        if regs is None:
            return None
        raw = b"".join(bytes(((r >> 8) & 0xFF, r & 0xFF)) for r in regs)
        return raw.rstrip(b"\x00").decode("ascii", errors="replace") or None

    def write_holding_register(self, address: int, value: int) -> bool:
        try:
            self.sim.write_sync(address, value)
        except TransportError:
            return False
        return True

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass
