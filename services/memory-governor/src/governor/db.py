"""asyncpg pool + feature-flag reads (60s cache) + agent_events emitter."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import asyncpg

from . import config

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
