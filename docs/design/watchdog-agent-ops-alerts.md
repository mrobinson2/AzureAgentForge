# Watchdog — Agent Ops Alert Pack

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md). For the watchdog itself, start at [`services/watchdog/README.md`](../../services/watchdog/README.md).

Three detectors added to the platform watchdog, aimed at the failure modes
agent platforms are famous for and that generic infra monitoring does not
catch: an agent that loops on itself without anyone noticing, a router that
quietly serves the wrong model, and a budget that is on pace to blow past its
cap before the period ends. All three are pure functions in
[`services/watchdog/detectors.py`](../../services/watchdog/detectors.py),
tested offline in
[`services/watchdog/tests/test_detectors.py`](../../services/watchdog/tests/test_detectors.py),
and wired into the same `Finding` → PaperClip-issue path every other
detector in this repo already uses. There is no second alert channel and no
enforcement action — see §5.

## 0. Why these three, and why now (for people new to agentic ops)

Traditional infra monitoring watches processes, containers, and endpoints:
is it up, is it responding, is the disk full. An agent platform can pass
every one of those checks while quietly doing something expensive and
useless — a container that's up, a process that's responding, runs that
exit 0, an issue that stays "in progress" for days. The thing that's broken
is a *behavior*, not a *resource*, so a resource monitor never sees it. Three
behaviors show up again and again once an agent platform is running real
workloads:

1. **Runaway run-loops.** An agent (or the orchestration logic managing it)
   keeps re-attempting the same unit of work without ever landing it. Every
   individual run can look fine in isolation — it starts, it does something,
   it ends — so nothing pages. The bill is the first thing that notices.
2. **Silent model degradation.** A router falls back from a preferred model
   to a cheaper or more available one when the preferred one can't be
   reached. That's *correct* behavior — a call should degrade gracefully
   rather than fail outright. The failure is that the fallback goes
   unnoticed: it typically logs at a level nobody reads, so the "model I
   configured" table and the "model that actually answered" table quietly
   diverge. Depending on direction, that's either a cost problem (cheap
   model requested, pricier one served) or a quality problem (capable model
   requested, weaker one served) — and either way the operator finds out
   from the invoice or from a user complaint, not from a dashboard.
3. **Spend burn-rate.** A budget cap is usually enforced as a hard stop:
   fine for preventing catastrophe, useless for prevention. By the time a
   cap trips, the spend already happened. An operator wants to know *today*
   that this month is on pace to land 3x over, not learn it when the cap
   fires on the 12th.

None of these are exotic — they're the direct, generalized descendants of
real incidents that happen to any team running agents against a metered
model API with delegation/retry logic. This alert pack gives them a name, a
signal, and a place in the existing alerting path, so they're loud on day
one instead of at invoice time.

## 1. Data sources — what each detector actually reads

The watchdog's contract with the rest of the platform is narrow on purpose
(see `services/watchdog/README.md`): pull a window of run results from the
PaperClip API and a window of rows from `agent_events`, run pure detectors
over them, file issues for what's fresh. All three new detectors stay
inside that contract rather than adding a new data source, a new poller, or
a new credential:

| Detector | Reads | Fields used |
|---|---|---|
| `detect_run_loop` | `runs` (standard window) | `agentName`/`agentId`, `issueId`/`issue_id` (optional), `stopReason`, `id` |
| `detect_model_degradation` | `runs` (standard window) | `agentName`/`agentId`, `model`, `cost_usd` (optional, for evidence only) |
| `detect_spend_burn_rate` | `runs` (its own longer window — see §3) | `agentName`, `cost_usd` |

Two deliberate choices worth calling out:

- **`issueId` is read opportunistically.** The run-result shape documented
  in `detectors.py` (`{id, agentId, agentName, status, stopReason, result,
  startedAt, finishedAt, model}`) doesn't guarantee an issue linkage field,
  and whether a given PaperClip deployment's runs API includes one can vary.
  `detect_run_loop` reads `issueId` then `issue_id` and falls back to a
  `"(none)"` bucket rather than requiring the field or dropping runs that
  lack it — the per-issue signal gets sharper when it's present, and the
  agent-wide churn-ratio signal still catches the loop when it isn't.
