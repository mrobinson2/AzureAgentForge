"""Acting budget enforcement (A6): BUDGET_ENFORCE_MODE warn|downgrade|block.

Per-tier daily budgets historically only WARNED. A6 adds an acting control
behind BUDGET_ENFORCE_MODE:

  warn (default) — pre-A6 behavior exactly (ship-dark safe): serve + WARN log.
  downgrade      — serve BUDGET_FALLBACK_TIER; mark the response with the
                   X-Router-Budget-Downgrade header (+ _router metadata).
  block          — 429 with a machine-readable budget_exceeded body.

Decision logic lives in budget_enforcement.py (stdlib-only, vendored from the
upstream private deployment). Wiring covers select_tier — the OpenAI-compat
/v1/chat/completions path (which dispatches Anthropic tiers to the direct-SDK
bypass via _call_model) — PLUS explicit checks on the two paths that bypass
select_tier: the native /v1/messages Anthropic path (which must stay
Anthropic-shaped, so a downgrade only serves an Anthropic-backed fallback and
otherwise degrades to warn) and /v1/embeddings (dedicated "embeddings" ledger
bucket; no same-vector-space downgrade target, so downgrade degrades to warn
there too).

The mode is read from env at import time (unset in the canonical test env →
warn); mode-specific tests monkeypatch main._BUDGET_ENFORCE_MODE, following
the suite's existing monkeypatch-the-module-global pattern.
"""

from datetime import date

import pytest

import budget_enforcement as be


def _pin_today(router):
    """Pin the budget day to today so the lazy rollover doesn't clear
    directly-injected spend mid-test."""
    router._budget_date = str(date.today())


def _make_over_budget(router, tier):
    """Blow `tier`'s daily budget in the ledger."""
    _pin_today(router)
    router._spend[tier] = router.MODELS[tier]["daily_budget"] + 1.0


def _chat_body(model="claude"):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


def _messages_body(model="claude"):
    return {"model": model, "max_tokens": 5,
            "messages": [{"role": "user", "content": "hi"}]}


# ── Pure module: resolve_mode ────────────────────────────────────────────────

class TestResolveMode:
    def test_none_defaults_to_warn(self):
        assert be.resolve_mode(None) == be.MODE_WARN

    def test_empty_defaults_to_warn(self):
        assert be.resolve_mode("") == be.MODE_WARN

    def test_normalizes_case_and_whitespace(self):
        assert be.resolve_mode("  DOWNGRADE ") == be.MODE_DOWNGRADE
        assert be.resolve_mode("Block") == be.MODE_BLOCK

    def test_invalid_value_fails_open_to_warn(self):
        # A config typo must not brick the router into an unknown mode.
        assert be.resolve_mode("explode") == be.MODE_WARN


# ── Pure module: decide ──────────────────────────────────────────────────────

