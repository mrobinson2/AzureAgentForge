"""HTTP contract tests for the internal tenant-budget reservation seam."""

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from budget_api import build_budget_router
from tenant_budget import BudgetDecision
from tenant_budget_store import BudgetReservation


class StubBudgetStore:
    def reserve(self, tenant_id, caller_id, request_id, amount, day):
        return BudgetReservation(
            tenant_id=tenant_id,
            day=day,
            request_id=request_id,
            caller_id=caller_id,
            reserved=Decimal(str(amount)),
            actual=None,
            cap=Decimal("1.00"),
            spent_before=Decimal("0.10"),
            spent_after=Decimal("0.35"),
            decision=BudgetDecision.ALLOW,
            status="pending",
        )

    def settle(self, tenant_id, request_id, actual):
        return BudgetReservation(
            tenant_id=tenant_id,
            day="2026-08-20",
            request_id=request_id,
            caller_id="agent-7",
            reserved=Decimal("0.25"),
            actual=Decimal(str(actual)),
            cap=Decimal("1.00"),
            spent_before=Decimal("0.35"),
            spent_after=Decimal("0.17"),
            decision=BudgetDecision.ALLOW,
            status="settled",
        )

    def release(self, tenant_id, request_id):
        return BudgetReservation(
            tenant_id=tenant_id,
            day="2026-08-20",
            request_id=request_id,
            caller_id="agent-7",
            reserved=Decimal("0.25"),
            actual=None,
            cap=Decimal("1.00"),
            spent_before=Decimal("0.35"),
            spent_after=Decimal("0.10"),
            decision=BudgetDecision.ALLOW,
            status="released",
        )

    def reservations(self, tenant_id, day, *, status=None, limit=100):
        assert status == "pending"
        assert limit == 25
        reservation = BudgetReservation(
            tenant_id=tenant_id,
            day=day,
            request_id="pending-router-request",
            caller_id="agent-7",
            reserved=Decimal("0.25"),
            actual=None,
            cap=Decimal("1.00"),
            spent_before=Decimal("0.10"),
            spent_after=Decimal("0.35"),
            decision=BudgetDecision.ALLOW,
            status="pending",
        )
        object.__setattr__(reservation, "created_at", "2026-08-20 10:00:00+00:00")
        object.__setattr__(reservation, "updated_at", "2026-08-20 10:05:00+00:00")
        return [reservation]


class BlockingBudgetStore(StubBudgetStore):
    def reserve(self, tenant_id, caller_id, request_id, amount, day):
        reservation = super().reserve(tenant_id, caller_id, request_id, amount, day)
        return BudgetReservation(
            **{
                **reservation.__dict__,
                "cap": Decimal("0.20"),
                "decision": BudgetDecision.BLOCK,
                "status": "blocked",
            }
        )


def _client(store_factory=StubBudgetStore):
    app = FastAPI()
    app.include_router(
        build_budget_router(
            get_db=lambda: object(),
            require_operator=lambda: None,
            store_factory=lambda _conn: store_factory(),
        )
    )
    return TestClient(app)


def test_reservation_endpoint_returns_auditable_pending_hold():
    client = _client()

    response = client.post(
        "/internal/budget/reservations",
        json={
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "caller_id": "agent-7",
            "request_id": "router-request-1",
            "reserved_usd": "0.25",
            "day": "2026-08-20",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "caller_id": "agent-7",
        "request_id": "router-request-1",
        "day": "2026-08-20",
        "reserved_usd": "0.25",
        "actual_usd": None,
        "cap_usd": "1.00",
        "spent_before_usd": "0.10",
        "spent_after_usd": "0.35",
        "decision": "allow",
        "status": "pending",
    }


def test_reservation_endpoint_maps_block_to_machine_readable_429():
    response = _client(BlockingBudgetStore).post(
        "/internal/budget/reservations",
        json={
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "caller_id": "agent-7",
            "request_id": "router-request-1",
            "reserved_usd": "0.25",
            "day": "2026-08-20",
        },
    )

    assert response.status_code == 429
    assert response.json()["detail"] == {
        "code": "tenant_budget_exceeded",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "request_id": "router-request-1",
        "cap_usd": "0.20",
        "spent_usd": "0.35",
    }


def test_settlement_endpoint_reconciles_hold_to_actual_cost():
    response = _client().post(
        "/internal/budget/reservations/00000000-0000-0000-0000-000000000001/router-request-1/settle",
        json={"actual_usd": "0.07"},
    )

    assert response.status_code == 200
    assert response.json()["actual_usd"] == "0.07"
    assert response.json()["spent_after_usd"] == "0.17"
    assert response.json()["status"] == "settled"


def test_release_endpoint_removes_pending_hold():
    response = _client().delete(
        "/internal/budget/reservations/00000000-0000-0000-0000-000000000001/router-request-1"
    )

    assert response.status_code == 200
    assert response.json()["actual_usd"] is None
    assert response.json()["spent_after_usd"] == "0.10"
    assert response.json()["status"] == "released"


def test_reservation_list_exposes_pending_holds_for_reconciliation():
    response = _client().get(
        "/internal/budget/reservations",
        params={
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "day": "2026-08-20",
            "status": "pending",
            "limit": 25,
        },
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "caller_id": "agent-7",
            "request_id": "pending-router-request",
            "day": "2026-08-20",
            "reserved_usd": "0.25",
            "actual_usd": None,
            "cap_usd": "1.00",
            "spent_before_usd": "0.10",
            "spent_after_usd": "0.35",
            "decision": "allow",
            "status": "pending",
            "created_at": "2026-08-20 10:00:00+00:00",
            "updated_at": "2026-08-20 10:05:00+00:00",
        }
    ]
