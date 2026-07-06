"""Embedding-staleness stats for /healthz. Pure helper in db.py so it's testable
without FastAPI or a live DB: pending = documents awaiting the embed worker
(sync_state='pending'), last_sync_at = freshest sync timestamp. Errors never
propagate — healthz must not die on a telemetry query."""

import asyncio
from datetime import datetime, timezone

from governor import db


class _FakePool:
    def __init__(self, values=None, raises=False):
        self.values = list(values or [])
        self.raises = raises
        self.queries = []

    async def fetchval(self, sql, *args):
        self.queries.append(sql)
        if self.raises:
            raise RuntimeError("db down")
        return self.values.pop(0)


class TestEmbeddingStats:
    def test_reports_pending_and_last_sync(self):
        ts = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
        pool = _FakePool(values=[7, ts])
        out = asyncio.run(db.embedding_stats(pool))
        assert out == {"pending": 7, "last_sync_at": ts.isoformat()}
        assert any("sync_state" in q for q in pool.queries)

    def test_null_last_sync(self):
        pool = _FakePool(values=[0, None])
        out = asyncio.run(db.embedding_stats(pool))
        assert out == {"pending": 0, "last_sync_at": None}

    def test_error_returns_error_marker(self):
        pool = _FakePool(raises=True)
        out = asyncio.run(db.embedding_stats(pool))
        assert out == "error"
