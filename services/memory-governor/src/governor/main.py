"""Memory Governor — FastAPI app.

Internal-only service. Operator traffic arrives through the auth-proxy
passthrough (/api/memory/* -> here) which injects the shared X-Governor-Key;
in-network platform callers (the deriver hook, the memory CLI helper) attach the
same key from their mounted secret.

This module grows phase by phase. Today it exposes /admit (the admission choke
point), the operator /memory/* admin surface, and the Plane D /session-memory
CRUD. The retrieval planner, background loops, digest, and skill-candidate
surfaces are added in later phases (see the TODO markers below).
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config, db, digest, escalation_sla
from . import memory_digest as mem_digest  # `memory_digest` below is the /digest handler
from .memory import admission, planner

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("governor.main")

app = FastAPI(title="memory-governor", version=config.SERVICE_VERSION)


async def require_key(x_governor_key: str | None = Header(default=None)) -> None:
    # Fail CLOSED: an unconfigured key must mean "down", never "open". The old
    # `if config.GOVERNOR_API_KEY and ...` silently disabled auth on every route
    # when the secret mount was absent (aaf-0004). config.database_url() already
    # raises on absence; auth must be no less strict. Constant-time compare so a
    # response-timing side channel can't leak the key byte by byte.
    if not config.GOVERNOR_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="governor authentication not configured (GOVERNOR_API_KEY unset)",
        )
    if not hmac.compare_digest(x_governor_key or "", config.GOVERNOR_API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid X-Governor-Key")


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    import logging

    from . import annotator, contradiction, migrate, scope_watcher, skill_miner

    # Apply the governed-memory schema overlay (idempotent) before the loops run,
    # so a fresh deployment has the columns/tables the governor queries. Fail-open:
    # a migration error is logged, not fatal (the API still serves; loops fail closed).
    try:
        await migrate.apply()
    except Exception:
        logging.getLogger("governor.main").exception(
            "schema migration failed — governed memory may not work until resolved")

    # Always-spawn, gate-inside: each loop checks its own feature flag every
    # cycle and idles when off, so spawning them is a no-op until a flag is on.
    # The second-stage classifier loop for deriver-emitted docs.
    app.state.annotator_task = asyncio.create_task(annotator.run_forever())
    # Task-scope lifecycle watcher. Idle unless PAPERCLIP_BASE_URL + the
    # automation JWT secret are configured.
    app.state.scope_watcher_task = asyncio.create_task(scope_watcher.run_forever())
    # Contradiction detection sweep (MEMORY_CONTRADICTION_SWEEP_ENABLED); idles
    # otherwise. Uses the in-pod router for the LLM judge.
    app.state.contradiction_task = asyncio.create_task(contradiction.run_forever())
    # Skill-autogen miner (SKILL_AUTOGEN_ENABLED); idles otherwise.
    app.state.skill_miner_task = asyncio.create_task(skill_miner.run_forever())
    # The TTL sweeper runs as a separate scheduled job (python -m
    # governor.sweeper), not as an in-process loop.


@app.on_event("shutdown")
async def _shutdown() -> None:
    for attr in ("annotator_task", "scope_watcher_task", "contradiction_task",
                 "skill_miner_task"):
        task = getattr(app.state, attr, None)
        if task:
            task.cancel()
    await db.close()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    # aaf-0011: unauthenticated liveness ONLY. The full feature-flag registry
    # (a security-posture map) and raw DB exception text used to be returned here
    # to any unauthenticated caller — moved to /healthz/detail behind require_key.
    out: dict[str, Any] = {"service": "memory-governor", "version": config.SERVICE_VERSION}
    try:
        p = await db.pool()
        await p.fetchval("SELECT 1")
        out["db"] = "ok"
    except Exception:  # noqa: BLE001
        log.exception("healthz db check failed")
        out["db"] = "error"  # generic — detail logged server-side, never leaked
    return out


@app.get("/healthz/detail", dependencies=[Depends(require_key)])
async def healthz_detail() -> dict[str, Any]:
    out: dict[str, Any] = {"service": "memory-governor", "version": config.SERVICE_VERSION}
    try:
        p = await db.pool()
        await p.fetchval("SELECT 1")
        out["db"] = "ok"
        out["flags"] = {
            name: await db.flag_enabled(name)
            for name in (
                "AGENT_EVENTS_ENABLED",
                "MEMORY_CLASSES_ENABLED",
                "MEMORY_PLANNER_ENABLED",
                "MEMORY_SESSION_SEPARATION_ENABLED",
                "MEMORY_TTL_SWEEPER_ENABLED",
            )
        }
        # Embedding-sync staleness: pending queue depth + last sync. When
        # pending grows, vector-ranked Plane C is silently ranking without the
        # newest docs (or falling back to trigram entirely).
        out["embedding"] = await db.embedding_stats(p)
    except Exception as exc:  # noqa: BLE001
        out["db"] = f"error: {exc}"
    return out


# ---------------------------------------------------------------------------
# /admit
# ---------------------------------------------------------------------------

class AdmitBody(BaseModel):
    content: str = Field(min_length=1, max_length=65000)
    workspace_name: str
    observer: str
    # A5: when the caller omits `observed`, default to the CANONICAL user peer
    # (HONCHO_USER_PEER_ID, fallback "user") — the same deploy-time input every
    # other component resolves. A hardcoded literal here is exactly how identity
    # fragments: a writer that omits the field lands on one peer while a
    # differently-defaulted reader queries another, and recall silently misses.
    # default_factory (not a literal) so the env var is read per request, not
    # frozen at import. See docs/design/memory-system.md §18.
    observed: str = Field(default_factory=config.user_peer_id)
    created_by_peer: str
    session_id: str | None = None
    issue_id: str | None = None
    context: str | None = None
    memory_class: str | None = None
    scope_kind: str | None = None
    scope_id: str | None = None
    source_type: str | None = None
    verification_state: str | None = None
    confidence_score: float | None = None
    half_life_days: float | None = None
    ttl_days: float | None = None
    pin_request: bool = False
    planner_hint: str | None = None


@app.post("/admit", dependencies=[Depends(require_key)])
async def admit(body: AdmitBody) -> dict[str, Any]:
    # aaf-0011 (twin vuln-0011): never honor a caller-asserted trust state on
    # write. A writer could self-declare a memory verification_state='confirmed'
    # / high-trust source_type and jump the earned-trust model (the planner
    # preferentially retrieves confirmed facts). Trust is earned via
    # re-observation or the operator confirm path (/memory/{id}/action confirm),
    # not asserted at write.
    fields = body.model_dump()
    fields["verification_state"] = None  # force admission's default classification
    fields["source_type"] = None  # treat source as a hint to derive, not a trust seed
    result = await admission.admit(admission.AdmitRequest(**fields))
    return result.__dict__


# ---------------------------------------------------------------------------
# /plan-retrieval — the four-plane retrieval planner. Returns a retrieval
# package gated by MEMORY_PLANNER_ENABLED + an injection allowlist; with the
# flag off it returns enabled=false and the caller keeps its native context.
# ---------------------------------------------------------------------------

class PlanBody(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    workspace_name: str
    agent_slug: str
    active_scope_kind: str | None = None
    active_scope_id: str | None = None
    session_id: str | None = None
    task_type: str | None = None
    reasoning_level: str = "medium"


@app.post("/plan-retrieval", dependencies=[Depends(require_key)])
async def plan_retrieval(body: PlanBody) -> dict[str, Any]:
    pkg = await planner.plan_retrieval(planner.RetrievalRequest(**body.model_dump()))
    return pkg.__dict__


# ---------------------------------------------------------------------------
# Admin API (backs the operator memory CLI via the auth-proxy passthrough)
# ---------------------------------------------------------------------------

VALID_ACTIONS = {"pin", "demote", "confirm", "dispute", "supersede", "rm", "reconfirm"}


@app.get("/memory", dependencies=[Depends(require_key)])
async def memory_list(
    workspace_name: str,
    memory_class: str | None = None,
    verification_state: str | None = None,
    scope_kind: str | None = None,
    created_by: str | None = None,
    pin_candidates: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["workspace_name = $1", "deleted_at IS NULL", "memory_class IS NOT NULL"]
    args: list[Any] = [workspace_name]
    for value, column in (
        (memory_class, "memory_class"),
        (verification_state, "verification_state"),
        (scope_kind, "memory_scope_kind"),
        (created_by, "created_by_peer"),
    ):
        if value:
            args.append(value)
            clauses.append(f"{column} = ${len(args)}")
    if pin_candidates:
        clauses.append("(internal_metadata->>'pin_candidate')::boolean = true")

    p = await db.pool()
    rows = await p.fetch(
        f"""SELECT id, left(content, 160) AS snippet, memory_class,
                   memory_scope_kind, memory_scope_id, source_type,
                   verification_state, confidence_score, trust_score,
                   created_by_peer, created_at, last_confirmed_at, expires_at,
                   half_life_days, is_always_on_candidate
            FROM documents
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT {max(1, min(int(limit), 200))}""",
        *args,
    )
    return [dict(r) for r in rows]


@app.get("/memory/audit", dependencies=[Depends(require_key)])
async def memory_audit(limit: int = 100) -> list[dict[str, Any]]:
    p = await db.pool()
    rows = await p.fetch(
        """SELECT ts, actor_peer, event_type, payload FROM agent_events
           WHERE event_type LIKE 'memory_%'
           ORDER BY ts DESC LIMIT $1""",
        max(1, min(limit, 500)),
    )
    return [dict(r) for r in rows]


# Declared before /memory/{doc_id} so the literal "inspector-summary" path
# segment isn't swallowed as a doc_id.
@app.get("/memory/inspector-summary", dependencies=[Depends(require_key)])
async def memory_inspector_summary(workspace_name: str) -> dict[str, Any]:
    """Read-only aggregation for an operator memory-inspector overview.
    `workspace_name` is required — this endpoint never defaults to a
    cross-workspace rollup. No mutation, no new state.

    Aggregates:
      - by_memory_class / by_verification_state / by_source_type: live
        counts from `documents` (deleted rows excluded).
      - embedding: pending-embed queue depth + last sync (db.embedding_stats,
        the same telemetry /healthz already exposes).
      - recent_ranking_modes: counts of the `ranking_mode` the planner stamps
        on each `memory_injected` event (vector vs trigram) over the last 7
        days, so an operator can see Plane C's retrieval quality at a glance.
        Global, not workspace-filtered — `memory_injected` payloads don't
        carry a workspace key (mirrors /memory/audit's global scan).
    """
    p = await db.pool()

    async def _counts(column: str) -> dict[str, int]:
        rows = await p.fetch(
            f"""SELECT {column} AS key, count(*) AS n
                  FROM documents
                 WHERE workspace_name = $1 AND deleted_at IS NULL
                 GROUP BY {column}
                 ORDER BY n DESC""",
            workspace_name,
        )
        return {(r["key"] or "unknown"): r["n"] for r in rows}

    by_memory_class = await _counts("memory_class")
    by_verification_state = await _counts("verification_state")
    by_source_type = await _counts("source_type")

    embedding = await db.embedding_stats(p)

    ranking_rows = await p.fetch(
        """SELECT payload->>'ranking_mode' AS ranking_mode, count(*) AS n
             FROM agent_events
            WHERE event_type = 'memory_injected'
              AND ts > now() - interval '7 days'
            GROUP BY 1
            ORDER BY n DESC"""
    )
    recent_ranking_modes = {(r["ranking_mode"] or "unknown"): r["n"] for r in ranking_rows}

    return {
        "workspace_name": workspace_name,
        "by_memory_class": by_memory_class,
        "by_verification_state": by_verification_state,
        "by_source_type": by_source_type,
        "embedding": embedding,
        "recent_ranking_modes": recent_ranking_modes,
    }


@app.get("/memory/{doc_id}", dependencies=[Depends(require_key)])
async def memory_show(doc_id: str) -> dict[str, Any]:
    # aaf-0007: explicit column projection instead of `SELECT *`. `SELECT *`
    # returned the raw `internal_metadata` jsonb (and the non-serializable
    # `embedding` vector) to the operator surface — an accidental sink for
    # anything a future column stashes there. Project the governed fields the
    # operator CLI actually renders; add new columns here deliberately.
    p = await db.pool()
    row = await p.fetchrow(
        """SELECT id, content, level, observer, observed, workspace_name,
                  session_name, sync_state, memory_class, memory_scope_kind,
                  memory_scope_id, source_type, verification_state,
                  confidence_score, trust_score, created_by_peer, created_at,
                  last_confirmed_at, last_accessed_at, expires_at, half_life_days,
                  reviewed_at, review_note, superseded_at, superseded_by,
                  promotion_source_doc_id, contradiction_count,
                  usage_success_count, is_always_on_candidate, planner_hint,
                  deleted_at
             FROM documents WHERE id = $1 AND deleted_at IS NULL""",
        doc_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    return dict(row)


class ActionBody(BaseModel):
    action: str
    actor: str = "operator"
    note: str | None = None
    demote_to: str | None = None  # for demote
    superseded_by: str | None = None  # for supersede


ACTION_EVENT = {
    "pin": "memory_promote",
    "demote": "memory_demote",
    "confirm": "memory_confirm",
    "dispute": "memory_dispute",
    "supersede": "memory_supersede",
    "rm": "memory_delete",
    "reconfirm": "memory_reconfirm",
}


@app.post("/memory/{doc_id}/action", dependencies=[Depends(require_key)])
async def memory_action(doc_id: str, body: ActionBody) -> dict[str, Any]:
    if body.action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action {body.action!r}")
    p = await db.pool()
    row = await p.fetchrow(
        """SELECT id, memory_class, workspace_name
             FROM documents WHERE id = $1 AND deleted_at IS NULL""",
        doc_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="document not found")

    if body.action == "pin":
        # Operator-only promotion — the auth-proxy route is operator surface;
        # agents never reach /memory/*.
        await p.execute(
            """UPDATE documents
               SET memory_class = 'pinned', verification_state = 'confirmed',
                   reviewed_at = now(), last_confirmed_at = now(),
                   review_note = $2, promotion_source_doc_id = id
               WHERE id = $1""",
            doc_id,
            body.note,
        )
    elif body.action == "demote":
        target = body.demote_to or "durable_fact"
        if target not in ("durable_fact", "user_preference", "task_scoped", "decaying"):
            raise HTTPException(status_code=400, detail=f"cannot demote to {target!r}")
        await p.execute(
            """UPDATE documents
               SET memory_class = $2, reviewed_at = now(), review_note = $3
               WHERE id = $1""",
            doc_id,
            target,
            body.note,
        )
    elif body.action == "confirm":
        await p.execute(
            """UPDATE documents
               SET verification_state = 'confirmed', last_confirmed_at = now(),
                   reviewed_at = now(), review_note = COALESCE($2, review_note)
               WHERE id = $1""",
            doc_id,
            body.note,
        )
    elif body.action == "dispute":
        await p.execute(
            """UPDATE documents
               SET verification_state = 'disputed', reviewed_at = now(),
                   contradiction_count = contradiction_count + 1,
                   review_note = COALESCE($2, review_note)
               WHERE id = $1""",
            doc_id,
            body.note,
        )
    elif body.action == "supersede":
        await p.execute(
            """UPDATE documents
               SET verification_state = 'superseded', superseded_at = now(),
                   reviewed_at = now(), review_note = COALESCE($2, review_note)
               WHERE id = $1""",
            doc_id,
            body.note,
        )
    elif body.action == "rm":
        await p.execute(
            "UPDATE documents SET deleted_at = now(), review_note = COALESCE($2, review_note) WHERE id = $1",
            doc_id,
            body.note,
        )
    elif body.action == "reconfirm":
        # Use-based earned trust: a memory that contributed to a successful
        # outcome earns usage_success_count + a fresh last_confirmed_at. Skip
        # disputed/superseded — a successful run must not resurrect
        # operator-killed memory. The watchdog is the caller (actor=watchdog).
        await p.execute(
            """UPDATE documents
               SET usage_success_count = COALESCE(usage_success_count, 0) + 1,
                   last_confirmed_at = now()
               WHERE id = $1
                 AND verification_state NOT IN ('disputed', 'superseded')""",
            doc_id,
        )

    # aaf-0007: attribute the change to the document's own workspace (resolved
    # from the row, never client-supplied) so the audit event lands in the right
    # tenant timeline and the mutation is provably workspace-scoped. AAF's
    # emit_event carries workspace in the payload (no dedicated column).
    await db.emit_event(
        ACTION_EVENT[body.action],
        body.actor,
        {
            "doc_id": doc_id,
            "action": body.action,
            "note": body.note,
            "memory_class": row["memory_class"],
            "workspace": row["workspace_name"],
        },
    )
    return {"doc_id": doc_id, "action": body.action, "status": "ok"}


# ---------------------------------------------------------------------------
# Session memory (Plane D) — direct CRUD for session-scoped working state
# ---------------------------------------------------------------------------

class SessionMemoryBody(BaseModel):
    workspace_name: str
    session_id: str
    content: str = Field(min_length=1, max_length=8000)
    peer_id: str | None = None
    created_by_peer: str = "unknown"


@app.post("/session-memory", dependencies=[Depends(require_key)])
async def session_memory_write(body: SessionMemoryBody) -> dict[str, Any]:
    if not await db.flag_enabled("MEMORY_SESSION_SEPARATION_ENABLED"):
        return {"status": "disabled", "reason": "MEMORY_SESSION_SEPARATION_ENABLED is off"}
    p = await db.pool()
    row = await p.fetchrow(
        """INSERT INTO session_memory
           (workspace_name, session_id, peer_id, memory_scope_id, content,
            source_type, created_by_peer, expires_at)
           VALUES ($1, $2, $3, $2, $4, 'agent_observed', $5,
                   now() + interval '24 hours')
           RETURNING id""",
        body.workspace_name,
        body.session_id,
        body.peer_id,
        body.content,
        body.created_by_peer,
    )
    return {"status": "ok", "id": str(row["id"])}


@app.get("/session-memory", dependencies=[Depends(require_key)])
async def session_memory_list(workspace_name: str, session_id: str) -> list[dict[str, Any]]:
    p = await db.pool()
    rows = await p.fetch(
        """SELECT id, content, peer_id, created_by_peer, created_at, expires_at
           FROM session_memory
           WHERE workspace_name = $1 AND session_id = $2 AND expires_at > now()
           ORDER BY created_at ASC""",
        workspace_name,
        session_id,
    )
    return [dict(r) for r in rows]


@app.delete("/session-memory", dependencies=[Depends(require_key)])
async def session_memory_close(workspace_name: str, session_id: str) -> dict[str, Any]:
    """Session close: hard-delete Plane D rows."""
    p = await db.pool()
    result = await p.execute(
        "DELETE FROM session_memory WHERE workspace_name = $1 AND session_id = $2",
        workspace_name,
        session_id,
    )
    await db.emit_event(
        "memory_expire",
        "session-close",
        {"workspace": workspace_name, "session_id": session_id, "result": result},
        session_id=None,
    )
    return {"status": "ok", "deleted": result}


# ---------------------------------------------------------------------------
# Daily memory digest — operator-curation flywheel
# ---------------------------------------------------------------------------


@app.get("/digest", dependencies=[Depends(require_key)])
async def memory_digest(window_hours: int = 24) -> dict[str, Any]:
    window_hours = max(1, min(int(window_hours), 168))
    p = await db.pool()
    rows = await p.fetch(
        """SELECT event_type, payload->>'memory_class' AS mc, count(*) AS n
           FROM agent_events
           WHERE ts > now() - make_interval(hours => $1)
             AND event_type LIKE 'memory_%'
           GROUP BY 1, 2""",
        window_hours,
    )
    writes_by_class: dict[str, int] = {}
    ev: dict[str, int] = {}
    for r in rows:
        et = r["event_type"]
        ev[et] = ev.get(et, 0) + r["n"]
        if et == "memory_write" and r["mc"]:
            writes_by_class[r["mc"]] = writes_by_class.get(r["mc"], 0) + r["n"]

    q = await p.fetchrow(
        """SELECT
             count(*) FILTER (
               WHERE (internal_metadata->>'pin_candidate')::boolean = true
                 AND verification_state <> 'confirmed' AND deleted_at IS NULL
             ) AS pin_candidates,
             count(*) FILTER (
               WHERE verification_state = 'needs_review' AND deleted_at IS NULL
             ) AS needs_review
           FROM documents"""
    )

    stats: dict[str, Any] = {
        "window_hours": window_hours,
        "writes_by_class": writes_by_class,
        "classified": ev.get("memory_classify", 0),
        "confirmed": ev.get("memory_confirm", 0),
        "disputed": ev.get("memory_dispute", 0),
        "expired": ev.get("memory_expire", 0),
        "promoted": ev.get("memory_promote", 0),
        "pin_candidates_pending": (q["pin_candidates"] if q else 0) or 0,
        "needs_review": (q["needs_review"] if q else 0) or 0,
    }
    stats["text"] = digest.format_digest(stats)

    # MEMORY_DIGEST_ENABLED: ONLY when the flag is on does the daily digest
    # gain the review-queue listing (a "review_queue" key + the rendered
    # section appended to "text", which digest_post delivers). With the flag
    # off (default, seeded by migration 0010) the response is byte-for-byte
    # what it was before the listing existed.
    if await db.flag_enabled("MEMORY_DIGEST_ENABLED"):
        review = await _memory_digest_stats(mem_digest.DEFAULT_LIMIT)
        review["text"] = mem_digest.format_memory_digest(review)
        stats["review_queue"] = review
        stats["text"] = stats["text"] + "\n\n" + review["text"]

    # ESCALATION_SLA_ENABLED: ONLY when the flag is on does the daily digest
    # gain the escalation ack-SLA section (an "escalation_sla" key + the
    # rendered report appended to "text", which digest_post delivers). With
    # the flag off (default, seeded by migration 0011) the response is
    # byte-for-byte unchanged. Cross-workspace here (workspace=None) — the
    # daily digest is the operator's whole-platform view; breaches and
    # currently-unacked escalations surface with ages.
    if await db.flag_enabled("ESCALATION_SLA_ENABLED"):
        esc = await db.escalation_sla_rollup(
            None, window_days=max(1, -(-window_hours // 24))
        )
        esc["text"] = escalation_sla.format_escalation_report(esc)
        stats["escalation_sla"] = esc
        stats["text"] = stats["text"] + "\n\n" + esc["text"]
    return stats


async def _memory_digest_stats(limit: int) -> dict[str, Any]:
    """Shared rollup for /memory-digest and the flag-gated /digest fold-in:
    pending pin-candidates, needs_review memories, and memories expiring soon
    (db.memory_digest_rollup). Read-only."""
    rollup = await db.memory_digest_rollup()
    return {"limit": limit, **rollup}


@app.get("/memory-digest", dependencies=[Depends(require_key)])
async def memory_digest_endpoint(limit: int = mem_digest.DEFAULT_LIMIT) -> dict[str, Any]:
    """Daily memory review-queue digest. A per-workspace LISTING (not just
    counts, unlike /digest above) of what needs operator action: pending
    pin-candidates, memories the contradiction sweep flagged needs_review, and
    memories expiring soon — each section capped to `limit` (default 10) with
    a "+N more" line so a large backlog never produces a wall of text.
    Read-only; never writes memory state.

    Always available for operator preview regardless of MEMORY_DIGEST_ENABLED;
    the flag only gates whether the listing is folded into the daily /digest
    (and therefore into digest_post's webhook delivery)."""
    limit = mem_digest.clamp_limit(limit)
    stats = await _memory_digest_stats(limit)
    stats["text"] = mem_digest.format_memory_digest(stats)
    return stats


@app.get("/escalation-sla", dependencies=[Depends(require_key)])
async def escalation_sla_endpoint(
    workspace: str | None = None, window_days: int = 7
) -> dict[str, Any]:
    """Escalation SLA auditor: pair escalation_opened/acked/resolved events
    (correlated by payload.escalation_id) plus the approval gate's
    autonomy_decision events (retro-compat ack+resolution; TTL expiry =
    breach + unresolved, always) into per-window ack-latency stats vs the
    SLA. Read-only; the auditor never acts. Always available for operator
    preview regardless of ESCALATION_SLA_ENABLED (mirrors /memory-digest's
    posture) — the flag only gates the fold-in to the daily /digest. Event
    emitters land when the HITL approval seam (apps/paperclip/approval.mjs)
    is wired for real volume; until then this reports the honest zero."""
    window_days = max(1, min(int(window_days), 90))
    stats = await db.escalation_sla_rollup(workspace, window_days)
    stats["text"] = escalation_sla.format_escalation_report(stats)
    return stats


class EscalationEventIn(BaseModel):
    """An escalation-taxonomy event emitted by a wired approval surface (the
    auth-proxy HITL gate today). The payload is rebuilt server-side through
    `escalation_sla.escalation_payload` so the auditor's build-time discipline
    (valid lane/source) is enforced at the choke point, not trusted from the
    caller."""

    event_type: str
    escalation_id: str
    lane: str
    source: str
    workspace: str
    actor_peer: str = "auth-proxy"
    issue_id: str | None = None
    # autonomy_decision carries these; escalation_opened omits them.
    decision: str | None = None
    latency_ms: int | None = None


@app.post("/escalation-event", dependencies=[Depends(require_key)])
async def escalation_event_emit(evt: EscalationEventIn) -> dict[str, Any]:
    """Emit one escalation-taxonomy event onto the `agent_events` spine so the
    v1.7 escalation-SLA auditor can pair it. The single emit endpoint for wired
    approval surfaces; validates the event type + rebuilds the payload through
    the discipline helper (bad lane/source -> 400). `AGENT_EVENTS_ENABLED` off
    makes the write a no-op — the endpoint still returns accepted, matching
    `db.emit_event`'s fail-open, observability-not-control-flow posture."""
    if evt.event_type not in escalation_sla.ROLLUP_EVENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"event_type must be one of {escalation_sla.ROLLUP_EVENT_TYPES}, "
                f"got {evt.event_type!r}"
            ),
        )
    extra: dict[str, Any] = {}
    if evt.decision is not None:
        extra["decision"] = evt.decision
    if evt.latency_ms is not None:
        extra["latency_ms"] = evt.latency_ms
    try:
        payload = escalation_sla.escalation_payload(
            evt.escalation_id,
            lane=evt.lane,
            source=evt.source,
            workspace=evt.workspace,
            **extra,
        )
    except ValueError as exc:  # bad lane/source — discipline helper is strict
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.emit_event(
        event_type=evt.event_type,
        actor_peer=evt.actor_peer,
        payload=payload,
        channel="approval",
        issue_id=evt.issue_id,
    )
    return {"accepted": True, "event_type": evt.event_type, "escalation_id": evt.escalation_id}


