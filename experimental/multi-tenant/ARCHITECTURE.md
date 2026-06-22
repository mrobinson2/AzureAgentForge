<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Multi-Tenant Architecture (Reference / Roadmap)

> 🚧 **Design target — not deployed.** This is a reference architecture for multi-tenancy. The single-tenant stack in this repository is what actually deploys and what CI validates. The multi-tenant *design* is ~complete; the *implementation* is partial (~20–30%) and has never been deployed. Treat this as a roadmap, not a shipped feature.

This document describes the intended multi-tenant design for AzureAgentForge — the path from a single-tenant deployment on Azure Container Apps to a fully isolated multi-tenant platform. It covers data-layer isolation via PostgreSQL RLS, per-tenant agent routing, Terraform module design, onboarding flow, cost modeling, and security controls. Use it as a reference architecture when planning the multi-tenant implementation.

---

## Table of Contents

1. [Tenant Isolation Strategy](#1-tenant-isolation-strategy)
2. [Data Layer Changes](#2-data-layer-changes)
3. [Hermes Agent Isolation](#3-hermes-agent-isolation)
4. [Model Router Multi-Tenancy](#4-model-router-multi-tenancy)
5. [Paperclip Orchestrator](#5-paperclip-orchestrator)
6. [Cloudflare Tunnel](#6-cloudflare-tunnel)
7. [Key Vault & Secrets](#7-key-vault--secrets)
8. [Terraform Module Design](#8-terraform-module-design)
9. [Onboarding Flow](#9-onboarding-flow)
10. [Cost Model](#10-cost-model)
11. [Security](#11-security)
12. [Migration Plan](#12-migration-plan)

---

## 1. Tenant Isolation Strategy

### Recommendation: Schema-per-Tenant with Row-Level Security (RLS)

The platform uses a single PostgreSQL 15 instance (B_Standard_B1ms, 32 GiB) with two databases: `honcho` and `paperclip`. Full database-per-tenant isolation is overkill at this scale and adds operational overhead for connection pooling and migrations. Schema-per-tenant with RLS provides strong isolation without multiplying infrastructure.

**Isolation tiers:**

| Layer | Isolation Mechanism |
|-------|-------------------|
| Database | Shared instance, shared databases (`honcho`, `paperclip`) |
| Schema | `public` schema with `tenant_id` column on every table |
| Row access | PostgreSQL RLS policies enforced at the database level |
| Application | `tenant_id` injected via JWT claims, propagated to every query |
| Network | Shared ACA environment, per-tenant routing via Cloudflare |
| Secrets | Tenant-prefixed keys in shared Key Vault (`aaf-dev-kv`) |

**Why not database-per-tenant:**
- Single Flex Server (B1ms) supports ~50 connections; separate databases exhaust the pool quickly.
- Migrations must be run N times instead of once.
- Cross-tenant reporting (admin dashboard, billing) requires cross-database queries.

**Why not schema-per-tenant (separate schemas):**
- Honcho uses Alembic migrations that target a single schema. Forking per schema adds migration complexity.
- Paperclip runs Drizzle migrations that expect `public` schema.
- RLS on `public` achieves equivalent isolation with simpler operations.

---

## 2. Data Layer Changes

### 2.1 Honcho Database

Honcho tables currently have no tenant awareness. The `app_id` column in some tables is close but not equivalent to a tenant identifier.

**Tables requiring `tenant_id`:**

| Table | Current PK / FK | Change |
|-------|----------------|--------|
| `workspaces` | `id` | Add `tenant_id UUID NOT NULL` |
| `peers` | `id`, `workspace_id` | Inherits via workspace; add direct `tenant_id` for RLS |
| `sessions` | `id`, `peer_id` | Add `tenant_id UUID NOT NULL` |
| `messages` | `id`, `session_id` | Add `tenant_id UUID NOT NULL` |
| `message_embeddings` | `id`, `message_id` | Add `tenant_id UUID NOT NULL` |
| `documents` | `id` | Add `tenant_id UUID NOT NULL` |
| `collections` | `id` | Add `tenant_id UUID NOT NULL` |
| `queue` | `id` | Add `tenant_id UUID NOT NULL` |

**Migration SQL:**

```sql
-- Step 1: Add tenant_id to all Honcho tables (default to existing tenant during migration)
-- Run inside the honcho database.

-- Create a tenants reference table in honcho DB
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Seed the existing single tenant
INSERT INTO tenants (slug) VALUES ('operator')
ON CONFLICT (slug) DO NOTHING;

-- Add tenant_id column to each table with a default pointing to the existing tenant
DO $$
DECLARE
    existing_tenant_id UUID;
BEGIN
    SELECT id INTO existing_tenant_id FROM tenants WHERE slug = 'operator';

    -- workspaces
    ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE workspaces SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE workspaces ALTER COLUMN tenant_id SET NOT NULL;
    ALTER TABLE workspaces ADD CONSTRAINT fk_workspaces_tenant
        FOREIGN KEY (tenant_id) REFERENCES tenants(id);

    -- peers
    ALTER TABLE peers ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE peers SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE peers ALTER COLUMN tenant_id SET NOT NULL;

    -- sessions
    ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE sessions SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE sessions ALTER COLUMN tenant_id SET NOT NULL;

    -- messages
    ALTER TABLE messages ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE messages SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE messages ALTER COLUMN tenant_id SET NOT NULL;

    -- message_embeddings
    ALTER TABLE message_embeddings ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE message_embeddings SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE message_embeddings ALTER COLUMN tenant_id SET NOT NULL;

    -- documents
    ALTER TABLE documents ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE documents SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE documents ALTER COLUMN tenant_id SET NOT NULL;

    -- collections
    ALTER TABLE collections ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE collections SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE collections ALTER COLUMN tenant_id SET NOT NULL;

    -- queue
    ALTER TABLE queue ADD COLUMN IF NOT EXISTS tenant_id UUID;
    UPDATE queue SET tenant_id = existing_tenant_id WHERE tenant_id IS NULL;
    ALTER TABLE queue ALTER COLUMN tenant_id SET NOT NULL;
END $$;

-- Step 2: Create indexes for tenant-scoped queries
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces (tenant_id);
CREATE INDEX IF NOT EXISTS idx_peers_tenant ON peers (tenant_id);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_messages_tenant ON messages (tenant_id);
CREATE INDEX IF NOT EXISTS idx_message_embeddings_tenant ON message_embeddings (tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_tenant ON documents (tenant_id);
CREATE INDEX IF NOT EXISTS idx_collections_tenant ON collections (tenant_id);
CREATE INDEX IF NOT EXISTS idx_queue_tenant ON queue (tenant_id);
```

**RLS policies for Honcho (see Section 11 for full policy listing):**

```sql
-- Step 3: Enable RLS on all Honcho tables
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE peers ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE collections ENABLE ROW LEVEL SECURITY;
ALTER TABLE queue ENABLE ROW LEVEL SECURITY;

-- Application role (used by Honcho service)
CREATE ROLE honcho_app LOGIN;

-- RLS policy pattern: applied to each table
-- The app sets current_setting('app.tenant_id') on every connection.
CREATE POLICY tenant_isolation_workspaces ON workspaces
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_peers ON peers
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_sessions ON sessions
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_messages ON messages
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_message_embeddings ON message_embeddings
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_documents ON documents
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_collections ON collections
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

CREATE POLICY tenant_isolation_queue ON queue
    USING (tenant_id = current_setting('app.tenant_id')::UUID)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::UUID);

-- Grant the app role access to all tables
GRANT ALL ON ALL TABLES IN SCHEMA public TO honcho_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO honcho_app;
```

### 2.2 Paperclip Database

Paperclip already has a multi-company model -- its `company` table and `company_id` foreign keys serve as a natural tenant boundary. The mapping is:

```
tenant.slug  <-->  paperclip.company.slug
tenant.id    <-->  paperclip.company.id
```

**Changes needed:**
- Add RLS policies to all Paperclip tables keyed on `company_id` (same `current_setting('app.tenant_id')` pattern).
- Add a `tenant_id` column to `company` that references the platform `tenants` table (or use `company.id` directly as the tenant identifier).
- Ensure the Paperclip auth layer (Better Auth) includes `company_id` in JWT claims.

### 2.3 Connection Middleware

Every database connection must set the tenant context before executing queries. This is done via a session-scoped `SET` on each connection checkout.

**Python (Honcho / SQLAlchemy):**

```python
from sqlalchemy import event
from sqlalchemy.pool import Pool

@event.listens_for(Pool, "checkout")
def set_tenant_on_checkout(dbapi_conn, connection_record, connection_proxy):
    """Set tenant context from the current request."""
    # tenant_id comes from the request context (JWT, header, etc.)
    tenant_id = get_current_tenant_id()
    if tenant_id:
        cursor = dbapi_conn.cursor()
        cursor.execute("SET app.tenant_id = %s", (str(tenant_id),))
        cursor.close()
```

**TypeScript (Paperclip / Drizzle):**

```typescript
// middleware/tenant.ts
import { sql } from "drizzle-orm";

export async function withTenant<T>(
  db: DrizzleClient,
  tenantId: string,
  fn: (tx: DrizzleClient) => Promise<T>
): Promise<T> {
  return db.transaction(async (tx) => {
    await tx.execute(sql`SET LOCAL app.tenant_id = ${tenantId}`);
    return fn(tx);
  });
}
```

---

## 3. Hermes Agent Isolation

### Current State

Hermes runs as a single container (`ca-agent-runtime`) with one Telegram bot token. It uses a persistent Azure File Share at `/opt/data` for `config.yaml`, sessions, and skills. All agent personas (orchestrator, planner, researcher, etc.) share the same Honcho app (`hermes-dev`) and the same model router sidecar.

### Multi-Tenant Design

**Option A (recommended for <20 tenants): Shared Hermes with tenant-scoped Honcho apps**

Each tenant gets a separate Honcho `app_id` (e.g., `hermes-dev-operator`, `hermes-dev-acme`). Hermes resolves the tenant from the inbound channel (Telegram chat ID, Paperclip company ID) and passes the tenant-scoped `app_id` to Honcho.

```
Tenant "operator"  -->  HONCHO_APP_ID = "hermes-dev-operator"
Tenant "acme"     -->  HONCHO_APP_ID = "hermes-dev-acme"
```

Changes to Hermes:
- Maintain a `tenant_id -> honcho_app_id` mapping (loaded from config or platform API).
- Pass `tenant_id` in the `X-Tenant-ID` header to the router sidecar for budget tracking.
- Use tenant-scoped directories on the file share: `/opt/data/{tenant_slug}/config.yaml`.

**Option B (>20 tenants): Dedicated Hermes container per tenant**

Deploy `ca-hermes-{tenant_slug}-{env}` as a separate ACA container app per tenant. Each gets its own:
- Telegram bot token (from Key Vault: `platform-telegram-{slug}-token`)
- File share mount: `hermes-data-{slug}`
- Honcho app ID: `hermes-{env}-{slug}`
- Router sidecar with tenant budget env vars

This is managed via Terraform modules (see Section 8).

### Session Isolation

Regardless of option, Honcho session isolation is enforced by:
1. RLS on the `sessions` table (tenant_id filter).
2. Separate `app_id` per tenant in the Honcho API.
3. Telegram chat IDs are unique -- no cross-tenant collision risk.

---

## 4. Model Router Multi-Tenancy

### Current State

Budget tracking in `services/model-router/main.py` uses a global in-memory dict:

```python
_spend: dict[str, float] = defaultdict(float)  # keyed by tier name
```

This tracks spend per model tier (gpt4o-mini, phi4, etc.) with a daily reset. There is no tenant dimension.

### Multi-Tenant Budget Tracking

Replace the flat `_spend` dict with a nested `tenant -> tier -> spend` structure, backed by PostgreSQL for persistence across restarts.

**Python changes (`services/model-router/main.py`):**

```python
from collections import defaultdict
from datetime import date
from typing import Optional
import os
import psycopg2
from psycopg2.extras import RealDictCursor

# ── Per-Tenant Budget Tracking ──────────────────────────────────────────────

# In-memory cache, flushed to DB periodically
_spend: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
_budget_date: str = ""

# Per-tenant daily budget overrides (loaded from DB or env)
_tenant_budgets: dict[str, dict[str, float]] = {}

# Default budgets (used when tenant has no override)
DEFAULT_BUDGETS: dict[str, float] = {
    "gpt4o-mini": 5.00,
    "phi4": 0.50,
    "kimi": 0.25,
    "claude": 0.25,
    "grok": 2.00,
}


def _get_tenant_id(request_headers: dict) -> str:
    """Extract tenant ID from X-Tenant-ID header. Falls back to 'default'."""
    return request_headers.get("x-tenant-id", "default")


def _reset_if_new_day() -> None:
    global _budget_date
    today = str(date.today())
    if today != _budget_date:
        _budget_date = today
        _spend.clear()
        _flush_to_db()


def record_cost(tenant_id: str, tier: str, cost: float) -> None:
    """Record spend for a specific tenant and tier."""
    _reset_if_new_day()
    _spend[tenant_id][tier] += cost


def is_over_budget(tenant_id: str, tier: str) -> bool:
    """Check if a tenant has exceeded their daily budget for a tier."""
    _reset_if_new_day()
    limit = _tenant_budgets.get(tenant_id, DEFAULT_BUDGETS).get(
        tier, DEFAULT_BUDGETS.get(tier, 999.0)
    )
    return _spend[tenant_id].get(tier, 0.0) >= limit


def get_budget_status(tenant_id: str) -> dict:
    """Return budget status for a single tenant."""
    _reset_if_new_day()
    budgets = _tenant_budgets.get(tenant_id, DEFAULT_BUDGETS)
    return {
        "tenant_id": tenant_id,
        "date": _budget_date,
        "tiers": {
            tier: {
                "spent": _spend[tenant_id].get(tier, 0.0),
                "limit": budgets.get(tier, DEFAULT_BUDGETS.get(tier, 999.0)),
                "over_budget": is_over_budget(tenant_id, tier),
            }
            for tier in MODELS
        },
    }


def _flush_to_db() -> None:
    """Persist current spend snapshot to PostgreSQL for durability."""
    conn_str = os.environ.get("BUDGET_DB_CONNSTR")
    if not conn_str:
        return
    try:
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        for tenant_id, tiers in _spend.items():
            for tier, amount in tiers.items():
                cur.execute("""
                    INSERT INTO budget_spend (tenant_id, tier, spend_date, amount)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, tier, spend_date)
                    DO UPDATE SET amount = EXCLUDED.amount
                """, (tenant_id, tier, _budget_date, amount))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[budget] flush error: {e}")
```

**Budget tracking table:**

```sql
CREATE TABLE IF NOT EXISTS budget_spend (
    tenant_id   TEXT NOT NULL,
    tier        TEXT NOT NULL,
    spend_date  DATE NOT NULL,
    amount      NUMERIC(10, 4) NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, tier, spend_date)
);

CREATE TABLE IF NOT EXISTS tenant_budget_limits (
    tenant_id   TEXT NOT NULL,
    tier        TEXT NOT NULL,
    daily_limit NUMERIC(10, 4) NOT NULL,
    PRIMARY KEY (tenant_id, tier)
);
```

### Request Flow

```
Hermes/Paperclip
    |  X-Tenant-ID: <tenant_slug>
    v
Router (localhost:8080)
    |  1. Extract tenant_id from header
    |  2. Check is_over_budget(tenant_id, tier)
    |  3. Route to model
    |  4. record_cost(tenant_id, tier, cost)
    v
Azure AI Foundry / Model Endpoint
```

---

## 5. Paperclip Orchestrator

### Companies = Tenants

Paperclip already models multi-company. The mapping is direct:

| Platform Concept | Paperclip Concept |
|-----------------|-------------------|
| Tenant | Company |
| `tenants.slug` | `company.slug` |
| `tenants.id` | `company.id` (or linked via FK) |
| Tenant user | Company member |
| Tenant agent config | Company settings |

### Changes Required

1. **JWT claims:** Better Auth JWTs must include `company_id` (already present as session context). Propagate to all API handlers.

2. **Agent spawning:** When Paperclip spawns a Hermes agent process, inject the tenant context:
   ```
   HONCHO_APP_ID=hermes-{env}-{tenant_slug}
   X-Tenant-ID={tenant_slug}  (passed to router)
   ```

3. **File share isolation:** Move from `/paperclip/` to `/paperclip/{tenant_slug}/` for persistent data. Each tenant gets a subdirectory.

4. **Board mutations:** The existing CSRF guard (`board-mutation-guard`) already validates per-company. No changes needed.

5. **Admin view:** Add a platform admin role that can see all companies for billing/support purposes.

---

## 6. Cloudflare Tunnel

### Current State

One tunnel (`dev-azureagentforge`) routes:
- `app.example.com` --> `http://ca-orchestrator` (internal ACA)

### Multi-Tenant Routing

**Option A (recommended): Wildcard subdomain routing**

Configure the tunnel with a wildcard rule:

```
*.app.example.com --> http://ca-orchestrator
```

Paperclip resolves the tenant from the subdomain:
- `acme.app.example.com` --> tenant `acme`
- `tenant2.app.example.com` --> tenant `tenant2`

Cloudflare dashboard config:
```
Public Hostname: *.app.example.com
Service:         http://ca-orchestrator
```

The subdomain is extracted in Paperclip middleware:

```typescript
// middleware/tenant-resolver.ts
export function resolveTenantFromHost(host: string): string | null {
  // host = "acme.app.example.com"
  const match = host.match(/^([a-z0-9-]+)\.app\./);
  return match ? match[1] : null;
}
```

**Option B (>50 tenants): Separate tunnels per tenant**

Not recommended unless tenants need fully isolated network paths.

**DNS setup:**

```
*.app.example.com  CNAME  <tunnel-id>.cfargotunnel.com
```

This single CNAME covers all tenant subdomains. Cloudflare handles TLS termination.

### Production Consideration

For production, use a separate tunnel (`prod-azureagentforge`) with:
```
*.app.example.com --> http://ca-orchestrator-prod
```

---

## 7. Key Vault & Secrets

### Current State

All secrets in `aaf-dev-kv` use a `platform-` prefix:
- `platform-telegram-devbot-token`
- `platform-paperclip-db-url`
- `platform-azure-ai-foundry-*`

### Multi-Tenant Secret Naming

Add a tenant prefix layer:

```
platform-{tenant_slug}-{service}-{secret_name}
```

**Examples:**

| Secret Name | Purpose |
|------------|---------|
| `platform-operator-telegram-bot-token` | Operator's Telegram bot |
| `platform-acme-telegram-bot-token` | ACME's Telegram bot |
| `platform-operator-paperclip-db-url` | Operator's Paperclip DB URL |
| `platform-shared-ai-foundry-project-api-key` | Shared AI Foundry key |

**Shared vs. tenant-specific:**

| Category | Naming | Example |
|----------|--------|---------|
| Shared infrastructure | `platform-shared-*` | `platform-shared-postgresql-connection-string` |
| Tenant-specific | `platform-{slug}-*` | `platform-acme-telegram-bot-token` |
| Global platform | `platform-*` (no slug) | `platform-cloudflared-token` |

### Key Vault Access

Each tenant's managed identity gets `Key Vault Secrets User` scoped to their specific secrets via access policy conditions:

```hcl
resource "azurerm_role_assignment" "tenant_kv_reader" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.tenant[each.key].principal_id

  # Condition: only secrets matching the tenant prefix
  condition = <<-EOT
    @Resource[Microsoft.KeyVault/vaults/secrets].name StringStartsWith 'platform-${each.key}-'
    OR
    @Resource[Microsoft.KeyVault/vaults/secrets].name StringStartsWith 'platform-shared-'
  EOT
  condition_version = "2.0"
}
```

---

## 8. Terraform Module Design

### Tenant as a Module

Create a `modules/tenant` module that encapsulates all per-tenant resources.

**File structure:**

```
infrastructure/
  modules/
    tenant/
      main.tf          # Orchestrates sub-resources
      variables.tf     # Tenant config inputs
      outputs.tf       # Tenant resource IDs
      identity.tf      # Managed identity + RBAC
      storage.tf       # Per-tenant file shares
      secrets.tf       # Key Vault secret references
      dns.tf           # Cloudflare DNS records (optional)
```

**`modules/tenant/variables.tf`:**

```hcl
variable "tenant_slug" {
  type        = string
  description = "URL-safe tenant identifier (e.g., 'acme', 'operator')"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.tenant_slug))
    error_message = "Tenant slug must be lowercase alphanumeric with hyphens only."
  }
}

variable "environment" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "container_app_environment_id" {
  type = string
}

variable "key_vault_id" {
  type = string
}

variable "key_vault_uri" {
  type = string
}

variable "container_registry_id" {
  type = string
}

variable "container_registry_login_server" {
  type = string
}

variable "storage_account_name" {
  type = string
}

variable "storage_account_access_key" {
  type      = string
  sensitive = true
}

variable "plan" {
  type    = string
  default = "personal"

  validation {
    condition     = contains(["personal", "team", "enterprise"], var.plan)
    error_message = "Plan must be one of: personal, team, enterprise."
  }
}

variable "enable_dedicated_hermes" {
  type        = bool
  default     = false
  description = "Deploy a dedicated Hermes container app for this tenant."
}

variable "telegram_bot_token_secret_id" {
  type        = string
  default     = ""
  description = "Key Vault secret ID for this tenant's Telegram bot token."
}

variable "daily_budget_usd" {
  type    = number
  default = 5.0
}

variable "tags" {
  type    = map(string)
  default = {}
}
```

**`modules/tenant/main.tf`:**

```hcl
# Per-Tenant Resource Module
# Creates: managed identity, storage, RBAC, optional dedicated Hermes

locals {
  tenant_prefix = "platform-${var.tenant_slug}"
}

# ── Managed Identity ──────────────────────────────────────────────────────────
resource "azurerm_user_assigned_identity" "tenant" {
  name                = "id-tenant-${var.tenant_slug}-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

# ── RBAC: ACR Pull ────────────────────────────────────────────────────────────
resource "azurerm_role_assignment" "tenant_acr_pull" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.tenant.principal_id
}

# ── RBAC: Key Vault (scoped to tenant prefix) ────────────────────────────────
resource "azurerm_role_assignment" "tenant_kv_reader" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.tenant.principal_id
}

# ── Per-Tenant File Share ─────────────────────────────────────────────────────
resource "azurerm_storage_share" "tenant_data" {
  name                 = "tenant-${var.tenant_slug}-data"
  storage_account_name = var.storage_account_name
  quota                = var.plan == "enterprise" ? 50 : 10
}

resource "azurerm_container_app_environment_storage" "tenant_data" {
  name                         = "tenant-${var.tenant_slug}-data"
  container_app_environment_id = var.container_app_environment_id
  account_name                 = var.storage_account_name
  share_name                   = azurerm_storage_share.tenant_data.name
  access_key                   = var.storage_account_access_key
  access_mode                  = "ReadWrite"
}

# ── Dedicated Hermes (optional) ──────────────────────────────────────────────
resource "azurerm_container_app" "hermes" {
  count = var.enable_dedicated_hermes ? 1 : 0

  name                         = "ca-hermes-${var.tenant_slug}-${var.environment}"
  container_app_environment_id = var.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.tenant.id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.tenant.id
  }

  # Tenant-specific Telegram token
  dynamic "secret" {
    for_each = var.telegram_bot_token_secret_id != "" ? [1] : []
    content {
      name                = "telegram-bot-token"
      key_vault_secret_id = var.telegram_bot_token_secret_id
      identity            = azurerm_user_assigned_identity.tenant.id
    }
  }

  template {
    min_replicas = 1
    max_replicas = 1

    volume {
      name         = "tenant-data"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.tenant_data.name
    }

    container {
      name   = "hermes"
      image  = "${var.container_registry_login_server}/hermes:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "HONCHO_APP_ID"
        value = "hermes-${var.environment}-${var.tenant_slug}"
      }
      env {
        name  = "HERMES_HOME"
        value = "/opt/data"
      }
      env {
        name  = "HERMES_DB_PATH"
        value = "/tmp/hermes-state.db"
      }

      volume_mounts {
        name = "tenant-data"
        path = "/opt/data"
      }
    }
  }

  tags = merge(var.tags, {
    Tenant = var.tenant_slug
  })
}
```

**`modules/tenant/outputs.tf`:**

```hcl
output "tenant_identity_id" {
  value = azurerm_user_assigned_identity.tenant.id
}

output "tenant_identity_principal_id" {
  value = azurerm_user_assigned_identity.tenant.principal_id
}

output "tenant_storage_share_name" {
  value = azurerm_storage_share.tenant_data.name
}

output "hermes_app_name" {
  value = var.enable_dedicated_hermes ? azurerm_container_app.hermes[0].name : null
}
```

### Tenant Registry in `dev/main.tf`

```hcl
# Tenant definitions — add a new block to onboard a tenant
locals {
  tenants = {
    operator = {
      plan                    = "personal"
      enable_dedicated_hermes = false
      daily_budget_usd        = 5.0
    }
    # acme = {
    #   plan                    = "team"
    #   enable_dedicated_hermes = true
    #   daily_budget_usd        = 20.0
    #   telegram_bot_token_secret_id = "${module.keyvault.uri}secrets/platform-acme-telegram-bot-token"
    # }
  }
}

module "tenant" {
  for_each = local.tenants
  source   = "../../modules/tenant"

  tenant_slug                     = each.key
  environment                     = var.environment
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  container_app_environment_id    = module.container_apps.environment_id
  key_vault_id                    = module.keyvault.id
  key_vault_uri                   = module.keyvault.uri
  container_registry_id           = module.container_registry.id
  container_registry_login_server = module.container_registry.login_server
  storage_account_name            = module.container_apps.storage_account_name
  storage_account_access_key      = module.container_apps.storage_account_access_key
  plan                            = each.value.plan
  enable_dedicated_hermes         = each.value.enable_dedicated_hermes
  daily_budget_usd                = each.value.daily_budget_usd
  telegram_bot_token_secret_id    = lookup(each.value, "telegram_bot_token_secret_id", "")
  tags                            = local.common_tags
}
```

---

## 9. Onboarding Flow

### Step-by-Step Tenant Onboarding

```
1. Admin creates tenant record
   POST /tenants { slug: "acme", display_name: "ACME Corp", ... }
   --> Creates row in platform tenants table
   --> Creates Azure AI Search index (mem-acme)
   --> Creates initial user + default channel

2. Terraform adds tenant to locals
   Edit infrastructure/environments/dev/main.tf:
     locals.tenants.acme = { plan = "team", ... }

3. Terraform apply
   --> Creates: managed identity, file share, RBAC, optional Hermes app
   --> Assigns Key Vault permissions

4. Seed Key Vault secrets
   az keyvault secret set --vault-name aaf-dev-kv \
     --name platform-acme-telegram-bot-token \
     --value "<token>"

5. Run database migration (from within VNet)
   INSERT INTO honcho.tenants (slug) VALUES ('acme');
   -- All existing Honcho tables get tenant_id via the provisioning API

6. Configure Cloudflare
   Add DNS record (if not using wildcard):
     acme.app.example.com CNAME <tunnel>.cfargotunnel.com

7. Verify
   curl https://acme.app.example.com/api/health
   --> { "status": "healthy", "tenant": "acme" }

8. Notify tenant
   Send welcome email with:
   - Login URL: https://acme.app.example.com
   - Telegram bot setup instructions (if applicable)
   - Budget limits and plan details
```

### Automated Onboarding (Future)

The platform API (`platform-api/main.py`) already handles steps 1 and 5 (search index). Extend it to:
- Trigger a GitHub Actions workflow for Terraform apply (step 3).
- Call the Cloudflare API for DNS setup (step 6).
- Send the welcome notification (step 8).

---

## 10. Cost Model

> **Note:** All figures in this section are illustrative design estimates, not measured costs from a running deployment.

### Per-Tenant Resource Estimates

**Shared infrastructure (fixed, amortized across tenants):**

| Resource | SKU | Monthly Cost | Notes |
|----------|-----|-------------|-------|
| PostgreSQL Flex Server | B_Standard_B1ms | ~$25 | Shared across all tenants |
| ACA Environment | Consumption | ~$0 | Pay-per-use only |
| Key Vault | Standard | ~$3 | Shared, per-operation billing |
| ACR | Basic | ~$5 | Shared image registry |
| Log Analytics | Pay-per-GB | ~$10 | Shared workspace |
| Cloudflare Tunnel | Free | $0 | Included in CF plan |
| **Total shared** | | **~$43/mo** | |

**Per-tenant marginal cost (shared Hermes mode):**

| Resource | Monthly Cost | Notes |
|----------|-------------|-------|
| File share (10 GiB) | ~$0.60 | Azure Files, LRS |
| AI Search index | ~$0 | Shared service, per-doc billing |
| Managed identity | $0 | Free resource |
| ACA compute (marginal) | ~$0-5 | Shared containers, no extra replicas |
| AI model spend | $5-50 | Depends on plan tier |
| **Per-tenant total** | **~$6-56/mo** | |

**Per-tenant marginal cost (dedicated Hermes mode):**

| Resource | Monthly Cost | Notes |
|----------|-------------|-------|
| All of shared mode | ~$6-56 | Same as above |
| Dedicated Hermes ACA (0.5 vCPU, 1Gi) | ~$15 | Always-on for Telegram polling |
| Dedicated router sidecar (0.25 vCPU, 0.5Gi) | ~$8 | Paired with Hermes |
| **Per-tenant total** | **~$29-79/mo** | |

### Plan Tiers

| Plan | Daily AI Budget | Dedicated Hermes | Storage | Price Target |
|------|----------------|-----------------|---------|-------------|
| Personal | $5/day | No (shared) | 10 GiB | $10/mo |
| Team | $20/day | Optional | 25 GiB | $30/mo |
| Enterprise | $100/day | Yes | 50 GiB | $100/mo |

---

## 11. Security

### 11.1 Row-Level Security Enforcement

RLS is the primary tenant isolation mechanism. It is enforced at the database level, making it impossible to bypass from application code.

**Critical rules:**

1. **Every table** with tenant data has RLS enabled.
2. **Every connection** sets `app.tenant_id` before executing queries.
3. **Superuser bypass:** The `postgres` admin user bypasses RLS. Application services use the `honcho_app` / `paperclip_app` roles which are subject to RLS.
4. **Default deny:** Tables with RLS enabled and no matching policy deny all access.

**Verification query:**

```sql
-- Check RLS is enabled on all tenant tables
SELECT schemaname, tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'workspaces', 'peers', 'sessions', 'messages',
    'message_embeddings', 'documents', 'collections', 'queue'
  );
-- All rows should show rowsecurity = true
```

### 11.2 Audit Logging

Create an audit table that captures all tenant-scoped mutations:

```sql
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL,
    table_name  TEXT NOT NULL,
    operation   TEXT NOT NULL,  -- INSERT, UPDATE, DELETE
    row_id      TEXT NOT NULL,
    old_data    JSONB,
    new_data    JSONB,
    performed_by TEXT,
    performed_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_audit_tenant ON audit_log (tenant_id, performed_at);

-- Generic audit trigger function
CREATE OR REPLACE FUNCTION audit_trigger_func()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO audit_log (tenant_id, table_name, operation, row_id, old_data, new_data, performed_by)
    VALUES (
        COALESCE(NEW.tenant_id, OLD.tenant_id),
        TG_TABLE_NAME,
        TG_OP,
        COALESCE(NEW.id::TEXT, OLD.id::TEXT),
        CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END,
        CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) END,
        current_setting('app.tenant_id', true)
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- Apply to all tenant tables
CREATE TRIGGER audit_workspaces
    AFTER INSERT OR UPDATE OR DELETE ON workspaces
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

CREATE TRIGGER audit_sessions
    AFTER INSERT OR UPDATE OR DELETE ON sessions
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

CREATE TRIGGER audit_messages
    AFTER INSERT OR UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

-- (Repeat for peers, documents, collections, queue, message_embeddings)
```

### 11.3 Cross-Tenant Prevention Checklist

| Attack Vector | Mitigation |
|--------------|-----------|
| SQL injection bypassing RLS | RLS is enforced at DB level; parameterized queries in app |
| Missing `tenant_id` on connection | Middleware rejects requests without valid tenant JWT |
| Direct DB access without `SET app.tenant_id` | App role has no default access (RLS default deny) |
| Tenant A guessing Tenant B's resource IDs | UUIDs are unguessable; RLS prevents access even if guessed |
| Shared model router leaking context | Router is stateless per-request; no cross-tenant state |
| File share traversal | Per-tenant mount paths; ACA volumes are isolated |
| Key Vault secret enumeration | RBAC condition limits to `platform-{slug}-*` prefix |
| Honcho session hijacking | RLS on sessions table + app_id scoping |
| Telegram bot cross-talk | Each tenant has a unique bot token; chat IDs are per-bot |

### 11.4 Penetration Testing Queries

Run these periodically to verify isolation:

```sql
-- As honcho_app role, try to access another tenant's data
SET ROLE honcho_app;
SET app.tenant_id = '<tenant_a_id>';
SELECT count(*) FROM sessions WHERE tenant_id = '<tenant_b_id>';
-- Expected: 0 rows (RLS filters it out)

-- Verify no rows leak without tenant context set
RESET app.tenant_id;
SELECT count(*) FROM sessions;
-- Expected: 0 rows (default deny)
```

---

## 12. Migration Plan

### Phase 1: Foundation (Week 1-2)

**Goal:** Add tenant_id columns and RLS without breaking existing functionality.

1. Create `tenants` table in both `honcho` and `paperclip` databases.
2. Seed the existing tenant (`operator`).
3. Add `tenant_id` column to all Honcho tables (nullable initially, backfill, then set NOT NULL).
4. Add indexes on `tenant_id`.
5. Create application database roles (`honcho_app`, `paperclip_app`).
6. Enable RLS on all tables with policies.
7. Deploy Honcho with connection middleware that sets `app.tenant_id`.
8. Verify: existing functionality works with RLS active.

**Rollback:** Drop RLS policies and revert to superuser role if issues arise.

### Phase 2: Router + Hermes (Week 3-4)

**Goal:** Add per-tenant budget tracking and tenant-aware agent routing.

1. Deploy router changes (nested `_spend` dict, `X-Tenant-ID` header).
2. Create `budget_spend` and `tenant_budget_limits` tables.
3. Update Hermes to pass `X-Tenant-ID` to router sidecar.
4. Update Hermes to use tenant-scoped `HONCHO_APP_ID`.
5. Add tenant-scoped file share directories (`/opt/data/{slug}/`).
6. Create `modules/tenant` Terraform module.
7. Deploy first tenant module (`operator`) -- no behavior change, just structured.

**Rollback:** Router falls back to flat `_spend` dict if `X-Tenant-ID` header is missing.

### Phase 3: Cloudflare + Paperclip (Week 5-6)

**Goal:** Enable tenant-scoped web access and onboarding API.

1. Configure wildcard DNS: `*.app.example.com`.
2. Add tenant-resolver middleware to Paperclip.
3. Update Paperclip to set `app.tenant_id` on all DB connections.
4. Update Better Auth JWT to include `company_id` / `tenant_id`.
5. Extend platform API onboarding endpoint to trigger Terraform.
6. Deploy tenant-prefixed Key Vault secrets for existing tenant.
7. Create the `audit_log` table and triggers.

**Rollback:** Remove wildcard DNS; revert to single-hostname routing.

### Phase 4: Production + Second Tenant (Week 7-8)

**Goal:** Onboard a second tenant end-to-end and validate isolation.

1. Onboard a test tenant (`test-tenant`) using the full flow (Section 9).
2. Run cross-tenant penetration tests (Section 11.4).
3. Verify budget isolation -- `test-tenant` spend does not affect `operator`.
4. Verify Honcho session isolation -- no data leakage across tenants.
5. Load test: simulate concurrent requests from both tenants.
6. Document operational runbooks for tenant management.
7. Plan production environment (`prod-azureagentforge` tunnel, `*.app.example.com`).
8. Promote to production after 1 week of stable dev operation.

### Timeline Summary

```
Week 1-2:  [Phase 1] Database + RLS .................. Foundation
Week 3-4:  [Phase 2] Router + Hermes ................. Tenant-aware services
Week 5-6:  [Phase 3] Cloudflare + Paperclip .......... Public access + onboarding
Week 7-8:  [Phase 4] Second tenant + production ...... Validation + go-live
```

### Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| RLS breaks existing queries | Medium | High | Phase 1 includes full regression testing |
| PostgreSQL B1ms connection exhaustion | Low | High | Monitor connections; upgrade to B2ms if >30 tenants |
| Honcho fork doesn't support tenant_id | Medium | Medium | Contribute upstream or maintain a fork |
| Cloudflare wildcard cert issues | Low | Low | CF auto-provisions certs for tunneled domains |
| Budget tracking data loss on restart | Medium | Low | DB-backed persistence in Phase 2 |
