# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.

"""Tenant authentication for the memory-store (aaf-0002 remediation).

The memory-store previously took ``tenant_id`` straight from the request path
or body with no authentication, so any caller could read/overwrite/delete any
tenant's records (cross-tenant IDOR), and an omitted ``tenant_id`` on search
degraded to an all-tenant (``1=1``) query. Tenant scope must instead be derived
from a verified caller identity — never from client-supplied path/body.

This module provides a ``require_tenant`` FastAPI dependency that verifies an
HS256 bearer token (stdlib only — no external JWT dependency) and returns the
``tenant_id`` claim. It FAILS CLOSED: if the signing secret is unconfigured the
service refuses to serve (503) rather than running unauthenticated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Header, HTTPException


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _signing_secret() -> str | None:
    val = os.environ.get("MEMORY_STORE_JWT_SECRET")
    if val:
        return val
    # Secrets-as-files convention (mirrors the rest of the platform).
    from pathlib import Path

    p = Path("/secrets") / "memory-store-jwt-secret"
    if p.exists():
        return p.read_text().strip()
    return None


def _verify_hs256(token: str, secret: str) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:  # not a 3-part JWT
        raise HTTPException(status_code=401, detail="malformed token") from exc

    header = json.loads(_b64url_decode(header_b64))
    if header.get("alg") != "HS256":
        # Reject alg confusion / alg=none outright.
        raise HTTPException(status_code=401, detail="unsupported token algorithm")

    expected = hmac.new(
        secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    provided = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid token signature")

    claims = json.loads(_b64url_decode(payload_b64))
    exp = claims.get("exp")
    if exp is not None and time.time() >= float(exp):
        raise HTTPException(status_code=401, detail="token expired")
    return claims


async def require_tenant(authorization: str | None = Header(default=None)) -> str:
    """Verify the bearer token and return its ``tenant_id`` claim.

    Fail closed: no configured secret -> 503; missing/invalid token -> 401.
    """
    secret = _signing_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="memory-store authentication not configured (MEMORY_STORE_JWT_SECRET unset)",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    claims = _verify_hs256(authorization[7:].strip(), secret)
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="token missing tenant_id claim")
    return str(tenant_id)
