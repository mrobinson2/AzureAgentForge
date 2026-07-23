# Reference design — part of the multi-tenant roadmap. Phase 2 of
# docs/notes/plans/2026-07-22-full-multi-tenant.md: per-user identity within a
# tenant. The control-plane is the identity authority — it MINTS the per-user
# access tokens the data-plane services (memory-store, ...) verify.

"""Per-user access tokens (HS256, stdlib only — no external JWT dependency).

A token carries the user's identity AND their tenant AND their role, so a
downstream service derives all three from one verified credential and never
trusts a client-supplied tenant/user/role. This is the issuing + verifying
half; the memory-store's require_principal is the consuming half.

Design notes:
  - `sub` is the user_id, `tenant_id` scopes it, `role` drives RBAC downstream.
  - Fail-closed everywhere: alg-confusion (alg != HS256, alg=none) is rejected,
    a bad signature is rejected, an expired token is rejected. A token missing
    sub/tenant_id/role is rejected — a principal is only ever fully-formed.
  - No secret is embedded; the caller passes it (from Key Vault / env), so this
    module has no I/O and is trivially unit-testable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

# Roles, least- to most-privileged. RBAC enforcement (phase 3) maps these to
# allowed scopes; the token just carries the assertion.
ROLES = ("viewer", "member", "operator", "owner")


class TokenError(Exception):
    """Raised on any verification failure (fail-closed)."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    role: str


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def issue_user_token(
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    secret: str,
    ttl_seconds: int = 3600,
    now: float | None = None,
) -> str:
    """Mint an HS256 token for (user_id, tenant_id, role). Raises ValueError on
    a bad role or empty id — a token is never issued for an invalid principal."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    if not user_id or not tenant_id:
        raise ValueError("user_id and tenant_id are required")
    if not secret:
        raise ValueError("secret is required to issue a token")
    issued = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def verify_user_token(token: str, secret: str, *, now: float | None = None) -> Principal:
    """Verify an HS256 token and return the Principal. Raises TokenError on any
    failure (malformed, alg confusion, bad signature, expired, missing claim)."""
    if not secret:
        raise TokenError("no signing secret configured")
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed token header") from exc
    if header.get("alg") != "HS256":
        raise TokenError("unsupported token algorithm")  # rejects alg=none / confusion

    expected = hmac.new(
        secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise TokenError("invalid token signature")

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed token payload") from exc

    exp = claims.get("exp")
    clock = now if now is not None else time.time()
    if exp is not None and clock >= float(exp):
        raise TokenError("token expired")

    user_id = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    role = claims.get("role")
    if not user_id or not tenant_id or not role:
        raise TokenError("token missing sub/tenant_id/role")
    if role not in ROLES:
        raise TokenError(f"unknown role in token: {role!r}")
    return Principal(user_id=str(user_id), tenant_id=str(tenant_id), role=role)
