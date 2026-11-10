"""Latch regressions fixed in 3.1.3 (adversarial review, 2026-11).

Each test reproduces a way the charger was left stuck off, or a Stop that
worked was recorded as failed, with no human around to clear it.
"""
from __future__ import annotations

import time

import pytest

from custom_components.foxess_charger import controller as controller_module
from custom_components.foxess_charger.controller import FAULT_RELEASE_S, ControlError

from test_comms_latch_release import cause, healthy_for
from test_controller import make


async def test_failed_stop_releases_after_ten_minutes_at_any_hour(monkeypatch):
    """The 18:00-21:00 no-release window is gone: stuck off all evening is a failure too."""
    evening = time.struct_time((2026, 11, 6, 19, 0, 0, 4, 310, 0))
    monkeypatch.setattr(controller_module.time, "localtime", lambda *a: evening)
    c, io = await make()
    io.zero_pauses, io.stop_works = False, False   # heard, drawing through zero and Stop
    with pytest.raises(ControlError, match="did not stop"):
        await c.async_pause()
    assert c.phase == "faulted" and cause(c) == "stop_failed"
    io.zero_pauses = True                          # charger stops
    await healthy_for(c, io, FAULT_RELEASE_S + 30)
    assert c.phase == "paused" and not c.intent_enabled
    assert not hasattr(controller_module, "NO_RELEASE_HOURS")
