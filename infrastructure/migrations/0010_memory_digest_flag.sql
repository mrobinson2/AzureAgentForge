-- Migration 0010 — Daily memory review-queue digest. Seeds the gate flag OFF.
--
-- Adds a per-workspace LISTING digest (governor.memory_digest + the
-- /memory-digest endpoint) on top of the existing one-line activity counter
-- (digest.py / /digest): pending pin-candidates, memories the contradiction
-- sweep flagged needs_review, and memories expiring soon (mirrors the TTL
-- sweeper's own criteria) — every section capped to top-N with a "+N more"
-- line so a large backlog never walls-of-text the operator. Read-only
-- throughout: nothing this migration gates ever writes memory state.
--
-- Enforcement point: /memory-digest is always available for operator preview
-- (no flag check). This flag only gates whether /digest folds the listing in
-- as an extra section (which digest_post then delivers to the webhook). With
-- the flag off, /digest's response is byte-for-byte unchanged.
--
-- Idempotent: safe to re-run.

INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('MEMORY_DIGEST_ENABLED', false,
   'Fold the per-workspace review-queue listing into the daily memory digest',
   'memory-digest-migration')
ON CONFLICT (name) DO NOTHING;
