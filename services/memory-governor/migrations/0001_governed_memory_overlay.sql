-- 0001 governed-memory overlay on the shared Honcho Postgres.
--
-- The governor does NOT own its own database — it operates on Honcho's Postgres
-- (the `documents`/`collections` tables + their 1536-dim embeddings are created
-- by Honcho's own migrations). This overlay adds:
--   (a) the governed-memory columns the governor reads/writes on `documents`, and
--   (b) the governor-owned tables (feature_flags, agent_events).
--
-- Idempotent (IF NOT EXISTS everywhere) so it is safe to (re-)apply and never
-- clobbers Honcho's columns. Applied by `python -m governor.migrate` (see
-- migrations/README.md), which the governor runs on startup.
--
-- ⚠️ TYPE RECONCILIATION: column TYPES below are derived from the governor's
-- query usage, not from the canonical source migrations (in the private upstream platform). Before enabling the
-- governor in production, reconcile `documents.id`'s type (UUID vs text) so the
-- *_doc_id references match, and confirm against the live Honcho schema.

-- (a) governed-memory columns on Honcho's documents table ---------------------
ALTER TABLE documents ADD COLUMN IF NOT EXISTS memory_class            text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS verification_state      text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS memory_scope_kind       text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS memory_scope_id         text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type             text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_by_peer         text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_confirmed_at       timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS expires_at              timestamptz;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS trust_score             real;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS superseded_by           text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS promotion_source_doc_id text;

CREATE INDEX IF NOT EXISTS documents_memory_class_idx ON documents (memory_class);
CREATE INDEX IF NOT EXISTS documents_expires_at_idx   ON documents (expires_at);

-- (b) governor-owned tables ---------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_flags (
    name        text PRIMARY KEY,
    enabled     boolean     NOT NULL DEFAULT false,
    description text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_events (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    actor_peer  text        NOT NULL,
    event_type  text        NOT NULL,
    channel     text        NOT NULL DEFAULT 'system',
    session_id  uuid,
    issue_id    text,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS agent_events_type_time_idx ON agent_events (event_type, created_at);

-- TODO(0002): port `session_memory` and `skill_candidates` from the canonical
-- canonical source migrations — their exact columns/types/indexes can't be safely derived
-- from the governor's query usage alone. The scope-watcher and skill-miner loops
-- stay idle until those exist (they fail closed), so the overlay above is enough
-- for memory export/curation + the flag spine; the full feature set needs 0002.
