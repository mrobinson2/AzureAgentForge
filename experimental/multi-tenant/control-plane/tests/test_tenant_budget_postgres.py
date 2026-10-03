"""Opt-in PostgreSQL proof for tenant budget locking.

Run this only against a disposable database that has been initialized with
``init_db.sql``:

    AAF_TEST_DATABASE_URL=postgresql://... python3 -m pytest -q \
      experimental/multi-tenant/control-plane/tests/test_tenant_budget_postgres.py

The normal offline suite skips this module when the URL or psycopg2 is absent.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
from threading import Barrier
from uuid import uuid4

import pytest

psycopg2 = pytest.importorskip("psycopg2")
DATABASE_URL = os.environ.get("AAF_TEST_DATABASE_URL")
if not DATABASE_URL:
    pytest.skip("AAF_TEST_DATABASE_URL is not configured", allow_module_level=True)

from tenant_budget import BudgetDecision  # noqa: E402
from tenant_budget_store import PostgresTenantBudget  # noqa: E402


def _bind_tenant(conn, tenant_id: str) -> None:
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.commit()
    cur.close()


def test_concurrent_blocking_charges_consume_remaining_cap_once():
    tenant_id = str(uuid4())
    slug = f"budget-test-{tenant_id[:8]}"
    day = "2099-12-31"
    setup = psycopg2.connect(DATABASE_URL)
    try:
        cur = setup.cursor()
        cur.execute(
            """
            INSERT INTO tenants (id, slug, display_name, mem0_namespace, vector_index_name,
                                 daily_budget_cap)
            VALUES (%s, %s, 'Budget integration test', %s, %s, 1.00)
            """,
            (tenant_id, slug, f"ns_{slug}", f"mem-{slug}"),
        )
        setup.commit()
        cur.close()

        barrier = Barrier(2)

        def charge(request_id: str):
            conn = psycopg2.connect(DATABASE_URL)
            try:
                _bind_tenant(conn, tenant_id)
                barrier.wait(timeout=10)
                return PostgresTenantBudget(conn).charge(
                    tenant_id, Decimal("0.70"), day, request_id=request_id
                )
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(charge, ("concurrent-a", "concurrent-b")))

        decisions = [result.decision for result in results]
        assert decisions.count(BudgetDecision.ALLOW) == 1
        assert decisions.count(BudgetDecision.BLOCK) == 1

        _bind_tenant(setup, tenant_id)
        cur = setup.cursor()
        cur.execute(
            "SELECT spent_usd FROM tenant_budget_daily WHERE tenant_id = %s AND day = %s",
            (tenant_id, day),
        )
        assert cur.fetchone()[0] == Decimal("0.70")
        cur.close()

        store = PostgresTenantBudget(setup)
        held = store.reserve(
            tenant_id,
            "agent-integration",
            "reservation-integration",
            Decimal("0.20"),
            day,
        )
        assert held.status == "pending"
        assert held.spent_after == Decimal("0.90")
        assert [
            item.request_id
            for item in store.reservations(tenant_id, day, status="pending")
        ] == ["reservation-integration"]

        settled = store.settle(
            tenant_id, "reservation-integration", Decimal("0.05")
        )
        assert settled.status == "settled"
        assert settled.spent_after == Decimal("0.75")
    finally:
        cur = setup.cursor()
        cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        setup.commit()
        cur.close()
        setup.close()
