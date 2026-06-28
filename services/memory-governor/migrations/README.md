# Governed-memory migrations

The memory-governor shares **Honcho's Postgres** — it does not own a database.
Honcho's own migrations create `documents`/`collections` (+ 1536-dim embeddings);
these files add the **governed-memory overlay** on top: extra columns the governor
reads/writes on `documents`, plus the governor-owned tables.

## Apply path

`python -m governor.migrate` applies every `*.sql` here in filename order, once
each, tracked in a `governor_schema_migrations` table. Every file is written
`IF NOT EXISTS`, so re-applying is a no-op and Honcho's columns are never
clobbered. The governor also calls `migrate.apply()` on startup, so a deploy is
self-provisioning — no separate pipeline stage needed.

To apply by hand against the shared DB:

```bash
DATABASE_URL="<postgres-connection-string from Key Vault>" \
  python -m governor.migrate
```

## Status / caveats

- **`0001_governed_memory_overlay.sql`** — the documents overlay columns +
  `feature_flags` + `agent_events`. Enough for memory **export/curation** (the
  Obsidian interface) and the feature-flag spine.
- **Column TYPES are derived from the governor's query usage, not the canonical
  MRTek migrations.** Before enabling the governor in production, reconcile
  against the live Honcho schema — in particular whether `documents.id` is `uuid`
  or `text` (so the `*_doc_id` reference columns match).
- **TODO `0002`** — `session_memory` and `skill_candidates` can't be safely
  derived from code; port them from the canonical MRTek migrations. The
  scope-watcher and skill-miner loops fail closed and stay idle until those exist,
  so the rest of the governor works without them.
