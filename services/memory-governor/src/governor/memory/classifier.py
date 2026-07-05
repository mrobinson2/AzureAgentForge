"""Memory-class classifier.

Pure stdlib on purpose: the golden-fixture tests under tests/memory-governor/
import this module directly (with sys.path at
services/memory-governor/src/governor) without FastAPI/asyncpg installed. The
LLM call itself lives in governor.llm — this module owns parsing, validation,
and admission math.

Safety invariants (tested by the golden fixtures):
  - parse_classification NEVER returns MemoryClass.PINNED — a "pinned"
    response converts to durable_fact with is_pinned_candidate=True.
  - Garbage / unknown classes fall back to decaying + EVENT_ONLY.
  - Admission thresholds: >=0.80 persist; 0.50-0.79 persist_decaying
    (except task_scoped, which persists inside its scope); <0.50 event_only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum


class MemoryClass(Enum):
    PINNED = "pinned"
    DURABLE_FACT = "durable_fact"
    USER_PREFERENCE = "user_preference"
    TASK_SCOPED = "task_scoped"
    EPHEMERAL = "ephemeral"
    DECAYING = "decaying"


class RetentionAction(Enum):
    PERSIST = "persist"
    PERSIST_DECAYING = "persist_decaying"
    EVENT_ONLY = "event_only"


class SourceType(Enum):
    USER_ASSERTED = "user_asserted"
    OPERATOR_ENTERED = "operator_entered"
    AGENT_OBSERVED = "agent_observed"
    DERIVED = "derived"
    EXTERNAL_IMPORT = "external_import"


class VerificationState(Enum):
    UNVERIFIED = "unverified"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    NEEDS_REVIEW = "needs_review"


# Admission thresholds (configurable via env).
PERSIST_THRESHOLD = float(os.environ.get("MEMORY_PERSIST_THRESHOLD", "0.80"))
DECAYING_THRESHOLD = float(os.environ.get("MEMORY_DECAYING_THRESHOLD", "0.50"))

DEFAULT_HALF_LIFE_DAYS = float(os.environ.get("MEMORY_DEFAULT_HALF_LIFE_DAYS", "14"))


@dataclass
class ClassificationResult:
    memory_class: MemoryClass
    retention_action: RetentionAction
    confidence: float
    source_type: SourceType
    verification_state: VerificationState
    is_pinned_candidate: bool = False
    is_always_on_candidate: bool = False
    half_life_days: float | None = None
    scope_kind: str | None = None
    scope_id: str | None = None
    reason: str = ""
    parse_error: str | None = None  # non-None when fallback path was taken


def compute_retention_action(
    memory_class: MemoryClass, confidence: float
) -> RetentionAction:
    """Three-outcome admission decision.

    task_scoped is exempt from the medium-confidence decay demotion: it is
    already lifecycle-bounded by its scope, so medium confidence persists.
    """
    if confidence >= PERSIST_THRESHOLD:
        return RetentionAction.PERSIST
    if confidence >= DECAYING_THRESHOLD:
        if memory_class == MemoryClass.TASK_SCOPED:
            return RetentionAction.PERSIST
        return RetentionAction.PERSIST_DECAYING
    return RetentionAction.EVENT_ONLY


def _fallback(reason: str) -> ClassificationResult:
    """Unparseable/invalid responses become low-risk decaying candidates that
    are NOT persisted (event_only): a clean memory is created by what the
    system refuses to store."""
    return ClassificationResult(
        memory_class=MemoryClass.DECAYING,
        retention_action=RetentionAction.EVENT_ONLY,
        confidence=0.0,
        source_type=SourceType.DERIVED,
        verification_state=VerificationState.UNVERIFIED,
        half_life_days=DEFAULT_HALF_LIFE_DAYS,
        reason="fallback",
        parse_error=reason,
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(raw: str) -> dict | None:
    """Tolerate fenced or surrounded JSON — LLMs decorate."""
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # last resort: first {...} block in the text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _enum_or(enum_cls, value, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def parse_classification(raw: str) -> ClassificationResult:
    """Parse an LLM classification response into a validated result.

    Never raises; never returns PINNED.
    """
    obj = _extract_json(raw if isinstance(raw, str) else "")
    if obj is None:
        return _fallback("unparseable response")

    cls_raw = obj.get("memory_class")
    try:
        memory_class = MemoryClass(cls_raw)
    except (ValueError, TypeError):
        return _fallback(f"unknown memory_class: {cls_raw!r}")

    try:
        confidence = float(obj.get("confidence_score", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    is_pinned_candidate = bool(obj.get("is_pinned_candidate", False))

    # SAFETY: the classifier may never directly create pinned.
    if memory_class == MemoryClass.PINNED:
        memory_class = MemoryClass.DURABLE_FACT
        is_pinned_candidate = True

    half_life = obj.get("half_life_days")
    try:
        half_life = float(half_life) if half_life is not None else None
    except (TypeError, ValueError):
        half_life = None
    if memory_class == MemoryClass.DECAYING and half_life is None:
        half_life = DEFAULT_HALF_LIFE_DAYS

    result = ClassificationResult(
        memory_class=memory_class,
        retention_action=compute_retention_action(memory_class, confidence),
        confidence=confidence,
        source_type=_enum_or(SourceType, obj.get("source_type"), SourceType.DERIVED),
        # unknown verification states default to INFERRED (the classifier
        # asserted SOMETHING, just not a recognized state) per the golden
        # fixture contract; absent ones default to UNVERIFIED in _fallback.
        verification_state=_enum_or(
            VerificationState, obj.get("verification_state"), VerificationState.INFERRED
        ),
        is_pinned_candidate=is_pinned_candidate,
        is_always_on_candidate=bool(obj.get("is_always_on_candidate", False)),
        half_life_days=half_life,
        scope_kind=obj.get("scope_kind") or None,
        scope_id=obj.get("scope_id") or None,
        reason=str(obj.get("reason", ""))[:500],
    )
    return result


# ---------------------------------------------------------------------------
# Classification prompt (used by governor.llm; kept here so the contract and
# the prompt evolve together and the replay harness can exercise it offline).
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM_PROMPT = """\
You classify a single observation into a memory class for a multi-agent system.
Respond with ONLY a JSON object, no prose, matching exactly this shape:
{"memory_class":"durable_fact|user_preference|task_scoped|ephemeral|decaying",
 "confidence_score":0.0-1.0,
 "reason":"one short sentence",
 "is_pinned_candidate":false,
 "is_always_on_candidate":false,
 "half_life_days":null or number,
 "scope_kind":null or "workspace|peer|task|session",
 "scope_id":null or "<id>",
 "source_type":"user_asserted|operator_entered|agent_observed|derived|external_import",
 "verification_state":"unverified|inferred|confirmed"}

Rules:
- durable_fact: stable world/user/system facts likely true beyond current tasks.
- user_preference: how the user wants work done.
- task_scoped: tied to a specific issue/thread/incident — MUST set scope_kind="task" and scope_id.
- ephemeral: session-only working state — MUST set scope_kind="session".
- decaying: time-sensitive context that should fade — set half_life_days (7/14/30).
- NEVER output "pinned". is_pinned_candidate=true is RARE — reserve it for
  operator policy rules and identity-critical facts that belong in every
  prompt (legal name, hard safety rules). Ordinary facts, preferences, and
  observations are NOT pin candidates. When unsure, false.
- Confidence reflects how sure you are of BOTH the class and the content's accuracy.
- Session chatter, speculation, and low-information content deserve LOW confidence.
"""


def build_classify_messages(content: str, context: str | None = None) -> list[dict]:
    user = f"Observation to classify:\n{content}"
    if context:
        user += f"\n\nContext:\n{context}"
    return [
        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
