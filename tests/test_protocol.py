"""protocol.py: snapshot decoding, validity flags, block failures, identity cache."""
from __future__ import annotations

import pytest

from custom_components.foxess_charger import protocol
from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS
from custom_components.foxess_charger.const import (
    BLOCK_CONFIG,
    BLOCK_PHASE_BOX,
    BLOCK_STATUS,
    REG_ID_MODEL_CODE,
    REG_ID_SERIAL_NUMBER,
)
from custom_components.foxess_charger.sensor import SENSORS
from custom_components.foxess_charger.transport import CommandRefused, TransportError


def ascii_regs(text: str, count: int) -> list[int]:
    raw = text.encode().ljust(count * 2, b"\x00")
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, count * 2, 2)]


class RegisterMap:
    """RegisterIO fake backed by a plain register dict. Every read is
    served from the dict at call time (no caching)."""

    def __init__(self, phase_box: bool = True) -> None:
        self.regs: dict[int, int] = {}
        status = [
            1, 0x0102, 1, 3, 3, 1, 750, 760, 2400, 0, 0, 160, 0, 0, 37,
            1, 1, 73, 14, 320, 60, 0b10,
            0x0001, 0x86A0,   # total energy 100000
            0, 125,           # session energy
            0x0002, 0x0001,   # fault bits 1 and 17
            0x1234, 0x5678,   # RFID
        ]
        for i, v in enumerate(status):
            self.regs[0x1000 + i] = v
        for i, v in enumerate([0, 160, 37, 0xFFFF, 0xFFFF, 60, 80]):
            self.regs[0x3000 + i] = v
        if phase_box:
            self.regs[0x300A] = 1
            self.regs[0x300B] = 5
        for i, v in enumerate(ascii_regs("A7300P1", 4)):
            self.regs[REG_ID_MODEL_CODE + i] = v
        for i, v in enumerate(ascii_regs("SN0001", 16)):
            self.regs[REG_ID_SERIAL_NUMBER + i] = v
        self.fail: dict[int, Exception] = {}
        self.reads: list[tuple[int, int]] = []

    async def read(self, address: int, count: int) -> tuple[int, ...]:
        self.reads.append((address, count))
        if address in self.fail:
            raise self.fail[address]
        try:
            return tuple(self.regs[address + i] for i in range(count))
        except KeyError:
            raise CommandRefused("illegal address", 0x02) from None

    async def write(self, address: int, value: int) -> None:
        raise AssertionError("protocol must never write")

    async def close(self) -> None:
        pass


async def test_snapshot_decodes_blocks():
    io = RegisterMap()
    snap = await protocol.async_read_snapshot(io, clock=lambda: 123.0)
    assert snap["status"] == 3 and snap["status_valid"] is True
    assert snap["cc_status"] == 1 and snap["cc_status_valid"] is True
    assert snap["power_raw"] == 37
    assert snap["total_energy_raw"] == 100000
    assert snap["current_energy_raw"] == 125
    assert snap["fault_code"] == 0x00020001
    assert snap["rfid_card"] == 0x12345678
    assert snap["active_faults"] == ["emergency_stop", "access_control"]
    assert snap["active_alarms"] == ["phase_cutting_box"]
    assert snap["max_charging_power_raw"] == 37
    assert snap["time_validity"] == 60
    assert snap["auto_phase_switch"] == 1 and snap["min_switch_interval"] == 5
    assert snap["observed_at"] == 123.0
    assert snap[f"{BLOCK_STATUS}_block_ok"] is True
    assert snap[f"{BLOCK_CONFIG}_block_ok"] is True
    assert snap[f"{BLOCK_PHASE_BOX}_block_ok"] is True
    assert (0x1000, 30) in io.reads and (0x3000, 7) in io.reads


async def test_every_snapshot_is_a_fresh_read_and_independent():
    io = RegisterMap()
    first = await protocol.async_read_snapshot(io)
    io.regs[0x1003] = 5
    second = await protocol.async_read_snapshot(io)
    assert first["status"] == 3 and second["status"] == 5
    second["status"] = 99
    third = await protocol.async_read_snapshot(io)
    assert third["status"] == 5


@pytest.mark.parametrize("raw", [7, 10, 0xFFFF])
async def test_unknown_status_is_none_not_inactive(raw):
    io = RegisterMap()
    io.regs[0x1003] = raw
    snap = await protocol.async_read_snapshot(io)
    assert snap["status"] is None
    assert snap["status_valid"] is False
    assert snap["status_raw"] == raw
    assert not protocol.is_valid_status(raw)


@pytest.mark.parametrize("raw", [2, 0xFFFF])
async def test_unknown_cc_status_is_none_not_disconnected(raw):
    io = RegisterMap()
    io.regs[0x1005] = raw
    snap = await protocol.async_read_snapshot(io)
    assert snap["cc_status"] is None
    assert snap["cc_status_valid"] is False
    assert snap["cc_status_raw"] == raw


