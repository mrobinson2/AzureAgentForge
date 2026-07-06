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

import logging
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config, db, digest
from .memory import admission, planner

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("governor.main")

app = FastAPI(title="memory-governor", version=config.SERVICE_VERSION)


async def require_key(x_governor_key: str | None = Header(default=None)) -> None:
    if config.GOVERNOR_API_KEY and x_governor_key != config.GOVERNOR_API_KEY:
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
    observed: str = "user"
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
    result = await admission.admit(admission.AdmitRequest(**body.model_dump()))
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


@app.get("/memory/{doc_id}", dependencies=[Depends(require_key)])
async def memory_show(doc_id: str) -> dict[str, Any]:
    p = await db.pool()
    row = await p.fetchrow(
        "SELECT * FROM documents WHERE id = $1 AND deleted_at IS NULL", doc_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    d = dict(row)
    d.pop("embedding", None)  # not JSON-serializable, not useful to operators
    return d


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
        "SELECT id, memory_class FROM documents WHERE id = $1 AND deleted_at IS NULL",
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

    await db.emit_event(
        ACTION_EVENT[body.action],
        body.actor,
        {"doc_id": doc_id, "action": body.action, "note": body.note},
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
    return stats


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
