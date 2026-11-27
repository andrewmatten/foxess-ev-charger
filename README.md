<p align="center"><img src="logo.png" width="120" alt="FoxESS EV Charger logo"></p>

# FoxESS EV Charger

A [Home Assistant](https://www.home-assistant.io/) custom integration for **FoxESS A-Series EV chargers**, communicating locally over **Modbus TCP** (requires **Home Assistant 2024.12.0** or newer). No cloud account required — the integration polls the charger directly on your LAN over a single persistent connection.

## Supported hardware

Currently developed and tested against the **FoxESS A7300P1-E-B-WO** (7.3 kW, single-phase, 32 A max, 230 V).

> **Three-phase models (11 kW / 22 kW).** Start, stop and current changes (6-32 A) were confirmed by a user on a 22 kW A022KP1 (issue #4, release 3.1.2); 11 kW models and all phase switching remain untested. The integration exposes the three-phase registers, but the phase-related entities are **disabled by default**. The manual phase selector only does anything if a separate FoxESS phase-switch box is fitted, so it also stays unavailable until you turn on *Phase-switch box fitted* in the integration options. Automatic phase switching is the charger's own function and does not need the box. As of 2.2.0, Max Charging Current/Power's allowed range is derived from the charger's own detected model (register `0x101E`) via a small capability table that includes best-effort entries for the three-phase A011 (11 kW / 16 A) and A022 (22 kW / 32 A) models - inferred from this fork's own lineage and the model-number naming convention, not confirmed against real three-phase hardware. If the detected model string doesn't match, the single-phase A7300 defaults (7.3 kW / 32 A) are used. If you have a three-phase model, you can enable the relevant entities manually in the entity settings, but everything else here is untested.

Entities disabled by default (three-phase models; the phase selector and phase sequence sensor also need a phase-switch box):

- `select.*_phase_sequence`
- `switch.*_auto_phase_switch` and `binary_sensor.*_auto_phase_switch`
- `number.*_min_switch_interval`
- `sensor.*_l2_voltage`, `sensor.*_l3_voltage`, `sensor.*_l2_current`, `sensor.*_l3_current`
- `sensor.*_phase_sequence`

## Installation

### HACS (recommended)

1. In HACS, search for **FoxESS EV Charger** and install it.
2. Restart Home Assistant.

If it does not show up in the search, add it as a custom repository: in HACS open the three-dot menu → **Custom repositories**, add `https://github.com/andrewmatten/foxess-ev-charger` with category **Integration**, then search and install as above.

### Manual

1. Download this repository.
2. Copy the repository's `custom_components/foxess_charger` folder into the `custom_components/` folder of your Home Assistant config directory (so the result is `custom_components/foxess_charger/manifest.json`).
3. Restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → FoxESS EV Charger**, then provide:

| Setting   | Notes                                              |
|-----------|----------------------------------------------------|
| Host      | The charger's IP address on your LAN               |
| Port      | Modbus TCP port — **default `502`**                |
| Slave ID  | Modbus unit/slave ID (default `1`)                 |

**Set the charger's Operation Mode to Modbus TCP in the FoxESS app.** If it is left on FoxCloud, the charger is read-only: the integration can read values but every write (start, stop, current, settings) is blocked.

The connection is verified during setup — if the charger can't be reached, you'll see an error on the form immediately rather than a silently-broken entry. If your router later assigns a new IP address or you change the port or unit ID, use **Settings → Devices & Services → FoxESS EV Charger → ⋮ → Reconfigure** to update the connection in place while keeping all stored energy baselines, session history, saved limits, and entities. Deleting the integration removes its `.storage` files cleanly.

**The charger's Work Mode can be Plug&Charge or Controlled.** Locked mode will not start charging; Home Assistant raises a Repairs warning while the charger is in it.

Change the Work Mode only while no car is connected: the charger applies it only then, and the integration refuses the change otherwise.

- **Plug&Charge** - the charger opens a session by itself at plug-in. The Charging switch pauses and resumes it through the power limit. After an unplug and replug, a session the charger starts by itself is adopted and the switch turns on.
- **Controlled** - nothing charges until the Charging switch is turned on. Turning it on writes the saved limits and, if that alone does not open a session, sends the charger's Start command. With the switch off the integration writes no current limit and no Start, re-asserts a 0 kW limit while a car is connected, and stops any session that appears anyway, including after an unplug and replug.

The scan interval is configurable via the integration options (default 10 s, range 5-300 s).

## Entities exposed

- **select** — Work Mode (Controlled / Plug&Charge / Locked); Phase Sequence *(disabled by default)*.
- **number** — Max Charging Current, Max Charging Power, Allowed Charge Time, Allowed Charge Energy, Command Time Validity, Default (fallback) Current; Min Phase Switch Interval *(disabled by default)*. Max Charging Current, Max Charging Power, and Default Current derive their allowed maximum from the detected model (see *Supported hardware*).
- **switch** — Charging (start/stop; reads "on" for the *start*, *charging*, *pause*, and *phase_switching* states — a car-initiated pause is still an active session, not a stopped one), Lock; Auto Phase Switch *(disabled by default)*.
- **sensor** — Status, CP/CC status, Lock status, Work Mode, Stop Reason, Charging Status Reason, internal temperature, L1 voltage/current, charging power, max/min supported power & current, current-session & total energy, Serial Number, Software Version, Device Address, alarm/fault codes, Transport Errors, last-completed-session energy/duration (persists across HA restarts); Port Temperature, L2/L3 voltage & current, Phase Sequence, and RFID Card *(disabled by default — card IDs are sensitive)*.
  - **Internal Temperature** is the enclosure's board sensor, not room air — expect it to climb well above ambient under load (23.6 °C idle vs 55.5 °C at 30 A on the same unit).
  - The device page shows your charger's **model** (`0x101E`), **serial number** (`0x1022`), and **firmware version** (`0x1001`, formatted as `major.minor`), updating automatically if those registers first answer after startup.
  - **Alarm Code** and **Fault Code** are bitmask registers — multiple conditions can be active at once. Each sensor carries an `active_alarms` / `active_faults` attribute listing the currently-active condition names (e.g. `["overcurrent", "leakage_current"]`), decoded per the protocol spec's appendix tables, while all-`0xFFFF` unreadable sentinels report as unknown.
  - **Entity categories**: setup controls (*Work Mode*, *Lock*, *Command Time Validity*, *Default Current*, *Auto Phase Switch*, *Phase Sequence*, *Min Phase Switch Interval*, *Allowed Charge Time*, *Allowed Charge Energy*) are placed under **Configuration**; protocol, capability, identity, and health sensors (*CP/CC Status*, *Lock Status*, *Work Mode* sensor, *Max/Min Supported Power & Current*, *Alarm/Fault Code*, *Serial Number*, *Software Version*, *Device Address*, *Charging Status Reason*, *Transport Errors*) are placed under **Diagnostic** so the main device card stays focused on live charging controls and telemetry.
- **binary_sensor** — Charging, Vehicle Connected, Fault, Alarm, Locked; Auto Phase Switch *(disabled by default)*.

## Diagnostics, device triggers & blueprint

- **Diagnostics**: download a diagnostics file from the integration's device page (Settings → Devices & Services → FoxESS EV Charger → ⋮ → Download diagnostics) — includes detected model/capabilities, firmware version, per-block polling health, controller state, and Modbus transport error counters. Host/IP, serial number, and the RFID card value are redacted.
- **Device triggers**: Vehicle Plugged In, Charging Started, Charging Stopped, Session Completed, Fault, Alarm — available (with English and German labels) from a device's own trigger picker when building automations, in addition to plain entity state triggers.
- **Blueprint**: [`blueprints/automation/foxess_charger/solar_surplus_charging.yaml`](blueprints/automation/foxess_charger/solar_surplus_charging.yaml) — holds grid import near a configurable ceiling by adjusting Max Charging Current (remember that *Max Charging Power* also caps draw independently if set). Charger entity pickers are filtered to `foxess_charger`. Grid power sensors must report W or kW; kW readings are converted to W, and unavailable, nonnumeric, or unsupported-unit readings immediately stop charging and disarm any staged enable. Its dwell timer is based on actual current/switch changes, so the once-per-minute safety check does not wedge normal adjustments. It requires a connected vehicle (not idle/finished) before starting, starts from the charger's minimum current when turning on from off, and clamps down to minimum current before stopping. Grid-limit/surplus control only; no tariff or cost logic.

## Fallback settings (Command Time Validity, Default Current)

In the FoxESS Modbus specification, **Command Time Validity** (`0x3005`) sets how long `Max Charging Current` / `Max Charging Power` writes remain valid and **Default Current** (`0x3006`) sets the fallback current if Home Assistant stops refreshing them. Each time the integration sets up (Home Assistant start, integration reload) it writes these two settings to the charger and reads them back; charging cannot be turned on until that is confirmed (if this stays unconfirmed — *Safety settings unconfirmed* — check that the charger's Operation Mode in the FoxESS app is **Modbus TCP** rather than read-only **FoxCloud**).

- If you never changed them in Home Assistant, it writes the startup defaults: **60 s** and **6 A**. Values set earlier in the FoxESS app are replaced by these.
- If you set either number in Home Assistant, your value is saved and re-written on every setup instead of the default.
- **Warning:** whatever **Default Current** and **Command Time Validity** you set in Home Assistant is what you are configuring the charger to run unmanaged if Home Assistant stops, crashes, or loses network connectivity (for example, setting Default Current to 32 A authorizes up to 32 A if Home Assistant goes offline).
- **What is actually observed on hardware:** on a tested A7300P1 (firmware 1.8), even when `Command Time Validity` reported `180 s`, the firmware reverted its active power/current caps to its **own rated maximum** about **60 s** after the last write, and physical fallback to `Default Current` on communications loss has not been verified to hold a lower cap or keep a paused session stopped (even 6 A still draws ~1.4 kW). The integration therefore refreshes active caps every ~20–30 s and keeps re-asserting `0 kW` while paused, but **cannot guarantee zero or capped draw while Home Assistant is offline**.
- **Upgrades and rollbacks across 3.1.4:** versions before `3.1.4` did not persist user-chosen fallback values, so upgrading from a release earlier than `3.1.4` (or rolling back to one) resets *Command Time Validity* and *Default Current* once to **60 s** and **6 A**.

Some models (for example the A022KP1) refuse Command Time Validity writes and keep their own fixed value. The integration accepts it if it is 180 s or less.

## When charging stops by itself

The integration sets the power limit to zero and "latches" when it cannot trust the link or the charger reports a fault. The charging switch then reads off and shows `latch_cause` and `needs_human` attributes, and the "Charging Status Reason" sensor says why.

- **`comms`**: lost or unconfirmed writes. Releases by itself after 2 min of healthy readings.
- **`fault`**: a charger fault that can clear by itself (status 6 without fault bits, or only voltage, frequency, temperature, CP or meter-comms bits). Releases after 10 min of healthy readings and not drawing.
- **`stop_failed`**: a Stop the charger did not confirm. Releases after 10 min of healthy readings and not drawing.
- **`hard`**: emergency stop, PE, leakage, overcurrent, contactor, breaker, lock, metering and similar. Never releases by itself.

Each release in a row doubles the wait, up to 2 hours. A release always leaves charging paused; it resumes on a normal turn-on (for example from an automation).

**What to do**

- `comms`, `fault`, `stop_failed`: nothing. It releases by itself, and your automation or you can turn charging on again. If your own automation turns the switch on whenever it is off, make it wait until `latch_cause` is empty.
- `hard` (`needs_human: true`, Repairs notice): check the charger and clear the fault, then turn the charging switch on yourself from the dashboard or app. An automation cannot do it.

## Known limitations

- **Some settings read as `65535` (0xFFFF) until a charge session starts.** *Allowed Charge Energy* and *Allowed Charge Time* report this sentinel/placeholder value when idle, meaning "no limit set" — it's expected charger behaviour per the protocol spec, not a bug. These display blank rather than `65535`.
- **Port Temperature is not fitted on all models.** The A7300P1 returns the same `65535` sentinel, so the entity is disabled by default. Port over-temperature protection still works in firmware (fault bit 4) — only the live reading is missing.
- **Staging and persistence of charging limits.** *Max Charging Current* and *Max Charging Power* are owned and persisted by the integration's controller across sessions and Home Assistant restarts. Setting either limit while paused or unplugged **stages** it locally (`last_outcome: staged`, `confirmed: false`) without waking the charger, then writes and confirms it on the charger when charging is enabled (`confirmed: true` once charging is on and the charger holds the saved value). Setting *Max Charging Power* between `0.1` and `1.3 kW` (below the `1.4 kW` / `6 A` minimum) holds wire output at `0 kW`; *Charging Status Reason* shows *Paused below 1.4 kW minimum*, and turning charging on with such a limit is refused with an error.
- Phase switching and the phase-switch box are untested on 11 kW hardware (see *Supported hardware* above).
- **Power limit reverts on firmware expiry (~60 s) if refreshes stop.** Regardless of what *Command Time Validity* (`0x3005`) reports (even `180 s`), observed firmware reverts `0x3001`/`0x3002` limits to the charger's **own rated maximum** about **60 s** after the last write (see *Fallback settings* above). Avoid restarting Home Assistant or reloading the integration while the car is charging if a power cap matters (for example in a peak tariff window). Changing the integration's options applies live without reloading it.

## Credits

Originally based on [ringaction/foxess_charger](https://github.com/ringaction/foxess_charger), since rewritten. Thanks to the original author for the groundwork on the FoxESS Modbus protocol. This project adds its own session control, safety handling, energy guard, device triggers, blueprint and test suite.

## Releasing

Releases go through `scripts/release.sh`, which refuses to tag the wrong commit.
Commit and push first. The manifest version and a `CHANGELOG.md` heading must already match.

    scripts/release.sh 3.1.3-beta.1 --dry-run   # check only, nothing tagged
    scripts/release.sh 3.1.3-beta.1             # beta: HEAD only needs to be pushed
    scripts/release.sh 3.1.3 --dry-run
    scripts/release.sh 3.1.3                    # stable: HEAD must be origin/main

It fetches, runs the tests, then tags `v<version>` and pushes only that tag.
Afterwards, publish the GitHub release (pre-release for a beta, with the changelog section for stable).

## AI assistance

This integration was developed with the help of AI coding assistants (Claude and Codex) and tested on real hardware (a FoxESS A7300P1-E-B-WO). Bug reports and pull requests are welcome.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

Released under the [MIT License](LICENSE).
