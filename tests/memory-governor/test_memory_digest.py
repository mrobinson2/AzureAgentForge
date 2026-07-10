"""Daily memory review-queue digest — offline tests.

Pure-renderer tests for governor.memory_digest (no DB, no HTTP, mirrors
test_digest.py), plus handler-level tests for GET /memory-digest, the
db.memory_digest_rollup queries, and the MEMORY_DIGEST_ENABLED /digest
fold-in — called directly (asyncio.run) against a fake pool, the
test_memory_inspector.py convention.
"""

import asyncio

from governor import db as governor_db
from governor import main as governor_main
from governor.memory_digest import (
    DEFAULT_LIMIT,
    clamp_limit,
    format_memory_digest,
)


def _pending(n, ws="tenant-a"):
    return [
        {
            "id": f"doc-{ws}-{i}",
            "workspace_name": ws,
            "memory_class": "durable_fact",
            "content": f"pending candidate {i} for {ws}",
            "created_at": f"2026-07-{i + 1:02d}T00:00:00+00:00",
        }
        for i in range(n)
    ]


def _review(n, ws="tenant-a"):
    return [
        {
            "id": f"rev-{ws}-{i}",
            "workspace_name": ws,
            "memory_class": "durable_fact",
            "content": f"flagged memory {i}",
            "review_note": f"contradiction sweep: conflicts with keeper-{i} (suggested: supersede)",
        }
        for i in range(n)
    ]


