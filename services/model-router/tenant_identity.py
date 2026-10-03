"""Verify control-plane HS256 user tokens for tenant-scoped routing.

The router accepts the token in ``x-tenant-token`` so its own
``ROUTER_API_KEY`` can continue to use either supported authentication header.
Tenant and caller identifiers are always derived from verified claims.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from uuid import UUID


ROLES = frozenset({"viewer", "member", "operator", "owner"})


class TenantIdentityError(RuntimeError):
    """A tenant token is malformed, expired, or untrusted."""


@dataclass(frozen=True)
class TenantPrincipal:
    user_id: str
    tenant_id: str
    role: str


def _b64url_decode(segment: str) -> bytes:
    if not segment:
        raise ValueError("empty token segment")
    padded = segment + "=" * (-len(segment) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def verify_tenant_token(
    token: str,
    secret: str,
    *,
    now: float | None = None,
    max_ttl_seconds: int = 3600,
) -> TenantPrincipal:
    """Verify a control-plane user token and return its trusted principal."""
    if not secret:
        raise TenantIdentityError("tenant token verification is not configured")
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise TenantIdentityError("invalid tenant token")
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header = json.loads(_b64url_decode(header_segment))
        payload = json.loads(_b64url_decode(payload_segment))
        provided = _b64url_decode(signature_segment)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TenantIdentityError("invalid tenant token") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise TenantIdentityError("invalid tenant token")
    if not isinstance(payload, dict):
        raise TenantIdentityError("invalid tenant token")

    signing_input = f"{header_segment}.{payload_segment}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        raise TenantIdentityError("invalid tenant token")

    try:
        issued_at = float(payload["iat"])
        expires_at = float(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TenantIdentityError("invalid tenant token") from exc
    clock = time.time() if now is None else float(now)
    if (
        expires_at <= clock
        or issued_at > clock + 30
        or expires_at <= issued_at
        or expires_at - issued_at > max_ttl_seconds
    ):
        raise TenantIdentityError("invalid tenant token")

    user_id = payload.get("sub")
    role = payload.get("role")
    if not isinstance(user_id, str) or not user_id or len(user_id) > 200:
        raise TenantIdentityError("invalid tenant token")
    if role not in ROLES:
        raise TenantIdentityError("invalid tenant token")
    try:
        tenant_id = str(UUID(str(payload["tenant_id"])))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise TenantIdentityError("invalid tenant token") from exc
    return TenantPrincipal(user_id=user_id, tenant_id=tenant_id, role=str(role))
