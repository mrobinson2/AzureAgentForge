"""Voice ops — per-turn latency + cost attribution (phase 5).

Phase 5 of docs/notes/plans/2026-07-22-voice-track.md is persona + ops
hardening. Observability is the offline-testable half: a voice turn's
felt quality is its end-to-end latency (STT + agent + TTS), and its cost must
attribute to the caller. This is the pure aggregation core; wiring it onto the
GenAI OTel pipeline + the model-router per-caller cap is the live follow-on.

Pure and deterministic — durations and costs are passed in, never measured from
a clock here, so tests pin exact percentiles and totals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TurnLatency:
    """Per-stage latency of one voice turn, in milliseconds."""

    stt_ms: float
    agent_ms: float
    tts_ms: float

    @property
    def total_ms(self) -> float:
        return self.stt_ms + self.agent_ms + self.tts_ms


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list (pct in 0..100)."""
    if not sorted_values:
        return 0.0
    rank = math.ceil(pct / 100 * len(sorted_values))
    idx = min(max(rank, 1), len(sorted_values)) - 1
    return sorted_values[idx]


class LatencyAggregator:
    """Accumulates TurnLatency samples and reports stage + total stats."""

    def __init__(self) -> None:
        self._totals: list[float] = []
        self._stt: list[float] = []
        self._agent: list[float] = []
        self._tts: list[float] = []

    def add(self, turn: TurnLatency) -> None:
        self._totals.append(turn.total_ms)
        self._stt.append(turn.stt_ms)
        self._agent.append(turn.agent_ms)
        self._tts.append(turn.tts_ms)

    @property
    def count(self) -> int:
        return len(self._totals)

    def stats(self) -> dict:
        if not self._totals:
            return {"count": 0}
        st = sorted(self._totals)
        mean = sum(self._totals) / len(self._totals)
        return {
            "count": self.count,
            "mean_total_ms": mean,
            "max_total_ms": max(self._totals),
            "p50_total_ms": _percentile(st, 50),
            "p95_total_ms": _percentile(st, 95),
            "mean_stt_ms": sum(self._stt) / len(self._stt),
            "mean_agent_ms": sum(self._agent) / len(self._agent),
            "mean_tts_ms": sum(self._tts) / len(self._tts),
        }


@dataclass
class CostLedger:
    """Per-caller cost attribution for voice turns. `attribute` accumulates a
    turn's cost against a caller (agent/tenant); totals roll up per caller and
    across all callers — the input to the model-router per-caller cap."""

    _by_caller: dict[str, float] = field(default_factory=dict)

    def attribute(self, caller: str, cost: float) -> None:
        if cost < 0:
            raise ValueError("cost must be non-negative")
        self._by_caller[caller] = self._by_caller.get(caller, 0.0) + cost

    def total(self, caller: str) -> float:
        return self._by_caller.get(caller, 0.0)

    def grand_total(self) -> float:
        return sum(self._by_caller.values())

    def breakdown(self) -> dict[str, float]:
        return dict(self._by_caller)