# ---------------------------------------------------------------------------
# Skill candidates (automatic repetition detection -> skill autogen, 0008)
# The miner proposes; the operator/curator disposes. The skill-curator job
# lists status='approved' candidates, writes them to the shared skills dir,
# then POSTs action='materialized'. Nothing is auto-injected into an agent.
# ---------------------------------------------------------------------------


@app.get("/skill-candidates", dependencies=[Depends(require_key)])
async def skill_candidates_list(
    status: str = "pending_review", agent_slug: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    clauses = ["status = $1"]
    args: list[Any] = [status]
    if agent_slug:
        args.append(agent_slug)
        clauses.append(f"agent_slug = ${len(args)}")
    p = await db.pool()
    rows = await p.fetch(
        f"""SELECT id, agent_slug, skill_name, left(skill_body, 280) AS body_preview,
                   recurrence, source_doc_ids, status, created_at, reviewed_at
            FROM skill_candidates
            WHERE {' AND '.join(clauses)}
            ORDER BY recurrence DESC, created_at DESC
            LIMIT {max(1, min(int(limit), 200))}""",
        *args,
    )
    return [dict(r) for r in rows]


@app.get("/skill-candidates/{candidate_id}", dependencies=[Depends(require_key)])
async def skill_candidate_show(candidate_id: str) -> dict[str, Any]:
    p = await db.pool()
    row = await p.fetchrow("SELECT * FROM skill_candidates WHERE id = $1", candidate_id)
    if not row:
        raise HTTPException(status_code=404, detail="skill candidate not found")
    return dict(row)


SKILL_CANDIDATE_ACTIONS = {"approve": "approved", "reject": "rejected", "materialized": "materialized"}


class SkillCandidateActionBody(BaseModel):
    action: str
    actor: str = "operator"
    note: str | None = None


@app.post("/skill-candidates/{candidate_id}/action", dependencies=[Depends(require_key)])
async def skill_candidate_action(candidate_id: str, body: SkillCandidateActionBody) -> dict[str, Any]:
    new_status = SKILL_CANDIDATE_ACTIONS.get(body.action)
    if not new_status:
        raise HTTPException(status_code=400, detail=f"unknown action {body.action!r}")
    p = await db.pool()
    row = await p.fetchrow(
        """UPDATE skill_candidates
           SET status = $2, reviewed_at = now(),
               review_note = COALESCE($3, review_note)
           WHERE id = $1
           RETURNING id, agent_slug, skill_name""",
        candidate_id, new_status, body.note,
    )
    if not row:
        raise HTTPException(status_code=404, detail="skill candidate not found")
    await db.emit_event(
        "skill_candidate_reviewed",
        body.actor,
        {
            "candidate_id": candidate_id,
            "action": body.action,
            "agent": row["agent_slug"],
            "skill_name": row["skill_name"],
        },
    )
    return {"candidate_id": candidate_id, "action": body.action, "status": new_status}
