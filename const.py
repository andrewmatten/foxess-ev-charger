"""Constants for FoxESS EV Charger integration."""
from homeassistant.const import Platform

DOMAIN = "foxess_charger"

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

# Config Keys
CONF_HOST     = "host"
CONF_PORT     = "port"
CONF_SLAVE_ID = "slave_id"

# Defaults
DEFAULT_PORT          = 502
DEFAULT_SLAVE_ID      = 1
DEFAULT_SCAN_INTERVAL = 10

# ── Read-Only Input Registers (0x1000–0x1022) ─────────────────────────────────
REG_DEVICE_ADDRESS  = 0x1000
REG_SOFTWARE_VER    = 0x1001
REG_STOP_REASON     = 0x1002
REG_STATUS          = 0x1003
REG_CP_STATUS       = 0x1004
REG_CC_STATUS       = 0x1005
REG_PORT_TEMP       = 0x1006
REG_AMBIENT_TEMP    = 0x1007
REG_L1_VOLTAGE      = 0x1008
REG_L2_VOLTAGE      = 0x1009
REG_L3_VOLTAGE      = 0x100A
REG_L1_CURRENT      = 0x100B
REG_L2_CURRENT      = 0x100C
REG_L3_CURRENT      = 0x100D
REG_ACTIVE_POWER    = 0x100E
REG_LOCK_STATUS     = 0x100F
REG_PHASE_SEQUENCE  = 0x1010
REG_MAX_POWER       = 0x1011
REG_MIN_POWER       = 0x1012
REG_MAX_CURRENT     = 0x1013
REG_MIN_CURRENT     = 0x1014
REG_ALARM_CODE      = 0x1015
# These two were swapped until 2.1.2. Confirmed against live hardware: the
# register at 0x1016 never resets between sessions (lifetime), while 0x1018
# starts from zero each time charging begins and tracked kW x elapsed time
# exactly over a 2h13m session. The old mapping made Total read *lower* than
# the current session, which is impossible.
REG_TOTAL_ENERGY    = 0x1016  # UINT32 (2 Register) - lifetime, never resets
REG_CURRENT_ENERGY  = 0x1018  # UINT32 (2 Register) - resets each session
REG_FAULT_CODE      = 0x101A  # UINT32 (2 Register)
REG_RFID_CARD       = 0x101C  # UINT32 (2 Register)
REG_ID_MODEL_CODE   = 0x101E  # ASCII (4 Register / 8 Bytes)
REG_ID_SERIAL_NUMBER= 0x1022  # ASCII (16 Register / 32 Bytes)

# ── Read/Write Holding Registers (0x3000–0x300B) ──────────────────────────────
REG_WORK_MODE            = 0x3000  # 0=Controlled, 1=Plug&Charge, 2=Locked
REG_MAX_CHARGING_CURRENT = 0x3001  # 0.1 A, 6–32 A
REG_MAX_CHARGING_POWER   = 0x3002  # 0.1 kW
REG_ALLOWED_CHARGE_TIME  = 0x3003  # min
REG_ALLOWED_CHARGE_ENERGY= 0x3004  # kWh
REG_TIME_VALIDITY        = 0x3005  # s, 10–60 s
REG_DEFAULT_CURRENT      = 0x3006  # 0.1 A, 6–32 A
REG_AUTO_PHASE_SWITCH    = 0x300A  # 0=off, 1=on
REG_MIN_SWITCH_INTERVAL  = 0x300B  # min, 5–30

# ── Write-Only Registers (0x4000–0x4003) ─────────────────────────────────────
REG_LOCK_CONTROL     = 0x4000  # 0=No action, 1=Unlock, 2=Lock
REG_CHARGING_CONTROL = 0x4001  # 0=No action, 1=Start, 2=Stop
REG_PHASE_SWITCHING  = 0x4002  # 0=3-phase, 1=L2, 2=L3
REG_RESTART          = 0x4003  # 0xA5A5 = Restart

# ── Status Maps ───────────────────────────────────────────────────────────────
STATUS_MAP = {
    0: "idle",
    1: "connected",
    2: "start",
    3: "charging",
    4: "paused",
    5: "finished",
    6: "fault",
    7: "reserved",
    8: "locked",
}

CP_STATUS_MAP = {
    0: "fault",
    1: "12v_disconnected",
    2: "9v_connected",
    3: "6v_ready",
}

WORK_MODE_MAP    = {0: "Controlled", 1: "Plug&Charge", 2: "Locked"}

# ── Fault/Alarm Bitmasks (Appendix 2 & 3) ──────────────────────────────────────
# fault_code (0x101A, UINT32) and alarm_code (0x1015, UINT16) are bitmasks -
# multiple conditions can be active at once, so these are decoded into a list
# of active names rather than looked up as a single enum value.
FAULT_BITS = {
    0:  "emergency_stop",
    1:  "overvoltage",
    2:  "undervoltage",
    3:  "overcurrent",
    4:  "charging_port_overtemp",
    5:  "pe_grounding",
    6:  "leakage_current",
    7:  "frequency",
    8:  "cp",
    9:  "connector",
    10: "ac_contactor",
    11: "electronic_lock",
    12: "breaker",
    13: "cc",
    14: "external_meter_communication",
    15: "metering_chip",
    16: "environment_temperature",
    17: "access_control",
}

ALARM_BITS = {
    0: "card_reader",
    1: "phase_cutting_box",
    2: "phase_loss",
}


def decode_bitmask(value: int | None, bit_map: dict[int, str]) -> list[str]:
    """Returns the list of active condition names for a bitmask register."""
    if not value:
        return []
    return [name for bit, name in bit_map.items() if value & (1 << bit)]


PHASE_SEQ_MAP    = {0: "three_phase", 1: "L2_single", 2: "L3_single"}
STOP_REASON_MAP  = {
    0: "none",           1: "command",          2: "time_completed",
    3: "s2_timeout",     4: "pause_timeout",    5: "emergency_stop",
    6: "cp_abnormal",    7: "connector_pulled", 8: "ac_contactor",
    9: "lock_abnormal", 10: "card_reader",      11: "overcurrent",
    12: "overvoltage",  13: "undervoltage",     14: "port_overtemp",
    15: "leakage",      16: "n_line_reversed",  17: "freq_abnormal",
    18: "stop_button",  19: "breaker",          20: "phase_loss",
    21: "pe_abnormal",  22: "ext_meter",        23: "ambient_overtemp",
    24: "metering_chip",25: "access_control",   26: "pbox_phase_switch",
    27: "energy_limit",
}
