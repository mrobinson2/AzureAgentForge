# Observability Pipeline (v1.3.1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the two items the ROADMAP "Later" section names as "the rest of the observability pipeline", building directly on what v1.3 shipped (`services/model-router/main.py` GenAI-semconv spans behind `OBSERVABILITY_ENABLED`; `infrastructure/modules/monitoring/alerts.tf` v1.2 alert rules + workbook):
1. A **`gen_ai.usage` cost/token metric** — the spans already set the span *attributes* `gen_ai.usage.input_tokens` / `output_tokens` / `cost_usd`; now ALSO emit aggregatable OpenTelemetry **metrics** (counters) for cost + tokens so they can be charted and alerted, not just traced.
2. **SLO burn-rate alert rules** — two new Azure Monitor scheduled-query alert rules in the existing monitoring Terraform module that compute the model-router error-budget burn rate (fast-burn + slow-burn), following the exact opt-in/count-gated patterns of the v1.2 alerts.

**Architecture:** The metric rides the SAME OTel provider the spans use. `configure_azure_monitor()` (called once in `_init_tracer`) configures the full OTel SDK — traces *and* metrics — and sets the global meter provider exported to Application Insights. So `opentelemetry.metrics.get_meter("model-router")` returns a meter backed by the already-wired Azure Monitor exporter; we add NO new exporter, connection string, or telemetry stack. A new `observe_genai_metrics()` reuses the existing `genai_semconv_attrs()` mapping for its metric attributes (same tier/system/model dimensions) and is called from the same `observe_genai()` body, so a single flag (`OBSERVABILITY_ENABLED`) and a single fail-open `try/except` govern both span and metric. The SLO alerts are pure HCL added to `alerts.tf`, count-gated on the existing `local.alerts_enabled` (`alert_emails` non-empty), querying the router's existing console-log markers (`call_failed` / `primary_failed` / `fallback_failed` for errors; `routing`/`success`/`messages success` for total volume) — so `terraform validate` stays clean with default (empty) inputs and the deploy footprint is unchanged.

**Tech Stack:** Python 3.12, `opentelemetry-api`/`opentelemetry-sdk` (transitively via `azure-monitor-opentelemetry>=1.6.0`), `opentelemetry.metrics` counters, pytest 8.3.3 (`services/model-router/tests`, the `router` + `monkeypatch` fixtures in `conftest.py`); Terraform 1.9.5, `azurerm_monitor_scheduled_query_rules_alert_v2`, KQL over `ContainerAppConsoleLogs_CL`.

---

### Task 0: Confirm the OTel metrics API surface against the pinned SDK (de-risk before coding)

**Why:** The metric-emission code below uses `opentelemetry.metrics.get_meter(...).create_counter(...).add(value, attributes=...)`. That API is stable in the OpenTelemetry Python SDK that `azure-monitor-opentelemetry>=1.6.0` pulls in, and `configure_azure_monitor()` already sets the global meter provider — but this repo never exercised the metrics path before, so prove the import + call shape once before writing the emitter. This is a read-only verification step (no source changes).

**Files:** none (verification only).

- [ ] **Step 1:** In the router venv (or CI image), confirm the metrics API is importable and the global meter provider is the default no-op until `configure_azure_monitor` runs:

```bash
cd services/model-router
pip install -r requirements.txt -r requirements-dev.txt
python -c "
from opentelemetry import metrics
m = metrics.get_meter('probe')
c = m.create_counter('probe.count', unit='1', description='probe')
c.add(3, attributes={'k': 'v'})    # no-op meter: must not raise
print('metrics API OK:', type(c).__name__)
"
```

Expected output (the default no-op meter accepts `.add` without an exporter):

```
metrics API OK: <a Counter or _ProxyCounter type name>
```

- [ ] **Step 2:** Confirm `configure_azure_monitor` configures metrics (not just traces) — this is what makes the metric DRY with the span. Read the package docstring; do NOT call it (it needs a real connection string):

```bash
python -c "import azure.monitor.opentelemetry as a; print(a.configure_azure_monitor.__doc__[:600])"
```

Expected: the docstring mentions configuring distributed tracing **and metrics** (and logging) via OpenTelemetry — confirming one call wires the meter provider the router's `_init_tracer` already invokes.

> If Step 1 raises `ImportError` on `opentelemetry.metrics`, STOP: the pinned `azure-monitor-opentelemetry` is older than expected. The remedy is to add an explicit `opentelemetry-api` pin to `requirements.txt`; reconcile, then resume. If Step 2 shows metrics are NOT configured by `configure_azure_monitor` in this version, the emitter in Task 2 must also create a metric reader/exporter — note it and adjust `_init_meter` accordingly. Both are unlikely on `>=1.6.0` but cheap to verify now.

---

### Task 1: `genai_metric_attrs()` — the pure metric-attribute mapping

**Why first:** Metrics need a *low-cardinality* attribute set (charting/alerting groups by these). The span attrs include per-call values like exact token counts and `agent.run_id` (high cardinality) that must NOT become metric dimensions or they explode the time-series count. So we derive a small, dedicated dimension set first, pure and unit-tested, mirroring how `genai_semconv_attrs()` is a pure mapping.

