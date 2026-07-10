-- Migration 0009 — Contradiction sweep performance. Trigram GIN index on
-- documents.content so the sweep's candidate query is indexable.
--
-- The sweep's candidate query (governor.contradiction._CANDIDATE_PAIRS_SQL) is
-- a pg_trgm similarity SELF-JOIN on documents (`a.content % b.content`).
-- Without a trigram index, every pass brute-forces similarity across the full
-- O(n^2) same-scope pair space — a corpus of only ~1k eligible docs (~500k
-- pairs) blows through the pool-wide command_timeout=30, so every pass raises
-- TimeoutError and no pair is ever judged. Found in production upstream.
--
-- This index makes the `%` join indexable (gin_trgm_ops serves the % operator
-- and similarity() filtering). Code-side, the same fix gives the candidate
-- fetch a dedicated per-query timeout and a recency window — see
-- contradiction.py (CONTRADICTION_QUERY_TIMEOUT_S / CONTRADICTION_LOOKBACK_DAYS).
-- Non-CONCURRENT is deliberate: CREATE INDEX CONCURRENTLY cannot run inside a
-- DO/plpgsql block, and at the corpus sizes this fixes the build is subsecond.
--
-- OPERATOR FOLLOW-UP after this lands: if the sweep had been timing out, run
-- one full-corpus pass with CONTRADICTION_LOOKBACK_DAYS=0 — the default 30-day
-- window would otherwise leave the older backlog permanently unjudged.
--
-- Guarded on pg_trgm presence (see 0004 and the managed-Postgres gotcha in
-- README.md) — the guard only matters for fresh/minimal environments where the
-- extension hasn't been allow-listed yet.
--
-- Idempotent: safe to re-run.

-- Canary payload records what actually happened (index built vs guard
-- skipped), so the audit trail never claims an index that isn't there.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    CREATE INDEX IF NOT EXISTS idx_documents_content_trgm
      ON documents USING gin (content gin_trgm_ops);
    INSERT INTO agent_events (actor_peer, event_type, channel, payload)
    VALUES ('system', 'migration_applied', 'system',
            '{"migration": "0009_contradiction_sweep_perf", "indexes": ["idx_documents_content_trgm"]}'::jsonb);
  ELSE
    RAISE NOTICE 'pg_trgm extension not installed; skipping idx_documents_content_trgm (sweep stays slow until it exists)';
    INSERT INTO agent_events (actor_peer, event_type, channel, payload)
    VALUES ('system', 'migration_applied', 'system',
            '{"migration": "0009_contradiction_sweep_perf", "indexes": [], "skipped": "pg_trgm missing"}'::jsonb);
  END IF;
END
$$;

-- Visibility: echo whether the index now exists (should be 1 row).
SELECT indexname FROM pg_indexes
WHERE tablename = 'documents' AND indexname = 'idx_documents_content_trgm';
