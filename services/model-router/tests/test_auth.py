"""Tests for `_verify_auth` — the optional Bearer-token gate. When
ROUTER_API_KEY (module global `_ROUTER_API_KEY`) is empty the router is in
internal-only mode and the check is a no-op; when set, every protected
endpoint requires a matching token. Comparison is constant-time over SHA-256
digests."""

import pytest
from fastapi import HTTPException


class TestVerifyAuth:
    def test_noop_when_unconfigured(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "")
        # No header at all — must not raise in internal-only mode.
        assert router._verify_auth(make_request(headers={})) is None

    def test_missing_header_401(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(make_request(headers={}))
        assert exc.value.status_code == 401
        assert "Authorization" in exc.value.detail

    def test_non_bearer_scheme_401(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(make_request(headers={"authorization": "Basic s3cret"}))
        assert exc.value.status_code == 401

    def test_wrong_token_403(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(make_request(headers={"authorization": "Bearer wrong"}))
        assert exc.value.status_code == 403
        assert "Invalid" in exc.value.detail

    def test_correct_token_passes(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        assert router._verify_auth(make_request(headers={"authorization": "Bearer s3cret"})) is None

    def test_bearer_case_insensitive_and_trimmed(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        # lowercase scheme + surrounding whitespace on the token both tolerated.
        req = make_request(headers={"authorization": "bearer   s3cret  "})
        assert router._verify_auth(req) is None

    def test_endpoint_rejects_unauthenticated(self, client, router, monkeypatch):
        """End-to-end: a protected endpoint 401s when the key is set and no
        header is sent (exercises the guard through the real ASGI Request)."""
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401
