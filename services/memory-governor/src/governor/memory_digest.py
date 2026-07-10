"""Daily memory review-queue digest. Distinct from `digest.py`'s one-line
activity counter: this is a per-workspace LISTING of the actual items the
operator needs to act on — pending pin-candidates, memories the contradiction
sweep flagged `needs_review`, and memories expiring soon — so
`pc-memory confirm/dispute/supersede` has a worklist instead of the queue
rotting silently.

Same shape as digest.py: `format_memory_digest` is a pure renderer over a
stats dict (unit-tested offline); the DB rollup lives in main.py's
`/memory-digest` endpoint (db.memory_digest_rollup). This module and its
callers are a READ-ONLY reporting path — nothing here ever writes memory
state.

Capping: an uncapped queue produces a wall of text. Every section is capped
independently to `limit` (default DEFAULT_LIMIT, clamped to MAX_LIMIT) with a
trailing "+N more" line rather than silently truncating — the operator always
knows the true queue size.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
SNIPPET_MAX = 160


def clamp_limit(raw: Any, default: int = DEFAULT_LIMIT) -> int:
    """Pure: coerce a caller-supplied limit into [1, MAX_LIMIT], falling back
    to `default` on anything unparsable — mirrors digest_post's _clamp_window."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, n))


def _cap(items: list[dict], limit: int) -> tuple[list[dict], int]:
    """Pure: (kept, overflow_count). Never mutates `items`."""
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _group_by_workspace(items: list[dict]) -> dict[str, list[dict]]:
    """Pure: stable grouping, insertion order preserved within each group."""
    out: dict[str, list[dict]] = {}
    for it in items:
        ws = it.get("workspace_name") or "(unscoped)"
        out.setdefault(ws, []).append(it)
    return out


def _snippet(text: str | None) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) > SNIPPET_MAX:
        return text[: SNIPPET_MAX - 1] + "…"
    return text


def _render_pending(pending: list[dict], limit: int) -> list[str]:
    lines = [f"**Pending pin-candidates** ({len(pending)} total, confirm/dispute via `pc-memory`)"]
    kept, overflow = _cap(pending, limit)
    by_ws = _group_by_workspace(kept)
    for ws in sorted(by_ws):
        lines.append(f"_{ws}_")
        for it in by_ws[ws]:
            lines.append(f"  • [{it.get('memory_class') or '?'}] {_snippet(it.get('content'))}")
    if overflow:
        lines.append(f"  …+{overflow} more pending — `pc-memory list --pin-candidates`")
    return lines


def _render_needs_review(items: list[dict], limit: int) -> list[str]:
    lines = [f"**Flagged needs_review** ({len(items)} total, contradiction sweep)"]
    kept, overflow = _cap(items, limit)
    by_ws = _group_by_workspace(kept)
    for ws in sorted(by_ws):
        lines.append(f"_{ws}_")
        for it in by_ws[ws]:
            note = _snippet(it.get("review_note")) or "no review note"
            lines.append(
                f"  • [{it.get('memory_class') or '?'}] {_snippet(it.get('content'))} — {note}"
            )
    if overflow:
        lines.append(f"  …+{overflow} more flagged needs_review")
    return lines


def _render_expiring(expiring: list[dict], limit: int) -> list[str]:
    lines = [f"**Expiring soon** ({len(expiring)} total, next 7 days)"]
    kept, overflow = _cap(expiring, limit)
    by_ws = _group_by_workspace(kept)
    for ws in sorted(by_ws):
        lines.append(f"_{ws}_")
        for it in by_ws[ws]:
            when = it.get("expires_at") or "soon"
            lines.append(f"  • [{it.get('memory_class') or '?'}] {_snippet(it.get('content'))} — expires {when}")
    if overflow:
        lines.append(f"  …+{overflow} more expiring")
    return lines


def format_memory_digest(stats: dict) -> str:
    """Render the review-queue digest from a stats dict:
      - limit: int, the per-section visible cap (default DEFAULT_LIMIT)
      - pending_candidates: list[{id, workspace_name, memory_class, content, created_at}]
      - needs_review: list[{id, workspace_name, memory_class, content, review_note}]
      - expiring: list[{id, workspace_name, memory_class, content, expires_at}]

    Tolerant of missing keys; an empty queue renders a short, honest message
    rather than an empty shell."""
    limit = clamp_limit(stats.get("limit", DEFAULT_LIMIT))
    pending = stats.get("pending_candidates") or []
    needs_review = stats.get("needs_review") or []
    expiring = stats.get("expiring") or []

    lines: list[str] = ["🧠 **Memory review queue**", ""]

    if not pending and not needs_review and not expiring:
        lines.append("Nothing pending — clean review queue.")
        return "\n".join(lines).strip()

    if pending:
        lines.extend(_render_pending(pending, limit))
        lines.append("")
    if needs_review:
        lines.extend(_render_needs_review(needs_review, limit))
        lines.append("")
    if expiring:
        lines.extend(_render_expiring(expiring, limit))
        lines.append("")

    return "\n".join(lines).strip()
