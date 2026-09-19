"""Constants for FoxESS EV Charger integration."""
import logging

from homeassistant.const import Platform

_LOGGER = logging.getLogger(__name__)

# ENERGY_QUANTUM_KWH / ENERGY_GUARD_SAFETY_FACTOR live in energy_guard.py
# alongside the pure decide_energy_reading() function that actually uses
# them, so the guard's threshold constants and its decision logic can't
# drift apart into two copies again. Re-exported here for backward
# compatibility with existing imports from this module.
from .energy_guard import ENERGY_QUANTUM_KWH, ENERGY_GUARD_SAFETY_FACTOR  # noqa: F401

DOMAIN = "foxess_charger"

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

# Config Keys
CONF_HOST     = "host"
CONF_PORT     = "port"
CONF_SLAVE_ID = "slave_id"

# Defaults
DEFAULT_PORT          = 502
DEFAULT_SLAVE_ID      = 1
DEFAULT_SCAN_INTERVAL = 10

# Single batched FC03 read covering the whole status+energy+fault+RFID block
# (0x1000–0x101D inclusive, 30 registers) - confirmed safe on single-phase
# hardware: unlike 0x3000-0x300B (see REG_AUTO_PHASE_SWITCH below), nothing
# in this range is phase-switch-box-only, so merging it into one request
# cannot reproduce the Round 2 Illegal Data Address bug. Cuts what used to
# be 5 separate Modbus round trips per poll (status block + 4x UINT32 reads)
# down to 1.
REG_STATUS_BLOCK_START = 0x1000
REG_STATUS_BLOCK_COUNT = 30  # 0x1000..0x101D inclusive

# ── Read-Only Input Registers (0x1000–0x1022) ─────────────────────────────────
REG_DEVICE_ADDRESS  = 0x1000
REG_SOFTWARE_VER    = 0x1001
REG_STOP_REASON     = 0x1002
REG_STATUS          = 0x1003
REG_CP_STATUS       = 0x1004
REG_CC_STATUS       = 0x1005
REG_PORT_TEMP       = 0x1006
REG_AMBIENT_TEMP    = 0x1007
REG_L1_VOLTAGE      = 0x1008
REG_L2_VOLTAGE      = 0x1009
REG_L3_VOLTAGE      = 0x100A
REG_L1_CURRENT      = 0x100B
REG_L2_CURRENT      = 0x100C
REG_L3_CURRENT      = 0x100D
REG_ACTIVE_POWER    = 0x100E
REG_LOCK_STATUS     = 0x100F
REG_PHASE_SEQUENCE  = 0x1010
REG_MAX_POWER       = 0x1011
REG_MIN_POWER       = 0x1012
REG_MAX_CURRENT     = 0x1013
REG_MIN_CURRENT     = 0x1014
REG_ALARM_CODE      = 0x1015
# These two were swapped until 2.1.2. Confirmed against live hardware: the
# register at 0x1016 never resets between sessions (lifetime), while 0x1018
# starts from zero each time charging begins and tracked kW x elapsed time
# exactly over a 2h13m session. The old mapping made Total read *lower* than
# the current session, which is impossible.
REG_TOTAL_ENERGY    = 0x1016  # UINT32 (2 Register) - lifetime, never resets
REG_CURRENT_ENERGY  = 0x1018  # UINT32 (2 Register) - resets each session
REG_FAULT_CODE      = 0x101A  # UINT32 (2 Register)
REG_RFID_CARD       = 0x101C  # UINT32 (2 Register)
REG_ID_MODEL_CODE   = 0x101E  # ASCII (4 Register / 8 Bytes)
REG_ID_SERIAL_NUMBER= 0x1022  # ASCII (16 Register / 32 Bytes)

# ── Read/Write Holding Registers (0x3000–0x300B) ──────────────────────────────
REG_WORK_MODE            = 0x3000  # 0=Controlled, 1=Plug&Charge, 2=Locked
REG_MAX_CHARGING_CURRENT = 0x3001  # 0.1 A, 6–32 A
REG_MAX_CHARGING_POWER   = 0x3002  # 0.1 kW
REG_ALLOWED_CHARGE_TIME  = 0x3003  # min
REG_ALLOWED_CHARGE_ENERGY= 0x3004  # kWh
REG_TIME_VALIDITY        = 0x3005  # s, originally documented 10-60s - live
                                    # evidence (Andrew's charger, confirmed
                                    # 2026-09-18) shows this register actually
                                    # accepts at least 180s. Do not clamp a
                                    # live-read value down to the old
                                    # documented range; see number.py's
                                    # native_max_value for the corrected UI
                                    # bound and get_heartbeat_interval() below
                                    # for why the *floor* on the derived
                                    # heartbeat interval is a separate concern
                                    # from this register's own valid range.
