"""Fail-closed request identity and PostgreSQL RLS binding helpers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from user_tokens import ROLES, Principal, TokenError, verify_user_token


class TenantContextError(RuntimeError):
    """Stable error for missing, invalid, or unbindable tenant context."""


def principal_from_authorization(
    authorization: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> Principal:
    """Verify a bearer token without trusting client-supplied tenant headers."""
    if not isinstance(authorization, str) or not authorization.lower().startswith("bearer "):
        raise TenantContextError("missing tenant bearer token")
    token = authorization[7:].strip()
    if not token:
        raise TenantContextError("missing tenant bearer token")
    try:
        principal = verify_user_token(token, secret, now=now)
        UUID(principal.tenant_id)
    except (TokenError, ValueError, TypeError) as exc:
        raise TenantContextError("invalid tenant context") from exc
    return principal


def bind_tenant_context(conn: Any, principal: Principal) -> None:
    """Set RLS context for the current transaction; do not commit here.

    ``set_config(..., true)`` is PostgreSQL's parameterizable equivalent of
    ``SET LOCAL``. The caller must run all tenant-scoped queries before its
    transaction commits or rolls back.
    """
    if (
        not isinstance(principal, Principal)
        or not principal.user_id
        or principal.role not in ROLES
    ):
        raise TenantContextError("tenant context is required")
    try:
        tenant_id = str(UUID(principal.tenant_id))
    except (ValueError, TypeError) as exc:
        raise TenantContextError("invalid tenant context") from exc

    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (tenant_id,),
        )
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise TenantContextError("could not bind tenant context") from exc
    finally:
        cur.close()
