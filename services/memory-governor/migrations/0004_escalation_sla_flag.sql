-- Overlay 0004 — seeds ESCALATION_SLA_ENABLED (OFF), mirroring the canonical
-- infrastructure/migrations/0011_escalation_sla_flag.sql so a self-provisioning
-- deployment (migrate.apply() on startup) has the flag row without a separate
-- pipeline stage. Both files are ON CONFLICT DO NOTHING, so applying either
-- (or both, in either order) is safe.
--
-- The flag gates only whether /digest folds the escalation ack-SLA report in
-- as an extra section; /escalation-sla itself is always available for preview.
--
-- Idempotent: safe to re-run.

INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('ESCALATION_SLA_ENABLED', false,
   'Fold escalation ack-SLA breaches and the closed/median-ack/breach summary into the daily digest',
   'escalation-sla-migration')
ON CONFLICT (name) DO NOTHING;
