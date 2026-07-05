-- Migration 0008 — Automatic repetition detection -> skill autogen (companion
-- to the model-driven skill_manage tool). The governor's skill_miner loop finds
-- PROCEDURAL memories a single agent recorded across MULTIPLE distinct task
-- scopes (a recurring procedure), asks the classifier tier to crystallize them
-- into a reusable skill, and persists it as a CANDIDATE for review. Per the
-- platform's "governor proposes, operator/curator disposes" stance (mirrors
-- pin_candidates / needs_review / contradiction) it NEVER writes a live skill
-- file directly — the skill-curator job materializes approved candidates.
--
-- Idempotent: safe to re-run.

-- Candidate skills mined from recurring procedural memory.
CREATE TABLE IF NOT EXISTS skill_candidates (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_slug       text NOT NULL,
  workspace_name   text,
  skill_name       text NOT NULL,
  skill_body       text NOT NULL,
  source_doc_ids   text[] NOT NULL DEFAULT '{}',
  -- Stable per-cluster key (the representative seed doc id) so re-running the
  -- miner never re-proposes the same cluster — enforced by the unique index.
  cluster_signature text NOT NULL,
  recurrence       integer NOT NULL DEFAULT 0,
  status           text NOT NULL DEFAULT 'pending_review',
  review_note      text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  reviewed_at      timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_candidates_sig
  ON skill_candidates (agent_slug, cluster_signature);
CREATE INDEX IF NOT EXISTS idx_skill_candidates_status
  ON skill_candidates (status);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'skill_candidates_status_chk') THEN
    ALTER TABLE skill_candidates ADD CONSTRAINT skill_candidates_status_chk
      CHECK (status IN ('pending_review','approved','rejected','materialized'));
  END IF;
END$$;

-- Gate the miner loop. With the flag off, skill_miner.mine_once() no-ops
-- (the in-service loop idles, mirroring the contradiction sweep). Flip to
-- canary once the skill-enabled agents have accumulated procedural memory.
INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('SKILL_AUTOGEN_ENABLED', false,
   'Automatic repetition detection: mine recurring procedural memory into skill candidates',
   'skill-autogen-migration')
ON CONFLICT (name) DO NOTHING;
