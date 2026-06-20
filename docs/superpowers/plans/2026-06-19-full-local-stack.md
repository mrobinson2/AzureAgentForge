# Full Local Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `docker compose --profile full up` bring up the whole AzureAgentForge platform locally with no Azure account, verified by a no-creds smoke gate.

**Architecture:** Repair and complete the single-file `full` compose profile (correct build contexts, a one-shot migration runner, service env + dev-only secrets, healthchecks and ordering), add `.env.example`, a `smoke-local.sh` no-creds health gate, a `local-stack.sh` wrapper, and a path-scoped CI smoke workflow. Builds on PR #17 (which made all 7 images buildable).

**Tech Stack:** Docker Compose v2, Postgres (pgvector), bash, GitHub Actions. Spec: `docs/superpowers/specs/2026-06-19-full-local-stack-design.md`.

**Verification note:** the authoring machine has **no Docker daemon**, so per-task verification is static (`python3` YAML parse, `bash -n`, `shellcheck` if present). The live `up`+smoke integration runs in **CI / on the operator's machine** (Task 8). Confirm-during-implementation items are flagged inline.

---

### Task 1: `.env.example`

**Files:**
- Create: `.env.example`
- Verify: `.gitignore` already ignores `.env` and un-ignores `.env.example` (lines `.env`, `.env.*`, `!.env.example`).

- [ ] **Step 1: Write `.env.example`**

```bash
# AzureAgentForge — local full-stack config. Copy to .env (gitignored) and edit.
#   docker compose --profile full up      (or: scripts/local-stack.sh up)

# ── Model backend — the ONE thing to set to DRIVE agents ─────────────────────
# The stack comes up healthy WITHOUT this; model calls return a clear error
# until you point it at any OpenAI-compatible endpoint. Pick one:
#   OpenAI:    OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1   OPENAI_COMPAT_API_KEY=sk-...
#   Ollama:    OPENAI_COMPAT_BASE_URL=http://host.docker.internal:11434/v1  OPENAI_COMPAT_API_KEY=ollama
#   LM Studio: OPENAI_COMPAT_BASE_URL=http://host.docker.internal:1234/v1   OPENAI_COMPAT_API_KEY=lm-studio
OPENAI_COMPAT_BASE_URL=
OPENAI_COMPAT_API_KEY=

# ── Postgres (local dev defaults) ────────────────────────────────────────────
POSTGRES_USER=aaf
POSTGRES_PASSWORD=localdev
POSTGRES_DB=aaf

# ── DEV ONLY — DO NOT USE IN PRODUCTION ──────────────────────────────────────
# Well-known placeholders so the stack runs with zero setup. A real deploy
# injects these from Key Vault.
PAPERCLIP_AUTOMATION_JWT_SECRET=localdev-automation-secret-change-me
PAPERCLIP_AGENT_JWT_SECRET=localdev-agent-secret-change-me
PAPERCLIP_ADMIN_EMAIL=admin@localhost
PAPERCLIP_ADMIN_PASSWORD=localdev-admin-change-me
GOVERNOR_API_KEY=localdev

# ── Host port overrides (change if a port is already in use) ──────────────────
POSTGRES_PORT=5432
ROUTER_PORT=8080
PAPERCLIP_PORT=3100
HONCHO_PORT=8000
GOVERNOR_PORT=8090

# ── PaperClip upstream pin (matches scripts/build-and-push.sh) ────────────────
PAPERCLIP_VERSION=v2026.517.0
PAPERCLIP_EXPECTED_SHA=3e6610fb938d04638fa578a1fc0d119b434fa2e4
```

- [ ] **Step 2: Verify gitignore + no real secrets**

