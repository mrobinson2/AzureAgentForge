"""Automatic repetition detection -> skill autogen (0008_skill_autogen.sql).

The model-driven `skill_manage` tool only creates a skill when the agent
*chooses* to. This closes that gap: a daily-cadence background loop
(mirrors contradiction.py) that, for each skill-enabled agent, finds PROCEDURAL
memories the agent recorded across MULTIPLE distinct task scopes -- a recurring
procedure -- and asks the classifier tier to crystallize the cluster into one
reusable skill.

The result is persisted as a CANDIDATE (skill_candidates), never written to a
live skill file: the skill-curator job materializes *approved* candidates. The
governor proposes; the operator/curator disposes -- the same posture as the
contradiction sweep and pin_candidates. Auto-injecting an unreviewed
skill into an agent's toolkit is exactly the silent-behavior-change the platform
deliberately avoids.

Gated by SKILL_AUTOGEN_ENABLED -- the loop runs unconditionally; with the flag
off, mine_once() no-ops. Uses the in-pod router sidecar for synthesis.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from . import db, llm

log = logging.getLogger("governor.skill_miner")

SKILL_AUTOGEN_INTERVAL_S = float(os.environ.get("SKILL_AUTOGEN_INTERVAL_S", "86400"))  # daily
# Minimum *other* task scopes the procedure must appear in (the seed's scope +
# this many sibling scopes = the procedure's footprint). Default 2 -> seen in
# >= 3 distinct tasks before it's worth a skill.
MIN_SIBLING_SCOPES = int(os.environ.get("SKILL_AUTOGEN_MIN_RECURRENCE", "2"))
SIM_THRESHOLD = float(os.environ.get("SKILL_AUTOGEN_SIM", "0.45"))
MAX_CLUSTERS_PER_AGENT = int(os.environ.get("SKILL_AUTOGEN_MAX_CLUSTERS", "10"))
MAX_SIBLING_CONTENTS = int(os.environ.get("SKILL_AUTOGEN_MAX_SIBLINGS", "6"))


def agent_allowlist() -> list[str]:
    """Slugs to mine. Defaults to the skill-enabled agents."""
    raw = os.environ.get(
        "SKILL_AUTOGEN_AGENT_ALLOWLIST",
        "coder,infrastructure,orchestrator",
    )
    return [s.strip() for s in raw.split(",") if s.strip()]


# Per-agent procedural memories that recur across >= MIN_SIBLING_SCOPES OTHER
# task scopes. The %/similarity() pair is the same trigram signal the
# contradiction sweep uses; the scope-inequality join is what makes it
# "same procedure, different tasks" rather than "duplicate within one task".
_RECURRING_SQL = """
SELECT a.id AS seed_id, a.content AS seed_content,
       count(DISTINCT b.memory_scope_id) AS recurrence,
       (array_agg(DISTINCT b.id))[1:$6]                  AS sibling_ids,
       (array_agg(DISTINCT left(b.content, 600)))[1:$6]  AS sibling_contents
FROM documents a
JOIN documents b
  ON a.id <> b.id
 AND a.workspace_name = b.workspace_name
 AND a.created_by_peer = b.created_by_peer
 AND a.content % b.content
 AND similarity(a.content, b.content) >= $3
 AND COALESCE(a.memory_scope_id, '') <> COALESCE(b.memory_scope_id, '')
WHERE a.workspace_name = $1
  AND a.created_by_peer = $2
  AND a.memory_class IN ('durable_fact', 'task_scoped')
  AND b.memory_class IN ('durable_fact', 'task_scoped')
  AND a.deleted_at IS NULL AND b.deleted_at IS NULL
  AND a.verification_state NOT IN ('disputed', 'superseded', 'needs_review')
  AND b.verification_state NOT IN ('disputed', 'superseded', 'needs_review')
GROUP BY a.id, a.content
HAVING count(DISTINCT b.memory_scope_id) >= $4
ORDER BY recurrence DESC
LIMIT $5
"""


def cluster_signature(seed_id) -> str:
    """Stable per-cluster key (the representative seed doc id) so the unique
    index (agent_slug, cluster_signature) dedups re-proposed clusters."""
    return hashlib.sha1(str(seed_id).encode()).hexdigest()[:16]


async def _persist_candidate(
    agent: str,
    workspace: str | None,
    seed_id,
    sibling_ids,
    recurrence: int,
    name: str,
    body: str,
) -> bool:
    """Insert one candidate. Returns False if the cluster was already proposed
    (ON CONFLICT DO NOTHING via the unique signature index)."""
    p = await db.pool()
    sig = cluster_signature(seed_id)
    source_ids = [str(seed_id)] + [str(s) for s in (sibling_ids or [])]
    row = await p.fetchrow(
        """INSERT INTO skill_candidates
             (agent_slug, workspace_name, skill_name, skill_body,
              source_doc_ids, cluster_signature, recurrence, status)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending_review')
           ON CONFLICT (agent_slug, cluster_signature) DO NOTHING
           RETURNING id""",
        agent, workspace, name, body, source_ids, sig, recurrence,
    )
    if row is None:
        return False
    await db.emit_event(
        "skill_candidate_generated",
        "skill-miner",
        {
            "candidate_id": str(row["id"]),
            "agent": agent,
            "skill_name": name,
            "recurrence": recurrence,
        },
    )
    return True


async def mine_agent(agent: str, workspace: str) -> int:
    """Mine one agent's recurring procedural memory into skill candidates."""
    p = await db.pool()
    rows = await p.fetch(
        _RECURRING_SQL, workspace, agent, SIM_THRESHOLD,
        MIN_SIBLING_SCOPES, MAX_CLUSTERS_PER_AGENT, MAX_SIBLING_CONTENTS,
    )
    created = 0
    covered: set[str] = set()  # doc ids already claimed by a chosen cluster
    for row in rows:
        c = dict(row)
        seed_key = str(c["seed_id"])
        sibling_keys = [str(s) for s in (c.get("sibling_ids") or [])]
        # one candidate per cluster: skip overlapping seeds
        if seed_key in covered or any(s in covered for s in sibling_keys):
            continue
        contents = [c["seed_content"], *(c.get("sibling_contents") or [])]
        skill = await llm.synthesize_skill(agent, contents)
        if not skill:
            continue
        if await _persist_candidate(
            agent, workspace, c["seed_id"], c.get("sibling_ids"),
            int(c["recurrence"]) + 1, skill["name"], skill["body"],
        ):
            created += 1
            covered.add(seed_key)
            covered.update(sibling_keys)
    return created


async def mine_once(workspace: str | None = None) -> int:
    """One pass across the agent allowlist. Returns candidates created."""
    if not await db.flag_enabled("SKILL_AUTOGEN_ENABLED"):
        return 0
    ws = workspace or os.environ.get("GOVERNOR_WORKSPACE") or os.environ.get("HONCHO_APP_ID")
    if not ws:
        return 0
    total = 0
    for agent in agent_allowlist():
        try:
            total += await mine_agent(agent, ws)
        except Exception:  # noqa: BLE001
            log.exception("skill mining failed for agent %s", agent)
    if total:
        log.info("skill miner: generated %d skill candidate(s)", total)
    return total


async def run_forever() -> None:
    log.info("skill miner starting (interval %ss)", SKILL_AUTOGEN_INTERVAL_S)
    while True:
        try:
            await mine_once()
        except Exception:  # noqa: BLE001
            log.exception("skill mining pass failed")
        await asyncio.sleep(SKILL_AUTOGEN_INTERVAL_S)
