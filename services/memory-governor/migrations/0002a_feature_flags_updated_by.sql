-- feature_flags.updated_by — make this chain self-consistent.
--
-- 0003 and 0004 seed their flags with an `updated_by` provenance value, but the
-- CREATE TABLE in 0001 never had that column: it only exists in the separate
-- infrastructure/migrations chain. So this chain worked wherever the infra
-- migrations had already run, and broke the moment it was applied on its own —
-- which is exactly what the governor does at startup. On a fresh database the
-- boot log read:
--
--   applying migration 0003_memory_digest_flag.sql
--   asyncpg.exceptions.UndefinedColumnError:
--     column "updated_by" of relation "feature_flags" does not exist
--
-- and apply() aborted, so 0004 never ran either.
--
-- Ordered 0002a so it lands before the first INSERT that needs the column,
-- rather than editing an already-shipped migration. Idempotent: a database that
-- got the column from the infra chain is untouched.

ALTER TABLE feature_flags ADD COLUMN IF NOT EXISTS updated_by text;
