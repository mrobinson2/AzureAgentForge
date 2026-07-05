-- Migration 0001 — Foundation: the event spine + feature-flag registry.
-- See docs/design/memory-system.md (§11 event spine, §13 feature flags).
--
-- Creates the agent_events append-only log + a NOTIFY trigger, and the
-- feature_flags registry. Idempotent: safe to re-run.
--
-- Apply against the Postgres that backs Honcho (the governor reads/writes the
-- same database). On a hardened deployment that server is private-endpoint
-- only, so run this from a network-attached host. See
-- infrastructure/migrations/README.md for apply order and the managed-Postgres
-- pg_trgm note.

CREATE TABLE IF NOT EXISTS agent_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts          timestamptz NOT NULL DEFAULT now(),
  session_id  uuid,
  issue_id    text,
  thread_id   text,
  actor_peer  text NOT NULL,
  event_type  text NOT NULL,
  channel     text NOT NULL CHECK (channel IN ('cli','chat','voice','orchestrator','system')),
  payload     jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_events_ts      ON agent_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events (session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_events_issue   ON agent_events (issue_id)   WHERE issue_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_events_thread  ON agent_events (thread_id)  WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_events_type    ON agent_events (event_type);

-- NOTIFY consumers on insert. Payload is the event id only — consumers re-read
-- the row (at-least-once delivery, idempotent by id).
CREATE OR REPLACE FUNCTION notify_agent_event() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('agent_events_channel', NEW.id::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_events_notify ON agent_events;
CREATE TRIGGER trg_agent_events_notify
  AFTER INSERT ON agent_events
  FOR EACH ROW EXECUTE FUNCTION notify_agent_event();

CREATE TABLE IF NOT EXISTS feature_flags (
  name        text PRIMARY KEY,
  enabled     boolean NOT NULL DEFAULT false,
  description text,
  updated_by  text,
  updated_at  timestamptz DEFAULT now()
);

-- Every flag is seeded OFF. AGENT_EVENTS_ENABLED is the master gate for the
-- event spine; flip it on per environment as a deliberate post-canary step,
-- not at seed time. Vector retrieval, contradiction sweep, and skill autogen
-- flags are seeded by their own later migrations (0006/0007/0008).
INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('AGENT_EVENTS_ENABLED',               false, 'master gate for the agent_events spine + watchdog', 'migration-0001'),
  ('MEMORY_CLASSES_ENABLED',             false, 'classifier + admission + annotator',                'migration-0001'),
  ('MEMORY_PLANNER_ENABLED',             false, 'four-plane retrieval planner',                      'migration-0001'),
  ('MEMORY_SESSION_SEPARATION_ENABLED',  false, 'route ephemeral writes to session_memory (Plane D)','migration-0001'),
  ('MEMORY_TTL_SWEEPER_ENABLED',         false, 'nightly TTL sweep actually deletes',                'migration-0001')
ON CONFLICT (name) DO NOTHING;
