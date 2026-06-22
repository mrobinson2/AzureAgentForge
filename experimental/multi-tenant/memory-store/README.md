<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Memory Store Service

> 🚧 **Design target — not deployed.** This is reference scaffolding for the multi-tenant memory store. The single-tenant stack in this repository is what actually deploys and what CI validates. Treat this as a roadmap, not a shipped feature.

Lightweight FastAPI service that exposes a thin HTTP interface on top of PostgreSQL + pgvector. It provides a cost-effective memory backend compatible with a future Azure AI Search re-index.

## Features

- Upsert semantic memories (tenant-scoped) with metadata and tags
- Vector similarity search with optional tenant/type/tag filters and score thresholds
- Hard delete by `(tenant_id, record_id)`
- Health check endpoint for Container Apps probes
- Pydantic schemas + FastAPI docs for quick integration

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/memory` | Insert or update a memory record |
| `POST` | `/memory/search` | Vector similarity search with filters |
| `DELETE` | `/memory/{tenant_id}/{record_id}` | Remove a record |
| `GET` | `/healthz` | Liveness/readiness probe |

OpenAPI/Swagger docs are available at `/docs` when running locally.

## Local development

```bash
cd experimental/multi-tenant/memory-store
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in DATABASE_URL
uvicorn app.main:app --reload --port 8000
```

`DATABASE_URL` should point at an Azure PostgreSQL Flexible Server with `pgvector` enabled:

```
DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/<dbname>
```

## Docker

A minimal image is provided via `Dockerfile`:

```bash
docker build -t memory-store:local .
docker run --rm -p 8000:8000 \
  -e DATABASE_URL=postgresql+psycopg://... \
  memory-store:local
```

Publish the image to your container registry and reference it from the Container Apps module when ready to deploy.

## Database schema

The table definition lives in `memory_records.sql` in this directory.

Apply it once per environment:

```bash
psql "${DATABASE_URL}" -f memory_records.sql
```

Indexes use cosine distance (`vector_cosine_ops`), so make sure your embeddings are normalized before writing them.
