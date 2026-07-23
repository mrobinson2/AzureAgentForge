"""Per-user token issue/verify — offline unit tests."""

import pytest

from user_tokens import (
    Principal,
    TokenError,
    issue_user_token,
    verify_user_token,
)

SECRET = "test-signing-secret"


def _token(**over):
    kw = dict(user_id="u1", tenant_id="t1", role="operator", secret=SECRET)
    kw.update(over)
    return issue_user_token(**kw)


def test_round_trip_returns_full_principal():
    tok = _token()
    p = verify_user_token(tok, SECRET)
    assert p == Principal(user_id="u1", tenant_id="t1", role="operator")


def test_issue_rejects_unknown_role():
    with pytest.raises(ValueError):
        _token(role="superadmin")


def test_issue_rejects_empty_ids():
    with pytest.raises(ValueError):
        _token(user_id="")
    with pytest.raises(ValueError):
        _token(tenant_id="")


def test_tampered_payload_fails_signature():
    header, payload, sig = _token().split(".")
    # swap in a different payload (role escalation attempt) keeping the old sig
    forged_payload = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB")
    with pytest.raises(TokenError):
        verify_user_token(f"{header}.{forged_payload}.{sig}", SECRET)


def test_alg_none_is_rejected():
    import base64
    import json

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1", "tenant_id": "t1", "role": "owner"}).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(TokenError):
        verify_user_token(f"{header}.{payload}.", SECRET)


def test_wrong_secret_is_rejected():
    with pytest.raises(TokenError):
        verify_user_token(_token(), "other-secret")


def test_expired_token_is_rejected():
    tok = issue_user_token(
        user_id="u1", tenant_id="t1", role="member", secret=SECRET,
        ttl_seconds=100, now=1000,
    )
    # clock past exp (1000 + 100)
    with pytest.raises(TokenError):
        verify_user_token(tok, SECRET, now=1101)
    # still valid before exp
    assert verify_user_token(tok, SECRET, now=1050).user_id == "u1"


def test_missing_claim_is_rejected():
    # hand-build a validly-signed token missing the role claim
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"sub": "u1", "tenant_id": "t1"}).encode())  # no role
    sig = b64(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    with pytest.raises(TokenError):
        verify_user_token(f"{h}.{p}.{sig}", SECRET)
