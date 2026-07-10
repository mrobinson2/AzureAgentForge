"""Memory inspector summary — READ-ONLY operator aggregate.

Covers GET /memory/inspector-summary's aggregation shape: per-class /
per-verification-state / per-source-type counts, the embedding-sync block, and
the recent ranking-mode tally. No mutation logic is touched or tested here —
the endpoint adds no write paths. The handler is called directly (asyncio.run)
against a fake pool — no FastAPI TestClient / real DB needed.
"""

import asyncio

from governor import main as governor_main


class _FakePool:
    """Replays canned rows per fetch()/fetchval() call (in call order) and
    records the SQL + args of every call so tests can assert on the generated
    clauses."""

    def __init__(self, results=None, fetchval_results=None):
        self._results = list(results or [])
        self._fetchval_results = list(fetchval_results or [])
        self.fetch_calls = []
        self.fetchval_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self._results.pop(0) if self._results else []

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self._fetchval_results.pop(0) if self._fetchval_results else None


def _patch_pool(monkeypatch, pool):
    async def _fake_pool():
        return pool

    monkeypatch.setattr(governor_main.db, "pool", _fake_pool)


class TestMemoryInspectorSummary:
    def test_aggregates_all_sections(self, monkeypatch):
        class_rows = [{"key": "durable_fact", "n": 12}, {"key": "pinned", "n": 3}]
        vstate_rows = [{"key": "confirmed", "n": 10}, {"key": None, "n": 1}]
        source_rows = [{"key": "agent_observed", "n": 8}]
        ranking_rows = [{"ranking_mode": "vector", "n": 40}, {"ranking_mode": "trigram", "n": 5}]

        pool = _FakePool(
            results=[class_rows, vstate_rows, source_rows, ranking_rows],
            fetchval_results=[2, None],  # embedding_stats: pending, last_sync_at
        )
        _patch_pool(monkeypatch, pool)

        out = asyncio.run(governor_main.memory_inspector_summary(workspace_name="ws-a"))

        assert out["workspace_name"] == "ws-a"
        assert out["by_memory_class"] == {"durable_fact": 12, "pinned": 3}
        assert out["by_verification_state"] == {"confirmed": 10, "unknown": 1}
        assert out["by_source_type"] == {"agent_observed": 8}
        assert out["embedding"] == {"pending": 2, "last_sync_at": None}
        assert out["recent_ranking_modes"] == {"vector": 40, "trigram": 5}

        # every documents-scoped query must carry the workspace + exclude
        # deleted rows — the summary must never silently roll up cross-tenant
        for sql, args in pool.fetch_calls[:3]:
            assert "workspace_name = $1" in sql
            assert "deleted_at IS NULL" in sql
            assert args[0] == "ws-a"

    def test_ranking_tally_is_windowed_and_injection_scoped(self, monkeypatch):
        pool = _FakePool(results=[[], [], [], []], fetchval_results=[0, None])
        _patch_pool(monkeypatch, pool)
        asyncio.run(governor_main.memory_inspector_summary(workspace_name="ws-a"))
        sql, _ = pool.fetch_calls[3]
        assert "event_type = 'memory_injected'" in sql
        assert "interval '7 days'" in sql

    def test_workspace_name_has_no_default(self):
        import inspect

        sig = inspect.signature(governor_main.memory_inspector_summary)
        assert sig.parameters["workspace_name"].default is inspect.Parameter.empty
