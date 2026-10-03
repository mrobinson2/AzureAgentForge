"""Async client for the control-plane tenant budget reservation API."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class TenantBudgetError(RuntimeError):
    """Base class for stable router-side budget failures."""


class TenantBudgetUnavailable(TenantBudgetError):
    """The durable budget authority could not provide a decision."""


class TenantBudgetBlocked(TenantBudgetError):
    """The tenant has no remaining budget for the requested reservation."""


@dataclass(frozen=True)
class ReservationDecision:
    tenant_id: str
    caller_id: str
    request_id: str
    day: str
    reserved_usd: str
    actual_usd: str | None
    decision: str
    status: str


class TenantBudgetClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not api_key:
            raise ValueError("tenant budget base_url and api_key are required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._transport = transport

    async def _request(self, method: str, path: str, *, json: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise TenantBudgetUnavailable("tenant budget authority unavailable") from exc
        if response.status_code == 429:
            raise TenantBudgetBlocked("tenant daily budget exceeded")
        if response.status_code >= 400:
            raise TenantBudgetUnavailable("tenant budget authority rejected request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TenantBudgetUnavailable("tenant budget authority returned invalid data") from exc
        if not isinstance(payload, dict):
            raise TenantBudgetUnavailable("tenant budget authority returned invalid data")
        return payload

    @staticmethod
    def _decision(payload: dict[str, Any]) -> ReservationDecision:
        try:
            decision = ReservationDecision(
                tenant_id=str(payload["tenant_id"]),
                caller_id=str(payload["caller_id"]),
                request_id=str(payload["request_id"]),
                day=str(payload["day"]),
                reserved_usd=str(payload["reserved_usd"]),
                actual_usd=(
                    str(payload["actual_usd"])
                    if payload.get("actual_usd") is not None
                    else None
                ),
                decision=str(payload["decision"]),
                status=str(payload["status"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TenantBudgetUnavailable("tenant budget authority returned invalid data") from exc
        if decision.decision not in {"allow", "warn", "block"}:
            raise TenantBudgetUnavailable("tenant budget authority returned invalid decision")
        if decision.status not in {"pending", "settled", "released", "blocked"}:
            raise TenantBudgetUnavailable("tenant budget authority returned invalid status")
        return decision

    @staticmethod
    def _same_money(left: str, right: str) -> bool:
        try:
            return Decimal(left) == Decimal(right)
        except (InvalidOperation, ValueError):
            return False

    async def reserve(
        self,
        *,
        tenant_id: str,
        caller_id: str,
        request_id: str,
        reserved_usd: str,
        day: str,
    ) -> ReservationDecision:
        payload = await self._request(
            "POST",
            "/internal/budget/reservations",
            json={
                "tenant_id": tenant_id,
                "caller_id": caller_id,
                "request_id": request_id,
                "reserved_usd": reserved_usd,
                "day": day,
            },
        )
        result = self._decision(payload)
        if result.decision == "block" or result.status == "blocked":
            raise TenantBudgetBlocked("tenant daily budget exceeded")
        if (
            result.tenant_id != tenant_id
            or result.caller_id != caller_id
            or result.request_id != request_id
            or result.day != day
            or not self._same_money(result.reserved_usd, reserved_usd)
            or result.actual_usd is not None
            or result.status != "pending"
            or result.decision not in {"allow", "warn"}
        ):
            raise TenantBudgetUnavailable("tenant budget authority returned mismatched data")
        return result

    async def settle(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actual_usd: str,
    ) -> ReservationDecision:
        payload = await self._request(
            "POST",
            f"/internal/budget/reservations/{tenant_id}/{request_id}/settle",
            json={"actual_usd": actual_usd},
        )
        result = self._decision(payload)
        if (
            result.tenant_id != tenant_id
            or result.request_id != request_id
            or result.actual_usd is None
            or not self._same_money(result.actual_usd, actual_usd)
            or result.status != "settled"
            or result.decision not in {"allow", "warn"}
        ):
            raise TenantBudgetUnavailable("tenant budget authority returned mismatched data")
        return result

    async def release(self, *, tenant_id: str, request_id: str) -> ReservationDecision:
        payload = await self._request(
            "DELETE",
            f"/internal/budget/reservations/{tenant_id}/{request_id}",
        )
        result = self._decision(payload)
        if (
            result.tenant_id != tenant_id
            or result.request_id != request_id
            or result.actual_usd is not None
            or result.status != "released"
            or result.decision not in {"allow", "warn"}
        ):
            raise TenantBudgetUnavailable("tenant budget authority returned mismatched data")
        return result
