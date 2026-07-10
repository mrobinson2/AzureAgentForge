"""Offline tests for contradiction detection — pure units, plus a fake-pool
check that the sweep's candidate query stays bounded (dedicated timeout +
recency window)."""

import asyncio
from datetime import datetime, timezone, timedelta

from governor import contradiction, db, llm

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FakePool:
    """Queues per-call fetch results in order; records SQL, args, AND kwargs
    of every fetch so tests can assert on the per-query timeout."""

    def __init__(self, fetch_results=None):
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetch_kwargs: list[dict] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self._fetch_results = list(fetch_results or [])

    async def fetch(self, sql, *args, **kwargs):
        self.fetch_calls.append((sql, args))
        self.fetch_kwargs.append(kwargs)
        return self._fetch_results.pop(0) if self._fetch_results else []

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


def _patch_db(monkeypatch, pool, flag_values=None):
    """flag_values: dict[name] -> bool; missing names default False."""
    events: list[tuple[str, str, dict]] = []
    flag_values = flag_values or {}

    async def _pool():
        return pool

    async def _flag(name):
        return flag_values.get(name, False)

    async def _emit(event_type, actor, payload, **kwargs):
        events.append((event_type, actor, payload))

    monkeypatch.setattr(db, "pool", _pool)
    monkeypatch.setattr(db, "flag_enabled", _flag)
    monkeypatch.setattr(db, "emit_event", _emit)
    return events


class TestParseContradictionOutcome:
    def test_clean_words(self):
        for w in ("none", "supersede", "scope_refine", "coexist", "needs_review"):
            assert llm.parse_contradiction_outcome(w) == w

    def test_case_and_punctuation_tolerant(self):
        assert llm.parse_contradiction_outcome("Supersede.") == "supersede"
        assert llm.parse_contradiction_outcome("  NEEDS_REVIEW\n") == "needs_review"
        assert llm.parse_contradiction_outcome("scope_refine — because ...") == "scope_refine"

    def test_noise_defaults_to_none(self):
        assert llm.parse_contradiction_outcome("") == "none"
        assert llm.parse_contradiction_outcome("   ") == "none"
        assert llm.parse_contradiction_outcome("banana") == "none"


class TestFlaggingPolicy:
    def test_only_real_conflicts_flag(self):
        assert "supersede" in contradiction.FLAGGING_OUTCOMES
        assert "scope_refine" in contradiction.FLAGGING_OUTCOMES
        assert "needs_review" in contradiction.FLAGGING_OUTCOMES
        # coexist / none never flag
        assert "coexist" not in contradiction.FLAGGING_OUTCOMES
        assert "none" not in contradiction.FLAGGING_OUTCOMES


class TestPickLoser:
    def _pair(self, a_trust, b_trust, a_created=T0, b_created=T0):
        return {"a_id": "A", "b_id": "B", "a_trust": a_trust, "b_trust": b_trust,
                "a_created": a_created, "b_created": b_created}

    def test_lower_trust_loses(self):
        assert contradiction._pick_loser(self._pair(0.4, 0.9)) == ("A", "B")
        assert contradiction._pick_loser(self._pair(0.9, 0.4)) == ("B", "A")

    def test_trust_tie_older_loses(self):
        older, newer = T0 - timedelta(days=10), T0
        assert contradiction._pick_loser(
            self._pair(0.6, 0.6, a_created=older, b_created=newer)
        ) == ("A", "B")
        assert contradiction._pick_loser(
            self._pair(0.6, 0.6, a_created=newer, b_created=older)
        ) == ("B", "A")


class TestSweepQueryBounds:
    """Regression: the candidate self-join must carry its own generous timeout
    (never the pool-wide 30s default) and the recency window that keeps
    steady-state passes off the full O(n^2) pair space."""

    def _sweep(self, monkeypatch):
        pool = _FakePool(fetch_results=[[]])
        _patch_db(monkeypatch, pool, {"MEMORY_CONTRADICTION_SWEEP_ENABLED": True})
        asyncio.run(contradiction.sweep_once(workspace="ws-a"))
        return pool

    def test_candidate_fetch_uses_dedicated_timeout(self, monkeypatch):
        pool = self._sweep(monkeypatch)
        assert pool.fetch_kwargs[0] == {"timeout": contradiction.CONTRADICTION_QUERY_TIMEOUT_S}
        # the dedicated timeout must beat the pool-wide command_timeout=30
        assert contradiction.CONTRADICTION_QUERY_TIMEOUT_S > 30

    def test_candidate_fetch_passes_lookback_param(self, monkeypatch):
        pool = self._sweep(monkeypatch)
        sql, args = pool.fetch_calls[0]
        assert args == ("ws-a", contradiction.SIM_LOW, contradiction.SIM_HIGH,
                        contradiction.MAX_PAIRS_PER_SWEEP,
                        contradiction.CONTRADICTION_LOOKBACK_DAYS)
        # SQL must let lookback <= 0 disable the window for a full-corpus pass
        assert "$5::float8 <= 0" in sql
        assert "GREATEST(a.created_at, b.created_at)" in sql

    def test_lookback_defaults_bounded(self):
        # steady-state default is a bounded window, not a full-corpus join
        assert contradiction.CONTRADICTION_LOOKBACK_DAYS > 0