**Files:**
- Modify: `services/model-router/main.py`
- Test: `services/model-router/tests/test_observability.py`

- [ ] **Step 1: Write the failing test** — append this class to `services/model-router/tests/test_observability.py`:

```python
class TestGenaiMetricAttrs:
    def test_low_cardinality_dimensions_only(self, router):
        attrs = router.genai_metric_attrs(
            tier="gpt4o-mini", model="gpt-4o-mini", run_id="r1",
        )
        # Dimensions a chart/alert can group by — and nothing per-call/high-card.
        assert attrs == {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "az.ai.foundry",
            "gen_ai.request.model": "gpt-4o-mini",
            "agent.tier": "gpt4o-mini",
        }
        # run_id and raw token/cost numbers must NOT be metric dimensions.
        assert "agent.run_id" not in attrs
        assert "gen_ai.usage.input_tokens" not in attrs

    def test_anthropic_system_dimension(self, router):
        attrs = router.genai_metric_attrs(tier="claude", model="claude-sonnet-4-6")
        assert attrs["gen_ai.system"] == "anthropic"

    def test_model_falls_back_to_tier_when_blank(self, router):
        attrs = router.genai_metric_attrs(tier="gpt4o-mini", model="")
        assert attrs["gen_ai.request.model"] == "gpt4o-mini"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestGenaiMetricAttrs`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'genai_metric_attrs'`.

- [ ] **Step 3: Write minimal implementation** — in `services/model-router/main.py`, immediately AFTER `genai_semconv_attrs()` (ends at the `return attrs` near line 423), add:

```python
def genai_metric_attrs(
    *, tier: str, model: str, run_id: str | None = None,
) -> dict[str, str]:
    """Low-cardinality dimension set for the gen_ai.usage METRICS.

    Deliberately a strict subset of genai_semconv_attrs(): per-call values
    (exact token counts, cost, run_id) are span attributes only — making them
    metric dimensions would explode the time-series cardinality. run_id is
    accepted for signature symmetry with observe_genai() but intentionally
    dropped. No I/O.
    """
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": _genai_system(tier),
        "gen_ai.request.model": model or tier,
        "agent.tier": tier,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestGenaiMetricAttrs`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(router): pure low-cardinality gen_ai metric attribute mapping"
```

---

### Task 2: `_init_meter()` + module-level instruments — reuse the span's OTel provider

**Why:** The meter must come from the SAME provider `configure_azure_monitor()` set up in `_init_tracer()`, so there's no parallel telemetry stack. `_init_meter()` mirrors `_init_tracer()`: lazy, idempotent, returns `None` when no connection string is set or setup fails. The three counters (cost, input tokens, output tokens) are created once and memoized.

