"""Tests for the adaptive setpoint re-assertion interval.

Adopted 2026-09 from a third-party PR (github.com/loadrunner42) after
comparing it against our own previously-fixed 30s interval: a fixed number
is only safe if it's always comfortably below whatever the charger's own
Command Time Validity (0x3005) is actually configured to, and that register's
documented valid range (10-60s) means a fixed 30s floor had no margin left
if it were ever set below ~60s. See const.py's get_heartbeat_interval().
"""
from __future__ import annotations

from custom_components.foxess_charger.const import (
    get_heartbeat_interval,
    SETPOINT_REASSERT_MIN_INTERVAL,
    DEFAULT_TIME_VALIDITY,
)


def test_halves_the_live_time_validity():
    assert get_heartbeat_interval(180) == 90
    assert get_heartbeat_interval(60) == 30


def test_floors_at_the_minimum_for_a_low_time_validity():
    # 4 -> 2, which is below the 3s floor, so the floor wins.
    assert get_heartbeat_interval(4) == SETPOINT_REASSERT_MIN_INTERVAL


def test_documented_minimum_time_validity_is_not_defeated_by_the_floor():
    """P0 audit fix: the floor used to be 10s, which clamped
    get_heartbeat_interval(10) back up to 10s instead of the 5s the
    charger's own Command Time Validity window (half of 10, its documented
    minimum) actually requires - silently defeating the entire
    half-validity safety guarantee at exactly the value it matters most.
    The floor (SETPOINT_REASSERT_MIN_INTERVAL) must stay below 5s so this
    case is governed by the half-value, not the floor."""
    assert get_heartbeat_interval(10) == 5.0
    assert SETPOINT_REASSERT_MIN_INTERVAL < 5


def test_missing_time_validity_uses_the_documented_default():
    assert get_heartbeat_interval(None) == DEFAULT_TIME_VALIDITY / 2
    assert get_heartbeat_interval(0) == DEFAULT_TIME_VALIDITY / 2


def test_real_deployed_value_matches_expectation():
    """Andrew's charger is currently configured at time_validity=180s
    (confirmed live 2026-09-18) - pins that the interval this actually
    produces on the real device is 90s, comfortably above the old fixed
    30s and with the same safety margin regardless of future reconfig."""
    assert get_heartbeat_interval(180) == 90
