-- Manual backfill — default governance metadata for pre-existing documents.
-- Run AFTER the classifier has been observed healthy on new writes. Lives in
-- manual/ ON PURPOSE: it backfills existing data and validates the NOT VALID
-- constraints, so it should be applied deliberately, not as part of an
-- automated sweep over infrastructure/migrations/*.sql.
-- Idempotent: only touches rows still missing memory_class.

UPDATE documents
SET memory_class       = 'durable_fact',
    source_type        = COALESCE(source_type, 'derived'),
    verification_state = COALESCE(verification_state, 'unverified'),
    trust_score        = COALESCE(trust_score, 0.5),
    usage_success_count   = COALESCE(usage_success_count, 0),
    contradiction_count   = COALESCE(contradiction_count, 0)
WHERE memory_class IS NULL;

-- Validate the NOT VALID constraints now that every row is populated.
ALTER TABLE documents VALIDATE CONSTRAINT documents_memory_class_chk;
ALTER TABLE documents VALIDATE CONSTRAINT documents_source_type_chk;
ALTER TABLE documents VALIDATE CONSTRAINT documents_verification_state_chk;

-- Canary + audit trail: record how many rows the backfill touched.
INSERT INTO agent_events (actor_peer, event_type, channel, payload)
SELECT 'system', 'memory_backfill_applied', 'system',
       jsonb_build_object(
         'migration', '0003_memory_backfill',
         'documents_total', (SELECT count(*) FROM documents),
         'documents_classified', (SELECT count(*) FROM documents WHERE memory_class IS NOT NULL)
       );
