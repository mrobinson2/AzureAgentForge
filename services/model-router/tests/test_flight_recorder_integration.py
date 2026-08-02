"""Endpoint-level wiring tests for the Flight Recorder + Waste Breakers pack.

Covers: /v1/chat/completions and /v1/messages emitting flight events on
success, fallback ("downgraded"), and total-failure ("error") paths;
waste-breaker enforcement (observe vs block); the /debug/flight-recorder
read surface; and that FLIGHT_RECORDER_ENABLED=false is a true zero-overhead
no-op (nothing recorded, debug endpoint 404s, requests still succeed).

Follows this suite's established pattern (see test_budget_enforcement.py,
test_endpoints.py): upstream model calls are monkeypatched so nothing
leaves the process, and mutable module globals are monkeypatched per test
(auto-restored by pytest's monkeypatch fixture)."""

import pytest

import flight_recorder as fr
import waste_breakers as wb


def _chat_body(model="gpt4o-mini", content="hi"):
    return {"model": model, "messages": [{"role": "user", "content": content}]}


def _messages_body(model="claude", content="hi"):
    return {
        "model": model, "max_tokens": 5,
        "messages": [{"role": "user", "content": content}],
    }


class _FakeUsage:
    input_tokens = 3
    output_tokens = 1


class _FakeAnthropicResp:
    usage = _FakeUsage()

    def model_dump(self, **_kw):
        return {
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude", "content": [{"type": "text", "text": "pong"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }


def _fake_anthropic_client():
    class _Messages:
        async def create(self, **kwargs):
            return _FakeAnthropicResp()

    class _Client:
        messages = _Messages()

    return _Client()


@pytest.fixture()
def fresh_recorder(router, monkeypatch):
    """A clean, small FlightRecorder wired into the router for one test,
    with waste breakers pointed at generous (never-trip) thresholds unless
    a test overrides them."""
    rec = fr.FlightRecorder(max_events=50, redact=True)
    monkeypatch.setattr(router, "_flight_recorder", rec)
    monkeypatch.setattr(router, "FLIGHT_RECORDER_ENABLED", True)
    monkeypatch.setattr(router, "WASTE_BREAKERS_ENABLED", True)
    monkeypatch.setattr(router, "_WASTE_BREAKER_ENFORCE_MODE", wb.MODE_OBSERVE)
    monkeypatch.setattr(router, "_WASTE_BREAKER_THRESHOLDS", wb.BreakerThresholds())
    return rec


# ── /v1/chat/completions ─────────────────────────────────────────────────────

class TestChatCompletionsRecording:
    def test_success_records_event(self, client, fresh_recorder, router, monkeypatch):
        async def fake_call(tier, body):
            return {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                "__router_cost_usd__": 0.0021,
            }

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200

        events = fresh_recorder.recent(limit=1)
        assert len(events) == 1
        event = events[0]
        assert event["endpoint"] == "chat_completions"
        assert event["outcome"] == fr.OUTCOME_SUCCESS
        assert event["served_tier"] == "gpt4o-mini"
        assert event["input_tokens"] == 10
        assert event["output_tokens"] == 4
        assert event["total_tokens"] == 14
        assert event["cost_usd"] == pytest.approx(0.0021)
        assert event["redacted"] is True
        assert "prompt_excerpt" not in event
        assert event["prompt_fingerprint"] == fr.prompt_fingerprint(_chat_body()["messages"])

    def test_fallback_records_downgraded_outcome(self, client, fresh_recorder, router, monkeypatch):
        async def fail_primary(tier, body):
            if tier == "gpt4o-mini":
                raise RuntimeError("primary down")
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fail_primary)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200

        event = fresh_recorder.recent(limit=1)[0]
        assert event["outcome"] == fr.OUTCOME_DOWNGRADED
        assert event["served_tier"] != "gpt4o-mini"

    def test_all_tiers_failed_records_error(self, client, fresh_recorder, router, monkeypatch):
        async def always_fail(tier, body):
            raise RuntimeError("nope")

        monkeypatch.setattr(router, "_call_model", always_fail)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 502

        event = fresh_recorder.recent(limit=1)[0]
        assert event["outcome"] == fr.OUTCOME_ERROR
        assert event["error_class"] == "all_tiers_failed"

    def test_streaming_open_records_event(self, client, fresh_recorder, router, monkeypatch):
        async def fake_open_stream(tier, body):
            async def gen():
                return
                yield  # pragma: no cover — never reached, makes this an async generator

            return gen()

        monkeypatch.setattr(router, "_open_stream", fake_open_stream)
        r = client.post("/v1/chat/completions", json={**_chat_body(), "stream": True})
        assert r.status_code == 200

        event = fresh_recorder.recent(limit=1)[0]
        assert event["streamed"] is True
        assert event["outcome"] == fr.OUTCOME_SUCCESS

    def test_caller_header_attributed_on_event(self, client, fresh_recorder, router, monkeypatch):
        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "hi"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post(
            "/v1/chat/completions", json=_chat_body(), headers={"x-agent-id": "agent-1"},
        )
        assert r.status_code == 200
        assert fresh_recorder.recent(limit=1)[0]["caller"] == "agent-1"


