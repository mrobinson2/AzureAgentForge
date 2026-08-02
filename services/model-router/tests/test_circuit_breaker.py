"""Unit tests for circuit_breaker.py — the pure decision half of the
Fail-Closed Resilience Pack.

Everything here runs against a fake clock and plain data: no FastAPI, no
network, no sleeping. The state machine and the trip taxonomy are the two
things that must be exactly right, so they get the most coverage — a breaker
that trips on the wrong signal causes the outage it was built to prevent.
"""

import pytest

import circuit_breaker as cb


class FakeClock:
    """Monotonic-shaped clock the tests advance by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def registry(clock):
    return cb.CircuitBreakerRegistry(
        cb.BreakerConfig(failure_threshold=3, cooldown_seconds=60.0, half_open_probes=1),
        clock=clock,
    )


KEY = "cred:testkey01"


# ── State machine ─────────────────────────────────────────────────────────────

class TestStateTransitions:
    def test_starts_closed_and_admits(self, registry):
        verdict = registry.admit(KEY)
        assert verdict.allowed is True
        assert verdict.state == cb.STATE_CLOSED

    def test_failures_below_threshold_stay_closed(self, registry):
        for _ in range(2):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.state(KEY) == cb.STATE_CLOSED
        assert registry.admit(KEY).allowed is True

    def test_closed_to_open_at_threshold(self, registry):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.state(KEY) == cb.STATE_OPEN
        assert registry.is_open(KEY) is True

    def test_open_refuses_admission(self, registry):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_QUOTA_EXHAUSTED)
        verdict = registry.admit(KEY)
        assert verdict.allowed is False
        assert verdict.state == cb.STATE_OPEN
        # Retry-after counts down the remaining cooldown, not the whole thing.
        assert 0 < verdict.retry_after_seconds <= 60.0

    def test_success_resets_failure_count(self, registry):
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        registry.record_success(KEY)
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.state(KEY) == cb.STATE_CLOSED
        assert registry.snapshot(KEY)["failures"] == 1

    def test_open_to_half_open_after_cooldown(self, registry, clock):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(59.9)
        assert registry.state(KEY) == cb.STATE_OPEN
        clock.advance(0.2)
        assert registry.state(KEY) == cb.STATE_HALF_OPEN

    def test_half_open_admits_exactly_the_probe_budget(self, clock):
        registry = cb.CircuitBreakerRegistry(
            cb.BreakerConfig(failure_threshold=1, cooldown_seconds=10.0, half_open_probes=2),
            clock=clock,
        )
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(11)
        first = registry.admit(KEY)
        second = registry.admit(KEY)
        third = registry.admit(KEY)
        assert (first.allowed, second.allowed) == (True, True)
        assert first.state == second.state == cb.STATE_HALF_OPEN
        # Budget exhausted: a third concurrent call is refused, not queued.
        assert third.allowed is False
        assert third.state == cb.STATE_HALF_OPEN

    def test_half_open_probe_success_closes(self, registry, clock):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(61)
        assert registry.admit(KEY).allowed is True
        registry.record_success(KEY)
        assert registry.state(KEY) == cb.STATE_CLOSED
        assert registry.admit(KEY).allowed is True

    def test_half_open_probe_failure_reopens_immediately(self, registry, clock):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(61)
        assert registry.admit(KEY).allowed is True
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.state(KEY) == cb.STATE_OPEN
        # A failed probe restarts the whole cooldown rather than letting the
        # already-elapsed time count toward the next one.
        clock.advance(59)
        assert registry.state(KEY) == cb.STATE_OPEN
        clock.advance(2)
        assert registry.state(KEY) == cb.STATE_HALF_OPEN

    def test_half_open_probe_failure_does_not_need_the_threshold_again(self, registry, clock):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(61)
        registry.admit(KEY)
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)  # one is enough
        assert registry.snapshot(KEY)["trips"] == 2

    def test_half_open_rearms_when_a_probe_never_reports(self, registry, clock):
        """Stall guard: a host that admits a probe and then never records an
        outcome (crash, cancelled request, bug) must not wedge the breaker in
        half-open with zero probes forever."""
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(61)
        assert registry.admit(KEY).allowed is True   # probe consumed...
        assert registry.admit(KEY).allowed is False  # ...and nothing reported
        clock.advance(61)
        assert registry.admit(KEY).allowed is True

    def test_failure_while_open_does_not_extend_cooldown(self, registry, clock):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(30)
        # A call admitted before the trip lands late and fails.
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(31)
        assert registry.state(KEY) == cb.STATE_HALF_OPEN

    def test_keys_are_independent(self, registry):
        for _ in range(3):
            registry.record_failure("cred:aaa", reason=cb.TRIP_AUTH_FAILURE)
        assert registry.is_open("cred:aaa") is True
        assert registry.is_open("cred:bbb") is False

    def test_is_open_is_false_while_half_open(self, registry, clock):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        clock.advance(61)
        # Half-open is the probing state — a host asking "is this shut off?"
        # must get False so the probe can happen.
        assert registry.is_open(KEY) is False


class TestDisabledRegistry:
    def test_disabled_always_admits_and_never_records(self, clock):
        registry = cb.CircuitBreakerRegistry(
            cb.BreakerConfig(enabled=False, failure_threshold=1), clock=clock,
        )
        for _ in range(10):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.admit(KEY).allowed is True
        assert registry.state(KEY) == cb.STATE_CLOSED
        assert registry.snapshot(KEY) == {"state": cb.STATE_CLOSED, "enabled": False}


class TestTransitionCallback:
    def test_trip_and_recovery_are_reported(self, clock):
        events = []
        registry = cb.CircuitBreakerRegistry(
            cb.BreakerConfig(failure_threshold=2, cooldown_seconds=5.0),
            clock=clock,
            on_transition=events.append,
        )
        registry.record_failure(KEY, reason=cb.TRIP_QUOTA_EXHAUSTED)
        assert events == []  # below threshold — nothing changed
        registry.record_failure(KEY, reason=cb.TRIP_QUOTA_EXHAUSTED)
        assert events[-1]["from_state"] == cb.STATE_CLOSED
        assert events[-1]["to_state"] == cb.STATE_OPEN
        assert events[-1]["reason"] == cb.TRIP_QUOTA_EXHAUSTED

        clock.advance(6)
        registry.admit(KEY)
        assert events[-1]["to_state"] == cb.STATE_HALF_OPEN
        registry.record_success(KEY)
        assert events[-1]["to_state"] == cb.STATE_CLOSED

    def test_callback_failure_never_breaks_the_breaker(self, clock):
        def boom(_event):
            raise RuntimeError("telemetry sink is down")

        registry = cb.CircuitBreakerRegistry(
            cb.BreakerConfig(failure_threshold=1), clock=clock, on_transition=boom,
        )
        registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.is_open(KEY) is True


# ── Trip taxonomy ─────────────────────────────────────────────────────────────

class _Err(Exception):
    """Stand-in for an SDK exception carrying an HTTP status."""

    def __init__(self, message="boom", status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def _named(name, **attrs):
    """Build an exception whose CLASS NAME matches an SDK's, so classification
    can be tested without importing litellm/anthropic."""
    return type(name, (Exception,), {})("boom", **attrs) if attrs else type(
        name, (Exception,), {}
    )("boom")


class TestTripClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (401, cb.TRIP_AUTH_FAILURE),
            (403, cb.TRIP_AUTH_FAILURE),
            (429, cb.TRIP_QUOTA_EXHAUSTED),
        ],
    )
    def test_credential_and_quota_statuses_trip(self, status, expected):
        assert cb.classify_failure(_Err(status_code=status)) == expected
        assert cb.classify_failure(status_code=status) == expected

    @pytest.mark.parametrize("status", [400, 404, 409, 413, 422, 500, 502, 503])
    def test_other_statuses_never_trip(self, status):
        assert cb.classify_failure(_Err(status_code=status)) is None
        assert cb.classify_failure(status_code=status) is None

    @pytest.mark.parametrize("name", sorted(cb.CONNECTION_EXCEPTION_NAMES))
    def test_connection_exception_names_trip(self, name):
        assert cb.classify_failure(_named(name)) == cb.TRIP_CONNECTION_FAILURE

    @pytest.mark.parametrize("name", sorted(cb.AUTH_EXCEPTION_NAMES))
    def test_auth_exception_names_trip(self, name):
        assert cb.classify_failure(_named(name)) == cb.TRIP_AUTH_FAILURE

    @pytest.mark.parametrize("name", sorted(cb.QUOTA_EXCEPTION_NAMES))
    def test_quota_exception_names_trip(self, name):
        assert cb.classify_failure(_named(name)) == cb.TRIP_QUOTA_EXHAUSTED

    @pytest.mark.parametrize("name", sorted(cb.NON_TRIPPING_EXCEPTION_NAMES))
    def test_excluded_exception_names_never_trip(self, name):
        """The acceptance bar for this feature: a healthy-but-empty response, a
        content-filter verdict, a malformed body, or a caller's bad request
        must never move the breaker. Counting them would trip on traffic that
        has nothing to do with the credential."""
        assert cb.classify_failure(_named(name)) is None

    def test_a_returned_status_beats_the_class_name(self):
        """An AuthenticationError carrying a 404 is a routing mistake (wrong
        deployment name), not a credential problem."""
        exc = type("AuthenticationError", (Exception,), {})("boom")
        exc.status_code = 404
        assert cb.classify_failure(exc) is None

    def test_oauth_error_identifiers_in_text_trip(self):
        assert cb.classify_failure(Exception("refresh failed: invalid_grant")) == \
            cb.TRIP_AUTH_FAILURE
        assert cb.classify_failure(Exception("invalid_api_key")) == cb.TRIP_AUTH_FAILURE

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "the model returned an empty response",
            "could not parse tool call arguments",
            "I'm sorry, I can't help with that",
            "unauthorized access is a theme in the user's novel",
            "the answer is 429 kilometres",
        ],
    )
    def test_ordinary_and_model_content_text_never_trips(self, text):
        assert cb.classify_failure(Exception(text)) is None

    def test_no_signal_at_all_never_trips(self):
        assert cb.classify_failure() is None
        assert cb.classify_failure(None) is None


# ── Keying ────────────────────────────────────────────────────────────────────

class TestCredentialKey:
    def test_same_credential_same_key(self):
        a = cb.credential_key(api_base="https://example.invalid/v1", api_key="k1")
        b = cb.credential_key(api_base="https://example.invalid/v1", api_key="k1")
        assert a == b

    def test_different_key_or_endpoint_splits_the_breaker(self):
        base = cb.credential_key(api_base="https://example.invalid/v1", api_key="k1")
        other_key = cb.credential_key(api_base="https://example.invalid/v1", api_key="k2")
        other_base = cb.credential_key(api_base="https://other.invalid/v1", api_key="k1")
        assert len({base, other_key, other_base}) == 3

    def test_key_leaks_neither_hostname_nor_key_material(self):
        key = cb.credential_key(api_base="https://secret-host.invalid/v1", api_key="sk-abc123")
        assert "secret-host" not in key
        assert "sk-abc123" not in key
        assert key.startswith("cred:")

    def test_missing_config_is_still_keyable(self):
        assert cb.credential_key(api_base=None, api_key=None).startswith("cred:")


# ── Config parsing ────────────────────────────────────────────────────────────

class TestConfigParsing:
    @pytest.mark.parametrize("raw", ["0", "-1", "abc", "", None, "1.5"])
    def test_positive_int_rejects_wedging_values(self, raw):
        # A 0 threshold trips on the first request; a 0 probe budget can never
        # admit a probe. Both are misconfigurations, not policies.
        assert cb.positive_int(raw, 5) == 5

    def test_positive_int_accepts_a_real_value(self):
        assert cb.positive_int("9", 5) == 9
        assert cb.positive_int(" 9 ", 5) == 9

    @pytest.mark.parametrize("raw", ["0", "-3", "nope", None])
    def test_positive_float_falls_back(self, raw):
        assert cb.positive_float(raw, 60.0) == 60.0

    def test_positive_float_accepts_a_real_value(self):
        assert cb.positive_float("2.5", 60.0) == 2.5


# ── Reporting ─────────────────────────────────────────────────────────────────

class TestReporting:
    def test_snapshot_reports_state_and_policy(self, registry):
        registry.record_failure(KEY, reason=cb.TRIP_QUOTA_EXHAUSTED)
        snap = registry.snapshot(KEY)
        assert snap["state"] == cb.STATE_CLOSED
        assert snap["failures"] == 1
        assert snap["failure_threshold"] == 3
        assert snap["cooldown_seconds"] == 60.0

    def test_open_keys_lists_only_open_breakers(self, registry):
        for _ in range(3):
            registry.record_failure("cred:aaa", reason=cb.TRIP_AUTH_FAILURE)
        registry.record_failure("cred:bbb", reason=cb.TRIP_AUTH_FAILURE)
        assert registry.open_keys() == ["cred:aaa"]

    def test_reset_clears_state(self, registry):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        assert registry.reset(KEY) == 1
        assert registry.state(KEY) == cb.STATE_CLOSED

    def test_reset_all(self, registry):
        registry.record_failure("cred:aaa", reason=cb.TRIP_AUTH_FAILURE)
        registry.record_failure("cred:bbb", reason=cb.TRIP_AUTH_FAILURE)
        assert registry.reset() == 2
        assert registry.snapshot_all() == {}


class TestOpenDetail:
    def test_body_is_machine_readable(self, registry):
        for _ in range(3):
            registry.record_failure(KEY, reason=cb.TRIP_AUTH_FAILURE)
        verdict = registry.admit(KEY)
        detail = cb.open_detail(verdict, tier="gpt4o-mini")
        assert detail["code"] == cb.BREAKER_OPEN_ERROR_CODE
        assert detail["error"] == "upstream_breaker_open"
        assert detail["tier"] == "gpt4o-mini"
        assert detail["fail_closed"] is True
        assert detail["retryable"] is True
        assert detail["retry_after_seconds"] > 0

    def test_fail_closed_fallback_variant_names_both_tiers(self, registry):
        verdict = cb.AdmitVerdict(allowed=False, state=cb.STATE_OPEN, key=KEY)
        detail = cb.open_detail(
            verdict, tier="gpt4o-mini", fail_closed_fallback=True, primary_tier="claude",
        )
        assert "claude" in detail["message"]
        assert "gpt4o-mini" in detail["message"]
        assert detail["primary_tier"] == "claude"
