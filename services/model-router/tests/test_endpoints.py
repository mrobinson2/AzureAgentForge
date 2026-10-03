"""Endpoint-level tests over the FastAPI app via TestClient. Upstream model
calls are monkeypatched, so nothing leaves the process. Covers the read-only
info routes plus the two completion routes' happy paths and error mapping."""

import time

import pytest

from tests.test_tenant_identity import issue_token


class _BudgetClientStub:
    def __init__(self, *, reserve_error=None, settle_error=None):
        self.calls = []
        self.reserve_error = reserve_error
        self.settle_error = settle_error

    async def reserve(self, **kwargs):
        self.calls.append(("reserve", kwargs))
        if self.reserve_error is not None:
            raise self.reserve_error
        return type("Decision", (), {"decision": "allow", "status": "pending"})()

    async def settle(self, **kwargs):
        self.calls.append(("settle", kwargs))
        if self.settle_error is not None:
            raise self.settle_error
        return type("Decision", (), {"decision": "allow", "status": "settled"})()

    async def release(self, **kwargs):
        self.calls.append(("release", kwargs))
        return type("Decision", (), {"decision": "allow", "status": "released"})()


def _tenant_headers(**extra):
    clock = int(time.time())
    headers = {
        "x-tenant-token": issue_token(
            issued_at=clock - 1,
            expires_at=clock + 299,
        )
    }
    headers.update(extra)
    return headers


# ── Info routes ──────────────────────────────────────────────────────────────

class TestInfoRoutes:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        # aaf-0016: /health reports only a per-tier over_budget boolean — no dollar
        # spend or configured budget figures leak from this unauthenticated probe.
        assert "gpt4o-mini" in body["tiers"]
        assert "phi4" in body["tiers"]
        assert set(body["tiers"]["gpt4o-mini"]) == {"over_budget"}
        assert "budgets" not in body

    def test_list_models(self, client):
        r = client.get("/v1/models")
        assert r.status_code == 200
        ids = {m["id"] for m in r.json()["data"]}
        assert {"gpt4o-mini", "phi4"} <= ids

    def test_version(self, client):
        r = client.get("/version")
        assert r.status_code == 200
        assert "version" in r.json()

    def test_get_known_model(self, client):
        r = client.get("/v1/models/gpt4o-mini")
        assert r.status_code == 200
        assert r.json()["id"] == "gpt4o-mini"

    def test_get_unknown_model_404(self, client):
        r = client.get("/v1/models/does-not-exist")
        assert r.status_code == 404


# ── /v1/chat/completions ─────────────────────────────────────────────────────

