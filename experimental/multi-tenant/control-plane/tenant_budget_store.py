"""Durable per-tenant budget enforcement for the multi-tenant reference path.

The pure :mod:`tenant_budget` module owns the decision vocabulary. This module
adds the PostgreSQL transaction boundary: a tenant row lock serializes charges
for one tenant, and the daily spend row is updated only after the decision is
known. It is intentionally not wired into the single-tenant deployment yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from tenant_budget import BudgetDecision, BudgetMode
from tenant_context import TenantContextError, bind_tenant_context
from user_tokens import Principal


class BudgetStoreError(RuntimeError):
    """Stable internal error for persistence failures."""


@dataclass(frozen=True)
class BudgetSnapshot:
    tenant_id: str
    day: str
    cap: Decimal
    spent: Decimal
    decision: BudgetDecision

    @property
    def remaining(self) -> Decimal:
        return max(Decimal("0"), self.cap - self.spent)


@dataclass(frozen=True)
class BudgetEvent:
    """One durable budget decision, including blocked attempts."""

    tenant_id: str
    day: str
    request_id: str
    amount: Decimal
    cap: Decimal
    spent_before: Decimal
    spent_after: Decimal
    decision: BudgetDecision


@dataclass(frozen=True)
class BudgetReservation:
    """A pre-dispatch budget hold for one tenant/caller request."""

    tenant_id: str
    day: str
    request_id: str
    caller_id: str
    reserved: Decimal
    actual: Decimal | None
    cap: Decimal
    spent_before: Decimal
    spent_after: Decimal
    decision: BudgetDecision
    status: str
    created_at: str | None = None
    updated_at: str | None = None


def _decimal_amount(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite non-negative number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite non-negative number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return amount


def _validate_day(day: Any) -> str:
    if not isinstance(day, str) or not day.strip():
        raise ValueError("day is required")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("day must be an ISO date (YYYY-MM-DD)") from exc
    return day


class PostgresTenantBudget:
    """Atomic daily budget store using a DB-API-compatible PostgreSQL connection.

    The caller owns the connection lifecycle. Every operation commits or rolls
    back its own transaction, which keeps the lock held for exactly one budget
    decision and prevents a blocked charge from leaking into later work.
    """

    def __init__(
        self,
        conn: Any,
        *,
        mode: BudgetMode = BudgetMode.BLOCK,
        principal: Principal | None = None,
    ) -> None:
        self._conn = conn
        self._mode = mode
        self._principal = principal

    def _bind_tenant(self, tenant_id: str) -> None:
        if self._principal is None:
            return
        if self._principal.tenant_id != tenant_id:
            raise BudgetStoreError("tenant context does not match requested tenant")
        try:
            bind_tenant_context(self._conn, self._principal)
        except TenantContextError as exc:
            raise BudgetStoreError("tenant context could not be established") from exc

    def charge(
        self,
        tenant_id: str,
        amount: Any,
        day: str,
        *,
        request_id: str | None = None,
    ) -> BudgetSnapshot:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        day = _validate_day(day)
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise ValueError("request_id must be a non-empty string")
        charge = _decimal_amount(amount, field="amount")
        request_id = request_id or uuid4().hex
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            # Every charge for a tenant takes this lock before reading spend.
            # That makes the cap check + write one serializable application
            # operation without requiring a database-wide lock.
            cur.execute(
                "SELECT daily_budget_cap FROM tenants WHERE id = %s FOR UPDATE",
                (tenant_id,),
            )
            tenant = cur.fetchone()
            if tenant is None:
                raise BudgetStoreError("tenant budget is not configured")
            cap = _decimal_amount(tenant[0], field="daily_budget_cap")

            # A caller may retry after a network timeout. Replay the original
            # decision instead of charging twice, and reject accidental reuse
            # of a request id for a different charge.
            cur.execute(
                """
                SELECT day, amount_usd, cap_usd, spent_before_usd,
                       spent_after_usd, decision
                  FROM tenant_budget_events
                 WHERE tenant_id = %s AND request_id = %s
                 FOR UPDATE
                """,
                (tenant_id, request_id),
            )
            prior = cur.fetchone()
            if prior is not None:
                prior_day, prior_amount, prior_cap, prior_before, prior_after, raw_decision = prior
                if str(prior_day) != day or _decimal_amount(prior_amount, field="amount") != charge:
                    raise BudgetStoreError("budget request id already used")
                self._conn.commit()
                return BudgetSnapshot(
                    tenant_id,
                    day,
                    _decimal_amount(prior_cap, field="cap_usd"),
                    _decimal_amount(prior_after, field="spent_after_usd"),
                    BudgetDecision(raw_decision),
                )

            cur.execute(
                """
                SELECT spent_usd
                  FROM tenant_budget_daily
                 WHERE tenant_id = %s AND day = %s
                 FOR UPDATE
                """,
                (tenant_id, day),
            )
            row = cur.fetchone()
            spent = _decimal_amount(row[0], field="spent_usd") if row else Decimal("0")
            projected = spent + charge
            if projected <= cap:
                decision = BudgetDecision.ALLOW
            elif self._mode is BudgetMode.OFF:
                decision = BudgetDecision.ALLOW
            elif self._mode is BudgetMode.WARN:
                decision = BudgetDecision.WARN
            else:
                decision = BudgetDecision.BLOCK

            if decision is not BudgetDecision.BLOCK:
                cur.execute(
                    """
                    INSERT INTO tenant_budget_daily (tenant_id, day, spent_usd)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id, day) DO UPDATE
                    SET spent_usd = EXCLUDED.spent_usd, updated_at = now()
                    """,
                    (tenant_id, day, projected),
                )
                spent = projected

            cur.execute(
                """
                INSERT INTO tenant_budget_events (
                    tenant_id, day, request_id, amount_usd, cap_usd,
                    spent_before_usd, spent_after_usd, decision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    day,
                    request_id,
                    charge,
                    cap,
                    spent if decision is BudgetDecision.BLOCK else spent - charge,
                    spent,
                    decision.value,
                ),
            )

            self._conn.commit()
            return BudgetSnapshot(tenant_id, day, cap, spent, decision)
        except BudgetStoreError:
            self._conn.rollback()
            raise
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()

    def reserve(
        self,
        tenant_id: str,
        caller_id: str,
        request_id: str,
        amount: Any,
        day: str,
    ) -> BudgetReservation:
        """Atomically hold estimated request cost before provider dispatch."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(caller_id, str) or not caller_id.strip():
            raise ValueError("caller_id is required")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required")
        day = _validate_day(day)
        reserved = _decimal_amount(amount, field="amount")
        if reserved <= 0:
            raise ValueError("amount must be positive")
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT daily_budget_cap FROM tenants WHERE id = %s FOR UPDATE",
                (tenant_id,),
            )
            tenant = cur.fetchone()
            if tenant is None:
                raise BudgetStoreError("tenant budget is not configured")
            cap = _decimal_amount(tenant[0], field="daily_budget_cap")

            cur.execute(
                """
                SELECT day, caller_id, reserved_usd, actual_usd, cap_usd,
                       spent_before_usd, spent_after_usd, decision, status
                  FROM tenant_budget_reservations
                 WHERE tenant_id = %s AND request_id = %s
                 FOR UPDATE
                """,
                (tenant_id, request_id),
            )
            prior = cur.fetchone()
            if prior is not None:
                (
                    prior_day,
                    prior_caller,
                    prior_reserved,
                    prior_actual,
                    prior_cap,
                    prior_before,
                    prior_after,
                    raw_decision,
                    status,
                ) = prior
                if (
                    str(prior_day) != day
                    or prior_caller != caller_id
                    or _decimal_amount(prior_reserved, field="reserved_usd") != reserved
                ):
                    raise BudgetStoreError("budget reservation id already used")
                self._conn.commit()
                return BudgetReservation(
                    tenant_id=tenant_id,
                    day=day,
                    request_id=request_id,
                    caller_id=caller_id,
                    reserved=reserved,
                    actual=(
                        _decimal_amount(prior_actual, field="actual_usd")
                        if prior_actual is not None
                        else None
                    ),
                    cap=_decimal_amount(prior_cap, field="cap_usd"),
                    spent_before=_decimal_amount(prior_before, field="spent_before_usd"),
                    spent_after=_decimal_amount(prior_after, field="spent_after_usd"),
                    decision=BudgetDecision(raw_decision),
                    status=status,
                )

            cur.execute(
                """
                SELECT spent_usd
                  FROM tenant_budget_daily
                 WHERE tenant_id = %s AND day = %s
                 FOR UPDATE
                """,
                (tenant_id, day),
            )
            row = cur.fetchone()
            spent_before = (
                _decimal_amount(row[0], field="spent_usd") if row else Decimal("0")
            )
            projected = spent_before + reserved
            if projected <= cap:
                decision = BudgetDecision.ALLOW
            elif self._mode is BudgetMode.OFF:
                decision = BudgetDecision.ALLOW
            elif self._mode is BudgetMode.WARN:
                decision = BudgetDecision.WARN
            else:
                decision = BudgetDecision.BLOCK

            status = "blocked" if decision is BudgetDecision.BLOCK else "pending"
            spent_after = spent_before
            if status == "pending":
                cur.execute(
                    """
                    INSERT INTO tenant_budget_daily (tenant_id, day, spent_usd)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id, day) DO UPDATE
                    SET spent_usd = EXCLUDED.spent_usd, updated_at = now()
                    """,
                    (tenant_id, day, projected),
                )
                spent_after = projected

            cur.execute(
                """
                INSERT INTO tenant_budget_reservations (
                    tenant_id, day, request_id, caller_id, reserved_usd,
                    cap_usd, spent_before_usd, spent_after_usd, decision, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    day,
                    request_id,
                    caller_id,
                    reserved,
                    cap,
                    spent_before,
                    spent_after,
                    decision.value,
                    status,
                ),
            )
            self._conn.commit()
            return BudgetReservation(
                tenant_id=tenant_id,
                day=day,
                request_id=request_id,
                caller_id=caller_id,
                reserved=reserved,
                actual=None,
                cap=cap,
                spent_before=spent_before,
                spent_after=spent_after,
                decision=decision,
                status=status,
            )
        except BudgetStoreError:
            self._conn.rollback()
            raise
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()

    def settle(
        self,
        tenant_id: str,
        request_id: str,
        actual_amount: Any,
    ) -> BudgetReservation:
        """Replace a pending hold with the provider's actual request cost."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required")
        actual = _decimal_amount(actual_amount, field="actual_amount")
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT daily_budget_cap FROM tenants WHERE id = %s FOR UPDATE",
                (tenant_id,),
            )
            tenant = cur.fetchone()
            if tenant is None:
                raise BudgetStoreError("tenant budget is not configured")

            cur.execute(
                """
                SELECT day, caller_id, reserved_usd, actual_usd, cap_usd,
                       spent_before_usd, spent_after_usd, decision, status
                  FROM tenant_budget_reservations
                 WHERE tenant_id = %s AND request_id = %s
                 FOR UPDATE
                """,
                (tenant_id, request_id),
            )
            row = cur.fetchone()
            if row is None:
                raise BudgetStoreError("budget reservation not found")
            (
                day,
                caller_id,
                raw_reserved,
                raw_actual,
                raw_cap,
                raw_before,
                raw_after,
                raw_decision,
                status,
            ) = row
            reserved = _decimal_amount(raw_reserved, field="reserved_usd")
            cap = _decimal_amount(raw_cap, field="cap_usd")
            spent_before = _decimal_amount(raw_before, field="spent_before_usd")
            if status == "settled":
                prior_actual = _decimal_amount(raw_actual, field="actual_usd")
                if prior_actual != actual:
                    raise BudgetStoreError("budget reservation already settled")
                self._conn.commit()
                return BudgetReservation(
                    tenant_id, str(day), request_id, caller_id, reserved,
                    prior_actual, cap, spent_before,
                    _decimal_amount(raw_after, field="spent_after_usd"),
                    BudgetDecision(raw_decision), status,
                )
            if status != "pending":
                raise BudgetStoreError("budget reservation is not pending")

            cur.execute(
                """
                SELECT spent_usd
                  FROM tenant_budget_daily
                 WHERE tenant_id = %s AND day = %s
                 FOR UPDATE
                """,
                (tenant_id, day),
            )
            daily = cur.fetchone()
            spent = _decimal_amount(daily[0], field="spent_usd") if daily else Decimal("0")
            if spent < reserved:
                raise BudgetStoreError("budget reservation ledger is inconsistent")
            settled_spend = spent - reserved + actual
            decision = BudgetDecision(raw_decision)
            if settled_spend > cap and decision is BudgetDecision.ALLOW:
                # Provider cost is already real; retain it and surface overage
                # instead of discarding spend that happened after reservation.
                decision = BudgetDecision.WARN

            cur.execute(
                """
                UPDATE tenant_budget_daily
                   SET spent_usd = %s, updated_at = now()
                 WHERE tenant_id = %s AND day = %s
                """,
                (settled_spend, tenant_id, day),
            )
            cur.execute(
                """
                UPDATE tenant_budget_reservations
                   SET actual_usd = %s,
                       spent_after_usd = %s,
                       decision = %s,
                       status = 'settled',
                       updated_at = now()
                 WHERE tenant_id = %s AND request_id = %s
                """,
                (actual, settled_spend, decision.value, tenant_id, request_id),
            )
            self._conn.commit()
            return BudgetReservation(
                tenant_id=tenant_id,
                day=str(day),
                request_id=request_id,
                caller_id=caller_id,
                reserved=reserved,
                actual=actual,
                cap=cap,
                spent_before=spent_before,
                spent_after=settled_spend,
                decision=decision,
                status="settled",
            )
        except BudgetStoreError:
            self._conn.rollback()
            raise
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()

    def release(self, tenant_id: str, request_id: str) -> BudgetReservation:
        """Release a pending hold when no provider request completed."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required")
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT daily_budget_cap FROM tenants WHERE id = %s FOR UPDATE",
                (tenant_id,),
            )
            if cur.fetchone() is None:
                raise BudgetStoreError("tenant budget is not configured")
            cur.execute(
                """
                SELECT day, caller_id, reserved_usd, actual_usd, cap_usd,
                       spent_before_usd, spent_after_usd, decision, status
                  FROM tenant_budget_reservations
                 WHERE tenant_id = %s AND request_id = %s
                 FOR UPDATE
                """,
                (tenant_id, request_id),
            )
            row = cur.fetchone()
            if row is None:
                raise BudgetStoreError("budget reservation not found")
            (
                day,
                caller_id,
                raw_reserved,
                raw_actual,
                raw_cap,
                raw_before,
                raw_after,
                raw_decision,
                status,
            ) = row
            reserved = _decimal_amount(raw_reserved, field="reserved_usd")
            cap = _decimal_amount(raw_cap, field="cap_usd")
            spent_before = _decimal_amount(raw_before, field="spent_before_usd")
            if status == "released":
                self._conn.commit()
                return BudgetReservation(
                    tenant_id, str(day), request_id, caller_id, reserved, None,
                    cap, spent_before,
                    _decimal_amount(raw_after, field="spent_after_usd"),
                    BudgetDecision(raw_decision), status,
                )
            if status != "pending":
                raise BudgetStoreError("budget reservation is not pending")

            cur.execute(
                """
                SELECT spent_usd
                  FROM tenant_budget_daily
                 WHERE tenant_id = %s AND day = %s
                 FOR UPDATE
                """,
                (tenant_id, day),
            )
            daily = cur.fetchone()
            spent = _decimal_amount(daily[0], field="spent_usd") if daily else Decimal("0")
            if spent < reserved:
                raise BudgetStoreError("budget reservation ledger is inconsistent")
            released_spend = spent - reserved
            cur.execute(
                """
                UPDATE tenant_budget_daily
                   SET spent_usd = %s, updated_at = now()
                 WHERE tenant_id = %s AND day = %s
                """,
                (released_spend, tenant_id, day),
            )
            cur.execute(
                """
                UPDATE tenant_budget_reservations
                   SET spent_after_usd = %s,
                       status = 'released',
                       updated_at = now()
                 WHERE tenant_id = %s AND request_id = %s
                """,
                (released_spend, tenant_id, request_id),
            )
            self._conn.commit()
            return BudgetReservation(
                tenant_id=tenant_id,
                day=str(day),
                request_id=request_id,
                caller_id=caller_id,
                reserved=reserved,
                actual=None,
                cap=cap,
                spent_before=spent_before,
                spent_after=released_spend,
                decision=BudgetDecision(raw_decision),
                status="released",
            )
        except BudgetStoreError:
            self._conn.rollback()
            raise
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()

    def reservations(
        self,
        tenant_id: str,
        day: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BudgetReservation]:
        """List reservation decisions for operator reconciliation."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        day = _validate_day(day)
        allowed_statuses = {"pending", "settled", "released", "blocked"}
        if status is not None and status not in allowed_statuses:
            raise ValueError("status must be pending, settled, released, or blocked")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                SELECT tenant_id, day, request_id, caller_id, reserved_usd,
                       actual_usd, cap_usd, spent_before_usd, spent_after_usd,
                       decision, status, created_at, updated_at
                  FROM tenant_budget_reservations
                 WHERE tenant_id = %s AND day = %s
                   AND (%s IS NULL OR status = %s)
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (tenant_id, day, status, status, limit),
            )
            rows = cur.fetchall()
            self._conn.commit()
            return [
                BudgetReservation(
                    tenant_id=row[0],
                    day=str(row[1]),
                    request_id=row[2],
                    caller_id=row[3],
                    reserved=_decimal_amount(row[4], field="reserved_usd"),
                    actual=(
                        _decimal_amount(row[5], field="actual_usd")
                        if row[5] is not None
                        else None
                    ),
                    cap=_decimal_amount(row[6], field="cap_usd"),
                    spent_before=_decimal_amount(row[7], field="spent_before_usd"),
                    spent_after=_decimal_amount(row[8], field="spent_after_usd"),
                    decision=BudgetDecision(row[9]),
                    status=row[10],
                    created_at=str(row[11]),
                    updated_at=str(row[12]),
                )
                for row in rows
            ]
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()

    def events(self, tenant_id: str, day: str, *, limit: int = 100) -> list[BudgetEvent]:
        """Return recent decisions for an operator or tenant audit view."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        day = _validate_day(day)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                SELECT tenant_id, day, request_id, amount_usd, cap_usd,
                       spent_before_usd, spent_after_usd, decision
                  FROM tenant_budget_events
                 WHERE tenant_id = %s AND day = %s
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (tenant_id, day, limit),
            )
            rows = cur.fetchall()
            self._conn.commit()
            return [
                BudgetEvent(
                    tenant_id=row[0],
                    day=str(row[1]),
                    request_id=row[2],
                    amount=_decimal_amount(row[3], field="amount_usd"),
                    cap=_decimal_amount(row[4], field="cap_usd"),
                    spent_before=_decimal_amount(row[5], field="spent_before_usd"),
                    spent_after=_decimal_amount(row[6], field="spent_after_usd"),
                    decision=BudgetDecision(row[7]),
                )
                for row in rows
            ]
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()

    def snapshot(self, tenant_id: str, day: str) -> BudgetSnapshot:
        """Read a tenant's current budget without recording a charge."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        day = _validate_day(day)
        self._bind_tenant(tenant_id)
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT daily_budget_cap FROM tenants WHERE id = %s", (tenant_id,))
            tenant = cur.fetchone()
            if tenant is None:
                raise BudgetStoreError("tenant budget is not configured")
            cap = _decimal_amount(tenant[0], field="daily_budget_cap")
            cur.execute(
                "SELECT spent_usd FROM tenant_budget_daily WHERE tenant_id = %s AND day = %s",
                (tenant_id, day),
            )
            row = cur.fetchone()
            spent = _decimal_amount(row[0], field="spent_usd") if row else Decimal("0")
            self._conn.commit()
            decision = BudgetDecision.ALLOW if spent <= cap else BudgetDecision.BLOCK
            return BudgetSnapshot(tenant_id, day, cap, spent, decision)
        except BudgetStoreError:
            self._conn.rollback()
            raise
        except Exception as exc:
            self._conn.rollback()
            raise BudgetStoreError("tenant budget persistence failed") from exc
        finally:
            cur.close()
