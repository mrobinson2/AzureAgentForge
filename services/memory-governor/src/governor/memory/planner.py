"""Retrieval planner.

Plane A (always-on) + Plane C (governed retrieval) + Plane D (session).
Plane B stays Honcho-native and is NOT duplicated here — the caller keeps
whatever native Honcho context it already injects.

Plane C candidate selection is hybrid: pgvector cosine (over Honcho's
`documents.embedding`, HNSW-indexed) blended with pg_trgm text similarity when
MEMORY_VECTOR_RETRIEVAL_ENABLED is on and the query embeds; otherwise
trigram-only. The blend keeps semantic recall while preserving exact-token
matches and not-yet-embedded (pending) docs. See `_plane_c_candidates`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import config, db
from .scoring import (
    ScoredMemory,
    apply_class_budgets,
    estimate_tokens,
    retrieval_score,
)

log = logging.getLogger("governor.planner")

PLANE_A_TOKEN_CEILING = 500
PLANE_A_MIN_TRUST_STATES = ("confirmed",)

# Failure-lesson injection: bounded, NOT similarity-gated. Lessons are few per
# agent and operationally important, so they ride their own small budget instead
# of competing in Plane C's similarity ranking.
FAILURE_LESSON_MAX = 8
FAILURE_LESSON_TOKEN_CEILING = 600
FAILURE_LESSON_HINT = "failure_lesson"

# Track-record routing: per-agent delegation scorecards, peer-scoped to the
# orchestrator and injected at delegation time like failure lessons (not
# similarity-gated). Written by the watchdog (services/watchdog/scorecards.py).
# Small budget — one short line per agent.
TRACK_RECORD_HINT = "track_record"
TRACK_RECORD_MAX = 14
TRACK_RECORD_TOKEN_CEILING = 700


@dataclass
class RetrievalRequest:
    query: str
    workspace_name: str
    agent_slug: str
    active_scope_kind: str | None = None
    active_scope_id: str | None = None
    session_id: str | None = None
    task_type: str | None = None
    reasoning_level: str = "medium"


@dataclass
class RetrievalPackage:
    enabled: bool
    plane_a: list[dict] = field(default_factory=list)
    plane_c: list[dict] = field(default_factory=list)
    plane_d: list[dict] = field(default_factory=list)
    # Self-improvement loop: peer-scoped agent_observed durable_fact "failure
    # lessons" injected regardless of query similarity so the agent reliably sees
    # the failures it keeps hitting.
    failure_lessons: list[dict] = field(default_factory=list)
    # Track-record routing: per-agent delegation scorecards, injected for the
    # orchestrator at delegation time so routing becomes a learned policy.
    track_records: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    reason: str = ""


async def _plane_a(req: RetrievalRequest) -> list[dict]:
    """Always-on: pinned + confirmed always-on candidates. Not vector-ranked;
    newest-confirmed first under a hard token ceiling."""
    p = await db.pool()
    rows = await p.fetch(
        """SELECT id, content, memory_class FROM documents
           WHERE workspace_name = $1
             AND deleted_at IS NULL
             AND superseded_at IS NULL
             AND (memory_class = 'pinned'
                  OR (is_always_on_candidate = true
                      AND verification_state = ANY($2::text[])))
             AND (memory_scope_kind IS NULL
                  OR memory_scope_kind IN ('workspace')
                  OR (memory_scope_kind = 'peer' AND memory_scope_id = $3))
           ORDER BY (memory_class = 'pinned') DESC, last_confirmed_at DESC NULLS LAST""",
        req.workspace_name,
        list(PLANE_A_MIN_TRUST_STATES),
        req.agent_slug,
    )
    out, used = [], 0
    for r in rows:
        cost = estimate_tokens(r["content"])
        if used + cost > PLANE_A_TOKEN_CEILING:
            break
        used += cost
        out.append({"doc_id": r["id"], "content": r["content"], "memory_class": r["memory_class"]})
    return out


# Plane C SQL — shared columns/filters, with a trigram-only and a hybrid variant.
_PLANE_C_COLUMNS = """id, content, memory_class, memory_scope_kind, memory_scope_id,
                  source_type, verification_state, created_at, half_life_days,
                  last_confirmed_at, usage_success_count, contradiction_count"""

_PLANE_C_FILTERS = """workspace_name = $1
             AND deleted_at IS NULL
             AND superseded_at IS NULL
             AND memory_class IN ('durable_fact','user_preference','task_scoped','decaying')
             AND verification_state NOT IN ('disputed','superseded','needs_review')
             AND (expires_at IS NULL OR expires_at > now())"""

_TRIGRAM_SQL = f"""SELECT {_PLANE_C_COLUMNS}, similarity(content, $2) AS sim
           FROM documents
           WHERE {_PLANE_C_FILTERS}
             AND similarity(content, $2) > 0.05
           ORDER BY sim DESC
           LIMIT 100"""

# $1 workspace, $2 query vector (::vector), $3 query text, $4 cosine threshold,
# $5 vector weight, $6 trigram weight. The blended value is returned as `sim`.
_HYBRID_SQL = f"""SELECT {_PLANE_C_COLUMNS},
                  ($5 * COALESCE(1 - (embedding <=> $2::vector), 0)
                   + $6 * similarity(content, $3)) AS sim
           FROM documents
           WHERE {_PLANE_C_FILTERS}
             AND ((embedding IS NOT NULL AND (1 - (embedding <=> $2::vector)) > $4)
                  OR similarity(content, $3) > 0.05)
           ORDER BY sim DESC
           LIMIT 100"""

VECTOR_WEIGHT = 0.7
TRIGRAM_WEIGHT = 0.3
COSINE_CANDIDATE_THRESHOLD = 0.30


def _vec_literal(vec: list[float]) -> str:
    """pgvector text literal '[f,f,...]' for an ``::vector`` cast — asyncpg sends
    it as text and Postgres casts."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def _plane_c_candidates(req: RetrievalRequest) -> list[dict]:
    """Candidate set: same-workspace, retrievable states, scope-eligible. Hybrid
    (pgvector cosine blended with trigram) when MEMORY_VECTOR_RETRIEVAL_ENABLED is
    on AND the query embeds; otherwise trigram-only. Any vector-path failure (no
    embedding, dim mismatch, missing extension) degrades to trigram — the vector
    is a ranker, never a gate. The blended value is returned as `sim`, consumed
    unchanged by retrieval_score()."""
    p = await db.pool()
    query_vec = None
    if await db.flag_enabled("MEMORY_VECTOR_RETRIEVAL_ENABLED"):
        from .. import llm

        query_vec = await llm.embed(req.query)
    if query_vec:
        try:
            rows = await p.fetch(
                _HYBRID_SQL,
                req.workspace_name,
                _vec_literal(query_vec),
                req.query,
                COSINE_CANDIDATE_THRESHOLD,
                VECTOR_WEIGHT,
                TRIGRAM_WEIGHT,
            )
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001 — vector is a ranker, not a gate
            log.exception("hybrid Plane C retrieval failed; falling back to trigram")
    rows = await p.fetch(_TRIGRAM_SQL, req.workspace_name, req.query)
    return [dict(r) for r in rows]