**Files:**
- Modify: `services/model-router/main.py`
- Test: `services/model-router/tests/test_observability.py`
- Modify: `services/model-router/tests/conftest.py` (snapshot the new meter globals so state doesn't leak between tests)

- [ ] **Step 1: Extend the state-isolation fixture FIRST** (so the new globals are restored around every test). In `services/model-router/tests/conftest.py`, inside `_isolate_router_state`, add snapshot lines next to the existing tracer snapshot (after `tracer_initialised_snapshot = main._tracer_initialised`):

```python
    meter_snapshot = main._meter
    meter_initialised_snapshot = main._meter_initialised
    instruments_snapshot = main._instruments
```

and restore lines next to the existing tracer restore (after `main._tracer_initialised = tracer_initialised_snapshot`):

```python
    main._meter = meter_snapshot
    main._meter_initialised = meter_initialised_snapshot
    main._instruments = instruments_snapshot
```

- [ ] **Step 2: Write the failing test** — append to `test_observability.py`:

```python
class TestInitMeter:
    def test_returns_none_without_connection_string(self, router, monkeypatch):
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        monkeypatch.setattr(router, "_meter", None)
        monkeypatch.setattr(router, "_meter_initialised", False)
        assert router._init_meter() is None

    def test_idempotent_does_not_reconfigure(self, router, monkeypatch):
        calls = []

        class FakeMeter:
            def create_counter(self, *a, **k):
                calls.append(("counter",) + a)
                return object()

        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
        monkeypatch.setattr(router, "_meter", None)
        monkeypatch.setattr(router, "_meter_initialised", False)
        monkeypatch.setattr(router, "_instruments", None)
        # configure_azure_monitor is already called by _init_tracer; the meter
        # comes from the global provider. Stub the get_meter import boundary.
        monkeypatch.setattr(router, "_get_otel_meter", lambda name: FakeMeter())
        m1 = router._init_meter()
        m2 = router._init_meter()
        assert m1 is m2  # memoized
        # three counters created exactly once (not on the second call)
        assert sum(1 for c in calls if c[0] == "counter") == 3
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestInitMeter`
Expected: FAIL — `AttributeError: module 'main' has no attribute '_init_meter'`.

- [ ] **Step 4: Write minimal implementation** — in `services/model-router/main.py`, immediately AFTER `_init_tracer()` (ends ~line 453, `return _tracer`), add:

```python
_meter = None
_meter_initialised = False
_instruments = None  # dict of created counters, built once


def _get_otel_meter(name: str):
    """Import boundary so tests can stub the global meter provider lookup.
    Returns a meter from whatever provider configure_azure_monitor() set up."""
    from opentelemetry import metrics
    return metrics.get_meter(name)


def _init_meter():
    """Lazily obtain the OTel meter once, from the SAME provider _init_tracer's
    configure_azure_monitor() set up (it configures traces AND metrics). Returns
    the meter, or None when no connection string is set or setup fails. Builds
    the gen_ai.usage instruments once and memoizes them on _instruments."""
    global _meter, _meter_initialised, _instruments
    if _meter_initialised:
        return _meter
    _meter_initialised = True
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return None
    try:
        # Ensure the Azure Monitor SDK is configured (idempotent — _init_tracer
        # may have already called it; configure_azure_monitor wires metrics too).
        _init_tracer()
        meter = _get_otel_meter("model-router")
        _instruments = {
            "cost": meter.create_counter(
                "gen_ai.usage.cost_usd",
                unit="USD",
                description="Estimated/billed model spend per call, summed.",
            ),
            "input_tokens": meter.create_counter(
                "gen_ai.usage.input_tokens",
                unit="{token}",
                description="Prompt (input) tokens consumed, summed.",
            ),
            "output_tokens": meter.create_counter(
                "gen_ai.usage.output_tokens",
                unit="{token}",
                description="Completion (output) tokens produced, summed.",
            ),
        }
        _meter = meter
    except Exception as e:  # pragma: no cover - requires live OTel SDK
        log.warning("observability: meter init failed, disabling metrics: %s", e)
        _meter = None
        _instruments = None
    return _meter
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestInitMeter`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/conftest.py services/model-router/tests/test_observability.py
git commit -m "feat(router): lazy OTel meter + gen_ai.usage counters off the shared provider"
```

---

### Task 3: `observe_genai_metrics()` — record cost + tokens onto the counters

**Files:**
- Modify: `services/model-router/main.py`
- Test: `services/model-router/tests/test_observability.py`

- [ ] **Step 1: Write the failing test** — append to `test_observability.py`:

```python
class TestObserveGenaiMetrics:
    def _fake_instruments(self, recorded):
        class FakeCounter:
            def __init__(self, key): self.key = key
            def add(self, amount, attributes=None):
                recorded.append((self.key, amount, attributes))
        return {
            "cost": FakeCounter("cost"),
            "input_tokens": FakeCounter("input_tokens"),
            "output_tokens": FakeCounter("output_tokens"),
        }

    def test_noop_when_no_meter(self, router, monkeypatch):
        recorded = []
        monkeypatch.setattr(router, "_init_meter", lambda: None)
        router.observe_genai_metrics(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=10, output_tokens=2, cost_usd=0.5,
        )
        assert recorded == []  # no meter → nothing recorded

    def test_records_three_counters_with_metric_dims(self, router, monkeypatch):
        recorded = []
        monkeypatch.setattr(router, "_init_meter", lambda: object())
        monkeypatch.setattr(router, "_instruments", self._fake_instruments(recorded))
        router.observe_genai_metrics(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=10, output_tokens=2, cost_usd=0.0012345678, run_id="r1",
        )
        by_key = {k: (amt, dims) for k, amt, dims in recorded}
        assert by_key["input_tokens"][0] == 10
        assert by_key["output_tokens"][0] == 2
        # cost rounded to 6 dp, same convention as the span attribute
        assert by_key["cost"][0] == 0.001235
        # low-cardinality dims only — no run_id leaking into the time series
        assert by_key["cost"][1] == {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "az.ai.foundry",
            "gen_ai.request.model": "gpt-4o-mini",
            "agent.tier": "gpt4o-mini",
        }

    def test_fail_open_on_counter_error(self, router, monkeypatch):
        class Boom:
            def add(self, *a, **k): raise RuntimeError("exporter down")
        monkeypatch.setattr(router, "_init_meter", lambda: object())
        monkeypatch.setattr(
            router, "_instruments",
            {"cost": Boom(), "input_tokens": Boom(), "output_tokens": Boom()},
        )
        # A metrics error must never propagate to the caller.
        router.observe_genai_metrics(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        )

    def test_negative_values_coerced_to_zero(self, router, monkeypatch):
        recorded = []
        monkeypatch.setattr(router, "_init_meter", lambda: object())
        monkeypatch.setattr(router, "_instruments", self._fake_instruments(recorded))
        router.observe_genai_metrics(
            tier="gpt4o-mini", model="m",
            input_tokens=-5, output_tokens=-1, cost_usd=-3.0,
        )
        by_key = {k: amt for k, amt, _ in recorded}
        # counters are monotonic; never add a negative delta
        assert by_key["input_tokens"] == 0
        assert by_key["output_tokens"] == 0
        assert by_key["cost"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestObserveGenaiMetrics`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'observe_genai_metrics'`.

- [ ] **Step 3: Write minimal implementation** — in `services/model-router/main.py`, immediately AFTER `observe_genai()` (ends ~line 477, the fail-open `except` block), add:

```python
def observe_genai_metrics(
    *, tier: str, model: str, input_tokens: int, output_tokens: int,
    cost_usd: float, run_id: str | None = None,
) -> None:
    """Record one model call onto the gen_ai.usage counters (cost, input/output
    tokens) with low-cardinality dimensions. Flag-gated by the caller
    (observe_genai); FAIL-OPEN — any metrics error is swallowed so it can never
    break a model call. Counter deltas are clamped >= 0 (counters are monotonic)."""
    try:
        if _init_meter() is None or not _instruments:
            return
        dims = genai_metric_attrs(tier=tier, model=model, run_id=run_id)
        _instruments["input_tokens"].add(max(0, int(input_tokens or 0)), attributes=dims)
        _instruments["output_tokens"].add(max(0, int(output_tokens or 0)), attributes=dims)
        _instruments["cost"].add(max(0.0, round(float(cost_usd or 0.0), 6)), attributes=dims)
    except Exception as e:  # fail-open: never surface telemetry errors
        log.debug("observe_genai_metrics swallowed: %s", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestObserveGenaiMetrics`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(router): observe_genai_metrics records cost+tokens onto the counters"
```

---

### Task 4: Wire the metric into `observe_genai()` — one flag, one fail-open boundary

**Why:** Keep span + metric DRY and co-gated. `observe_genai()` is already called from `record_cost()` on every model call (both the LiteLLM and Anthropic-direct paths — see `_call_model`). Calling `observe_genai_metrics()` from inside the existing `observe_genai()` body means the metric inherits the same `OBSERVABILITY_ENABLED` gate and the same outer fail-open `try/except` — no second call site, no second flag.

**Files:**
- Modify: `services/model-router/main.py`
- Test: `services/model-router/tests/test_observability.py`

- [ ] **Step 1: Write the failing test** — append to `test_observability.py`:

```python
class TestObserveGenaiEmitsBoth:
    def test_metric_emitted_alongside_span(self, router, monkeypatch):
        calls = []

        class FakeSpan:
            def set_attribute(self, k, v): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class FakeTracer:
            def start_as_current_span(self, name): return FakeSpan()

        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", True)
        monkeypatch.setattr(router, "_init_tracer", lambda: FakeTracer())
        monkeypatch.setattr(
            router, "observe_genai_metrics",
            lambda **kw: calls.append(kw),
        )
        router.observe_genai(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=7, output_tokens=3, cost_usd=0.01, run_id="rX",
        )
        assert len(calls) == 1
        assert calls[0]["input_tokens"] == 7
        assert calls[0]["cost_usd"] == 0.01
        assert calls[0]["tier"] == "gpt4o-mini"

    def test_metric_not_emitted_when_flag_off(self, router, monkeypatch):
        calls = []
        monkeypatch.setattr(router, "OBSERVABILITY_ENABLED", False)
        monkeypatch.setattr(router, "observe_genai_metrics", lambda **kw: calls.append(kw))
        router.observe_genai(
            tier="gpt4o-mini", model="gpt-4o-mini",
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        )
        assert calls == []  # flag off → neither span nor metric
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestObserveGenaiEmitsBoth`
Expected: FAIL on `test_metric_emitted_alongside_span` — `assert len(calls) == 1` fails (0 calls) because `observe_genai()` does not yet call `observe_genai_metrics()`. (`test_metric_not_emitted_when_flag_off` already passes.)

- [ ] **Step 3: Write minimal implementation** — in `services/model-router/main.py`, inside `observe_genai()`, in the `try` block AFTER the `with tracer.start_as_current_span(...)` block (after the `span.set_attribute` loop, still inside `try`), add the metric call. The edited body of the `try` becomes:

```python
        attrs = genai_semconv_attrs(
            tier=tier, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_usd=cost_usd, run_id=run_id,
        )
        with tracer.start_as_current_span("gen_ai.chat") as span:
            for k, v in attrs.items():
                span.set_attribute(k, v)
        # Same flag, same fail-open boundary: also emit the aggregatable
        # gen_ai.usage metrics so cost/tokens can be charted + alerted (v1.3.1).
        observe_genai_metrics(
            tier=tier, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_usd=cost_usd, run_id=run_id,
        )
```

> Note: place the `observe_genai_metrics` call so it runs whether or not the tracer is `None`. Re-check the guard: `observe_genai` currently returns early when `_init_tracer()` is `None`. To emit metrics even when only the meter (not the tracer) is available, move the metric call ABOVE the `if tracer is None: return` guard. Concretely, restructure the `try` so the metric emission is unconditional once the flag is on:
>
> ```python
>     try:
>         observe_genai_metrics(
>             tier=tier, model=model, input_tokens=input_tokens,
>             output_tokens=output_tokens, cost_usd=cost_usd, run_id=run_id,
>         )
>         tracer = _init_tracer()
>         if tracer is None:
>             return
>         attrs = genai_semconv_attrs(...)
>         with tracer.start_as_current_span("gen_ai.chat") as span:
>             for k, v in attrs.items():
>                 span.set_attribute(k, v)
>     except Exception as e:  # fail-open
>         log.debug("observe_genai swallowed: %s", e)
> ```
>
> This keeps span and metric independently fail-open (each has its own inner guard) while both ride `OBSERVABILITY_ENABLED`. The Task-4 tests above stub `_init_tracer` to a FakeTracer so both orderings pass; this ordering also satisfies the existing `TestObserveGenai::test_noop_when_no_tracer` (still no exception) — re-run that class in Step 4 to confirm.

- [ ] **Step 4: Run the test + the existing observe-genai tests to verify no regression**

Run: `cd services/model-router && pytest -q tests/test_observability.py::TestObserveGenaiEmitsBoth tests/test_observability.py::TestObserveGenai`
Expected: PASS (2 new + 4 existing = 6 tests). In particular `TestObserveGenai::test_noop_when_no_tracer` and `test_fail_open_on_tracer_error` still pass.

- [ ] **Step 5: Run the FULL router suite to confirm nothing else regressed**

Run: `cd services/model-router && pytest -q tests/`
Expected: PASS — all existing tests plus the new observability classes. (The CI step is `pytest -q services/model-router/tests`; this is the same suite.)

- [ ] **Step 6: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(router): emit gen_ai.usage metrics alongside the span (OBSERVABILITY_ENABLED)"
```

---

### Task 5: SLO fast-burn alert rule (model-router error budget) in the monitoring module

**Why:** A burn-rate alert fires when the router's error ratio over a short window is high enough to exhaust a meaningful slice of the error budget fast. We follow the v1.2 rules exactly: `azurerm_monitor_scheduled_query_rules_alert_v2`, count-gated on `local.alerts_enabled`, querying `ContainerAppConsoleLogs_CL` for the router's existing error markers vs. its total request markers. No new variables beyond an opt-in SLO target/threshold pair with safe defaults so `terraform validate` stays clean.

**Files:**
- Modify: `infrastructure/modules/monitoring/variables.tf`
- Modify: `infrastructure/modules/monitoring/alerts.tf`

- [ ] **Step 1: Add the SLO variables** — append to `infrastructure/modules/monitoring/variables.tf`:

```hcl
# ─── SLO burn-rate alerts (model-router availability) ────────────────────────
# Opt-in alongside the other alert rules (they only exist when alert_emails is
# non-empty). The burn-rate ratio threshold is unitless: errors / total over the
# rule's window. Defaults page on a sustained high error fraction; tune per SLO.

variable "router_slo_fast_burn_ratio" {
  description = "Fast-burn threshold: model-router error fraction (errors / total requests) over PT5M that pages immediately. 0.10 = 10% of requests failing burns the error budget ~14x faster than a 99.9% SLO allows."
  type        = number
  default     = 0.10
}

variable "router_slo_slow_burn_ratio" {
  description = "Slow-burn threshold: model-router error fraction over PT1H that warns on a slower, sustained budget burn. 0.02 = 2% sustained."
  type        = number
  default     = 0.02
}

variable "router_app_name" {
  description = "Container App name of the model-router (or its host app, since the router runs as a sidecar) used to scope the SLO log queries. Empty matches across all apps on the router's unique log markers (routing / call_failed)."
  type        = string
  default     = ""
}
```

- [ ] **Step 2: Add the fast-burn rule** — in `infrastructure/modules/monitoring/alerts.tf`, AFTER the `watchdog_run_failure` resource (ends line 169) and BEFORE the `# ─── Observability workbook ───` section (line 171), first extend the `locals` at the top of the file. Append to the existing `locals { ... }` block (lines 18-28) a router scope filter, mirroring `watchdog_app_filter`:

```hcl
  # Optional scoping to the model-router's host app. Empty → match across all
  # apps. The router runs as a sidecar, so this is the host Container App name.
  router_app_filter = (
    var.router_app_name != ""
    ? "| where ContainerAppName_s == \"${var.router_app_name}\""
    : ""
  )
```

Then add the rule:

```hcl
# ─── SLO: model-router fast error-budget burn ────────────────────────────────
# Burn-rate alert. Over a short PT5M window, compute the router's error fraction
# (upstream call failures / total routed requests) and page when it exceeds the
# fast-burn threshold — a high error fraction is burning the availability error
# budget far faster than the SLO allows. Error markers: call_failed /
# primary_failed / fallback_failed. Total marker: "routing" (logged once per
# request in chat_completions) plus "messages tier=" (the /v1/messages path).
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "router_slo_fast_burn" {
  count                   = local.alerts_enabled ? 1 : 0
  name                    = "alert-${var.project}-${var.environment}-router-slo-fast-burn"
  resource_group_name     = var.resource_group_name
  location                = var.location
  description             = "Model-router error-budget FAST burn: the upstream error fraction over the last 5 minutes exceeds the fast-burn SLO threshold. An agent-facing path is degraded and the availability budget is draining quickly."
  display_name            = "Router SLO: fast error-budget burn (${var.environment})"
  severity                = 1
  enabled                 = true
  evaluation_frequency    = "PT5M"
  window_duration         = "PT5M"
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  auto_mitigation_enabled = false

  criteria {
    query                   = <<-KQL
      let errors = ContainerAppConsoleLogs_CL
        ${local.router_app_filter}
        | where Log_s has_any ("call_failed", "primary_failed", "fallback_failed")
        | summarize Errors = count();
      let total = ContainerAppConsoleLogs_CL
        ${local.router_app_filter}
        | where Log_s startswith "routing tier=" or Log_s startswith "messages tier="
        | summarize Total = count();
      errors
      | extend Total = toscalar(total)
      | extend BurnRatio = iff(Total == 0, 0.0, todouble(Errors) / todouble(Total))
      | where BurnRatio > ${var.router_slo_fast_burn_ratio}
      | project BurnRatio, Errors, Total
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.alerts[0].id]
  }

  tags = var.tags
}
```

- [ ] **Step 3: Validate** — from the dev environment, run the same command CI runs:

Run:
```bash
cd infrastructure/environments/dev
terraform init -backend=false
terraform validate
```
Expected: `Success! The configuration is valid.` with default (empty) `alert_emails` — the new resource is count-gated to 0 and the new variables have defaults, so nothing changes in the default footprint.

- [ ] **Step 4: fmt** — run the CI fmt gate so the new HCL is canonical:

Run: `terraform fmt -check -recursive infrastructure`
Expected: no output (exit 0). If it lists files, run `terraform fmt -recursive infrastructure` and re-check.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/modules/monitoring/variables.tf infrastructure/modules/monitoring/alerts.tf
git commit -m "feat(monitoring): model-router fast-burn SLO alert (opt-in, count-gated)"
```

---

### Task 6: SLO slow-burn alert rule + module output + dev wiring

**Files:**
- Modify: `infrastructure/modules/monitoring/alerts.tf`
- Modify: `infrastructure/environments/dev/variables.tf`
- Modify: `infrastructure/environments/dev/main.tf`

- [ ] **Step 1: Add the slow-burn rule** — in `infrastructure/modules/monitoring/alerts.tf`, AFTER the `router_slo_fast_burn` resource, add the slow-burn twin (longer window, lower threshold, Sev2):

```hcl
# ─── SLO: model-router slow error-budget burn ────────────────────────────────
# The slower companion to the fast-burn rule. Over PT1H, a lower but sustained
# error fraction still erodes the monthly budget — warn (Sev2) before it
# becomes an outage. Same markers; longer window, lower threshold.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "router_slo_slow_burn" {
  count                   = local.alerts_enabled ? 1 : 0
  name                    = "alert-${var.project}-${var.environment}-router-slo-slow-burn"
  resource_group_name     = var.resource_group_name
  location                = var.location
  description             = "Model-router error-budget SLOW burn: a lower but sustained upstream error fraction over the last hour is steadily eroding the availability budget. Investigate before it escalates to a fast burn."
  display_name            = "Router SLO: slow error-budget burn (${var.environment})"
  severity                = 2
  enabled                 = true
  evaluation_frequency    = var.alert_evaluation_frequency
  window_duration         = "PT1H"
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  auto_mitigation_enabled = false

  criteria {
    query                   = <<-KQL
      let errors = ContainerAppConsoleLogs_CL
        ${local.router_app_filter}
        | where Log_s has_any ("call_failed", "primary_failed", "fallback_failed")
        | summarize Errors = count();
      let total = ContainerAppConsoleLogs_CL
        ${local.router_app_filter}
        | where Log_s startswith "routing tier=" or Log_s startswith "messages tier="
        | summarize Total = count();
      errors
      | extend Total = toscalar(total)
      | extend BurnRatio = iff(Total == 0, 0.0, todouble(Errors) / todouble(Total))
      | where BurnRatio > ${var.router_slo_slow_burn_ratio}
      | project BurnRatio, Errors, Total
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.alerts[0].id]
  }

  tags = var.tags
}
```

- [ ] **Step 2: Add both rule ids to the existing `alert_rule_ids` output** — in `infrastructure/modules/monitoring/alerts.tf`, extend the `alert_rule_ids` output (lines 268-275) so the SLO rules are exported too:

```hcl
output "alert_rule_ids" {
  description = "Resource ids of the scheduled-query alert rules (empty when disabled)."
  value = local.alerts_enabled ? [
    azurerm_monitor_scheduled_query_rules_alert_v2.watchdog_critical[0].id,
    azurerm_monitor_scheduled_query_rules_alert_v2.secret_expiry[0].id,
    azurerm_monitor_scheduled_query_rules_alert_v2.watchdog_run_failure[0].id,
    azurerm_monitor_scheduled_query_rules_alert_v2.router_slo_fast_burn[0].id,
    azurerm_monitor_scheduled_query_rules_alert_v2.router_slo_slow_burn[0].id,
  ] : []
}
```

- [ ] **Step 3: Thread the new vars through the dev environment.** In `infrastructure/environments/dev/variables.tf`, after the `enable_observability_workbook` variable (line ~242-246), add:

```hcl
variable "router_app_name" {
  description = "Container App name hosting the model-router sidecar, used to scope the SLO burn-rate log queries. Empty matches across all apps on the router's log markers."
  type        = string
  default     = ""
}

