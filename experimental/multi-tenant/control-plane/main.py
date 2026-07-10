# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional, List
import hmac
import psycopg2
import os
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex, SimpleField, SearchableField, VectorSearch,
    VectorSearchProfile, HnswAlgorithmConfiguration
)
from azure.core.exceptions import ResourceNotFoundError

app = FastAPI(title="AzureAgentForge Platform API", version="1.0.0")


# ─── Operator authentication (aaf-0001 remediation) ──────────────────────────
# The tenant control plane (create/list/read tenants) is operator-only. It
# previously had NO authentication — any caller could provision tenants and
# enumerate every tenant's namespaces / vault paths. Gate all routes behind a
# configured operator key and FAIL CLOSED when the key is unset: a missing key
# means "refuse to serve", never "serve unauthenticated".
def require_operator(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("CONTROL_PLANE_OPERATOR_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="control-plane authentication not configured (CONTROL_PLANE_OPERATOR_KEY unset)",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing operator bearer token")
    if not hmac.compare_digest(authorization[7:].strip(), expected):
        raise HTTPException(status_code=403, detail="invalid operator key")


# Database connection
# aaf-0018: PG_CONNSTR MUST authenticate as a role granted BYPASSRLS (see the
# control_plane_operator block at the bottom of init_db.sql). Every route here
# is a cross-tenant operator operation — create_tenant INSERTs a brand-new
# tenant row (no app.tenant_id could exist yet to satisfy the RLS WITH CHECK),
# and list_tenants enumerates every tenant — so there is no single verified
# tenant_id to SET LOCAL the way memory-store/app/db.py does for its
# per-tenant requests. Connecting as a non-BYPASSRLS role breaks every route:
# inserts raise "new row violates row-level security policy" and reads return
# zero rows under the tenants/users/channels/tenant_api_keys/tenant_features
# RLS policies.
def get_db():
    conn = psycopg2.connect(os.environ["PG_CONNSTR"])
    try:
        yield conn
    finally:
        conn.close()

# Key Vault client
credential = DefaultAzureCredential()
kv_client = SecretClient(vault_url=os.environ["KV_URI"], credential=credential)

# Azure Search client
search_endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
search_credential = DefaultAzureCredential()
search_client = SearchIndexClient(endpoint=search_endpoint, credential=search_credential)

# Pydantic models
class TenantCreate(BaseModel):
    slug: str
    display_name: str
    primary_email: str
    use_orchestrator: bool = True
    plan_name: str = "personal"

class TenantResponse(BaseModel):
    id: str
    slug: str
    display_name: str
    mem0_namespace: str
    vector_index_name: str
    agent_vault_path: str
    status: str

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.post("/tenants", response_model=TenantResponse)
def create_tenant(tenant: TenantCreate, conn = Depends(get_db), _op: None = Depends(require_operator)):
    """Create a new tenant with all associated resources."""

    # Derived values
    mem0_namespace = f"ns_{tenant.slug}"
    vector_index_name = f"mem-{tenant.slug}"
    agent_vault_path = f"vaults/{tenant.slug}/"

    try:
        cur = conn.cursor()

        # 1. Create tenant in database
        cur.execute("""
            INSERT INTO tenants (
                slug, display_name, mem0_namespace, vector_index_name,
                use_orchestrator, agent_vault_path, plan_name, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
            RETURNING id
        """, (
            tenant.slug, tenant.display_name, mem0_namespace,
            vector_index_name, tenant.use_orchestrator, agent_vault_path,
            tenant.plan_name
        ))
        tenant_id = cur.fetchone()[0]

        # 2. Create initial user (owner)
        cur.execute("""
            INSERT INTO users (
                tenant_id, email, display_name, auth_provider, auth_subject_id, role
            ) VALUES (%s, %s, %s, 'entra', %s, 'owner')
        """, (tenant_id, tenant.primary_email, tenant.display_name, f"pending-{tenant.slug}"))

        # 3. Create default web channel
        cur.execute("""
            INSERT INTO channels (
                tenant_id, type, name, is_primary, config_json
            ) VALUES (%s, 'web', 'default-web', true, '{}'::jsonb)
        """, (tenant_id,))

        conn.commit()
        cur.close()

        # 4. Create Azure AI Search index
        create_search_index(vector_index_name)

        return TenantResponse(
            id=str(tenant_id),
            slug=tenant.slug,
            display_name=tenant.display_name,
            mem0_namespace=mem0_namespace,
            vector_index_name=vector_index_name,
            agent_vault_path=agent_vault_path,
            status="active"
        )

    except psycopg2.IntegrityError as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"Tenant with slug '{tenant.slug}' already exists")
    except Exception as e:
        conn.rollback()
        # aaf-0016/aaf-0001: do not leak raw DB/Azure exception text to the caller.
        print(f"[control-plane] tenant creation failed: {e}")
        raise HTTPException(status_code=500, detail="tenant creation failed")

