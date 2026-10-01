"""Register map decoding for the FoxESS charger (read side only).

Turns fresh `RegisterIO` reads into an observation dict using the key names
the entities have always consumed, plus explicit validity metadata:

- `status` / `cc_status` are None when the raw value is not a known state;
  `status_valid` / `cc_status_valid` say so and the raw value is kept in
  `status_raw` / `cc_status_raw`. An unknown value is never mapped to an
  inactive or disconnected state.
- `<block>_block_ok` per register block (keys from const.BLOCK_*): True read,
  False attempted and failed, None not attempted. Keys of a failed or
  skipped block are absent, never carried over from an earlier read.
- `observed_at`: monotonic time taken after the status block was read.
- Energy counters are raw; plausibility filtering belongs to the energy layer.

This module never writes.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    ALARM_BITS,
    BLOCK_CONFIG,
    BLOCK_PHASE_BOX,
    BLOCK_STATUS,
    CC_STATUS_MAP,
    FAULT_BITS,
    REG_ACTIVE_POWER,
    REG_ALARM_CODE,
    REG_ALLOWED_CHARGE_ENERGY,
    REG_ALLOWED_CHARGE_TIME,
    REG_AMBIENT_TEMP,
    REG_AUTO_PHASE_SWITCH,
    REG_CC_STATUS,
    REG_CP_STATUS,
    REG_CURRENT_ENERGY,
    REG_DEFAULT_CURRENT,
    REG_DEVICE_ADDRESS,
    REG_FAULT_CODE,
    REG_L1_CURRENT,
    REG_L1_VOLTAGE,
    REG_L2_CURRENT,
    REG_L2_VOLTAGE,
    REG_L3_CURRENT,
    REG_L3_VOLTAGE,
    REG_ID_MODEL_CODE,
    REG_ID_SERIAL_NUMBER,
    REG_LOCK_STATUS,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_MAX_CURRENT,
    REG_MAX_POWER,
    REG_MIN_CURRENT,
    REG_MIN_POWER,
    REG_MIN_SWITCH_INTERVAL,
    REG_PHASE_SEQUENCE,
    REG_PORT_TEMP,
    REG_RFID_CARD,
    REG_SOFTWARE_VER,
    REG_STATUS,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
    REG_STOP_REASON,
    REG_TIME_VALIDITY,
    REG_TOTAL_ENERGY,
    REG_WORK_MODE,
    STATUS_MAP,
    decode_bitmask,
)
from .modbus_client import decode_ascii
from .transport import CommandRefused, RegisterIO, TransportError

# Status 9 is a transitional phase-switching state (reported by three-phase
# units while the phase box changes over). It keeps the session alive and is
# not evidence of Stop or unplug. 7 is listed as reserved in the register map
# and is not a state the firmware documents emitting, so it is treated as
# unknown.
STATUS_PHASE_SWITCHING = 9
STATUS_NAMES: dict[int, str] = {
    **{k: v for k, v in STATUS_MAP.items() if k != 7},
    STATUS_PHASE_SWITCHING: "phase_switching",
}
# Statuses in which a session is in progress (start, charging, paused by
# vehicle, phase switching). None of these can confirm a stop.
ACTIVE_STATUSES = frozenset({2, 3, 4, STATUS_PHASE_SWITCHING})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_valid_status(value: Any) -> bool:
    return _is_int(value) and value in STATUS_NAMES


def is_valid_cc_status(value: Any) -> bool:
    return _is_int(value) and value in CC_STATUS_MAP


def is_active_status(value: Any) -> bool:
    """True only for a known status in which a session is in progress."""
    return is_valid_status(value) and value in ACTIVE_STATUSES


# ── Register descriptors ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Field:
    key: str
    address: int
    words: int = 1  # 2 = big-endian UINT32 (high word first)


@dataclass(frozen=True)
class Block:
    name: str
    address: int
    count: int
    fields: tuple[Field, ...]

    def decode(self, regs: tuple[int, ...]) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.fields:
            i = f.address - self.address
            if f.words == 2:
                out[f.key] = (regs[i] << 16) | regs[i + 1]
            else:
                out[f.key] = regs[i]
        return out


STATUS_BLOCK = Block(BLOCK_STATUS, REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT, (
    Field("device_address", REG_DEVICE_ADDRESS),
    Field("software_version", REG_SOFTWARE_VER),
    Field("stop_reason", REG_STOP_REASON),
    Field("status", REG_STATUS),
    Field("cp_status", REG_CP_STATUS),
    Field("cc_status", REG_CC_STATUS),
    Field("port_temp_raw", REG_PORT_TEMP),
    Field("ambient_temp_raw", REG_AMBIENT_TEMP),
    Field("l1_voltage_raw", REG_L1_VOLTAGE),
    Field("l2_voltage_raw", REG_L2_VOLTAGE),
    Field("l3_voltage_raw", REG_L3_VOLTAGE),
    Field("l1_current_raw", REG_L1_CURRENT),
    Field("l2_current_raw", REG_L2_CURRENT),
    Field("l3_current_raw", REG_L3_CURRENT),
    Field("power_raw", REG_ACTIVE_POWER),
    Field("lock_status", REG_LOCK_STATUS),
    Field("phase_sequence", REG_PHASE_SEQUENCE),
    Field("max_power_raw", REG_MAX_POWER),
    Field("min_power_raw", REG_MIN_POWER),
    Field("max_current_raw", REG_MAX_CURRENT),
    Field("min_current_raw", REG_MIN_CURRENT),
    Field("alarm_code", REG_ALARM_CODE),
    Field("total_energy_raw", REG_TOTAL_ENERGY, 2),
    Field("current_energy_raw", REG_CURRENT_ENERGY, 2),
    Field("fault_code", REG_FAULT_CODE, 2),
    Field("rfid_card", REG_RFID_CARD, 2),
))

CONFIG_BLOCK = Block(BLOCK_CONFIG, REG_WORK_MODE, 7, (
    Field("work_mode", REG_WORK_MODE),
    Field("max_charging_current_raw", REG_MAX_CHARGING_CURRENT),
    Field("max_charging_power_raw", REG_MAX_CHARGING_POWER),
    Field("allowed_charge_time", REG_ALLOWED_CHARGE_TIME),
    Field("allowed_charge_energy", REG_ALLOWED_CHARGE_ENERGY),
    Field("time_validity", REG_TIME_VALIDITY),
    Field("default_current_raw", REG_DEFAULT_CURRENT),
))

PHASE_BOX_BLOCK = Block(BLOCK_PHASE_BOX, REG_AUTO_PHASE_SWITCH, 2, (
    Field("auto_phase_switch", REG_AUTO_PHASE_SWITCH),
    Field("min_switch_interval", REG_MIN_SWITCH_INTERVAL),
))

IDENTITY_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("id_model_code", REG_ID_MODEL_CODE, 4),
    ("id_serial_number", REG_ID_SERIAL_NUMBER, 16),
)


def _block_ok_key(block: Block) -> str:
    return f"{block.name}_block_ok"


# ── Reading ───────────────────────────────────────────────────────────────────

async def async_read_identity(
    io: RegisterIO, fields: tuple[tuple[str, int, int], ...] = IDENTITY_FIELDS,
) -> dict[str, str]:
    """Reads static identity strings; keys whose read failed or came back
    empty are omitted."""
    out: dict[str, str] = {}
    for key, address, count in fields:
        try:
            text = decode_ascii(await io.read(address, count))
        except TransportError:
            continue
        if text:
            out[key] = text
    return out


async def async_read_snapshot(
    io: RegisterIO,
    *,
    identity: Mapping[str, str] | None = None,
    read_phase_box: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """One fresh observation.

    The status block is mandatory: its TransportError propagates. Config and
    phase-box failures are recorded in their `_block_ok` flag. `identity` is
    merged in as given (see SnapshotReader for the cached variant).
    """
    regs = await io.read(STATUS_BLOCK.address, STATUS_BLOCK.count)
    observed_at = clock()
    if len(regs) != STATUS_BLOCK.count:
        raise TransportError(f"status block returned {len(regs)} registers")
    snap: dict[str, Any] = STATUS_BLOCK.decode(regs)
    snap[_block_ok_key(STATUS_BLOCK)] = True
    snap["observed_at"] = observed_at

    raw_status, raw_cc = snap["status"], snap["cc_status"]
    snap["status_raw"], snap["cc_status_raw"] = raw_status, raw_cc
    snap["status_valid"] = is_valid_status(raw_status)
    snap["cc_status_valid"] = is_valid_cc_status(raw_cc)
    if not snap["status_valid"]:
        snap["status"] = None
    if not snap["cc_status_valid"]:
        snap["cc_status"] = None
    snap["active_faults"] = decode_bitmask(snap["fault_code"], FAULT_BITS, "fault_code")
    snap["active_alarms"] = decode_bitmask(snap["alarm_code"], ALARM_BITS, "alarm_code")

    await _read_optional(io, CONFIG_BLOCK, snap)
    if read_phase_box:
        await _read_optional(io, PHASE_BOX_BLOCK, snap)
    else:
        snap[_block_ok_key(PHASE_BOX_BLOCK)] = None

    if identity:
        snap.update(identity)
    return snap


async def _read_optional(io: RegisterIO, block: Block, snap: dict[str, Any]) -> None:
    try:
        regs = await io.read(block.address, block.count)
    except TransportError as ex:
        snap[_block_ok_key(block)] = False
        snap[f"{block.name}_block_refused"] = isinstance(ex, CommandRefused)
        return
    if len(regs) != block.count:
        snap[_block_ok_key(block)] = False
        return
    snap.update(block.decode(regs))
    snap[_block_ok_key(block)] = True


class SnapshotReader:
    """Stateful snapshot helper owned by whoever owns the transport.

    - Static identity is read until each string has been obtained once, then
      served from cache; it never costs a per-poll wire read afterwards.
    - The phase box is polled until the device definitively refuses it
      (Modbus exception, as on single-phase units); after that it is no
      longer requested. Timeouts do not count as refusal.
    """

    def __init__(self, io: RegisterIO, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._io = io
        self._clock = clock
        self._identity: dict[str, str] = {}
        self.phase_box_supported: bool | None = None

    @property
    def identity(self) -> Mapping[str, str]:
        return dict(self._identity)

    async def async_read(self) -> dict[str, Any]:
        snap = await async_read_snapshot(
            self._io,
            read_phase_box=self.phase_box_supported is not False,
            clock=self._clock,
        )
        ok = snap.get(_block_ok_key(PHASE_BOX_BLOCK))
        if ok:
            self.phase_box_supported = True
        elif ok is False and snap.get(f"{PHASE_BOX_BLOCK.name}_block_refused"):
            self.phase_box_supported = False

        if len(self._identity) < len(IDENTITY_FIELDS):
            missing = tuple(f for f in IDENTITY_FIELDS if f[0] not in self._identity)
            self._identity.update(await async_read_identity(self._io, missing))
        snap.update(self._identity)
        return snap
