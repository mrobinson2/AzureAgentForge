"""Tests for daily budget tracking and the over-budget fallback in routing.

`record_cost` accumulates per-tier spend for the day; `is_over_budget`
compares spend to the tier's `daily_budget`; `_reset_if_new_day` zeroes spend
when the calendar day rolls. `select_tier` uses this: a gpt4o-mini request
that has exhausted its budget routes to phi4 instead. The autouse isolation
fixture restores `_spend`/`_budget_date` between tests."""

from datetime import date

import pytest


def _pin_today(router):
    """Pin the budget day to today so a stale `_budget_date` doesn't trigger
    `_reset_if_new_day` and wipe directly-injected spend."""
    router._budget_date = str(date.today())


class TestBudgetTracking:
    def test_record_cost_accumulates(self, router):
        router._spend.clear()
        router.record_cost("gpt4o-mini", 0.10)
        router.record_cost("gpt4o-mini", 0.05)
        assert router._spend["gpt4o-mini"] == pytest.approx(0.15)

    def test_is_over_budget_false_under_limit(self, router):
        router._spend.clear()
        limit = router.MODELS["gpt4o-mini"]["daily_budget"]
        router.record_cost("gpt4o-mini", limit / 2)
        assert router.is_over_budget("gpt4o-mini") is False

    def test_is_over_budget_true_at_limit(self, router):
        router._spend.clear()
        limit = router.MODELS["gpt4o-mini"]["daily_budget"]
        router.record_cost("gpt4o-mini", limit)
        # Spend >= budget is over (the comparison is `>=`).
        assert router.is_over_budget("gpt4o-mini") is True

    def test_is_over_budget_true_above_limit(self, router):
        router._spend.clear()
        limit = router.MODELS["gpt4o-mini"]["daily_budget"]
        router.record_cost("gpt4o-mini", limit + 1.0)
        assert router.is_over_budget("gpt4o-mini") is True

    def test_reset_if_new_day_clears_spend(self, router):
        router._spend.clear()
        router.record_cost("gpt4o-mini", 1.0)
        # Force the recorded budget date into the past so the next call resets.
        router._budget_date = "1999-01-01"
        router._reset_if_new_day()
        assert router._spend.get("gpt4o-mini", 0.0) == 0.0
        # And the date is now refreshed to today's value.
        from datetime import date

        assert router._budget_date == str(date.today())


class TestOverBudgetRouting:
    def test_gpt4o_over_budget_routes_to_phi4(self, router):
        router._spend.clear()
        _pin_today(router)
        # Exhaust gpt4o-mini's daily budget.
        router._spend["gpt4o-mini"] = router.MODELS["gpt4o-mini"]["daily_budget"]
        tier = router.select_tier(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert tier == "phi4"

    def test_gpt4o_under_budget_stays_gpt4o(self, router):
        router._spend.clear()
        tier = router.select_tier(
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert tier == "gpt4o-mini"

    def test_phi4_request_unaffected_by_gpt4o_budget(self, router):
        router._spend.clear()
        _pin_today(router)
        router._spend["gpt4o-mini"] = router.MODELS["gpt4o-mini"]["daily_budget"]
        # An explicit phi4 request is not rerouted by gpt4o-mini's exhaustion.
        tier = router.select_tier(
            {"tier": "phi4", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert tier == "phi4"
