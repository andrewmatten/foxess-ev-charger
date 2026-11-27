"""3.2 R5-9: CHANGELOG distinguishes unacked 0x3001 writes (flagged) from wrong read-back (latches)."""
from __future__ import annotations

import pathlib

from test_controller import CUR, POW, make, saved

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def test_changelog_distinguishes_unacked_current_write_from_wrong_readback():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "a charger that ignores it is flagged, not stopped" not in text
    assert "current_cap_mismatch" in text
    assert "read-back still latches to zero" in text


async def test_unacked_matching_current_flags_only_while_wrong_readback_latches():
    """Refused/unacked 0x3001 writes whose read-back still matches flag
    degraded without stopping; a persistently wrong 0x3001 read-back still
    latches comms to zero even while 0x3002 power refreshes succeed."""
    c, io = await make(saved(power=70, current=100), retry_interval=3.0)

    # Matching read-back with refused 0x3001 writes: flagged, keeps charging.
    io.refuse.add(CUR)
    for _ in range(4):
        io.clock.now += 20
        assert not await c._op_refresh()
        assert c.phase == "enabled"
        assert "current_cap_mismatch" in c.diagnostics["degraded_reasons"]
        assert io.regs[POW] == 70
    assert not c.export_state()["safety_latched"]

    # Wrong 0x3001 read-back persisting over the validity window: latches comms.
    io.refuse.discard(CUR)
    io.ignore.add(CUR)
    io.regs[CUR] = 320
    for _ in range(4):
        io.clock.now += 20
        await c._op_refresh()

    assert c.phase == "faulted"
    assert c.latch_cause == "comms"
    assert c.export_state()["safety_latched"] is True
    assert io.regs[POW] == 0
