"""Contradiction detection sweep.

A nightly-cadence background loop (mirrors scope_watcher) that finds
topically-similar durable memory pairs in the SAME scope, asks the classifier
tier whether they conflict (the LLM *suggests*, the operator *finalizes*
high-impact resolution), and flags the lower-trust member `needs_review`, which
the planner already excludes from injection. It NEVER auto-supersedes;
supersede/scope_refine are surfaced as a suggestion in the review_note + event.

Gated by MEMORY_CONTRADICTION_SWEEP_ENABLED — the loop can run unconditionally;
with the flag off, sweep_once() no-ops. Uses the in-pod router sidecar for the
LLM judge (hence a service loop, not a standalone Job).
"""

from __future__ import annotations

import asyncio
import logging
import os

from . import db, llm

log = logging.getLogger("governor.contradiction")

CONTRADICTION_INTERVAL_S = float(os.environ.get("CONTRADICTION_INTERVAL_S", "21600"))  # 6h
MAX_PAIRS_PER_SWEEP = int(os.environ.get("CONTRADICTION_MAX_PAIRS", "25"))
SIM_LOW = float(os.environ.get("CONTRADICTION_SIM_LOW", "0.4"))
SIM_HIGH = float(os.environ.get("CONTRADICTION_SIM_HIGH", "0.92"))

# Outcomes that mean "real conflict → flag the loser for review". coexist/none
# are left untouched.
FLAGGING_OUTCOMES = {"supersede", "scope_refine", "needs_review"}

# Topically-similar (but not duplicate), same-scope, active durable pairs.
_CANDIDATE_PAIRS_SQL = """
SELECT a.id AS a_id, a.content AS a_content,
       COALESCE(a.trust_score, 0.5) AS a_trust, a.created_at AS a_created,
       b.id AS b_id, b.content AS b_content,
       COALESCE(b.trust_score, 0.5) AS b_trust, b.created_at AS b_created
FROM documents a
JOIN documents b
  ON a.id < b.id
 AND a.workspace_name = b.workspace_name
 AND COALESCE(a.memory_scope_kind, '') = COALESCE(b.memory_scope_kind, '')
 AND COALESCE(a.memory_scope_id, '')   = COALESCE(b.memory_scope_id, '')
 AND a.content % b.content
 AND similarity(a.content, b.content) BETWEEN $2 AND $3
WHERE a.workspace_name = $1
  AND a.memory_class IN ('durable_fact', 'user_preference')
  AND b.memory_class IN ('durable_fact', 'user_preference')
  AND a.deleted_at IS NULL AND b.deleted_at IS NULL
  AND a.verification_state NOT IN ('disputed', 'superseded', 'needs_review')
  AND b.verification_state NOT IN ('disputed', 'superseded', 'needs_review')
ORDER BY similarity(a.content, b.content) DESC
LIMIT $4
"""


def _pick_loser(pair: dict) -> tuple[str, str]:
    """(loser_id, keeper_id): flag the lower-trust member; on a trust tie, flag
    the older one (newer info is more likely current)."""
    a_trust, b_trust = float(pair["a_trust"]), float(pair["b_trust"])
    if a_trust != b_trust:
        return (pair["a_id"], pair["b_id"]) if a_trust < b_trust else (pair["b_id"], pair["a_id"])
    return (pair["a_id"], pair["b_id"]) if pair["a_created"] <= pair["b_created"] else (pair["b_id"], pair["a_id"])


async def _flag_needs_review(loser_id: str, keeper_id: str, outcome: str) -> None:
    p = await db.pool()
    await p.execute(
        """UPDATE documents
           SET verification_state = 'needs_review', reviewed_at = now(),
               contradiction_count = COALESCE(contradiction_count, 0) + 1,
               review_note = $2
           WHERE id = $1 AND verification_state NOT IN ('disputed', 'superseded')""",
        loser_id,
        f"contradiction sweep: conflicts with {keeper_id} (suggested: {outcome})",
    )
    await db.emit_event(
        "memory_needs_review",
        "contradiction-sweep",
        {"doc_id": loser_id, "conflicts_with": keeper_id, "suggested_outcome": outcome},
    )


async def sweep_once(workspace: str | None = None) -> int:
    """One pass. Returns the number of memories flagged needs_review."""
    if not await db.flag_enabled("MEMORY_CONTRADICTION_SWEEP_ENABLED"):
        return 0
    ws = workspace or os.environ.get("GOVERNOR_WORKSPACE") or os.environ.get("HONCHO_APP_ID")
    if not ws:
        return 0
    p = await db.pool()
    pairs = await p.fetch(_CANDIDATE_PAIRS_SQL, ws, SIM_LOW, SIM_HIGH, MAX_PAIRS_PER_SWEEP)
    flagged = 0
    touched: set[str] = set()
    for row in pairs:
        pair = dict(row)
        # don't cascade-flag a memory already flagged this pass
        if pair["a_id"] in touched or pair["b_id"] in touched:
            continue
        outcome = await llm.judge_contradiction(pair["a_content"], pair["b_content"])
        if outcome not in FLAGGING_OUTCOMES:
            continue
        loser_id, keeper_id = _pick_loser(pair)
        await _flag_needs_review(loser_id, keeper_id, outcome)
        touched.add(loser_id)
        flagged += 1
    if flagged:
        log.info("contradiction sweep: flagged %d memories needs_review", flagged)
    return flagged


async def run_forever() -> None:
    log.info("contradiction sweep starting (interval %ss)", CONTRADICTION_INTERVAL_S)
    while True:
        try:
            await sweep_once()
        except Exception:  # noqa: BLE001
            log.exception("contradiction sweep pass failed")
        await asyncio.sleep(CONTRADICTION_INTERVAL_S)
