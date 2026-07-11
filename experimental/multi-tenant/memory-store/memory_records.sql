-- Reference design — NOT deployed. Part of the multi-tenant roadmap
-- (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
-- provided to illustrate the intended design.

CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS memory_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_vector VECTOR(3072) NOT NULL,
    tags TEXT[] NULL,
    status TEXT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, record_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_records_tenant_id
    ON memory_records (tenant_id);

CREATE INDEX IF NOT EXISTS idx_memory_records_record_type
    ON memory_records (record_type);

CREATE INDEX IF NOT EXISTS idx_memory_records_tags
    ON memory_records USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_memory_records_status
    ON memory_records (status);

CREATE INDEX IF NOT EXISTS idx_memory_records_vector
    ON memory_records USING ivfflat (content_vector vector_cosine_ops)
    WITH (lists = 100);

CREATE TRIGGER trg_memory_records_updated
    BEFORE UPDATE ON memory_records
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

-- ─── Row-Level Security backstop (aaf-0018) ─────────────────────────────────
-- Defense-in-depth beneath the memory-store's token-derived tenant scoping
-- (aaf-0002). Even if a query is ever built without a tenant predicate (the old
-- `1=1` search bug), the database refuses to return another tenant's rows. The
-- service sets the active tenant per request/transaction:
--     SET LOCAL app.tenant_id = '<tenant_id>';
-- `current_setting('app.tenant_id', true)` returns NULL when unset, so an
-- un-scoped connection sees NO rows (fail closed) rather than every tenant's.
-- tenant_id is TEXT here, so the GUC compares as text (no ::uuid cast).
ALTER TABLE memory_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_records FORCE ROW LEVEL SECURITY;
CREATE POLICY memory_records_tenant_isolation ON memory_records
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
