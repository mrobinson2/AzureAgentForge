-- Migration 0006 — Vector retrieval. Seeds the gate flag OFF. The planner's
-- _plane_c_candidates blends pgvector cosine (over Honcho's documents.embedding,
-- HNSW-indexed) with pg_trgm only when this flag is on AND the query embeds;
-- otherwise it stays trigram-only. Flip to canary after the embedding key is
-- wired on the router.
--
-- Idempotent: safe to re-run.

INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('MEMORY_VECTOR_RETRIEVAL_ENABLED', false,
   'Plane C hybrid retrieval: pgvector cosine blended with pg_trgm',
   'vector-retrieval-migration')
ON CONFLICT (name) DO NOTHING;