class TestChatCompletions:
    def test_happy_path_injects_router_metadata(self, client, router, monkeypatch):
        async def fake_call(tier, body):
            return {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["_router"]["tier"] == "gpt4o-mini"
        assert body["choices"][0]["message"]["content"] == "hello"

    def test_empty_messages_400(self, client):
        r = client.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []})
        assert r.status_code == 400

    def test_context_overflow_413(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_fits_model", lambda *a, **k: False)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 413

    def test_all_tiers_fail_502(self, client, router, monkeypatch):
        async def boom(tier, body):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(router, "_call_model", boom)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 502

    def test_durable_tenant_budget_reserves_then_settles_actual_cost(
        self, client, router, monkeypatch
    ):
        calls = []
        provider_callers = []

        class BudgetClient:
            async def reserve(self, **kwargs):
                calls.append(("reserve", kwargs))
                return type("Decision", (), {"decision": "allow", "status": "pending"})()

            async def settle(self, **kwargs):
                calls.append(("settle", kwargs))
                return type("Decision", (), {"decision": "allow", "status": "settled"})()

            async def release(self, **kwargs):
                calls.append(("release", kwargs))

        async def fake_call(tier, body):
            provider_callers.append(body.get("__router_caller__"))
            return {
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "__router_cost_usd__": 0.07,
            }

        monkeypatch.setattr(router, "_tenant_budget_client", BudgetClient())
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(
            router, "uuid4", lambda: type("UUID", (), {"hex": "budget-reservation-1"})()
        )
        monkeypatch.setattr(router, "_call_model", fake_call)

        response = client.post(
            "/v1/chat/completions",
            headers=_tenant_headers(
                **{
                    "x-tenant-id": "00000000-0000-0000-0000-000000000099",
                    "x-agent-id": "spoofed-agent",
                    "x-request-id": "router-request-1",
                }
            ),
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 200
        assert provider_callers == ["agent-7"]
        assert calls == [
            (
                "reserve",
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "caller_id": "agent-7",
                    "request_id": "budget-reservation-1",
                    "reserved_usd": "0.25",
                    "day": router._budget_date,
                },
            ),
            (
                "settle",
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "request_id": "budget-reservation-1",
                    "actual_usd": "0.07",
                },
            ),
        ]

    def test_durable_tenant_budget_block_stops_provider_dispatch(
        self, client, router, monkeypatch
    ):
        provider_called = False

        async def fake_call(tier, body):
            nonlocal provider_called
            provider_called = True
            return {"choices": []}

        budget = _BudgetClientStub(
            reserve_error=router.TenantBudgetBlocked("tenant daily budget exceeded")
        )
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(router, "_call_model", fake_call)

        response = client.post(
            "/v1/chat/completions",
            headers=_tenant_headers(),
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 429
        assert provider_called is False
        assert [name for name, _ in budget.calls] == ["reserve"]

    def test_durable_tenant_budget_releases_hold_when_all_providers_fail(
        self, client, router, monkeypatch
    ):
        async def boom(tier, body):
            raise RuntimeError("upstream down")

        budget = _BudgetClientStub()
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(
            router, "uuid4", lambda: type("UUID", (), {"hex": "failed-budget-hold"})()
        )
        monkeypatch.setattr(router, "_call_model", boom)

        response = client.post(
            "/v1/chat/completions",
            headers=_tenant_headers(**{"x-request-id": "failed-provider-request"}),
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 502
        assert [name for name, _ in budget.calls] == ["reserve", "release"]
        assert budget.calls[-1][1] == {
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "request_id": "failed-budget-hold",
        }

    def test_durable_tenant_budget_keeps_hold_when_settlement_is_unavailable(
        self, client, router, monkeypatch
    ):
        async def fake_call(tier, body):
            return {"choices": [], "__router_cost_usd__": 0.07}

        budget = _BudgetClientStub(
            settle_error=router.TenantBudgetUnavailable("authority unavailable")
        )
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(router, "_call_model", fake_call)

        response = client.post(
            "/v1/chat/completions",
            headers=_tenant_headers(),
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 200
        assert response.headers["X-Tenant-Budget-Settlement"] == "pending"
        assert [name for name, _ in budget.calls] == ["reserve", "settle"]

    @pytest.mark.parametrize(
        ("headers", "payload", "expected_status", "expected_detail"),
        [
            (
                {"x-tenant-id": "00000000-0000-0000-0000-000000000001"},
                {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
                401,
                "signed tenant principal",
            ),
            (
                {"x-tenant-token": "invalid.token.value"},
                {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
                403,
                "Invalid tenant principal",
            ),
            (
                _tenant_headers(),
                {
                    "model": "gpt-4o-mini",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                400,
                "Streaming is not available",
            ),
        ],
    )
    def test_durable_tenant_budget_rejects_unsafe_request_shapes(
        self, client, router, monkeypatch, headers, payload, expected_status, expected_detail
    ):
        budget = _BudgetClientStub()
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")

        response = client.post("/v1/chat/completions", headers=headers, json=payload)

        assert response.status_code == expected_status
        assert expected_detail in response.json()["detail"]
        assert budget.calls == []

    @pytest.mark.parametrize("reservation_amount", ["0", "-1", "nan", "inf", "not-money"])
    def test_durable_tenant_budget_rejects_non_positive_or_non_finite_reservation(
        self, client, router, monkeypatch, reservation_amount
    ):
        budget = _BudgetClientStub()

        async def provider_must_not_run(tier, body):
            raise AssertionError("invalid reservation config must stop before dispatch")

        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", reservation_amount)
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(router, "_call_model", provider_must_not_run)

        response = client.post(
            "/v1/chat/completions",
            headers=_tenant_headers(),
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 503
        assert "reservation amount" in response.json()["detail"]
        assert budget.calls == []


# ── /v1/messages ─────────────────────────────────────────────────────────────

class _FakeAnthropicResp:
    def model_dump(self, **_kw):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        }


def _install_claude_tier(router):
    router.MODELS["claude"] = {
        "litellm_model": "anthropic/claude-sonnet-4-6",
        "api_base": "https://foundry.example/anthropic",
        "api_key": "k",
        "daily_budget": 0.25,
        "max_tokens": 4096,
        "context_limit": 128000,
        "timeout_seconds": 60,
        "supports_tools": True,
    }


class TestMessagesEndpoint:
    def test_happy_path(self, client, router, monkeypatch):
        _install_claude_tier(router)

        class FakeMessages:
            async def create(self, **kwargs):
                return _FakeAnthropicResp()

        class FakeClient:
            messages = FakeMessages()

        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: FakeClient())
        r = client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["_router"]["tier"] == "claude"
        assert body["content"][0]["text"] == "hi"

    def test_durable_tenant_budget_reserves_and_settles_messages_cost(
        self, client, router, monkeypatch
    ):
        _install_claude_tier(router)
        attributed_callers = []

        class FakeMessages:
            async def create(self, **kwargs):
                return _FakeAnthropicResp()

        class FakeClient:
            messages = FakeMessages()

        budget = _BudgetClientStub()
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(
            router, "uuid4", lambda: type("UUID", (), {"hex": "messages-hold-1"})()
        )
        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: FakeClient())
        monkeypatch.setattr(router, "_estimate_anthropic_cost", lambda *args: 0.07)
        monkeypatch.setattr(
            router,
            "_record_anthropic_usage",
            lambda *args, **kwargs: attributed_callers.append(kwargs.get("caller")),
        )

        response = client.post(
            "/v1/messages",
            headers=_tenant_headers(
                **{
                    "x-tenant-id": "00000000-0000-0000-0000-000000000099",
                    "x-agent-id": "spoofed-agent",
                }
            ),
            json={
                "model": "claude",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert attributed_callers == ["agent-7"]
        assert budget.calls == [
            (
                "reserve",
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "caller_id": "agent-7",
                    "request_id": "messages-hold-1",
                    "reserved_usd": "0.25",
                    "day": router._budget_date,
                },
            ),
            (
                "settle",
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "request_id": "messages-hold-1",
                    "actual_usd": "0.07",
                },
            ),
        ]

    def test_durable_tenant_budget_block_is_anthropic_shaped_and_stops_messages_dispatch(
        self, client, router, monkeypatch
    ):
        _install_claude_tier(router)
        provider_called = False

        class FakeMessages:
            async def create(self, **kwargs):
                nonlocal provider_called
                provider_called = True
                return _FakeAnthropicResp()

        class FakeClient:
            messages = FakeMessages()

        budget = _BudgetClientStub(
            reserve_error=router.TenantBudgetBlocked("tenant daily budget exceeded")
        )
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: FakeClient())

        response = client.post(
            "/v1/messages",
            headers=_tenant_headers(),
            json={
                "model": "claude",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 429
        assert response.json()["type"] == "error"
        assert response.json()["error"]["type"] == "rate_limit_error"
        assert provider_called is False
        assert [name for name, _ in budget.calls] == ["reserve"]

    def test_durable_tenant_budget_releases_messages_hold_on_provider_failure(
        self, client, router, monkeypatch
    ):
        _install_claude_tier(router)

        class FakeMessages:
            async def create(self, **kwargs):
                raise RuntimeError("provider down")

        class FakeClient:
            messages = FakeMessages()

        budget = _BudgetClientStub()
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(
            router, "uuid4", lambda: type("UUID", (), {"hex": "messages-failed-hold"})()
        )
        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: FakeClient())

        response = client.post(
            "/v1/messages",
            headers=_tenant_headers(),
            json={
                "model": "claude",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 502
        assert [name for name, _ in budget.calls] == ["reserve", "release"]
        assert budget.calls[-1][1]["request_id"] == "messages-failed-hold"

    def test_durable_tenant_budget_keeps_messages_hold_when_settlement_is_unavailable(
        self, client, router, monkeypatch
    ):
        _install_claude_tier(router)

        class FakeMessages:
            async def create(self, **kwargs):
                return _FakeAnthropicResp()

        class FakeClient:
            messages = FakeMessages()

        budget = _BudgetClientStub(
            settle_error=router.TenantBudgetUnavailable("authority unavailable")
        )
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")
        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: FakeClient())

        response = client.post(
            "/v1/messages",
            headers=_tenant_headers(),
            json={
                "model": "claude",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.headers["X-Tenant-Budget-Settlement"] == "pending"
        assert [name for name, _ in budget.calls] == ["reserve", "settle"]

    def test_durable_tenant_budget_rejects_messages_stream_before_reserving(
        self, client, router, monkeypatch
    ):
        _install_claude_tier(router)
        budget = _BudgetClientStub()
        monkeypatch.setattr(router, "_tenant_budget_client", budget)
        monkeypatch.setattr(router, "_TENANT_BUDGET_RESERVATION_USD", "0.25")
        monkeypatch.setattr(router, "_TENANT_BUDGET_TOKEN_SECRET", "tenant-secret")

        response = client.post(
            "/v1/messages",
            headers=_tenant_headers(),
            json={
                "model": "claude",
                "stream": True,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 400
        assert response.json()["type"] == "error"
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert budget.calls == []

    def test_missing_max_tokens_400(self, client, router):
        _install_claude_tier(router)
        r = client.post(
            "/v1/messages",
            json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 400

    def test_non_anthropic_model_400(self, client, router):
        _install_claude_tier(router)
        r = client.post(
            "/v1/messages",
            json={"model": "gpt-4o-mini", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 400

    def test_upstream_error_surfaces_status(self, client, router, monkeypatch):
        _install_claude_tier(router)

        class Boom(Exception):
            status_code = 503

        class FakeMessages:
            async def create(self, **kwargs):
                raise Boom("rate limited")

        class FakeClient:
            messages = FakeMessages()

        monkeypatch.setattr(router, "_make_anthropic_client", lambda cfg: FakeClient())
        r = client.post(
            "/v1/messages",
            json={"model": "claude", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 503
        assert r.json()["type"] == "error"