async def test_status_9_is_valid_phase_switching():
    io = RegisterMap()
    io.regs[0x1003] = 9
    snap = await protocol.async_read_snapshot(io)
    assert snap["status"] == 9 and snap["status_valid"] is True
    assert protocol.STATUS_NAMES[9] == "phase_switching"
    assert protocol.is_active_status(9), "phase switching must not read as stopped"


def test_status_helpers():
    for s in (0, 1, 2, 3, 4, 5, 6, 8, 9):
        assert protocol.is_valid_status(s)
    for s in (None, -1, 7, 10, 0xFFFF, True):
        assert not protocol.is_valid_status(s)
    assert protocol.is_valid_cc_status(0) and protocol.is_valid_cc_status(1)
    assert not protocol.is_valid_cc_status(None) and not protocol.is_valid_cc_status(2)
    assert {s for s in range(12) if protocol.is_active_status(s)} == {2, 3, 4, 9}
    assert not protocol.is_active_status(None) and not protocol.is_active_status(0xFFFF)


async def test_status_block_failure_raises():
    io = RegisterMap()
    io.fail[0x1000] = TransportError("timeout")
    with pytest.raises(TransportError):
        await protocol.async_read_snapshot(io)


async def test_config_block_failure_is_marked_not_hidden():
    io = RegisterMap()
    io.fail[0x3000] = TransportError("timeout")
    snap = await protocol.async_read_snapshot(io)
    assert snap[f"{BLOCK_CONFIG}_block_ok"] is False
    for key in ("work_mode", "time_validity", "max_charging_power_raw"):
        assert key not in snap
    assert snap["status_valid"] is True


async def test_phase_box_refusal_marks_block_and_reader_stops_asking():
    io = RegisterMap(phase_box=False)
    reader = protocol.SnapshotReader(io)
    snap = await reader.async_read()
    assert snap[f"{BLOCK_PHASE_BOX}_block_ok"] is False
    assert "auto_phase_switch" not in snap
    io.reads.clear()
    snap = await reader.async_read()
    assert snap[f"{BLOCK_PHASE_BOX}_block_ok"] is None  # not attempted
    assert all(addr != 0x300A for addr, _ in io.reads)


async def test_phase_box_transient_failure_keeps_trying():
    io = RegisterMap()
    io.fail[0x300A] = TransportError("timeout")
    reader = protocol.SnapshotReader(io)
    assert (await reader.async_read())[f"{BLOCK_PHASE_BOX}_block_ok"] is False
    del io.fail[0x300A]
    snap = await reader.async_read()
    assert snap[f"{BLOCK_PHASE_BOX}_block_ok"] is True
    assert snap["auto_phase_switch"] == 1


async def test_identity_read_once_and_cached():
    io = RegisterMap()
    reader = protocol.SnapshotReader(io)
    first = await reader.async_read()
    second = await reader.async_read()
    assert first["id_model_code"] == second["id_model_code"] == "A7300P1"
    assert first["id_serial_number"] == "SN0001"
    ident_reads = [r for r in io.reads if r[0] in (REG_ID_MODEL_CODE, REG_ID_SERIAL_NUMBER)]
    assert len(ident_reads) == 2


async def test_identity_retried_until_read():
    io = RegisterMap()
    io.fail[REG_ID_SERIAL_NUMBER] = TransportError("timeout")
    reader = protocol.SnapshotReader(io)
    snap = await reader.async_read()
    assert snap["id_model_code"] == "A7300P1"
    assert "id_serial_number" not in snap
    del io.fail[REG_ID_SERIAL_NUMBER]
    snap = await reader.async_read()
    assert snap["id_serial_number"] == "SN0001"
    snap = await reader.async_read()
    serial_reads = [r for r in io.reads if r[0] == REG_ID_SERIAL_NUMBER]
    assert len(serial_reads) == 2


async def test_entity_value_functions_accept_snapshot():
    """Every existing sensor/binary_sensor value_fn runs on a snapshot and
    hardware-backed ones produce a value."""
    snap = await protocol.SnapshotReader(RegisterMap()).async_read()
    coordinator_owned = {"last_session_energy", "last_session_duration"}
    for desc in SENSORS:
        value = desc.value_fn(snap)
        if desc.key not in coordinator_owned and not desc.key.startswith("diag"):
            assert value is not None or desc.key in ("stop_reason",), desc.key
    for desc in BINARY_SENSORS:
        assert isinstance(desc.value_fn(snap), bool), desc.key


async def test_snapshot_keys_match_legacy_coordinator():
    """The hardware-derived keys and values the 2.4.3 coordinator put in its
    data dict are all present, with the same values, in the snapshot.
    Session-tracking and diagnostics keys are derived by the coordinator
    layer, not read from registers, and are excluded. The 2.4.3 output is
    frozen in tests/legacy_243_snapshot.py (captured from commit 4490e4a),
    since that coordinator no longer exists in this tree."""
    from legacy_243_snapshot import LEGACY_243_SNAPSHOT

    snap = await protocol.SnapshotReader(RegisterMap()).async_read()
    missing = set(LEGACY_243_SNAPSHOT) - set(snap)
    assert not missing
    for key, value in LEGACY_243_SNAPSHOT.items():
        assert snap[key] == value, key
