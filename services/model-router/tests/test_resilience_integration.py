"""Endpoint-level wiring tests for the Fail-Closed Resilience Pack.

The claim this pack makes is operational, not algorithmic: *while a breaker is
open the router does not call that upstream*, and *while a kill-switch scope is
engaged the router does not spend*. Both are only true if the guard sits
outside the dispatch function, so every test that matters here asserts against
a mock that counts invocations — a passing state-machine unit test proves
nothing about whether main.py actually consults it.

Follows the suite's established pattern (see test_flight_recorder_integration.py):
upstream calls are monkeypatched so nothing leaves the process, and mutable
module globals are monkeypatched per test.
"""

import pytest

import circuit_breaker as cb
import flight_recorder as fr
import kill_switch as ks


def _chat_body(model="gpt4o-mini", content="hi"):
    return {"model": model, "messages": [{"role": "user", "content": content}]}


def _ok_result():
    return {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        "__router_cost_usd__": 0.0,
    }


class Upstream401(Exception):
    """Credential rejected — a breaker-eligible signal."""

    status_code = 401


class ModelReturnedNothing(Exception):
    """A healthy-but-useless response. Must NEVER move a breaker."""


class FakeClock:
    def __init__(self, now=5000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def breakers(router, monkeypatch, clock):
    """A fresh registry with a fake clock and a low threshold, wired into the
    router for one test. Transitions still flow to the real handler so the
    flight-recorder side of the wiring is exercised too."""
    registry = cb.CircuitBreakerRegistry(
        cb.BreakerConfig(failure_threshold=2, cooldown_seconds=30.0, half_open_probes=1),
        clock=clock,
        on_transition=router._on_breaker_transition,
    )
    monkeypatch.setattr(router, "_breakers", registry)
    monkeypatch.setattr(router, "BREAKERS_ENABLED", True)
    monkeypatch.setattr(router, "_BREAKER_FAIL_CLOSED", True)
    return registry


@pytest.fixture()
def switch(router, monkeypatch):
    fresh = ks.KillSwitch(on_event=router._on_kill_switch_event)
    monkeypatch.setattr(router, "_kill_switch", fresh)
    return fresh


@pytest.fixture()
def recorder(router, monkeypatch):
    rec = fr.FlightRecorder(max_events=50, redact=True)
    monkeypatch.setattr(router, "_flight_recorder", rec)
    return rec


@pytest.fixture()
def counting_upstream(router, monkeypatch):
    """Replaces _call_model with a counter whose behavior each test sets."""

    calls = []

    class Upstream:
        def __init__(self):
            self.raises = None

        async def __call__(self, tier, body):
            calls.append(tier)
            if self.raises is not None:
                raise self.raises
            return _ok_result()

        @property
        def calls(self):
            return calls

    upstream = Upstream()
    monkeypatch.setattr(router, "_call_model", upstream)
    return upstream


def _register_local_tier(router, name="tiny-local", fallback_for="gpt4o-mini"):
    """Add a zero-marginal-cost edge tier. conftest restores MODELS and
    _FALLBACK_PREFERENCE after each test."""
    router.MODELS[name] = {
        "litellm_model": f"openai/{name}",
        "api_base": "http://localhost:11434",
        "api_key": "ollama",
        "daily_budget": 1000.0,
        "max_tokens": 2048,
        "context_limit": 32768,
        "timeout_seconds": 120,
        "supports_tools": False,
        "is_ollama": True,
    }
    router._FALLBACK_PREFERENCE[fallback_for] = [name]
    return name


# ── Breaker: trip, refuse, and never invoke the upstream ─────────────────────

class TestBreakerFailClosed:
    def test_trips_after_threshold_then_refuses_without_calling_upstream(
        self, client, router, breakers, counting_upstream
    ):
        counting_upstream.raises = Upstream401("invalid credential")

        # First request burns the primary and its fallback and reports the
        # ordinary "everything errored" 502.
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 502
        # The second request's primary failure crosses the threshold mid-flight,
        # so the fallback hop is already refused rather than attempted.
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 503

        calls_before = len(counting_upstream.calls)
        assert calls_before > 0
        assert breakers.is_open(router._breaker_key("gpt4o-mini")) is True

        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == cb.BREAKER_OPEN_ERROR_CODE
        assert r.headers["X-Router-Error-Code"] == cb.BREAKER_OPEN_ERROR_CODE
        # THE claim: nothing new reached the upstream.
        assert len(counting_upstream.calls) == calls_before

    def test_open_primary_refuses_a_metered_fallback(
        self, client, router, breakers, counting_upstream
    ):
        """Fail-closed cost posture: a credential outage must not silently
        become spend on a different metered deployment."""
        key = router._breaker_key("gpt4o-mini")
        for _ in range(2):
            breakers.record_failure(key, reason=cb.TRIP_AUTH_FAILURE)
        assert breakers.is_open(key) is True

        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == cb.BREAKER_OPEN_ERROR_CODE
        assert r.json()["detail"]["fail_closed"] is True
        assert counting_upstream.calls == []

    def test_open_primary_still_allows_a_free_local_fallback(
        self, client, router, breakers, counting_upstream
    ):
        """Zero marginal cost is not what a cost control is for — an edge host
        keeps serving so a credential outage doesn't become an outage."""
        local = _register_local_tier(router)
        for _ in range(2):
            breakers.record_failure(
                router._breaker_key("gpt4o-mini"), reason=cb.TRIP_AUTH_FAILURE
            )

        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200
        assert r.json()["_router"]["tier"] == local
        assert r.json()["_router"]["fallback_from"] == "gpt4o-mini"
        assert counting_upstream.calls == [local]

    def test_disabled_breakers_never_refuse(
        self, client, router, breakers, counting_upstream, monkeypatch
    ):
        monkeypatch.setattr(router, "BREAKERS_ENABLED", False)
        monkeypatch.setattr(
            router, "_breakers",
            cb.CircuitBreakerRegistry(cb.BreakerConfig(enabled=False, failure_threshold=1)),
        )
        counting_upstream.raises = Upstream401("invalid credential")
        for _ in range(4):
            assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 502
        # Every request still reached the upstream — the pack is genuinely off.
        assert len(counting_upstream.calls) >= 8


class TestBreakerTaxonomyThroughTheRouter:
    def test_empty_or_malformed_responses_never_trip(
        self, client, router, breakers, counting_upstream
    ):
        """The excluded-signal bar, asserted end to end: a model that keeps
        returning nothing useful is not a credential problem, and a breaker
        that counts it would refuse healthy traffic."""
        counting_upstream.raises = ModelReturnedNothing("empty completion")
        for _ in range(6):
            assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 502
        assert breakers.state(router._breaker_key("gpt4o-mini")) == cb.STATE_CLOSED
        assert breakers.state(router._breaker_key("phi4")) == cb.STATE_CLOSED
        assert len(counting_upstream.calls) == 12

    def test_a_success_clears_accumulated_failures(
        self, client, router, breakers, counting_upstream
    ):
        counting_upstream.raises = Upstream401("invalid credential")
        client.post("/v1/chat/completions", json=_chat_body())
        counting_upstream.raises = None
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 200
        counting_upstream.raises = Upstream401("invalid credential")
        client.post("/v1/chat/completions", json=_chat_body())
        # One failure before and one after the success — never two in a row.
        assert breakers.state(router._breaker_key("gpt4o-mini")) == cb.STATE_CLOSED


class TestHalfOpenProbeThroughTheRouter:
    def test_cooldown_admits_one_probe_and_success_closes(
        self, client, router, breakers, counting_upstream, clock
    ):
        key = router._breaker_key("gpt4o-mini")
        counting_upstream.raises = Upstream401("invalid credential")
        for _ in range(2):
            client.post("/v1/chat/completions", json=_chat_body())
        assert breakers.is_open(key) is True

        clock.advance(31)
        counting_upstream.raises = None
        calls_before = len(counting_upstream.calls)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200
        assert len(counting_upstream.calls) == calls_before + 1  # exactly one probe
        assert breakers.state(key) == cb.STATE_CLOSED

    def test_a_failed_probe_reopens(
        self, client, router, breakers, counting_upstream, clock
    ):
        key = router._breaker_key("gpt4o-mini")
        counting_upstream.raises = Upstream401("invalid credential")
        for _ in range(2):
            client.post("/v1/chat/completions", json=_chat_body())

        clock.advance(31)
        probe_baseline = len(counting_upstream.calls)
        client.post("/v1/chat/completions", json=_chat_body())
        # The probe genuinely reached the (still broken) upstream...
        assert len(counting_upstream.calls) == probe_baseline + 1
        # ...and one failure was enough to re-open, no re-accumulation.
        assert breakers.is_open(key) is True

        calls_before = len(counting_upstream.calls)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 503
        assert len(counting_upstream.calls) == calls_before


# ── Kill switch ───────────────────────────────────────────────────────────────

class TestKillSwitchScopes:
    def test_paid_fallback_leaves_the_primary_alone(
        self, client, router, breakers, switch, counting_upstream
    ):
        switch.engage(ks.SCOPE_PAID_FALLBACK, actor="test", reason="bill alert")
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 200
        assert counting_upstream.calls == ["gpt4o-mini"]

    def test_paid_fallback_blocks_the_metered_fallback_hop(
        self, client, router, breakers, switch, counting_upstream
    ):
        switch.engage(ks.SCOPE_PAID_FALLBACK)
        counting_upstream.raises = ModelReturnedNothing("nope")

        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == ks.KILL_SWITCH_ERROR_CODE
        assert r.json()["detail"]["kill_switch_scope"] == ks.SCOPE_PAID_FALLBACK
        assert r.headers["X-Router-Kill-Switch-Scope"] == ks.SCOPE_PAID_FALLBACK
        # The primary was tried; the paid fallback was not.
        assert counting_upstream.calls == ["gpt4o-mini"]

    def test_paid_fallback_still_allows_a_free_fallback(
        self, client, router, breakers, switch, monkeypatch
    ):
        local = _register_local_tier(router)
        switch.engage(ks.SCOPE_PAID_FALLBACK)
        seen = []

        async def fail_primary_only(tier, body):
            seen.append(tier)
            if tier == "gpt4o-mini":
                raise ModelReturnedNothing("nope")
            return _ok_result()

        monkeypatch.setattr(router, "_call_model", fail_primary_only)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200
        assert r.json()["_router"]["tier"] == local
        assert seen == ["gpt4o-mini", local]

    def test_all_paid_blocks_the_primary_without_calling_upstream(
        self, client, router, breakers, switch, counting_upstream
    ):
        switch.engage(ks.SCOPE_ALL_PAID, actor="oncall", reason="runaway agent")
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == ks.KILL_SWITCH_ERROR_CODE
        assert r.json()["detail"]["kill_switch_scope"] == ks.SCOPE_ALL_PAID
        assert r.json()["detail"]["retryable"] is False
        assert counting_upstream.calls == []

    def test_all_paid_leaves_free_tiers_serving(
        self, client, router, breakers, switch, counting_upstream
    ):
        """The platform stays up; the meter stops."""
        local = _register_local_tier(router)
        switch.engage(ks.SCOPE_ALL_PAID)
        r = client.post("/v1/chat/completions", json={
            "model": local, "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200
        assert counting_upstream.calls == [local]

    def test_all_paid_blocks_the_native_messages_path(
        self, client, router, breakers, switch, monkeypatch
    ):
        created = []

        def fake_client(cfg):
            class _Messages:
                async def create(self, **kwargs):
                    created.append(kwargs)
                    raise AssertionError("upstream must not be reached")

            return type("C", (), {"messages": _Messages()})()

        monkeypatch.setattr(router, "_make_anthropic_client", fake_client)
        switch.engage(ks.SCOPE_ALL_PAID)

        r = client.post("/v1/messages", json={
            "model": "claude", "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 503
        body = r.json()
        # Anthropic-shaped envelope so an anthropic_messages transport can
        # parse it like any other upstream error.
        assert body["type"] == "error"
        assert body["error"]["detail"]["code"] == ks.KILL_SWITCH_ERROR_CODE
        assert created == []


class TestKillSwitchRuntimeFlip:
    def test_engage_then_release_over_http(
        self, client, router, breakers, switch, counting_upstream
    ):
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 200

        r = client.post("/debug/kill-switch", json={
            "scope": ks.SCOPE_ALL_PAID, "action": "engage", "reason": "spend spike",
            "actor": "oncall",
        })
        assert r.status_code == 200
        assert r.json()["kill_switch"]["engaged"] == [ks.SCOPE_ALL_PAID]

        blocked = client.post("/v1/chat/completions", json=_chat_body())
        assert blocked.status_code == 503

        r = client.post("/debug/kill-switch", json={
            "scope": ks.SCOPE_ALL_PAID, "action": "release", "reason": "resolved",
        })
        assert r.status_code == 200
        assert r.json()["kill_switch"]["engaged"] == []
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 200

    def test_state_endpoint_reports_scopes_and_counts(
        self, client, router, breakers, switch, counting_upstream
    ):
        switch.engage(ks.SCOPE_ALL_PAID)
        client.post("/v1/chat/completions", json=_chat_body())
        r = client.get("/debug/kill-switch")
        assert r.status_code == 200
        # One count per refused dispatch: the primary and its fallback were
        # both evaluated and both refused.
        assert r.json()["kill_switch"]["blocked_counts"][ks.SCOPE_ALL_PAID] == 2
        assert set(r.json()["scopes_available"]) == set(ks.ALL_SCOPES)

    @pytest.mark.parametrize("payload", [
        {"scope": "everything", "action": "engage"},
        {"scope": "all_paid", "action": "detonate"},
        {"action": "engage"},
        {},
    ])
    def test_bad_payloads_are_client_errors(self, client, switch, payload):
        assert client.post("/debug/kill-switch", json=payload).status_code == 400

    def test_admin_routes_require_auth(self, unauth_client):
        assert unauth_client.get("/debug/kill-switch").status_code == 401
        assert unauth_client.get("/debug/circuit-breakers").status_code == 401
        assert unauth_client.post("/debug/kill-switch", json={}).status_code == 401

    def test_admin_key_separates_calling_from_disarming(
        self, client, router, switch, monkeypatch
    ):
        """Every in-mesh agent holds ROUTER_API_KEY. When ROUTER_ADMIN_API_KEY
        is set, holding it is no longer enough to turn spend controls off."""
        monkeypatch.setattr(router, "_ROUTER_ADMIN_API_KEY", "test-admin-key")
        payload = {"scope": ks.SCOPE_ALL_PAID, "action": "engage"}

        assert client.post("/debug/kill-switch", json=payload).status_code == 403
        assert client.post(
            "/debug/kill-switch", json=payload, headers={"X-Router-Admin-Key": "wrong"},
        ).status_code == 403
        assert client.post(
            "/debug/kill-switch", json=payload,
            headers={"X-Router-Admin-Key": "test-admin-key"},
        ).status_code == 200
        # Reads stay on the ordinary router credential.
        assert client.get("/debug/kill-switch").status_code == 200


# ── Operator surface ──────────────────────────────────────────────────────────

class TestOperatorSurface:
    def test_health_reports_breaker_state_and_kill_switch(
        self, client, router, breakers, switch
    ):
        for _ in range(2):
            breakers.record_failure(
                router._breaker_key("gpt4o-mini"), reason=cb.TRIP_AUTH_FAILURE
            )
        switch.engage(ks.SCOPE_PAID_FALLBACK)

        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["circuit_breakers"]["by_tier"]["gpt4o-mini"] == cb.STATE_OPEN
        assert body["circuit_breakers"]["open_keys"] == 1
        assert body["kill_switch"]["scopes"][ks.SCOPE_PAID_FALLBACK] is True

    def test_health_does_not_leak_credentials_or_hostnames(
        self, client, router, breakers
    ):
        breakers.record_failure(
            router._breaker_key("gpt4o-mini"), reason=cb.TRIP_AUTH_FAILURE
        )
        raw = client.get("/health").text
        assert "test-key" not in raw
        assert "localhost:8888" not in raw
        # Per-key trip history is auth-gated, not on the liveness probe.
        assert "by_key" not in raw

    def test_health_tier_block_is_unchanged(self, client):
        """The pre-existing aaf-0016 contract: /health's per-tier block carries
        an over_budget boolean and nothing else."""
        body = client.get("/health").json()
        assert set(body["tiers"]["gpt4o-mini"]) == {"over_budget"}

    def test_debug_breakers_reports_per_key_detail(self, client, router, breakers):
        key = router._breaker_key("gpt4o-mini")
        for _ in range(2):
            breakers.record_failure(key, reason=cb.TRIP_QUOTA_EXHAUSTED)
        body = client.get("/debug/circuit-breakers").json()
        assert body["circuit_breakers"]["by_key"][key]["state"] == cb.STATE_OPEN
        assert body["circuit_breakers"]["by_key"][key]["last_trip_reason"] == \
            cb.TRIP_QUOTA_EXHAUSTED

    def test_reset_closes_breakers_without_waiting_out_the_cooldown(
        self, client, router, breakers, counting_upstream
    ):
        key = router._breaker_key("gpt4o-mini")
        for _ in range(2):
            breakers.record_failure(key, reason=cb.TRIP_AUTH_FAILURE)
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 503

        r = client.post("/debug/circuit-breakers/reset", json={})
        assert r.status_code == 200
        assert r.json()["cleared"] >= 1
        assert client.post("/v1/chat/completions", json=_chat_body()).status_code == 200

    def test_reset_requires_the_admin_key_when_configured(
        self, client, router, breakers, monkeypatch
    ):
        monkeypatch.setattr(router, "_ROUTER_ADMIN_API_KEY", "test-admin-key")
        assert client.post("/debug/circuit-breakers/reset", json={}).status_code == 403


# ── Flight recorder integration ───────────────────────────────────────────────

class TestGovernanceEventsAreRecorded:
    def test_breaker_trip_is_recorded(
        self, client, router, breakers, recorder, counting_upstream
    ):
        counting_upstream.raises = Upstream401("invalid credential")
        for _ in range(2):
            client.post("/v1/chat/completions", json=_chat_body())

        transitions = [
            e for e in recorder.recent(limit=50)
            if e.get("event_type") == "breaker_transition"
        ]
        assert transitions, "a breaker trip must land in the flight trace"
        trip = transitions[-1]
        assert trip["breaker_to_state"] == cb.STATE_OPEN
        assert trip["breaker_reason"] == cb.TRIP_AUTH_FAILURE
        assert trip["outcome"] == fr.OUTCOME_ERROR

    def test_kill_switch_engage_and_release_are_recorded(
        self, client, router, breakers, switch, recorder, counting_upstream
    ):
        client.post("/debug/kill-switch", json={
            "scope": ks.SCOPE_PAID_FALLBACK, "action": "engage", "reason": "spend spike",
            "actor": "oncall",
        })
        client.post("/debug/kill-switch", json={
            "scope": ks.SCOPE_PAID_FALLBACK, "action": "release", "reason": "resolved",
        })
        events = [
            e for e in recorder.recent(limit=50)
            if e.get("endpoint") == "kill_switch"
        ]
        kinds = {e["event_type"] for e in events}
        assert kinds == {ks.EVENT_ENGAGED, ks.EVENT_RELEASED}
        engaged = next(e for e in events if e["event_type"] == ks.EVENT_ENGAGED)
        assert engaged["kill_switch_actor"] == "oncall"
        assert engaged["kill_switch_reason"] == "spend spike"

    def test_a_refused_request_is_recorded_as_a_resilience_block(
        self, client, router, breakers, switch, recorder, counting_upstream
    ):
        switch.engage(ks.SCOPE_ALL_PAID)
        client.post("/v1/chat/completions", json=_chat_body())
        blocks = [
            e for e in recorder.recent(limit=50)
            if (e.get("error_class") or "").startswith("resilience_block:")
        ]
        assert blocks
        assert blocks[0]["error_class"] == f"resilience_block:{ks.KILL_SWITCH_ERROR_CODE}"
