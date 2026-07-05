-- Migration 0002 — Memory classes schema. See docs/design/memory-system.md.
-- Idempotent: safe to re-run. Apply against the Honcho Postgres (see
-- infrastructure/migrations/README.md).
--
-- NOTE: Honcho's documents/messages tables live in the Honcho schema of the
-- shared server. ADD COLUMN IF NOT EXISTS keeps re-runs clean; constraints are
-- guarded with a DO block because ADD CONSTRAINT has no IF NOT EXISTS form.

-- 1. documents — governance metadata
ALTER TABLE documents ADD COLUMN IF NOT EXISTS memory_class text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS memory_scope_kind text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS memory_scope_id text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS verification_state text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS confidence_score numeric;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS trust_score numeric;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS half_life_days numeric;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_by_peer text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_accessed_at timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_confirmed_at timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
-- documents.id is a 21-char nanoid (TEXT) in honcho's schema, not uuid
ALTER TABLE documents ADD COLUMN IF NOT EXISTS promotion_source_doc_id text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS review_note text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS usage_success_count integer DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS contradiction_count integer DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_always_on_candidate boolean DEFAULT false;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS planner_hint text;

-- 2. messages — class tagging
ALTER TABLE messages ADD COLUMN IF NOT EXISTS memory_class text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS memory_scope_kind text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS memory_scope_id text;

-- 3. session_memory — Plane D separate physical store
CREATE TABLE IF NOT EXISTS session_memory (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- honcho keys workspaces by unique name (text), not uuid
  workspace_name text NOT NULL,
  session_id text NOT NULL,
  peer_id text,
  memory_scope_kind text NOT NULL DEFAULT 'session',
  memory_scope_id text NOT NULL,
  content text NOT NULL,
  source_type text,
  confidence_score numeric,
  created_by_peer text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  metadata jsonb DEFAULT '{}'::jsonb
);

-- 4. Indexes
CREATE INDEX IF NOT EXISTS idx_documents_memory_class ON documents(memory_class);
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(memory_scope_kind, memory_scope_id) WHERE memory_scope_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_expires_at ON documents(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_verification_state ON documents(verification_state);
CREATE INDEX IF NOT EXISTS idx_documents_superseded_at ON documents(superseded_at) WHERE superseded_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_last_confirmed_at ON documents(last_confirmed_at) WHERE last_confirmed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_always_on_candidate ON documents(is_always_on_candidate) WHERE is_always_on_candidate = true;
CREATE INDEX IF NOT EXISTS idx_session_memory_session ON session_memory(workspace_name, session_id);
CREATE INDEX IF NOT EXISTS idx_session_memory_expires_at ON session_memory(expires_at);

-- 5. Constraints — guarded; NOT VALID so pre-existing unclassified
-- rows (NULL memory_class until backfill 0003) don't block the deploy.
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
END
$$;

-- 6. Canary: prove the migration ran end-to-end (Phase 0 pattern).
INSERT INTO agent_events (actor_peer, event_type, channel, payload)
VALUES ('system', 'migration_applied', 'system',
        '{"migration": "0002_memory_classes", "tables": ["documents", "messages", "session_memory"]}'::jsonb);
