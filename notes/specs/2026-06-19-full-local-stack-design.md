# One-Command Full Local Stack — Design Spec

**Date:** 2026-06-19
**Status:** Approved (brainstorming) → ready for implementation plan
**Branch:** `feat/full-local-stack` (builds on `feat/vendoring-buildability` / PR #17)
**Roadmap:** v1.2 — "One-command full local stack (`docker compose --profile full up`)"

## 1. Problem

`docker compose --profile full up` is promised in the README/ROADMAP (v1.2) but the current `full`
profile is a non-working skeleton:

- **Wrong build context.** `paperclip` and `honcho` build with `build: ./services/<n>`, but their
  Dockerfiles COPY from `apps/` and require **repo-root** context (`context: .`, `dockerfile:
  services/<n>/Dockerfile`). As written, the build fails.
- **No migration runner.** The governor tables (`infrastructure/migrations/0001…0008.sql`) are never
  applied locally — there is no existing runner for them (in Azure they run via a pipeline stage).
- **No service config.** `paperclip`, `honcho`, `memory-governor` have no env wiring (model routing,
  Honcho URL, dev secrets, admin bootstrap), so even if they built they would not run together.
- **No boot ordering / healthchecks**, no `.env.example`, no local smoke test.

PR #17 made all 7 images **buildable** from a clean clone; this spec makes the full stack **run**
from a clean clone, with **no Azure account**.

## 2. Goal & success criteria

A developer with **no Azure account** runs one command and gets the whole platform up and
explorable. To *drive* an agent they add a single model endpoint to `.env`; to merely look around
(PaperClip UI, agent roster, governed-memory wiring) they need **zero credentials**.

Success = the no-creds **`smoke-local.sh`** gate passes:

1. Migrations applied (the `feature_flags` table exists and is seeded).
2. `model-router` reachable (`GET /health` → 200); a model call with no provider returns a **clear error**, not a crash.
3. `honcho` healthy (`GET /openapi.json` → 200).
4. `paperclip` UI reachable (`GET /` → 200; there is no dedicated health endpoint).
5. `memory-governor` healthy (`GET /healthz` → 200) and `POST /admit` (with the dev `GOVERNOR_API_KEY`) returns `"disabled"`.

## 3. Chosen approach — A: fix & complete the single `--profile full` in place

Keep one `docker-compose.yml`; repair and complete the `full` profile. Rejected alternative B
(split base + `docker-compose.full.yml`) because it changes the documented `--profile full` UX for
no real gain. Add a thin `scripts/local-stack.sh` wrapper for the nicest one-command first run.

## 4. Service topology

```
postgres ──(healthy)──┬─► migrate (one-shot; applies SQL; exits 0)
                      ├─► honcho            (:8000, manages own schema)
model-router ─────────┤
   (:8080)            ├─► paperclip         (:3100, bundles the Hermes runtime)
                      ├─► memory-governor   (:8090, flag-OFF / idle)
                      └─► watchdog          (loops, flag-OFF / inert)
```

**No separate Hermes/Telegram container.** The PaperClip image already bundles the Hermes runtime
(Dockerfile `hermes-cli` stage installs `apps/hermes/src` to `/opt/hermes`; the
`hermes-paperclip-adapter` spawns it per task inside the paperclip container). The standalone
`agent-runtime` image is the Telegram **gateway** (needs a bot token) and is **out of scope** here.

**Default profile (unchanged):** `postgres`, `model-router`.
**`full` profile adds:** `migrate`, `honcho`, `paperclip`, `memory-governor`, `watchdog`.

### Boot ordering (healthchecks + depends_on conditions)

| Service | Healthcheck | depends_on |
|---|---|---|
| postgres | `pg_isready` | — |
| model-router | `GET /health` | — |
| migrate | (one-shot; no healthcheck) | postgres: `service_healthy` |
| honcho | `GET /openapi.json` | postgres: `service_healthy` |
| paperclip | `GET /` (UI root; no dedicated health endpoint) | postgres healthy, migrate `service_completed_successfully`, model-router healthy |
| memory-governor | `GET /healthz` | postgres healthy, migrate completed, honcho healthy, model-router healthy |
| watchdog | (long-running loop; minimal/no healthcheck) | postgres healthy, migrate completed |

## 5. Build-context fix

- **Self-contained** (context = service dir; already correct): `model-router`, `memory-governor`,
  `watchdog`.
- **Upstream** (context = repo root; COPY from `apps/`): `honcho`, `paperclip`:
  ```yaml
  build:
    context: .
    dockerfile: services/paperclip/Dockerfile
    args:
      PAPERCLIP_VERSION: ${PAPERCLIP_VERSION:-latest}
      PAPERCLIP_EXPECTED_SHA: ${PAPERCLIP_EXPECTED_SHA:-}
  ```

## 6. Configuration & secrets — `.env.example` → `.env`

A committed `.env.example` (copied to `.env`, which is gitignored). Contents:

- **Model backend (the one knob), empty by default**, with inline examples:
  - OpenAI: `OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1` + `OPENAI_COMPAT_API_KEY=sk-...`
  - Ollama: `OPENAI_COMPAT_BASE_URL=http://host.docker.internal:11434/v1`
  - LM Studio: `OPENAI_COMPAT_BASE_URL=http://host.docker.internal:1234/v1`
- **Postgres** dev defaults (`aaf` / `localdev` / `aaf`).
- **Dev-only secrets** under a loud `# DEV ONLY — DO NOT USE IN PRODUCTION` banner:
  `PAPERCLIP_AUTOMATION_JWT_SECRET`, `PAPERCLIP_AGENT_JWT_SECRET`, `PAPERCLIP_ADMIN_EMAIL`,
  `PAPERCLIP_ADMIN_PASSWORD`, `GOVERNOR_API_KEY` — all obvious `localdev-*` placeholders.
- **Optional port overrides:** `POSTGRES_PORT`, `ROUTER_PORT`, `PAPERCLIP_PORT`, `HONCHO_PORT`,
  `GOVERNOR_PORT` (each `${X_PORT:-default}`).

Per-service env (key items):

- **paperclip:** `OPENAI_BASE_URL=http://model-router:8080/v1` (the bundled Hermes routes through the
  local router), `HONCHO_BASE_URL=http://honcho:8000`, the dev JWT secrets + admin bootstrap. The
  **auth-proxy is enabled** (dev JWT secret present) so the full UI + skills/automation API are
  reachable on a single host port (`:3100`).
- **memory-governor:** `DATABASE_URL`, `ROUTER_BASE_URL=http://model-router:8080/v1`,
  `HONCHO_BASE_URL`, `GOVERNOR_API_KEY`. All feature flags seed **false** (DB), so it idles.
- **honcho:** `DATABASE_URL` to the shared Postgres.
- **watchdog:** `DATABASE_URL` (+ router/governor URLs); all detector/loop flags **off**.

## 7. Migration runner (new)

One-shot `migrate` service:

```yaml
migrate:
  profiles: ["full"]
  image: postgres:16            # for psql
  depends_on: { postgres: { condition: service_healthy } }
  volumes: ["./infrastructure/migrations:/migrations:ro"]
  environment: { PGHOST: postgres, PGUSER: ..., PGPASSWORD: ..., PGDATABASE: ... }
  entrypoint: ["sh","-c","set -e; for f in /migrations/*.sql; do echo \"applying $f\"; psql -v ON_ERROR_STOP=1 -f \"$f\"; done"]
  restart: "no"
```

- Files apply in lexical order (`0001, 0002, 0004, 0006, 0007, 0008`); they are already idempotent.
- `0004_pg_trgm.sql`'s `CREATE EXTENSION pg_trgm` **works on the local `pgvector/pgvector:pg16`
  image** (the Azure Flexible-Server rejection gotcha does not apply locally).
- Honcho continues to manage its own schema (its entrypoint), independent of this runner.

## 8. `scripts/smoke-local.sh` — no-creds health gate

Bash; asserts the five success criteria in §2 against the running stack **without any model key**.
The governor check (`POST /admit`) sends the dev `GOVERNOR_API_KEY` (the route is key-gated) and
expects `{"status":"disabled"}`. Non-zero exit + a clear message on first failure. Used by humans and
by CI.

## 9. `scripts/local-stack.sh` — wrapper (`up | down | smoke | logs`)

`up`:
1. Preflight: if `apps/hermes/src` / `apps/honcho/src` are empty, run `git submodule update --init`.
2. If `.env` is missing, copy `.env.example` → `.env` and print "edit `.env` to add a model key".
3. `docker compose --profile full up -d --build`.
4. Wait for all healthchecks to go healthy (poll, with a timeout).
5. Run `scripts/smoke-local.sh`.
6. Print URLs (PaperClip `http://localhost:3100`, router `:8080`, honcho `:8000`, governor `:8090`)
   and "set `OPENAI_COMPAT_*` in `.env` to drive agents."

`down`: `docker compose --profile full down` (`-v` optional). `smoke`: run the gate. `logs`: tail.

## 10. Error handling / failure modes

- **No model creds:** stack healthy; router returns an explicit "no provider configured" on model
  calls; agent runs fail with a clear message in PaperClip. Smoke passes (it makes no model calls).
- **Port collisions:** ports overridable via `${X_PORT:-default}` in `.env`.
- **Empty submodules:** wrapper auto-inits before build.
- **Slow first build:** multi-stage build of ~5 images is slow on first run; documented. Cached after.

## 11. Testing / CI

- **Local:** `scripts/smoke-local.sh` is the verification.
- **CI:** a GitHub Actions workflow builds the `full` profile, runs `smoke-local.sh`, tears down.
  Because building ~5 images is heavy, the workflow is **path-scoped** (triggers only on changes to
  `docker-compose.yml`, `services/**`, `apps/**`, `infrastructure/migrations/**`,
  `scripts/{smoke-local,local-stack}.sh`, `.env.example`) rather than on every PR.

## 12. Resolved decisions

1. **Model backend:** stack comes up with zero creds; agents driven by a user-supplied
   OpenAI-compatible endpoint (OpenAI / Ollama / LM Studio) via `.env`. *(No bundled local model.)*
2. **Scope:** full platform incl. governed memory — `postgres + model-router + honcho + paperclip +
   memory-governor + watchdog + migrate`. Governor + watchdog **present but flag-OFF/idle**.
3. **Success bar:** no-creds health smoke (not a full agent round-trip).
4. **Structure:** single-file `--profile full` (Approach A) + `local-stack.sh` wrapper.
5. **Watchdog:** included in `full`, idle (flags off).
6. **CI:** path-scoped full-stack smoke workflow.

## 13. Constraints & out-of-scope

- **No Docker daemon on the authoring machine.** Implementation produces all files + static
  validation (YAML parse, `bash -n`/shellcheck, and `docker compose config` where a CLI is
  available). The **live `up` + smoke run is gated to the operator or CI** — it cannot be executed
  during implementation.
- **Out of scope (YAGNI):** Telegram/Discord/Teams surfaces, the standalone `agent-runtime` gateway,
  a bundled local model, and production secret management.

## 14. Acceptance criteria (testable)

- [ ] `docker compose config --profile full` parses with no errors and resolves all build contexts.
- [ ] `migrate` applies all `infrastructure/migrations/*.sql` against the local Postgres and exits 0.
- [ ] With an empty `.env` (no model key), `docker compose --profile full up` reaches all-healthy and
      `scripts/smoke-local.sh` exits 0.
- [ ] `scripts/local-stack.sh up` performs submodule preflight, `.env` bootstrap, build, wait, smoke,
      and prints the service URLs.
- [ ] `.env.example` documents the model-backend knob (OpenAI / Ollama / LM Studio) and dev-only
      secrets with a production warning; `.env` is gitignored.
- [ ] The path-scoped CI workflow builds the full profile, runs the smoke gate, and tears down.
- [ ] No Telegram/Teams/agent-runtime services appear in the `full` profile.
