-- Reference design — NOT deployed. Part of the multi-tenant roadmap
-- (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
-- provided to illustrate the intended design.

-- Initialize AzureAgentForge Platform database schema
-- Run this after PostgreSQL is deployed

-- Create database (run as admin)
-- CREATE DATABASE aaf_core;

-- Connect to aaf_core and run:

-- Tenants: each end-user / SMB customer
CREATE TABLE tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    mem0_namespace text NOT NULL,
    vector_index_name text NOT NULL,
    use_orchestrator boolean NOT NULL DEFAULT true,
    agent_vault_path text,
    default_channel text NOT NULL DEFAULT 'web',
    default_locale text NOT NULL DEFAULT 'en-US',
    plan_name text NOT NULL DEFAULT 'personal',
    monthly_memory_limit int,
    monthly_token_limit bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tenants_slug ON tenants(slug);
CREATE INDEX idx_tenants_status ON tenants(status);

-- Users: accounts under a tenant
CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email text NOT NULL,
    display_name text NOT NULL,
    auth_provider text NOT NULL DEFAULT 'entra',
    auth_subject_id text NOT NULL,
    role text NOT NULL DEFAULT 'member',
    is_active boolean NOT NULL DEFAULT true,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_users_auth ON users(auth_provider, auth_subject_id);

-- Channels: Telegram, web widgets, email endpoints, etc.
CREATE TABLE channels (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    type text NOT NULL,
    name text NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_channels_tenant ON channels(tenant_id);
CREATE INDEX idx_channels_type ON channels(type);

-- API keys for internal tools
CREATE TABLE tenant_api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name text NOT NULL,
    hashed_key text NOT NULL,
    scopes text[] NOT NULL DEFAULT ARRAY['all'],
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);

CREATE INDEX idx_api_keys_tenant ON tenant_api_keys(tenant_id);

-- Feature flags per tenant
CREATE TABLE tenant_features (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key text NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY (tenant_id, key)
);

-- Auto-update timestamp trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_tenants_updated_at BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_channels_updated_at BEFORE UPDATE ON channels
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ─── Row-Level Security backstop (aaf-0018) ─────────────────────────────────
-- Defense-in-depth beneath the application's tenant-scoping. Even if a query is
-- ever built without a tenant predicate, the database refuses to return another
-- tenant's rows. The application sets the active tenant per request/transaction:
--     SET LOCAL app.tenant_id = '<tenant-uuid>';
-- `current_setting('app.tenant_id', true)` returns NULL when unset, so an
-- un-scoped connection sees NO tenant rows (fail closed) rather than all of them.
--
-- The control-plane operator (which must provision and enumerate ALL tenants)
-- should connect as a role created with BYPASSRLS, or run under a role listed in
-- the tenants policy below; RLS here guards the per-tenant data-plane paths.

-- tenants: a tenant may see only its own row (keyed on its own id).
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_self_isolation ON tenants
    USING (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- Child tables: scoped by tenant_id.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_tenant_isolation ON users
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE channels FORCE ROW LEVEL SECURITY;
CREATE POLICY channels_tenant_isolation ON channels
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE tenant_api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_api_keys_tenant_isolation ON tenant_api_keys
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE tenant_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_features FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_features_tenant_isolation ON tenant_features
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

-- ─── Control-plane operator role (BYPASSRLS) ────────────────────────────────
-- control-plane/main.py (PG_CONNSTR) MUST connect as a role with BYPASSRLS.
-- Every control-plane route is cross-tenant by nature (create_tenant INSERTs a
-- brand-new tenant row before any app.tenant_id could exist to satisfy the
-- tenants_self_isolation WITH CHECK; list_tenants enumerates ALL tenants) —
-- there is no single verified tenant_id to SET LOCAL for an operator request,
-- so (unlike the per-request `SET LOCAL app.tenant_id` the memory-store uses,
-- see memory-store/app/db.py) BYPASSRLS is the only coherent fit here, not a
-- workaround. Without it every control-plane route breaks under RLS: inserts
-- raise "new row violates row-level security policy" and reads return zero
-- rows.
--
-- Uncomment and set a real, generated password before running (or use your
-- provisioning tool's role-creation step instead), then point PG_CONNSTR at
-- this role rather than the table-owning/migration role:
--   CREATE ROLE control_plane_operator WITH LOGIN PASSWORD '<CHANGE_ME>' BYPASSRLS;
--   GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO control_plane_operator;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO control_plane_operator;
