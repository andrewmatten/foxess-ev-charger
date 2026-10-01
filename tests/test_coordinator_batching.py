"""Register-read batching, now owned by the snapshot reader the controller
uses for every poll (protocol.SnapshotReader).

Pins two load-bearing properties:

1. The status/energy/fault/RFID block (0x1000-0x101D, 30 registers) is one
   FC03 request, not several round trips.
2. The phase-switch-box block (0x300A/0x300B) stays a distinct request:
   folding it into a larger read fails the whole request on single-phase
   hardware (Illegal Data Address) and blanks everything else in it.
"""
from __future__ import annotations

from custom_components.foxess_charger import protocol
from custom_components.foxess_charger.const import (
    REG_ID_MODEL_CODE,
    REG_ID_SERIAL_NUMBER,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
)

from test_protocol import RegisterMap

IDENTITY_ADDRESSES = {REG_ID_MODEL_CODE, REG_ID_SERIAL_NUMBER}


def register_reads(io: RegisterMap) -> list[tuple[int, int]]:
    return [r for r in io.reads if r[0] not in IDENTITY_ADDRESSES]


async def test_status_block_is_a_single_batched_fc03_request():
    io = RegisterMap()
    snap = await protocol.SnapshotReader(io).async_read()
    status_calls = [r for r in io.reads if r == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)]
    assert len(status_calls) == 1
    assert not [r for r in io.reads if REG_STATUS_BLOCK_START < r[0] <= 0x101D]
    # The batch was decoded into the expected fields.
    assert snap["status"] == 3
    assert snap["total_energy_raw"] == 100000


async def test_phase_switch_box_block_is_a_distinct_separate_request():
    io = RegisterMap(phase_box=True)
    await protocol.SnapshotReader(io).async_read()
    assert register_reads(io).count((0x300A, 2)) == 1
    assert register_reads(io).count((0x3000, 7)) == 1
    # Neither the config nor the status read spans 0x300A.
    for address, count in register_reads(io):
        if (address, count) != (0x300A, 2):
            assert not address <= 0x300A < address + count


async def test_exactly_three_read_registers_calls_per_poll():
    """One batched status block, one config block, one phase-box read per
    poll while the box answers. CONTRACTS.md B: once the device has
    definitively refused the box (single-phase), it is no longer probed, so
    such a unit costs two requests per poll after the first."""
    io = RegisterMap(phase_box=True)
    reader = protocol.SnapshotReader(io)
    for _ in range(3):
        io.reads.clear()
        await reader.async_read()
        assert register_reads(io) == [
            (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT), (0x3000, 7), (0x300A, 2),
        ]

    io = RegisterMap(phase_box=False)
    reader = protocol.SnapshotReader(io)
    await reader.async_read()
    assert register_reads(io).count((0x300A, 2)) == 1
    io.reads.clear()
    await reader.async_read()
    assert register_reads(io) == [(REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT), (0x3000, 7)]


# replaces test_command_lock::TestPollDrivenWriterIsGone (both tests)
async def test_polling_never_issues_a_command(hass):
    """The poll path only reads: the coordinator never calls a controller
    command, whatever the observation shows."""
    from custom_components.foxess_charger.coordinator import FoxESSChargerCoordinator

    from fake_controller import FakeController

    fake = FakeController(status=3)
    coordinator = FoxESSChargerCoordinator(hass, fake, 10)
    for status in (3, 4, 5, 1, 0):
        fake.hw["status"] = status
        await coordinator.async_refresh()
    assert {c[0] for c in fake.calls} == {"poll"}
