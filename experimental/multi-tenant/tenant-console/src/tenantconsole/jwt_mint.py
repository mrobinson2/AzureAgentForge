"""Mint the admin HS256 JWT the executor uses against the PaperClip API.

Stdlib only (``hmac`` + ``hashlib`` + ``base64`` + ``json``) so the CLI has no
crypto dependency — the same three-layer HS256 scheme PaperClip already uses
(browser session cookies, agent JWTs, and scoped automation JWTs). The console
mints an *admin*-role token; admin bypasses scopes, so no scope claim is emitted.

Claims are EXACTLY: ``sub``, ``role``, ``iss``, ``aud``, ``iat``, ``exp``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

__all__ = ["ISSUER", "AUDIENCE", "mint_jwt", "decode_jwt"]

ISSUER = "aaf-control-plane"
AUDIENCE = "paperclip-api"


def _b64url(raw: bytes) -> str:
    """URL-safe base64 without ``=`` padding (JWT segment encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    """Inverse of :func:`_b64url` — re-pad then decode."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _encode_segment(obj: Dict[str, Any]) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def mint_jwt(
    secret: str,
    sub: str = "tenant-console",
    role: str = "admin",
    ttl_seconds: int = 3600,
    now: Optional[int] = None,
) -> str:
    """Return a signed HS256 JWT with the fixed AzureAgentForge claim set.

    *now* overrides the ``iat`` epoch (tests pin it for determinism); ``exp`` is
    ``iat + ttl_seconds``.
    """
    iat = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": sub,
        "role": role,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": iat,
        "exp": iat + int(ttl_seconds),
    }
    signing_input = f"{_encode_segment(header)}.{_encode_segment(claims)}"
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_jwt(token: str, secret: Optional[str] = None, verify: bool = True) -> Dict[str, Any]:
    """Decode a JWT's claims; when *secret* is given and *verify*, check the HMAC.

    Raises ``ValueError`` on a malformed token or (when verifying) a bad
    signature. This is the executor/test-side counterpart to :func:`mint_jwt`;
    the real PaperClip auth-proxy is the production verifier.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed JWT: expected three dot-separated segments")
    header_seg, claims_seg, sig_seg = parts
    if verify and secret is not None:
        signing_input = f"{header_seg}.{claims_seg}"
        expected = hmac.new(
            secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_seg)):
            raise ValueError("JWT signature verification failed")
    try:
        return json.loads(_b64url_decode(claims_seg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JWT claims segment: {exc}") from exc