REG_DEFAULT_CURRENT      = 0x3006  # 0.1 A, 6–32 A
REG_AUTO_PHASE_SWITCH    = 0x300A  # 0=off, 1=on
REG_MIN_SWITCH_INTERVAL  = 0x300B  # min, 5–30

# ── Write-Only Registers (0x4000–0x4003) ─────────────────────────────────────
REG_LOCK_CONTROL     = 0x4000  # 0=No action, 1=Unlock, 2=Lock
REG_CHARGING_CONTROL = 0x4001  # 0=No action, 1=Start, 2=Stop
REG_PHASE_SWITCHING  = 0x4002  # 0=3-phase, 1=L2, 2=L3
REG_RESTART          = 0x4003  # 0xA5A5 = Restart

# ── Status Maps ───────────────────────────────────────────────────────────────
STATUS_MAP = {
    0: "idle",
    1: "connected",
    2: "start",
    3: "charging",
    4: "paused",
    5: "finished",
    6: "fault",
    7: "reserved",
    8: "locked",
}

CP_STATUS_MAP = {
    0: "fault",
    1: "12v_disconnected",
    2: "9v_connected",
    3: "6v_ready",
}

# Lowercase snake_case state keys, not the display-cased strings ("Plug&Charge"
# included an ampersand!) previously used directly as the ENUM sensor's and
# the Work Mode select's state/option values. State values need to be stable
# machine identifiers that translations/en.json and de.json's state maps key
# off of - a display label is not a valid state value (HA enum device
# classes expect exactly this style, e.g. STATUS_MAP below).
WORK_MODE_MAP    = {0: "controlled", 1: "plug_and_charge", 2: "locked"}

# The select entity's `options` contract is separate from the sensor's enum
# states. HA validates select.select_option against entity.options *before*
# ever calling async_select_option - a same-entity compatibility shim can't
# intercept an old value, because the call never reaches the entity if the
# value isn't already in `options`. So the select keeps the original
# display-cased strings through 2.2.x; only the plain sensor's state (which
# nothing calls a service against) moves to the new lowercase identifiers
# now. The select migrates to lowercase in 3.0.0, as a documented breaking
# change with its own audit/tests - not bundled into 2.2.0.
WORK_MODE_SELECT_OPTIONS = {0: "Controlled", 1: "Plug&Charge", 2: "Locked"}

# Simple 2-value enums per spec (0x1005 CC Status, 0x100F Lock Status) -
# table-driven for the same reason as the bigger maps above: an unrecognized
# raw value should surface as unknown, not silently fall through a ternary
# to whichever label happens to be the "else" branch.
CC_STATUS_MAP   = {0: "disconnected", 1: "connected"}
LOCK_STATUS_MAP = {0: "unlocked", 1: "locked"}

# ── Fault/Alarm Bitmasks (Appendix 2 & 3) ──────────────────────────────────────
# fault_code (0x101A, UINT32) and alarm_code (0x1015, UINT16) are bitmasks -
# multiple conditions can be active at once, so these are decoded into a list
# of active names rather than looked up as a single enum value.
FAULT_BITS = {
    0:  "emergency_stop",
    1:  "overvoltage",
    2:  "undervoltage",
    3:  "overcurrent",
    4:  "charging_port_overtemp",
    5:  "pe_grounding",
    6:  "leakage_current",
    7:  "frequency",
    8:  "cp",
    9:  "connector",
    10: "ac_contactor",
    11: "electronic_lock",
    12: "breaker",
    13: "cc",
    14: "external_meter_communication",
    15: "metering_chip",
    16: "environment_temperature",
    17: "access_control",
}

ALARM_BITS = {
    0: "card_reader",
    1: "phase_cutting_box",
    2: "phase_loss",
}


def decode_bitmask(value: int | None, bit_map: dict[int, str], label: str = "bitmask") -> list[str]:
    """Returns the list of active condition names for a bitmask register.

    Any bit set in `value` that isn't one of `bit_map`'s documented bits is
    logged once (not silently dropped) - it means either firmware raised a
    condition this integration's copy of the spec's appendix doesn't know
    about yet, or the read is corrupt, and either way it's worth surfacing
    rather than the active-condition list just quietly missing an entry.
    """
    if not value:
        return []
    known_mask = 0
    active: list[str] = []
    for bit, name in bit_map.items():
        known_mask |= 1 << bit
        if value & (1 << bit):
            active.append(name)
    unknown_mask = value & ~known_mask
    if unknown_mask:
        _LOGGER.warning(
            "Unrecognized bit(s) set in %s: 0x%X (raw value 0x%X)",
            label, unknown_mask, value,
        )
    return active


