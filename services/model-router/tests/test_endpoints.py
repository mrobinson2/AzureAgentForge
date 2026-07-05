"""Endpoint-level tests over the FastAPI app via TestClient. Upstream model
calls are monkeypatched, so nothing leaves the process. Covers the read-only
info routes plus the two completion routes' happy paths and error mapping."""

import pytest


# ── Info routes ──────────────────────────────────────────────────────────────

class TestInfoRoutes:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        # Budgets reported per registered tier.
        assert "gpt4o-mini" in body["budgets"]
        assert "phi4" in body["budgets"]
        assert set(body["budgets"]["gpt4o-mini"]) == {"spent", "limit", "over_budget"}

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
