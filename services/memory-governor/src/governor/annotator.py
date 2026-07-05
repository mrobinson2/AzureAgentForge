"""Second-stage classifier for deriver-emitted observations.

The Honcho deriver persists observations natively (it runs as an hourly job).
Instead of patching the deriver — a build-time patch we'd re-risk on every
Honcho upgrade — the governor polls for documents that have no
memory_class yet, classifies them, and UPDATEs their governance columns in
place. Classification lag is bounded by the poll interval (default 30s),
which is noise next to the deriver's own hourly cadence.

Conservative retention mapping for already-persisted docs: an event_only
verdict cannot un-persist someone else's row, so it demotes to decaying with
a short half-life and lets the TTL sweeper age it out.
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import db, llm
from .memory.classifier import MemoryClass, RetentionAction
from .memory.scoring import BASE_SOURCE_TRUST

log = logging.getLogger("governor.annotator")

ANNOTATOR_INTERVAL_S = float(os.environ.get("ANNOTATOR_INTERVAL_S", "30"))
ANNOTATOR_BATCH = int(os.environ.get("ANNOTATOR_BATCH", "20"))
EVENT_ONLY_DEMOTION_HALF_LIFE_DAYS = 3.0
MAX_ANNOTATE_ATTEMPTS = int(os.environ.get("MAX_ANNOTATE_ATTEMPTS", "3"))


async def _bump_attempts(p, doc_id: str) -> int:
    """Increment and return the per-doc annotation attempt counter
    (internal_metadata.annotate_attempts)."""
    row = await p.fetchrow(
        """UPDATE documents
           SET internal_metadata = internal_metadata
             || jsonb_build_object('annotate_attempts',
                  COALESCE((internal_metadata->>'annotate_attempts')::int, 0) + 1)
           WHERE id = $1
           RETURNING (internal_metadata->>'annotate_attempts')::int AS attempts""",
        doc_id,
    )
    return int(row["attempts"]) if row else 0


async def annotate_batch() -> int:
    """Classify up to ANNOTATOR_BATCH unclassified documents. Returns count."""
    if not await db.flag_enabled("MEMORY_CLASSES_ENABLED"):
        return 0
    p = await db.pool()
    rows = await p.fetch(
        """SELECT id, content, observer, workspace_name FROM documents
           WHERE memory_class IS NULL AND deleted_at IS NULL
           ORDER BY created_at DESC
           LIMIT $1""",
        ANNOTATOR_BATCH,
    )
    n = 0
    consecutive_failures = 0
    for r in rows:
        cr = await llm.classify(r["content"])
        # Transport failure is NOT a classification — but it is also not
        # always an outage: content-filter rejections (model-provider policy)
        # surface as router 502s and are PERMANENT for that document. A single
        # poisoned doc retried head-of-line can otherwise stall the queue
        # forever. Policy: per-doc attempt counter; after MAX_ANNOTATE_ATTEMPTS
        # the doc
        # gets the conservative fallback (short-decay + parse_error on record)
        # so the queue moves. Three consecutive failing DOCS = systemic
        # outage -> stop the batch and let the next cycle retry.
        if cr.parse_error and cr.parse_error.startswith("llm transport"):
            consecutive_failures += 1
            attempts = await _bump_attempts(p, r["id"])
            if attempts >= MAX_ANNOTATE_ATTEMPTS:
                log.warning(
                    "doc %s failed classification %d times — applying fallback",
                    r["id"], attempts,
                )
                # fall through and write cr's fallback shape below
            elif consecutive_failures >= 3:
                log.warning("3 consecutive docs failed — classifier looks down, ending batch")
                break
            else:
                log.warning("classifier failed for doc %s (attempt %d) — will retry", r["id"], attempts)
                continue
        else:
            consecutive_failures = 0
        memory_class = cr.memory_class
        half_life = cr.half_life_days
        if cr.retention_action == RetentionAction.PERSIST_DECAYING:
            memory_class = MemoryClass.DECAYING
            half_life = half_life or 14.0
        elif cr.retention_action == RetentionAction.EVENT_ONLY:
            # already persisted by the deriver — age it out, don't delete
            memory_class = MemoryClass.DECAYING
            half_life = EVENT_ONLY_DEMOTION_HALF_LIFE_DAYS
        # ephemeral can't be moved to session_memory retroactively (no
        # session binding on deriver docs) — treat as short decaying
        if memory_class == MemoryClass.EPHEMERAL:
            memory_class = MemoryClass.DECAYING
            half_life = EVENT_ONLY_DEMOTION_HALF_LIFE_DAYS

        await p.execute(
            """UPDATE documents
               SET memory_class = $2, memory_scope_kind = $3, memory_scope_id = $4,
                   source_type = $5, verification_state = $6,
                   confidence_score = $7, trust_score = $8, half_life_days = $9,
                   is_always_on_candidate = $10,
                   internal_metadata = internal_metadata
                     || jsonb_build_object('governed', true,
                                           'annotator', true,
                                           'pin_candidate', $11::boolean)
               WHERE id = $1 AND memory_class IS NULL""",
            r["id"],
            memory_class.value,
            cr.scope_kind,
            cr.scope_id,
            cr.source_type.value,
            cr.verification_state.value,
            cr.confidence,
            BASE_SOURCE_TRUST.get(cr.source_type.value, 0.5),
            half_life,
            cr.is_always_on_candidate,
            cr.is_pinned_candidate,
        )
        await db.emit_event(
            "memory_classify",
            "annotator",
            {
                "doc_id": r["id"],
                "memory_class": memory_class.value,
                "classifier_confidence": cr.confidence,
                "retention_action": cr.retention_action.value,
                "second_stage": True,
                "parse_error": cr.parse_error,
            },
        )
        n += 1
    return n


async def repair_transport_fallbacks() -> int:
    """One-shot startup repair for documents mislabeled by the old
    transport-failure fallback path (decaying/3d written when the LLM was
    merely unreachable). Identified by correlating their memory_classify
    events (parse_error 'llm transport…'); reset to NULL so the loop
    reclassifies them properly. Idempotent: repaired docs no longer match.
    """
    p = await db.pool()
    result = await p.execute(
        """UPDATE documents d
           SET memory_class = NULL, half_life_days = NULL,
               memory_scope_kind = NULL, memory_scope_id = NULL,
               source_type = NULL, verification_state = NULL,
               confidence_score = NULL, trust_score = NULL
           FROM (
             SELECT DISTINCT payload->>'doc_id' AS doc_id FROM agent_events
             WHERE event_type = 'memory_classify'
               AND payload->>'parse_error' LIKE 'llm transport%'
           ) bad
           WHERE d.id = bad.doc_id
             AND d.memory_class = 'decaying'
             AND d.half_life_days = 3
             AND d.internal_metadata->>'annotator' = 'true'"""
    )
    count = int(result.split()[-1]) if result.startswith("UPDATE") else 0
    if count:
        log.info("repaired %d transport-fallback misclassifications", count)
        await db.emit_event(
            "memory_classify",
            "annotator",
            {"repair": "transport_fallback_reset", "count": count},
        )
    return count


async def run_forever() -> None:
    log.info("annotator loop starting (interval %ss)", ANNOTATOR_INTERVAL_S)
    try:
        await repair_transport_fallbacks()
    except Exception:  # noqa: BLE001
        log.exception("startup repair failed — continuing with normal loop")
    while True:
        try:
            n = await annotate_batch()
            if n:
                log.info("annotated %d documents", n)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("annotator batch failed")
        await asyncio.sleep(ANNOTATOR_INTERVAL_S)
