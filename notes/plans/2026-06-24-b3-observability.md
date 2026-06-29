# B3 — GenAI-semconv Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every model call through AAF's `services/model-router` emits one OpenTelemetry GenAI-semconv span (model, tokens, cost, latency) to Application Insights — behind `OBSERVABILITY_ENABLED` (default off), content-redacted — and the previously-untracked Anthropic path becomes cost-tracked + observable.

**Architecture:** A pure attribute-mapping function (`genai_semconv_attrs`) feeds a flag-gated, fail-open emitter (`observe_genai`) that lazily initialises an Azure Monitor tracer (`_init_tracer`). The single cost chokepoint `record_cost` is widened to carry model/token metadata and call the emitter, so both the litellm and Anthropic cost paths flow through one hook. The Anthropic path additionally gets a list-price cost estimator since the Anthropic SDK doesn't return `response_cost`.

**Tech Stack:** Python 3, FastAPI, OpenTelemetry via the `azure-monitor-opentelemetry` distro, pytest.

---

## File Structure

- **Modify** `services/model-router/main.py`:
  - new "Observability" section (after the Budget Tracking block, ~line 385): `_genai_system`, `genai_semconv_attrs`, `_init_tracer`, `observe_genai`, `OBSERVABILITY_ENABLED`, `_ANTHROPIC_PRICING_PER_MTOK`, `_estimate_anthropic_cost`.
  - widen `record_cost` (line 377-379).
  - wire both cost paths in `_call_model` (lines 1062-1069).
- **Modify** `services/model-router/requirements.txt`: add the OTel distro.
- **Modify** `infrastructure/modules/container-apps/hermes.tf`: add two env vars to the router **sidecar** container block (B3c).
- **Create** `services/model-router/tests/test_observability.py`: unit tests for the pure mapping, the emitter's flag-gating/fail-open, the estimator, and the path wiring.
- **Modify** `services/model-router/tests/test_budget.py`: extend for the widened `record_cost` signature.

All tests live under `services/model-router/tests/` and inherit `conftest.py` (which primes tier env vars before `main` imports and exposes the `router` fixture = the imported `main` module).

---

## Task 1: Pure GenAI-semconv attribute mapping

**Files:**
- Modify: `services/model-router/main.py` (new Observability section after line 385)
- Test: `services/model-router/tests/test_observability.py` (create)

- [ ] **Step 1: Write the failing test**

Create `services/model-router/tests/test_observability.py`:

```python
"""Tests for the B3 GenAI-semconv observability layer: the pure attribute
mapping, the flag-gated fail-open emitter, the Anthropic cost estimator, and
the wiring of both cost paths through the widened record_cost."""

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestGenaiSemconvAttrs -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'genai_semconv_attrs'`.

> Note: the `claude` tier presence depends on Foundry CLAUDE env vars. If `test_anthropic_tier_system` errors on a missing `claude` tier rather than asserting, prime it like the other tiers by adding to `conftest.py`: `os.environ.setdefault("CLAUDE_BASE_URL", "http://localhost:7777"); os.environ.setdefault("CLAUDE_API_KEY", "test-claude-key")`. Add that line in this step if the tier is absent.

- [ ] **Step 3: Write minimal implementation**

In `services/model-router/main.py`, add after the Budget Tracking block (after `is_over_budget`, ~line 385):

```python
# ─── Observability (GenAI semconv) ────────────────────────────────────────────
# One OTel span per model call with GenAI semantic-convention attributes, behind
# OBSERVABILITY_ENABLED (default off), content-redacted (no prompt/response text).
# `gen_ai.*` are the OTel standard attributes; `agent.*` are this repo's custom
# additions (tier + optional run id).

def _genai_system(tier: str) -> str:
    """gen_ai.system: 'anthropic' for Anthropic-dispatched tiers, else Foundry."""
    return "anthropic" if _is_anthropic_tier(tier) else "az.ai.foundry"


def genai_semconv_attrs(
    *, tier: str, model: str, input_tokens: int, output_tokens: int,
    cost_usd: float, run_id: str | None = None,
) -> dict[str, object]:
    """Pure mapping → OTel GenAI-semconv span attributes. No I/O."""
    attrs: dict[str, object] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": _genai_system(tier),
        "gen_ai.request.model": model or tier,
        "gen_ai.usage.input_tokens": int(input_tokens or 0),
        "gen_ai.usage.output_tokens": int(output_tokens or 0),
        "gen_ai.usage.cost_usd": round(float(cost_usd or 0.0), 6),
        "agent.tier": tier,
    }
    if run_id:
        attrs["agent.run_id"] = run_id
    return attrs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestGenaiSemconvAttrs -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(model-router): genai_semconv_attrs pure mapping (B3a)"
```

