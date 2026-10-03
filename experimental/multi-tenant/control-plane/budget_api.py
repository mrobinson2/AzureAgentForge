"""Internal HTTP seam for model-router tenant budget reservations."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from tenant_budget import BudgetDecision
from tenant_budget_store import BudgetReservation, BudgetStoreError, PostgresTenantBudget


class ReservationCreate(BaseModel):
    tenant_id: UUID
    caller_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    reserved_usd: Decimal = Field(gt=0)
    day: date


class ReservationSettlement(BaseModel):
    actual_usd: Decimal = Field(ge=0)


class ReservationResponse(BaseModel):
    tenant_id: str
    caller_id: str
    request_id: str
    day: str
    reserved_usd: str
    actual_usd: str | None
    cap_usd: str
    spent_before_usd: str
    spent_after_usd: str
    decision: str
    status: str


class ReservationAuditResponse(ReservationResponse):
    created_at: str
    updated_at: str


def _money(value: Decimal) -> str:
    return format(value, "f")


def _response(value: BudgetReservation) -> ReservationResponse:
    return ReservationResponse(
        tenant_id=value.tenant_id,
        caller_id=value.caller_id,
        request_id=value.request_id,
        day=value.day,
        reserved_usd=_money(value.reserved),
        actual_usd=_money(value.actual) if value.actual is not None else None,
        cap_usd=_money(value.cap),
        spent_before_usd=_money(value.spent_before),
        spent_after_usd=_money(value.spent_after),
        decision=value.decision.value,
        status=value.status,
    )


def _audit_response(value: BudgetReservation) -> ReservationAuditResponse:
    if value.created_at is None or value.updated_at is None:
        raise BudgetStoreError("reservation audit timestamps are unavailable")
    base = _response(value)
    payload = base.model_dump() if hasattr(base, "model_dump") else base.dict()
    return ReservationAuditResponse(
        **payload,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def build_budget_router(
    *,
    get_db: Callable[..., Any],
    require_operator: Callable[..., Any],
    store_factory: Callable[[Any], PostgresTenantBudget] = PostgresTenantBudget,
) -> APIRouter:
    """Build the authenticated internal router with injectable test seams."""
    router = APIRouter(prefix="/internal/budget", tags=["tenant-budget"])

    @router.get("/reservations", response_model=list[ReservationAuditResponse])
    def list_reservations(
        tenant_id: UUID,
        day: date,
        status: Literal["pending", "settled", "released", "blocked"] | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        conn=Depends(get_db),
        _operator=Depends(require_operator),
    ):
        try:
            reservations = store_factory(conn).reservations(
                str(tenant_id),
                day.isoformat(),
                status=status,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BudgetStoreError as exc:
            raise HTTPException(status_code=503, detail="tenant budget unavailable") from exc
        return [_audit_response(reservation) for reservation in reservations]

    @router.post("/reservations", response_model=ReservationResponse)
    def create_reservation(
        request: ReservationCreate,
        conn=Depends(get_db),
        _operator=Depends(require_operator),
    ):
        try:
            reservation = store_factory(conn).reserve(
                str(request.tenant_id),
                request.caller_id,
                request.request_id,
                request.reserved_usd,
                request.day.isoformat(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BudgetStoreError as exc:
            raise HTTPException(status_code=503, detail="tenant budget unavailable") from exc
        if reservation.decision is BudgetDecision.BLOCK:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "tenant_budget_exceeded",
                    "tenant_id": reservation.tenant_id,
                    "request_id": reservation.request_id,
                    "cap_usd": _money(reservation.cap),
                    "spent_usd": _money(reservation.spent_after),
                },
            )
        return _response(reservation)

    @router.post(
        "/reservations/{tenant_id}/{request_id}/settle",
        response_model=ReservationResponse,
    )
    def settle_reservation(
        tenant_id: UUID,
        request_id: str,
        request: ReservationSettlement,
        conn=Depends(get_db),
        _operator=Depends(require_operator),
    ):
        try:
            return _response(
                store_factory(conn).settle(
                    str(tenant_id), request_id, request.actual_usd
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BudgetStoreError as exc:
            raise HTTPException(status_code=503, detail="tenant budget unavailable") from exc

    @router.delete(
        "/reservations/{tenant_id}/{request_id}",
        response_model=ReservationResponse,
    )
    def release_reservation(
        tenant_id: UUID,
        request_id: str,
        conn=Depends(get_db),
        _operator=Depends(require_operator),
    ):
        try:
            return _response(store_factory(conn).release(str(tenant_id), request_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BudgetStoreError as exc:
            raise HTTPException(status_code=503, detail="tenant budget unavailable") from exc

    return router
