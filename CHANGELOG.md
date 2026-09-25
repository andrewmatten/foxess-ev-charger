# Changelog

## 2.4.3

Fixes from the 2026-09-25 audit of the charger's power-limit behaviour. New
`tests/fake_charger.py` models the real firmware: limits revert to max ~60s
after the last write regardless of 0x3005, writes can fail, blocks can go
stale, alarms/faults can be raised.

- **Heartbeat timing** - the charger reports Command Time Validity (0x3005)
  as 180s, but this firmware reverts the lower charging limit after about
  60s. The heartbeat now caps the effective validity at 60s and reasserts
  the limit every 30s. Invalid or missing validity readings use the default.
- **Drift detection** - each poll during an active, desired session compares
  0x3001/0x3002 against the desired setpoint. A mismatch logs a warning,
  increments `setpoint_drift_events` (Transport Errors sensor attribute) and
  wakes the heartbeat to re-send immediately.
- **Fast retry** - a failed heartbeat write during an active session retries
  after `SETPOINT_REASSERT_MIN_INTERVAL` (3s) instead of a full interval
  (one missed write at 30s used to land right on the ~60s revert). Counted
  as `heartbeat_write_failures`.
- **Heartbeat fails safe** - a stale config/status block or a non-fatal alarm
  no longer silences the heartbeat (silence = charger reverts to max). An
  active *fault* now sends Stop instead of going silent.
- **Restored setpoints type-checked** - a non-int value in storage is
  discarded instead of crashing setup with a TypeError.
- **Energy guard** - live `max_power_raw` is capped at 1.5x rated, so a
  garbage read (0xFFFF) can't widen the plausibility guard to accept anything.
- **Solar surplus blueprint** - skips limit writes when current is already at
  the entity maximum and skips Stop when charging is already off. This avoids
  repeated no-op Modbus commands on its periodic safety checks.
- **Stop confirmation and retries** - a requested Stop remains pending until a
  fresh status read confirms inactivity. Failed or unapplied Stop commands are
  retried, including after a reload, with failure counts exposed in diagnostics.
- **Safer starts** - staged current and power limits are sent before the Start
  command. An uncertain limit or Start write triggers a compensating Stop.
  Firmware can resume charging on a limit write, so a zero-surge start cannot
  be guaranteed by this protocol.
- **Persistence and readings** - stopped-state limit changes are saved before
  the service returns, and implausible nonzero session-energy resets are
  rejected. Charging Started/Stopped device triggers now follow session state
  across a vehicle pause.
- **Solar surplus blueprint units** - W and kW grid sensors are handled
  explicitly; missing, invalid, or unsupported readings stop charging.

Developed with assistance from Codex and Claude.

## 2.4.2

**Fixed** - Shipped the Solar Surplus Charging automation blueprint that was
documented and tested but accidentally omitted from 2.4.1. Its adjustment
dwell timer measures actual Max Charging Current changes rather than the
automation's once-per-minute trigger time, so regular surplus adjustments
continue to run as intended.

## 2.4.1

**Fixed** — an external automation wrote Max Charging Power while charging
was intentionally stopped. On this firmware, writing that register can
resume charging. The integration's natural-start detection then treated
the unexpected resume as intentional and its heartbeat kept the session
running.

- `number.py`'s two reassertable-register writes (Max Charging Current/
  Power) now always record the desired value, but only submit the
  physical write when charging is genuinely, currently active - decided
  fresh inside the command lock, atomically with the write.
- Closed a second race in the start path: a concurrent stop's flag-clear
  (synchronous, lock-free) could previously be silently overwritten by a
  start write's success handling if that handler ran after the lock was
  already released. The desired-state transition now happens inside the
  same locked section as the write itself.
- New `_stop_inhibit` latch: while HA has just intentionally stopped
  charging, an unexpected resume no longer gets natural-start detection's
  blessing - no `_charging_desired`, no heartbeat reinforcement. Cleared
  on an explicit successful start or the vehicle physically disconnecting.
  Persisted across restarts, including a startup-path bug (found in
  review) where a Core restart mid-inhibit would otherwise have silently
  re-armed the exact session the latch was protecting against.
- A failed stop attempt no longer restores `_charging_desired`/clears the
  inhibit on the theory that "still active means still wanted" - the
  integration can't tell that apart from the incident condition, so it
  now fails toward the protective branch instead.

## 2.4.0

**Fixed** — three real bugs identified by an independent review of the live
v2.3.3 deployment, plus a diagnostics privacy gap.