def _in_scope(row: dict, req: RetrievalRequest) -> bool:
    kind, sid = row.get("memory_scope_kind"), row.get("memory_scope_id")
    if row["memory_class"] == "task_scoped":
        return (
            kind == "task"
            and sid is not None
            and req.active_scope_kind == "task"
            and sid == req.active_scope_id
        )
    if kind in (None, "workspace"):
        return True
    if kind == "peer":
        return sid == req.agent_slug
    if kind == "task":
        return req.active_scope_kind == "task" and sid == req.active_scope_id
    return False


def _diversity_penalties(rows: list[dict]) -> dict[str, float]:
    """Diversity guard: crude shingle-overlap clustering; later members of a
    near-duplicate cluster get suppressed."""
    penalties: dict[str, float] = {}
    seen: list[tuple[str, set]] = []
    for r in rows:
        sh = set(r["content"].lower().split())
        penalty = 1.0
        for _, other in seen:
            inter = len(sh & other)
            union = len(sh | other) or 1
            if inter / union > 0.6:
                penalty = 0.2
                break
        penalties[r["id"]] = penalty
        seen.append((r["id"], sh))
    return penalties


async def _plane_d(req: RetrievalRequest) -> list[dict]:
    if not req.session_id:
        return []
    if not await db.flag_enabled("MEMORY_SESSION_SEPARATION_ENABLED"):
        return []
    p = await db.pool()
    rows = await p.fetch(
        """SELECT id, content FROM session_memory
           WHERE workspace_name = $1 AND session_id = $2 AND expires_at > now()
           ORDER BY created_at ASC""",
        req.workspace_name,
        req.session_id,
    )
    out, used = [], 0
    for r in rows:
        cost = estimate_tokens(r["content"])
        if used + cost > 300:  # ephemeral ceiling
            break
        used += cost
        out.append({"id": str(r["id"]), "content": r["content"]})
    return out


