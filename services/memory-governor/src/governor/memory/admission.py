"""Admission pipeline:

    observe -> classify -> validate -> deduplicate -> retention decision
            -> persist | persist_decaying | event_only

Writes go DIRECTLY to Honcho's Postgres: documents rows are inserted with
sync_state='pending' so Honcho's own vector-sync worker generates the
embedding — Honcho keeps embedding ownership without API coupling.
Ephemeral memories route to the separate session_memory table (Plane D).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config, db, llm
from .classifier import (
    ClassificationResult,
    MemoryClass,
    RetentionAction,
    SourceType,
    VerificationState,
    compute_retention_action,
)

log = logging.getLogger("governor.admission")

_NANOID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"


def nanoid(size: int = 21) -> str:
    """honcho-compatible nanoid (documents.id CHECK: 21 chars, [A-Za-z0-9_-])."""
    return "".join(secrets.choice(_NANOID_ALPHABET) for _ in range(size))


SESSION_MEMORY_TTL_HOURS = 24
TASK_SCOPE_GRACE_DAYS = 14


@dataclass
class AdmitRequest:
    content: str
    workspace_name: str
    observer: str  # peer that holds the memory (usually the agent slug)
    observed: str  # peer the memory is about (usually 'user' or the agent)
    created_by_peer: str
    session_id: str | None = None
    issue_id: str | None = None
    context: str | None = None
    # explicit intent — classifier fills whatever is omitted
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


@dataclass
class AdmitResult:
    status: str  # admitted | event_only | duplicate | disabled | rejected
    retention_action: str | None = None
    memory_class: str | None = None
    doc_id: str | None = None
    confidence: float | None = None
    reason: str = ""
    reconfirmed: bool = False  # a duplicate that re-confirmed an existing memory


def _explicit_classification(req: AdmitRequest) -> ClassificationResult | None:
    """Agent supplied an explicit class — honor it but still apply safety
    rules: never pinned, thresholds still computed."""
    if not req.memory_class:
        return None
    try:
        mc = MemoryClass(req.memory_class)
    except ValueError:
        return None
    pin_candidate = req.pin_request
    if mc == MemoryClass.PINNED:
        # pin_request requires privilege and still does not auto-pin
        mc = MemoryClass.DURABLE_FACT
        pin_candidate = True
    confidence = req.confidence_score if req.confidence_score is not None else 0.85
    confidence = max(0.0, min(1.0, confidence))
    src = SourceType(req.source_type) if req.source_type in {s.value for s in SourceType} else SourceType.AGENT_OBSERVED
    ver = (
        VerificationState(req.verification_state)
        if req.verification_state in {v.value for v in VerificationState}
        else VerificationState.UNVERIFIED
    )
    return ClassificationResult(
        memory_class=mc,
        retention_action=compute_retention_action(mc, confidence),
        confidence=confidence,
        source_type=src,
        verification_state=ver,
        is_pinned_candidate=pin_candidate,
        half_life_days=req.half_life_days,
        scope_kind=req.scope_kind,
        scope_id=req.scope_id,
        reason="explicit agent intent",
    )


def _validate_scope(cr: ClassificationResult, req: AdmitRequest) -> str | None:
    """Class/scope enforcement. Returns a rejection reason or None."""
    scope_kind = cr.scope_kind or req.scope_kind
    scope_id = cr.scope_id or req.scope_id
    if cr.memory_class == MemoryClass.TASK_SCOPED and not (scope_kind and scope_id):
        return "task_scoped requires scope_kind and scope_id"
    if cr.memory_class == MemoryClass.EPHEMERAL and not (req.session_id or scope_id):
        return "ephemeral requires a session"
    return None


async def _find_duplicate(req: AdmitRequest, cr: ClassificationResult) -> str | None:
    """Dedup guard: returns the id of the closest recent same-class
    near-duplicate in the same workspace (or None). A hit blocks persistence of
    a new doc; the caller reconfirms the matched memory instead. Requires the
    pg_trgm extension (see infrastructure/migrations)."""
    try:
        p = await db.pool()
        row = await p.fetchrow(
            """SELECT id FROM documents
               WHERE workspace_name = $1
                 AND memory_class = $2
                 AND deleted_at IS NULL
                 AND created_at > now() - make_interval(days => $3)
                 AND similarity(content, $4) > $5
               ORDER BY similarity(content, $4) DESC
               LIMIT 1""",
            req.workspace_name,
            cr.memory_class.value,
            config.DEDUP_LOOKBACK_DAYS,
            req.content,
            config.DEDUP_SIMILARITY_THRESHOLD,
        )
        return row["id"] if row else None
    except Exception:  # noqa: BLE001 — dedup is a guard, not a gate
        log.exception("dedup check failed; admitting without dedup")
        return None


def _command_rowcount(command_tag: str) -> int:
    """asyncpg returns a command tag like 'UPDATE 1'; extract the row count."""
    try:
        return int(command_tag.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0


async def _reconfirm_document(doc_id: str) -> bool:
    """Re-observation reconfirm: a near-duplicate of an existing memory is
    corroboration, so that memory earns trust. Bumps usage_success_count (feeds
    usage_success_factor) and last_confirmed_at (feeds confirmation_factor).
    Disputed/superseded docs are skipped — a re-observation must never resurrect
    operator-killed memory. Returns True iff a row updated.

    This is the self-contained re-observation signal. The use-based signal (a
    memory injected into a successful run earns trust) is a separate follow-up
    needing injection tracking + an outcome hook."""
    try:
        p = await db.pool()
        tag = await p.execute(
            """UPDATE documents
               SET usage_success_count = COALESCE(usage_success_count, 0) + 1,
                   last_confirmed_at = now()
               WHERE id = $1
                 AND deleted_at IS NULL
                 AND COALESCE(verification_state, 'unverified')
                       NOT IN ('disputed', 'superseded')""",
            doc_id,
        )
        return _command_rowcount(tag) > 0
    except Exception:  # noqa: BLE001 — earned-trust is best-effort, never a gate
        log.exception("reconfirm of %s failed", doc_id)
        return False


async def _ensure_collection(req: AdmitRequest) -> None:
    p = await db.pool()
    await p.execute(
        """INSERT INTO collections (id, observer, observed, workspace_name)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (observer, observed, workspace_name) DO NOTHING""",
        nanoid(),
        req.observer,
        req.observed,
        req.workspace_name,
    )


async def _write_document(req: AdmitRequest, cr: ClassificationResult) -> str:
    final_class = (
        MemoryClass.DECAYING
        if cr.retention_action == RetentionAction.PERSIST_DECAYING
        else cr.memory_class
    )
    half_life = cr.half_life_days
    if final_class == MemoryClass.DECAYING and not half_life:
        half_life = 14.0
    expires_at = None
    if req.ttl_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=req.ttl_days)

    from .scoring import BASE_SOURCE_TRUST

    doc_id = nanoid()
    await _ensure_collection(req)
    p = await db.pool()
    await p.execute(
        """INSERT INTO documents
           (id, content, level, observer, observed, workspace_name, session_name,
            sync_state, memory_class, memory_scope_kind, memory_scope_id,
            source_type, verification_state, confidence_score, trust_score,
            half_life_days, expires_at, created_by_peer, is_always_on_candidate,
            planner_hint, internal_metadata)
           VALUES ($1, $2, 'explicit', $3, $4, $5, $6, 'pending', $7, $8, $9,
                   $10, $11, $12, $13, $14, $15, $16, $17, $18,
                   jsonb_build_object('governed', true, 'pin_candidate', $19::boolean))""",
        doc_id,
        req.content,
        req.observer,
        req.observed,
        req.workspace_name,
        req.session_id,
        final_class.value,
        cr.scope_kind or req.scope_kind,
        cr.scope_id or req.scope_id,
        cr.source_type.value,
        cr.verification_state.value,
        cr.confidence,
        BASE_SOURCE_TRUST.get(cr.source_type.value, 0.5),
        half_life,
        expires_at,
        req.created_by_peer,
        cr.is_always_on_candidate,
        req.planner_hint,
        cr.is_pinned_candidate,
    )
    return doc_id


async def _write_session_memory(req: AdmitRequest, cr: ClassificationResult) -> str:
    p = await db.pool()
    row = await p.fetchrow(
        """INSERT INTO session_memory
           (workspace_name, session_id, peer_id, memory_scope_id, content,
            source_type, confidence_score, created_by_peer, expires_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                   now() + make_interval(hours => $9))
           RETURNING id""",
        req.workspace_name,
        req.session_id or cr.scope_id or "unknown",
        req.observer,
        req.session_id or cr.scope_id or "unknown",
        req.content,
        cr.source_type.value,
        cr.confidence,
        req.created_by_peer,
        SESSION_MEMORY_TTL_HOURS,
    )
    return str(row["id"])


async def admit(req: AdmitRequest) -> AdmitResult:
    """The pipeline. Flag-off returns 'disabled' so callers fall back to the
    legacy (ungoverned) write path — flag-off means zero behavior change."""
    if not await db.flag_enabled("MEMORY_CLASSES_ENABLED"):
        return AdmitResult(status="disabled", reason="MEMORY_CLASSES_ENABLED is off")

    await db.emit_event(
        "memory_candidate",
        req.created_by_peer,
        {"snippet": req.content[:200], "workspace": req.workspace_name},
        session_id=req.session_id,
        issue_id=req.issue_id,
    )

    cr = _explicit_classification(req) or await llm.classify(req.content, req.context)

    await db.emit_event(
        "memory_classify",
        req.created_by_peer,
        {
            "memory_class": cr.memory_class.value,
            "classifier_confidence": cr.confidence,
            "retention_action": cr.retention_action.value,
            "reason": cr.reason,
            "parse_error": cr.parse_error,
        },
        session_id=req.session_id,
        issue_id=req.issue_id,
    )

    rejection = _validate_scope(cr, req)
    if rejection:
        return AdmitResult(
            status="rejected",
            memory_class=cr.memory_class.value,
            confidence=cr.confidence,
            reason=rejection,
        )

    # memoryProfile enforcement: write authority is not trust authority.
    # Checked against the FINAL class (post any decaying demotion).
    from .. import profiles

    final_class = (
        MemoryClass.DECAYING.value
        if cr.retention_action == RetentionAction.PERSIST_DECAYING
        else cr.memory_class.value
    )
    if not profiles.can_write(req.created_by_peer, final_class):
        await db.emit_event(
            "memory_candidate",
            req.created_by_peer,
            {
                "outcome": "write_denied",
                "memory_class": final_class,
                "snippet": req.content[:120],
            },
            session_id=req.session_id,
        )
        return AdmitResult(
            status="event_only",
            retention_action="event_only",
            memory_class=final_class,
            confidence=cr.confidence,
            reason=f"writer {req.created_by_peer!r} lacks write authority for {final_class}",
        )

    if cr.retention_action == RetentionAction.EVENT_ONLY:
        return AdmitResult(
            status="event_only",
            retention_action="event_only",
            memory_class=cr.memory_class.value,
            confidence=cr.confidence,
            reason=cr.reason or "below admission threshold",
        )

    # Plane D routing: ephemeral never touches the durable documents table.
    if cr.memory_class == MemoryClass.EPHEMERAL:
        if await db.flag_enabled("MEMORY_SESSION_SEPARATION_ENABLED"):
            mem_id = await _write_session_memory(req, cr)
            await db.emit_event(
                "memory_write",
                req.created_by_peer,
                {"store": "session_memory", "id": mem_id, "memory_class": "ephemeral"},
                session_id=req.session_id,
            )
            return AdmitResult(
                status="admitted",
                retention_action="persist",
                memory_class="ephemeral",
                doc_id=mem_id,
                confidence=cr.confidence,
            )
        return AdmitResult(
            status="event_only",
            retention_action="event_only",
            memory_class="ephemeral",
            confidence=cr.confidence,
            reason="MEMORY_SESSION_SEPARATION_ENABLED is off — ephemeral not persisted",
        )

    dup_id = await _find_duplicate(req, cr)
    if dup_id:
        # Re-observation = corroboration: reconfirm the matched memory so it
        # earns trust, rather than silently dropping the duplicate.
        reconfirmed = await _reconfirm_document(dup_id)
        if reconfirmed:
            await db.emit_event(
                "memory_reconfirm",
                req.created_by_peer,
                {
                    "doc_id": dup_id,
                    "trigger": "re-observation",
                    "memory_class": cr.memory_class.value,
                    "snippet": req.content[:120],
                },
                session_id=req.session_id,
                issue_id=req.issue_id,
            )
        else:
            await db.emit_event(
                "memory_candidate",
                req.created_by_peer,
                {"outcome": "duplicate", "snippet": req.content[:120]},
                session_id=req.session_id,
            )
        return AdmitResult(
            status="duplicate",
            memory_class=cr.memory_class.value,
            doc_id=dup_id,
            confidence=cr.confidence,
            reconfirmed=reconfirmed,
            reason=(
                "near-duplicate — reconfirmed existing memory"
                if reconfirmed
                else "near-duplicate of existing memory"
            ),
        )

    doc_id = await _write_document(req, cr)
    await db.emit_event(
        "memory_write",
        req.created_by_peer,
        {
            "doc_id": doc_id,
            "memory_class": cr.memory_class.value,
            "content_snippet": req.content[:200],
            "source_type": cr.source_type.value,
            "verification_state": cr.verification_state.value,
            "confidence_score": cr.confidence,
            "retention_action": cr.retention_action.value,
            "pin_candidate": cr.is_pinned_candidate,
        },
        session_id=req.session_id,
        issue_id=req.issue_id,
    )
    return AdmitResult(
        status="admitted",
        retention_action=cr.retention_action.value,
        memory_class=(
            MemoryClass.DECAYING.value
            if cr.retention_action == RetentionAction.PERSIST_DECAYING
            else cr.memory_class.value
        ),
        doc_id=doc_id,
        confidence=cr.confidence,
    )
