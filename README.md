<p align="center"><img src="logo.png" width="120" alt="FoxESS EV Charger logo"></p>

# FoxESS EV Charger

A [Home Assistant](https://www.home-assistant.io/) custom integration for **FoxESS A-Series EV chargers**, communicating locally over **Modbus TCP**. No cloud account required — the integration polls the charger directly on your LAN over a single persistent connection.

## Supported hardware

Currently developed and tested against the **FoxESS A7300P1-E-B-WO** (7.3 kW, single-phase, 32 A max, 230 V).

> **Three-phase models (11 kW / 22 kW) are not yet auto-detected.** The integration exposes the three-phase registers, but the phase-related entities are **disabled by default** because there is no support yet for the external phase-switching-box accessory that those features depend on. If you have a three-phase model, you can enable the relevant entities manually in the entity settings, but they are untested.

Entities disabled by default (phase-switch-box / three-phase only):

- `select.*_phase_sequence`
- `switch.*_auto_phase_switch` and `binary_sensor.*_auto_phase_switch`
- `number.*_min_switch_interval`
- `sensor.*_l2_voltage`, `sensor.*_l3_voltage`, `sensor.*_l2_current`, `sensor.*_l3_current`
- `sensor.*_phase_sequence`

## Installation

### HACS (recommended)

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/andrewmatten/foxess-ev-charger` with category **Integration**.
3. Search for **FoxESS EV Charger** in HACS and install it.
4. Restart Home Assistant.

### Manual

1. Download this repository.
2. Copy **all files** into a new folder named `custom_components/foxess_charger/` inside your Home Assistant config directory. (The repository is flat — its files become the contents of that folder.)
3. Restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → FoxESS EV Charger**, then provide:

| Setting   | Notes                                              |
|-----------|----------------------------------------------------|
| Host      | The charger's IP address on your LAN               |
| Port      | Modbus TCP port — **default `502`**                |
| Slave ID  | Modbus unit/slave ID (default `1`)                 |

The connection is verified during setup — if the charger can't be reached, you'll see an error on the form immediately rather than a silently-broken entry.

The scan interval is configurable via the integration options (default 10 s, range 5-300 s).

## Entities exposed

- **select** — Work Mode (Controlled / Plug&Charge / Locked); Phase Sequence *(disabled by default)*.
- **number** — Max Charging Current, Max Charging Power, Allowed Charge Time, Allowed Charge Energy, Command Time Validity, Default (fallback) Current; Min Phase Switch Interval *(disabled by default)*.
- **switch** — Charging (start/stop; reads "on" for the *start*, *charging*, and *pause* states — a car-initiated pause is still an active session, not a stopped one), Lock; Auto Phase Switch *(disabled by default)*.
- **sensor** — Status, CP/CC status, Lock status, Work Mode, Stop Reason, internal temperature, L1 voltage/current, charging power, max/min supported power & current, current-session & total energy, Serial Number, alarm/fault codes, RFID card; Port Temperature, L2/L3 voltage & current and Phase Sequence *(disabled by default)*.
  - **Internal Temperature** is the enclosure's board sensor, not room air — expect it to climb well above ambient under load (23.6 °C idle vs 55.5 °C at 30 A on the same unit).
  - The device's **model** is read from the charger itself (register `0x101E`) rather than hardcoded, so the device page shows your actual hardware.
  - **Alarm Code** and **Fault Code** are bitmask registers — multiple conditions can be active at once. Each sensor carries an `active_alarms` / `active_faults` attribute listing the currently-active condition names (e.g. `["overcurrent", "leakage_current"]`), decoded per the protocol spec's appendix tables, instead of just a raw integer.
- **binary_sensor** — Charging, Vehicle Connected, Fault, Alarm, Locked; Auto Phase Switch *(disabled by default)*.

## Known limitations

- **Some settings read as `65535` (0xFFFF) until a charge session starts.** *Allowed Charge Energy* and *Allowed Charge Time* report this sentinel/placeholder value when idle, meaning "no limit set" — it's expected charger behaviour per the protocol spec, not a bug. These display blank rather than `65535`.
- **Port Temperature is not fitted on all models.** The A7300P1 returns the same `65535` sentinel, so the entity is disabled by default. Port over-temperature protection still works in firmware (fault bit 4) — only the live reading is missing.
- *Max Charging Current* and *Max Charging Power* only take effect while the charger is in the **charging** state (per the protocol spec) — setting them earlier may be a no-op until a session actually starts, and both reset to the charger's max after each session ends.
- Three-phase / phase-switch-box features are untested (see *Supported hardware* above).

## Credits

Forked and adapted from [ringaction/foxess_charger](https://github.com/ringaction/foxess_charger). Thanks to the original author for the groundwork on the FoxESS Modbus protocol.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

Released under the [MIT License](LICENSE).