- **Model degradation does not need a new event type.** A tempting design
  would have the model-router stamp a `model_routed` event into
  `agent_events` (mirroring how the governor already stamps `ranking_mode`
  into `memory_injected` for the trigram-fallback detector) — richer, but it
  means shipping a matching change to `services/model-router`, which is out
  of scope for a watchdog-only change and turns one detector into a
  two-service feature. Every run result already carries a `model` field:
  the model that actually served it. Comparing that against an
  operator-supplied "expected model per agent" map gets the same signal
  from data the watchdog already has in hand every tick. A future PR wiring
  the router to emit a dedicated event would give the detector more
  granularity (per-call reason codes, not just per-run drift) — worth doing,
  not required to ship the alert.

## 2. Runaway run-loop

**Signal.** Two independent triggers, because a loop can take either shape:

- **Raw count** — the same `(agent, issue)` pair produces
  `RUN_LOOP_MAX_RUNS_PER_ISSUE` (default **8**) or more runs inside one
  detection window. Catches a loop pinned to a single unit of work.
- **Churn ratio** — an agent with `RUN_LOOP_MIN_RUNS` (default **10**) or
  more runs in the window, where `RUN_LOOP_CHURN_RATIO` (default **0.6**,
  i.e. 60%) or more of them end in a crash-class `stopReason`
  (`adapter_failed`/`error`/`timeout` — the same set
  `detect_adapter_failures` already uses). Catches a loop spread thin
  across many issues, or one where `issueId` isn't available at all.

