# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md).

"""RLS integration test (aaf-0018) — proves the memory_records row-level-
security policy actually enforces tenant isolation now that app/db.py sets
app.tenant_id per transaction (SET LOCAL-equivalent, via set_config; see
db._set_tenant_scope).

NOT part of this platform's offline test suites (the norm elsewhere in this
repo — see e.g. the model-router / memory-governor "all offline" test
evidence). This one needs a real Postgres with the memory_records.sql schema
applied, reached as a role WITHOUT BYPASSRLS/superuser — RLS is a silent
no-op for those, which is the classic false-negative when testing this kind
of policy. Skipped automatically when DATABASE_URL is unset.

One-time throwaway setup:
    docker run --rm -d --name aaf-rls-test -e POSTGRES_PASSWORD=test \\
      -e POSTGRES_DB=rls_test -p 15433:5432 pgvector/pgvector:pg16

    docker exec -i aaf-rls-test psql -U postgres -d rls_test \\
      -c "CREATE EXTENSION IF NOT EXISTS vector;"

    # The ivfflat index statement in memory_records.sql errors out (pgvector's
    # ivfflat caps at 2000 dimensions; this table's content_vector is 3072-dim
    # — a pre-existing mismatch in the reference schema, unrelated to RLS).
    # Everything after it — the trigger and the RLS ENABLE/FORCE/POLICY
    # statements this test exercises — still applies; psql just needs to keep
    # going past that one error:
    docker exec -i aaf-rls-test psql -U postgres -d rls_test \\
      < ../memory_records.sql

    docker exec -i aaf-rls-test psql -U postgres -d rls_test -v ON_ERROR_STOP=1 -c "
      CREATE ROLE memory_store_app LOGIN PASSWORD 'test' NOSUPERUSER NOBYPASSRLS;
      GRANT ALL ON memory_records TO memory_store_app;"

Run:
    DATABASE_URL=postgresql://memory_store_app:test@localhost:15433/rls_test \\
      pytest experimental/multi-tenant/memory-store/tests/test_rls.py -v

Teardown:
    docker rm -f aaf-rls-test
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="RLS integration test requires a live Postgres — set DATABASE_URL (see module docstring)",
    ),
]


def _vec(seed: float = 0.0) -> list[float]:
    # content_vector is VECTOR(3072) NOT NULL.
    return [seed] * 3072


@pytest.fixture(scope="module", autouse=True)
async def _pool_lifecycle():
    """Open the pool once for the module (db.py builds it with open=False —
    see db.py's comment) and close it when the module's tests are done."""
    from app import db

    await db.pool.open(wait=True, timeout=10)
    yield
    await db.pool.close()


@pytest.fixture
async def two_tenants():
    """Insert one record each for two distinct tenants (via the real
    service.upsert_memory path — the same code the route uses), and clean
    them up afterward. Returns (tenant_a, tenant_b, record_id)."""
    from app import schemas
    from app.service import upsert_memory

    record_id = f"rls-test-{uuid.uuid4()}"
    tenant_a, tenant_b = f"tenant-a-{uuid.uuid4()}", f"tenant-b-{uuid.uuid4()}"

    await upsert_memory(
        schemas.MemoryRecordRequest(
            tenant_id=tenant_a,
            record_id=record_id,
            record_type="note",
            content="tenant A's secret",
            content_vector=_vec(0.1),
        )
    )
    await upsert_memory(
        schemas.MemoryRecordRequest(
            tenant_id=tenant_b,
            record_id=record_id,
            record_type="note",
            content="tenant B's secret",
            content_vector=_vec(0.2),
        )
    )

    yield tenant_a, tenant_b, record_id

    # Cleanup: scope each delete to its own tenant so RLS doesn't block it.
    from app.db import async_execute

    for t in (tenant_a, tenant_b):
        await async_execute(
            "DELETE FROM memory_records WHERE tenant_id = %(t)s AND record_id = %(r)s",
            {"t": t, "r": record_id},
            tenant_id=t,
        )


async def test_upsert_requires_matching_tenant_scope(two_tenants):
    """WITH CHECK proof: upsert_memory scopes the transaction to
    record.tenant_id (aaf-0018). Sanity check that the two fixture inserts —
    which went through this exact path — actually landed, each visible only
    under its own tenant scope."""
    from app.db import async_execute

    tenant_a, tenant_b, record_id = two_tenants

    rows_a = await async_execute(
        "SELECT content FROM memory_records WHERE record_id = %(r)s",
        {"r": record_id},
        tenant_id=tenant_a,
    )
    assert [r["content"] for r in rows_a] == ["tenant A's secret"]

    rows_b = await async_execute(
        "SELECT content FROM memory_records WHERE record_id = %(r)s",
        {"r": record_id},
        tenant_id=tenant_b,
    )
    assert [r["content"] for r in rows_b] == ["tenant B's secret"]


async def test_query_without_tenant_scope_is_denied(two_tenants):
    """The core aaf-0018 proof: a query that never sets app.tenant_id for
    this transaction (the bug — a caller that forgot to scope, or the old
    unscoped `1=1` search) sees ZERO rows, not every tenant's. Fail closed,
    not fail open — even though two matching records definitely exist."""
    from app.db import async_execute

    _tenant_a, _tenant_b, record_id = two_tenants

    rows = await async_execute(
        "SELECT content FROM memory_records WHERE record_id = %(r)s",
        {"r": record_id},
        # tenant_id intentionally omitted — simulates a caller that forgot to
        # scope the transaction.
    )
    assert rows == []


async def test_scoped_query_sees_only_its_own_tenant(two_tenants):
    """A transaction correctly scoped to tenant_id (aaf-0018's SET LOCAL
    equivalent) sees its own row and NEVER the other tenant's, even when
    both share the same record_id and the query has no tenant filter of its
    own — the RLS policy is the thing doing the scoping here, not the SQL."""
    from app.db import async_execute

    tenant_a, tenant_b, record_id = two_tenants

    rows_scoped_a = await async_execute(
        "SELECT tenant_id, content FROM memory_records WHERE record_id = %(r)s",
        {"r": record_id},
        tenant_id=tenant_a,
    )
    assert len(rows_scoped_a) == 1
    assert rows_scoped_a[0]["tenant_id"] == tenant_a
    assert rows_scoped_a[0]["content"] == "tenant A's secret"

    rows_scoped_b = await async_execute(
        "SELECT tenant_id, content FROM memory_records WHERE record_id = %(r)s",
        {"r": record_id},
        tenant_id=tenant_b,
    )
    assert len(rows_scoped_b) == 1
    assert rows_scoped_b[0]["tenant_id"] == tenant_b


async def test_delete_memory_service_is_tenant_scoped(two_tenants):
    """End-to-end through the actual route-level service function
    (service.delete_memory), not just raw db.async_execute — proves the
    wiring in service.py (not just db.py) is correct for the write/delete
    path too, not only upsert (test_upsert_requires_matching_tenant_scope)
    and raw reads (the two tests above).

    Uses a record_id that ONLY tenant A owns (unlike the shared record_id in
    the `two_tenants` fixture, which the other tests use specifically to
    prove RLS scopes rows sharing a record_id across tenants) so a "tenant B
    deletes it" attempt has no legitimate self-scoped interpretation — a
    non-zero result here could only mean a cross-tenant delete happened.

    (service.search_memory's `content_vector <=> %(query_vector)s` cosine
    query has a separate, pre-existing defect — psycopg needs an explicit
    vector cast for a bare python list literal outside an INSERT context —
    unrelated to aaf-0018/RLS tenant scoping, so it is intentionally not
    exercised by this RLS-focused suite.)"""
    from app import schemas
    from app.db import async_execute
    from app.service import delete_memory, upsert_memory

    tenant_a, tenant_b, _shared_record_id = two_tenants
    solo_record_id = f"rls-test-solo-{uuid.uuid4()}"

    await upsert_memory(
        schemas.MemoryRecordRequest(
            tenant_id=tenant_a,
            record_id=solo_record_id,
            record_type="note",
            content="only tenant A owns this",
            content_vector=_vec(0.3),
        )
    )

    # Tenant B has no row under solo_record_id — delete_memory scopes both
    # the WHERE clause AND the RLS transaction to tenant_b, so this is a
    # true no-op, not a cross-tenant delete.
    deleted_by_b = await delete_memory(tenant_b, solo_record_id)
    assert deleted_by_b is False

    # Tenant A's row survived the attempt.
    still_there = await async_execute(
        "SELECT 1 FROM memory_records WHERE tenant_id = %(t)s AND record_id = %(r)s",
        {"t": tenant_a, "r": solo_record_id},
        tenant_id=tenant_a,
    )
    assert len(still_there) == 1

    # Tenant A deletes its own row successfully.
    deleted_by_a = await delete_memory(tenant_a, solo_record_id)
    assert deleted_by_a is True

    gone = await async_execute(
        "SELECT 1 FROM memory_records WHERE tenant_id = %(t)s AND record_id = %(r)s",
        {"t": tenant_a, "r": solo_record_id},
        tenant_id=tenant_a,
    )
    assert gone == []
