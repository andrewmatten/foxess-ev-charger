"""Regression test for the 2.1.3 register-read batching change.

Pins two things at once, both load-bearing:

1. The status/energy/fault/RFID block (0x1000-0x101D, 30 registers) is read
   with exactly one FC03 request instead of the previous five separate
   round trips (one 22-register status read + four individual UINT32 reads).
2. The phase-switch-box block (0x300A/0x300B) stays a distinct, separate
   request rather than being folded into a larger block - this is precisely
   what caused the Round 2 "Illegal Data Address" bug (see
   const.py/CHANGELOG.md 2.1.0): those two registers don't exist in firmware
   on single-phase hardware, and merging them into a bigger read fails the
   *entire* request, silently blanking out everything else in it.

Uses a MagicMock in place of FoxESSModbusClient - the transport itself
already has its own dedicated tests (test_modbus_client.py); this is purely
about what the coordinator asks the client for.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger import FoxESSChargerCoordinator
from custom_components.foxess_charger.const import (
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
)


def make_status_block_registers() -> list[int]:
    """30 registers covering 0x1000-0x101D with plausible values."""
    regs = [0] * REG_STATUS_BLOCK_COUNT
    regs[0]  = 1       # device_address
    regs[1]  = 0x0102  # software_version
    regs[2]  = 0       # stop_reason
    regs[3]  = 1       # status (connected)
    regs[4]  = 3       # cp_status
    regs[5]  = 1       # cc_status
    regs[6]  = 550     # port_temp_raw
    regs[7]  = 750     # ambient_temp_raw
    regs[8]  = 2300    # l1_voltage_raw
    regs[14] = 0       # power_raw
    regs[17] = 73      # max_power_raw (7.3kW)
    regs[21] = 0       # alarm_code
    # UINT32 pairs: total_energy(22,23), current_energy(24,25),
    # fault_code(26,27), rfid_card(28,29)
    regs[22], regs[23] = 0, 3785   # total_energy_raw = 3785
    regs[24], regs[25] = 0, 0      # current_energy_raw = 0
    regs[26], regs[27] = 0, 0      # fault_code = 0
    regs[28], regs[29] = 0, 0      # rfid_card = 0
    return regs


def make_mock_client() -> MagicMock:
    client = MagicMock()

    def _read_registers(address, count, quiet=False):
        if address == REG_STATUS_BLOCK_START and count == REG_STATUS_BLOCK_COUNT:
            return make_status_block_registers()
        if address == 0x3000 and count == 7:
            return [0, 320, 73, 0xFFFF, 0xFFFF, 30, 320]
        if address == 0x300A and count == 2:
            return None  # expected to fail on single-phase hardware
        raise AssertionError(f"Unexpected read_registers(0x{address:04X}, {count})")

    client.read_registers.side_effect = _read_registers
    client.read_ascii.return_value = None
    return client


async def test_status_block_is_a_single_batched_fc03_request(hass):
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)

    data = coordinator._fetch()

    status_block_calls = [
        call for call in client.read_registers.call_args_list
        if call.args[:2] == (REG_STATUS_BLOCK_START, REG_STATUS_BLOCK_COUNT)
    ]
    assert len(status_block_calls) == 1, (
        "expected exactly one FC03 request for address=0x1000, count=30"
    )

    # Sanity: the batch actually got decoded into the expected fields.
    assert data["status"] == 1
    assert data["total_energy_raw"] == 3785


async def test_phase_switch_box_block_is_a_distinct_separate_request(hass):
    """Guards against ever re-merging 0x300A/0x300B into a larger read -
    that merge is exactly what caused the Round 2 Illegal Data Address bug
    on single-phase hardware."""
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)

    coordinator._fetch()

    phase_box_calls = [
        call for call in client.read_registers.call_args_list
        if call.args[:2] == (0x300A, 2)
    ]
    assert len(phase_box_calls) == 1
    # Must be its own request, not merged into the 0x3000-0x3006 config read
    # or the 0x1000-0x101D status block.
    config_calls = [
        call for call in client.read_registers.call_args_list
        if call.args[:2] == (0x3000, 7)
    ]
    assert len(config_calls) == 1
    assert phase_box_calls[0] != config_calls[0]


async def test_exactly_three_read_registers_calls_per_poll(hass):
    """Pins the total request count: one batched status/energy block, one
    core config block, one phase-switch-box probe - not more, not fewer."""
    client = make_mock_client()
    coordinator = FoxESSChargerCoordinator(hass, client, scan_interval=10)

    coordinator._fetch()

    assert client.read_registers.call_count == 3
