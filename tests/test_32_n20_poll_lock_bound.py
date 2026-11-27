"""3.2 N20: bound poll lock hold on bad/slow links by aborting optional reads."""
from __future__ import annotations

from custom_components.foxess_charger.const import (
    REG_ACTIVE_POWER,
    REG_AUTO_PHASE_SWITCH,
    REG_CC_STATUS,
    REG_DEFAULT_CURRENT,
    REG_ID_MODEL_CODE,
    REG_ID_SERIAL_NUMBER,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_MAX_CURRENT,
    REG_MAX_POWER,
    REG_MIN_SWITCH_INTERVAL,
    REG_STATUS,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
    REG_TIME_VALIDITY,
    REG_WORK_MODE,
)
from custom_components.foxess_charger.controller import ChargingController
from custom_components.foxess_charger.transport import CommandRefused, TransportError

from test_controller import FakeClock


class TimedIO:
    """Minimal RegisterIO over FakeClock with per-block delays and faults."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.regs: dict[int, int] = {
            REG_STATUS: 3,
            REG_CC_STATUS: 1,
            REG_ACTIVE_POWER: 70,
            REG_MAX_POWER: 73,
            REG_MAX_CURRENT: 320,
            REG_WORK_MODE: 1,
            REG_MAX_CHARGING_CURRENT: 160,
            REG_MAX_CHARGING_POWER: 70,
            REG_TIME_VALIDITY: 60,
            REG_DEFAULT_CURRENT: 60,
            REG_AUTO_PHASE_SWITCH: 1,
            REG_MIN_SWITCH_INTERVAL: 10,
        }
        for addr, text, count in (
            (REG_ID_MODEL_CODE, "A7300P1", 4),
            (REG_ID_SERIAL_NUMBER, "SN0001", 16),
        ):
            raw = text.encode("ascii").ljust(count * 2, b"\x00")
            for i in range(count):
                self.regs[addr + i] = int.from_bytes(raw[i * 2 : i * 2 + 2], "big")
        self.delay: dict[int, float] = {}
        self.error: dict[int, Exception] = {}
        self.reads: list[tuple[int, int]] = []

    async def read(self, address: int, count: int) -> tuple[int, ...]:
        self.reads.append((address, count))
        if address in self.delay:
            self.clock.now += self.delay[address]
        if address in self.error:
            raise self.error[address]
        return tuple(self.regs.get(address + i, 0) for i in range(count))

    async def write(self, address: int, value: int) -> None:
        self.regs[address] = value

    async def close(self) -> None:
        pass


async def _make_poller():
    clock = FakeClock()
    io = TimedIO(clock)
    ctl = ChargingController(io, clock=clock, sleep=clock.sleep)
    await ctl.async_initialize(None, configure_safety=False)
    io.reads.clear()
    return ctl, io, clock


async def test_poll_aborts_remaining_optional_reads_after_transport_error():
    """After a transport timeout on an optional block, remaining optional and
    identity reads in the same poll are skipped instead of stacking ~22 s
    under the controller lock, and recover cleanly on the next healthy poll."""
    ctl, io, clock = await _make_poller()
    for addr in (
        REG_WORK_MODE,
        REG_AUTO_PHASE_SWITCH,
        REG_ID_MODEL_CODE,
        REG_ID_SERIAL_NUMBER,
    ):
        io.delay[addr] = 5.5
        io.error[addr] = TransportError("timeout")

    start = clock.now
    snap = await ctl.async_poll()
    assert clock.now - start == 5.5
    assert io.reads == [
        (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT),
        (REG_WORK_MODE, 7),
    ]
    assert snap["status_block_ok"] is True
    assert snap["config_block_ok"] is False
    assert snap["phase_box_block_ok"] is False

    # Link recovers: phase box was not misclassified as unsupported and
    # identity strings are read normally.
    io.delay.clear()
    io.error.clear()
    io.reads.clear()
    snap2 = await ctl.async_poll()
    assert snap2["config_block_ok"] is True
    assert snap2["phase_box_block_ok"] is True
    assert snap2["id_model_code"] == "A7300P1"
    assert snap2["id_serial_number"] == "SN0001"


async def test_poll_continues_after_modbus_command_refused():
    """A Modbus exception (e.g. phase-box unsupported on single-phase units)
    does not abort identity reads and is still cached by SnapshotReader."""
    ctl, io, _clock = await _make_poller()
    io.error[REG_AUTO_PHASE_SWITCH] = CommandRefused("illegal address", code=2)

    snap = await ctl.async_poll()
    assert snap["config_block_ok"] is True
    assert snap["phase_box_block_ok"] is False
    assert snap["id_model_code"] == "A7300P1"
    assert snap["id_serial_number"] == "SN0001"

    io.reads.clear()
    snap2 = await ctl.async_poll()
    assert snap2["phase_box_block_ok"] is None
    assert all(addr != REG_AUTO_PHASE_SWITCH for addr, _ in io.reads)


async def test_poll_stops_optional_reads_when_time_budget_exceeded():
    """Even if slow reads keep succeeding just inside the 5 s deadline,
    remaining optional/identity reads are deferred once poll_budget elapses."""
    ctl, io, clock = await _make_poller()
    for addr in (
        REG_STATUS_BLOCK_START,
        REG_WORK_MODE,
        REG_AUTO_PHASE_SWITCH,
        REG_ID_MODEL_CODE,
        REG_ID_SERIAL_NUMBER,
    ):
        io.delay[addr] = 4.5

    start = clock.now
    snap = await ctl.async_poll()
    assert clock.now - start == 9.0
    assert [addr for addr, _ in io.reads] == [REG_STATUS_BLOCK_START, REG_WORK_MODE]
    assert snap["status_block_ok"] is True
    assert snap["config_block_ok"] is True
    assert snap["phase_box_block_ok"] is False


async def test_poll_keeps_controller_lock_across_snapshot_read():
    """Bounding optional reads does not release the controller lock during
    snapshot reads, preserving command/fault serialization."""
    ctl, io, _clock = await _make_poller()
    real_read = io.read
    locked_during_reads: list[bool] = []

    async def spy_read(address: int, count: int) -> tuple[int, ...]:
        locked_during_reads.append(ctl._lock.locked())
        return await real_read(address, count)

    io.read = spy_read
    await ctl.async_poll()
    assert locked_during_reads and all(locked_during_reads)
