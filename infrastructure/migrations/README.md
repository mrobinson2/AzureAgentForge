# Database migrations

Plain `.sql` migrations for the governed-memory layer. They run against the
Postgres database that backs Honcho — the memory governor reads and writes the
same database, so there is no separate store to provision.

See [`docs/design/memory-system.md`](../../docs/design/memory-system.md) for the
design these migrations implement.

## Apply order

Apply in filename order. Each file is idempotent (`CREATE … IF NOT EXISTS`,
`INSERT … ON CONFLICT DO NOTHING`, `CREATE OR REPLACE`), so re-running is safe.

| File | What it does |
|---|---|
| `0001_agent_events_and_feature_flags.sql` | Event spine (`agent_events` + NOTIFY trigger) and the `feature_flags` registry, all flags seeded **off**. |
| `0002_memory_classes.sql` | Governance metadata columns on `documents`/`messages`, the `session_memory` table (Plane D), indexes, and `NOT VALID` CHECK constraints. |
| `manual/0003_memory_backfill.sql` | **Manual** backfill: defaults pre-existing rows and `VALIDATE`s the constraints. Apply deliberately, not as part of an automated sweep. |
| `0004_pg_trgm.sql` | Enables `pg_trgm` (guarded for managed Postgres). Backs trigram dedup + ranking. |
| `0006_vector_retrieval_flag.sql` | Seeds `MEMORY_VECTOR_RETRIEVAL_ENABLED` (off) — the hybrid pgvector + trigram Plane C blend. |
| `0007_contradiction_sweep_flag.sql` | Seeds `MEMORY_CONTRADICTION_SWEEP_ENABLED` (off) — the nightly contradiction sweep. |
| `0008_skill_autogen.sql` | `skill_candidates` table + seeds `SKILL_AUTOGEN_ENABLED` (off). |

Numbering follows the design's feature ordering, so there is no `0005`.

The ordering the design calls for: the spine + flag registry + NOTIFY trigger
first; then the `documents` / `session_memory` columns, indexes, and `NOT VALID`
constraints; then the manual backfill that defaults pre-existing rows and
`VALIDATE`s the constraints; then the `pg_trgm` extension; then the per-feature
flag-seed migrations.

## Applying

```bash
# Against a local Postgres (e.g. the docker-compose `full` profile):
psql "$DATABASE_URL" -f infrastructure/migrations/0001_agent_events_and_feature_flags.sql

# Apply every migration in order:
for f in infrastructure/migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
```

On a hardened deployment the database is private-endpoint only — run migrations
from a network-attached host (a jobs container or an admin session), not from a
laptop over the public internet.

## Managed-Postgres `pg_trgm` gotcha

Some managed Postgres offerings reject `CREATE EXTENSION pg_trgm` even when the
extension is already installed, unless it appears in an allowed-extensions list.
The migration that needs it therefore **guards on the extension's presence**
rather than issuing an unconditional `CREATE EXTENSION`. Until `pg_trgm` is
present, `similarity()` calls throw and the trigram dedup/ranking path degrades —
so confirm the extension is allow-listed for your server before enabling the
features that depend on it.

## Flags ship off

Every flag in `feature_flags` is seeded `false`. With all flags off the governor
is an idle, low-CPU app and the platform behaves exactly as it did before these
tables existed. Turn flags on per environment as you validate each capability.
```sql
UPDATE feature_flags SET enabled = true, updated_by = 'operator' WHERE name = 'AGENT_EVENTS_ENABLED';
```