class TestDecide:
    TIERS = {"gpt4o-mini": {}, "phi4": {}, "claude": {}}

    def _decide(self, **over):
        kwargs = dict(
            tier="claude", over_budget=True, mode=be.MODE_WARN,
            fallback_tier="gpt4o-mini", registered_tiers=self.TIERS,
            spent_usd=6.25, limit_usd=0.25,
        )
        kwargs.update(over)
        return be.decide(**kwargs)

    def test_under_budget_allows_any_mode(self):
        for mode in be.VALID_MODES:
            d = self._decide(over_budget=False, mode=mode)
            assert d.action == be.ACTION_ALLOW
            assert d.serve_tier == "claude"

    def test_warn_mode_serves_requested_tier(self):
        d = self._decide(mode=be.MODE_WARN)
        assert d.action == be.ACTION_WARN
        assert d.serve_tier == "claude"
        assert "warn" in d.reason

    def test_downgrade_mode_serves_fallback(self):
        d = self._decide(mode=be.MODE_DOWNGRADE)
        assert d.action == be.ACTION_DOWNGRADE
        assert d.serve_tier == "gpt4o-mini"
        assert d.requested_tier == "claude"

    def test_downgrade_unregistered_fallback_degrades_to_warn(self):
        # Misconfigured fallback must never strand the request.
        d = self._decide(mode=be.MODE_DOWNGRADE, fallback_tier="no-such-tier")
        assert d.action == be.ACTION_WARN
        assert d.serve_tier == "claude"
        assert "unavailable" in d.reason

    def test_downgrade_self_referential_fallback_degrades_to_warn(self):
        # The fallback tier itself is exempt: it's the designated floor.
        d = self._decide(tier="gpt4o-mini", mode=be.MODE_DOWNGRADE,
                         fallback_tier="gpt4o-mini")
        assert d.action == be.ACTION_WARN
        assert d.serve_tier == "gpt4o-mini"

    def test_block_mode_blocks(self):
        d = self._decide(mode=be.MODE_BLOCK)
        assert d.action == be.ACTION_BLOCK
        assert d.serve_tier is None

    def test_block_detail_shape(self):
        d = self._decide(mode=be.MODE_BLOCK)
        detail = be.block_detail(d)
        assert detail["error"] == "budget_exceeded"
        assert detail["tier"] == "claude"
        assert detail["spent_usd"] == pytest.approx(6.25)
        assert detail["limit_usd"] == pytest.approx(0.25)
        assert detail["mode"] == "block"
        assert "budget exhausted" in detail["message"]

    def test_downgrade_header_value(self):
        d = self._decide(mode=be.MODE_DOWNGRADE)
        name, value = be.downgrade_header(d)
        assert name == "X-Router-Budget-Downgrade"
        assert value == "claude->gpt4o-mini"


# ── warn (default): ship-dark — behavior unchanged ───────────────────────────

class TestWarnModeIsShipDark:
    def test_import_default_is_warn(self, router):
        # BUDGET_ENFORCE_MODE is unset in the canonical test env → warn.
        assert router._BUDGET_ENFORCE_MODE == "warn"

    def test_over_budget_tier_still_selected(self, router):
        _make_over_budget(router, "claude")
        body = _chat_body()
        assert router.select_tier(body) == "claude"
        # No downgrade marker → no response header.
        assert "__budget_downgrade__" not in body

    def test_endpoint_serves_over_budget_tier_without_marker(
        self, client, router, monkeypatch
    ):
        _make_over_budget(router, "claude")
        called = []

        async def fake_call(tier, body):
            called.append(tier)
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200
        assert called == ["claude"]
        assert "X-Router-Budget-Downgrade" not in r.headers
        assert "budget_downgraded_from" not in r.json()["_router"]

    def test_existing_gpt4o_to_phi4_redirect_untouched(self, router):
        # The pre-existing base-selector redirect keeps working in warn mode.
        _make_over_budget(router, "gpt4o-mini")
        assert router.select_tier(_chat_body(model="gpt-4o-mini")) == "phi4"


# ── downgrade mode ───────────────────────────────────────────────────────────

