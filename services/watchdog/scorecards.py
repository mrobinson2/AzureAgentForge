"""Track-record routing — delegation as a learned policy.

Pure aggregation: turn delegation/run history into per-agent scorecards
(completion rate, median duration, median cost). watchdog.py persists each as a
`durable_fact` memory on the orchestrator's peer scope (planner_hint=track_record)
and the memory planner injects them at delegation time — so the record becomes
the routing policy, not a hand-tuned rule. Degrading scorecards double as
watchdog signals.

No I/O here — `watchdog.py` fetches the run window and writes the memories; this
module just computes, so the whole scoring is unit-testable offline (mirrors
detectors.py / attribution.py)."""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterable, Optional

from .attribution import SUCCESS_STATUSES, _is_success
from .roster import slug_for

# Below this many runs in the window there isn't enough signal to score an agent.
MIN_RUNS = 3


def _parse_dt(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _duration_s(run: dict) -> Optional[float]:
    a, b = _parse_dt(run.get("startedAt")), _parse_dt(run.get("finishedAt"))
    if a and b and b >= a:
        return (b - a).total_seconds()
    return None


def _cost_usd(run: dict) -> Optional[float]:
    for k in ("costUsd", "cost", "computeObservedAmount"):
        v = run.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _summary(name: str, slug: str, total: int, ok: int, comp: float,
             med_dur: Optional[float], med_cost: Optional[float]) -> str:
    dur = f"{med_dur / 60:.0f}m" if med_dur else "n/a"
    cost = f"${med_cost:.2f}" if med_cost is not None else "n/a"
    # The [track-record:<slug>] tag lets the watchdog find + retire the prior
    # scorecard for this agent before writing the fresh one (mutable upsert).
    return (
        f"Delegation track record — {name} ({slug}): {comp * 100:.0f}% completion "
        f"({ok}/{total} runs), median {dur}, median {cost}. "
        f"[track-record:{slug}]"
    )


def compute_scorecards(runs: Iterable[dict], *, min_runs: int = MIN_RUNS) -> list[dict]:
    """Per-agent delegation scorecards from a window of run results. Agents with
    fewer than `min_runs` are skipped (not enough signal). Sorted best-first."""
    by_agent: dict[str, dict] = {}
    for r in runs:
        name = r.get("agentName") or r.get("agentId")
        slug = slug_for(name)
        if not slug:
            continue
        by_agent.setdefault(slug, {"name": name, "runs": []})["runs"].append(r)

    cards: list[dict] = []
    for slug, d in by_agent.items():
        rs = d["runs"]
        if len(rs) < min_runs:
            continue
        ok = sum(1 for r in rs if _is_success(r, SUCCESS_STATUSES))
        durs = [s for r in rs if (s := _duration_s(r)) is not None]
        costs = [c for r in rs if (c := _cost_usd(r)) is not None]
        comp = ok / len(rs)
        med_dur = statistics.median(durs) if durs else None
        med_cost = statistics.median(costs) if costs else None
        cards.append({
            "slug": slug,
            "name": d["name"],
            "total": len(rs),
            "ok": ok,
            "completion_rate": round(comp, 3),
            "median_duration_s": round(med_dur, 1) if med_dur is not None else None,
            "median_cost_usd": round(med_cost, 4) if med_cost is not None else None,
            "summary": _summary(d["name"], slug, len(rs), ok, comp, med_dur, med_cost),
        })
    cards.sort(key=lambda c: c["completion_rate"], reverse=True)
    return cards
