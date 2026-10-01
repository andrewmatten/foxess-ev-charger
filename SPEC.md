# FoxESS controller rebuild - design specification

Status: design record for the 3.0.0 rebuild.

The implementation replaces `custom_components/foxess_charger` in place.

## 1. Evidence vocabulary and scope

**OBSERVED** means supported by the named source, with its scope specified: local telemetry, historical incident record, source-code behaviour, simulator test, or third-party report. An observed source-code behaviour is not proof that hardware behaves as its comment claims.

**ASSUMED** means a proposed design decision, timing parameter, or hardware hypothesis requiring testing. All normative rebuild behaviour in sections 5-10 is **ASSUMED (proposed design)** unless stated otherwise; it must not be described as experimentally verified.

Requirements: one controller owns all writes; preserve domain, entry/device/entity identities, entity IDs, service interfaces, Work Mode strings and stored settings. No identity/serial migration. No second integration. No direct `.storage` edits. No code copied or translated from evcc; reuse correct maps/framing/energy logic from this MIT integration, not its coordinator.

## 2. Reference ledger

- **L1 — Published baseline:** GitHub `v2.4.3` annotated tag `8f0fa0d…`, peeled commit `4490e4a26459089fe2ce16df79c43c25df233437`. Verified with remote tag lookup on this date. Local branch matches the commit and is clean. Export: `published/` beside this document.
- **L2 - Live baseline:** source exported from a running install. Fifteen runtime/manifest/translation files compared; exactly `__init__.py` and `const.py` differ. `live-comparison.txt`, `init-live.diff`, `const-live.diff`, `live.tar` and `live/` retain evidence. The live manifest still says 2.4.3. Hashes establish on-disk source, not loaded bytecode independently.
- **L3 - Telemetry sample:** the existing Software Version sensor reported `1.8`; validity and fallback entities reported `180.0 s` and `8.0 A`; status idle. The version sensor decodes the high and low byte of `0x1001` as decimal major/minor (`sensor.py`), equivalent to raw `0x0108`; that raw word is inferred from the existing decoder, not from a direct Modbus transaction.
- **L4 - Recorder history:** recorder queries covering two charging evenings. Recorder polling cannot resolve sub-poll ordering.
- **L5 - Incident records:** notes on three earlier incidents (battery-protection stop/resume, power sawtooth, refused Start), the published CHANGELOG and tests. Later updates and current source/history take precedence over older notes.
- **L6 - Review reproductions:** a review suite of eight probes. Against the published code, 290 original tests passed and seven additional probes failed; against the live overlay, 286 original tests passed and four failed, with eight probes failing. Not all old-suite failures prove a hardware defect: three assert the superseded Start sequence and one patches the old retry constant.
- **E1 — evcc source:** [pinned driver](https://github.com/evcc-io/evcc/blob/4d8139b425842775fa2a1200aca8a4543b6471a1/charger/foxess-evc.go), repository HEAD resolved on this date. Licence notice excludes this module from MIT. Inspected for behaviour only.
- **E2 — evcc tests:** [historical test file](https://github.com/evcc-io/evcc/blob/0d024070b8f4c638aa16d85d8d7721f6563d3f12/charger/foxess-evc_test.go). Current master returns 404; [commit 545867b](https://github.com/evcc-io/evcc/commit/545867b1e95314928f632a91765ffc343fb8296c) removed it on 2026-08-01 while removing broken session-energy support. Historical tests were located and read, not copied.
- **E3 — Community evidence:** [discussion 26218](https://github.com/evcc-io/evcc/discussions/26218). Reports power-based control and fallback/resume surprises. It also contains experimental configurations and contradictory energy-register examples; it is not an authoritative replacement for locally verified register maps.
- **E4 — Firmware report:** [discussion 28130](https://github.com/evcc-io/evcc/discussions/28130) contains a user's firmware 1.05/OCPP 1.07 current-limiting failure report. It concerns OCPP, not proof of a Modbus 0x3001 defect on local firmware 1.8. No firmware update/downgrade is proposed.

## 3. Start-first patch (refused-Start incident)

**OBSERVED — source diff, L1/L2.** Published Start holds the command lock, writes staged current/power limits first, then sends `0x4001=1`. Failed/exceptional limits or Start trigger compensating `0x4001=2` and pending-Stop protection.

Live behaviour differs as follows:

1. Deletes the entire pre-Start staged-limit loop.
2. Sends `0x4001=1` under the existing command lock. An exception still invokes compensating Stop.
3. A false write result causes a fresh FC03 read of `0x1000..0x1003` via added `_read_status_now()`. Status 2/3/4 is treated as already started. Any other non-None raw status returns failure without compensation; unavailable status invokes compensation. This includes an invalid non-None status, a remaining validation weakness.
4. If the Start result and command generation remain valid, marks charging desired, releases the command lock, then sleeps five seconds (`POST_START_SETTLE_S=5`). Stop can obtain the lock during that sleep.
5. Reads status again; updates cached status only if active; then submits each staged limit through the existing generation-checked setpoint writer. Failed caps increment diagnostics and request heartbeat retry. The method ultimately returns True even if all cap writes fail or a newer Stop supersedes the post-delay writes. The switch's later generation/read-back logic is separate.
6. Adds `FAILED_WRITE_RETRY_S=3`; pending heartbeat/Stop retries use the smaller of the normal interval and this constant. Previously retries used the separate minimum-interval constant directly.

**OBSERVED — historical explanation recorded in live comments, L2.** A nonzero limit write could start a session implicitly; a subsequent explicit Start received exception 0x03; the published compensating Stop then ended that session. The patch was intended to avoid this start/stop loop. The client returns boolean failure, so live code does not actually distinguish exception 0x03 from a lost reply before status reconciliation.

**OBSERVED - corroboration, not a wire trace, L4.** Field telemetry was consistent with an implicit start followed by a refused explicit Start; no matching command log was recovered, so exact exception causality is inferred rather than proven.

**ASSUMED — not a safety guarantee.** The live comment's expectation that the vehicle waits 15–20 seconds before drawing power does not prove a universal safe five-second uncapped interval. The rebuild must not rely on that delay, or copy this Start sequence.

## 4. Firmware/behaviour ledger

| ID | Classification | Evidence and implication |
|---|---|---|
| Q1 | OBSERVED — local HA decoder | Firmware is reported as 1.8, not 1.05 (L3). Different app/OCPP firmware identifiers may exist; do not equate them. |
| Q2 | OBSERVED — incident record | An intended Stop was later followed by an unconditional power update that implicitly resumed charging, and the heartbeat sustained it (L5). A limit update while off must never authorize resume. |
| Q3 | OBSERVED — history and incident analysis | Measured power oscillated between a low cap and full power while the desired cap was steady (L4/L5). Validity read 180 s; old heartbeat was 90 s. Roughly 60 s firmware expiry is the high-confidence explanation, not a direct timer trace. |
| Q4 | OBSERVED — published correction | Heartbeat interval capped at 30 s and rapid retries were introduced; recorded subsequent charging session validation passed (L5/L6). Never schedule at 90 s merely because the register says 180. |
| Q5 | OBSERVED — reference implementation; ASSUMED locally | E1 implements zero-power disable and positive-power enable; local zero-pause/resume has not been demonstrated. Must be the first live functional test. |
| Q6 | OBSERVED — reference; ASSUMED locally | Status 9 is treated as phase switching in E1/E2. Preserve intent/session through it; it cannot prove Stop or unplugging. Single-phase hardware may never emit it. |
| Q7 | OBSERVED — current value; ASSUMED physical fallback | Local default-current entity reads 8 A. Setting raw 60 to request 6 A is proposed as user-required startup safety configuration. Its effect after communications loss, especially after zero pause, requires physical validation. Six amps can still consume roughly 1.4 kW; it is not a guarantee of remaining stopped. |
| Q8 | OBSERVED — source/tests/incidents | TCP fragmentation, frame association/echo validation, 0.1 kWh quantisation, energy baseline persistence and implicit-resume protection have meaningful existing tests and incident context (L1/L5/L6). Retain their guarantees. |
| Q9 | ASSUMED — command failure model | Refused write, lost reply after application, acknowledgement before delayed application, ignored command and disconnection are distinct. Simulation must cover all; no ACK-only success. Local occurrence of every variant is not claimed. |
| Q10 | OBSERVED — review simulations | Stale heartbeat overwrite, cancelled-Start latch, invalid telemetry clearing Stop/inhibit, cached read-back, empty-limit fault omission and restored arbitrary-register writes were reproduced (L6). |
| Q11 | OBSERVED — external report only | Firmware 1.05 report is OCPP-specific (E4). It motivates measured-effect tests, not automatic removal of the current entity or a claim about our Modbus firmware. |
| Q12 | ASSUMED — bounded firmware ordering | The device processes our serialized requests without arbitrarily applying an old queued value after a later confirmed command. Test delayed-apply scenarios; investigate ordering on hardware. Modbus supplies no revision token with which to make an absolute guarantee against arbitrary firmware reordering. |

### Evaluation of the primary candidate

**OBSERVED — E1.** The driver combines enable and power, serializes state/writes, refreshes its setpoint periodically, requests validity 60 s and reads it back, and recognises status 9. It defines a fallback-current constant but does not write that register. Its write helper accepts acknowledgement without immediate application read-back. Its cached setpoint can be updated by readings; our intent must instead remain independent of hardware drift.

**OBSERVED — E2.** Historical tests cover power conversion/bounds, phase/status interpretation, enable/disable writes and concurrent access through a mock Modbus server. That mock applies writes immediately; it does not establish delayed/lost-command safety.

**ASSUMED — selected candidate.** Adopt power-setpoint control as the main on/off mechanism, independently implemented. Add local firmware validation, persistent intent, confirmed writes, safe failure states and 6 A fallback. Do not import evcc's code, test data tables or comments.

## 5. Proposed control contract

All items here are **ASSUMED — design for review**, not claims of local hardware validation.

### 5.1 One owner, one intent

- One `ChargingController` owns the write-capable transport. Entities, poller, persistence and protocol decoder cannot issue writes. All controller state mutation occurs on the HA event loop; transport workers return immutable results.
- One immutable, revisioned intent record contains enabled/paused intent, desired power and the existing independent current ceiling. Requested power stays distinct from the derived wire value: paused => raw zero; enabled => validated positive target in 0.1 kW units. There is one power refresh loop, not separate Start, Stop and setpoint writers.
- Configuration commands (mode, lock, allowed time/energy, validity, fallback, disabled-by-default phase controls) are queued through the same owner. They do not create another charging authority.
- Reads never replace desired intent with reverted hardware maxima. Observations carry block, request sequence, acquisition timestamps and validity; command results carry intent revision, actual read-back and outcome.

### 5.2 On/off and compatibility

- Normal pause writes `0x3002=0`; resume writes the saved positive power. Range for the verified single-phase candidate is zero or at least 1.4 kW, bounded by validated device capability. Existing inputs between zero and 1.4 remain representable as saved settings but must never be silently raised above the requested cap; the wire outcome is pause and must be visible.
- No `0x4001` Start or Stop is planned, including no hidden fallback. Its separate session command was central to the refused-Start/compensation incident; mixing methods reintroduces ambiguity. If zero power cannot reliably pause/resume this firmware or mode, stop deployment and revise this spec for review rather than smuggling Start/Stop back in.
- `switch.turn_off` means confirmed user-requested pause, while vehicle-initiated suspension with positive enable intent remains logically on. Status 4 alone is insufficient to decide between these. A zero read-back must be combined with fresh valid status/power observations to confirm cessation; absent/invalid telemetry remains unconfirmed, never off by default.
- `switch.turn_on` succeeds only after fresh setpoint read-back and a known compatible session state (starting/charging/vehicle-paused, with connected cable). Vehicle refusal to draw is not automatically charger failure. No success if hardware remains idle, locked, faulted or unconfirmed.
- Provisional pause confirmation: fresh zero setpoint plus two valid status/power observations at least one second apart, measured power at or below 0.1 kW, and a known compatible inactive/paused state (0/1/4/5) with consistent cable telemetry. Status 2/3/9, fault, missing data or sustained draw leaves pause pending/faulted. Dedicated confirmation reads run within the operation deadline rather than wait for the ordinary poll interval. Threshold/timing are ASSUMED and must be calibrated in the first hardware test; zero register read-back alone is insufficient proof of physical pause.
- Maintain session-completion/device-trigger behaviour at the logical HA session boundary: confirmed intentional off ends the logical session once; vehicle pause/status 9 does not. Use lifetime-energy deltas across logical sessions so an on-device session counter that does not reset on zero-pause cannot double count.
- While intentionally off, changing desired power/current saves the next-session limit but cannot send a nonzero control write, clear off intent or authorize natural resume. Keep writing confirmed zero as necessary to prevent expiry while connected.
- Unplugging requires `cc_status == 0`, never an unknown enum. Preserve the legacy ability to recognise a genuinely new Plug&Charge/RFID session after confirmed unplug/replug; do not clear an intentional pause merely because active telemetry appeared. This external-authorization path needs explicit tests and a fresh session boundary.

### 5.3 Existing current control — compatibility constraint

The current-number entity must not become a decorative or approximate power control. Preserve its saved amperes and direct current-ceiling meaning via `0x3001`, but only through the one controller, guarded by the same intent/revision. Stage while off. When on, order/confirm auxiliary current-limit writes and the authoritative power write as a single operation; a newer pause wins. Refresh the current cap if device expiry requires it, within the same scheduler, never an independent loop. If current application cannot be confirmed/effect-tested, expose failure and pause rather than report an effective ceiling.

This is a deliberate extension beyond a strict power-register-only model, needed to preserve both current and power service semantics. Do not silently substitute a nominal-voltage conversion: that would change the meaning of the existing current control. Power remains the on/off authority. Exact physical enforcement remains a validation gate.

### 5.4 Staging versus success — compatibility clarification

Existing stopped-state number services return after persisting a staged limit. Preserve that API, but label it `staged`, with desired value separate from observed/applied value and `confirmed=false`. It does **not** report charger-write success; there was no nonzero write. All hardware-changing operations require fresh charger confirmation. If “no success” is intended to prohibit even successful local staging, it conflicts with existing service behaviour and must be resolved before the entity contract is implemented.

## 6. Initialization, timing and failure handling

All items here are **ASSUMED — design for review**.

1. Load/validate persisted intent and limits first; obtain fresh status/config/capability observations. Saved off/pending-off always wins over active telemetry. Corrupt/ambiguous restoration selects protective pause, not maximum power. No blind adoption of a nonzero idle register.
2. At deployment, configure `0x3005=60` and `0x3006=60` (raw 6 A), with fresh reads proving both settings. Establish the protective/current desired power promptly rather than waiting a full heartbeat; do not leave an active restored session uncapped during initialization.
3. Initialization writes are centrally scheduled. If safety configuration cannot be confirmed, positive enable requests are blocked and protective zero attempts continue with visible fault state. The initial order of fallback/validity/power writes must be covered against an already active session; none may be assumed incapable of affecting charging without evidence.
4. Schedule successful setpoint refreshes at half effective validity, capped at 30 s using the recorded local expiry bound. If confirmed validity is shorter than 60 s, use its half interval, not a 30 s minimum. If validity is nonsensical or the transport cannot meet its timing budget, do not enable. Schedule from the previous transmission time, accounting for I/O duration, not “sleep 30 s after finishing.”
5. Provisional engineering budgets: whole Modbus transaction deadline 5 s (raised from 2 s: replies sometimes exceed 2 s, and a timed-out request's late reply arriving during the *next* transaction must be recognized as stale rather than desyncing the connection - see CHANGELOG); confirmation budget up to 10 s for control/config, per register rather than split between several sharing one operation; retry target at most 3 s and earlier when expiry margin requires. These are testable starting values, not hardware facts. Read-back polling and retry pacing must remain compatible with that deadline and cannot starve zero/off commands.
6. Serialize the entire write/read-back operation. Recheck the latest revision immediately before transport dispatch. Drop superseded queued work; periodic refresh reads the latest intent under the same guard. No stale snapshot can be written after a newer value has been dispatched. New off intent invalidates queued positive work immediately.
7. Do not cancel an executor future and assume its socket operation ended. Shield/drain bounded transactions; retain uncertain outcomes until reconciled. Cancellation of a caller cannot disable the controller's protective work. On teardown, quiesce the sole writer and close I/O only after draining. Do not intentionally clear saved intent on normal HA restart.
8. Distinguish protocol refusal, transport loss and delayed application. After lost reply, read hardware; matching fresh value can confirm the current revision. ACK alone cannot. Reconcile before issuing another positive revision. Persistent mismatch or fault switches effective output to zero, latches a visible control fault and reports service failure; saved target may remain for recovery, but positive resume requires explicit authorization after a safety fault.
9. Unknown/stale status, cable or other safety-critical telemetry triggers protective zero and uncertainty, never proof of stopped/unplugged. Keep zero retry/refresh active rather than go silent. Valid status 9 is a transition, not disconnection; retain session and constrain output. Optional absent phase-box data does not become a whole-device safety failure.
10. An unchanged value still needs heartbeat refresh. A fresh read is required for a command reported as confirmed; age-based cache freshness is not enough. Reads initiated before the relevant write cannot confirm it. Refresh failure is visible and triggers bounded recovery, not a silent counter.
11. Communication loss cannot guarantee zero draw. The intended 6 A fallback reduces potential load but does not replace confirmation, a real safety interlock or the need for a tested rollback. The spec makes no claim that an HA-only controller can enforce pause while offline.

## 7. Layers and storage

All items here are **ASSUMED — design for review**.

- **Transport:** adapt our proven TCP framing/FC03/FC06/FC16 logic; validate length/TID/unit/function/echo and add absolute deadlines. Reject arbitrary register writes at the controller/protocol boundary. One connection, no polling/write races and no background worker mutating control state.
- **Protocol:** typed register descriptors, units, sentinels, byte order, enums including status 9, validated capabilities and explicit observation validity. Retain the locally verified lifetime `0x1016` / session `0x1018` mapping. Check actual hardware limits against sane model bounds; do not infer support for a new model from its name alone.
- **Controller:** typed phases such as initializing, paused, applying, enabled, uncertain and faulted, plus the single intent record and immutable observations. No entity-private Start/Stop flags. Positive writes require valid authorization, not merely an active status.
- **Persistence:** use HA Store only. Preserve existing version-1 keys `foxess_charger_{entry_id}_session`, `_setpoints`, `_energy_baseline`; import session baseline/last session/prev_status/stop_inhibit/stop_pending and validated desired current/power. Add new controller schema under a separate versioned key if needed so the old version can still load its files on rollback. Keep legacy projections of limits/Stop intent current through Store, with a revision/dirty-save policy; no hand-edited `.storage` file migration.
- **Entities:** preserve exact unique-ID suffixes, domain/platform, units, ranges/options, enabled defaults, service routing, diagnostic attribute contracts and event/device-trigger identifiers; freeze an inventory before implementation. Preserve the two Work Mode maps and exact select options `Controlled`, `Plug&Charge`, `Locked`. Do not convert existing entries to serial-based identity.
- **Intentional device-setting changes:** startup 180→60 s and fallback 8→6 A are the user's requested safety changes, not lost migration values. Preserve recorded pre-deploy values for rollback. Keep existing number services: a later explicitly requested validity/fallback change must read back and display truthfully, recalculate timing, and expose the weaker protection if applicable. Do not silently overwrite a requested setting and claim it succeeded. Startup defaults and user overrides must have documented precedence and tests.
- **Energy:** retain proven pure guard logic and quantisation-aware rate/rolling-window protections; preserve baseline across restart, use lifetime delta for logical sessions, reject impossible/reversed readings unless a validated reset rule applies, and never emit corrupt increments into HA statistics. First observation, long outage, corrupted storage and counter reset remain explicit tests. Do not invent new guard thresholds as part of structural cleanup.

## 8. Tests-first acceptance matrix

All planned tests are **ASSUMED — required acceptance design**. L6's already reproduced failures are OBSERVED software evidence. Tests must drive behaviour, not look for text/private class layouts.

| Scenario | Required observable result |
|---|---|
| Battery-protection incident | Confirmed off followed by higher power/current staging and active-looking status never sends positive power or grants resume; restart does not forget the off decision. |
| 1.4/7 kW sawtooth incident | Simulator expires caps at 60 s even if validity reports 180; 1.4 kW cap remains refreshed, including failed reply/retry and long poll cases. |
| Refused-Start failures | Simulator implicitly resumes on nonzero cap and refuses redundant 0x4001; rebuild completes enable without 0x4001 and without a compensating stop loop. |
| Stale-write race | Old heartbeat cannot overwrite a newer lower limit; latest off supersedes every queued positive operation. Test barriers and scheduler interleavings. |
| Cancellation | Caller cancellation, setup failure and unload leave no orphan writer, permanently blocked confirmation state or lost protective intent. |
| Invalid status/cable | 65535/unknown values cannot confirm off/unplug or complete a session. Status 9 preserves a transitioning session. |
| False read-back | ACK + failed/stale/config read never verifies itself; fresh post-write mismatches fail visibly. |
| Fault with no limits | Protective zero still attempted; no dependency on an existing desired-limit dictionary. |
| Corrupt stored registers | Non-setpoint addresses, booleans, invalid containers/ranges never reach transport; no startup-at-maximum fallback. |
| Failed enable/cap | No successful enabled/confirmed result while intended cap is unapplied; lost reply with correct fresh read-back is distinguishable from refusal. |
| Full session | Start, 7.0→1.4→7.0 kW, pause, resume, HA restart mid-session and lost reply complete without lost intent, unbounded output or duplicate energy/session events. |
| Offline and fallback | Expiry/fallback model includes paused/charging/offline HA cases; test cannot mislabel 6 A fallback as stopped. |
| Compatibility | Registry/unique-ID fixture, all entity service contracts, blueprint tests, legacy stores and downgrade projection pass. |

**Baseline limitation requiring honest acceptance:** Battery-protection and sawtooth protections already exist in 2.4.3; their original regressions may correctly pass there. Do not deliberately break a fixture to claim they fail. For every actual present defect, retain failing baseline → passing rebuild proof against published/live code. For already-fixed incidents, retain passing baseline and rebuild tests, plus a historical pre-fix replay if available. New zero-control tests may fail on the old design, but that is a changed-contract test, not proof the historical defect still exists. This is the proposed interpretation of “all incidents fail on 2.4.3”; literal universal failure is not an honest achievable requirement when fixes are already present.

Maintain an old-test disposition table: retained unchanged, adapted through a public-behaviour adapter, superseded with named replacement/spec reason, or genuinely inapplicable. Parent reviews every deletion/adaptation; private `_charging_desired` flag expectations are not the new architecture's contract. No rewrite/production tests executed in Step 0.

## 9. Review and deployment gates

All items here are **ASSUMED - plan**.

1. Shared type/API contracts and a compatibility inventory are established first; each component is developed and reviewed separately, then merged sequentially.
2. A final whole-codebase review covers concurrency/cancellation, deadlines, false confirmation, persistence/downgrade, compatibility, licensing, secrets, a full simulated session and old-test disposition.
3. A deploy plan must include a verified full HA backup, an exact file backup/hash manifest, preserved device-setting values and legacy Store compatibility, and a rehearsed rollback under five minutes. File restore alone is insufficient if firmware registers were changed.
4. The first live functional test is zero-power pause/resume on the target firmware, with only one writer, defined observation/abort criteria and rollback. It must establish that zero holds while the heartbeat runs, that resume throttles correctly, and that mode/cable/status interpretation works. Necessary safety-configuration writes must be itemised, not silently prepended.
5. Watch one real charging session, including throttling and pause/resume, before publication. Firmware-loss/fallback tests need separately explicit scope; simulator success is not physical proof.

## 10. Key decisions

The recommended design is power-based on/off with **no 0x4001**, a single revisioned controller, fresh confirmation, startup validity 60 s and fallback 6 A, and gated hardware verification.

The design includes the compatibility clarifications: staged number changes are successful local storage operations but never claimed hardware success (§5.4); current remains a real auxiliary ceiling under the same writer (§5.3); logical session/off semantics distinguish user zero-pause from vehicle pause (§5.2); already-fixed incidents are honest regression tests rather than fabricated baseline failures (§8).

