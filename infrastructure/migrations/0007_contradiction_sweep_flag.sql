-- Migration 0007 — Contradiction detection. Gates the governor's nightly
-- contradiction sweep. Seeds OFF: with the flag off the in-service loop
-- (governor.contradiction.run_forever) idles. Flip to canary once verified.
--
-- Idempotent: safe to re-run.

INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('MEMORY_CONTRADICTION_SWEEP_ENABLED', false,
   'Nightly contradiction sweep: flag conflicting durable memories needs_review',
   'contradiction-migration')
ON CONFLICT (name) DO NOTHING;