# ── /v1/messages ──────────────────────────────────────────────────────────────

class TestMessagesRecording:
    def test_success_records_event(self, client, fresh_recorder, router, monkeypatch):
        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: _fake_anthropic_client())
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 200

        event = fresh_recorder.recent(limit=1)[0]
        assert event["endpoint"] == "messages"
        assert event["outcome"] == fr.OUTCOME_SUCCESS
        assert event["input_tokens"] == 3
        assert event["output_tokens"] == 1

    def test_upstream_failure_records_error(self, client, fresh_recorder, router, monkeypatch):
        class _BoomMessages:
            async def create(self, **kwargs):
                raise RuntimeError("upstream boom")

        class _BoomClient:
            messages = _BoomMessages()

        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: _BoomClient())
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 502

        event = fresh_recorder.recent(limit=1)[0]
        assert event["outcome"] == fr.OUTCOME_ERROR
        assert event["error_class"] == "RuntimeError"


# ── Waste breaker enforcement ───────────────────────────────────────────────

class TestWasteBreakerEnforcement:
    def test_observe_mode_never_blocks(self, client, fresh_recorder, router, monkeypatch):
        monkeypatch.setattr(
            router, "_WASTE_BREAKER_THRESHOLDS",
            wb.BreakerThresholds(oversized_prompt_tokens=1),  # trivially "tripped"
        )
        # Mode stays MODE_OBSERVE (fresh_recorder fixture default).
        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200

        event = fresh_recorder.recent(limit=1)[0]
        breaker = next(
            v for v in event["breaker_verdicts"] if v["breaker"] == wb.OVERSIZED_PROMPT
        )
        assert breaker["tripped"] is True

    def test_block_mode_refuses_before_spend(self, client, fresh_recorder, router, monkeypatch):
        monkeypatch.setattr(router, "_WASTE_BREAKER_ENFORCE_MODE", wb.MODE_BLOCK)
        monkeypatch.setattr(
            router, "_WASTE_BREAKER_THRESHOLDS",
            wb.BreakerThresholds(oversized_prompt_tokens=1),
        )

        async def must_not_be_called(tier, body):  # pragma: no cover
            raise AssertionError("blocked request must not reach the model")

        monkeypatch.setattr(router, "_call_model", must_not_be_called)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 429
        detail = r.json()["detail"]
        assert detail["error"] == "waste_breaker_tripped"
        assert detail["breaker"] == wb.OVERSIZED_PROMPT

        event = fresh_recorder.recent(limit=1)[0]
        assert event["outcome"] == fr.OUTCOME_ERROR
        assert event["error_class"] == f"waste_breaker:{wb.OVERSIZED_PROMPT}"

    def test_retry_storm_trips_after_repeated_calls(self, client, fresh_recorder, router, monkeypatch):
        monkeypatch.setattr(router, "_WASTE_BREAKER_ENFORCE_MODE", wb.MODE_BLOCK)
        monkeypatch.setattr(
            router, "_WASTE_BREAKER_THRESHOLDS",
            wb.BreakerThresholds(retry_storm_calls=3, retry_storm_window_seconds=3600),
        )

        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        headers = {"x-agent-id": "loopy-agent"}
        # First 3 calls (below/at the pre-check point) succeed; the window
        # check happens BEFORE this call is recorded, so it takes
        # threshold+1 calls to see a 429.
        for _ in range(3):
            r = client.post(
                "/v1/chat/completions", json=_chat_body(content="varying " + str(_)),
                headers=headers,
            )
            assert r.status_code == 200
        r = client.post("/v1/chat/completions", json=_chat_body(content="one more"), headers=headers)
        assert r.status_code == 429
        assert r.json()["detail"]["breaker"] == wb.RETRY_STORM

    def test_repeated_identical_calls_trips(self, client, fresh_recorder, router, monkeypatch):
        monkeypatch.setattr(router, "_WASTE_BREAKER_ENFORCE_MODE", wb.MODE_BLOCK)
        monkeypatch.setattr(
            router, "_WASTE_BREAKER_THRESHOLDS",
            wb.BreakerThresholds(
                repeated_identical_calls=2, repeated_identical_window_seconds=3600,
                retry_storm_calls=1000,
            ),
        )

        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        headers = {"x-agent-id": "repeat-agent"}
        same_body = _chat_body(content="same every time")
        for _ in range(2):
            r = client.post("/v1/chat/completions", json=same_body, headers=headers)
            assert r.status_code == 200
        r = client.post("/v1/chat/completions", json=same_body, headers=headers)
        assert r.status_code == 429
        assert r.json()["detail"]["breaker"] == wb.REPEATED_IDENTICAL_CALLS