def _select_failure_lessons(
    rows: list[dict],
    *,
    exclude_ids: set[str],
    max_count: int = FAILURE_LESSON_MAX,
    token_ceiling: int = FAILURE_LESSON_TOKEN_CEILING,
) -> list[dict]:
    """Pure: bound + dedup the failure-lesson rows the SQL already ordered
    (confirmed first, newest first). Excludes ids already injected by another
    plane so a lesson is never shown twice in one turn."""
    out: list[dict] = []
    used = 0
    for r in rows:
        if r["id"] in exclude_ids:
            continue
        cost = estimate_tokens(r["content"])
        if used + cost > token_ceiling:
            break
        used += cost
        out.append(
            {
                "doc_id": r["id"],
                "content": r["content"],
                "memory_class": r["memory_class"],
                "verification_state": r.get("verification_state"),
            }
        )
        if len(out) >= max_count:
            break
    return out


async def _failure_lessons(req: RetrievalRequest, *, exclude_ids: set[str]) -> list[dict]:
    """Peer-scoped agent_observed durable_fact lessons for this agent.

    NOT similarity-gated: every still-valid lesson for the agent is eligible,
    bounded only by count + token ceiling. Disputed/superseded lessons are
    excluded — the operator's `dispute` action is the kill switch."""
    p = await db.pool()
    rows = await p.fetch(
        """SELECT id, content, memory_class, verification_state
           FROM documents
           WHERE workspace_name = $1
             AND deleted_at IS NULL
             AND superseded_at IS NULL
             AND memory_class = 'durable_fact'
             AND memory_scope_kind = 'peer'
             AND memory_scope_id = $2
             AND planner_hint = $3
             AND verification_state NOT IN ('disputed','superseded')
             AND (expires_at IS NULL OR expires_at > now())
           ORDER BY (verification_state = 'confirmed') DESC, created_at DESC
           LIMIT 50""",
        req.workspace_name,
        req.agent_slug,
        FAILURE_LESSON_HINT,
    )
    return _select_failure_lessons([dict(r) for r in rows], exclude_ids=exclude_ids)


async def _track_records(req: RetrievalRequest, *, exclude_ids: set[str]) -> list[dict]:
    """Peer-scoped delegation scorecards for this agent (track-record routing).
    Same shape as failure lessons — peer-scoped agent_observed
    durable_facts, NOT similarity-gated — only the planner_hint differs. Empty
    for agents that aren't a delegation hub (no scorecards are scoped to them).
    Disputed/superseded scorecards are excluded (operator kill switch)."""
    p = await db.pool()
    rows = await p.fetch(
        """SELECT id, content, memory_class, verification_state
           FROM documents
           WHERE workspace_name = $1
             AND deleted_at IS NULL
             AND superseded_at IS NULL
             AND memory_class = 'durable_fact'
             AND memory_scope_kind = 'peer'
             AND memory_scope_id = $2
             AND planner_hint = $3
             AND verification_state NOT IN ('disputed','superseded')
             AND (expires_at IS NULL OR expires_at > now())
           ORDER BY (verification_state = 'confirmed') DESC, created_at DESC
           LIMIT 50""",
        req.workspace_name,
        req.agent_slug,
        TRACK_RECORD_HINT,
    )
    return _select_failure_lessons(
        [dict(r) for r in rows], exclude_ids=exclude_ids,
        max_count=TRACK_RECORD_MAX, token_ceiling=TRACK_RECORD_TOKEN_CEILING,
    )


def _injected_doc_ids(pkg: "RetrievalPackage") -> list[str]:
    """Durable doc ids injected this turn — Plane A + Plane C + failure lessons,
    order-preserving and de-duped. Plane D is ephemeral session memory and is not
    eligible for earned trust (a memory that contributed to a successful run
    earns usage_success_count via a downstream success signal)."""
    seen: set[str] = set()
    out: list[str] = []
    for plane in (pkg.plane_a, pkg.plane_c, pkg.failure_lessons, pkg.track_records):
        for m in plane:
            doc_id = m.get("doc_id")
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                out.append(doc_id)
    return out