Run: `git check-ignore .env && echo ok; grep -nE 'sk-[A-Za-z0-9]{20,}|BEGIN .*PRIVATE KEY' .env.example && echo LEAK || echo clean`
Expected: `ok` then `clean`.

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "feat(local): .env.example for the full local stack"
```

---

### Task 2: Rewrite `docker-compose.yml` (build contexts, healthchecks, ports, full services)

**Files:**
- Modify: `docker-compose.yml` (replace whole file)

This replaces the broken skeleton. Key fixes: `honcho`/`paperclip` build with `context: .` + `dockerfile:`; add `migrate`; add `watchdog`; wire env; add healthchecks + ordering; port overrides; named `paperclip-data` volume.

- [ ] **Step 1: Write the full `docker-compose.yml`**

```yaml
# Default `docker compose up` = working slice: Postgres + model-router.
# Full platform locally (no Azure):  docker compose --profile full up
# Or use the wrapper:                scripts/local-stack.sh up
# See .env.example and docs/local-development.md.

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-aaf}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-localdev}
      POSTGRES_DB: ${POSTGRES_DB:-aaf}
    ports: ["${POSTGRES_PORT:-5432}:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-aaf} -d ${POSTGRES_DB:-aaf}"]
      interval: 5s
      timeout: 5s
      retries: 12

  model-router:
    build: ./services/model-router
    environment:
      LLM_PROVIDER: ${LLM_PROVIDER:-azure_foundry}
      AZURE_FOUNDRY_ENDPOINT: ${AZURE_FOUNDRY_ENDPOINT:-}
      AZURE_FOUNDRY_API_KEY: ${AZURE_FOUNDRY_API_KEY:-}
      GPT4O_BASE_URL: ${GPT4O_BASE_URL:-${AZURE_FOUNDRY_ENDPOINT:-}}
      GPT4O_API_KEY: ${GPT4O_API_KEY:-${AZURE_FOUNDRY_API_KEY:-}}
      OPENAI_COMPAT_BASE_URL: ${OPENAI_COMPAT_BASE_URL:-}
      OPENAI_COMPAT_API_KEY: ${OPENAI_COMPAT_API_KEY:-}
    ports: ["${ROUTER_PORT:-8080}:8080"]
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status==200 else 1)\""]
      interval: 5s
      timeout: 5s
      retries: 12

  # ── full profile ───────────────────────────────────────────────────────────
  migrate:
    profiles: ["full"]
    image: pgvector/pgvector:pg16
    depends_on:
      postgres: { condition: service_healthy }
    volumes: ["./infrastructure/migrations:/migrations:ro"]
    environment:
      PGHOST: postgres
      PGUSER: ${POSTGRES_USER:-aaf}
      PGPASSWORD: ${POSTGRES_PASSWORD:-localdev}
      PGDATABASE: ${POSTGRES_DB:-aaf}
    entrypoint:
      - sh
      - -c
      - |
        set -e
        for f in /migrations/*.sql; do
          echo "[migrate] applying $${f}"
          psql -v ON_ERROR_STOP=1 -f "$${f}"
        done
        echo "[migrate] all migrations applied"
    restart: "no"

  honcho:
    profiles: ["full"]
    build:
      context: .
      dockerfile: services/honcho/Dockerfile
    depends_on:
      postgres: { condition: service_healthy }
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-aaf}:${POSTGRES_PASSWORD:-localdev}@postgres:5432/${POSTGRES_DB:-aaf}
    ports: ["${HONCHO_PORT:-8000}:8000"]

  paperclip:
    profiles: ["full"]
    build:
      context: .
      dockerfile: services/paperclip/Dockerfile
      args:
        PAPERCLIP_VERSION: ${PAPERCLIP_VERSION:-v2026.517.0}
        PAPERCLIP_EXPECTED_SHA: ${PAPERCLIP_EXPECTED_SHA:-3e6610fb938d04638fa578a1fc0d119b434fa2e4}
    depends_on:
      postgres: { condition: service_healthy }
      migrate: { condition: service_completed_successfully }
      model-router: { condition: service_healthy }
      honcho: { condition: service_started }
    environment:
      PORT: "3100"
      OPENAI_BASE_URL: http://model-router:8080/v1
      HONCHO_BASE_URL: http://honcho:8000
      GOVERNOR_BASE_URL: http://memory-governor:8090
      PAPERCLIP_AUTOMATION_JWT_SECRET: ${PAPERCLIP_AUTOMATION_JWT_SECRET:-localdev-automation-secret-change-me}
      PAPERCLIP_AGENT_JWT_SECRET: ${PAPERCLIP_AGENT_JWT_SECRET:-localdev-agent-secret-change-me}
      PAPERCLIP_ADMIN_EMAIL: ${PAPERCLIP_ADMIN_EMAIL:-admin@localhost}
      PAPERCLIP_ADMIN_PASSWORD: ${PAPERCLIP_ADMIN_PASSWORD:-localdev-admin-change-me}
    ports: ["${PAPERCLIP_PORT:-3100}:3100"]
    volumes: ["paperclip-data:/paperclip"]

  memory-governor:
    profiles: ["full"]
    build: ./services/memory-governor
    depends_on:
      postgres: { condition: service_healthy }
      migrate: { condition: service_completed_successfully }
      model-router: { condition: service_healthy }
      honcho: { condition: service_started }
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-aaf}:${POSTGRES_PASSWORD:-localdev}@postgres:5432/${POSTGRES_DB:-aaf}
      ROUTER_BASE_URL: http://model-router:8080/v1
      HONCHO_BASE_URL: http://honcho:8000
      GOVERNOR_API_KEY: ${GOVERNOR_API_KEY:-localdev}
    ports: ["${GOVERNOR_PORT:-8090}:8090"]

  watchdog:
    profiles: ["full"]
    build: ./services/watchdog
    depends_on:
      postgres: { condition: service_healthy }
      migrate: { condition: service_completed_successfully }
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER:-aaf}:${POSTGRES_PASSWORD:-localdev}@postgres:5432/${POSTGRES_DB:-aaf}
      ROUTER_BASE_URL: http://model-router:8080/v1
      GOVERNOR_BASE_URL: http://memory-governor:8090
      GOVERNOR_API_KEY: ${GOVERNOR_API_KEY:-localdev}

volumes:
  pgdata:
  paperclip-data:
```

**Confirm during implementation:**
- model-router `GET /health` returns 200 **without** provider creds (liveness, not provider check). Grep `services/model-router/main.py:1387` region. If it depends on creds, switch the healthcheck to a TCP/port liveness probe.
- `watchdog` required env — read `services/watchdog/src/watchdog/watchdog.py`; add any missing required vars (it reads governance flags from the DB, which seed off, so it idles).

- [ ] **Step 2: Validate YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml')); print('yaml ok')"`
Expected: `yaml ok`

- [ ] **Step 3: Assert structure**

Run:
```bash
python3 - <<'PY'
import yaml
c=yaml.safe_load(open('docker-compose.yml'))['services']
default=[s for s,v in c.items() if 'profiles' not in v]
full=[s for s,v in c.items() if v.get('profiles')==['full']]
assert set(default)=={'postgres','model-router'}, default
assert set(full)=={'migrate','honcho','paperclip','memory-governor','watchdog'}, full
assert c['paperclip']['build']['dockerfile']=='services/paperclip/Dockerfile'
assert c['honcho']['build']['dockerfile']=='services/honcho/Dockerfile'
print('structure ok')
PY
```
Expected: `structure ok`

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(local): full compose profile — contexts, migrate, healthchecks, watchdog"
```

---

### Task 3: `scripts/smoke-local.sh` — no-creds health gate

**Files:**
- Create: `scripts/smoke-local.sh` (chmod +x)

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# No-credentials health gate for the local full stack. Makes NO model calls, so
# it passes without any model key. Exits non-zero on first failure.
set -uo pipefail

ROUTER_PORT="${ROUTER_PORT:-8080}"
PAPERCLIP_PORT="${PAPERCLIP_PORT:-3100}"
HONCHO_PORT="${HONCHO_PORT:-8000}"
GOVERNOR_PORT="${GOVERNOR_PORT:-8090}"
GOVERNOR_API_KEY="${GOVERNOR_API_KEY:-localdev}"
POSTGRES_USER="${POSTGRES_USER:-aaf}"
POSTGRES_DB="${POSTGRES_DB:-aaf}"
COMPOSE="${COMPOSE:-docker compose}"

fail=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=1; }

http_is() { # url expected label
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo 000)"
  [ "$code" = "$2" ] && pass "$3 ($code)" || bad "$3 (got $code, want $2)"
}

echo "smoke-local: checking the full stack (no model key required)…"

# 1. migrations applied — feature_flags seeded
if $COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
     'select count(*) from feature_flags;' >/tmp/aaf_ff 2>/dev/null \
   && [ "$(tr -d '[:space:]' </tmp/aaf_ff)" -ge 1 ] 2>/dev/null; then
  pass "migrations applied (feature_flags rows: $(tr -d '[:space:]' </tmp/aaf_ff))"
else
  bad "migrations applied (feature_flags missing/empty)"
fi

# 2. model-router liveness (no creds)
http_is "http://localhost:${ROUTER_PORT}/health" 200 "model-router /health"
# 3. honcho
http_is "http://localhost:${HONCHO_PORT}/openapi.json" 200 "honcho /openapi.json"
# 4. paperclip UI
http_is "http://localhost:${PAPERCLIP_PORT}/" 200 "paperclip UI /"
# 5. governor healthy + idle
http_is "http://localhost:${GOVERNOR_PORT}/healthz" 200 "memory-governor /healthz"
admit="$(curl -s -X POST "http://localhost:${GOVERNOR_PORT}/admit" \
  -H "Authorization: Bearer ${GOVERNOR_API_KEY}" -H 'Content-Type: application/json' \
  -d '{"content":"smoke","agent_id":"smoke","scope_kind":"task","scope_id":"smoke"}' 2>/dev/null || echo '')"
if printf '%s' "$admit" | grep -q '"status"[[:space:]]*:[[:space:]]*"disabled"'; then
  pass "memory-governor /admit → disabled (flags off)"
else
  bad "memory-governor /admit → expected disabled, got: ${admit:-<none>}"
fi

echo
[ "$fail" -eq 0 ] && { echo "smoke-local: PASS"; exit 0; } || { echo "smoke-local: FAIL"; exit 1; }
```

**Confirm during implementation:** the `/admit` request body shape against `services/memory-governor/src/governor/main.py:115` (the admission model). If the schema differs and returns 422 before the disabled short-circuit, either adjust the body to match or replace check 5b with a DB assertion (`select enabled from feature_flags where flag='MEMORY_CLASSES_ENABLED'` → `f`).

- [ ] **Step 2: Make executable + syntax check**

Run: `chmod +x scripts/smoke-local.sh && bash -n scripts/smoke-local.sh && echo "bash ok"; command -v shellcheck >/dev/null && shellcheck scripts/smoke-local.sh || echo "(shellcheck not installed — skipped)"`
Expected: `bash ok` (and shellcheck clean or skipped)

- [ ] **Step 3: Commit**

```bash
git add scripts/smoke-local.sh
git commit -m "feat(local): smoke-local.sh no-creds health gate"
```

---

### Task 4: `scripts/local-stack.sh` — wrapper

**Files:**
- Create: `scripts/local-stack.sh` (chmod +x)

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# One-command local full stack.
#   scripts/local-stack.sh up           preflight, .env bootstrap, build, wait, smoke, print URLs
#   scripts/local-stack.sh down [args]  stop and remove (pass -v to drop volumes)
#   scripts/local-stack.sh smoke        run the health gate against a running stack
#   scripts/local-stack.sh logs [svc]   follow logs
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose"

up() {
  for s in apps/hermes/src apps/honcho/src; do
    if [ -z "$(ls -A "$s" 2>/dev/null || true)" ]; then
      echo "[local-stack] initializing submodule $s…"; git submodule update --init "$s"
    fi
  done
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "[local-stack] created .env from .env.example — edit it to add a model key to drive agents."
  fi
  $COMPOSE --profile full up -d --build
  echo "[local-stack] waiting for services to become healthy (up to 5 min)…"
  deadline=$(( $(date +%s) + 300 ))
  while :; do
    unhealthy="$($COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | awk '$2!="" && $2!="healthy"{print $1}')"
    [ -z "$unhealthy" ] && break
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[local-stack] timeout; still unhealthy: $unhealthy"; $COMPOSE ps; exit 1
    fi
    sleep 5
  done
  scripts/smoke-local.sh
  cat <<EOF

[local-stack] up. URLs:
  PaperClip UI/API : http://localhost:${PAPERCLIP_PORT:-3100}
  Model router     : http://localhost:${ROUTER_PORT:-8080}
  Honcho           : http://localhost:${HONCHO_PORT:-8000}
  Memory governor  : http://localhost:${GOVERNOR_PORT:-8090}

Set OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_API_KEY in .env to drive agents.
EOF
}

case "${1:-up}" in
  up)    up ;;
  down)  shift; $COMPOSE --profile full down "$@" ;;
  smoke) scripts/smoke-local.sh ;;
  logs)  $COMPOSE --profile full logs -f "${2:-}" ;;
  *)     echo "usage: $0 {up|down [-v]|smoke|logs [service]}"; exit 2 ;;
esac
```

- [ ] **Step 2: Make executable + syntax check**

Run: `chmod +x scripts/local-stack.sh && bash -n scripts/local-stack.sh && echo "bash ok"; command -v shellcheck >/dev/null && shellcheck scripts/local-stack.sh || echo "(shellcheck skipped)"`
Expected: `bash ok`

- [ ] **Step 3: Commit**

```bash
git add scripts/local-stack.sh
git commit -m "feat(local): local-stack.sh one-command wrapper"
```

---

### Task 5: CI — path-scoped full-stack smoke

**Files:**
- Create: `.github/workflows/local-stack-smoke.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: local-stack-smoke
on:
  push:
    paths: &paths
      - 'docker-compose.yml'
      - 'services/**'
      - 'apps/**'
      - 'infrastructure/migrations/**'
      - 'scripts/smoke-local.sh'
      - 'scripts/local-stack.sh'
      - '.env.example'
      - '.github/workflows/local-stack-smoke.yml'
  pull_request:
    paths: *paths
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with: { submodules: recursive }
      - name: Bring up full stack (no model creds)
        run: docker compose --profile full up -d --build
      - name: Wait for healthy
        run: |
          deadline=$(( $(date +%s) + 600 ))
          while :; do
            unhealthy="$(docker compose ps --format '{{.Service}} {{.Health}}' | awk '$2!="" && $2!="healthy"{print $1}')"
            [ -z "$unhealthy" ] && break
            if [ "$(date +%s)" -ge "$deadline" ]; then
              echo "timeout; unhealthy: $unhealthy"; docker compose ps; docker compose logs; exit 1
            fi
            sleep 10
          done
      - name: Smoke
        run: bash scripts/smoke-local.sh
      - name: Logs on failure
        if: failure()
        run: docker compose logs
      - name: Teardown
        if: always()
        run: docker compose --profile full down -v
```

- [ ] **Step 2: Validate YAML**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/local-stack-smoke.yml')); print('yaml ok')"`
Expected: `yaml ok`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/local-stack-smoke.yml
git commit -m "ci(local): path-scoped full-stack smoke workflow"
```

---

### Task 6: Docs — reflect that the full local stack shipped

**Files:**
- Modify: `README.md` (the "planned for v1.2" local-stack lines + the roadmap checkbox)
- Create: `docs/local-development.md` (usage)

- [ ] **Step 1: Update README local-stack references**

In `README.md`, update the three local-stack mentions (text match, line numbers approximate):
- `~303`: "The current local quickstart brings up PostgreSQL and the model router. The full one-command local stack and full end-to-end Azure installer are planned for v1.2." → "`docker compose --profile full up` (or `scripts/local-stack.sh up`) brings up the whole platform locally with no Azure account; the full end-to-end Azure installer is planned for v1.2."
- `~377`: "A one-command full local stack is planned for v1.2." → "A one-command full local stack is available: `scripts/local-stack.sh up`. See [docs/local-development.md](docs/local-development.md)."
- Roadmap (`~471`): change `⬜ One-command full local stack (`docker compose --profile full up`)` to `✅`.

- [ ] **Step 2: Write `docs/local-development.md`**

```markdown
# Local development — full stack, no Azure

Bring up the whole AzureAgentForge platform on your machine, no Azure account needed.

## Quickstart

```bash
scripts/local-stack.sh up
```

This initializes the upstream submodules if needed, creates `.env` from `.env.example`,
builds and starts the full profile, waits for health, runs the smoke gate, and prints URLs:

| Service | URL |
|---|---|
| PaperClip UI / API | http://localhost:3100 |
| Model router | http://localhost:8080 |
| Honcho (memory) | http://localhost:8000 |
| Memory governor | http://localhost:8090 |

The stack comes up **healthy with no credentials**. The governed-memory governor and watchdog
are present but **flag-off / idle**.

## Driving agents

To actually run agents, set ONE OpenAI-compatible endpoint in `.env`:

```bash
OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1   # or Ollama / LM Studio
OPENAI_COMPAT_API_KEY=sk-...
```

Then `scripts/local-stack.sh down && scripts/local-stack.sh up`.

## Plain compose

```bash
docker compose up                  # working slice: Postgres + model-router
docker compose --profile full up   # full platform
```

## Verify / teardown

```bash
scripts/local-stack.sh smoke       # re-run the health gate
scripts/local-stack.sh logs paperclip
scripts/local-stack.sh down -v     # stop and drop volumes
```
```

- [ ] **Step 3: Commit**

```bash
git add README.md docs/local-development.md
git commit -m "docs(local): document the one-command full local stack"
```

---

### Task 7: Static end-to-end self-check (no daemon)

**Files:** none (verification only)

- [ ] **Step 1: Re-validate everything statically**

Run:
```bash
python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml')); yaml.safe_load(open('.github/workflows/local-stack-smoke.yml')); print('yaml ok')"
bash -n scripts/smoke-local.sh && bash -n scripts/local-stack.sh && echo "bash ok"
git check-ignore .env >/dev/null && echo ".env ignored"
```
Expected: `yaml ok`, `bash ok`, `.env ignored`

---

### Task 8: Live integration verification (CI / operator — has Docker)

**Files:** none (gated; cannot run on the authoring machine — no Docker daemon)

- [ ] **Step 1:** Push the branch; the `local-stack-smoke` workflow builds the full profile, waits for healthy, runs `scripts/smoke-local.sh`, and tears down. This is the authoritative end-to-end check.
- [ ] **Step 2 (optional, operator local):** `scripts/local-stack.sh up` on a machine with Docker; confirm all-healthy + smoke PASS + the printed URLs load.

---

## Self-Review

- **Spec coverage:** §4 topology → Task 2; §5 contexts → Task 2; §6 `.env.example`/secrets → Task 1 + Task 2 env; §7 migrate → Task 2 (`migrate` service); §8 smoke → Task 3; §9 wrapper → Task 4; §11 CI → Task 5; docs/§roadmap → Task 6; §13 no-daemon constraint → Tasks 7–8. All covered.
- **Open confirmations (resolve in-task, not placeholders):** router `/health` creds-independence (Task 2); watchdog required env (Task 2); governor `/admit` body schema (Task 3). Each has a concrete fallback.
- **Type/name consistency:** port vars (`ROUTER_PORT`/`PAPERCLIP_PORT`/`HONCHO_PORT`/`GOVERNOR_PORT`), `GOVERNOR_API_KEY`, service names (`model-router`, `memory-governor`), and URLs (`http://model-router:8080/v1`, `http://honcho:8000`, `http://memory-governor:8090`) are consistent across compose, smoke, wrapper, and docs.