def _expiring(n, ws="tenant-a"):
    return [
        {
            "id": f"exp-{ws}-{i}",
            "workspace_name": ws,
            "memory_class": "task_scoped",
            "content": f"expiring memory {i}",
            "expires_at": f"2026-07-{i + 1:02d}T00:00:00+00:00",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# clamp_limit
# ---------------------------------------------------------------------------


def test_clamp_limit_defaults_and_bounds():
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit("nope") == DEFAULT_LIMIT
    assert clamp_limit("5") == 5
    assert clamp_limit(0) == 1
    assert clamp_limit(-3) == 1
    assert clamp_limit(9999) == 50  # MAX_LIMIT


# ---------------------------------------------------------------------------
# Empty queue -> graceful message
# ---------------------------------------------------------------------------


def test_empty_queue_is_graceful():
    out = format_memory_digest({})
    assert "Nothing pending — clean review queue." in out
    assert "Memory review queue" in out


def test_empty_lists_treated_as_empty_queue():
    out = format_memory_digest({"pending_candidates": [], "needs_review": [], "expiring": []})
    assert "Nothing pending" in out


# ---------------------------------------------------------------------------
# Cap at N + "+M more"
# ---------------------------------------------------------------------------


def test_pending_caps_at_limit_and_reports_overflow():
    out = format_memory_digest({"limit": 5, "pending_candidates": _pending(12)})
    assert "12 total" in out
    assert "…+7 more pending" in out
    assert "pc-memory list --pin-candidates" in out
    # exactly 5 candidate lines rendered
    assert out.count("[durable_fact]") == 5


def test_pending_under_limit_has_no_overflow_line():
    out = format_memory_digest({"limit": 10, "pending_candidates": _pending(3)})
    assert "3 total" in out
    assert "more pending" not in out


def test_needs_review_caps_and_overflow():
    out = format_memory_digest({"limit": 3, "needs_review": _review(8)})
    assert "8 total" in out
    assert "…+5 more flagged needs_review" in out
    # the sweep's suggestion is surfaced via the review note
    assert "suggested: supersede" in out


def test_expiring_caps_and_overflow():
    out = format_memory_digest({"limit": 2, "expiring": _expiring(6)})
    assert "6 total" in out
    assert "…+4 more expiring" in out
    assert "expires " in out


def test_default_limit_applies_when_unspecified():
    out = format_memory_digest({"pending_candidates": _pending(DEFAULT_LIMIT + 5)})
    assert "…+5 more pending" in out


# ---------------------------------------------------------------------------
# Per-workspace grouping
# ---------------------------------------------------------------------------


def test_pending_grouped_by_workspace():
    items = _pending(2, ws="tenant-a") + _pending(2, ws="tenant-b")
    out = format_memory_digest({"limit": 10, "pending_candidates": items})
    assert "_tenant-a_" in out
    assert "_tenant-b_" in out
    # tenant-a's group appears before tenant-b's (alphabetical, stable)
    assert out.index("_tenant-a_") < out.index("_tenant-b_")


def test_pending_unscoped_workspace_falls_back():
    items = [{"id": "x", "memory_class": "durable_fact", "content": "no workspace here"}]
    out = format_memory_digest({"pending_candidates": items})
    assert "_(unscoped)_" in out


def test_needs_review_grouped_by_workspace():
    items = _review(1, ws="tenant-a") + _review(1, ws="tenant-b")
    out = format_memory_digest({"limit": 10, "needs_review": items})
    assert "_tenant-a_" in out
    assert "_tenant-b_" in out


def test_expiring_grouped_by_workspace():
    items = _expiring(1, ws="tenant-a") + _expiring(1, ws="tenant-b")
    out = format_memory_digest({"limit": 10, "expiring": items})
    assert "_tenant-a_" in out
    assert "_tenant-b_" in out


# ---------------------------------------------------------------------------
# Combined / misc
# ---------------------------------------------------------------------------


def test_all_three_sections_render_when_present():
    out = format_memory_digest({
        "limit": 10,
        "pending_candidates": _pending(1),
        "needs_review": _review(1),
        "expiring": _expiring(1),
    })
    assert "Pending pin-candidates" in out
    assert "Flagged needs_review" in out
    assert "Expiring soon" in out


def test_only_present_sections_render():
    out = format_memory_digest({"limit": 10, "expiring": _expiring(1)})
    assert "Expiring soon" in out
    assert "Pending pin-candidates" not in out
    assert "Flagged needs_review" not in out


def test_never_writes_anything_pure_function_contract():
    # format_memory_digest must not mutate its input (read-only contract).
    stats = {"limit": 2, "pending_candidates": _pending(5)}
    before = [dict(x) for x in stats["pending_candidates"]]
    format_memory_digest(stats)
    assert stats["pending_candidates"] == before


def test_missing_review_note_renders_placeholder():
    items = [{"id": "r", "workspace_name": "tenant-a", "memory_class": "durable_fact",
              "content": "flagged", "review_note": None}]
    out = format_memory_digest({"needs_review": items})
    assert "no review note" in out


def test_long_content_is_snippeted():
    long_content = "x" * 500
    out = format_memory_digest({"pending_candidates": [
        {"id": "1", "workspace_name": "tenant-a", "memory_class": "durable_fact", "content": long_content}
    ]})
    assert "x" * 500 not in out
    assert "…" in out


# ---------------------------------------------------------------------------
# Handler + rollup + fold-in (fake pool, no FastAPI TestClient / real DB)
# ---------------------------------------------------------------------------


class _FakePool:
    """Replays canned rows per fetch()/fetchrow() call (in call order) and
    records the SQL of every call so tests can assert on the clauses."""

    def __init__(self, results=None, fetchrow_results=None):
        self._results = list(results or [])
        self._fetchrow_results = list(fetchrow_results or [])
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._results.pop(0) if self._results else []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        return self._fetchrow_results.pop(0) if self._fetchrow_results else None


def _patch_pool(monkeypatch, pool):
    async def _fake_pool():
        return pool

    # governor_main.db and governor_db are the same module object; patching
    # once covers both the handler and db.memory_digest_rollup.
    monkeypatch.setattr(governor_db, "pool", _fake_pool)


def _patch_flag(monkeypatch, enabled):
    async def _fake_flag(name):
        return enabled

    monkeypatch.setattr(governor_db, "flag_enabled", _fake_flag)


def _row(**kw):
    base = {"id": "doc-1", "workspace_name": "tenant-a", "memory_class": "durable_fact",
            "content": "some memory", "created_at": None, "review_note": None,
            "reviewed_at": None, "expires_at": None}
    base.update(kw)
    return base


class TestMemoryDigestRollup:
    def test_three_readonly_queries_with_expected_clauses(self, monkeypatch):
        pool = _FakePool(results=[[_row()], [_row(id="rev-1")], [_row(id="exp-1")]])
        _patch_pool(monkeypatch, pool)

        out = asyncio.run(governor_db.memory_digest_rollup())

        assert [d["id"] for d in out["pending_candidates"]] == ["doc-1"]
        assert [d["id"] for d in out["needs_review"]] == ["rev-1"]
        assert [d["id"] for d in out["expiring"]] == ["exp-1"]

        pending_sql, review_sql, expiring_sql = (c[0] for c in pool.fetch_calls)
        assert "pin_candidate" in pending_sql
        assert "verification_state <> 'confirmed'" in pending_sql
        assert "verification_state = 'needs_review'" in review_sql
        assert "expires_at <= now() + interval '7 days'" in expiring_sql
        # every query is fetch-capped and excludes deleted rows
        for sql, args in pool.fetch_calls:
            assert "deleted_at IS NULL" in sql
            assert args == (governor_db.MEMORY_DIGEST_FETCH_CAP,)

    def test_fails_open_to_empty_lists(self, monkeypatch):
        async def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(governor_db, "pool", _boom)
        out = asyncio.run(governor_db.memory_digest_rollup())
        assert out == {"pending_candidates": [], "needs_review": [], "expiring": []}


class TestMemoryDigestEndpoint:
    def test_returns_sections_and_rendered_text(self, monkeypatch):
        pool = _FakePool(results=[[_row(content="pending one")], [], []])
        _patch_pool(monkeypatch, pool)

        out = asyncio.run(governor_main.memory_digest_endpoint(limit=5))

        assert out["limit"] == 5
        assert out["pending_candidates"][0]["content"] == "pending one"
        assert out["needs_review"] == []
        assert out["expiring"] == []
        assert "Memory review queue" in out["text"]
        assert "pending one" in out["text"]

    def test_limit_is_clamped(self, monkeypatch):
        pool = _FakePool(results=[[], [], []])
        _patch_pool(monkeypatch, pool)
        out = asyncio.run(governor_main.memory_digest_endpoint(limit=9999))
        assert out["limit"] == 50  # MAX_LIMIT


class TestDigestFoldIn:
    _EV_ROWS = []  # no memory_* events in the window
    _QUEUE_ROW = {"pin_candidates": 0, "needs_review": 0}

    def test_flag_off_digest_unchanged(self, monkeypatch):
        pool = _FakePool(results=[self._EV_ROWS], fetchrow_results=[dict(self._QUEUE_ROW)])
        _patch_pool(monkeypatch, pool)
        _patch_flag(monkeypatch, False)

        out = asyncio.run(governor_main.memory_digest(window_hours=24))

        assert "review_queue" not in out
        assert "Memory review queue" not in out["text"]
        # only the events fetch ran — the rollup queries never fired
        assert len(pool.fetch_calls) == 1

    def test_flag_on_appends_review_queue(self, monkeypatch):
        pool = _FakePool(
            results=[self._EV_ROWS, [_row(content="pending one")], [], []],
            fetchrow_results=[dict(self._QUEUE_ROW)],
        )
        _patch_pool(monkeypatch, pool)
        _patch_flag(monkeypatch, True)

        out = asyncio.run(governor_main.memory_digest(window_hours=24))

        assert out["review_queue"]["pending_candidates"][0]["content"] == "pending one"
        # the one-line digest still leads; the listing is appended below it
        assert out["text"].startswith("📋 Memory digest")
        assert "Memory review queue" in out["text"]
        assert "pending one" in out["text"]
