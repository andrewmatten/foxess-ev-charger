"""A behaviourally realistic fake FoxESS charger for tests.

Unlike a MagicMock client (which obeys any value forever), this models the
firmware behaviour that actually bit us on 2026-09-24:

- 0x3001/0x3002 (max charging current/power) silently revert to the
  charger's maximum ~FIRMWARE_REVERT_S after the last write to them, no
  matter what Command Time Validity (0x3005) claims - Andrew's charger
  reports 180s there but still reverts at ~60s.
- Writes can be made to fail on demand (`fail_writes`), like a dropped
  Modbus frame.
- Reads can be made to fail per block (`fail_status_reads`,
  `fail_config_reads`) so blocks go stale.
- alarm_code / fault_code / status can be set directly.

Time is a manually-advanced clock (`advance()`), so a 60s revert can be
tested without waiting 60s.
"""
from __future__ import annotations

from custom_components.foxess_charger.const import (
    REG_CHARGING_CONTROL,
    REG_MAX_CHARGING_CURRENT,
    REG_MAX_CHARGING_POWER,
    REG_STATUS_BLOCK_COUNT,
    REG_STATUS_BLOCK_START,
)

FIRMWARE_REVERT_S = 60
MAX_CURRENT_RAW = 320  # 32.0 A
MAX_POWER_RAW = 73     # 7.3 kW


class FakeCharger:
    def __init__(self, time_validity: int = 180) -> None:
        self.now = 0.0
        self.status = 3  # charging
        self.cc_status = 1
        self.alarm_code = 0
        self.fault_code = 0
        self.power_raw = MAX_POWER_RAW
        self.holding = {
            0x3000: 0,
            REG_MAX_CHARGING_CURRENT: MAX_CURRENT_RAW,
            REG_MAX_CHARGING_POWER: MAX_POWER_RAW,
            0x3003: 0xFFFF,
            0x3004: 0xFFFF,
            0x3005: time_validity,
            0x3006: 80,
        }
        self._last_limit_write: float | None = None
        self.fail_writes = 0          # number of upcoming writes to fail
        self.fail_status_reads = False
        self.fail_config_reads = False
        self.writes: list[tuple[float, int, int]] = []       # successful
        self.write_attempts: list[tuple[float, int, int]] = []
        # transport counters the coordinator's diagnostics read
        self.txid_mismatches = self.short_reads = self.connection_errors = 0
        self.malformed_headers = self.unit_id_mismatches = 0
        self.write_echo_mismatches = 0

    # ── clock ────────────────────────────────────────────────────────────
    def advance(self, seconds: float) -> None:
        self.now += seconds
        self._apply_revert()

    def _apply_revert(self) -> None:
        if (
            self._last_limit_write is not None
            and self.now - self._last_limit_write >= FIRMWARE_REVERT_S
        ):
            self.holding[REG_MAX_CHARGING_CURRENT] = MAX_CURRENT_RAW
            self.holding[REG_MAX_CHARGING_POWER] = MAX_POWER_RAW
            self._last_limit_write = None

    def effective_power_raw(self) -> int:
        """What the car would actually be allowed to draw right now."""
        self._apply_revert()
        return self.holding[REG_MAX_CHARGING_POWER]

    # ── client API used by the coordinator ───────────────────────────────
    def read_registers(self, address: int, count: int, quiet: bool = False):
        self._apply_revert()
        if address == REG_STATUS_BLOCK_START:
            if self.fail_status_reads:
                return None
            regs = [0] * REG_STATUS_BLOCK_COUNT
            regs[3] = self.status
            regs[4] = 3
            regs[5] = self.cc_status
            regs[6] = regs[7] = 750
            regs[8] = 2400
            regs[14] = self.power_raw
            regs[17] = MAX_POWER_RAW
            regs[19] = MAX_CURRENT_RAW
            regs[20] = 60
            regs[21] = self.alarm_code
            regs[26] = (self.fault_code >> 16) & 0xFFFF
            regs[27] = self.fault_code & 0xFFFF
            return regs[:count]
        if address == 0x3000:
            if self.fail_config_reads:
                return None
            return [self.holding[0x3000 + i] for i in range(count)]
        return None  # 0x300A phase box: absent on single-phase

    def read_ascii(self, address: int, reg_count: int):
        return "A7300P1-E-B-WO" if address == 0x101E else "SERIAL"

    def write_holding_register(self, address: int, value: int) -> bool:
        self.write_attempts.append((self.now, address, value))
        if self.fail_writes > 0:
            self.fail_writes -= 1
            return False
        if address in (REG_MAX_CHARGING_CURRENT, REG_MAX_CHARGING_POWER):
            self.holding[address] = value
            self._last_limit_write = self.now
        elif address == REG_CHARGING_CONTROL:
            if value == 2:
                self.status = 5
            elif value == 1:
                self.status = 3
        else:
            self.holding[address] = value
        self.writes.append((self.now, address, value))
        return True

    def disconnect(self) -> None:
        pass
