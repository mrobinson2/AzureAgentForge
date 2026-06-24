"""Tests for the B3 GenAI-semconv observability layer (services/model-router).

This file is the home for the observability test suite; later tasks add classes
for the emitter, the Anthropic cost estimator, and the cost-path wiring. Today
it covers genai_semconv_attrs — the pure OTel attribute mapping (B3a)."""

import pytest


class TestGenaiSemconvAttrs:
    def test_maps_standard_and_agent_attributes(self, router):
        attrs = router.genai_semconv_attrs(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=100, output_tokens=20, cost_usd=0.0012345678, run_id="r1",
        )
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.system"] == "az.ai.foundry"
        assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
        assert attrs["gen_ai.usage.input_tokens"] == 100
        assert attrs["gen_ai.usage.output_tokens"] == 20
        # cost rounded to 6 decimal places
        assert attrs["gen_ai.usage.cost_usd"] == 0.001235
        assert attrs["agent.tier"] == "gpt4o-mini"
        assert attrs["agent.run_id"] == "r1"

    def test_run_id_omitted_when_absent(self, router):
        attrs = router.genai_semconv_attrs(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        )
        assert "agent.run_id" not in attrs

    def test_anthropic_tier_system(self, router):
        # CLAUDE tier is registered as an anthropic/ litellm_model in conftest's env
        attrs = router.genai_semconv_attrs(
            tier="claude", model="claude-sonnet-4-6",
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        )
        assert attrs["gen_ai.system"] == "anthropic"

    def test_zero_tokens_and_cost_coerced(self, router):
        attrs = router.genai_semconv_attrs(
            tier="gpt4o-mini", model="m", input_tokens=0, output_tokens=0, cost_usd=0.0,
        )
        assert attrs["gen_ai.usage.input_tokens"] == 0
        assert attrs["gen_ai.usage.output_tokens"] == 0
        assert attrs["gen_ai.usage.cost_usd"] == 0.0
