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
