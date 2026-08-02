"""Unit tests for waste_breakers.py — pure waste-pattern decision logic.

Mirrors test_budget_enforcement.py's approach to budget_enforcement.py: this
module is pure (no I/O, no clock), so every case is plain data in, verdicts
out. Covers each of the four breakers (repeated identical calls, retry
storm, oversized prompt, consecutive failures), resolve_mode's fail-open
behavior, and trip_detail's response shape."""

import pytest

import waste_breakers as wb


# ── resolve_mode ──────────────────────────────────────────────────────────────

class TestResolveMode:
    def test_none_defaults_to_observe(self):
        assert wb.resolve_mode(None) == wb.MODE_OBSERVE

    def test_empty_defaults_to_observe(self):
        assert wb.resolve_mode("") == wb.MODE_OBSERVE

    def test_normalizes_case_and_whitespace(self):
        assert wb.resolve_mode("  BLOCK ") == wb.MODE_BLOCK
        assert wb.resolve_mode("Observe") == wb.MODE_OBSERVE

    def test_invalid_value_fails_open_to_observe(self):
        assert wb.resolve_mode("explode") == wb.MODE_OBSERVE


# ── evaluate: each breaker in isolation ──────────────────────────────────────

class TestEvaluate:
    def test_always_returns_all_four_breakers(self):
        obs = wb.CallerObservation(caller="a")
        verdicts = wb.evaluate(obs)
        assert {v.breaker for v in verdicts} == set(wb.ALL_BREAKERS)

    def test_nothing_trips_on_a_quiet_caller(self):
        obs = wb.CallerObservation(
            caller="a", calls_in_window=1, identical_calls_in_window=1,
            consecutive_failures=0, estimated_prompt_tokens=500,
        )
        verdicts = wb.evaluate(obs)
        assert wb.tripped_only(verdicts) == []

    def test_repeated_identical_calls_trips_at_threshold(self):
        t = wb.BreakerThresholds(repeated_identical_calls=5)
        under = wb.CallerObservation(identical_calls_in_window=4)
        at = wb.CallerObservation(identical_calls_in_window=5)
        assert not wb._verdict(wb.REPEATED_IDENTICAL_CALLS, 4, 5, "").tripped
        verdicts_under = wb.evaluate(under, t)
        verdicts_at = wb.evaluate(at, t)
        v_under = next(v for v in verdicts_under if v.breaker == wb.REPEATED_IDENTICAL_CALLS)
        v_at = next(v for v in verdicts_at if v.breaker == wb.REPEATED_IDENTICAL_CALLS)
        assert v_under.tripped is False
        assert v_at.tripped is True

    def test_retry_storm_trips_at_threshold(self):
        t = wb.BreakerThresholds(retry_storm_calls=20)
        obs_under = wb.CallerObservation(calls_in_window=19)
        obs_at = wb.CallerObservation(calls_in_window=20)
        v_under = next(v for v in wb.evaluate(obs_under, t) if v.breaker == wb.RETRY_STORM)
        v_at = next(v for v in wb.evaluate(obs_at, t) if v.breaker == wb.RETRY_STORM)
        assert v_under.tripped is False
        assert v_at.tripped is True

    def test_oversized_prompt_trips_at_threshold(self):
        t = wb.BreakerThresholds(oversized_prompt_tokens=100_000)
        obs_under = wb.CallerObservation(estimated_prompt_tokens=99_999)
        obs_at = wb.CallerObservation(estimated_prompt_tokens=100_000)
        v_under = next(v for v in wb.evaluate(obs_under, t) if v.breaker == wb.OVERSIZED_PROMPT)
        v_at = next(v for v in wb.evaluate(obs_at, t) if v.breaker == wb.OVERSIZED_PROMPT)
        assert v_under.tripped is False
        assert v_at.tripped is True

    def test_consecutive_failures_trips_at_threshold(self):
        t = wb.BreakerThresholds(consecutive_failures=4)
        obs_under = wb.CallerObservation(consecutive_failures=3)
        obs_at = wb.CallerObservation(consecutive_failures=4)
        v_under = next(
            v for v in wb.evaluate(obs_under, t) if v.breaker == wb.CONSECUTIVE_FAILURES
        )
        v_at = next(v for v in wb.evaluate(obs_at, t) if v.breaker == wb.CONSECUTIVE_FAILURES)
        assert v_under.tripped is False
        assert v_at.tripped is True

    def test_multiple_breakers_can_trip_together(self):
        obs = wb.CallerObservation(
            calls_in_window=100, identical_calls_in_window=50,
            estimated_prompt_tokens=500_000, consecutive_failures=10,
        )
        tripped = {v.breaker for v in wb.tripped_only(wb.evaluate(obs))}
        assert tripped == set(wb.ALL_BREAKERS)

    def test_zero_threshold_never_trips(self):
        # Guards the `threshold > 0` clause: a breaker configured to 0 must
        # not become "always tripped" via `observed >= 0`.
        t = wb.BreakerThresholds(oversized_prompt_tokens=0)
        obs = wb.CallerObservation(estimated_prompt_tokens=0)
        v = next(v for v in wb.evaluate(obs, t) if v.breaker == wb.OVERSIZED_PROMPT)
        assert v.tripped is False


# ── trip_detail ───────────────────────────────────────────────────────────────

class TestTripDetail:
    def test_shape(self):
        v = wb._verdict(wb.RETRY_STORM, 25, 20, "25 calls in 60s (threshold 20)")
        detail = wb.trip_detail(v)
        assert detail["error"] == "waste_breaker_tripped"
        assert detail["breaker"] == wb.RETRY_STORM
        assert detail["observed"] == 25
        assert detail["threshold"] == 20
        assert detail["mode"] == wb.MODE_BLOCK
        assert "60s" in detail["message"]


class TestBreakerVerdictAsDict:
    def test_as_dict_round_trips_fields(self):
        v = wb._verdict(wb.OVERSIZED_PROMPT, 150_000, 100_000, "big prompt")
        d = v.as_dict()
        assert d == {
            "breaker": wb.OVERSIZED_PROMPT,
            "tripped": True,
            "observed": 150_000,
            "threshold": 100_000,
            "detail": "big prompt",
        }