**Threshold rationale.** 8 runs on one issue and a 60% crash rate across 10+
runs are both comfortably past "an agent retried once after a flaky call" —
that's normal and shouldn't page anyone. They're conservative enough to
need real, repeated churn before firing, and both are env-configurable for
platforms with a different normal (a chatty multi-step agent's baseline run
count is not the same as a single-shot researcher's).

**Alert payload (`Finding.evidence`).**

```jsonc
// raw-count trigger
{"agent": "Orchestrator", "issue": "ISSUE-42", "run_count": 11,
 "crash_stop_count": 6, "run_ids": ["r1", "r2", "..."]}

// churn-ratio trigger
{"agent": "Orchestrator", "run_count": 14, "crash_stop_count": 9,
 "churn_ratio": 0.643}
```

**Severity.** `critical` for the raw-count trigger (a concentrated loop on
one issue is the shape that produced the worst real-world incidents this
pack is modeled on — pinned, repeated, invisible until the bill). `high`
for the churn-ratio trigger (a broader but softer signal).

**Owner.** `Orchestrator` — the retry/continuation logic deciding whether
to re-attempt a unit of work is the thing to fix, not any one agent.

**False-positive posture.** The two-signal design exists specifically to
avoid firing on ordinary retry behavior: a single transient failure and
retry never reaches either threshold. The dominant remaining false-positive
risk is a legitimately long-running, multi-step agent whose normal workflow
touches one issue many times (e.g., an agent that iterates a document
draft) — operators running that pattern should raise
`RUN_LOOP_MAX_RUNS_PER_ISSUE` for that deployment rather than the detector
guessing at intent.

## 3. Silent model degradation

**Signal.** For each agent listed in `WATCHDOG_EXPECTED_MODELS`, the share
of that agent's runs (with a recorded `model`) served by something other
than the expected model. Fires when both:

- `MODEL_DEGRADATION_MIN_CALLS` (default **5**) or more calls with a
  recorded model exist for the agent in the window — enough volume that one
  fallback can't trip it, and
- the mismatch rate is `MODEL_DEGRADATION_THRESHOLD` (default **0.3**, i.e.
  30%) or higher.

**Configuration.** `WATCHDOG_EXPECTED_MODELS` is a JSON object mapping
`agentName`/`agentId` to either a single expected model id or a list of
acceptable ids (a tier with more than one interchangeable deployment):

```bash
WATCHDOG_EXPECTED_MODELS={"Researcher":"deepseek-v4-flash","Coder":["gpt4o-mini","phi4"]}
```

Left at its default (`{}`), the detector is a **documented no-op** — the
same posture `detect_budget_anomaly` already has with an empty
`agent_caps`. It runs every tick either way (see §5); it just has nothing
to compare against until an operator states intent.

**Alert payload.**

```jsonc
{"agent": "Researcher", "expected_models": ["deepseek-v4-flash"],
 "top_served_model": "gpt-5.4-mini", "mismatch_count": 7,
 "total_calls": 10, "mismatch_rate": 0.7,
 "mismatch_spend_usd": 7.0}   // present only when cost_usd was recorded
```

**Severity.** `high` — a diverging model table is a router/config problem
worth same-day attention, but (unlike a secret that's already expired) it
isn't yet a hard outage.

**Owner.** `Infrastructure` — the fix is on the router/deployment side
(quota, a dead endpoint, a de-registered model), not the agent.

**False-positive posture.** The 30%-of-5+-calls default tolerates
occasional legitimate fallback (a brief upstream blip that recovers) without
firing, while still catching the sustained divergence that actually costs
money or quality. Both the volume floor and the rate threshold are
per-deployment tunable because "normal" fallback rate depends entirely on
how many tiers a deployment runs and how aggressively it's configured to
degrade.

## 4. Spend burn-rate

**Signal.** Per-agent spend over a smoothed window, projected out to a full
billing period, compared against that agent's cap (the same
`{agentName: monthly_cap_usd}` map `detect_budget_anomaly` already takes —
one cap config, two detectors). Fires when the projected period spend is
`BURN_RATE_PACE_MULTIPLIER` (default **2.0x**) or more of the cap;
`critical` instead of `high` at `BURN_RATE_CRITICAL_PACE_MULTIPLIER`
(default **4.0x**).

**Why this is a different detector from `detect_budget_anomaly`, not a
tweak to it.** The existing budget detector checks a running total against
a ratio of the cap — "you've already spent 90% of your budget." That's a
near-miss alarm; it's necessarily late, because the number it's watching
only grows to 90% after 90% of the money is already gone. Burn-rate instead
asks "at the rate you're spending *right now*, where do you land by the end
of the period" — it can fire in the first week of a 30-day period, while
there's still time to act, on an agent that has spent very little in
absolute terms but is spending it fast.

**Why the window has to be smoothed, not the platform's normal poll tick.**
This is the single most important operational lesson behind this detector's
design: **a short window makes an ordinary burst indistinguishable from a
runaway loop.** A backlog-recovery run, a manual batch replay, a burst of
legitimate work — any of them can spend real money in 30 minutes. Projected
naively (`spend_in_30min × 48 half-hours/day × 30 days`), that burst reads
as a wildly over-budget runaway even though the underlying steady-state
rate is nowhere near it. The fix is not a smarter threshold, it's a longer
window: `detect_spend_burn_rate` takes an explicit `window_hours` and
**does not know or care** what the platform's regular polling cadence is —
it trusts whatever window the caller hands it. `watchdog.py` deliberately
does **not** feed it the standard `WATCHDOG_WINDOW_MIN` (default 30 min)
window; it performs its own separate fetch at `BURN_RATE_WINDOW_HOURS`
(default **24h**) each tick, the same way it already does a separate,
longer fetch for track-record scorecards. A day of smoothing absorbs a
half-hour burst while still catching a burn rate that's genuinely elevated
for hours at a stretch.

**Alert payload.**

```jsonc
{"agent": "Orchestrator", "window_hours": 24.0, "spend_in_window_usd": 14.20,
 "projected_period_spend_usd": 213.0, "cap_usd": 100.0,
 "pace_multiplier": 2.13, "period_days": 30, "eta_hours_to_cap": 169.0}
```

`eta_hours_to_cap` is "at this rate, how long until the *entire* cap is
gone" — a simple, always-available number that doesn't require the watchdog
to separately track month-to-date spend. It's deliberately not
"days-remaining-in-the-period accounting"; a full month-to-date ledger is a
reasonable future enhancement (see §7) but isn't needed for the pace signal
to be useful today.

**Severity.** `high` at 2x pace, `critical` at 4x pace — the same two-tier
shape `detect_expiring_secrets` uses (soon vs. already-a-problem), applied
to spend instead of time.

**Owner.** `CostGuardian` — reusing the owner label `detect_budget_anomaly`
already introduced for cost-lane findings, rather than inventing a second
one.

**False-positive posture.** The whole design is oriented around not paging
on a legitimate burst — that's what the smoothed window is *for*. The
remaining false-positive case is a deployment whose real usage is
intentionally front-loaded in the billing period (e.g., a monthly batch job
that spends most of its budget in the first three days by design); that
shape genuinely is "2x pace" by this detector's math and there's no way to
distinguish it from a real runaway without knowing intent, so those
deployments should set `BURN_RATE_PACE_MULTIPLIER` higher or disable the
detector (`SPEND_BURN_RATE_ENABLED=false`) rather than have the detector
guess.

## 5. Wiring into the existing alert path (no new channel)

Every `Finding` these detectors produce flows through the exact same path
every other detector in this file already uses — `watchdog.py`'s
`run_detectors()` → `detectors.dedup()` → `filer.file_finding()` → a
PaperClip issue, tagged `[watchdog]`, deduped by `Finding.dedup_key()`, with
the finding's `severity`/`recommended_owner`/`evidence` in the issue body.
Nothing in this pack introduces a second notification path, a webhook, or a
different dedup store. If the platform's self-improvement loop
(`GOVERNOR_*`) is configured, each finding's `subject_agent`/`lesson` also
flows through the existing `memory.write_lesson` path exactly like the
current detectors' lessons do — an agent that keeps tripping the run-loop
or model-degradation detector gets that fact re-injected into its own
planning context, the same mechanism that already closes the loop for
adapter failures and budget anomalies.

`detect_run_loop` and `detect_model_degradation` are pure functions of the
window already fetched every tick, so they're composed directly into
`detectors.run_detectors()` alongside the existing always-on detectors.
`detect_spend_burn_rate` needs its own longer fetch (§4), so — like
`detect_expiring_secrets` and `detect_research_backends` before it —
`watchdog.py` calls it directly rather than routing it through
`run_detectors()`.

## 6. Posture: alert, never enforce

All three detectors are **enabled-but-observe** by default. They add
findings; they do not pause an agent, cancel a run, roll back a deployment,
or otherwise take action. That mirrors every existing detector in this
service and is a deliberate, not incidental, choice: a detector that acts on
a false positive is worse than one that's merely noisy, and "which agent
should be stopped" is an operator decision that depends on context the
watchdog doesn't have. Automated enforcement on top of these signals (e.g.,
an operator-approved auto-pause after N consecutive `critical` run-loop
findings) is a reasonable follow-on but is explicitly out of scope here.

