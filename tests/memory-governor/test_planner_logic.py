"""Offline tests for planner scope filtering + diversity guard (no DB)."""

from governor.memory.planner import (
    RetrievalPackage,
    RetrievalRequest,
    _diversity_penalties,
    _in_scope,
    _injected_doc_ids,
    _select_failure_lessons,
    _vec_literal,
)


def req(**over) -> RetrievalRequest:
    base = dict(query="what branch", workspace_name="dev", agent_slug="orchestrator")
    base.update(over)
    return RetrievalRequest(**base)


def row(**over) -> dict:
    base = dict(memory_class="durable_fact", memory_scope_kind=None, memory_scope_id=None)
    base.update(over)
    return base


class TestScopeFilter:
    def test_workspace_memory_always_in_scope(self):
        assert _in_scope(row(), req()) is True
        assert _in_scope(row(memory_scope_kind="workspace"), req()) is True

    def test_task_scoped_invisible_outside_scope(self):
        r = row(memory_class="task_scoped", memory_scope_kind="task", memory_scope_id="task-1")
        assert _in_scope(r, req()) is False
        assert _in_scope(r, req(active_scope_kind="task", active_scope_id="task-2")) is False

    def test_task_scoped_visible_in_matching_scope(self):
        r = row(memory_class="task_scoped", memory_scope_kind="task", memory_scope_id="task-1")
        assert _in_scope(r, req(active_scope_kind="task", active_scope_id="task-1")) is True

    def test_peer_scoped_only_for_that_peer(self):
        r = row(memory_scope_kind="peer", memory_scope_id="coder")
        assert _in_scope(r, req()) is False
        assert _in_scope(r, req(agent_slug="coder")) is True

    def test_task_scoped_missing_scope_id_never_visible(self):
        r = row(memory_class="task_scoped", memory_scope_kind="task", memory_scope_id=None)
        assert _in_scope(r, req(active_scope_kind="task", active_scope_id=None)) is False


class TestDiversityGuard:
    def test_near_duplicates_penalized(self):
        rows = [
            {"id": "a", "content": "the deploy target branch is feature login revamp"},
            {"id": "b", "content": "the deploy target branch is feature login revamp today"},
            {"id": "c", "content": "user prefers terse answers in chat"},
        ]
        p = _diversity_penalties(rows)
        assert p["a"] == 1.0
        assert p["b"] == 0.2  # near-dup of a
        assert p["c"] == 1.0

    def test_all_distinct_unpenalized(self):
        rows = [
            {"id": "a", "content": "alpha bravo charlie delta"},
            {"id": "b", "content": "echo foxtrot golf hotel"},
        ]
        p = _diversity_penalties(rows)
        assert all(v == 1.0 for v in p.values())


def _lrow(i, content="the staging deploy needs the migration applied first",
          state="unverified"):
    return {"id": f"d{i}", "content": content, "memory_class": "durable_fact",
            "verification_state": state}


class TestFailureLessonSelection:
    """The bounded, deduped failure-lesson injection set."""

    def test_excludes_already_injected_ids(self):
        out = _select_failure_lessons([_lrow(1), _lrow(2)], exclude_ids={"d1"})
        assert [m["doc_id"] for m in out] == ["d2"]

    def test_respects_max_count(self):
        rows = [_lrow(i, content=f"distinct lesson number {i}") for i in range(20)]
        out = _select_failure_lessons(rows, exclude_ids=set(), max_count=3,
                                      token_ceiling=10_000)
        assert len(out) == 3

    def test_respects_token_ceiling(self):
        big = "x" * 1000  # ~250 tokens (len//4)
        rows = [_lrow(i, content=big) for i in range(10)]
        out = _select_failure_lessons(rows, exclude_ids=set(), max_count=8,
                                      token_ceiling=300)
        assert len(out) == 1  # one fits; a second would exceed 300

    def test_preserves_order_and_shape(self):
        rows = [_lrow(1, state="confirmed"), _lrow(2, content="another distinct lesson")]
        out = _select_failure_lessons(rows, exclude_ids=set())
        assert out[0]["doc_id"] == "d1"
        assert out[0]["verification_state"] == "confirmed"
        assert out[0]["memory_class"] == "durable_fact"
        assert "content" in out[0]


class TestVecLiteral:
    """The pgvector text literal for the ::vector cast."""

    def test_basic_format(self):
        assert _vec_literal([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"

    def test_coerces_to_float_and_brackets(self):
        out = _vec_literal([1, 2, 3])
        assert out.startswith("[") and out.endswith("]")
        assert out == "[1.0,2.0,3.0]"

    def test_round_trips_through_json(self):
        import json

        vec = [0.0123456789, -0.5, 1.0]
        parsed = json.loads(_vec_literal(vec))
        assert parsed == vec  # full precision, valid array syntax


class TestInjectedDocIds:
    """The durable ids credited for a successful run (Plane A + Plane C +
    failure lessons; Plane D excluded; order-preserving, de-duped)."""

    def test_collects_across_durable_planes(self):
        pkg = RetrievalPackage(
            enabled=True,
            plane_a=[{"doc_id": "a1"}],
            plane_c=[{"doc_id": "c1"}, {"doc_id": "c2"}],
            plane_d=[{"id": "d1"}],  # ephemeral — must be excluded
            failure_lessons=[{"doc_id": "f1"}],
        )
        assert _injected_doc_ids(pkg) == ["a1", "c1", "c2", "f1"]

    def test_dedups_preserving_order(self):
        pkg = RetrievalPackage(
            enabled=True,
            plane_a=[{"doc_id": "x"}],
            plane_c=[{"doc_id": "x"}, {"doc_id": "y"}],
            failure_lessons=[{"doc_id": "y"}],
        )
        assert _injected_doc_ids(pkg) == ["x", "y"]

    def test_empty_package(self):
        assert _injected_doc_ids(RetrievalPackage(enabled=True)) == []