def create_search_index(index_name: str):
    """Create Azure AI Search index for a tenant."""
    try:
        # Check if index already exists
        try:
            search_client.get_index(index_name)
            return  # Index already exists
        except ResourceNotFoundError:
            pass

        # Create new index
        index = SearchIndex(
            name=index_name,
            fields=[
                SimpleField(name="id", type="Edm.String", key=True),
                SimpleField(name="tenant_id", type="Edm.String", filterable=True),
                SearchableField(name="content", type="Edm.String"),
                SimpleField(
                    name="embedding",
                    type="Collection(Edm.Single)",
                    searchable=True,
                    vector_search_dimensions=1536,
                    vector_search_profile_name="default"
                ),
                SimpleField(name="user_id", type="Edm.String", filterable=True),
                SimpleField(name="timestamp", type="Edm.DateTimeOffset", sortable=True),
                SimpleField(name="memory_type", type="Edm.String", filterable=True),
            ],
            vector_search=VectorSearch(
                profiles=[VectorSearchProfile(name="default", algorithm_configuration_name="default")],
                algorithms=[HnswAlgorithmConfiguration(name="default")]
            )
        )
        search_client.create_index(index)
    except Exception as e:
        print(f"Warning: Could not create search index: {e}")

@app.get("/tenants/{slug}", response_model=TenantResponse)
def get_tenant(slug: str, conn = Depends(get_db), _op: None = Depends(require_operator)):
    """Get tenant configuration by slug."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id, slug, display_name, mem0_namespace, vector_index_name,
               agent_vault_path, status
        FROM tenants WHERE slug = %s
    """, (slug,))
    row = cur.fetchone()
    cur.close()

    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return TenantResponse(
        id=str(row[0]),
        slug=row[1],
        display_name=row[2],
        mem0_namespace=row[3],
        vector_index_name=row[4],
        agent_vault_path=row[5],
        status=row[6]
    )

@app.get("/tenants", response_model=List[TenantResponse])
def list_tenants(conn = Depends(get_db), _op: None = Depends(require_operator)):
    """List all tenants."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id, slug, display_name, mem0_namespace, vector_index_name,
               agent_vault_path, status
        FROM tenants WHERE status = 'active'
    """)
    rows = cur.fetchall()
    cur.close()

    return [
        TenantResponse(
            id=str(row[0]),
            slug=row[1],
            display_name=row[2],
            mem0_namespace=row[3],
            vector_index_name=row[4],
            agent_vault_path=row[5],
            status=row[6]
        )
        for row in rows
    ]

@app.get("/tenants/{slug}/config")
def get_tenant_config(slug: str, conn = Depends(get_db), _op: None = Depends(require_operator)):
    """Get full tenant configuration for service initialization."""
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, t.slug, t.display_name, t.mem0_namespace, t.vector_index_name,
               t.agent_vault_path, t.use_orchestrator, t.plan_name,
               array_agg(DISTINCT c.type) as channels
        FROM tenants t
        LEFT JOIN channels c ON c.tenant_id = t.id
        WHERE t.slug = %s
        GROUP BY t.id
    """, (slug,))
    row = cur.fetchone()
    cur.close()

    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return {
        "tenant_id": str(row[0]),
        "slug": row[1],
        "display_name": row[2],
        "mem0_namespace": row[3],
        "vector_index_name": row[4],
        "agent_vault_path": row[5],
        "use_orchestrator": row[6],
        "plan_name": row[7],
        "channels": row[8] if row[8] else []
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
