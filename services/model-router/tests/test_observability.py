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


class TestObserveGenai:
    def test_noop_when_flag_disabled(self, router, monkeypatch):
        calls = []
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", False)
        monkeypatch.setattr(router, "_init_tracer", lambda: calls.append("init") or None)
        router.observe_genai(tier="gpt4o-mini", model="gpt-4o-mini",
                             input_tokens=1, output_tokens=1, cost_usd=0.0)
        assert calls == []  # flag off → tracer never touched

    def test_noop_when_no_tracer(self, router, monkeypatch):
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", True)
        monkeypatch.setattr(router, "_init_tracer", lambda: None)
        # No exception when there is no configured exporter.
        router.observe_genai(tier="gpt4o-mini", model="gpt-4o-mini",
                             input_tokens=1, output_tokens=1, cost_usd=0.0)

    def test_fail_open_on_tracer_error(self, router, monkeypatch):
        def boom():
            raise RuntimeError("exporter down")
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", True)
        monkeypatch.setattr(router, "_init_tracer", boom)
        # A telemetry error must never propagate to the caller.
        router.observe_genai(tier="gpt4o-mini", model="gpt-4o-mini",
                             input_tokens=1, output_tokens=1, cost_usd=0.0)

    def test_emits_span_with_attrs(self, router, monkeypatch):
        recorded = {}

        class FakeSpan:
            def set_attribute(self, k, v): recorded[k] = v
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class FakeTracer:
            def start_as_current_span(self, name):
                recorded["__span_name__"] = name
                return FakeSpan()

        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", True)
        monkeypatch.setattr(router, "_init_tracer", lambda: FakeTracer())
        router.observe_genai(tier="gpt4o-mini", model="gpt-4o-mini",
                             input_tokens=5, output_tokens=2, cost_usd=0.001, run_id="r9")
        assert recorded["__span_name__"] == "gen_ai.chat"
        assert recorded["gen_ai.request.model"] == "gpt-4o-mini"
        assert recorded["gen_ai.usage.input_tokens"] == 5
        assert recorded["agent.run_id"] == "r9"

    def test_observe_genai_exports_real_span(self, router, monkeypatch):
        """End-to-end through a REAL OTel pipeline: observe_genai must EXPORT a
        gen_ai.chat span (not just call set_attribute on a fake). This is the
        coverage that was missing — the live bug created spans that never reached
        an exporter (logs/metrics exported, spans silently didn't). Uses a
        SimpleSpanProcessor so export is synchronous on span-end and the assertion
        is deterministic (the production BatchSpanProcessor path is exercised live
        end-to-end against App Insights). sampler=ALWAYS_ON so the assertion doesn't
        depend on ambient OTel context — observe_genai uses start_as_current_span,
        and a non-sampled parent left in context by another test would otherwise
        make this child non-sampled (in production gen_ai.chat is a sampled root)."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        exporter = InMemorySpanExporter()
        provider = TracerProvider(sampler=ALWAYS_ON)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", True)
        monkeypatch.setattr(router, "_init_tracer", lambda: provider.get_tracer("test"))
        router.observe_genai(
            tier="grok", model="grok-4-1-fast-reasoning",
            input_tokens=7, output_tokens=2, cost_usd=0.0012, run_id="probe-1",
        )
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "gen_ai.chat"
        assert span.attributes["gen_ai.request.model"] == "grok-4-1-fast-reasoning"
        assert span.attributes["gen_ai.usage.input_tokens"] == 7
        assert span.attributes["gen_ai.usage.output_tokens"] == 2
        assert span.attributes["agent.run_id"] == "probe-1"


class TestAnthropicCostEstimate:
    def test_sonnet_rates(self, router):
        # 1,000,000 input @ $3 + 1,000,000 output @ $15 = $18.00 (sonnet list price)
        cost = router._estimate_anthropic_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.0)

    def test_opus_rates(self, router):
        # 1,000,000 input @ $15 + 1,000,000 output @ $75 = $90.00
        cost = router._estimate_anthropic_cost("claude-opus-4-8", 1_000_000, 1_000_000)
        assert cost == pytest.approx(90.0)

    def test_partial_tokens(self, router):
        # 100 input + 50 output on sonnet = 100/1e6*3 + 50/1e6*15
        cost = router._estimate_anthropic_cost("claude-sonnet-4-6", 100, 50)
        assert cost == pytest.approx(100 / 1e6 * 3 + 50 / 1e6 * 15)

    def test_unknown_model_falls_back_to_sonnet(self, router):
        cost = router._estimate_anthropic_cost("some-future-claude", 1_000_000, 0)
        assert cost == pytest.approx(3.0)

    def test_haiku_rates(self, router):
        # 1M input @ $0.80 + 1M output @ $4.00 = $4.80 (3.5 Haiku list price)
        cost = router._estimate_anthropic_cost("claude-haiku-3-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(4.80)

    def test_zero_tokens(self, router):
        assert router._estimate_anthropic_cost("claude-sonnet-4-6", 0, 0) == pytest.approx(0.0)

    def test_none_model_falls_back_to_sonnet(self, router):
        cost = router._estimate_anthropic_cost(None, 1_000_000, 0)
        assert cost == pytest.approx(3.0)


class TestUsageFromResult:
    def test_extracts_tokens_and_model(self, router):
        result = {"model": "claude-sonnet-4-6",
                  "usage": {"prompt_tokens": 120, "completion_tokens": 34}}
        model, inp, out = router._usage_from_result(result, fallback_tier="claude")
        assert model == "claude-sonnet-4-6"
        assert inp == 120
        assert out == 34

    def test_missing_usage_defaults_zero_and_tier_model(self, router):
        result = {}
        model, inp, out = router._usage_from_result(result, fallback_tier="gpt4o-mini")
        assert inp == 0 and out == 0
        # falls back to the tier's configured litellm_model (minus provider prefix)
        assert model == "gpt-4o-mini"

    def test_unregistered_fallback_tier_uses_tier_name(self, router):
        model, inp, out = router._usage_from_result({}, fallback_tier="nonexistent-tier")
        assert model == "nonexistent-tier"
        assert inp == 0 and out == 0