variable "router_slo_fast_burn_ratio" {
  description = "Fast-burn SLO threshold: router error fraction over 5m that pages (Sev1). Default 0.10."
  type        = number
  default     = 0.10
}

variable "router_slo_slow_burn_ratio" {
  description = "Slow-burn SLO threshold: router error fraction over 1h that warns (Sev2). Default 0.02."
  type        = number
  default     = 0.02
}
```

Then in `infrastructure/environments/dev/main.tf`, inside the `module "monitoring"` block (after `enable_observability_workbook = var.enable_observability_workbook`, line 183), pass them through:

```hcl
  router_app_name            = var.router_app_name
  router_slo_fast_burn_ratio = var.router_slo_fast_burn_ratio
  router_slo_slow_burn_ratio = var.router_slo_slow_burn_ratio
```

- [ ] **Step 4: Validate + fmt** (the CI gates):

Run:
```bash
cd infrastructure/environments/dev
terraform init -backend=false
terraform validate
cd ../../.. && terraform fmt -check -recursive infrastructure
```
Expected: `Success! The configuration is valid.` and `fmt -check` exits 0 with no listed files. Defaults keep `alert_emails = []` → both SLO rules count to 0; footprint unchanged.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/modules/monitoring/alerts.tf infrastructure/environments/dev/variables.tf infrastructure/environments/dev/main.tf
git commit -m "feat(monitoring): router slow-burn SLO alert + dev wiring + rule-id output"
```

