-- Enable pg_trgm on the Honcho database. The trigram similarity() path backs
-- the admission dedup guard and the planner's Plane C ranking.
--
-- Idempotent AND managed-Postgres-safe. Some managed offerings (e.g. Azure
-- Flexible Server) reject `CREATE EXTENSION pg_trgm` when it is not in the
-- server's allow-list parameter — and that check fires even when the extension
-- is ALREADY installed, which would abort the run under ON_ERROR_STOP=1. So we
-- guard on actual presence: a re-run is a clean no-op when pg_trgm already
-- exists. If it is genuinely absent, the real fix is allow-listing it on the
-- server, not this migration.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    CREATE EXTENSION pg_trgm;
  END IF;
END
$$;

INSERT INTO agent_events (actor_peer, event_type, channel, payload)
VALUES ('system', 'migration_applied', 'system',
        '{"migration": "0004_pg_trgm"}'::jsonb);
