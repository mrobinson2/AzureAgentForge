<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Memory Store Architecture (pgvector)

> 🚧 **Design target — not deployed.** This is reference scaffolding for the multi-tenant memory store. The single-tenant stack in this repository is what actually deploys and what CI validates. Treat this as a roadmap, not a shipped feature.

This document describes the PostgreSQL-based memory system that can replace Azure AI Search in dev/test, while keeping a clean migration path back to Azure Search later.

## Components

1. **Schema** – `memory_records.sql` creates the `memory_records` table, indexes, and trigger in an Azure PostgreSQL Flexible Server. Run once per environment.
2. **Service** – FastAPI app (this directory) exposes `/memory`, `/memory/search`, and `/healthz` so agent runtimes can talk to the store over HTTP.
3. **Terraform toggle** – `enable_ai_search` (default `false`). When `false`, the Container Apps module receives `memory_service_url` instead of provisioning Azure Search.

## Deployment workflow

1. Apply schema:
   ```bash
   export DATABASE_URL="postgresql+psycopg://<user>:<password>@<host>:5432/<dbname>"
   psql "$DATABASE_URL" -f memory_records.sql
   ```
2. Build + push the image:
   ```bash
   docker build -t <registry>.azurecr.io/memory-store:<tag> .
   az acr login --name <registry>
   docker push <registry>.azurecr.io/memory-store:<tag>
   ```
3. Update the Container Apps module (or Helm release) to deploy the new image and set `DATABASE_URL` (use Key Vault secret references).
4. Set `memory_service_url` to the internal Container Apps hostname (e.g., `http://memory-store.<env>.internal`) so other services route to pgvector instead of Azure Search.
5. (Optional) If/when you need Azure AI Search again, flip `enable_ai_search` back to `true`, dual-write from the memory service to Azure Search, and update the route map.

## Client contract

Request payloads intentionally mirror the Azure Search fields:

```json
{
  "tenant_id": "tenant-a",
  "record_id": "thread-123",
  "record_type": "journal",
  "content": "Summarized insight...",
  "content_vector": [0.1, 0.2, "..."],
  "tags": ["project", "retro"],
  "status": "ready",
  "metadata": {"source": "api"}
}
```

Search payload:
```json
{
  "query_vector": [0.09, -0.11, "..."],
  "tenant_id": "tenant-a",
  "record_types": ["journal", "artifact"],
  "limit": 10,
  "min_score": 0.35
}
```

Responses include the cosine similarity score (`0-1`), so callers can keep their existing ranking logic.

## Rollback steps

1. Stop the Container App for memory-store.
2. Set `enable_ai_search=true` and `memory_service_url` to the Azure Search endpoint.
3. Re-run `terraform apply` to reprovision Azure Search.
4. Delete the `memory_records` table if desired (optional).

Keeping both the schema and the service in-repo means pgvector can run locally, in dev, or in prod without burning Azure AI Search spend until it's needed.
