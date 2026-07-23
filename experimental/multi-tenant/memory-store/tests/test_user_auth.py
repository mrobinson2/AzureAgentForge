"""require_principal — offline tests, including control-plane issuer interop.

Sync tests driving the async dependency via asyncio.run (no pytest-asyncio
needed). Proves a token minted by the control-plane issuer verifies to the same
Principal the memory-store derives — the phase-2 identity contract end to end.
"""

import asyncio
import pathlib
import sys

import pytest
from fastapi import HTTPException

# control-plane issuer (sibling service) on the path for the interop test.
_CP = pathlib.Path(__file__).resolve().parents[3] / "control-plane"
sys.path.insert(0, str(_CP))

from app.auth import Principal, require_principal  # noqa: E402
from user_tokens import issue_user_token  # noqa: E402

SECRET = "shared-user-secret"


def _call(authorization):
    return asyncio.run(require_principal(authorization=authorization))


def test_issuer_token_verifies_to_matching_principal(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_JWT_SECRET", SECRET)
    tok = issue_user_token(user_id="u9", tenant_id="t9", role="owner", secret=SECRET)
    p = _call(f"Bearer {tok}")
    assert p == Principal(tenant_id="t9", user_id="u9", role="owner")


def test_missing_secret_fails_closed_503(monkeypatch):
    monkeypatch.delenv("MEMORY_STORE_JWT_SECRET", raising=False)
    # also ensure the /secrets file fallback isn't present in CI
    with pytest.raises(HTTPException) as exc:
        _call("Bearer whatever")
    assert exc.value.status_code == 503


def test_missing_bearer_is_401(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_JWT_SECRET", SECRET)
    with pytest.raises(HTTPException) as exc:
        _call(None)
    assert exc.value.status_code == 401


def test_wrong_secret_is_401(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_JWT_SECRET", SECRET)
    tok = issue_user_token(user_id="u1", tenant_id="t1", role="member", secret="other")
    with pytest.raises(HTTPException) as exc:
        _call(f"Bearer {tok}")
    assert exc.value.status_code == 401


def test_tenant_only_token_missing_sub_role_is_401(monkeypatch):
    # a legacy require_tenant-style token (tenant_id only) must NOT satisfy the
    # stricter principal contract.
    import base64
    import hashlib
    import hmac
    import json

    monkeypatch.setenv("MEMORY_STORE_JWT_SECRET", SECRET)

    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"tenant_id": "t1"}).encode())
    sig = b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    with pytest.raises(HTTPException) as exc:
        _call(f"Bearer {h}.{p}.{sig}")
    assert exc.value.status_code == 401
