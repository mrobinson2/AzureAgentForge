"""Daily memory digest — the operator-curation flywheel. Summarizes the last
window of `memory_*` agent_events plus the
pending pin-candidate / needs-review queue, so 60 seconds/day of confirm/dispute
keeps the trust model curated instead of letting the queue rot.

`format_digest` is a pure function (unit-tested offline); the DB queries that
populate `stats` live in main.py's `/digest` endpoint.
"""

from __future__ import annotations

# Render order for the "wrote N <class>" line.
CLASS_ORDER = ["pinned", "durable_fact", "user_preference", "task_scoped", "decaying", "ephemeral"]


def format_digest(stats: dict) -> str:
    """Render a one-line digest from a stats dict (see /digest). Tolerant of
    missing keys so a quiet day still produces a sensible line."""
    w = stats.get("window_hours", 24)
    wb = stats.get("writes_by_class") or {}
    parts: list[str] = []

    learned = ", ".join(f"{wb[c]} {c}" for c in CLASS_ORDER if wb.get(c))
    parts.append(f"📋 Memory digest (last {w}h): " + (f"wrote {learned}" if learned else "no new memories written"))

    activity = [
        f"{stats[k]} {label}"
        for k, label in (("confirmed", "confirmed"), ("disputed", "disputed"),
                         ("expired", "expired"), ("promoted", "promoted"))
        if stats.get(k)
    ]
    if activity:
        parts.append("; ".join(activity))

    queue = []
    if stats.get("pin_candidates_pending"):
        queue.append(f"📌 {stats['pin_candidates_pending']} pin-candidate(s) pending")
    if stats.get("needs_review"):
        queue.append(f"⚠️ {stats['needs_review']} need(s) review")
    if queue:
        parts.append(", ".join(queue) + " — triage with `pc-memory list --pin-candidates`")

    return " | ".join(parts)
