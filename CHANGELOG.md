# Changelog

## 2.1.0

**Reliability**
- Persistent Modbus TCP connection, reused across reads and writes instead of opening a new socket per register call (was 7 connections every poll cycle). Guarded by a lock since poll reads and entity writes run on separate threads.
- Split the `0x3000-0x300B` config register read in two. On single-phase hardware, the phase-switch-box-only registers `0x300A`/`0x300B` aren't implemented in firmware, which was failing the *entire* 12-register request with a Modbus Illegal Data Address exception — silently blanking out Work Mode, Max Charging Current/Power, Allowed Charge Time/Energy, and Time Validity, every single poll, forever. Now `0x3000-0x3006` (core config) and `0x300A-0x300B` (phase-switch-box) are read independently, and the latter logs at debug instead of error when it fails on single-phase units, since that's expected.

**New**
- Serial Number sensor, and the device's **model** is now read from the charger itself (register `0x101E`) instead of being hardcoded — fixes the device page showing the wrong model on non-A7300P1 hardware.
- `Alarm Code` / `Fault Code` sensors now carry `active_alarms` / `active_faults` attributes — a decoded, human-readable list of active conditions from the bitmask, per the protocol spec's appendix tables, instead of just a meaningless raw integer.
- Config flow now validates the connection before creating the entry (`cannot_connect` error on failure) instead of silently creating a broken entry.

**Fixed**
- `Charging` switch now reads "on" during a car-initiated pause (status `4`), not just *start*/*charging*. A pause is the car suspending itself, not a stop command — the session is still active.
- `Status` sensor state `2` relabeled from `"ready"` to `"start"` to match the protocol spec (EVC has sent the start command and is waiting on the car).
- `manifest.json`'s `documentation`/`issue_tracker` URLs pointed at the original upstream repo instead of this fork.
- Options flow now validates an upper bound (300s) on the scan interval, not just a lower one — and the validation error message is now actually translated (it existed only as an untranslated key before).

## 2.0.0

- Adapted for single-phase **A7300P1-E-B-WO** hardware: Max Charging Power capped at 7.3 kW, phase-switch-box-only entities (phase sequence, auto phase switch, L2/L3 voltage & current, min switch interval) disabled by default.
- Modbus reliability: seed each poll from last-known-good data so one failed register block doesn't wipe unrelated state; delay before read-back after a write; corrected default port `1502` → `502`; corrected device model string.
- Added HACS metadata, README, and MIT license for distribution as a HACS custom repository.

## 1.0.0

- Initial fork from [ringaction/foxess_charger](https://github.com/ringaction/foxess_charger), covering the A022/A011/A7300 series register map.
