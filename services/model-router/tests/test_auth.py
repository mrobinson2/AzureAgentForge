"""Tests for `_verify_auth` — the Bearer-token gate. FAIL CLOSED (aaf-0005):
when ROUTER_API_KEY (module global `_ROUTER_API_KEY`) is empty the router
refuses every request (503) instead of serving unauthenticated traffic; when
set, every protected endpoint requires a matching token. Comparison is
constant-time over SHA-256 digests (hmac.compare_digest)."""

import pytest
from fastapi import HTTPException


class TestVerifyAuth:
    def test_fails_closed_when_unconfigured(self, router, monkeypatch, make_request):
        # aaf-0005: an unset key must mean "down" (503), never "open".
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(make_request(headers={"authorization": "Bearer anything"}))
        assert exc.value.status_code == 503

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

    def test_x_api_key_passes(self, router, monkeypatch, make_request):
        """Anthropic-native clients (Hermes anthropic_messages transport) send
        x-api-key instead of Authorization — same key, alternate header."""
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        req = make_request(headers={"x-api-key": "s3cret"})
        assert router._verify_auth(req) is None

    def test_x_api_key_wrong_403(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(make_request(headers={"x-api-key": "wrong"}))
        assert exc.value.status_code == 403

    def test_bearer_wins_over_x_api_key(self, router, monkeypatch, make_request):
        """When both headers are present the Bearer token is authoritative — a
        wrong Bearer is rejected even if x-api-key is correct (no fallback that
        would mask a misconfigured primary credential)."""
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(
                make_request(headers={"authorization": "Bearer wrong", "x-api-key": "s3cret"})
            )
        assert exc.value.status_code == 403

    def test_x_api_key_fails_closed_when_unconfigured(self, router, monkeypatch, make_request):
        # aaf-0005 also holds for the x-api-key scheme.
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "")
        with pytest.raises(HTTPException) as exc:
            router._verify_auth(make_request(headers={"x-api-key": "anything"}))
        assert exc.value.status_code == 503

    def test_endpoint_rejects_unauthenticated(self, unauth_client, router, monkeypatch):
        """End-to-end: a protected endpoint 401s when the key is set and no
        header is sent (exercises the guard through the real ASGI Request)."""
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "s3cret")
        r = unauth_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 401

    def test_endpoint_503_when_key_unset(self, unauth_client, router, monkeypatch):
        """aaf-0005: fail closed end-to-end — a protected endpoint 503s when the
        key is unset, rather than serving the request unauthenticated."""
        monkeypatch.setattr(router, "_ROUTER_API_KEY", "")
        r = unauth_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 503
