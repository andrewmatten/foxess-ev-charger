# Rebuild contracts

Conditions overriding the original SPEC: model reported validity 60/180 independently from effective expiry (including reported180/effective60); incident failing tests use genuinely buggy historical versions; preserve staged/current services; confirmed 0x4001 Stop is mandatory fallback when power0 does not pause.

Package is custom_components/foxess_charger, normal HACS layout.

## B: protocol.py + transport.py (+ own modbus_client.py improvements)

- `RegisterIO` Protocol: `async read(address:int, count:int)->tuple[int,...]`; `async write(address:int,value:int)->None`; `async close()->None`. Reads are fresh wire requests, no cache. Writes acknowledge only, never imply applied. Exceptions `TransportError`, `CommandRefused` (definite protocol refusal), `UnknownOutcome` (write may have applied) in transport.py; all derive from TransportError. Preserve existing sync client public API/tests where possible.
- `ModbusRegisterIO(client)` wraps the existing synchronous client with serialized, cancellation-drained executor operations; closing cannot race a worker. Sync transport gets bounded whole-transaction deadlines. Only controller has this object.
- `protocol.async_read_snapshot(io)` -> dict of existing coordinator keys for entity compatibility. Reads mandatory status block (0x1000,30), config (0x3000,7); optional phase box only when supported; cache static identity outside worker as appropriate. Invalid control enums remain None plus `status_valid`/`cc_status_valid`; never coerce unknown into inactive. Include `observed_at` monotonic. Raw energy remains for energy layer. Immutable/independent observations, no optimistic cache. Per-block acquisition failure marked (do not hide as current success).
- Protocol constants reuse own const.py. Ensure raw status9 accepted as phase_switching, not stopped. `is_valid_status`, `is_valid_cc_status`, `is_active_status` helpers are optional; C must use explicit validation.

## C: controller.py

`ChargingController(io, *, persist=None, clock=time.monotonic, sleep=asyncio.sleep, confirmation_timeout=10.0, retry_interval=3.0)`; persist is async callback receiving export_state dict.

Public async methods: `async_initialize(saved=None, *, configure_safety=True)`, `async_poll()->dict`, `async_set_power(raw:int)`, `async_set_current(raw:int)`, `async_enable()`, `async_pause()`, `async_set_register(address:int,value:int)`, `async_start()` (background loop only), `async_close()`.

User command methods return frozen `CommandResult(outcome:str, revision:int, observed:int|None=None)` where outcome is confirmed/staged/superseded. Failures raise `ControlError`; no success ACK-only. Staging means persisted local intent, never charger success. `export_state()->dict` synchronous; `.data` current independent observation mapping for adapters; `.desired_power_raw`, `.desired_current_raw`, `.phase` string; `.diagnostics` mapping of counters/status (include old diag names where practical). `.intent_enabled` boolean derived from single state record (not independently mutable).

Persistence schema controller v1: `schema:1, enabled:bool, power_raw:int|None, current_raw:int|None, revision:int, safety_latched:bool, stop_fallback:bool`. Explicit off survives restart. Unknown/malformed saved state fails protective. D translates legacy stop_inhibit/stop_pending and limits to this shape; saved=None means first install, require fresh hardware observation before inferring session authorization.

Exactly one controller owns all writes and read/write ordering. Latest accepted intent revision invalidates queued older work. Actual old in-flight transport must drain before new write; never claim hardware can undo arbitrary firmware queue reordering. Fresh readback only. Pause0 timeout/refusal => 0x4001=2; verify known inactive status and low measured power; latch fallback, suppress ALL cap writes/heartbeat that could implicitly restart after fallback. Explicit resume must be deliberate and freshly confirmed, using nonzero power (never 0x4001 Start); failure remains latched/off. Unknown readings cannot confirm stop. Cancellation cleanup doesn't disable protective work.

Expose config/entity control writes only by allowlist and confirm relevant registers/status. No restart register writer. Configure60s/6A only in runtime initialization at runtime initialization.

## D: HA adapters + persistence.py + session/energy tracking

Own __init__.py fresh HA orchestration (do NOT copy coordinator), number/switch/select/sensor/binary_sensor adapters, config_flow/diagnostics/device_trigger compatibility, persistence/session/energy wrappers and own tests. `FoxESSChargerCoordinator` name may remain an adapter for public import compatibility but legacy private flag tests require explicit disposition rather than copying state machine. All commands call C. Legacy entity IDs/suffixes/options/units/defaults preserved. Retain sensor descriptions/maps/energy guard pure logic.

Persistence uses HA Store API only, validates and imports three legacy stores, new controller state key; maintains downgrade-readable legacy projections. Both number desired controls retain service staging. Fresh hardware values separate attributes. Real HA setup/services/unload tests required. Session/energy is independent from controller command state, with logical intentional pause vs vehicle pause semantics.

## A: simulator/tests

Own `tests/rebuild_simulator.py`, simulator tests and incident/full-session tests in own files. Fake implements RegisterIO directly (async fresh read/write/close); manual clock; reported validity and actual expiry independently configurable. Cover60/60,180/180,180/60; zero-pause works/ignored; fallback Stop applied/refused/lost/delayed; implicit resume; never let fake-cache substitute hardware. C may initially use simple local fake; parent integrates A's full fixture.

The maintainer handles old baseline recovery, manifest inventory, old-test disposition and end-to-end verification . Do not edit existing tests to pass by weakening them. Commit only owned files.
