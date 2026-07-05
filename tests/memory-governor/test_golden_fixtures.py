"""Golden fixture tests for the memory-class classifier.

Tests ``parse_classification`` against hand-crafted LLM-style JSON responses
covering all 6 classes, edge cases, and the admission pipeline.

Acceptance criteria:
  - >=90% exact-class match
  - 100% safe-class match (never produces "pinned" directly)
  - Low-confidence (<0.50) always produces event_only retention
  - Medium-confidence (0.50-0.79) produces persist_decaying (except task_scoped)
  - High-confidence (>=0.80) produces persist

Run with: pytest -q tests/memory-governor
"""

import pytest

from memory.classifier import (
    ClassificationResult,
    MemoryClass,
    RetentionAction,
    SourceType,
    VerificationState,
    parse_classification,
    compute_retention_action,
)


# ---------------------------------------------------------------------------
# Golden fixtures: (raw_json_response, expected_class, description)
# ---------------------------------------------------------------------------

GOLDEN_FIXTURES: list[tuple[str, MemoryClass, str]] = [
    # --- durable_fact ---
    (
        '{"memory_class":"durable_fact","confidence_score":0.95,"reason":"Stable identity fact","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DURABLE_FACT,
        "User's legal name",
    ),
    (
        '{"memory_class":"durable_fact","confidence_score":0.92,"reason":"Company name","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"workspace","scope_id":null,"retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.DURABLE_FACT,
        "User's company",
    ),
    (
        '{"memory_class":"durable_fact","confidence_score":0.90,"reason":"Cloud provider","is_pinned_candidate":false,"is_always_on_candidate":true,"half_life_days":null,"scope_kind":"workspace","scope_id":null,"retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.DURABLE_FACT,
        "Primary cloud provider",
    ),
    (
        '{"memory_class":"durable_fact","confidence_score":0.88,"reason":"Timezone","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DURABLE_FACT,
        "User timezone",
    ),
    (
        '{"memory_class":"durable_fact","confidence_score":0.91,"reason":"Primary language","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DURABLE_FACT,
        "Primary language",
    ),
    # --- user_preference ---
    (
        '{"memory_class":"user_preference","confidence_score":0.93,"reason":"Output style preference","is_pinned_candidate":false,"is_always_on_candidate":true,"half_life_days":null,"scope_kind":"workspace","scope_id":null,"retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.USER_PREFERENCE,
        "Prefers terse responses",
    ),
    (
        '{"memory_class":"user_preference","confidence_score":0.89,"reason":"Indent preference","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.USER_PREFERENCE,
        "2-space indent in YAML",
    ),
    (
        '{"memory_class":"user_preference","confidence_score":0.87,"reason":"UI preference","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.USER_PREFERENCE,
        "Prefers dark mode",
    ),
    (
        '{"memory_class":"user_preference","confidence_score":0.85,"reason":"Communication style","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.USER_PREFERENCE,
        "No emoji in responses",
    ),
    (
        '{"memory_class":"user_preference","confidence_score":0.88,"reason":"Format preference","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.USER_PREFERENCE,
        "Prefers markdown formatting",
    ),
    # --- task_scoped ---
    (
        '{"memory_class":"task_scoped","confidence_score":0.94,"reason":"Issue branch","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"task","scope_id":"PC-142","retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.TASK_SCOPED,
        "Target branch for issue",
    ),
    (
        '{"memory_class":"task_scoped","confidence_score":0.91,"reason":"Blocker","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"task","scope_id":"PC-200","retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.TASK_SCOPED,
        "Issue blocker",
    ),
    (
        '{"memory_class":"task_scoped","confidence_score":0.89,"reason":"Thread decision","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"task","scope_id":"t-abc","retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.TASK_SCOPED,
        "Thread decision",
    ),
    (
        '{"memory_class":"task_scoped","confidence_score":0.60,"reason":"Possible assignee","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"task","scope_id":"PC-305","retention_action":"persist","source_type":"derived","verification_state":"unverified"}',
        MemoryClass.TASK_SCOPED,
        "Task-scoped with medium confidence (should still persist as task_scoped)",
    ),
    (
        '{"memory_class":"task_scoped","confidence_score":0.90,"reason":"Deadline","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"task","scope_id":"PC-142","retention_action":"persist","source_type":"user_asserted","verification_state":"confirmed"}',
        MemoryClass.TASK_SCOPED,
        "Task deadline",
    ),
    # --- ephemeral ---
    (
        '{"memory_class":"ephemeral","confidence_score":0.96,"reason":"In-session meeting ref","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"session","scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.EPHEMERAL,
        "Meeting reference",
    ),
    (
        '{"memory_class":"ephemeral","confidence_score":0.93,"reason":"Thinking out loud","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"session","scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.EPHEMERAL,
        "Thinking out loud",
    ),
    (
        '{"memory_class":"ephemeral","confidence_score":0.91,"reason":"Scratch calc","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"session","scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.EPHEMERAL,
        "Scratch calculation",
    ),
    (
        '{"memory_class":"ephemeral","confidence_score":0.99,"reason":"Contains secret","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"session","scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.EPHEMERAL,
        "Secret detected",
    ),
    (
        '{"memory_class":"ephemeral","confidence_score":0.88,"reason":"Transient","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"session","scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"inferred"}',
        MemoryClass.EPHEMERAL,
        "Transient observation",
    ),
    # --- decaying ---
    (
        '{"memory_class":"decaying","confidence_score":0.85,"reason":"Debug session","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":14,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DECAYING,
        "Debug session context",
    ),
    (
        '{"memory_class":"decaying","confidence_score":0.82,"reason":"Weather chat","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":7,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"unverified"}',
        MemoryClass.DECAYING,
        "Weather conversation",
    ),
    (
        '{"memory_class":"decaying","confidence_score":0.80,"reason":"Conversation context","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":14,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DECAYING,
        "General conversation context",
    ),
    (
        '{"memory_class":"decaying","confidence_score":0.83,"reason":"Old cost data","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":30,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DECAYING,
        "Old cost analysis",
    ),
    (
        '{"memory_class":"decaying","confidence_score":0.81,"reason":"Casual mention","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":14,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"agent_observed","verification_state":"unverified"}',
        MemoryClass.DECAYING,
        "Casual mention",
    ),
    # --- pinned (should be converted to durable_fact) ---
    (
        '{"memory_class":"pinned","confidence_score":0.97,"reason":"Always-on fact","is_pinned_candidate":true,"is_always_on_candidate":true,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"operator_entered","verification_state":"confirmed"}',
        MemoryClass.DURABLE_FACT,
        "Pinned → durable_fact conversion #1",
    ),
    (
        '{"memory_class":"pinned","confidence_score":0.95,"reason":"Critical rule","is_pinned_candidate":true,"is_always_on_candidate":true,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"operator_entered","verification_state":"confirmed"}',
        MemoryClass.DURABLE_FACT,
        "Pinned → durable_fact conversion #2",
    ),
    # --- edge cases ---
    (
        '```json\n{"memory_class":"durable_fact","confidence_score":0.90,"reason":"Fenced","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}\n```',
        MemoryClass.DURABLE_FACT,
        "Code-fenced JSON response",
    ),
    (
        "this is not valid json at all!",
        MemoryClass.DECAYING,
        "Garbage input → decaying fallback",
    ),
    (
        '{"memory_class":"banana","confidence_score":0.50,"reason":"Unknown class","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"derived","verification_state":"inferred"}',
        MemoryClass.DECAYING,
        "Unknown class → decaying fallback",
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_json, expected_class, description",
    GOLDEN_FIXTURES,
    ids=[f[2] for f in GOLDEN_FIXTURES],
)
def test_classification(raw_json: str, expected_class: MemoryClass, description: str):
    result = parse_classification(raw_json)
    assert result.memory_class == expected_class, (
        f"[{description}] expected {expected_class.value}, got {result.memory_class.value}"
    )


def test_never_returns_pinned():
    """The classifier should NEVER return MemoryClass.PINNED."""
    for raw_json, _, desc in GOLDEN_FIXTURES:
        result = parse_classification(raw_json)
        assert result.memory_class != MemoryClass.PINNED, (
            f"[{desc}] returned pinned — critical safety violation"
        )


def test_exact_class_rate():
    """At least 90% of fixtures should produce the exact expected class."""
    correct = sum(
        1 for raw_json, expected, _ in GOLDEN_FIXTURES
        if parse_classification(raw_json).memory_class == expected
    )
    rate = correct / len(GOLDEN_FIXTURES)
    assert rate >= 0.90, f"Exact-class rate {rate:.0%} < 90%"


def test_pinned_candidate_flag_on_pinned_input():
    """When LLM outputs 'pinned', is_pinned_candidate must be True."""
    raw = '{"memory_class":"pinned","confidence_score":0.97,"reason":"test","is_pinned_candidate":true,"is_always_on_candidate":true,"half_life_days":null,"scope_kind":null,"scope_id":null,"retention_action":"persist","source_type":"operator_entered","verification_state":"confirmed"}'
    result = parse_classification(raw)
    assert result.is_pinned_candidate is True


def test_confidence_bounded():
    """Every parsed result should have confidence in [0, 1]."""
    for raw_json, _, _ in GOLDEN_FIXTURES:
        result = parse_classification(raw_json)
        assert 0.0 <= result.confidence <= 1.0


def test_decaying_has_half_life():
    """Decaying fixtures should have a non-null half_life_days."""
    for raw_json, expected, _ in GOLDEN_FIXTURES:
        if expected == MemoryClass.DECAYING:
            result = parse_classification(raw_json)
            if result.memory_class == MemoryClass.DECAYING:
                assert result.half_life_days is not None


def test_task_scoped_has_scope():
    """Task-scoped fixtures should have scope_kind and scope_id."""
    for raw_json, expected, _ in GOLDEN_FIXTURES:
        if expected == MemoryClass.TASK_SCOPED:
            result = parse_classification(raw_json)
            if result.memory_class == MemoryClass.TASK_SCOPED:
                assert result.scope_kind is not None
                assert result.scope_id is not None


# ---------------------------------------------------------------------------
# v4 admission pipeline tests
# ---------------------------------------------------------------------------

class TestAdmissionThresholds:
    """Test the three-outcome admission pipeline."""

    def test_high_confidence_persist(self):
        raw = '{"memory_class":"durable_fact","confidence_score":0.95,"reason":"high confidence","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"inferred"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.PERSIST

    def test_medium_confidence_persist_decaying(self):
        raw = '{"memory_class":"durable_fact","confidence_score":0.65,"reason":"medium confidence","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"unverified"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.PERSIST_DECAYING

    def test_low_confidence_event_only(self):
        raw = '{"memory_class":"durable_fact","confidence_score":0.35,"reason":"low confidence","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"unverified"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.EVENT_ONLY

    def test_task_scoped_medium_still_persists(self):
        """Task-scoped with medium confidence should persist (not decay)."""
        raw = '{"memory_class":"task_scoped","confidence_score":0.65,"reason":"medium task","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":"task","scope_id":"PC-99","source_type":"derived","verification_state":"inferred"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.PERSIST

    def test_garbage_is_event_only(self):
        result = parse_classification("not json")
        assert result.retention_action == RetentionAction.EVENT_ONLY

    def test_threshold_boundary_persist(self):
        raw = '{"memory_class":"user_preference","confidence_score":0.80,"reason":"boundary","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"inferred"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.PERSIST

    def test_threshold_boundary_decaying(self):
        raw = '{"memory_class":"user_preference","confidence_score":0.79,"reason":"boundary","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"unverified"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.PERSIST_DECAYING

    def test_threshold_boundary_event_only(self):
        raw = '{"memory_class":"decaying","confidence_score":0.49,"reason":"boundary","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":14,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"unverified"}'
        result = parse_classification(raw)
        assert result.retention_action == RetentionAction.EVENT_ONLY


class TestV4MetadataFields:
    """Test that v4 metadata fields are correctly parsed."""

    def test_source_type_parsed(self):
        raw = '{"memory_class":"durable_fact","confidence_score":0.90,"reason":"test","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"user_asserted","verification_state":"confirmed"}'
        result = parse_classification(raw)
        assert result.source_type == SourceType.USER_ASSERTED

    def test_verification_state_parsed(self):
        raw = '{"memory_class":"user_preference","confidence_score":0.85,"reason":"test","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":null,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"confirmed"}'
        result = parse_classification(raw)
        assert result.verification_state == VerificationState.CONFIRMED

    def test_always_on_candidate_parsed(self):
        raw = '{"memory_class":"durable_fact","confidence_score":0.95,"reason":"test","is_pinned_candidate":false,"is_always_on_candidate":true,"half_life_days":null,"scope_kind":"workspace","scope_id":null,"source_type":"user_asserted","verification_state":"confirmed"}'
        result = parse_classification(raw)
        assert result.is_always_on_candidate is True

    def test_unknown_source_type_defaults(self):
        raw = '{"memory_class":"decaying","confidence_score":0.80,"reason":"test","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":14,"scope_kind":null,"scope_id":null,"source_type":"banana","verification_state":"inferred"}'
        result = parse_classification(raw)
        assert result.source_type == SourceType.DERIVED

    def test_unknown_verification_state_defaults(self):
        raw = '{"memory_class":"decaying","confidence_score":0.80,"reason":"test","is_pinned_candidate":false,"is_always_on_candidate":false,"half_life_days":14,"scope_kind":null,"scope_id":null,"source_type":"derived","verification_state":"banana"}'
        result = parse_classification(raw)
        assert result.verification_state == VerificationState.INFERRED


class TestComputeRetentionAction:
    """Test the standalone retention action computation."""

    def test_high_confidence_all_classes(self):
        for mc in [MemoryClass.DURABLE_FACT, MemoryClass.USER_PREFERENCE, MemoryClass.DECAYING]:
            assert compute_retention_action(mc, 0.90) == RetentionAction.PERSIST

    def test_medium_confidence_becomes_decaying(self):
        assert compute_retention_action(MemoryClass.DURABLE_FACT, 0.65) == RetentionAction.PERSIST_DECAYING

    def test_medium_confidence_task_scoped_still_persists(self):
        assert compute_retention_action(MemoryClass.TASK_SCOPED, 0.65) == RetentionAction.PERSIST

    def test_low_confidence_event_only(self):
        assert compute_retention_action(MemoryClass.DURABLE_FACT, 0.30) == RetentionAction.EVENT_ONLY
