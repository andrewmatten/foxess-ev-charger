"""Pure energy-counter plausibility guard.

Deliberately has zero imports from homeassistant.* (or anything else that
touches hass/coordinator state) - the accept/reject decision here is plain
arithmetic over primitives, so it can be tested directly with no HA test
harness, no coordinator instance, and no stubbing of any kind.

FoxESSChargerCoordinator._sanitize_energy (see __init__.py) is a thin
stateful wrapper around decide_energy_reading(): it owns _last_energy /
energy_rejections and the datetime/logging side effects, and delegates the
actual "is this raw value plausible" question to this module.

Two real incidents (2026-08-27 19:53, 2026-09-02 19:19) each landed within
seconds of a charger fault (status -> fault, fault_code changing) and
produced wildly implausible raw values - one of them the exact same bogus
number (65800, i.e. 6580.0 kWh) both times, so this is a repeatable bad
read tied to the fault transition, not random noise.

Both energy sensors are TOTAL_INCREASING, so HA's recorder folds them into
a cumulative sum forever: a single bad sample doesn't just show a wrong
number for a few seconds, it permanently corrupts the long-term statistics
until someone manually finds and fixes it. total_energy and current_energy
share this exposure but differ in one way: total_energy is a lifetime
counter where any decrease is implausible, while current_energy legitimately
resets to zero at the start of every session (see ENERGY_GUARDS in
const.py, which carries that per-counter difference).

2026-09: the threshold below originally had no floor for the register's own
resolution (ENERGY_QUANTUM_KWH) - at short poll intervals a single genuine
step could arrive faster than the pure rate-based limit allowed, rejecting
essentially every real reading during active charging.
"""
from __future__ import annotations

from dataclasses import dataclass

# The register only reports whole ENERGY_QUANTUM_KWH steps, so a single
# genuine tick can legitimately land entirely within one poll no matter how
# short the poll interval is - the old rate-only threshold (max_power *
# elapsed * margin) had no floor for this and rejected essentially every
# real reading once the poll interval dropped below roughly
# (quantum / rated_power * 3600) seconds. The fix floors the allowance at
# one full quantum, then adds a time-scaled term on top (still using
# elapsed_h * ENERGY_GUARD_SAFETY_FACTOR, not the actual measured charging
# rate) to keep catching genuine anomalies - the original guard's real
# target was a single ~6580 kWh bogus read tied to a fault-event Modbus
# desync, several orders of magnitude beyond anything this loosens.
#
# SAFETY_FACTOR > 1 makes rejection of a physically-real reading impossible
# at any elapsed time: the true maximum a real reading can jump by is at
# most one pending quantum plus rated_power * elapsed (never more, since the
# register can't report faster than power delivery allows), while this
# formula allows one quantum plus rated_power * elapsed * SAFETY_FACTOR -
# strictly more, for any elapsed time. A jump exceeding this allowance is
# therefore always genuinely implausible, not a false rejection.
ENERGY_QUANTUM_KWH = 0.1
ENERGY_GUARD_SAFETY_FACTOR = 1.5

# 2026-09 (second audit): no register on this hardware family (7.3-22kW EV
# chargers) could ever legitimately accumulate this much lifetime energy -
# it's an absolute backstop, not a tuned threshold, deliberately several
# orders of magnitude above anything real (a 22kW charger run flat-out
# 24/7 would take about 189 days to reach it). Applied unconditionally,
# including to the very first observation for a key (prev=None): without
# this, a corrupt first-ever reading (e.g. a garbage Modbus response right
# at startup) becomes the new trusted baseline forever, with nothing on
# record yet to compare it against.
ENERGY_ABS_MAX_KWH = 100_000.0

# How close to zero a decrease must land to be treated as a session-boundary
# reset when the caller can't independently confirm one occurred (see
# `session_boundary` below) - one quantum's margin past absolute zero, for
# read timing/rounding, not a real non-zero baseline.
NEAR_ZERO_RAW_UNITS = 2

# A rolling window's worth of accepted deltas is checked against what the
# charger's rated max could actually have delivered over that window (see
# check_cumulative_window below) - catches a sustained corruption of exactly
# one register quantum per poll, which individually always passes the
# per-poll floor above no matter how short the poll interval, but which sums
# to a physically impossible rate over a long enough stretch. 30 minutes is
# long enough that a real charger's rated-max delivery over the window is
# well below what "one quantum every ~10-13s poll" would sum to, while still
# being short enough to recover quickly once a corrupted run of reads stops.
ENERGY_WINDOW_SECONDS = 1800


@dataclass(frozen=True)
class EnergyGuardDecision:
    """Result of one plausibility check against a single energy counter.

    `delta_kwh` and `max_plausible_kwh` are populated even when accepted, so
    callers that want to log/record context don't have to recompute the
    same arithmetic a second time.
    """

    accepted: bool
    value: int | None  # raw value to treat as the new last-known-good; None if rejected
    delta_kwh: float
    elapsed_s: float
    max_plausible_kwh: float


