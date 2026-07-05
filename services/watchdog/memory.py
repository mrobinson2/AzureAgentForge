"""Persist failure lessons as governed durable_fact memories.

This closes the self-improvement loop: when the watchdog files an issue for a
failure that names a specific agent, it
ALSO writes a peer-scoped, agent-observed `durable_fact` through the memory
governor's admission pipeline. The planner then re-injects that lesson into the
very agent that keeps hitting it — so agents stop relearning the same outage.

Design choices that make this safe and idempotent:
  - source_type=agent_observed  → trust_score seeds at 0.60 (scoring.py),
    verification_state=unverified. Lessons are LOW-trust until the operator
    confirms them; the operator's `dispute` action flips them to disputed
    (trust 0.0), which removes them from retrieval — the kill switch.
  - confidence_score=0.85 (>= MEMORY_PERSIST_THRESHOLD) so the explicit
    durable_fact actually persists instead of being demoted to `decaying`
    (classifier.compute_retention_action).
  - planner_hint=failure_lesson  → the planner's dedicated failure-lessons
    retrieval surfaces it regardless of query similarity.
  - Re-filing the same failure is collapsed by the governor's trigram dedup —
    no row spam across watchdog ticks.

The network call is isolated + injectable (poster=) so the payload builder
stays pure and unit-tested, mirroring filer.py.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .detectors import Finding
from .roster import slug_for

# >= MEMORY_PERSIST_THRESHOLD (classifier.py, default 0.80) so the explicit
# durable_fact persists rather than demoting to decaying. Trust (0.6) comes
# from source_type, NOT this number.
LESSON_CONFIDENCE = 0.85

# created_by_peer the governor must grant durable_fact write authority — see the
# `watchdog` profile in the governor's profiles.py. Without write authority the
# admission pipeline returns event_only (no persist).
WATCHDOG_WRITER = "watchdog"

# Track-record routing: scorecards are durable_facts on the orchestrator's peer
# scope so its planner injects them at delegation time; planner_hint lets the
# planner surface them regardless of query similarity (mirrors failure_lesson).
TRACK_RECORD_HINT = "track_record"
ROUTER_AGENT_SLUG = "orchestrator"


def build_admit_payload(
    f: Finding, *, workspace: str, writer: str = WATCHDOG_WRITER
) -> Optional[dict]:
    """Build the governor /admit body for a finding's failure lesson.

    Returns None when the finding has no agent-scoped lesson (infra-level
    findings) or names an agent we can't resolve to a live slug — the planner
    only matches lessons whose scope_id is the agent's live slug.
    """
    slug = slug_for(f.subject_agent)
    if not slug or not f.lesson:
        return None
    # Trailing signature tag links the memory back to the watchdog issue and
    # keeps re-writes of the SAME finding byte-identical (so trigram dedup
    # collapses them) while DIFFERENT findings stay distinct.
    content = f"{f.lesson} [signature: {f.signature}]"
    return {
        "content": content,
        "workspace_name": workspace,
        "observer": slug,
        "observed": slug,
        "created_by_peer": writer,
        "memory_class": "durable_fact",
        "scope_kind": "peer",
        "scope_id": slug,
        "source_type": "agent_observed",
        "verification_state": "unverified",
        "confidence_score": LESSON_CONFIDENCE,
        "planner_hint": "failure_lesson",
    }


def write_lesson(
    f: Finding,
    *,
    base_url: str,
    key: str,
    workspace: str,
    poster: Optional[Callable] = None,
) -> Optional[dict]:
    """Persist one finding's failure lesson via governor /admit.

    Returns the admission verdict dict, or None if the finding produced no
    lesson. `poster` is overridable for tests (defaults to urllib POST). The
    governor itself no-ops (status=disabled) when MEMORY_CLASSES_ENABLED is off,
    so this is safe to call before the flag is flipped.
    """
    payload = build_admit_payload(f, workspace=workspace)
    if payload is None:
        return None
    url = f"{base_url.rstrip('/')}/admit"
    if poster is not None:
        return poster(url, payload, key)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Governor-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "{}")


def reconfirm_memory(
    doc_id: str,
    run_id: str,
    *,
    base_url: str,
    key: str,
    actor: str = "watchdog",
    poster: Optional[Callable] = None,
) -> dict:
    """Credit a memory that contributed to a successful run via the governor's
    `reconfirm` action (use-based earned trust). The governor
    increments usage_success_count + last_confirmed_at and skips
    disputed/superseded docs. `poster` is overridable for tests."""
    url = f"{base_url.rstrip('/')}/memory/{doc_id}/action"
    payload = {"action": "reconfirm", "actor": actor, "note": f"successful-use: run {run_id}"}
    if poster is not None:
        return poster(url, payload, key)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Governor-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "{}")


# ── Track-record routing — delegation scorecards as governed memory ──

def build_scorecard_payload(
    card: dict, *, workspace: str, target_slug: str = ROUTER_AGENT_SLUG,
    writer: str = WATCHDOG_WRITER,
) -> dict:
    """Governor /admit body for one agent's delegation scorecard. Scoped to the
    orchestrator's peer scope + planner_hint=track_record so HIS planner injects
    it at delegation time. source_type=agent_observed (trust 0.6) so a misleading
    scorecard can be disputed away by the operator."""
    return {
        "content": card["summary"],
        "workspace_name": workspace,
        "observer": target_slug,
        "observed": card["slug"],
        "created_by_peer": writer,
        "memory_class": "durable_fact",
        "scope_kind": "peer",
        "scope_id": target_slug,
        "source_type": "agent_observed",
        "verification_state": "unverified",
        "confidence_score": LESSON_CONFIDENCE,
        "planner_hint": TRACK_RECORD_HINT,
    }


def _http_get(url: str, key: str):
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Governor-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "[]")


def _http_post(url: str, payload: dict, key: str):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Governor-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or "{}")


def write_scorecards(
    cards: list[dict], *, base_url: str, key: str, workspace: str,
    target_slug: str = ROUTER_AGENT_SLUG,
    getter: Optional[Callable] = None, poster: Optional[Callable] = None,
) -> int:
    """Upsert per-agent scorecards as durable_fact memories.

    Scorecards are MUTABLE (numbers change each cycle), so for each agent we
    retire the prior scorecard (rm → deleted_at, which the admission dedup query
    excludes) before admitting the fresh one — otherwise trigram dedup would
    reconfirm the stale card and the numbers would never update. Returns the
    number written. Safe before MEMORY_CLASSES_ENABLED (governor no-ops).
    getter/poster are injectable for tests."""
    base = base_url.rstrip("/")
    get = getter or _http_get
    post = poster or _http_post
    q = urllib.parse.urlencode({
        "workspace_name": workspace, "created_by": WATCHDOG_WRITER,
        "memory_class": "durable_fact", "scope_kind": "peer", "limit": 200,
    })
    try:
        existing = get(f"{base}/memory?{q}", key) or []
    except Exception:  # noqa: BLE001 — listing is best-effort; worst case a stale card lingers
        existing = []
    written = 0
    for card in cards:
        tag = f"[track-record:{card['slug']}]"
        for row in existing:
            if tag in (row.get("snippet") or ""):
                try:
                    post(f"{base}/memory/{row['id']}/action",
                         {"action": "rm", "actor": WATCHDOG_WRITER,
                          "note": "retired: superseded by fresh scorecard"}, key)
                except Exception:  # noqa: BLE001
                    pass
        try:
            post(f"{base}/admit",
                 build_scorecard_payload(card, workspace=workspace, target_slug=target_slug), key)
            written += 1
        except Exception:  # noqa: BLE001
            pass
    return written
