"""Regression tests for the ~91s power sawtooth.

Observed evidence: 0x3005 Command Time
Validity reads 180s, so get_heartbeat_interval() produced a 90s heartbeat -
but the charger reverted 0x3001/0x3002 to maximum ~60s after each write
(surges every ~91s lasting ~40s; a setpoint write was followed by
a surge ~65s later, not 90s). The firmware evidently caps the window at its
documented 60s maximum regardless of the register value, so the heartbeat
must never be computed from more than 60s.
"""
from __future__ import annotations

import pytest

from custom_components.foxess_charger.const import (
    DEFAULT_TIME_VALIDITY,
    get_heartbeat_interval,
)

# ── Unit: interval math ──────────────────────────────────────────────────────

def test_live_value_180_is_capped_to_the_firmware_window():
    assert get_heartbeat_interval(180) <= 30


@pytest.mark.parametrize("tv,expected", [(60, 30), (20, 10), (10, 5), (180, 30), (3600, 30)])
def test_sensible_values(tv, expected):
    assert get_heartbeat_interval(tv) == expected


@pytest.mark.parametrize("tv", [None, 0, -5, "garbage", float("nan")])
def test_missing_or_garbage_uses_safe_default(tv):
    assert get_heartbeat_interval(tv) == DEFAULT_TIME_VALIDITY / 2


# The behavioural 5-minute run against the old _heartbeat_loop is replaced by
# test_rebuild_contract::test_cap_held_for_ten_minutes_with_lost_replies[*-180-60]
# and test_controller::test_refresh_every_30s_even_when_validity_reports_180.
