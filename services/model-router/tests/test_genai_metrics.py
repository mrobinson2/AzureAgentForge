"""gen_ai.usage metric points + correlation-id on the span. The metric points
are a pure mapping (aggregatable token/cost counters by model/tier/system);
record_genai_metrics is flag-gated + fail-open like the span. Offline — no live
exporter is built (that path needs the Azure SDK + a connection string)."""


class TestMetricPoints:
    def test_emits_token_and_cost_points(self, router):
        pts = router.genai_metric_points(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=100, output_tokens=40, cost_usd=0.012,
        )
        by = {(p["instrument"], p["attributes"].get("gen_ai.token.type")): p for p in pts}
        assert by[("gen_ai.client.token.usage", "input")]["value"] == 100
        assert by[("gen_ai.client.token.usage", "output")]["value"] == 40
        cost = [p for p in pts if p["instrument"] == "gen_ai.client.cost.usd"][0]
        assert cost["value"] == 0.012
        # Dimensions are low-cardinality: model + tier + system.
        assert cost["attributes"]["agent.tier"] == "gpt4o-mini"
        assert "gen_ai.system" in cost["attributes"]

    def test_negative_values_clamped(self, router):
        pts = router.genai_metric_points(
            tier="phi4", model="phi-4", input_tokens=-5, output_tokens=-1, cost_usd=-2.0)
        assert all(p["value"] >= 0 for p in pts)


class TestRecordGenaiMetrics:
    def test_no_op_when_flag_off(self, router, monkeypatch):
        # Flag off → returns without touching the meter (no exception, no init).
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", False)
        monkeypatch.setattr(router, "_meter_initialised", False)
        router.record_genai_metrics(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.001)
        assert router._meter_initialised is False  # never initialised

    def test_fail_open_when_meter_unavailable(self, router, monkeypatch):
        # Flag on but no connection string → _init_meter returns None; must not raise.
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", True)
        monkeypatch.setattr(router, "_meter_initialised", False)
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        router.record_genai_metrics(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.001)  # no exception


class TestCorrelationOnSpan:
    def test_correlation_id_in_attrs(self, router):
        attrs = router.genai_semconv_attrs(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.0, correlation_id="corr-9")
        assert attrs["agent.correlation_id"] == "corr-9"

    def test_no_correlation_key_when_absent(self, router):
        attrs = router.genai_semconv_attrs(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.0)
        assert "agent.correlation_id" not in attrs
