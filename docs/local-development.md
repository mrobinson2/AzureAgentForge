<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Local development

`docker compose up` starts two services: Postgres and the model-router — the
working slice, enough to develop and test LLM routing. The **whole platform**
runs locally with one command: `scripts/local-stack.sh up` (or
`docker compose --profile full up`). It comes up healthy with **no credentials**;
to drive agents, point it at any OpenAI-compatible endpoint. You need Docker
Desktop; an LLM endpoint is optional until you want to run agents.

---

## Environment setup

Copy the example file and fill in your LLM credentials:

```bash
cp .env.example .env
```

The minimum you need to fill in before `docker compose up` will succeed:

- **`LLM_PROVIDER`**: keep `azure_foundry` if you have an AI Foundry
  project, or change to `openai_compat` for any other endpoint.
- **`AZURE_FOUNDRY_ENDPOINT` + `AZURE_FOUNDRY_API_KEY`**: if using AI
  Foundry.
- **`OPENAI_COMPAT_BASE_URL` + `OPENAI_COMPAT_API_KEY`**: if using an
  alternative endpoint.

Everything else has a working default. Postgres uses `aaf`/`localdev`/`aaf`
unless you override `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`.
Leave the bot tokens empty unless you are testing Telegram or Discord.

---

## Starting the stack

```bash
docker compose up
```

First run builds the model-router image from `services/model-router` and pulls
the Postgres image. Expect a minute or two. Subsequent starts are fast.

### What comes up (default slice)

| Service | Port | What it does |
|---|---|---|
| `postgres` | 5432 | PostgreSQL 16 with pgvector extension |
| `model-router` | 8080 | Routes LLM requests; normalises Foundry and OpenAI-compat APIs |

Postgres data persists in the `pgdata` named volume between restarts.

---

## Full local stack (no Azure)

```bash
scripts/local-stack.sh up
```

This initializes the upstream submodules if needed, creates `.env` from
`.env.example`, builds and starts the `full` profile, waits for health, runs the
smoke gate, and prints the URLs:

| Service | URL | Notes |
|---|---|---|
| PaperClip UI / API | http://localhost:3100 | bundles the Hermes runtime |
| Model router | http://localhost:8080 | |
| Honcho (memory) | http://localhost:8000 | |
| Memory governor | http://localhost:8090 | flag-off / idle (`/admit` → `disabled`) |

The stack comes up **healthy with no credentials**. The watchdog runs once, sees
its flag off, and exits cleanly. To actually **drive agents**, set ONE
OpenAI-compatible endpoint in `.env` and restart:

```bash
# .env  (Ollama / LM Studio run on your host → use host.docker.internal)
OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1
OPENAI_COMPAT_API_KEY=sk-...
```

```bash
scripts/local-stack.sh down && scripts/local-stack.sh up
```

Other wrapper commands: `scripts/local-stack.sh smoke` (re-run the health gate),
`logs [service]`, and `down -v` (drop volumes). Ports are overridable in `.env`.

---

## Iterating on a service

To rebuild a single service after editing its code:

```bash
docker compose up --build paperclip
```

Replace `paperclip` with whichever service you changed. The other services
keep running.

To tear everything down and start clean (this drops the `pgdata` volume):

```bash
docker compose down -v
docker compose up
```

---

## Limitations

Each service has its own `Dockerfile` under `services/<name>/Dockerfile`.
They build from upstream base images. If an upstream image changes its
interface, the build may break. Check `services/<name>/Dockerfile` for the
exact base and pin if you need reproducibility.

PaperClip, Honcho, the memory-governor, and the watchdog are behind the `full`
Compose profile (`docker compose --profile full up`). The Hermes and Honcho
upstream sources are vendored as git submodules under `apps/*/src` and the
wrapper auto-initializes them; PaperClip is pulled at build time from the pinned
upstream. No manual cloning is needed.

The `agent-runtime` service (the standalone Hermes / Telegram gateway) has a
Dockerfile in `services/agent-runtime/` but is **not** part of the local stack:
the PaperClip image already bundles the Hermes runtime it spawns per task, and
the gateway needs a bot token.

There is no hot-reload. After changing source files in a service, you need
to rebuild that container (`docker compose up --build <service>`).