async def plan_retrieval(req: RetrievalRequest) -> RetrievalPackage:
    if not await db.flag_enabled("MEMORY_PLANNER_ENABLED"):
        return RetrievalPackage(enabled=False, reason="MEMORY_PLANNER_ENABLED is off")
    if (
        config.PLANNER_AGENT_ALLOWLIST
        and req.agent_slug not in config.PLANNER_AGENT_ALLOWLIST
    ):
        return RetrievalPackage(
            enabled=False, reason=f"agent {req.agent_slug} not in planner allowlist"
        )

    plane_a = await _plane_a(req)
    candidates = await _plane_c_candidates(req)

    # readClasses filter: agents only retrieve classes their profile allows.
    from .. import profiles

    readable = set(profiles.readable_classes(req.agent_slug))
    candidates = [r for r in candidates if r["memory_class"] in readable]

    penalties = _diversity_penalties(candidates)

    plane_a_ids = {m["doc_id"] for m in plane_a}
    scored = [
        ScoredMemory(
            doc_id=r["id"],
            content=r["content"],
            memory_class=r["memory_class"],
            semantic_similarity=float(r["sim"]),
            trust=0.0,
            score=retrieval_score(
                semantic_similarity=float(r["sim"]),
                memory_class=r["memory_class"],
                in_scope=_in_scope(r, req),
                created_at=r["created_at"],
                half_life_days=float(r["half_life_days"]) if r["half_life_days"] else None,
                source_type=r["source_type"],
                verification_state=r["verification_state"],
                last_confirmed_at=r["last_confirmed_at"],
                usage_success_count=r["usage_success_count"] or 0,
                contradiction_count=r["contradiction_count"] or 0,
                diversity_penalty=penalties.get(r["id"], 1.0),
            ),
            metadata={},
        )
        for r in candidates
        if r["id"] not in plane_a_ids  # dedup across planes
    ]
    selected = apply_class_budgets(scored)

    plane_d = await _plane_d(req)

    # failure-lesson injection — peer-scoped, NOT similarity-gated, deduped
    # against anything Plane A / Plane C already chose this turn.
    failure_lessons: list[dict] = []
    track_records: list[dict] = []
    if "durable_fact" in readable:
        exclude = plane_a_ids | {m.doc_id for m in selected}
        failure_lessons = await _failure_lessons(req, exclude_ids=exclude)
        # track-record routing: scorecards for whatever this agent delegates
        # to, deduped against everything already chosen this turn.
        track_records = await _track_records(
            req, exclude_ids=exclude | {m["doc_id"] for m in failure_lessons}
        )

    pkg = RetrievalPackage(
        enabled=True,
        plane_a=plane_a,
        plane_c=[
            {
                "doc_id": m.doc_id,
                "content": m.content,
                "memory_class": m.memory_class,
                "score": round(m.score, 4),
            }
            for m in selected
        ],
        plane_d=plane_d,
        failure_lessons=failure_lessons,
        track_records=track_records,
    )
    pkg.total_tokens = sum(
        estimate_tokens(m["content"])
        for plane in (pkg.plane_a, pkg.plane_c, pkg.plane_d, pkg.failure_lessons, pkg.track_records)
        for m in plane
    )

    # analytics only: access does NOT extend survival
    try:
        ids = [m["doc_id"] for m in pkg.plane_c]
        if ids:
            p = await db.pool()
            await p.execute(
                "UPDATE documents SET last_accessed_at = now() WHERE id = ANY($1::text[])",
                ids,
            )
    except Exception:  # noqa: BLE001
        log.exception("last_accessed_at update failed (analytics only)")

    # Record which durable memories were injected this turn, keyed to the active
    # task, so a downstream success signal (the watchdog) can credit them (earned
    # trust via successful use). Telemetry only; never blocks retrieval.
    try:
        injected = _injected_doc_ids(pkg)
        if injected:
            await db.emit_event(
                "memory_injected",
                req.agent_slug,
                {"doc_ids": injected, "count": len(injected), "query": req.query[:120]},
                session_id=req.session_id,
                issue_id=req.active_scope_id if req.active_scope_kind == "task" else None,
            )
    except Exception:  # noqa: BLE001
        log.exception("memory_injected emit failed (telemetry only)")

    return pkg
