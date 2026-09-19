"""Tests for the 2.2.0 model -> capability table (const.get_capabilities)
and its use in number.py's dynamic Max Charging Current/Power bounds.

Previously the rated max power (7.3kW) / current (32A) used throughout the
integration - the energy plausibility guard's ceiling, and the Max Charging
Current/Power number entities' native_max_value - were hardcoded to the
single-phase A7300 family regardless of what id_model_code (0x101E)
actually reported. See MODEL_CAPABILITIES in const.py for the sourcing
behind the A011/A022 three-phase entries.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.foxess_charger.const import (
    DEFAULT_CAPABILITY,
    MODEL_CAPABILITIES,
    get_capabilities,
)
from custom_components.foxess_charger.number import NUMBERS, FoxESSNumber


class TestGetCapabilities:
    def test_known_single_phase_model_matches_by_prefix(self):
        # id_model_code reads back the full part number, never the bare
        # table key - must match by prefix, not equality.
        caps = get_capabilities("A7300P1-E-B-WO")
        assert caps == MODEL_CAPABILITIES["A7300"]
        assert caps["max_power_kw"] == 7.3
        assert caps["max_current_a"] == 32.0

    def test_three_phase_11kw_model_matches(self):
        caps = get_capabilities("A011-SOME-SUFFIX")
        assert caps["max_power_kw"] == 11.0
        assert caps["max_current_a"] == 16.0

    def test_three_phase_22kw_model_matches(self):
        caps = get_capabilities("A022-SOME-SUFFIX")
        assert caps["max_power_kw"] == 22.0
        assert caps["max_current_a"] == 32.0

    def test_match_is_case_insensitive(self):
        assert get_capabilities("a7300p1-e-b-wo") == MODEL_CAPABILITIES["A7300"]

    def test_none_model_falls_back_to_default(self):
        """No successful id_model_code read yet - must not crash or return
        an undefined result."""
        assert get_capabilities(None) == DEFAULT_CAPABILITY

    def test_unrecognized_model_falls_back_to_default(self):
        assert get_capabilities("XYZ-UNKNOWN-MODEL") == DEFAULT_CAPABILITY

    def test_default_fallback_is_the_only_tested_hardware(self):
        assert DEFAULT_CAPABILITY == MODEL_CAPABILITIES["A7300"]


def make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    return entry


def make_number(key: str, coordinator_data: dict) -> FoxESSNumber:
    desc = next(d for d in NUMBERS if d.key == key)
    coordinator = MagicMock()
    coordinator.data = coordinator_data
    return FoxESSNumber(coordinator, MagicMock(), desc, make_entry())


class TestDynamicNumberBounds:
    def test_max_charging_current_defaults_to_a7300_when_model_unknown(self):
        entity = make_number("max_charging_current", {})
        assert entity.native_max_value == 32.0

    def test_max_charging_current_follows_detected_three_phase_model(self):
        entity = make_number("max_charging_current", {"id_model_code": "A022-XYZ"})
        assert entity.native_max_value == 32.0
        entity = make_number("max_charging_current", {"id_model_code": "A011-XYZ"})
        assert entity.native_max_value == 16.0

    def test_max_charging_power_follows_detected_model(self):
        entity = make_number("max_charging_power", {"id_model_code": "A022-XYZ"})
        assert entity.native_max_value == 22.0

    def test_non_capability_bound_number_uses_static_description_value(self):
        """allowed_charge_time has no capability_key - must return the
        plain static native_max_value untouched."""
        entity = make_number("allowed_charge_time", {"id_model_code": "A022-XYZ"})
        assert entity.native_max_value == 1440
