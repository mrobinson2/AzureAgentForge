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

- **`0001_governed_memory_overlay.sql`** — the minimal documents overlay +
  `feature_flags` + `agent_events`. Enough for memory **export/curation** (the
  Obsidian interface) and the feature-flag spine.
- **`0002_governed_memory_full_overlay.sql`** — completes the documents overlay
  with the columns the **retrieval planner** reads (`half_life_days`,
  `usage_success_count`, `contradiction_count`, `is_always_on_candidate`,
  `superseded_at`, …), the `messages` class overlay, and the two governor-owned
  tables (`session_memory`, `skill_candidates`). Seeds all eight feature flags
  **OFF**. With 0001+0002 applied, `MEMORY_PLANNER_ENABLED` and the loops can be
  enabled for real (previously the planner 500'd on missing columns).
- **Type reconciliation (resolved).** `documents.id` is a 21-char nanoid
  (**TEXT**) in Honcho's schema, so `*_doc_id` reference columns are text.
  `deleted_at` / `sync_state` / `last_sync_at` are Honcho-native (its
  external-embeddings migration), not created by this overlay.
- **Still operator-gated to run live:** a `text-embedding-3-small`-class
  embedding deployment + real `openai-api-key` secret (so Plane C ranks with
  vectors instead of the trigram fallback), and threading
  `memory_governor_enabled` through the deploy. See the repo issue for the
  go-live checklist.
