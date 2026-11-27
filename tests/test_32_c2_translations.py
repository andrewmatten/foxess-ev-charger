"""C2: Translated entity names apply in German, English names/IDs stay unchanged,
de.json wrong_work_mode repair text allows Controlled mode, and work_mode_control
select state keys match WORK_MODE_SELECT_OPTIONS."""
from __future__ import annotations

import json
import os

from homeassistant.helpers import entity_registry as er

from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS
from custom_components.foxess_charger.const import DOMAIN, WORK_MODE_SELECT_OPTIONS
from custom_components.foxess_charger.number import NUMBERS
from custom_components.foxess_charger.sensor import SENSORS

from fake_controller import FakeController
from ha_harness import Harness, make_entry

ENTRY_ID = "c2transentry01"
TRANSLATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "foxess_charger", "translations",
)


def _load(lang: str) -> dict:
    with open(os.path.join(TRANSLATIONS_DIR, f"{lang}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_descriptions_do_not_hardcode_name_overriding_translation_key():
    for desc in (*SENSORS, *BINARY_SENSORS, *NUMBERS):
        assert desc.translation_key
        # EntityDescription.name is UNDEFINED when omitted, never a string.
        assert not isinstance(desc.name, str), desc.key


def test_work_mode_select_state_keys_match_select_options():
    expected = set(WORK_MODE_SELECT_OPTIONS.values())
    for lang in ("en", "de"):
        data = _load(lang)
        states = data["entity"]["select"]["work_mode_control"]["state"]
        assert set(states) == expected, lang


def test_german_wrong_work_mode_repair_allows_controlled_mode():
    de = _load("de")["issues"]["wrong_work_mode"]
    assert "{mode}" in de["description"]
    assert "Plug&Charge" in de["description"]
    assert "Gesteuert" in de["description"]
    assert "nur mit Plug&Charge" not in de["description"]


async def test_german_setup_translates_select_and_switch_names(
    hass, enable_custom_integrations
):
    hass.config.language = "de"
    fake = FakeController(status=3)
    fake.phase_box = {"auto_phase_switch": 1, "min_switch_interval": 10}
    harness = Harness(hass, make_entry(ENTRY_ID), fake)
    assert await harness.async_setup()
    try:
        registry = er.async_get(hass)
        by_uid = {
            e.unique_id: e
            for e in er.async_entries_for_config_entry(registry, ENTRY_ID)
        }
        # Selects and switches previously had hardcoded _attr_name in English.
        assert by_uid[f"{ENTRY_ID}_work_mode"].original_name == "Arbeitsmodus"
        assert by_uid[f"{ENTRY_ID}_phase_sequence"].original_name == "Phasenumschaltung"
        assert by_uid[f"{ENTRY_ID}_charging"].original_name == "Laden"
        assert by_uid[f"{ENTRY_ID}_lock"].original_name == "Sperre"
    finally:
        await harness.async_unload()


async def test_english_setup_keeps_exact_entity_ids_and_names(
    hass, enable_custom_integrations
):
    hass.config.language = "en"
    harness = Harness(hass, make_entry(ENTRY_ID), FakeController(status=1))
    assert await harness.async_setup()
    try:
        registry = er.async_get(hass)
        by_domain_uid = {
            (e.domain, e.unique_id): e
            for e in er.async_entries_for_config_entry(registry, ENTRY_ID)
        }
        assert by_domain_uid[("select", f"{ENTRY_ID}_work_mode")].entity_id == (
            "select.foxess_charger_work_mode"
        )
        assert by_domain_uid[("switch", f"{ENTRY_ID}_charging")].entity_id == (
            "switch.foxess_charger_charging"
        )
        assert by_domain_uid[("switch", f"{ENTRY_ID}_lock")].entity_id == (
            "switch.foxess_charger_lock"
        )
    finally:
        await harness.async_unload()