def decode_enum(
    value: int | None, mapping: dict[int, str], label: str,
    unknown_label: str | None = None,
) -> str | None:
    """Maps a raw enum-valued register through `mapping`.

    Returns None for "no reading yet" (value is None), always - HA's own
    native unknown state is the right thing to show before the first read.

    For "unrecognized raw value" (value not None but not in mapping),
    returns `unknown_label` (default None, same as the "no reading yet"
    case). Callers backing a device_class=ENUM entity should pass
    `unknown_label="unknown"` (and add "unknown" to that entity's
    `options`/`_attr_options` - HA raises if an ENUM sensor's/select's
    current value isn't one of its declared options) so the two cases are
    distinguishable to the user via a real, translatable state instead of
    both collapsing into the same generic native "Unknown" - see
    translations/en.json and de.json's per-entity "unknown" state entries.
    Left as opt-in (default None) rather than the new behaviour everywhere,
    so plain string sensors without a declared options list (e.g.
    stop_reason) keep their original "just show nothing" behaviour.

    Either way, an unrecognized value is logged once per occurrence -
    likely either an undocumented firmware state or a corrupt/garbled read,
    worth surfacing rather than silently swallowed.
    """
    if value is None:
        return None
    if value not in mapping:
        _LOGGER.warning(
            "Unrecognized %s raw value: %r (expected one of %s)",
            label, value, sorted(mapping),
        )
        return unknown_label
    return mapping[value]


# Lowercase snake_case, matching translations/en.json and de.json's existing
# phase_sequence/phase_switching_control state keys - "L2_single"/"L3_single"
# never matched either file's "l2_single_phase"/"l3_single_phase" keys, so
# those states were always shown untranslated.
PHASE_SEQ_MAP    = {0: "three_phase", 1: "l2_single_phase", 2: "l3_single_phase"}
STOP_REASON_MAP  = {
    0: "none",           1: "command",          2: "time_completed",
    3: "s2_timeout",     4: "pause_timeout",    5: "emergency_stop",
    6: "cp_abnormal",    7: "connector_pulled", 8: "ac_contactor",
    9: "lock_abnormal", 10: "card_reader",      11: "overcurrent",
    12: "overvoltage",  13: "undervoltage",     14: "port_overtemp",
    15: "leakage",      16: "n_line_reversed",  17: "freq_abnormal",
    18: "stop_button",  19: "breaker",          20: "phase_loss",
    21: "pe_abnormal",  22: "ext_meter",        23: "ambient_overtemp",
    24: "metering_chip",25: "access_control",   26: "pbox_phase_switch",
    27: "energy_limit",
}

# ── Session / setpoint handling (added 2.2.0) ─────────────────────────────────
STATUS_CHARGING = 3

# Statuses that mean "a session is underway". Pause (4) counts: the car has
# suspended itself, which is not the same as the session having stopped -
# same reasoning as FoxESSChargingSwitch.is_on.
SESSION_ACTIVE_STATUSES = {2, 3, 4}

# Fired by FoxESSChargerCoordinator._track_session() (see __init__.py) the
# instant a genuine active -> inactive transition ends a session - and only
# then, never as a side effect of an entity's state being restored/written.
# device_trigger.py's "session_completed" trigger listens for this event
# rather than diffing the Last Session Duration sensor's value: a plain
# state-change trigger looked identical whether a session had genuinely just
# ended or the sensor's restored-at-startup state was merely being written
# for the first time this process, and two back-to-back real sessions with
# an identical (rounded) duration produced no state change at all and
# silently never fired. Event payload includes "entry_id" so a
# multi-charger install's device triggers only fire for their own device.
EVENT_SESSION_COMPLETED = f"{DOMAIN}_session_completed"

# Consecutive failures of the core status block (0x1000-0x101D - the whole
# REG_STATUS_BLOCK_COUNT batch above, not just up to alarm_code) before the
# coordinator declares the charger unreachable. Below this, one failed poll
# is absorbed by the seed-from-last-known-good pattern; at or above it,
# entities must go unavailable rather than keep showing stale values as live.
MAX_STATUS_READ_FAILURES = 3