## 7. Known gaps / follow-ups

- **Model degradation compares per-run, not per-call.** A run that makes
  several model calls is represented by one `model` field on the run
  result, so a run that mixes expected and fallback calls internally is
  scored by whatever `model` the run record reports, not a per-call
  breakdown. A router-level event (see §1) would fix this at the cost of a
  cross-service change.
- **Burn-rate's ETA is rate-only, not ledger-aware.** `eta_hours_to_cap`
  answers "how long until the whole cap is gone at this rate," not "how
  long until *this month's remaining* budget is gone" — it doesn't know
  month-to-date spend, only the window it was given. Wiring in a real
  month-to-date figure (the way `scorecards.py` already pulls a longer
  historical window for track-record) would sharpen the ETA; the pace
  signal itself doesn't need it.
- **Run-loop's `issueId` fallback is coarse.** Deployments whose PaperClip
  runs API doesn't expose an issue linkage field only get the agent-wide
  churn-ratio signal, not the sharper per-issue one. Confirming the field
  name against a live deployment and adjusting the read order is a small,
  low-risk follow-up.
- **No dashboard/trend view.** All three detectors are point-in-time
  (per-tick) checks like their neighbors; there's no persisted history of
  fallback rate or burn pace over time beyond what lands in filed issues.
  A trend view would help operators distinguish "this just started" from
  "this has been creeping for a week," but is a larger, separate feature.