---

## Task 2: Flag-gated, fail-open emitter + lazy tracer

**Files:**
- Modify: `services/model-router/main.py` (Observability section)
- Test: `services/model-router/tests/test_observability.py`

- [ ] **Step 1: Write the failing test**

Append to `services/model-router/tests/test_observability.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestObserveGenai -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'OBSERVABILITY_ENABLED'` (or `observe_genai`).

- [ ] **Step 3: Write minimal implementation**

In `services/model-router/main.py`, append to the Observability section:

```python
OBSERVABILITY_ENABLED = os.environ.get("OBSERVABILITY_ENABLED", "").strip().lower() in (
    "1", "true", "yes",
)

_tracer = None
_tracer_initialised = False


def _init_tracer():
    """Lazily configure the Azure Monitor OTel tracer once. Returns the tracer or
    None when no connection string is set or setup fails. Swap point: replace the
    configure_azure_monitor call with any OTLP exporter for a non-Azure backend."""
    global _tracer, _tracer_initialised
    if _tracer_initialised:
        return _tracer
    _tracer_initialised = True
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return None
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import trace
        configure_azure_monitor(connection_string=conn)
        _tracer = trace.get_tracer("model-router")
    except Exception as e:  # pragma: no cover - exercised via fail-open test
        log.warning("observability: tracer init failed, disabling: %s", e)
        _tracer = None
    return _tracer


def observe_genai(
    *, tier: str, model: str, input_tokens: int, output_tokens: int,
    cost_usd: float, run_id: str | None = None,
) -> None:
    """Emit one GenAI-semconv span. Flag-gated and FAIL-OPEN: any telemetry error
    is swallowed so it can never break a model call (the router is on every call)."""
    if not OBSERVABILITY_ENABLED:
        return
    try:
        tracer = _init_tracer()
        if tracer is None:
            return
        attrs = genai_semconv_attrs(
            tier=tier, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_usd=cost_usd, run_id=run_id,
        )
        with tracer.start_as_current_span("gen_ai.chat") as span:
            for k, v in attrs.items():
                span.set_attribute(k, v)
    except Exception as e:  # fail-open: never surface telemetry errors
        log.debug("observe_genai swallowed: %s", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestObserveGenai -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(model-router): flag-gated fail-open observe_genai + lazy tracer (B3a)"
```

---

## Task 3: Widen record_cost + hook the emitter

**Files:**
- Modify: `services/model-router/main.py:377-379`
- Test: `services/model-router/tests/test_budget.py`

- [ ] **Step 1: Write the failing test**

Append to `services/model-router/tests/test_budget.py` (inside the file, a new test class):

```python
class TestRecordCostObservability:
    def test_still_accumulates_with_new_kwargs(self, router):
        router._spend.clear()
        router.record_cost("gpt4o-mini", 0.10, model="gpt-4o-mini",
                           input_tokens=100, output_tokens=20)
        assert router._spend["gpt4o-mini"] == pytest.approx(0.10)

    def test_calls_observe_genai_with_metadata(self, router, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            router, "observe_genai",
            lambda **kw: seen.update(kw),
        )
        router._spend.clear()
        router.record_cost("gpt4o-mini", 0.05, model="gpt-4o-mini",
                           input_tokens=7, output_tokens=3, run_id="rX")
        assert seen["tier"] == "gpt4o-mini"
        assert seen["model"] == "gpt-4o-mini"
        assert seen["input_tokens"] == 7
        assert seen["output_tokens"] == 3
        assert seen["cost_usd"] == pytest.approx(0.05)
        assert seen["run_id"] == "rX"

    def test_backward_compatible_positional_call(self, router):
        # Existing callers using record_cost(tier, cost) must still work.
        router._spend.clear()
        router.record_cost("gpt4o-mini", 0.02)
        assert router._spend["gpt4o-mini"] == pytest.approx(0.02)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && python -m pytest tests/test_budget.py::TestRecordCostObservability -v`
Expected: FAIL — `test_calls_observe_genai_with_metadata` fails because `record_cost` doesn't accept `model=`/`input_tokens=` yet (`TypeError: record_cost() got an unexpected keyword argument`).

- [ ] **Step 3: Write minimal implementation**