- **Energy guard: the cumulative-window corruption check could falsely
  reject legitimate 7.3kW readings.** It was missing the same one-quantum
  floor the per-poll check already had, so two genuine 0.1kWh register
  ticks landing within ~55s (a normal ~10-13s poll cadence) could exceed
  the old threshold and get rejected. Also fixed: a legitimate
  session-boundary reset (`current_energy_raw` resetting to ~0) used to
  push a large negative delta into the same rolling window, which could
  mask a real corruption arriving shortly after for up to 30 minutes. The
  window now stores raw observations instead of deltas and is cleared and
  reseeded on a confirmed reset.
- **Energy guard: a corrupt first-ever reading after any HA restart wasn't
  caught.** The only defense for a first-ever reading was a very loose
  absolute ceiling (100,000 kWh) — the known real incident value
  (raw=65800, i.e. 6580.0 kWh) sailed straight through it, and because the
  last-known-good tracking was in-memory only, this weak path was hit again
  on every single restart. The last-known-good baseline is now persisted
  via HA's `Store` helper, so the first live reading after a restart is
  checked against a realistic prior value instead.
- **Command lock: a poll-driven setpoint write could silently resume
  charging in the instant after a stop command.** `_reassert_setpoints()`
  ran synchronously inside the poll cycle's own executor thread, gated only
  on a stale status snapshot, racing against `switch.py`'s event-loop-driven
  stop write — and writing the charge-limit setpoint registers is itself an
  implicit "resume charging" on this firmware. Replaced with one
  `asyncio.Lock` serializing every charger-control write (start, stop,
  heartbeat setpoint pushes), with the generation/desired-state/freshness/
  fault gates re-checked fresh inside the lock immediately before each
  write. The old poll-driven writer is deleted entirely — the heartbeat
  task's existing wake-on-session-start behavior already covers what it was
  for.
- **Diagnostics: the charger's hardware serial number wasn't redacted**
  from diagnostics downloads, alongside the existing host/RFID redaction.

**Known, deliberately-scoped residual (tracked, not fixed here):**
`number.py`/`select.py`'s user-initiated setpoint writes (Max Charging
Current/Power) still go directly to the charger, outside the new command
lock — a user changing a setpoint in the same narrow window as a stop could
in principle still interleave. Far lower risk than the automatic
poll-driven pattern this release closes (which fired unconditionally every
poll during any active session), and self-corrects via the heartbeat's next
tick regardless. Candidate follow-up: route those writes through the same
lock via a thin `async_send_setpoint_user()`-style method.

## 2.3.3

**Fixed** — the background heartbeat loop could die silently.

- `_heartbeat_loop`'s `while True` body was unguarded. `_heartbeat_tick`
  already guards its own register writes, but everything else in an
  iteration - the interval calculation, the wait, the gate checks - was not:
  one unexpected exception from any of it would end the task with no
  traceback anyone would see until GC, and nothing restarts it short of an HA
  restart. That is the worst failure mode this loop has, because the loop *is*
  the safety guarantee - once it stops, the charger reverts 0x3001/0x3002 to
  maximum at the end of the current Command Time Validity window, mid-session,
  with nothing left running to notice. Each iteration is now wrapped: errors
  are logged with a full traceback and the loop backs off to the default
  interval and continues. `CancelledError` is explicitly re-raised so unload
  still stops the task cleanly instead of hanging on its own await.

## 2.3.2

**Fixed** — solar-surplus blueprint only. No change to the integration code.

- **The blueprint could never actually adjust charging current.** Its
  minimum-dwell gate was measured from `this.attributes.last_triggered`, but HA
  stamps that at the start of *every* automation run - including the
  once-a-minute `time_pattern` tick and every no-op pass through the hysteresis
  band. With a trigger firing at least once a minute, `now() - last_triggered`
  could never exceed ~60s, so the default two-minute dwell never elapsed: after
  the very first run, both the raise and lower branches were permanently
  unreachable. The automation could only ever fail-safe-stop, never modulate.
  The dwell is now measured from the Max Charging Current entity's own
  `last_changed` - the time the setpoint last actually moved, which is the
  thing being throttled.
- Writes are now skipped when they would change nothing, which the new dwell
  reference depends on: a no-op re-write leaves `last_changed` frozen, holding
  the gate permanently open and reintroducing the per-minute write loop the
  dwell exists to prevent. Specifically - the raise branch no longer re-writes
  the setpoint once it is already pinned at the entity's maximum, and the
  below-minimum stop no longer re-issues `switch.turn_off` every minute at an
  already-stopped charger.