def decide_energy_reading(
    key: str,
    raw: int,
    prev: int | None,
    prev_ts: float | None,
    now: float,
    max_power_kw: float,
    allow_decrease: bool,
    session_boundary: bool = False,
) -> EnergyGuardDecision:
    """Decides whether `raw` is a plausible next reading for a counter.

    Pure function: takes only primitives, touches no clock but the
    caller-supplied `now`, and has no notion of "coordinator" or "hass".

    `prev`/`prev_ts` being None means there is no last-known-good reading
    yet for this key (the very first read this process) - there is nothing
    to compare a *rate* against, but `raw` is still run past the absolute
    ENERGY_ABS_MAX_KWH backstop below, so a wildly corrupt first-ever
    reading can't become the new trusted baseline forever.

    `max_power_kw` should be the charger's rated maximum, not the live
    instantaneous draw - see the SAFETY_FACTOR comment above for why that's
    what keeps this a true worst-case bound rather than a tracking filter.

    `allow_decrease` marks a counter that legitimately resets at a session
    boundary (current_energy_raw); it does not mean "any decrease is fine".
    A decrease is only ever accepted when it actually looks like a reset:
    either the caller's own session-tracking state confirms a real boundary
    just occurred (`session_boundary=True` - the strong signal), or, lacking
    that, the new value itself lands at/near zero (the weak signal - what a
    reset actually looks like). Any other decrease (e.g. an unexplained
    mid-session drop) is rejected exactly like any other implausible
    reading, the same as a lifetime counter's decrease already is.

    `key` is accepted (and unused beyond identifying the counter to the
    caller) purely so call sites and log messages have it on hand; the
    decision itself only depends on the other arguments.
    """
    if raw < 0 or raw * ENERGY_QUANTUM_KWH > ENERGY_ABS_MAX_KWH:
        return EnergyGuardDecision(
            False, None, round(raw * ENERGY_QUANTUM_KWH, 2), 0.0, ENERGY_ABS_MAX_KWH
        )

    if prev is None or prev_ts is None:
        return EnergyGuardDecision(True, raw, 0.0, 0.0, 0.0)

    elapsed_s = max(now - prev_ts, 1.0)  # floor at 1s
    delta = raw - prev

    # One full register quantum is always allowed regardless of elapsed
    # time - a real step can legitimately land entirely within a single
    # poll - plus a time-scaled allowance on top for genuinely fast
    # multi-step jumps.
    max_plausible_delta = (
        ENERGY_QUANTUM_KWH
        + max_power_kw * elapsed_s / 3600 * ENERGY_GUARD_SAFETY_FACTOR
    ) / ENERGY_QUANTUM_KWH  # convert back to raw (ENERGY_QUANTUM_KWH-sized) units

    if delta < 0:
        # A confirmed new session permits a non-zero counter reset, but the
        # value must still fit the energy the charger could have delivered
        # since the previous observation. This preserves legitimate reads
        # that occur after charging has begun while rejecting stale/corrupt
        # values copied from a previous session.
        boundary_max_raw = max_plausible_delta
        plausible_reset = session_boundary and raw <= boundary_max_raw
        near_zero_reset = raw <= NEAR_ZERO_RAW_UNITS
        implausible = not (allow_decrease and (plausible_reset or near_zero_reset))
    else:
        implausible = delta > max_plausible_delta

    delta_kwh = round(delta * ENERGY_QUANTUM_KWH, 2)
    max_plausible_kwh = round(max_plausible_delta * ENERGY_QUANTUM_KWH, 2)

    if implausible:
        return EnergyGuardDecision(False, None, delta_kwh, elapsed_s, max_plausible_kwh)

    return EnergyGuardDecision(True, raw, delta_kwh, elapsed_s, max_plausible_kwh)


def check_cumulative_window(
    total_delta_kwh: float, window_elapsed_s: float, max_power_kw: float,
) -> bool:
    """True if a rolling window's cumulative *accepted* energy is itself
    implausible for a charger rated at max_power_kw, even though every
    individual reading inside the window passed decide_energy_reading() on
    its own.

    Catches a sustained corruption of exactly one register quantum per
    poll: any single such delta is always within decide_energy_reading()'s
    per-poll floor (one quantum is unconditionally allowed, however short
    the poll interval), so no individual sample ever trips it. Summed
    continuously over a long enough window, though, a real charger
    physically cannot have delivered that much energy - so an implausible
    *sum* is the tell, even when every individual sample looked fine in
    isolation. Same SAFETY_FACTOR-based margin as the per-poll check, so a
    real (if unusually bursty) charging pattern is never flagged.

    Pure function: the caller owns the actual rolling window (a bounded
    list of timestamped deltas) - this only judges one already-summarised
    window, the same separation of concerns as decide_energy_reading()
    itself.
    """
    if window_elapsed_s <= 0:
        return False
    max_plausible_kwh = (
        ENERGY_QUANTUM_KWH
        + max_power_kw * (window_elapsed_s / 3600) * ENERGY_GUARD_SAFETY_FACTOR
    )
    return total_delta_kwh > max_plausible_kwh