---

### Task 7: Docs — README, ROADMAP, and the workbook cost tile

**Files:**
- Modify: `infrastructure/modules/monitoring/README.md`
- Modify: `infrastructure/modules/monitoring/alerts.tf` (add a cost-metric workbook tile)
- Modify: `ROADMAP.md`

- [ ] **Step 1: Add a `gen_ai.usage` cost tile to the workbook** — in `infrastructure/modules/monitoring/alerts.tf`, inside `local.workbook_json`'s `items` array, add one more tile after the existing model-router failures tile (the last item, ends line 246). Application Insights surfaces custom OTel metrics in the `customMetrics` table (Log Analytics export), so the tile charts cumulative cost:

```hcl
      ,
      {
        type = 3
        content = {
          version      = "KqlItem/1.0"
          query        = "customMetrics\n| where name == 'gen_ai.usage.cost_usd'\n| summarize CostUSD = sum(valueSum) by bin(timestamp, 1h), tostring(customDimensions['agent.tier'])\n| render timechart"
          size         = 0
          title        = "Model spend by tier (per hour, USD)"
          timeContext  = { durationMs = 604800000 }
          queryType    = 0
          resourceType = "microsoft.operationalinsights/workspaces"
        }
      }
```

> Note the leading `,` — it terminates the prior array element. Verify the array stays valid JSON after the edit (Step 3 `terraform validate` will catch a malformed `jsonencode` input). The `customMetrics` table is the App-Insights name for OTel metrics; the `valueSum` column holds the counter's summed value and `customDimensions` carries the `agent.tier` dimension set in Task 1.

