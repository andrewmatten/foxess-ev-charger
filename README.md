# FoxESS EV Charger

A [Home Assistant](https://www.home-assistant.io/) custom integration for **FoxESS A-Series EV chargers**, communicating locally over **Modbus TCP**. No cloud account required — the integration polls the charger directly on your LAN.

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

The scan interval is configurable via the integration options (default 10 s).

> **Firmware requirement: 1.06 or newer.** Earlier firmware cannot read the configuration register block (`0x3000–0x300B`), so the R/W settings (work mode, current/power limits, etc.) will be unavailable.

## Entities exposed

- **select** — Work Mode (Controlled / Plug&Charge / Locked); Phase Sequence *(disabled by default)*.
- **number** — Max Charging Current, Max Charging Power, Allowed Charge Time, Allowed Charge Energy, Command Time Validity, Default (fallback) Current; Min Phase Switch Interval *(disabled by default)*.
- **switch** — Charging (start/stop), Lock; Auto Phase Switch *(disabled by default)*.
- **sensor** — Status, CP/CC status, Lock status, Work Mode, Stop Reason, port & ambient temperature, L1 voltage/current, charging power, max/min supported power & current, current-session & total energy, alarm/fault codes, RFID card; L2/L3 voltage & current and Phase Sequence *(disabled by default)*.
- **binary_sensor** — Charging, Vehicle Connected, Fault, Alarm, Locked; Auto Phase Switch *(disabled by default)*.

## Known limitations

- **Some settings read blank until a charge session starts.** *Allowed Charge Energy*, *Allowed Charge Time*, *Default Current*, and *Max Charging Current* can show no value until the charger has an active session. This is because the charger reports sentinel/placeholder values on those registers when idle — it is expected behaviour, **not a bug**.
- Three-phase / phase-switch-box features are untested (see *Supported hardware* above).

## Credits

Forked and adapted from [ringaction/foxess_charger](https://github.com/ringaction/foxess_charger). Thanks to the original author for the groundwork on the FoxESS Modbus protocol.

## License

Released under the [MIT License](LICENSE).