- The fail-safe stop (grid sensor unavailable) is likewise no longer re-issued
  every minute for the duration of the outage. It is still attempted whenever
  the switch is not positively known to be off - an unavailable switch means we
  do not know the session is stopped, and a safety stop should be attempted on
  missing information rather than skipped.

**Added** - `tests/test_solar_surplus_blueprint.py`: nine functional tests that
instantiate the real blueprint as a live HA automation and drive it, rather
than asserting on the YAML's text. Four of them fail against the 2.3.1
blueprint. This class of bug is invisible to reading the file.

## 2.3.1

**Fixed** — pre-empted by a second independent review before 2.3.0's heartbeat
code had run against a real session; all found and fixed before deployment:
- Heartbeat interval floor (`SETPOINT_REASSERT_MIN_INTERVAL`) lowered 10s→3s -
  the old floor silently defeated the half-Time-Validity guarantee at exactly
  the charger's documented minimum (10s), clamping the required ~5s heartbeat
  back up to 10s.
- `desired_setpoints` now persists across an HA restart (was in-memory only),
  bounds-checked against the detected model's capabilities on restore -
  falls back to the charger's own default current, never to model maximum.
- Plug & Charge / RFID-initiated sessions (not started via the HA switch) now
  get heartbeat protection too - previously only HA-initiated sessions did.
- The heartbeat now also requires the status block (not just config) to be
  fresh, and suppresses itself entirely while a fault or alarm is active.
- `_charging_desired` is now only set true after the start command actually
  succeeds (was optimistic); a failed stop command now restores heartbeat
  protection instead of leaving a still-running charger unprotected.
- Closed a stop/heartbeat race with a generation-token check around each
  register write (before and after) - a stop landing mid-write can no longer
  have its effects (data patch, re-assertion count) applied after the fact.
  One residual risk remains and is documented, not solved: a Python executor
  job already in flight isn't cancellable, so a write already handed to the
  thread pool can still reach the wire in the narrow window around unload.
- `desired_setpoints` is now iterated as a snapshot, not the live dict, since
  a concurrent `number` entity write could otherwise mutate it mid-iteration.
- Command Time Validity's declared UI range raised 60→255 - live evidence
  (this charger reports 180) shows the originally-assumed 10-60s spec range
  doesn't hold for this firmware; the live value is never clamped down.
- A restart during an active session no longer looks like a fresh session
  start: `_prev_status` is now persisted and restored (was left `None`),
  which previously clobbered the just-restored start time/energy baseline.
- The energy guard's very first reading after setup/reload is no longer
  trusted unconditionally - an absolute 100,000 kWh plausibility ceiling now
  applies even to it. `current_energy`'s allow-decrease is no longer
  unconditional - a mid-session decrease with no genuine session boundary is
  now rejected like any other implausible reading. A bounded 30-minute
  rolling-window check now catches sustained one-quantum-per-poll corruption
  that would otherwise always pass the per-poll floor individually.
- Modbus responses now also reject a nonzero MBAP Protocol ID, and an FC03
  response whose declared byte count doesn't exactly match the requested
  register count - both close/reconnect the same way other framing failures
  already did.
- Power-sensor plausibility bounds are now derived from the detected model's
  capabilities instead of a fixed ~10kW ceiling, which would have rejected
  genuine readings from the three-phase A011 (11kW) and A022 (22kW) models.
- `Transport Errors`' state_class corrected `TOTAL_INCREASING`→`MEASUREMENT`
  - its counters reset on every reload (a fresh client object), which
  `TOTAL_INCREASING` would have misread as a meter rollover in statistics.
- The `Session Completed` device trigger now keys off a real completion
  event instead of diffing the Last Session Duration sensor's value - closes
  a false-fire on state restoration at startup, and a false-negative for two
  genuinely consecutive sessions with the same rounded duration. Device
  triggers no longer offer a disabled entity as a target.
- Solar Surplus Charging blueprint: the fail-safe stop now bypasses the
  normal dwell delay, the current/power limit is set before turning charging
  on (was after, risking a brief full-power window), and an ordinary limit
  adjustment no longer redundantly re-sends `turn_on`.

## 2.3.0