# ── /debug/flight-recorder ──────────────────────────────────────────────────

class TestDebugEndpoint:
    def test_requires_auth(self, unauth_client, fresh_recorder):
        r = unauth_client.get("/debug/flight-recorder")
        assert r.status_code == 401

    def test_returns_recent_events_and_config(self, client, fresh_recorder):
        fresh_recorder.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        r = client.get("/debug/flight-recorder")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["stats"]["buffer_size"] == 1
        assert "waste_breakers" in body
        assert len(body["recent"]) == 1

    def test_filters_by_caller_query_param(self, client, fresh_recorder):
        fresh_recorder.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        fresh_recorder.record(caller="b", outcome=fr.OUTCOME_SUCCESS)
        r = client.get("/debug/flight-recorder", params={"caller": "a"})
        assert r.status_code == 200
        assert len(r.json()["recent"]) == 1
        assert r.json()["recent"][0]["caller"] == "a"

    def test_get_single_event(self, client, fresh_recorder):
        event_id = fresh_recorder.record(caller="a", outcome=fr.OUTCOME_SUCCESS)
        r = client.get(f"/debug/flight-recorder/{event_id}")
        assert r.status_code == 200
        assert r.json()["event_id"] == event_id

    def test_unknown_event_id_404s(self, client, fresh_recorder):
        r = client.get("/debug/flight-recorder/does-not-exist")
        assert r.status_code == 404

    def test_disabled_recorder_404s(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_flight_recorder", None)
        r = client.get("/debug/flight-recorder")
        assert r.status_code == 404


# ── FLIGHT_RECORDER_ENABLED=false: zero-overhead no-op ──────────────────────

class TestDisabledIsNoOp:
    def test_requests_still_succeed_with_recorder_off(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_flight_recorder", None)
        monkeypatch.setattr(router, "FLIGHT_RECORDER_ENABLED", False)

        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200

    def test_waste_breakers_never_block_when_recorder_off(self, client, router, monkeypatch):
        # Even with aggressive thresholds, no history exists to trip
        # history-based breakers when the recorder is off — only
        # oversized_prompt (which needs no history) still evaluates.
        monkeypatch.setattr(router, "_flight_recorder", None)
        monkeypatch.setattr(router, "_WASTE_BREAKER_ENFORCE_MODE", wb.MODE_BLOCK)
        monkeypatch.setattr(
            router, "_WASTE_BREAKER_THRESHOLDS",
            wb.BreakerThresholds(retry_storm_calls=1, repeated_identical_calls=1),
        )

        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200
