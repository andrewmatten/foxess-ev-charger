"""C8: auto_phase_switch binary sensor and switch return None (unknown) for
missing, None, or invalid/sentinel (e.g. 65535) register values."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.foxess_charger.binary_sensor import BINARY_SENSORS, FoxESSBinarySensor
from custom_components.foxess_charger.switch import FoxESSAutoPhaseSwitchSwitch


def _binary(data):
    desc = next(d for d in BINARY_SENSORS if d.key == "auto_phase_switch")
    entity = object.__new__(FoxESSBinarySensor)
    entity.entity_description = desc
    entity.coordinator = SimpleNamespace(data=data)
    return entity.is_on


def _switch(data):
    entity = object.__new__(FoxESSAutoPhaseSwitchSwitch)
    entity.coordinator = SimpleNamespace(data=data)
    return entity.is_on


@pytest.mark.parametrize("data", [None, {}, {"auto_phase_switch": None},
                                  {"auto_phase_switch": 65535}, {"auto_phase_switch": 2}])
def test_auto_phase_switch_unknown_values_return_none(data):
    assert _binary(data) is None
    assert _switch(data) is None


def test_auto_phase_switch_valid_values():
    assert _binary({"auto_phase_switch": 0}) is False
    assert _binary({"auto_phase_switch": 1}) is True
    assert _switch({"auto_phase_switch": 0}) is False
    assert _switch({"auto_phase_switch": 1}) is True
