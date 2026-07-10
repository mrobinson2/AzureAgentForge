-- Overlay 0003 — seeds MEMORY_DIGEST_ENABLED (OFF), mirroring the canonical
-- infrastructure/migrations/0010_memory_digest_flag.sql so a self-provisioning
-- deployment (migrate.apply() on startup) has the flag row without a separate
-- pipeline stage. Both files are ON CONFLICT DO NOTHING, so applying either
-- (or both, in either order) is safe.
--
-- The flag gates only whether /digest folds the review-queue listing in as an
-- extra section; /memory-digest itself is always available for preview.
--
-- Idempotent: safe to re-run.

INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('MEMORY_DIGEST_ENABLED', false,
   'Fold the per-workspace review-queue listing into the daily memory digest',
   'memory-digest-migration')
ON CONFLICT (name) DO NOTHING;
