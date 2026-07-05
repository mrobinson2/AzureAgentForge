"""Retrieval scoring + dynamic trust math.

Pure functions — no I/O — so the offline suite can pin the math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

# default class weights
CLASS_WEIGHTS: dict[str, float] = {
    "user_preference": 1.20,
    "durable_fact": 1.00,
    "task_scoped": 0.95,  # in scope; out of scope is filtered before scoring
    "decaying": 0.80,
    "ephemeral": 0.70,  # in-session; pinned is injected, never scored
}

# baseline trust by source
BASE_SOURCE_TRUST: dict[str, float] = {
    "operator_entered": 1.00,
    "user_asserted": 0.90,
    "external_import": 0.80,
    "agent_observed": 0.60,
    "derived": 0.50,
}

# verification weights
VERIFICATION_WEIGHT: dict[str, float] = {
    "confirmed": 1.15,
    "inferred": 0.95,
    "unverified": 0.85,
    "needs_review": 0.40,
    "disputed": 0.0,
    "superseded": 0.0,
}

# per-turn ceilings: class -> (top_k, token_ceiling)
CLASS_BUDGETS: dict[str, tuple[int, int]] = {
    "pinned": (-1, 500),  # -1 = all eligible
    "user_preference": (5, 300),
    "durable_fact": (5, 400),
    "task_scoped": (8, 500),
    "decaying": (3, 200),
    "ephemeral": (-1, 300),
}

USAGE_SUCCESS_CAP = 1.25
CONTRADICTION_STEP = 0.15
CONTRADICTION_FLOOR = 0.25


def decay_factor(
    created_at: datetime, half_life_days: float | None, now: datetime | None = None
) -> float:
    """exp(-age_days / half_life_days); 1.0 for non-decaying memories."""
    if not half_life_days or half_life_days <= 0:
        return 1.0
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    return math.exp(-age_days / half_life_days)


def usage_success_factor(usage_success_count: int) -> float:
    """Starts at 1.0, climbs slowly with successful reuse, capped."""
    return min(USAGE_SUCCESS_CAP, 1.0 + 0.05 * max(0, usage_success_count))


def contradiction_penalty(contradiction_count: int) -> float:
    """Decreases as contradiction count rises until reviewed/resolved."""
    return max(CONTRADICTION_FLOOR, 1.0 - CONTRADICTION_STEP * max(0, contradiction_count))


def confirmation_factor(
    last_confirmed_at: datetime | None, now: datetime | None = None
) -> float:
    """Recency-weighted confirmation boost: fresh confirmation ~1.1, decaying
    back to 1.0 over ~180 days; never-confirmed = 1.0."""
    if last_confirmed_at is None:
        return 1.0
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - last_confirmed_at).total_seconds() / 86400.0)
    return 1.0 + 0.1 * math.exp(-age_days / 180.0)


def trust_modifier(
    source_type: str | None,
    verification_state: str | None,
    last_confirmed_at: datetime | None,
    usage_success_count: int,
    contradiction_count: int,
    now: datetime | None = None,
) -> float:
    """Retrieval-time trust formula. 0.0 for disputed/superseded."""
    base = BASE_SOURCE_TRUST.get(source_type or "derived", 0.50)
    vw = VERIFICATION_WEIGHT.get(verification_state or "unverified", 0.85)
    if vw == 0.0:
        return 0.0
    return (
        base
        * vw
        * confirmation_factor(last_confirmed_at, now)
        * usage_success_factor(usage_success_count)
        * contradiction_penalty(contradiction_count)
    )


@dataclass
class ScoredMemory:
    doc_id: str
    content: str
    memory_class: str
    score: float
    semantic_similarity: float
    trust: float
    metadata: dict


def retrieval_score(
    semantic_similarity: float,
    memory_class: str,
    in_scope: bool,
    created_at: datetime,
    half_life_days: float | None,
    source_type: str | None,
    verification_state: str | None,
    last_confirmed_at: datetime | None,
    usage_success_count: int = 0,
    contradiction_count: int = 0,
    diversity_penalty: float = 1.0,
    now: datetime | None = None,
) -> float:
    """Composite retrieval score. Out-of-scope scoped memory scores 0."""
    if not in_scope:
        return 0.0
    class_weight = CLASS_WEIGHTS.get(memory_class, 0.0)
    dec = (
        decay_factor(created_at, half_life_days, now)
        if memory_class == "decaying"
        else 1.0
    )
    trust = trust_modifier(
        source_type,
        verification_state,
        last_confirmed_at,
        usage_success_count,
        contradiction_count,
        now,
    )
    return semantic_similarity * class_weight * dec * trust * diversity_penalty


def estimate_tokens(text: str) -> int:
    """Cheap token estimate for ceiling enforcement (chars/4)."""
    return max(1, len(text) // 4)


def apply_class_budgets(
    scored: list[ScoredMemory],
) -> list[ScoredMemory]:
    """Top-K + token ceilings per class, preserving score order.

    Also applies the diversity guard upstream callers compute into
    diversity_penalty; here we enforce only counts and tokens.
    """
    out: list[ScoredMemory] = []
    counts: dict[str, int] = {}
    tokens: dict[str, int] = {}
    for m in sorted(scored, key=lambda s: s.score, reverse=True):
        if m.score <= 0:
            continue
        top_k, ceiling = CLASS_BUDGETS.get(m.memory_class, (3, 200))
        used_k = counts.get(m.memory_class, 0)
        used_t = tokens.get(m.memory_class, 0)
        cost = estimate_tokens(m.content)
        if top_k != -1 and used_k >= top_k:
            continue
        if used_t + cost > ceiling:
            continue
        counts[m.memory_class] = used_k + 1
        tokens[m.memory_class] = used_t + cost
        out.append(m)
    return out