In `services/model-router/main.py`, replace `record_cost` (lines 377-379):

```python
def record_cost(
    tier: str, cost: float, *, model: str | None = None,
    input_tokens: int = 0, output_tokens: int = 0, run_id: str | None = None,
) -> None:
    _reset_if_new_day()
    _spend[tier] += cost
    observe_genai(
        tier=tier, model=model or MODELS.get(tier, {}).get("litellm_model", tier),
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost, run_id=run_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && python -m pytest tests/test_budget.py -v`
Expected: PASS (all existing budget tests + the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_budget.py
git commit -m "feat(model-router): widen record_cost with model/token metadata + observe hook (B3a)"
```

---

## Task 4: Anthropic list-price cost estimator

**Files:**
- Modify: `services/model-router/main.py` (Observability section)
- Test: `services/model-router/tests/test_observability.py`

- [ ] **Step 1: Write the failing test**

Append to `services/model-router/tests/test_observability.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestAnthropicCostEstimate -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_estimate_anthropic_cost'`.

- [ ] **Step 3: Write minimal implementation**

In `services/model-router/main.py`, append to the Observability section:

```python
# Anthropic list-price per-million-token rates (input, output). The Anthropic SDK
# does not return response_cost, so cost on this path is a LIST-PRICE ESTIMATE,
# not a billed figure. Substring match on the model name; sonnet is the fallback.
_ANTHROPIC_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.80, 4.0),
}


def _estimate_anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """List-price estimate in USD for an Anthropic call. Defaults to sonnet rates."""
    name = (model or "").lower()
    in_rate, out_rate = next(
        (rates for key, rates in _ANTHROPIC_PRICING_PER_MTOK.items() if key in name),
        _ANTHROPIC_PRICING_PER_MTOK["claude-sonnet"],
    )
    return (input_tokens or 0) / 1_000_000 * in_rate + (output_tokens or 0) / 1_000_000 * out_rate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestAnthropicCostEstimate -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(model-router): Anthropic list-price cost estimator (B3b)"
```

---

## Task 5: Wire both cost paths through the widened record_cost

**Files:**
- Modify: `services/model-router/main.py:1062-1069` (inside `_call_model`)
- Test: `services/model-router/tests/test_observability.py`

This closes the Anthropic cost gap: the Anthropic path currently *skips* `record_cost` (line 1064 comment). After this task, both paths record cost with model/token metadata. To keep it unit-testable, extract usage parsing into a tiny pure helper first.

- [ ] **Step 1: Write the failing test**

Append to `services/model-router/tests/test_observability.py`:

```python
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
        assert model  # non-empty
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestUsageFromResult -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_usage_from_result'`.

- [ ] **Step 3: Write minimal implementation**

In `services/model-router/main.py`, append to the Observability section:

```python
def _usage_from_result(result: dict, *, fallback_tier: str) -> tuple[str, int, int]:
    """Pull (model, input_tokens, output_tokens) from an OpenAI-shaped result dict,
    falling back to the tier's configured model name when the result omits it."""
    usage = result.get("usage") or {}
    model = result.get("model") or MODELS[fallback_tier]["litellm_model"].split("/", 1)[-1]
    return model, int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/model-router && python -m pytest tests/test_observability.py::TestUsageFromResult -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire the two cost paths**

In `services/model-router/main.py`, replace the if/else cost block in `_call_model` (lines 1062-1069):

```python
            if is_anthropic:
                result = await _call_anthropic_direct(tier, body)
                # Anthropic SDK doesn't surface response_cost — estimate from
                # list price so Claude calls are cost-tracked + observable (B3b).
                _model, _in, _out = _usage_from_result(result, fallback_tier=tier)
                record_cost(
                    tier, _estimate_anthropic_cost(_model, _in, _out),
                    model=_model, input_tokens=_in, output_tokens=_out,
                )
            else:
                response = await litellm.acompletion(**kwargs)
                result = response.model_dump()
                _model, _in, _out = _usage_from_result(result, fallback_tier=tier)
                record_cost(
                    tier, response._hidden_params.get("response_cost") or 0.0,
                    model=_model, input_tokens=_in, output_tokens=_out,
                )
```

- [ ] **Step 6: Run the full router suite**

Run: `cd services/model-router && python -m pytest tests/ -q`
Expected: PASS — all pre-existing tests (146) plus the new observability tests green. (No new network; the wiring uses already-mocked paths in `test_endpoints.py`.)

- [ ] **Step 7: Commit**

```bash
git add services/model-router/main.py services/model-router/tests/test_observability.py
git commit -m "feat(model-router): record cost on both paths incl. Anthropic estimate (B3b)"
```

---

## Task 6: Dependency + infra env wiring (B3c) + go-live notes

**Files:**
- Modify: `services/model-router/requirements.txt`
- Modify: `infrastructure/modules/container-apps/hermes.tf` (router sidecar container block)

- [ ] **Step 1: Add the OTel distro dependency**

In `services/model-router/requirements.txt`, append:

```
azure-monitor-opentelemetry>=1.6.0
```

- [ ] **Step 2: Verify the import is runtime-only (no default-image behavior change)**

Run: `cd services/model-router && python -m pytest tests/ -q`
Expected: PASS — tests don't import the distro (it's only imported inside `_init_tracer`, which the tests monkeypatch), so the suite stays green without the package installed.

- [ ] **Step 3: Add the two env vars to the router sidecar (B3c)**

In `infrastructure/modules/container-apps/hermes.tf`, locate the **router sidecar** `container` block (the second container in the hermes pod — the one running the model-router image, NOT the hermes app container). Add to its `env` list:

```hcl
    env {
      name  = "OBSERVABILITY_ENABLED"
      value = var.observability_enabled ? "true" : "false"
    }
    env {
      name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
      value = var.app_insights_connection_string
    }
```

If `var.observability_enabled` does not yet exist, add it to the module's `variables.tf`:

```hcl
variable "observability_enabled" {
  description = "Emit GenAI-semconv spans from the model-router to App Insights."
  type        = bool
  default     = false
}
```

and pass it from the dev environment composition (`infrastructure/environments/dev/main.tf`) where the other hermes module inputs are set: `observability_enabled = var.observability_enabled`, with a matching `variable "observability_enabled" { type = bool, default = false }` in that environment's `variables.tf`. `var.app_insights_connection_string` is already a module input (used by the hermes app container) — reuse it.

- [ ] **Step 4: Validate the Terraform**

Run: `cd infrastructure/environments/dev && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

```bash
git add services/model-router/requirements.txt infrastructure/modules/container-apps/hermes.tf infrastructure/modules/container-apps/variables.tf infrastructure/environments/dev/main.tf infrastructure/environments/dev/variables.tf
git commit -m "feat(model-router): OTel dep + router-sidecar observability env wiring (B3c)"
```

- [ ] **Step 6: Operator go-live (manual, not a code step)**

After merge + deploy with `observability_enabled = true`:
1. Confirm the router sidecar has the env: `az containerapp show ... --query "properties.template.containers[?name=='model-router'].env"`.
2. Drive one model call through an agent, then in App Insights run KQL:
   `dependencies | where name == "gen_ai.chat" | project timestamp, customDimensions`
   Expect a span with `gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens/cost_usd`, `agent.tier`.
3. Sanity: Σ `gen_ai.usage.cost_usd` over a run ≈ that run's `record_cost` total (check `/budget` or the `_spend` value via the status endpoint).

---

## Self-Review

**Spec coverage** (against §2.1 of the v1.3 design):
- `genai_semconv_attrs` (genericized `agent.tier`/`agent.run_id`) → Task 1. ✅
- `observe_genai` (flag-gated, lazy, fail-open) + `_init_tracer` (distro + swap point) → Task 2. ✅
- Widened `record_cost` + single-chokepoint hook → Task 3. ✅
- Anthropic list-price estimator → Task 4. ✅
- Wire both cost paths / close the Anthropic gap (`main.py:1062-1069`) → Task 5. ✅
- Dependency + router-sidecar env (`hermes.tf`) + go-live KQL → Task 6. ✅
- Tests: ported observability tests + estimator tests + extended budget test → Tasks 1–5. ✅
- Fail-open + BatchSpanProcessor + content-redaction + `run_id` as attribute → covered by `observe_genai` design (Task 2) and the redaction-by-construction in `genai_semconv_attrs` (no text fields). ✅

**Placeholder scan:** none — every code/test step has complete content.

**Type consistency:** `record_cost` keyword params (`model`, `input_tokens`, `output_tokens`, `run_id`) are consistent across Tasks 3 and 5; `genai_semconv_attrs`/`observe_genai`/`_estimate_anthropic_cost`/`_usage_from_result` signatures match between definition and call sites. Span name `"gen_ai.chat"` is consistent between Task 2 (emit) and Task 6 (KQL).
