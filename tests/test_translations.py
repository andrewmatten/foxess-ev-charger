"""Tests for the 2.2.0 translation wiring fix.

Before this, no entity anywhere in the integration set `translation_key`
(or `_attr_translation_key`) - translations/en.json and de.json's per-entity
name/state maps were entirely dead: HA only translates an entity's name/
enum states through its `translation_key`, falling back to the raw `name`
attribute (English only) otherwise. On top of that, two of the underlying
enum maps used display-cased strings as the actual state *values*
(WORK_MODE_MAP's "Plug&Charge", PHASE_SEQ_MAP's "L2_single"/"L3_single"),
which could never have matched a lowercase snake_case translation key
anyway even if translation_key had been wired up.

This covers:
1. decode_enum's new unknown_label param (default-preserving).
2. Every device_class=ENUM sensor declares translation_key + "unknown" in
   its own options list (required - HA raises if an ENUM sensor's state
   isn't one of its declared options).
3. WORK_MODE_MAP/PHASE_SEQ_MAP are lowercase snake_case, not display-cased.
4. translations/en.json and de.json are valid JSON and actually contain an
   entry for every translation_key this integration sets, so a future
   renamed/added entity can't silently ship with a dead translation key
   again.
"""
from __future__ import annotations

import json
import logging
import os

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS
from custom_components.foxess_charger.const import (
    CC_STATUS_MAP, CP_STATUS_MAP, LOCK_STATUS_MAP, PHASE_SEQ_MAP,
    STATUS_MAP, WORK_MODE_MAP, decode_enum,
)
from custom_components.foxess_charger.number import NUMBERS
from custom_components.foxess_charger.sensor import SENSORS

_TRANSLATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "translations"
)


def _load(lang: str) -> dict:
    with open(os.path.join(_TRANSLATIONS_DIR, f"{lang}.json"), encoding="utf-8") as fh:
        return json.load(fh)


class TestDecodeEnumUnknownLabel:
    def test_default_unchanged_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert decode_enum(99, {0: "idle"}, "status") is None

    def test_unknown_label_returned_for_unrecognized_value(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = decode_enum(99, {0: "idle"}, "status", unknown_label="unknown")
        assert result == "unknown"
        assert "Unrecognized status raw value: 99" in caplog.text

    def test_none_value_ignores_unknown_label(self):
        """"No reading yet" must stay indistinguishable from HA's own
        native unknown state, not be coerced into the literal "unknown"
        enum option."""
        assert decode_enum(None, {0: "idle"}, "status", unknown_label="unknown") is None

    def test_known_value_ignores_unknown_label(self):
        assert decode_enum(0, {0: "idle"}, "status", unknown_label="unknown") == "idle"


class TestEnumMapsAreStableLowercaseKeys:
    """Guards against display-cased strings being reused as actual state
    values - the bug that made WORK_MODE_MAP/PHASE_SEQ_MAP's states
    untranslatable (and, for WORK_MODE_MAP, contain "Plug&Charge" - not
    even a safe machine-readable identifier)."""

    def test_work_mode_map_is_lowercase_snake_case(self):
        assert set(WORK_MODE_MAP.values()) == {"controlled", "plug_and_charge", "locked"}

    def test_phase_seq_map_is_lowercase_snake_case(self):
        assert set(PHASE_SEQ_MAP.values()) == {
            "three_phase", "l2_single_phase", "l3_single_phase",
        }


# Every device_class=ENUM sensor description in sensor.py, alongside the raw
# value map it decodes through - used to check both the "unknown" option and
# the translation file entries below.
_ENUM_SENSOR_MAPS = {
    "status": STATUS_MAP,
    "cp_status": CP_STATUS_MAP,
    "cc_status": CC_STATUS_MAP,
    "lock_status": LOCK_STATUS_MAP,
    "work_mode_sensor": WORK_MODE_MAP,
    "phase_sequence": PHASE_SEQ_MAP,
}


class TestEnumSensorsDeclareUnknownOption:
    def test_every_enum_sensor_is_covered_by_this_test(self):
        """Fails loudly if a future ENUM sensor is added without being
        added to _ENUM_SENSOR_MAPS above, rather than the coverage below
        silently not checking it."""
        enum_keys = {d.key for d in SENSORS if d.device_class == SensorDeviceClass.ENUM}
        assert enum_keys == set(_ENUM_SENSOR_MAPS)

    def test_options_include_unknown(self):
        for key in _ENUM_SENSOR_MAPS:
            desc = next(d for d in SENSORS if d.key == key)
            assert "unknown" in desc.options, f"{key}: options missing 'unknown'"

    def test_options_include_every_mapped_value(self):
        for key, mapping in _ENUM_SENSOR_MAPS.items():
            desc = next(d for d in SENSORS if d.key == key)
            for value in mapping.values():
                assert value in desc.options, f"{key}: options missing {value!r}"

    def test_unrecognized_raw_value_decodes_to_unknown_not_none(self):
        """Full round trip through the actual value_fn, not just the
        underlying decode_enum() call - pins that each ENUM sensor's
        value_fn was actually updated to pass unknown_label="unknown"."""
        for key in _ENUM_SENSOR_MAPS:
            desc = next(d for d in SENSORS if d.key == key)
            data_key = {
                "status": "status", "cp_status": "cp_status", "cc_status": "cc_status",
                "lock_status": "lock_status", "work_mode_sensor": "work_mode",
                "phase_sequence": "phase_sequence",
            }[key]
            result = desc.value_fn({data_key: 9999})
            assert result == "unknown", f"{key}: expected 'unknown', got {result!r}"


class TestTranslationFilesAreValidAndComplete:
    def test_both_files_are_valid_json(self):
        assert _load("en")
        assert _load("de")

    def test_every_translation_key_used_in_code_exists_in_both_files(self):
        used = {
            "sensor": {d.translation_key for d in SENSORS if d.translation_key},
            "binary_sensor": {d.translation_key for d in BINARY_SENSORS if d.translation_key},
            "number": {d.translation_key for d in NUMBERS if d.translation_key} | {d.key for d in NUMBERS},
            # select/switch entities set _attr_translation_key directly rather
            # than via an EntityDescription - listed explicitly here.
            "select": {"work_mode_control", "phase_switching_control"},
            "switch": {"charging", "lock", "auto_phase_switch"},
        }
        for lang in ("en", "de"):
            data = _load(lang)
            for platform, keys in used.items():
                available = set(data["entity"].get(platform, {}))
                missing = keys - available
                assert not missing, f"{lang}.json entity.{platform} missing: {missing}"

    def test_enum_sensor_states_are_fully_translated_in_both_files(self):
        """Every value an ENUM sensor can actually produce (including
        "unknown") must have a translated state entry - a missing one means
        the frontend falls back to showing the raw, untranslated state
        string for that specific value only."""
        key_to_translation_key = {
            "status": "status", "cp_status": "cp_status", "cc_status": "cc_status",
            "lock_status": "lock_status", "work_mode_sensor": "work_mode",
            "phase_sequence": "phase_sequence",
        }
        for lang in ("en", "de"):
            data = _load(lang)
            for key, mapping in _ENUM_SENSOR_MAPS.items():
                translation_key = key_to_translation_key[key]
                states = data["entity"]["sensor"][translation_key]["state"]
                expected = set(mapping.values()) | {"unknown"}
                assert expected <= set(states), (
                    f"{lang}.json sensor.{translation_key}.state missing "
                    f"{expected - set(states)}"
                )
