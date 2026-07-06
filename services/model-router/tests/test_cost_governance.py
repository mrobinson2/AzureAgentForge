"""Per-caller cost governance: on top of the per-tier daily caps, record_cost
attributes spend to a caller (agent/tenant) id, is_over_caller_budget enforces a
per-caller daily ceiling (fail-open when unset), and daily_cost_rollup snapshots
today's spend. The autouse isolation fixture restores the ledgers between tests."""

from datetime import date

import pytest


def _pin_today(router):
    router._budget_date = str(date.today())


class TestPerCallerAttribution:
    def test_record_cost_attributes_to_caller(self, router):
        _pin_today(router)
        router._spend.clear()
        router._spend_by_caller.clear()
        router.record_cost("gpt4o-mini", 0.10, caller="agent-a")
        router.record_cost("gpt4o-mini", 0.05, caller="agent-a")
        router.record_cost("gpt4o-mini", 0.20, caller="agent-b")
        assert router._spend_by_caller["agent-a"] == pytest.approx(0.15)
        assert router._spend_by_caller["agent-b"] == pytest.approx(0.20)
        # Per-tier total still accumulates across callers.
        assert router._spend["gpt4o-mini"] == pytest.approx(0.35)

    def test_no_caller_is_not_attributed(self, router):
        _pin_today(router)
        router._spend_by_caller.clear()
        router.record_cost("gpt4o-mini", 0.10)
        assert dict(router._spend_by_caller) == {}


class TestPerCallerCap:
    def test_fails_open_when_no_cap_configured(self, router, monkeypatch):
        monkeypatch.setattr(router, "PER_CALLER_DAILY_USD", 0.0)
        _pin_today(router)
        router._spend_by_caller.clear()
        router.record_cost("gpt4o-mini", 999.0, caller="agent-a")
        assert router.is_over_caller_budget("agent-a") is False

    def test_over_cap_when_configured_and_exceeded(self, router, monkeypatch):
        monkeypatch.setattr(router, "PER_CALLER_DAILY_USD", 1.0)
        _pin_today(router)
        router._spend_by_caller.clear()
        router.record_cost("gpt4o-mini", 0.9, caller="agent-a")
        assert router.is_over_caller_budget("agent-a") is False
        router.record_cost("gpt4o-mini", 0.2, caller="agent-a")
        assert router.is_over_caller_budget("agent-a") is True

    def test_explicit_cap_override(self, router):
        _pin_today(router)
        router._spend_by_caller.clear()
        router.record_cost("gpt4o-mini", 0.5, caller="agent-a")
        assert router.is_over_caller_budget("agent-a", cap=0.4) is True
        assert router.is_over_caller_budget("agent-a", cap=1.0) is False

    def test_no_caller_never_over(self, router, monkeypatch):
        monkeypatch.setattr(router, "PER_CALLER_DAILY_USD", 0.01)
        assert router.is_over_caller_budget(None) is False


class TestRollup:
    def test_rollup_shape(self, router):
        _pin_today(router)
        router._spend.clear()
        router._spend_by_caller.clear()
        router.record_cost("gpt4o-mini", 0.10, caller="agent-a")
        router.record_cost("phi4", 0.02, caller="agent-b")
        roll = router.daily_cost_rollup()
        assert roll["total_usd"] == pytest.approx(0.12)
        assert roll["by_tier"]["gpt4o-mini"] == pytest.approx(0.10)
        assert roll["by_caller"]["agent-b"] == pytest.approx(0.02)
        assert roll["date"] == str(date.today())


class TestResolvers:
    def test_resolve_caller_id_precedence(self, router):
        assert router.resolve_caller_id({"x-agent-id": "a1"}) == "a1"
        assert router.resolve_caller_id({"x-tenant-id": "t1"}) == "t1"
        assert router.resolve_caller_id({}) is None

    def test_resolve_correlation_id(self, router):
        assert router.resolve_correlation_id({"x-correlation-id": "c1"}) == "c1"
        assert router.resolve_correlation_id({"x-request-id": "r1"}) == "r1"
        assert router.resolve_correlation_id({}) is None