class TestDowngradeMode:
    @pytest.fixture()
    def downgrade_router(self, router, monkeypatch):
        monkeypatch.setattr(router, "_BUDGET_ENFORCE_MODE", "downgrade")
        return router

    def test_select_tier_downgrades_and_marks_body(self, downgrade_router):
        _make_over_budget(downgrade_router, "claude")
        body = _chat_body()
        assert downgrade_router.select_tier(body) == "gpt4o-mini"
        assert body["__budget_downgrade__"] == {
            "from": "claude", "to": "gpt4o-mini",
        }

    def test_under_budget_not_downgraded(self, downgrade_router):
        body = _chat_body()
        assert downgrade_router.select_tier(body) == "claude"
        assert "__budget_downgrade__" not in body

    def test_endpoint_serves_fallback_with_header(
        self, client, downgrade_router, monkeypatch
    ):
        _make_over_budget(downgrade_router, "claude")
        called = []

        async def fake_call(tier, body):
            called.append(tier)
            return {"choices": [{"message": {"content": "cheap ok"}}]}

        monkeypatch.setattr(downgrade_router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200
        assert called == ["gpt4o-mini"]
        assert r.headers["X-Router-Budget-Downgrade"] == "claude->gpt4o-mini"
        meta = r.json()["_router"]
        assert meta["tier"] == "gpt4o-mini"
        assert meta["budget_downgraded_from"] == "claude"

    def test_non_anthropic_tier_also_downgraded(self, downgrade_router):
        # Enforcement is generic, not Claude-specific.
        _make_over_budget(downgrade_router, "phi4")
        body = _chat_body(model="phi4")
        assert downgrade_router.select_tier(body) == "gpt4o-mini"

    def test_unavailable_fallback_serves_requested_tier(
        self, downgrade_router, monkeypatch
    ):
        monkeypatch.setattr(downgrade_router, "_BUDGET_FALLBACK_TIER", "no-such-tier")
        _make_over_budget(downgrade_router, "claude")
        body = _chat_body()
        assert downgrade_router.select_tier(body) == "claude"
        assert "__budget_downgrade__" not in body


# ── block mode ───────────────────────────────────────────────────────────────

class TestBlockMode:
    @pytest.fixture()
    def block_router(self, router, monkeypatch):
        monkeypatch.setattr(router, "_BUDGET_ENFORCE_MODE", "block")
        return router

    def test_endpoint_429_with_budget_exceeded_body(
        self, client, block_router, monkeypatch
    ):
        _make_over_budget(block_router, "claude")

        async def must_not_be_called(tier, body):  # pragma: no cover
            raise AssertionError("blocked request must not reach the model")

        monkeypatch.setattr(block_router, "_call_model", must_not_be_called)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 429
        detail = r.json()["detail"]
        assert detail["error"] == "budget_exceeded"
        assert detail["tier"] == "claude"
        assert detail["mode"] == "block"
        assert detail["spent_usd"] > detail["limit_usd"]

    def test_under_budget_still_served(self, client, block_router, monkeypatch):
        async def fake_call(tier, body):
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(block_router, "_call_model", fake_call)
        r = client.post("/v1/chat/completions", json=_chat_body())
        assert r.status_code == 200

    def test_fallback_floor_tier_is_exempt(self, client, block_router, monkeypatch):
        # gpt4o-mini over budget redirects to phi4 in the base selector; blow
        # phi4's budget too and point the enforcement fallback at phi4 itself —
        # the floor is exempt (self-referential fallback degrades to warn), so
        # the request is served, never stranded.
        monkeypatch.setattr(block_router, "_BUDGET_ENFORCE_MODE", "downgrade")
        monkeypatch.setattr(block_router, "_BUDGET_FALLBACK_TIER", "phi4")
        _make_over_budget(block_router, "gpt4o-mini")
        block_router._spend["phi4"] = block_router.MODELS["phi4"]["daily_budget"] + 1.0
        body = _chat_body(model="gpt-4o-mini")
        assert block_router.select_tier(body) == "phi4"
        assert "__budget_downgrade__" not in body


# ── Native /v1/messages path (bypasses select_tier) ──────────────────────────
# The Anthropic-native endpoint picks its tier via
# _select_anthropic_tier_for_model and calls the SDK directly — the exact gap
# class that let the upstream Claude tier overrun with only WARN lines — so it
# has its own explicit enforcement and its own coverage here.

class _FakeUsage:
    input_tokens = 3
    output_tokens = 1
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _FakeAnthropicResp:
    usage = _FakeUsage()

    def model_dump(self, **_kw):
        return {
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude",
            "content": [{"type": "text", "text": "pong"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }


def _fake_anthropic_client(created):
    class _Messages:
        async def create(self, **kwargs):
            created.append(kwargs)
            return _FakeAnthropicResp()

    class _Client:
        messages = _Messages()

    return _Client()


class TestMessagesNativePathEnforcement:
    def test_warn_mode_still_serves_claude_direct(self, client, router, monkeypatch):
        # Current behavior preserved: over-budget Claude on the direct-SDK
        # path is served (with a WARN), not downgraded or blocked.
        _make_over_budget(router, "claude")
        created = []
        monkeypatch.setattr(router, "_make_anthropic_client",
                            lambda cfg: _fake_anthropic_client(created))
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 200
        assert len(created) == 1  # the direct SDK client WAS called
        assert r.json()["_router"]["tier"] == "claude"
        assert "X-Router-Budget-Downgrade" not in r.headers

    def test_native_path_records_spend(self, client, router, monkeypatch):
        # A6 prerequisite: the direct-SDK path accrues to the daily ledger
        # (before this it recorded nothing, so enforcement would be inert).
        _pin_today(router)
        router._spend.pop("claude", None)
        created = []
        monkeypatch.setattr(router, "_make_anthropic_client",
                            lambda cfg: _fake_anthropic_client(created))
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 200
        assert router._spend["claude"] > 0.0

    def test_block_mode_429_before_any_upstream_call(self, client, router, monkeypatch):
        monkeypatch.setattr(router, "_BUDGET_ENFORCE_MODE", "block")
        _make_over_budget(router, "claude")

        def must_not_build_client(cfg):  # pragma: no cover
            raise AssertionError("blocked request must not build an SDK client")

        monkeypatch.setattr(router, "_make_anthropic_client", must_not_build_client)
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 429
        body = r.json()
        # Anthropic-shaped error so anthropic_messages transports can parse it.
        assert body["type"] == "error"
        assert body["error"]["type"] == "rate_limit_error"
        assert body["error"]["detail"]["error"] == "budget_exceeded"
        assert body["error"]["detail"]["tier"] == "claude"

    def test_downgrade_with_non_anthropic_fallback_degrades_to_warn(
        self, client, router, monkeypatch
    ):
        # AAF divergence from the upstream deployment (documented in the PR):
        # this router has no Anthropic→OpenAI response translation for the
        # native path, so with the default gpt4o-mini fallback (not Anthropic-
        # backed) a downgrade cannot keep the response Anthropic-shaped. The
        # module's fallback-unavailable rule kicks in: degrade to warn, serve
        # the requested tier — never strand the request.
        monkeypatch.setattr(router, "_BUDGET_ENFORCE_MODE", "downgrade")
        _make_over_budget(router, "claude")
        created = []
        monkeypatch.setattr(router, "_make_anthropic_client",
                            lambda cfg: _fake_anthropic_client(created))
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 200
        assert len(created) == 1  # served by the requested tier's SDK client
        assert r.json()["_router"]["tier"] == "claude"
        assert "X-Router-Budget-Downgrade" not in r.headers

    def test_downgrade_with_anthropic_fallback_serves_it_natively(
        self, client, router, monkeypatch
    ):
        # An Anthropic-backed fallback CAN serve the native shape, so the
        # downgrade happens in-path and is marked on the response.
        router.MODELS["claude-mini"] = dict(
            router.MODELS["claude"],
            litellm_model="anthropic/claude-mini",
            daily_budget=5.0,
        )
        monkeypatch.setattr(router, "_BUDGET_ENFORCE_MODE", "downgrade")
        monkeypatch.setattr(router, "_BUDGET_FALLBACK_TIER", "claude-mini")
        _make_over_budget(router, "claude")
        created = []
        monkeypatch.setattr(router, "_make_anthropic_client",
                            lambda cfg: _fake_anthropic_client(created))
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 200
        assert len(created) == 1
        assert created[0]["model"] == "claude-mini"  # fallback deployment used
        assert r.headers["X-Router-Budget-Downgrade"] == "claude->claude-mini"
        meta = r.json()["_router"]
        assert meta["tier"] == "claude-mini"
        assert meta["budget_downgraded_from"] == "claude"

    def test_under_budget_native_path_untouched_in_downgrade_mode(
        self, client, router, monkeypatch
    ):
        monkeypatch.setattr(router, "_BUDGET_ENFORCE_MODE", "downgrade")
        _pin_today(router)
        router._spend.pop("claude", None)
        created = []
        monkeypatch.setattr(router, "_make_anthropic_client",
                            lambda cfg: _fake_anthropic_client(created))
        r = client.post("/v1/messages", json=_messages_body())
        assert r.status_code == 200
        assert len(created) == 1
        assert r.json()["_router"]["tier"] == "claude"


# ── /v1/embeddings path (no tier at all) ─────────────────────────────────────
# Embeddings bypass tier selection; A6 gives them a dedicated "embeddings"
# ledger bucket + cap. No same-vector-space downgrade target exists, so
# downgrade mode degrades to warn; block mode 429s.

async def _fake_aembedding(**kwargs):
    return {
        "data": [{"index": 0, "embedding": [0.1, 0.2]}],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 100, "total_tokens": 100},
    }


class TestEmbeddingsBudget:
    @pytest.fixture()
    def embed_router(self, router, monkeypatch):
        monkeypatch.setattr(router, "_EMBED_API_KEY", "test-embed-key")
        monkeypatch.setattr(router.litellm, "aembedding", _fake_aembedding)
        return router

    def _blow_bucket(self, router):
        _pin_today(router)
        router._spend[router._EMBED_LEDGER_BUCKET] = router._EMBED_DAILY_BUDGET_USD + 1.0

    def test_spend_accrues_to_embeddings_bucket(self, client, embed_router):
        _pin_today(embed_router)
        embed_router._spend.pop("embeddings", None)
        r = client.post("/v1/embeddings", json={"input": "hello"})
        assert r.status_code == 200
        # 100 tokens at the default $0.02/MTok list price.
        assert embed_router._spend["embeddings"] == pytest.approx(0.000002)

    def test_warn_mode_over_budget_still_served(self, client, embed_router):
        self._blow_bucket(embed_router)
        r = client.post("/v1/embeddings", json={"input": "hello"})
        assert r.status_code == 200

    def test_block_mode_over_budget_429(self, client, embed_router, monkeypatch):
        monkeypatch.setattr(embed_router, "_BUDGET_ENFORCE_MODE", "block")
        self._blow_bucket(embed_router)

        async def must_not_be_called(**kwargs):  # pragma: no cover
            raise AssertionError("blocked embeddings call must not reach upstream")

        monkeypatch.setattr(embed_router.litellm, "aembedding", must_not_be_called)
        r = client.post("/v1/embeddings", json={"input": "hello"})
        assert r.status_code == 429
        detail = r.json()["detail"]
        assert detail["error"] == "budget_exceeded"
        assert detail["tier"] == "embeddings"

    def test_block_mode_under_budget_served(self, client, embed_router, monkeypatch):
        monkeypatch.setattr(embed_router, "_BUDGET_ENFORCE_MODE", "block")
        _pin_today(embed_router)
        embed_router._spend.pop("embeddings", None)
        r = client.post("/v1/embeddings", json={"input": "hello"})
        assert r.status_code == 200

    def test_downgrade_mode_degrades_to_warn(self, client, embed_router, monkeypatch):
        # No same-vector-space fallback exists → downgrade serves anyway.
        monkeypatch.setattr(embed_router, "_BUDGET_ENFORCE_MODE", "downgrade")
        self._blow_bucket(embed_router)
        r = client.post("/v1/embeddings", json={"input": "hello"})
        assert r.status_code == 200
        assert "X-Router-Budget-Downgrade" not in r.headers

    def test_zero_cap_disables_ceiling(self, client, embed_router, monkeypatch):
        monkeypatch.setattr(embed_router, "_BUDGET_ENFORCE_MODE", "block")
        monkeypatch.setattr(embed_router, "_EMBED_DAILY_BUDGET_USD", 0.0)
        _pin_today(embed_router)
        embed_router._spend["embeddings"] = 999.0
        r = client.post("/v1/embeddings", json={"input": "hello"})
        assert r.status_code == 200

    def test_per_caller_cap_applies_to_embeddings(self, client, embed_router, monkeypatch):
        # aaf-0005 parity: an attributed caller over its daily cap is refused
        # on the embeddings path just like on the chat paths.
        monkeypatch.setattr(embed_router, "PER_CALLER_DAILY_USD", 1.0)
        _pin_today(embed_router)
        embed_router._spend_by_caller["agent-a"] = 2.0
        r = client.post("/v1/embeddings", json={"input": "hello"},
                        headers={"x-agent-id": "agent-a"})
        assert r.status_code == 429
        assert "Per-caller" in r.json()["detail"]
