"""Tests for tenant identity derived from control-plane user tokens."""

import base64
import hashlib
import hmac
import json

import pytest

from tenant_identity import TenantIdentityError, verify_tenant_token


def _b64(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def issue_token(
    *,
    secret: str = "tenant-secret",
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
    user_id: str = "agent-7",
    role: str = "member",
    issued_at: int = 1_800_000_000,
    expires_at: int = 1_800_000_300,
) -> str:
    header = _b64({"alg": "HS256", "typ": "JWT"})
    payload = _b64(
        {
            "sub": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "iat": issued_at,
            "exp": expires_at,
        }
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{signing_input}.{encoded_signature}"


def test_verify_tenant_token_derives_canonical_principal():
    principal = verify_tenant_token(
        issue_token(), "tenant-secret", now=1_800_000_100
    )

    assert principal.tenant_id == "00000000-0000-0000-0000-000000000001"
    assert principal.user_id == "agent-7"
    assert principal.role == "member"


@pytest.mark.parametrize(
    "token",
    [
        issue_token(secret="wrong-secret"),
        issue_token(expires_at=1_800_000_099),
        issue_token(tenant_id="not-a-uuid"),
        issue_token(role="superadmin"),
    ],
)
def test_verify_tenant_token_fails_closed_for_invalid_assertions(token):
    with pytest.raises(TenantIdentityError):
        verify_tenant_token(token, "tenant-secret", now=1_800_000_100)
