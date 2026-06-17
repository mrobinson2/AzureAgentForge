"""Tests for `_check_rate_limit` — the per-client-IP sliding-window limiter.
Window state lives in the module global `_rate_windows` (reset between tests
by the autouse isolation fixture). The limit is `_RATE_LIMIT_RPM`; <= 0
disables it entirely."""

import time

import pytest
from fastapi import HTTPException


class TestCheckRateLimit:
    def test_disabled_when_zero_rpm(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_RATE_LIMIT_RPM", 0)
        req = make_request(host="198.51.100.1")
        # Even a flood is allowed when disabled.
        for _ in range(100):
            assert router._check_rate_limit(req) is None

    def test_allows_up_to_limit(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_RATE_LIMIT_RPM", 3)
        req = make_request(host="198.51.100.2")
        for _ in range(3):
            assert router._check_rate_limit(req) is None

    def test_rejects_over_limit(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_RATE_LIMIT_RPM", 3)
        req = make_request(host="198.51.100.3")
        for _ in range(3):
            router._check_rate_limit(req)
        with pytest.raises(HTTPException) as exc:
            router._check_rate_limit(req)
        assert exc.value.status_code == 429
        assert "Rate limit" in exc.value.detail

    def test_separate_ips_have_separate_windows(self, router, monkeypatch, make_request):
        monkeypatch.setattr(router, "_RATE_LIMIT_RPM", 2)
        a = make_request(host="198.51.100.4")
        b = make_request(host="198.51.100.5")
        router._check_rate_limit(a)
        router._check_rate_limit(a)
        # a is now at its limit, but b is untouched.
        assert router._check_rate_limit(b) is None
        assert router._check_rate_limit(b) is None
        with pytest.raises(HTTPException):
            router._check_rate_limit(a)

    def test_old_entries_are_pruned(self, router, monkeypatch, make_request):
        """Timestamps older than 60s are dropped before the limit check, so a
        client that was saturated a minute ago is allowed again."""
        monkeypatch.setattr(router, "_RATE_LIMIT_RPM", 2)
        ip = "198.51.100.6"
        # Pre-seed two timestamps well outside the 60s window, measured against
        # the *live* monotonic clock. A literal like 0.0 is unsafe: monotonic's
        # origin is arbitrary, so on a freshly-booted host where now < 60 the
        # cutoff (now - 60) is negative and 0.0 sorts as "recent", leaving the
        # entry inside the window (this exact case failed only in CI).
        old = time.monotonic() - 120.0
        router._rate_windows[ip] = [old, old]
        req = make_request(host=ip)
        # Ancient entries are pruned; this request is admitted, not rejected.
        assert router._check_rate_limit(req) is None
        # Exactly one live entry remains (the one we just added).
        assert len(router._rate_windows[ip]) == 1

    def test_unknown_client_host(self, router, monkeypatch):
        """A request with no client info buckets under 'unknown' and still works."""
        monkeypatch.setattr(router, "_RATE_LIMIT_RPM", 5)

        class NoClient:
            headers = {}
            client = None

        assert router._check_rate_limit(NoClient()) is None
