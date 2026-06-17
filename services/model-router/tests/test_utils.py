"""Tests for the router's pure helpers: completion-kwargs assembly (temperature
defaults, max_tokens capping, tool forwarding), context-fit checks, token
estimation fallback, think-tag sanitisation, passthrough config, and the
passthrough fallback-chain augmentation."""

import pytest


# ── _build_completion_kwargs ─────────────────────────────────────────────────

class TestBuildCompletionKwargs:
    def test_basic_fields(self, router):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        kw = router._build_completion_kwargs("gpt4o-mini", body, stream=False)
        cfg = router.MODELS["gpt4o-mini"]
        assert kw["model"] == cfg["litellm_model"]
        assert kw["api_key"] == cfg["api_key"]
        assert kw["api_base"] == cfg["api_base"]
        assert kw["timeout"] == cfg["timeout_seconds"]
        assert kw["stream"] is False
        assert kw["messages"] == body["messages"]

    def test_max_tokens_capped_to_tier_ceiling(self, router):
        cap = router.MODELS["gpt4o-mini"]["max_tokens"]
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": cap + 100000}
        kw = router._build_completion_kwargs("gpt4o-mini", body, stream=False)
        assert kw["max_tokens"] == cap

    def test_caller_temperature_passes_through(self, router):
        body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
        kw = router._build_completion_kwargs("gpt4o-mini", body, stream=False)
        assert kw["temperature"] == 0.2

    def test_default_temperature_non_gpt5(self, router):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        kw = router._build_completion_kwargs("gpt4o-mini", body, stream=False)
        assert kw["temperature"] == 0.7

    def test_default_temperature_gpt5_family_is_one(self, router):
        router.MODELS["gpt5test"] = {
            "litellm_model": "openai/gpt-5-nano",
            "api_base": "http://x", "api_key": "k", "daily_budget": 1.0,
            "max_tokens": 4096, "context_limit": 128000, "timeout_seconds": 30,
            "supports_tools": True,
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}
        kw = router._build_completion_kwargs("gpt5test", body, stream=False)
        # gpt-5 family only accepts temperature=1; the default must reflect that.
        assert kw["temperature"] == 1.0

    def test_tools_forwarded_when_supported(self, router):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": "auto",
        }
        kw = router._build_completion_kwargs("gpt4o-mini", body, stream=False)
        assert kw["tools"] == body["tools"]
        assert kw["tool_choice"] == "auto"

    def test_tools_dropped_when_unsupported(self, router):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
        # phi4 has supports_tools=False — tools must not be forwarded.
        kw = router._build_completion_kwargs("phi4", body, stream=False)
        assert "tools" not in kw


# ── _fits_model ──────────────────────────────────────────────────────────────

class TestFitsModel:
    def test_fits_small_request(self, router):
        assert router._fits_model("gpt4o-mini", 100, 100) is True

    def test_does_not_fit_when_input_exceeds_context(self, router):
        limit = router.MODELS["gpt4o-mini"]["context_limit"]
        assert router._fits_model("gpt4o-mini", limit + 1, 1) is False

    def test_requested_max_capped_by_tier_for_fit_math(self, router):
        # requested_max is min()'d with the tier ceiling, so a huge requested
        # max doesn't by itself blow the context budget for a small input.
        assert router._fits_model("gpt4o-mini", 100, 10_000_000) is True


# ── _estimate_tokens ─────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_returns_positive_int(self, router):
        n = router._estimate_tokens([{"role": "user", "content": "hello there friend"}])
        assert isinstance(n, int) and n > 0

    def test_fallback_when_counter_raises(self, router, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("tokenizer unavailable")

        monkeypatch.setattr(router.litellm, "token_counter", boom)
        msgs = [{"role": "user", "content": "abcdefghi"}]  # 9 chars -> 9//3 = 3
        assert router._estimate_tokens(msgs) == 3


# ── _sanitise_content ────────────────────────────────────────────────────────

class TestSanitiseContent:
    def test_strips_think_tags(self, router):
        out = router._sanitise_content("before<think>secret reasoning</think>after")
        assert out == "beforeafter"

    def test_strips_thinking_tags_case_insensitive(self, router):
        out = router._sanitise_content("a<THINKING>x</THINKING>b")
        assert out == "ab"

    def test_strips_multiline(self, router):
        out = router._sanitise_content("a<think>\nline1\nline2\n</think>b")
        assert out == "ab"

    def test_strip_false_keeps_surrounding_whitespace(self, router):
        out = router._sanitise_content("  hello  ", strip=False)
        assert out == "  hello  "

    def test_strip_true_trims(self, router):
        assert router._sanitise_content("  hello  ") == "hello"


# ── _get_passthrough_config & passthrough fallback ───────────────────────────

class TestPassthrough:
    def test_passthrough_config_marks_passthrough(self, router):
        cfg = router._get_passthrough_config("some-deployment")
        assert cfg["passthrough"] is True
        assert cfg["litellm_model"] == "openai/some-deployment"
        assert cfg["supports_tools"] is True

    def test_passthrough_tier_gets_gpt4o_mini_fallback(self, router):
        # select_tier registers the ephemeral passthrough tier into MODELS.
        tier = router.select_tier(
            {"model": "brand-new-foundry-model", "messages": [{"role": "user", "content": "hi"}]}
        )
        chain = router._build_fallback_chain(tier, 10, 100)
        assert "gpt4o-mini" in chain