# Minimum floor on how often setpoints may be re-written to the charger,
# regardless of the configured Command Time Validity - every write is
# Modbus traffic to an embedded stack that has already demonstrated it can
# desync under load, so re-assertion must never become a per-poll write loop
# even if time_validity were misconfigured to something tiny (or zero).
#
# Must stay comfortably *below* half of time_validity's own documented
# minimum (10s -> 5s), not above it: a floor of 10s used to defeat the
# entire half-validity guarantee get_heartbeat_interval() exists to provide
# right at that minimum - get_heartbeat_interval(10) was clamped back up to
# 10s instead of the 5s the charger's own Command Time Validity window
# actually requires. 3s leaves a real margin under 5s while still being far
# enough above zero to block a runaway sub-second write loop for an
# absurdly small/zero time_validity.
SETPOINT_REASSERT_MIN_INTERVAL = 3  # seconds

# Used before the first successful read of REG_TIME_VALIDITY (0x3005) has
# populated coordinator.data - a conservative placeholder only, not a
# real device value.
DEFAULT_TIME_VALIDITY = 60  # seconds

# 2026-09: adopted from a third-party PR (github.com/loadrunner42, upstream
# evcc-io/evcc's foxess-evc.go driver convention) after comparing it against
# our own fixed 30s interval. The charger silently reverts 0x3001/0x3002 to
# maximum if not refreshed within its own configured Command Time Validity
# (0x3005) - a fixed re-assert interval is only actually safe if it's always
# comfortably shorter than whatever that register is set to. Ours wasn't
# guaranteed to be: time_validity's own valid range is 10-60s, and a fixed
# 30s floor has no margin left if it's ever set to, say, 15s. Halving the
# *live* value (like evcc does) keeps the same safety margin regardless of
# how time_validity is configured, rather than assuming a fixed number the
# charger might not actually be using.
def get_heartbeat_interval(time_validity: int | float | None) -> float:
    """Half the charger's own configured Command Time Validity, floored at
    SETPOINT_REASSERT_MIN_INTERVAL so a very low or missing time_validity
    can't turn re-assertion into a per-poll write loop."""
    value = time_validity if time_validity else DEFAULT_TIME_VALIDITY
    return max(SETPOINT_REASSERT_MIN_INTERVAL, value / 2)


# Maps each re-asserted setpoint register to the coordinator.data key it
# populates on a successful write. Shared by __init__.py's
# _reassert_setpoints() (poll-driven, only writes once the live register has
# drifted from desired) and the background heartbeat task added alongside it
# (time-driven off get_heartbeat_interval() above, writes unconditionally
# whenever a tick's gates all pass) - one table so the two correction paths
# can't disagree on which key means what.
REASSERTED_DATA_KEYS = {
    REG_MAX_CHARGING_CURRENT: "max_charging_current_raw",
    REG_MAX_CHARGING_POWER:   "max_charging_power_raw",
}

# Per-counter configuration for the energy plausibility guard.
# total_energy is a lifetime counter, so any decrease is implausible.
# current_energy legitimately resets to zero at the start of each session.
ENERGY_GUARDS = {
    "total_energy_raw":   {"allow_decrease": False},
    "current_energy_raw": {"allow_decrease": True},
}

MAX_ENERGY_REJECTION_RECORDS = 20

# ── Per-block freshness (added 2.2.0) ─────────────────────────────────────────
# Identifies which of the three independent register reads in _fetch() a
# given entity's data comes from. Previously every entity trusted HA's
# CoordinatorEntity.available default (last_update_success, coordinator-wide)
# - so one failed block (most commonly 0x300A/0x300B on single-phase
# hardware, or a transient glitch on the 0x3000 config read) made HA report
# `available=False` for every single entity, including ones whose own block
# read fine. Entities now check their own block's freshness on top of that
# default (see FoxESSBlockAvailabilityMixin in __init__.py).
BLOCK_STATUS    = "status"     # 0x1000-0x101D: status/energy/fault/RFID batch
BLOCK_CONFIG    = "config"     # 0x3000-0x3006: R/W config registers
BLOCK_PHASE_BOX = "phase_box"  # 0x300A-0x300B: phase-switch-box only

