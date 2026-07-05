"""Agent memory profiles, enforced.

Write authority is not trust authority: an agent may write task_scoped memory
with no right to create durable global memory. The admission pipeline consults
writeClasses for the FINAL class (after classification and any decaying
demotion); the planner consults readClasses.

Override per deployment with MEMORY_PROFILES_JSON (same shape as
DEFAULT_PROFILES). Unknown writers get the SPECIALIST profile — restrictive by
default.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("governor.profiles")

ALL_CLASSES = ["pinned", "durable_fact", "user_preference", "task_scoped", "ephemeral", "decaying"]

ORCHESTRATOR = {
    "read": ALL_CLASSES,
    "write": ALL_CLASSES,  # admission still converts pinned -> candidate
}
SPECIALIST = {
    "read": ["pinned", "durable_fact", "task_scoped", "decaying"],
    "write": ["task_scoped", "ephemeral"],
}
SECURITY = {
    "read": ["pinned", "durable_fact", "decaying"],
    "write": ["ephemeral"],
}
MONITOR = {
    "read": ["pinned", "durable_fact", "decaying"],
    "write": ["decaying"],
}
# The watchdog service (services/watchdog) writes peer-scoped agent_observed
# durable_fact "failure lessons". Least privilege: it may create durable_fact +
# decaying observations and read facts back, but never user_preference,
# task_scoped, ephemeral, or pinned.
WATCHDOG = {
    "read": ["pinned", "durable_fact", "decaying"],
    "write": ["durable_fact", "decaying"],
}
SYSTEM = {"read": ALL_CLASSES, "write": ALL_CLASSES}

DEFAULT_PROFILES: dict[str, dict] = {
    "orchestrator": ORCHESTRATOR,
    "strategy": SPECIALIST,
    "planner": SPECIALIST,
    "coder": SPECIALIST,
    "infrastructure": SPECIALIST,
    "researcher": SPECIALIST,
    "coach": SPECIALIST,
    "business": SPECIALIST,
    "psychology": SPECIALIST,
    "qa": SPECIALIST,
    "curator": SPECIALIST,
    "security": SECURITY,
    "cost-guardian": MONITOR,
    # non-agent writers
    "operator": SYSTEM,
    "annotator": SYSTEM,
    "system": SYSTEM,
    "sweeper": SYSTEM,
    # the watchdog service — durable_fact write authority for failure lessons.
    "watchdog": WATCHDOG,
}


def _load() -> dict[str, dict]:
    raw = os.environ.get("MEMORY_PROFILES_JSON")
    if not raw:
        return DEFAULT_PROFILES
    try:
        override = json.loads(raw)
        merged = dict(DEFAULT_PROFILES)
        merged.update(override)
        return merged
    except (ValueError, TypeError):
        log.exception("MEMORY_PROFILES_JSON unparseable — using defaults")
        return DEFAULT_PROFILES


_PROFILES = _load()


def profile_for(slug: str | None) -> dict:
    return _PROFILES.get(slug or "", SPECIALIST)


def can_write(slug: str | None, memory_class: str) -> bool:
    return memory_class in profile_for(slug)["write"]


def readable_classes(slug: str | None) -> list[str]:
    return profile_for(slug)["read"]
