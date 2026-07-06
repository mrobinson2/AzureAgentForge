"""Ranking-mode observability: Plane C must report which ranking path it took —
'vector' (hybrid pgvector), 'trigram' (flag off), or 'trigram_fallback' (flag on
but embed failed / hybrid SQL raised) — so the silent vector→trigram degradation
becomes visible in the retrieval package and the memory_injected event. Offline:
pool/flag/embedder faked, so no DB or embedding provider is touched."""

import asyncio

from governor import db, llm
from governor.memory import planner


class _FakePool:
    """Distinguishes hybrid vs trigram by arity: _HYBRID_SQL takes 6 params,
    _TRIGRAM_SQL takes 2."""

    def __init__(self, hybrid_raises=False):
        self.hybrid_raises = hybrid_raises
        self.hybrid_calls = 0
        self.trigram_calls = 0

    async def fetch(self, sql, *args):
        if len(args) == 6:
            self.hybrid_calls += 1
            if self.hybrid_raises:
                raise RuntimeError("vector extension missing")
            return []
        self.trigram_calls += 1
        return []


def _req():
    return planner.RetrievalRequest(
        query="what branch",
        workspace_name="dev",
        agent_slug="orchestrator",
    )


def _async_const(v):
    async def _f(*a, **k):
        return v
    return _f


def _patch(monkeypatch, pool, *, flag, embed):
    monkeypatch.setattr(db, "pool", _async_const(pool))
    monkeypatch.setattr(db, "flag_enabled", _async_const(flag))
    monkeypatch.setattr(llm, "embed", _async_const(embed))


class TestRankingMode:
    def test_flag_off_is_trigram(self, monkeypatch):
        pool = _FakePool()
        _patch(monkeypatch, pool, flag=False, embed=None)
        rows, mode = asyncio.run(planner._plane_c_candidates(_req()))
        assert mode == "trigram"
        assert pool.trigram_calls == 1 and pool.hybrid_calls == 0

    def test_flag_on_embed_none_is_fallback(self, monkeypatch):
        pool = _FakePool()
        _patch(monkeypatch, pool, flag=True, embed=None)
        rows, mode = asyncio.run(planner._plane_c_candidates(_req()))
        assert mode == "trigram_fallback"
        assert pool.trigram_calls == 1 and pool.hybrid_calls == 0

    def test_flag_on_hybrid_ok_is_vector(self, monkeypatch):
        pool = _FakePool()
        _patch(monkeypatch, pool, flag=True, embed=[0.1, 0.2])
        rows, mode = asyncio.run(planner._plane_c_candidates(_req()))
        assert mode == "vector"
        assert pool.hybrid_calls == 1 and pool.trigram_calls == 0

    def test_flag_on_hybrid_raises_is_fallback(self, monkeypatch):
        pool = _FakePool(hybrid_raises=True)
        _patch(monkeypatch, pool, flag=True, embed=[0.1, 0.2])
        rows, mode = asyncio.run(planner._plane_c_candidates(_req()))
        assert mode == "trigram_fallback"
        assert pool.hybrid_calls == 1 and pool.trigram_calls == 1


class TestPackageCarriesMode:
    def test_default_field_exists(self):
        pkg = planner.RetrievalPackage(enabled=False)
        assert pkg.ranking_mode == "trigram"