**Fixed**
- Setpoint re-assertion's adaptive interval (added just above, this
  session) still only ever ran inside the poll cycle (`_fetch()`,
  ~10-13s per `DEFAULT_SCAN_INTERVAL`) - so a Command Time Validity
  (`0x3005`) configured near its own documented minimum (10s) still
  couldn't reliably get a write in within the required heartbeat (5s).
  Re-assertion now also runs on a real independent background task,
  decoupled from polling entirely - woken immediately when charging
  starts, a setpoint changes, or Time Validity itself changes, and
  otherwise firing on its own `get_heartbeat_interval()` schedule. Gated
  on charging being desired, the charger reporting an active status, and
  the config register block being fresh, so it never fires against stale
  or inactive data. The charging switch's stop path now clears the
  "charging desired" flag as its very first action, before the stop
  command is even sent - closing a race where an in-flight tick could
  otherwise re-push the charge-limit registers and, per this firmware's
  documented behavior, silently resume the very session just stopped.
  Background heartbeat task design adapted from a third-party PR by
  github.com/loadrunner42 (PR #2 on `andrewmatten/foxess-ev-charger`), in
  turn based on the established `evcc-io/evcc` project's own FoxESS
  driver convention - distinct from the interval-math credit already
  given for `get_heartbeat_interval()` itself in the previous entry.

## 2.2.0

**Reliability**
- One register block failing repeatedly (most commonly the phase-switch-box
  probe on single-phase hardware, or a transient glitch on the config read)
  used to make *every* entity in the integration go unavailable, because HA's
  `CoordinatorEntity` base class only knows one coordinator-wide success
  flag. Each of the three register blocks now tracks its own last-successful-
  read time; only the entities actually backed by a stale block go
  unavailable now, and they recover automatically once that block succeeds
  again. A genuine total connection loss still takes everything unavailable,
  same as before.

**New**
- Max Charging Current/Power's allowed range, and the energy plausibility
  guard's ceiling, are now derived from the charger's own detected model
  (register `0x101E`) instead of being hardcoded to the single-phase A7300
  family's 7.3kW/32A. Falls back to those A7300 values for any model that
  doesn't match, so nothing is ever left undefined.
- `diagnostics.py`: downloadable config entry diagnostics (Settings →
  Devices & Services → this integration → Download Diagnostics) - detected
  model/firmware/capabilities, per-block polling health, and the transport
  error counters. Host/IP and the RFID card value are redacted.
- Charging session state (the in-progress session's start time/baseline,
  and the last-completed session's summary) now survives an HA restart,
  using HA's standard local storage helper instead of living only in memory.
- Device triggers: Vehicle Plugged In, Charging Started, Charging Stopped,
  Session Completed, Fault, Alarm - available when building automations
  from a device's own trigger picker, not just via entity state triggers.
- A new automation blueprint, **Solar Surplus Charging**
  (`blueprints/automation/foxess_charger/solar_surplus_charging.yaml`):
  raises/lowers Max Charging Current to hold grid import near a configurable
  ceiling, with hysteresis, a minimum time between adjustments, and a
  fail-safe stop if the grid sensor goes unavailable. Grid-limit/surplus
  control only - no tariff or cost logic.

**Fixed**
- Translations were entirely dead: no entity anywhere set `translation_key`,
  so `translations/en.json`/`de.json`'s per-entity names and states were
  never actually used. Now wired up throughout, including several keys that
  didn't match what the code or the translation files actually contained.
- The plain Work Mode **sensor**'s state values were the display strings
  `"Controlled"`/`"Plug&Charge"`/`"Locked"` rather than stable identifiers -
  fixed to `"controlled"`/`"plug_and_charge"`/`"locked"`. The Work Mode
  **select**'s `options` intentionally keep the original capitalized
  strings for now: HA validates `select.select_option` against an entity's
  declared `options` *before* the entity ever sees the call, so changing
  them here would silently break any existing automation/script calling
  `select.select_option` with the old values, with no way for this
  integration to intercept and translate it after the fact. That migration
  is deferred to a documented breaking change in a future major version,
  audited on its own. Phase Sequence's select **did** move to lowercase
  now (`"L2_single"`/`"L3_single"` -> `"l2_single_phase"`/`"l3_single_phase"`),
  since it doesn't carry the same undocumented-legacy-value risk.
- The six enum-valued sensors (Status, CP Status, CC Status, Lock Status,
  Work Mode, Phase Sequence) now show a real, translated "Unrecognized"
  state if the charger ever reports a raw value outside what's documented,
  instead of the same generic native "Unknown" state used for "no reading
  yet" - the two cases are now distinguishable.

**Changed**
- Serial Number, Software Version, and Device Address are now marked as
  diagnostic entities.
- RFID Card is now disabled by default - card IDs are sensitive.

## 2.1.3

**Reliability**
- Modbus TCP responses are now read correctly regardless of how the OS
  chooses to split them across TCP segments. A single `recv()` call used to
  be trusted to return one whole frame - it isn't guaranteed to on a byte
  stream - so the client now reads the 7-byte MBAP header first, then reads
  exactly the number of bytes it declares, looping until each stage is
  complete. Any incomplete read resets the connection so the next call
  reconnects cleanly.
- The response's Unit ID is now checked against the slave ID a request was
  addressed to (same crossed-wires protection as the existing Transaction
  ID check), and a response with an unexpected function code is rejected
  instead of being decoded as if it were the expected one.
- Writes (`Charging`/`Lock`/`Auto Phase Switch` switches, all `number`/
  `select` entities) now verify the response actually echoes the function
  code, address, and value/quantity that were sent, not just that a
  response of plausible length came back.
- A failed write now raises an error HA surfaces in the UI and logbook,
  instead of silently reverting with only a log line. After a successful
  write, once the next poll completes, a read-back that doesn't match what
  was written is logged clearly (the charger acknowledged the write but may
  not have applied it).
- The `Charging`/`Lock`/`Auto Phase Switch` switches and all `number`/
  `select` entities now update automatically on every coordinator refresh
  (previously only on their own writes), matching how the sensors already
  behaved.
- The status/energy/fault/RFID register block (0x1000-0x101D) is read in a
  single Modbus request instead of five separate ones every poll cycle.

**Fixed**
- Sensors backed by a fixed value map (`Status`, `CP Status`, `CC Status`,
  `Lock Status`, `Work Mode`, `Phase Sequence`, `Stop Reason`, and the
  `Work Mode`/`Phase Sequence` selects) now show as unavailable rather than
  a guessed/wrong label if the charger ever reports a raw value outside
  what's documented - two of these (`CC Status`, `Lock Status`) previously
  defaulted to a *specific* state for literally any unrecognized value.
  `Fault Code`/`Alarm Code`'s active-condition lists now log if the charger
  ever sets a bit outside the documented appendix tables, instead of
  silently dropping it.
- Voltage, current, power, and temperature sensors now report unavailable
  instead of a physically impossible number (e.g. thousands of volts/amps)
  if a register read is corrupted - all comfortably outside anything this
  single-phase 7.3kW/32A hardware can actually produce.

**Internal**
- First real automated test suite (`pytest-homeassistant-custom-component`)
  - see `requirements_test.txt`/`tests/`. The energy plausibility guard's
  decision logic moved into its own dependency-free module (`energy_guard.py`)
  so it can be tested directly, with no coordinator or HA stubbing needed.

## 2.1.2

**Fixed**
- **`Total Energy` and `Current Session Energy` were reading each other's registers.** `Total` reported *less* than the current session, which is impossible. Confirmed against live hardware: `0x1016` never resets between sessions (lifetime) while `0x1018` starts from zero when charging begins and tracked kW × elapsed time exactly across a 2h13m session. The two constants were also duplicated as magic numbers in the coordinator's read loop, so they now reference `const.py` instead and can't drift apart again.
- **`Locked` binary sensor showed the exact opposite of the lock state.** Home Assistant's `lock` binary-sensor device class is inverted by design — `on` means *unlocked* — but the sensor returned `on` when the connector was locked. The `Lock Status` sensor and `Lock` switch were always correct; only this entity was wrong.

**Changed**
- `Ambient Temperature` renamed to **Internal Temperature**. Despite the name used in the protocol spec, it's a board-mounted sensor inside the enclosure, not room air — the same unit read 23.6 °C idle and 55.5 °C while delivering 30 A. Scaling was correct; only the label was misleading.
- `Port Temperature` is now **disabled by default**. Not all models fit the probe — the A7300P1 returns the `65535` "no sensor" sentinel, leaving the entity permanently `unknown` and looking broken. Enable it manually if your hardware has it. Port over-temperature protection is unaffected either way (fault bit 4).

## 2.1.1

**Fixed**
- `Allowed Charge Time` / `Allowed Charge Energy` now show blank instead of the literal `65535` sentinel value when the charger reports "no limit set" (idle, no active session). The values were always correct per spec, just displayed unhelpfully - `native_max_value` on both sliders already made it impossible to ever *write* 65535 through this integration, so this only affects the read/display side.

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
