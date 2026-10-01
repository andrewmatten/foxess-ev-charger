"""Stateful wrapper around energy_guard's pure plausibility decisions.

Owns the per-counter last-known-good baseline, the rolling acceptance
window and the rejection log; energy_guard.py decides whether each raw
reading is plausible. Baselines persist as ``{key: {"raw", "wall_ts"}}``
(the 2.4.3 ``_energy_baseline`` schema) so the first reading after a
restart is rate-checked against the last good value rather than trusted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Callable

from .const import (
    ENERGY_GUARDS, ENERGY_QUANTUM_KWH, MAX_ENERGY_REJECTION_RECORDS, get_capabilities,
)
from .energy_guard import (
    ENERGY_WINDOW_SECONDS, NEAR_ZERO_RAW_UNITS, check_cumulative_window,
    decide_energy_reading,
)

_LOGGER = logging.getLogger(__name__)

# The rolling window is only judged once it holds enough samples to mean
# something, so the first polls after a restart cannot trip it.
ENERGY_WINDOW_MIN_SAMPLES = 5


class EnergyTracker:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._wall = wall_clock
        self._last: dict[str, tuple[int, float]] = {}
        self._window: dict[str, list[tuple[float, int]]] = {}
        self.rejections: list[dict] = []
        self.dirty = False

    def restore(self, stored: dict[str, dict]) -> None:
        now_mono, now_wall = self._clock(), self._wall()
        for key, entry in stored.items():
            elapsed = max(now_wall - entry["wall_ts"], 0.0)
            self._last[key] = (entry["raw"], now_mono - elapsed)

    def export(self) -> dict[str, dict]:
        now_mono, now_wall = self._clock(), self._wall()
        return {
            key: {"raw": raw, "wall_ts": now_wall - (now_mono - ts)}
            for key, (raw, ts) in self._last.items()
        }

    def last_good(self, key: str) -> int | None:
        entry = self._last.get(key)
        return entry[0] if entry else None

    def apply(self, data: dict, *, session_boundary: bool) -> None:
        """Replace each raw energy counter in ``data`` with a guarded value:
        accepted readings pass through, rejected or missing ones fall back
        to the last accepted value (or are removed if there is none)."""
        for key in ENERGY_GUARDS:
            raw = data.get(key)
            if not isinstance(raw, int) or isinstance(raw, bool):
                last = self.last_good(key)
                if last is None:
                    data.pop(key, None)
                else:
                    data[key] = last
                continue
            accepted = self._sanitize(key, raw, data, session_boundary)
            if accepted is None:
                last = self.last_good(key)
                if last is None:
                    data.pop(key, None)
                else:
                    data[key] = last

    def _max_power_kw(self, data: dict) -> float:
        rated = get_capabilities(data.get("id_model_code"))["max_power_kw"]
        live = (data.get("max_power_raw") or rated * 10) * 0.1
        return max(min(live, rated * 1.5), rated)

    def _sanitize(self, key: str, raw: int, data: dict, session_boundary: bool) -> int | None:
        allow_decrease = ENERGY_GUARDS[key]["allow_decrease"]
        now = self._clock()
        prev_entry = self._last.get(key)
        prev, prev_ts = prev_entry if prev_entry is not None else (None, None)
        max_power_kw = self._max_power_kw(data)
        decision = decide_energy_reading(
            key, raw, prev, prev_ts, now, max_power_kw, allow_decrease,
            session_boundary=session_boundary,
        )
        if not decision.accepted:
            self._reject(key, prev, raw, decision)
            return None

        if allow_decrease and (session_boundary or raw <= NEAR_ZERO_RAW_UNITS):
            # A counter reset is not rate evidence; restart the window.
            self._window[key] = [(now, raw)]
            if session_boundary:
                self.dirty = True
        else:
            window = self._window.setdefault(key, [])
            window.append((now, raw))
            cutoff = now - ENERGY_WINDOW_SECONDS
            while window and window[0][0] < cutoff:
                window.pop(0)
            if len(window) >= ENERGY_WINDOW_MIN_SAMPLES:
                elapsed = window[-1][0] - window[0][0]
                total_kwh = (window[-1][1] - window[0][1]) * ENERGY_QUANTUM_KWH
                if check_cumulative_window(total_kwh, elapsed, max_power_kw):
                    _LOGGER.warning(
                        "Rejected %s: sustained rate over the last %.0fs (%.2f kWh) "
                        "tracks the per-poll acceptance edge - likely repeating "
                        "corruption, not real charging", key, elapsed, total_kwh,
                    )
                    self._reject(key, prev, raw, decision)
                    self._window[key] = []
                    return None

        if prev_entry is None or prev_entry[0] != raw:
            self.dirty = True
        self._last[key] = (raw, now)
        return raw

    def _reject(self, key: str, prev: int | None, raw: int, decision) -> None:
        self.rejections.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "key": key,
            "last_good_raw": prev,
            "rejected_raw": raw,
            "last_good_kwh": round(prev * ENERGY_QUANTUM_KWH, 2) if prev is not None else None,
            "rejected_kwh": round(raw * ENERGY_QUANTUM_KWH, 2),
            "delta_kwh": decision.delta_kwh,
            "elapsed_s": round(decision.elapsed_s),
            "max_plausible_kwh": decision.max_plausible_kwh,
        })
        del self.rejections[:-MAX_ENERGY_REJECTION_RECORDS]
        _LOGGER.warning(
            "Rejected implausible %s read: raw=%d (prev=%s, delta=%.2f kWh over %.0fs, "
            "max plausible=%.2f kWh) - keeping last known-good value",
            key, raw, prev, decision.delta_kwh, decision.elapsed_s,
            decision.max_plausible_kwh,
        )
