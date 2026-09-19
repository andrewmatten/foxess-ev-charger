"""Tests for the 2.1.3 register validation changes: decode_enum/decode_bitmask
unknown-value handling, and physical plausibility bounds on sensor.py's
numeric entity descriptions.

decode_enum/decode_bitmask are pure (no HA imports at all beyond the module
they live in needing homeassistant.const.Platform - see const.py), so most
of this needs no HA test harness. The plausibility-bound test builds a
FoxESSChargerSensor directly against a mocked coordinator.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

from custom_components.foxess_charger.const import decode_bitmask, decode_enum
from custom_components.foxess_charger.sensor import SENSORS, FoxESSChargerSensor


class TestDecodeEnum:
    def test_known_value_maps_to_its_label(self):
        assert decode_enum(1, {0: "idle", 1: "charging"}, "status") == "charging"

    def test_none_value_returns_none_without_logging(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = decode_enum(None, {0: "idle", 1: "charging"}, "status")
        assert result is None
        assert caplog.text == ""

    def test_unrecognized_value_returns_none_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = decode_enum(99, {0: "idle", 1: "charging"}, "status")
        assert result is None
        assert "Unrecognized status raw value: 99" in caplog.text

    def test_does_not_silently_fall_through_to_a_wrong_known_label(self):
        """The bug this guards: a ternary's "else" branch used to claim a
        specific known state (e.g. "disconnected") for ANY value that wasn't
        the one explicit case, including values that were never actually
        documented as meaning that."""
        mapping = {0: "disconnected", 1: "connected"}
        # raw=2 is neither 0 nor 1 - must not silently become "disconnected".
        assert decode_enum(2, mapping, "cc_status") is None


class TestDecodeBitmask:
    def test_known_bits_decode_to_names(self):
        bit_map = {0: "a", 1: "b", 2: "c"}
        assert decode_bitmask(0b101, bit_map) == ["a", "c"]

    def test_zero_value_is_no_active_conditions(self):
        assert decode_bitmask(0, {0: "a"}) == []

    def test_unknown_bit_is_logged_but_does_not_crash(self, caplog):
        bit_map = {0: "a"}
        with caplog.at_level(logging.WARNING):
            result = decode_bitmask(0b10, bit_map, "fault_code")  # bit 1 undocumented
        assert result == []  # no known condition active
        assert "Unrecognized bit(s) set in fault_code" in caplog.text

    def test_mixed_known_and_unknown_bits(self, caplog):
        bit_map = {0: "a"}
        with caplog.at_level(logging.WARNING):
            result = decode_bitmask(0b11, bit_map, "fault_code")  # bit 0 known, bit 1 not
        assert result == ["a"]
        assert "Unrecognized bit(s)" in caplog.text


class TestTransportErrorsStateClass:
    """2026-09 (second audit): transport_errors' underlying counters are
    plain in-memory attributes on FoxESSModbusClient, which is recreated
    from scratch on every integration reload - so the value legitimately
    drops back to zero, not just increases. TOTAL_INCREASING would have HA's
    recorder treat that drop as a meter rollover and inflate long-term
    statistics; MEASUREMENT correctly allows a non-monotonic value."""

    def test_transport_errors_is_measurement_not_total_increasing(self):
        from homeassistant.components.sensor import SensorStateClass
        desc = next(d for d in SENSORS if d.key == "transport_errors")
        assert desc.state_class == SensorStateClass.MEASUREMENT


def make_sensor(key: str, data: dict) -> FoxESSChargerSensor:
    desc = next(d for d in SENSORS if d.key == key)
    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.entry_id = "test_entry"
    sensor = FoxESSChargerSensor(coordinator, desc, entry)
    return sensor


class TestPlausibilityBounds:
    def test_normal_voltage_reading_passes_through(self):
        sensor = make_sensor("l1_voltage", {"l1_voltage_raw": 2300})  # 230.0V
        assert sensor.native_value == 230.0

    def test_implausible_voltage_from_a_corrupt_register_is_unavailable(self, caplog):
        """raw=65535 (the classic corrupt/sentinel value) decodes to 6553.5V -
        physically impossible for this hardware - must report unavailable,
        not a nonsense number."""
        with caplog.at_level(logging.WARNING):
            sensor = make_sensor("l1_voltage", {"l1_voltage_raw": 65535})
            value = sensor.native_value
        assert value is None
        assert "implausible reading" in caplog.text

    def test_normal_current_reading_passes_through(self):
        sensor = make_sensor("l1_current", {"l1_current_raw": 160})  # 16.0A
        assert sensor.native_value == 16.0

    def test_implausible_current_is_unavailable(self):
        sensor = make_sensor("l1_current", {"l1_current_raw": 5000})  # 500A
        assert sensor.native_value is None

    def test_normal_temperature_reading_passes_through(self):
        # raw*0.1-50 = 25.0C at raw=750
        sensor = make_sensor("ambient_temperature", {"ambient_temp_raw": 750})
        assert sensor.native_value == 25.0

    def test_implausible_temperature_is_unavailable(self):
        # raw=0 -> -50.0C is within bounds; push it further out.
        sensor = make_sensor("ambient_temperature", {"ambient_temp_raw": 99999})
        assert sensor.native_value is None

    def test_normal_power_reading_passes_through(self):
        sensor = make_sensor("charging_power", {"power_raw": 73})  # 7.3kW
        assert sensor.native_value == 7.3

    def test_implausible_power_is_unavailable(self):
        sensor = make_sensor("charging_power", {"power_raw": 9999})  # 999.9kW
        assert sensor.native_value is None


class TestDynamicPowerBoundsForThreePhaseModels:
    """2026-09 (second audit): charging_power/max_supported_power/
    min_supported_power's plausibility ceiling used to be a hardcoded 10kW
    (right for the single-phase A7300, wrong for the three-phase A011/A022
    models const.py already has capability entries for) - now derived from
    the detected model via capability_key, same mechanism number.py already
    uses for Max Charging Current/Power's native_max_value."""

    def test_unknown_model_falls_back_to_the_a7300_default_bound(self):
        """No id_model_code read yet - 7.3kW * 1.5 margin = 10.95kW, close
        to (but not identical to) the old hardcoded 10kW literal."""
        sensor = make_sensor("charging_power", {"power_raw": 73})  # 7.3kW
        assert sensor.native_value == 7.3
        sensor = make_sensor("charging_power", {"power_raw": 999})  # 99.9kW - still implausible
        assert sensor.native_value is None

    def test_a022_22kw_reading_is_no_longer_incorrectly_rejected(self):
        """The actual bug: a real 22kW reading on a detected A022 unit used
        to be rejected outright by the old fixed 10kW ceiling."""
        sensor = make_sensor(
            "charging_power",
            {"power_raw": 220, "id_model_code": "A022-SOME-SUFFIX"},  # 22.0kW
        )
        assert sensor.native_value == 22.0

    def test_a011_11kw_reading_is_no_longer_incorrectly_rejected(self):
        sensor = make_sensor(
            "charging_power",
            {"power_raw": 110, "id_model_code": "A011-SOME-SUFFIX"},  # 11.0kW
        )
        assert sensor.native_value == 11.0

    def test_a022_still_rejects_a_genuinely_implausible_reading(self):
        """The dynamic bound isn't unbounded - a corrupt reading nowhere
        near even the A022's 22kW rating is still caught."""
        sensor = make_sensor(
            "charging_power",
            {"power_raw": 9999, "id_model_code": "A022-SOME-SUFFIX"},  # 999.9kW
        )
        assert sensor.native_value is None

    def test_max_supported_power_and_min_supported_power_scale_too(self):
        for key, data_key in (
            ("max_supported_power", "max_power_raw"),
            ("min_supported_power", "min_power_raw"),
        ):
            sensor = make_sensor(key, {data_key: 220, "id_model_code": "A022-XYZ"})
            assert sensor.native_value == 22.0

    def test_current_sensor_bounds_are_unaffected(self):
        """Current sensors keep their static 40A bound - it already
        comfortably covers every known model's rated current (32A max for
        both A7300 and A022), so there's no equivalent bug to fix there."""
        sensor = make_sensor(
            "l1_current", {"l1_current_raw": 320, "id_model_code": "A022-XYZ"},
        )  # 32.0A, A022's own rated max
        assert sensor.native_value == 32.0
