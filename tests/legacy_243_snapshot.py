"""Hardware-derived coordinator data produced by the published 2.4.3
coordinator (commit 4490e4a, FoxESSChargerCoordinator._fetch) for the
register image built by test_protocol.RegisterMap() (phase box present).

Captured once by running the 4490e4a package unmodified in a
pytest-homeassistant environment with a synchronous adapter serving
RegisterMap().regs (reads of absent addresses returned None, as the old
client did). Excluded: diag_* counters and the session/energy keys the
coordinator derives rather than reads (energy_rejection_log,
last_session, session_start, session_active).

Regenerate only if RegisterMap changes: extract git show 4490e4a:<file>
for every top-level .py file into a custom_components/<name>/ package and
repeat the capture. Do not hand-edit values.
"""

LEGACY_243_SNAPSHOT: dict = {
    'active_alarms': ['phase_cutting_box'],
    'active_faults': ['emergency_stop', 'access_control'],
    'alarm_code': 2,
    'allowed_charge_energy': 65535,
    'allowed_charge_time': 65535,
    'ambient_temp_raw': 760,
    'auto_phase_switch': 1,
    'cc_status': 1,
    'cp_status': 3,
    'current_energy_raw': 125,
    'default_current_raw': 80,
    'device_address': 1,
    'fault_code': 131073,
    'id_model_code': 'A7300P1',
    'id_serial_number': 'SN0001',
    'l1_current_raw': 160,
    'l1_voltage_raw': 2400,
    'l2_current_raw': 0,
    'l2_voltage_raw': 0,
    'l3_current_raw': 0,
    'l3_voltage_raw': 0,
    'lock_status': 1,
    'max_charging_current_raw': 160,
    'max_charging_power_raw': 37,
    'max_current_raw': 320,
    'max_power_raw': 73,
    'min_current_raw': 60,
    'min_power_raw': 14,
    'min_switch_interval': 5,
    'phase_sequence': 1,
    'port_temp_raw': 750,
    'power_raw': 37,
    'rfid_card': 305419896,
    'software_version': 258,
    'status': 3,
    'stop_reason': 1,
    'time_validity': 60,
    'total_energy_raw': 100000,
    'work_mode': 0,
}
