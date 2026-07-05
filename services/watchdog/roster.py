"""Agent display-name → live slug resolution.

The watchdog sees two forms of agent identity:
  - display names ("Researcher") in PaperClip run results, and
  - peer slugs ("researcher") in agent_events rows.

Failure-lesson memories are scoped peer:<slug>, and the memory planner matches
`memory_scope_id` against the agent's LIVE slug — so both inputs must resolve to
the same live slug. The authoritative slugs are the role slugs used by the agent
roster and the governor's profiles.py (`orchestrator`, `researcher`, …).
"""

from __future__ import annotations

from typing import Optional

# Display name (as seen in PaperClip run results) → live agent slug.
AGENT_NAME_TO_SLUG: dict[str, str] = {
    "Orchestrator": "orchestrator",
    "Strategy": "strategy",
    "Planner": "planner",
    "Coder": "coder",
    "Infrastructure": "infrastructure",
    "Researcher": "researcher",
    "Coach": "coach",
    "Business": "business",
    "Psychology": "psychology",
    "QA": "qa",
    "Security": "security",
    "CostGuardian": "cost-guardian",
    "Curator": "curator",
}

# Slugs are also valid input (agent_events.actor_peer is already a slug).
KNOWN_SLUGS = frozenset(AGENT_NAME_TO_SLUG.values())

_LOWER_NAME_TO_SLUG = {name.lower(): slug for name, slug in AGENT_NAME_TO_SLUG.items()}


def slug_for(name_or_slug: Optional[str]) -> Optional[str]:
    """Resolve a display name OR an already-correct slug to the live agent slug.

    Returns None for unknown agents, empty input, or opaque ids (e.g. a bare
    agent UUID) — callers skip the failure-lesson write rather than scope a
    memory to a peer the planner will never match.
    """
    if not name_or_slug:
        return None
    candidate = name_or_slug.strip()
    if candidate in KNOWN_SLUGS:
        return candidate
    return _LOWER_NAME_TO_SLUG.get(candidate.lower())
