# Honcho memory service

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

Vendored build of [plastic-labs/honcho](https://github.com/plastic-labs/honcho)
(AGPL-3.0), pinned as a git submodule at `apps/honcho/src`.

| | |
|---|---|
| **Pinned version** | `v3.0.11` (`14538cfc906c1d209983f69c3a703485b452f3c4`) |
| **Upstream** | `github.com/plastic-labs/honcho` |
| **License** | AGPL-3.0 (isolated to this image via the submodule boundary — no source-level mixing with AAF's MIT code; see repo `NOTICE`) |
| **Port** | 8000 (health: `GET /openapi.json`) |
| **Build** | `services/honcho/Dockerfile` (uv, Python 3.13, alembic-on-boot) |

The Dockerfile installs from the submodule and runs `alembic upgrade head` on
start, then `fastapi run src/main.py`. A **fresh database** is assumed (the AAF
local stack starts an empty Postgres); migrating an existing pre-3.0 Honcho
database is a larger, out-of-scope migration.

---

## ⚠️ NOTE — model config changed in Honcho 3.0.7 (flat `PROVIDER`/`MODEL` is gone)

**If you copy an older Honcho env, the service will boot but silently use the
wrong model.** As of **3.0.7** (upstream #459), the flat per-specialist keys were
**removed** in favor of a nested `MODEL_CONFIG` sub-model:

| Removed (pre-3.0.7, now **silently ignored**) | Replacement (3.0.7+) |
|---|---|
| `SUMMARY_PROVIDER` | `SUMMARY_MODEL_CONFIG__TRANSPORT` |
| `SUMMARY_MODEL` | `SUMMARY_MODEL_CONFIG__MODEL` |
| `DERIVER_PROVIDER` | `DERIVER_MODEL_CONFIG__TRANSPORT` |
| `DERIVER_MODEL` | `DERIVER_MODEL_CONFIG__MODEL` |
| `DIALECTIC_LEVELS__<lvl>__PROVIDER` | `DIALECTIC_LEVELS__<lvl>__MODEL_CONFIG__TRANSPORT` |
| `DIALECTIC_LEVELS__<lvl>__MODEL` | `DIALECTIC_LEVELS__<lvl>__MODEL_CONFIG__MODEL` |
| `DIALECTIC_LEVELS__<lvl>__THINKING_BUDGET_TOKENS` | `DIALECTIC_LEVELS__<lvl>__MODEL_CONFIG__THINKING_BUDGET_TOKENS` |

**Why this bites silently:** every settings model uses pydantic-settings
`extra="ignore"`, so the removed keys don't error — they're dropped. The
per-specialist defaults are all `transport="openai"`, `model="gpt-5.4-mini"`
(`src/config.py`). So an un-migrated deployment **falls back to direct-OpenAI
`gpt-5.4-mini`** rather than the provider/model you thought you set. There is no
warning; you find out from your OpenAI bill or from wrong answers.

Notes on the new shape:
- **`transport`** is a closed set: `openai` | `anthropic` | `gemini`. There is no
  `custom` transport. To reach an **OpenAI-compatible proxy** (OpenRouter, vLLM,
  a gateway, etc.), keep `transport=openai` and set the base URL:
  `<SECTION>_MODEL_CONFIG__OVERRIDES__BASE_URL=https://your-proxy/v1`
  (per-specialist) or `LLM_OPENAI_BASE_URL=...` (global default).
- **All five** dialectic levels (`minimal`/`low`/`medium`/`high`/`max`) must
  resolve; 3.0.11 fills any level you omit from built-in defaults, so a partial
  config no longer crash-loops — it just uses defaults for the gaps.
- The nested env format is `env_prefix` + `__` delimiter, e.g.
  `DERIVER_MODEL_CONFIG__MODEL`, `DIALECTIC_LEVELS__high__MODEL_CONFIG__MODEL`.
  A TOML equivalent (`config.toml`, `[deriver.model_config]` etc.) is also
  supported — see the upstream `config.toml.example`.

The AAF `docker-compose.yml` honcho service and `.env.example` already use the
migrated shape (pinned to a small `gpt-4o-mini` default for cheap local runs).

---

## Operator notes

- **API-only in this compose.** The Dockerfile runs the Honcho **API server**
  only. Honcho's background **deriver** (memory formation) and reconciler run in
  a separate process (`python -m src.deriver`) in upstream's own compose. The
  local AAF stack serves the memory API and applies migrations, but does not run
  the deriver — real memory derivation needs a deriver process **and** real LLM
  keys.
- **Placeholder keys boot fine.** `LLM_*_API_KEY` placeholders let the API start;
  clients are constructed lazily at call time (not at import), so no key is
  required just to bring the service up. Set real keys to actually drive
  summary/deriver/dialectic work.
- **Redis is optional.** Caching is off by default (`CACHE_ENABLED=false`); the
  queue is Postgres-backed, so no Redis is required for the API to run.
- **DB connection** uses `DB_CONNECTION_URI` (pydantic `env_prefix=DB_`) with the
  psycopg3 scheme (`postgresql+psycopg://...`) — not `DATABASE_URL`.