# ── Model -> capability table (added 2.2.0) ───────────────────────────────────
# Rated max power/current used throughout the integration (energy
# plausibility guard, Max Charging Current/Power number bounds) used to be
# hardcoded to the single-phase A7300 family's 7.3kW/32A regardless of what
# `id_model_code` (0x101E) actually reported.
#
# The A7300 entry is this integration's only tested hardware (see README
# "Supported hardware"). The A011/A022 entries are three-phase models this
# repo has never run against - inferred from two independently-corroborating
# sources rather than invented: (1) the README/CHANGELOG's own statement
# that this is a fork "covering the A022/A011/A7300 series register map"
# with "three-phase models (11 kW / 22 kW)", and (2) the A7300 model
# string's own naming convention (7300 = 7300W = 7.3kW) applied consistently
# to A011/A022 (011 -> 11kW, 022 -> 22kW). The per-phase current figures
# (16A for 11kW, 32A for 22kW three-phase @ 230V/phase) are not a guess
# either - they're the two standard IEC 61851 Mode 3 AC charging tiers this
# power split has ever come in. What's genuinely unconfirmed is only
# whether "A011"/"A022" are the *exact* id_model_code prefixes this
# hardware's firmware reports - untested, same caveat the README already
# carries for three-phase support generally.
MODEL_CAPABILITIES: dict[str, dict[str, float]] = {
    "A7300": {"max_power_kw": 7.3,  "max_current_a": 32.0},  # single-phase, tested
    "A011":  {"max_power_kw": 11.0, "max_current_a": 16.0},  # three-phase, untested
    "A022":  {"max_power_kw": 22.0, "max_current_a": 32.0},  # three-phase, untested
}

# This integration's only tested hardware - used whenever the detected
# model string (or its absence, before the first successful read) doesn't
# match any known prefix, so capability values are never left undefined.
DEFAULT_CAPABILITY = MODEL_CAPABILITIES["A7300"]


def get_capabilities(model_code: str | None) -> dict[str, float]:
    """Looks up rated max power (kW) / current (A) for a detected model.

    Matches by prefix, not exact equality: the charger's own id_model_code
    reads back the full part number (e.g. "A7300P1-E-B-WO"), which never
    equals the bare "A7300" table key. Falls back to DEFAULT_CAPABILITY -
    never raises, never returns an incomplete/undefined result - for both
    `model_code=None` (no successful read yet) and any model string that
    doesn't start with a known prefix (unrecognized/future hardware).
    """
    if model_code:
        upper = model_code.upper()
        for prefix, caps in MODEL_CAPABILITIES.items():
            if upper.startswith(prefix):
                return caps
    return DEFAULT_CAPABILITY


# ── Setpoint restoration bounds (added for restart-persisted desired_setpoints) ──
# The two registers the coordinator re-asserts (see REASSERTED_DATA_KEYS
# above) each have a minimum that doesn't vary by model (only the maximum
# does, via MODEL_CAPABILITIES/get_capabilities) - kept here, rather than
# inline in number.py's NUMBERS descriptions, so that a value restored from
# storage (see FoxESSChargerCoordinator.async_validate_desired_setpoints in
# __init__.py) can be bounds-checked against the exact same limits the
# number entities themselves enforce, without __init__.py importing
# number.py (circular import: number.py already imports from __init__.py)
# or the two ever silently drifting apart on what "in range" means.
SETPOINT_MIN_RAW = {
    REG_MAX_CHARGING_CURRENT: 60,  # number.py: native_min_value=6.0A
    REG_MAX_CHARGING_POWER:   0,   # number.py: native_min_value=0.0kW
}

SETPOINT_CAPABILITY_KEY = {
    REG_MAX_CHARGING_CURRENT: "max_current_a",
    REG_MAX_CHARGING_POWER:   "max_power_kw",
}


def get_setpoint_bounds(register: int, model_code: str | None) -> tuple[int, int] | None:
    """(min_raw, max_raw) for a re-assertable setpoint register, in the same
    0.1-unit raw representation the register itself stores and the number
    entities scale to/from - bounded by the detected model's own rated
    capabilities (see get_capabilities above), not a single hardcoded model.
    Returns None for any register this doesn't apply to."""
    capability_key = SETPOINT_CAPABILITY_KEY.get(register)
    if capability_key is None:
        return None
    max_value = get_capabilities(model_code)[capability_key]
    return SETPOINT_MIN_RAW[register], int(round(max_value * 10))


# A block's last successful read must be within this many scan intervals to
# be considered fresh. 3x mirrors MAX_STATUS_READ_FAILURES's "3 consecutive
# misses" tolerance for the status block, applied uniformly to all blocks -
# generous enough to absorb a couple of transient poll failures without
# flapping entities unavailable, while still catching a block that's
# genuinely stopped answering.
BLOCK_STALENESS_FACTOR = 3
