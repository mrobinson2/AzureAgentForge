"""asyncpg pool + feature-flag reads (60s cache) + agent_events emitter."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import asyncpg

from . import config, escalation_sla

log = logging.getLogger("governor.db")

_pool: asyncpg.Pool | None = None
_flag_cache: dict[str, tuple[bool, float]] = {}


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.database_url(), min_size=0, max_size=5, command_timeout=30
        )
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def flag_enabled(name: str) -> bool:
    """feature_flags lookup with in-process TTL cache. Fails CLOSED (False) —
    a DB hiccup must never accidentally enable governed behavior."""
    now = time.monotonic()
    cached = _flag_cache.get(name)
    if cached and now - cached[1] < config.FLAG_CACHE_TTL_S:
        return cached[0]
    try:
        p = await pool()
        row = await p.fetchrow(
            "SELECT enabled FROM feature_flags WHERE name = $1", name
        )
        enabled = bool(row["enabled"]) if row else False
    except Exception:  # noqa: BLE001 — fail closed, keep serving
        log.exception("flag lookup failed for %s; treating as disabled", name)
        enabled = False
    _flag_cache[name] = (enabled, now)
    return enabled


async def embedding_stats(p) -> dict[str, Any] | str:
    """Embedding-sync staleness for /healthz: how many documents still await the
    embed worker (``sync_state = 'pending'``) and when anything last synced.
    Vector-ranked Plane C silently degrades to trigram while docs sit pending;
    this makes the queue visible. Returns the string "error" instead of raising
    — healthz must not die on a telemetry query."""
    try:
        pending = await p.fetchval(
            "SELECT count(*) FROM documents "
            "WHERE sync_state = 'pending' AND deleted_at IS NULL"
        )
        last = await p.fetchval("SELECT max(last_sync_at) FROM documents")
        return {"pending": pending, "last_sync_at": last.isoformat() if last else None}
    except Exception:  # noqa: BLE001 — telemetry never fails healthz
        log.exception("embedding_stats failed")
        return "error"


# SQL safety valve on the daily memory review-queue digest's raw fetch — NOT
# the visible top-N cap. governor.memory_digest.format_memory_digest applies
# the real, operator-facing cap + "+N more"; this just bounds a pathological
# queue from ever loading an unbounded payload into the process.
MEMORY_DIGEST_FETCH_CAP = 500


async def memory_digest_rollup(fetch_cap: int = MEMORY_DIGEST_FETCH_CAP) -> dict[str, Any]:
    """Read-only rollup for the memory review-queue digest: pending
    pin-candidates, memories the contradiction sweep flagged `needs_review`,
    and memories expiring soon (next 7 days) — all workspace_name-tagged for
    governor.memory_digest's per-workspace grouping.

    Never writes; never raises (returns empty lists on a DB hiccup, the same
    fail-open posture as embedding_stats above)."""
    out: dict[str, Any] = {"pending_candidates": [], "needs_review": [], "expiring": []}
    try:
        p = await pool()
        pending_rows = await p.fetch(
            """SELECT id, workspace_name, memory_class, content, created_at
                 FROM documents
                WHERE (internal_metadata->>'pin_candidate')::boolean = true
                  AND verification_state <> 'confirmed'
                  AND deleted_at IS NULL
                ORDER BY created_at ASC
                LIMIT $1""",
            fetch_cap,
        )
        out["pending_candidates"] = [
            {
                "id": str(r["id"]),
                "workspace_name": r["workspace_name"],
                "memory_class": r["memory_class"],
                "content": r["content"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in pending_rows
        ]

        review_rows = await p.fetch(
            """SELECT id, workspace_name, memory_class, content, review_note, reviewed_at
                 FROM documents
                WHERE verification_state = 'needs_review'
                  AND deleted_at IS NULL
                ORDER BY reviewed_at ASC NULLS FIRST, created_at ASC
                LIMIT $1""",
            fetch_cap,
        )
        out["needs_review"] = [
            {
                "id": str(r["id"]),
                "workspace_name": r["workspace_name"],
                "memory_class": r["memory_class"],
                "content": r["content"],
                "review_note": r["review_note"],
                "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            }
            for r in review_rows
        ]

        expiring_rows = await p.fetch(
            """SELECT id, workspace_name, memory_class, content, expires_at
                 FROM documents
                WHERE memory_class = 'task_scoped'
                  AND expires_at IS NOT NULL
                  AND expires_at <= now() + interval '7 days'
                  AND deleted_at IS NULL
                ORDER BY expires_at ASC
                LIMIT $1""",
            fetch_cap,
        )
        out["expiring"] = [
            {
                "id": str(r["id"]),
                "workspace_name": r["workspace_name"],
                "memory_class": r["memory_class"],
                "content": r["content"],
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            }
            for r in expiring_rows
        ]
    except Exception:  # noqa: BLE001 — read-only reporting path, fail open
        log.exception("memory_digest_rollup failed")
    return out


async def escalation_sla_rollup(
    workspace_name: str | None,
    window_days: int = 7,
    sla_config: Any = None,
) -> dict[str, Any]:
    """Escalation SLA auditor rollup: fetch the window's escalation_opened/
    acked/resolved + autonomy_decision events (payload->>'workspace' is the
    tenant dimension; migration 0001's agent_events indexes serve this query
    shape — no new spine, no new table) and delegate all pairing/SLA math to
    the pure escalation_sla.build_sla_stats. ``workspace_name=None`` rolls up
    across every tenant (the daily-digest posture). Never raises — a DB
    hiccup yields the honest empty stats, not a 500."""
    try:
        p = await pool()
        if workspace_name:
            rows = await p.fetch(
                """SELECT ts, actor_peer, event_type, payload
                     FROM agent_events
                    WHERE ts > now() - make_interval(days => $2)
                      AND event_type = ANY($3::text[])
                      AND payload->>'workspace' = $1
                    ORDER BY ts ASC""",
                workspace_name,
                window_days,
                list(escalation_sla.ROLLUP_EVENT_TYPES),
            )
        else:
            rows = await p.fetch(
                """SELECT ts, actor_peer, event_type, payload
                     FROM agent_events
                    WHERE ts > now() - make_interval(days => $1)
                      AND event_type = ANY($2::text[])
                    ORDER BY ts ASC""",
                window_days,
                list(escalation_sla.ROLLUP_EVENT_TYPES),
            )
        parsed: list[dict[str, Any]] = []
        for r in rows:
            payload = r["payload"]
            if not isinstance(payload, dict):
                try:
                    payload = json.loads(payload) if payload else {}
                except (TypeError, ValueError):
                    payload = {}
            parsed.append(
                {
                    "ts": r["ts"],
                    "actor_peer": r["actor_peer"],
                    "event_type": r["event_type"],
                    "payload": payload,
                }
            )
        return escalation_sla.build_sla_stats(
            parsed,
            workspace=workspace_name,
            window_days=window_days,
            sla_config=sla_config,
        )
    except Exception:  # noqa: BLE001 — read-only reporting path, fail open
        log.exception("escalation_sla_rollup(%s) failed", workspace_name)
        return escalation_sla.empty_stats(workspace_name, window_days)


async def emit_event(
    event_type: str,
    actor_peer: str,
    payload: dict[str, Any],
    channel: str = "system",
    session_id: str | None = None,
    issue_id: str | None = None,
) -> None:
    """agent_events spine. Gated on AGENT_EVENTS_ENABLED; never raises into the
    caller — the spine is observability, not control flow."""
    try:
        if not await flag_enabled("AGENT_EVENTS_ENABLED"):
            return
        p = await pool()
        await p.execute(
            """INSERT INTO agent_events
               (actor_peer, event_type, channel, session_id, issue_id, payload)
               VALUES ($1, $2, $3, $4::uuid, $5, $6::jsonb)""",
            actor_peer,
            event_type,
            channel,
            session_id,
            issue_id,
            json.dumps(payload, default=str),
        )
    except Exception:  # noqa: BLE001
        log.exception("emit_event(%s) failed", event_type)
