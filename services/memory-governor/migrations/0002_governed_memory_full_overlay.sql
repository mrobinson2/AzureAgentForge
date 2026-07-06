-- 0002 governed-memory full overlay: complete the documents overlay, add the
-- governor's two owned tables (session_memory, skill_candidates), and seed the
-- feature-flag spine OFF.
--
-- 0001 shipped the minimal overlay + flag table — enough for memory
-- export/curation. The retrieval planner, admission pipeline, scope-watcher and
-- skill-miner read columns and tables 0001 does not create, so enabling the
-- governor on 0001 alone 500s. This migration closes that gap so
-- MEMORY_PLANNER_ENABLED (and the loops) can be turned on for real (see #83).
-- Column set, enum values, index and constraint names mirror the canonical
-- infrastructure/migrations (0002_memory_classes + 0008_skill_autogen), the
-- vetted apply-by-hand set — this file makes the governor self-provision the
-- same schema on startup (governor.migrate), so no separate SQL step is needed.
--
-- TYPE RECONCILIATION (resolved): `documents.id` is a 21-char nanoid (TEXT) in
-- Honcho's schema, not uuid — so every `*_doc_id` reference column is text.
-- `deleted_at`, `sync_state`, `last_sync_at` are Honcho-native (its
-- external-embeddings migration), not created here. Everything below is
-- IF NOT EXISTS / guarded, so this is safe to (re-)apply and never clobbers
-- Honcho or the columns 0001 already added.

-- (a) complete the documents governed-memory overlay --------------------------
-- (memory_class, verification_state, memory_scope_kind/id, source_type,
--  created_by_peer, last_confirmed_at, expires_at, trust_score,
--  promotion_source_doc_id, superseded_by came in 0001.)
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confidence_score       numeric;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS half_life_days         numeric;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_accessed_at       timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS reviewed_at            timestamptz;
-- The planner filters on `superseded_at IS NULL`; 0001 only added the
-- `superseded_by` pointer. Both coexist (pointer + timestamp).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS superseded_at          timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS review_note            text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS usage_success_count    integer DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS contradiction_count    integer DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_always_on_candidate boolean DEFAULT false;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS planner_hint           text;

-- (b) messages overlay (Plane D reads class metadata off messages) ------------
ALTER TABLE messages ADD COLUMN IF NOT EXISTS memory_class      text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS memory_scope_kind text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS memory_scope_id   text;

-- (c) session_memory — Plane D separate physical store ------------------------
-- Honcho keys workspaces by unique name (text), not uuid.
CREATE TABLE IF NOT EXISTS session_memory (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_name    text NOT NULL,
  session_id        text NOT NULL,
  peer_id           text,
  memory_scope_kind text NOT NULL DEFAULT 'session',
  memory_scope_id   text NOT NULL,
  content           text NOT NULL,
  source_type       text,
  confidence_score  numeric,
  created_by_peer   text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  metadata          jsonb DEFAULT '{}'::jsonb
);

-- (d) skill_candidates — recurring procedural memory mined into skill drafts ---
-- The governor proposes; an operator/curator disposes. The miner never writes a
-- live skill file — it persists a candidate for review.
CREATE TABLE IF NOT EXISTS skill_candidates (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_slug        text NOT NULL,
  workspace_name    text,
  skill_name        text NOT NULL,
  skill_body        text NOT NULL,
  source_doc_ids    text[] NOT NULL DEFAULT '{}',
  -- Stable per-cluster key (the representative seed doc id) so re-running the
  -- miner never re-proposes the same cluster — enforced by the unique index.
  cluster_signature text NOT NULL,
  recurrence        integer NOT NULL DEFAULT 0,
  status            text NOT NULL DEFAULT 'pending_review',
  review_note       text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  reviewed_at       timestamptz
);

-- (e) indexes -----------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(memory_scope_kind, memory_scope_id) WHERE memory_scope_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_verification_state ON documents(verification_state);
CREATE INDEX IF NOT EXISTS idx_documents_superseded_at ON documents(superseded_at) WHERE superseded_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_last_confirmed_at ON documents(last_confirmed_at) WHERE last_confirmed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_always_on_candidate ON documents(is_always_on_candidate) WHERE is_always_on_candidate = true;
CREATE INDEX IF NOT EXISTS idx_session_memory_session ON session_memory(workspace_name, session_id);
CREATE INDEX IF NOT EXISTS idx_session_memory_expires_at ON session_memory(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_candidates_sig ON skill_candidates (agent_slug, cluster_signature);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_status ON skill_candidates (status);

-- (f) value constraints — guarded; documents CHECKs are NOT VALID so pre-existing
-- unclassified rows (NULL memory_class until a backfill) don't block the deploy.
-- NULLs pass CHECK constraints, so post-backfill rows are fully enforced.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_memory_class_chk') THEN
    ALTER TABLE documents ADD CONSTRAINT documents_memory_class_chk
      CHECK (memory_class IN ('pinned','durable_fact','user_preference','task_scoped','decaying')) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_source_type_chk') THEN
    ALTER TABLE documents ADD CONSTRAINT documents_source_type_chk
      CHECK (source_type IN ('user_asserted','operator_entered','agent_observed','derived','external_import')) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_verification_state_chk') THEN
    ALTER TABLE documents ADD CONSTRAINT documents_verification_state_chk
      CHECK (verification_state IN ('unverified','inferred','confirmed','disputed','superseded','needs_review')) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'session_memory_scope_chk') THEN
    ALTER TABLE session_memory ADD CONSTRAINT session_memory_scope_chk
      CHECK (memory_scope_kind = 'session');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'skill_candidates_status_chk') THEN
    ALTER TABLE skill_candidates ADD CONSTRAINT skill_candidates_status_chk
      CHECK (status IN ('pending_review','approved','rejected','materialized'));
  END IF;
END$$;

-- (g) seed the feature-flag spine OFF -----------------------------------------
-- Every governed behavior is gated; each seeds disabled so this migration is
-- inert until an operator flips a flag. flag_enabled() fails closed, so an
-- absent row already reads false — these rows just make the switches visible.
-- (feature_flags here is the governor-owned table from 0001: name/enabled/description.)
INSERT INTO feature_flags (name, enabled, description) VALUES
  ('AGENT_EVENTS_ENABLED',               false, 'agent_events observability spine'),
  ('MEMORY_CLASSES_ENABLED',             false, 'six-class admission + governance metadata'),
  ('MEMORY_PLANNER_ENABLED',             false, 'four-plane governed-retrieval injection'),
  ('MEMORY_SESSION_SEPARATION_ENABLED',  false, 'Plane D session-scoped memory store'),
  ('MEMORY_TTL_SWEEPER_ENABLED',         false, 'decaying-memory TTL sweeper'),
  ('MEMORY_VECTOR_RETRIEVAL_ENABLED',    false, 'hybrid pgvector ranking in Plane C'),
  ('MEMORY_CONTRADICTION_SWEEP_ENABLED', false, 'contradiction detection sweep'),
  ('SKILL_AUTOGEN_ENABLED',              false, 'mine recurring procedural memory into skill candidates')
ON CONFLICT (name) DO NOTHING;

-- (h) canary: prove the migration ran end-to-end.
INSERT INTO agent_events (actor_peer, event_type, channel, payload)
VALUES ('system', 'migration_applied', 'system',
        '{"migration": "0002_governed_memory_full_overlay", "tables": ["documents", "messages", "session_memory", "skill_candidates"]}'::jsonb);