- [ ] **Step 2: Update the module README** — in `infrastructure/modules/monitoring/README.md`:

In the "What it creates" table (lines 9-15), change the alert-rules row count and add the SLO row:

```markdown
| 5× `azurerm_monitor_scheduled_query_rules_alert_v2` | `alert_emails` non-empty | Watchdog critical / secret expiry / watchdog run failure / router SLO fast-burn / router SLO slow-burn. |
```

In "The log-marker contract" table (lines 27-32), add a row for the request-volume marker the SLO denominator uses:

```markdown
| Router request (SLO denominator) | `routing tier=…` / `messages tier=…` | `services/model-router/main.py` |
```

Add a new section after "Alert semantics":

```markdown
## SLO burn-rate alerts (model-router)

Two rules compute the router's error fraction — upstream failures
(`call_failed` / `primary_failed` / `fallback_failed`) over total routed
requests (`routing tier=` / `messages tier=`) — and fire on a high enough
ratio:

| Rule | Window | Default threshold | Severity |
|---|---|---|---|
| `router_slo_fast_burn` | 5 min | `router_slo_fast_burn_ratio` = 0.10 | Sev1 (page) |
| `router_slo_slow_burn` | 1 h   | `router_slo_slow_burn_ratio` = 0.02 | Sev2 (warn) |

Tune the ratios to your availability target (e.g. a 99.9% SLO over 30 days has
a small error budget; 10% of requests failing for 5 minutes is a fast burn).
Scope the queries to the router's host app with `router_app_name`.

## gen_ai.usage cost metric

When `OBSERVABILITY_ENABLED=true` on the model-router, every call also emits
aggregatable OpenTelemetry counters — `gen_ai.usage.cost_usd`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` — dimensioned by
`agent.tier`, `gen_ai.request.model`, and `gen_ai.system`. They ride the same
Application Insights exporter as the spans (no extra config) and surface in the
`customMetrics` table. The workbook's "Model spend by tier" tile charts them.
```

- [ ] **Step 3: Validate the workbook JSON change** (catches a broken tile):

Run:
```bash
cd infrastructure/environments/dev
terraform init -backend=false
terraform validate
```
Expected: `Success! The configuration is valid.` (a malformed `jsonencode` argument would fail here).

- [ ] **Step 4: Update ROADMAP** — in `ROADMAP.md`, move the two items out of "Later" (line 77) into the "v1.3 — shipped" section (after line 70). Replace the "Later" sentence's observability clause so it no longer lists the now-shipped items, and add a shipped bullet:

```markdown
- **Observability pipeline completed (v1.3.1).** The model-router now also emits aggregatable OpenTelemetry **metrics** — `gen_ai.usage.cost_usd` / `input_tokens` / `output_tokens`, dimensioned by tier/model/system — alongside the v1.3 spans, on the same Application Insights exporter and the same `OBSERVABILITY_ENABLED` flag (default off). The monitoring module gains two opt-in **SLO burn-rate alert rules** (fast 5-min / slow 1-h) over the router's error fraction, count-gated on `alert_emails` like the v1.2 rules, plus a "model spend by tier" workbook tile. `terraform validate` clean; metrics + mapping offline-tested.
```

And trim the "Later" line to:

```markdown
Multi-tenant implementation (the [`experimental/multi-tenant/`](experimental/multi-tenant/) design). More chat surfaces.
```

- [ ] **Step 5: fmt + commit**

```bash
terraform fmt -check -recursive infrastructure
git add infrastructure/modules/monitoring/ ROADMAP.md
git commit -m "docs(observability): cost-metric workbook tile, README SLO section, ROADMAP v1.3.1"
```

---

## Self-Review

- **Spec coverage:**
  - *Item 1 — `gen_ai.usage` cost metric:* Task 1 (low-cardinality dimension mapping, pure + tested) → Task 2 (`_init_meter` off the SAME `configure_azure_monitor` provider the spans use, idempotent, tested) → Task 3 (`observe_genai_metrics` records the three counters, fail-open, clamps negatives, tested) → Task 4 (wired into `observe_genai` so it's co-gated by `OBSERVABILITY_ENABLED` and shares the fail-open boundary; existing observe-genai tests re-run for no regression). DRY: no new exporter/connection string/telemetry stack; reuses `_genai_system`, the 6-dp cost rounding convention, and the same call site (`record_cost` → `observe_genai`).
  - *Item 2 — SLO burn-rate alerts:* Task 5 (fast-burn, Sev1, PT5M, count-gated on `local.alerts_enabled`) + Task 6 (slow-burn, Sev2, PT1H; module output + dev variable/main wiring). Both follow the exact `azurerm_monitor_scheduled_query_rules_alert_v2` shape, opt-in posture (`alert_emails`-gated, defaulted thresholds), and `ContainerAppConsoleLogs_CL` marker contract of the v1.2 rules; build on the v1.2 alert rules + workbook (Task 7 cost tile + README/ROADMAP).
- **Placeholder scan:** No `add appropriate X` / `similar to Task N` / TODO left in shipped code. Every step has a concrete pytest body or concrete HCL. The two inherently environment-dependent facts — (a) the exact OTel metrics API/provider behavior and (b) the App-Insights `customMetrics`/`valueSum` table shape for the workbook tile — are de-risked by Task 0's read-only probe and by `terraform validate` respectively, not deferred as code TODOs.
- **Type consistency:** `genai_metric_attrs(*, tier, model, run_id=None) -> dict[str, str]` is a strict subset of `genai_semconv_attrs`'s output keys; `observe_genai_metrics(...)` takes the identical keyword signature as `observe_genai(...)` so the wiring in Task 4 forwards `**kw` cleanly; counter `.add(amount, attributes=dims)` uses int token deltas and a float USD delta, both clamped `>= 0` (monotonic). On the HCL side, the SLO rules reuse the module's existing var types (`number` thresholds, `string` app name) and the `alerts_enabled` count gate; `alert_rule_ids` stays `list(string)`.
- **Default-off / opt-in preserved:** metric rides `OBSERVABILITY_ENABLED` (unchanged default false) and emits nothing without `APPLICATIONINSIGHTS_CONNECTION_STRING`; both SLO rules are `count = local.alerts_enabled ? 1 : 0` (zero when `alert_emails` is empty) with defaulted thresholds, so `terraform validate` + `fmt -check` pass on the default empty inputs and the deploy footprint is unchanged for a clean fork.
