# Watchdog

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

Platform self-watch: detects failure signatures across run results +
`agent_events`, files deduped issues with evidence, and (optionally) writes the
failure as a governed memory lesson so the agent that keeps hitting it gets
reminded. Design: [`docs/design/memory-system.md`](../../docs/design/memory-system.md)
(the self-improvement loop).

## Run

```bash
# Gated: does nothing unless feature_flags.AGENT_EVENTS_ENABLED is true.
export WATCHDOG_COMPANY_ID=<company-uuid>
export WATCHDOG_JWT=$(...)            # admin/automation JWT
export AGENT_EVENTS_DSN=postgresql://.../honcho?sslmode=require
python -m services.watchdog.watchdog   # one pass — wire to a 10-min cron
```

## Layout

| File | Role | Tested |
|---|---|---|
| `detectors.py` | pure failure-signature library | ✅ offline |
| `filer.py` | Finding → PaperClip issue (camelCase) | ✅ offline (stub poster) |
| `memory.py` | Finding → governed durable_fact lesson | ✅ offline (stub poster) |
| `scorecards.py` | per-agent delegation track records | ✅ offline |
| `attribution.py` | success → earned-trust attribution | ✅ offline |
| `roster.py` | display name ↔ live agent slug | ✅ offline |
| `watchdog.py` | orchestrator: fetch → detect → dedup → file | I/O glue |

## Tests

```bash
python -m pytest services/watchdog/tests -q   # offline — no network/DB
```

## Adding a detector

A detector is a pure `fn(runs, events, ...) -> list[Finding]`. Add one when a
new failure class is worth surfacing to an operator; describe it in its
docstring; register it in `run_detectors()`; add a test with a synthetic window.

## Agent Ops Alert Pack

Three detectors aimed squarely at the failure modes agent platforms are
famous for — a stuck agent that never notices it's stuck, a router that
degrades quietly, and a budget that blows past the operator before the
invoice does. Full design (signal choice, data source, false-positive
posture) in [`docs/design/watchdog-agent-ops-alerts.md`](../../docs/design/watchdog-agent-ops-alerts.md).
All three are enabled-but-observe by default: they only add `Finding`s to the
same PaperClip issue-filing path every other detector uses — no enforcement,
no separate alert channel.

| Detector | Fires when | Configure via |
|---|---|---|
| `detect_run_loop` | An agent re-runs the same issue >= N times, or churns (crash-stops) across many issues | `RUN_LOOP_MAX_RUNS_PER_ISSUE`, `RUN_LOOP_CHURN_RATIO`, `RUN_LOOP_MIN_RUNS` |
| `detect_model_degradation` | An agent's calls are sustainedly served by a model other than the one configured for it | `WATCHDOG_EXPECTED_MODELS`, `MODEL_DEGRADATION_MIN_CALLS`, `MODEL_DEGRADATION_THRESHOLD` |
| `detect_spend_burn_rate` | Current spend, projected across the billing period, is on pace to blow past the cap | `SPEND_BURN_RATE_ENABLED`, `BURN_RATE_WINDOW_HOURS`, `BURN_RATE_PACE_MULTIPLIER`, `BURN_RATE_CRITICAL_PACE_MULTIPLIER`, `BILLING_PERIOD_DAYS` |

Run-loop and model-degradation compose into `run_detectors()` like the
existing detectors, on the standard polling window. Burn-rate is called
directly from `watchdog.py` (like `detect_expiring_secrets` /
`detect_research_backends`) because it needs its own longer, smoothed runs
fetch — a 30-minute window massively over-projects any ordinary burst. See
`.env.example` for the full variable list with defaults and examples.
