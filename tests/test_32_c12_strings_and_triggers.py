"""C12: strings.json matches translations/en.json, drops unused config scan_interval,
and defines device_automation.trigger_type for all 6 triggers in strings/en/de."""
from __future__ import annotations

import json
import pathlib

from custom_components.foxess_charger.device_trigger import TRIGGER_TYPES

COMP_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "foxess_charger"
)


def _read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_strings_json_matches_en_json_and_drops_unused_scan_interval():
    strings = _read_json(COMP_DIR / "strings.json")
    en = _read_json(COMP_DIR / "translations" / "en.json")
    de = _read_json(COMP_DIR / "translations" / "de.json")

    assert strings == en
    for doc in (strings, en, de):
        assert "scan_interval" not in doc["config"]["step"]["user"]["data"]
        assert "scan_interval" in doc["options"]["step"]["init"]["data"]


def test_device_trigger_translations_cover_all_six_triggers():
    for path in (
        COMP_DIR / "strings.json",
        COMP_DIR / "translations" / "en.json",
        COMP_DIR / "translations" / "de.json",
    ):
        doc = _read_json(path)
        trigger_map = doc["device_automation"]["trigger_type"]
        assert set(trigger_map) == set(TRIGGER_TYPES), path.name
        assert all(isinstance(v, str) and v.strip() for v in trigger_map.values())
