"""Use-based earned trust.

The planner emits a `memory_injected` event per retrieval, recording which
durable memories it injected (keyed to agent + time). When a run reaches
TERMINAL SUCCESS, the memories injected for that agent during the run earn
trust: the watchdog calls the governor's `reconfirm` action per credited doc.

Runs carry no issue id, so attribution matches a success run to the
`memory_injected` events from the SAME agent (roster-resolved) whose timestamp
falls within the run's [startedAt, finishedAt] window. usage_success_count is a
slow, capped signal, so approximate (agent+time) attribution is acceptable;
disputed/superseded memories are guarded on the governor side.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from . import roster
from .detectors import CRASH_STOP_REASONS

# Run.status values that count as terminal success. Defensive superset — the
# exact PaperClip enum is confirmed against live when the loop is switched on.
SUCCESS_STATUSES = frozenset({"completed", "complete", "succeeded", "success", "done"})
DEFAULT_BUFFER_S = 120.0
NO_FINISH_WINDOW_S = 1800.0  # cap the window at 30 min when a run has no finishedAt


def _to_epoch(value) -> Optional[float]:
    """datetime or ISO-8601 string -> epoch seconds; None if unparseable."""
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        try:
            return value.timestamp()
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _is_success(run: dict, success_statuses) -> bool:
    if (run.get("status") or "").strip().lower() not in success_statuses:
        return False
    return run.get("stopReason") not in CRASH_STOP_REASONS


def _injected_events(events: Iterable[dict]) -> list[tuple]:
    """(slug, epoch_ts, [doc_ids]) for each memory_injected event."""
    out: list[tuple] = []
    for e in events:
        if e.get("event_type") != "memory_injected":
            continue
        slug = roster.slug_for(e.get("actor_peer"))
        doc_ids = (e.get("payload") or {}).get("doc_ids") or []
        ts = _to_epoch(e.get("ts"))
        if slug and doc_ids and ts is not None:
            out.append((slug, ts, doc_ids))
    return out


def attribute_successes(
    runs: Iterable[dict],
    events: Iterable[dict],
    *,
    success_statuses=SUCCESS_STATUSES,
    buffer_s: float = DEFAULT_BUFFER_S,
) -> list[dict]:
    """Pure: terminal-success runs × overlapping same-agent memory_injected
    events -> credit tuples ``[{doc_id, run_id, agent_slug}]``, deduped per
    (run_id, doc_id). A run with no parseable startedAt is skipped (can't be
    time-bounded → won't over-credit)."""
    injected = _injected_events(events)
    credits: list[dict] = []
    seen: set[tuple] = set()
    for run in runs:
        if not _is_success(run, success_statuses):
            continue
        slug = roster.slug_for(run.get("agentName") or run.get("agentId"))
        if not slug:
            continue
        started = _to_epoch(run.get("startedAt"))
        if started is None:
            continue  # no lower time bound → skip rather than over-credit
        finished = _to_epoch(run.get("finishedAt"))
        lo = started - buffer_s
        hi = (finished + buffer_s) if finished is not None else (started + NO_FINISH_WINDOW_S)
        run_id = run.get("id")
        for e_slug, e_ts, doc_ids in injected:
            if e_slug != slug or e_ts < lo or e_ts > hi:
                continue
            for doc_id in doc_ids:
                key = (run_id, doc_id)
                if key in seen:
                    continue
                seen.add(key)
                credits.append({"doc_id": doc_id, "run_id": run_id, "agent_slug": slug})
    return credits
