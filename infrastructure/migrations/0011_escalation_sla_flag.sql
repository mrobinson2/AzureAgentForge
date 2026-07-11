-- Migration 0011 — Escalation SLA auditor. Seeds the gate flag OFF.
--
-- Tracks every red/yellow-lane escalation from creation to human
-- acknowledgement (escalation_opened/acked/resolved on the agent_events
-- spine, correlated by payload.escalation_id; the approval gate's
-- autonomy_decision events serve as retroactive ack+resolution) and measures
-- ack latency against a per-tenant SLA. TTL expiry always counts as a breach
-- AND an unresolved escalation — a fail-closed approval gate's posture is
-- never weakened, only made visible.
--
-- No new spine, no new table, no DDL — 0001's agent_events + indexes
-- suffice. This is a flag seed only. Event emitters land when the HITL
-- approval seam (apps/paperclip/approval.mjs, ships unwired) is wired for
-- real volume; until then the auditor reports the honest zero.
--
-- Enforcement point: /escalation-sla is always available for operator
-- preview (no flag check). This flag only gates whether the daily /digest
-- folds the escalation report in as an extra section (which digest_post then
-- delivers to the webhook). With the flag off, /digest's response is
-- byte-for-byte unchanged.
--
-- Idempotent: safe to re-run.

INSERT INTO feature_flags (name, enabled, description, updated_by) VALUES
  ('ESCALATION_SLA_ENABLED', false,
   'Fold escalation ack-SLA breaches and the closed/median-ack/breach summary into the daily digest',
   'escalation-sla-migration')
ON CONFLICT (name) DO NOTHING;
