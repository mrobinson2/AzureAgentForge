# Roster Cost Gate

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md). For the two cost layers this gate completes, see [`services/model-router/budget_enforcement.py`](../../services/model-router/budget_enforcement.py) and [`docs/design/watchdog-agent-ops-alerts.md`](watchdog-agent-ops-alerts.md).

A CI check that sums every agent's committed cost ceiling, projects the sum
across a month, and fails the build if that projection exceeds the
platform's committed monthly cap. It runs at config review — a PR that adds
an agent or raises a budget — not on the invoice.

## 0. The failure mode this closes

AAF already has two cost-control layers, and both are scoped to a single
agent or a single model tier:

1. **Runtime enforcement** — `budget_enforcement.py` acts when *one tier*
   (say `gpt4o-mini`) exhausts its `*_DAILY_BUDGET_USD`: warn, downgrade to
   a fallback, or block. It reacts to spend that has already happened,
   against one number.
2. **Detection** — the watchdog's `detect_spend_burn_rate`
   (`services/watchdog/detectors.py`) flags *one agent* pacing over its
   monthly cap before the billing period ends. It also reacts to spend
   that's already happening, against one agent's number.

Neither layer ever adds the roster up. Every agent in
[`agents/profiles/*.yaml`](../../agents/profiles/) declares a
`daily_budget_usd` ceiling — how much that agent is *allowed* to cost per
day. The platform separately commits to a
`PLATFORM_MONTHLY_BUDGET_USD` in [`docker-compose.yml`](../../docker-compose.yml)
— how much the whole platform is allowed to cost per month. Nothing checked
whether the first number, summed across all fourteen agents and projected
across a month, could even fit inside the second. Two numbers, living in two
different files, edited independently, with no shared reviewer and no
automated check that they still agree.

That's the actual defect class: not overspending, but **config drift between
two commitments that were never cross-checked.** Add a fifteenth agent with
a generous ceiling, or trim the platform cap during a budget review, and
nothing before this gate would have noticed the two numbers no longer add
up. The roster cost gate is the reconciliation step — it runs at PR time,
where the fix is a decision (raise the cap, or lower a ceiling), not a
post-mortem.

## 1. Ceilings, not a forecast

This is the distinction the gate's own output insists on, because it's easy
to misread a `FAIL` as "the platform is overspending":

- `daily_budget_usd` is a **ceiling** — the most an agent is configured to
  be allowed to spend in a day, enforced at runtime by the model router.
- An agent essentially never spends its full ceiling every single day. Real
  spend is well under the sum of ceilings, almost always.
- The gate sums ceilings and asks a hypothetical: *if every agent
  simultaneously spent its full daily ceiling for 30 days straight, would
  the total fit under the platform's committed monthly cap?*

A `FAIL` means that hypothetical doesn't hold — the configured ceilings and
the configured cap are inconsistent with each other. It says nothing about
today's actual bill. That's a deliberate, narrower claim than a spend
forecast, and it's why the gate can run at config time with no billing data
at all: it only ever reads two files.

## 2. Config surface: where the two numbers actually live

The gate reads the same files a deployment actually loads — no parallel
manifest.

| Number | Lives in | Read by |
|---|---|---|
| Per-agent ceiling | `daily_budget_usd` field in each `agents/profiles/<role>.yaml` | `scripts/roster-cost-gate.py`, `agents/validate_profiles.py` (schema) |
| Platform cap | `PLATFORM_MONTHLY_BUDGET_USD` under the `x-roster-cost-gate` block in `docker-compose.yml` | `scripts/roster-cost-gate.py` |

**Why `agents/profiles/*.yaml` and not somewhere new.** AAF already has a
canonical, schema-validated, per-agent config surface —
[`agents/profile.schema.json`](../../agents/profile.schema.json) governs
`name`, `role`, `description`, `model_tier`, `toolsets`, `reports_to` for
every shipped profile, checked by `agents/validate_profiles.py` and
`agents/tests/test_profiles.py` in CI. `daily_budget_usd` is added to that
schema as an optional numeric field (minimum `0`) rather than inventing a
second per-agent file. Every shipped profile now declares one.

**Why a new `x-roster-cost-gate` block instead of reusing
`x-router-models`.** `docker-compose.yml` already has an `x-router-models`
anchor merged (`<<: *router-models`) into the model-router service's
environment — that's the anchor that feeds a *running container*
(`GPT4O_DAILY_BUDGET_USD`, `PHI_DAILY_BUDGET_USD`, `BUDGET_ENFORCE_MODE`,
…). `PLATFORM_MONTHLY_BUDGET_USD` is deliberately **not** merged into any
service: nothing at runtime reads it, and merging it into the router's env
would misleadingly imply the router enforces it. It lives in its own
`x-roster-cost-gate` block instead — a config-time-only value, consumed
only by this gate. Also documented in `.env.example` next to the acting
budget-enforcement vars, since that's where an operator setting the real
value will look.

**A near-miss worth naming.** The watchdog already has a per-agent monthly
cap concept — `DEFAULT_CAPS` in `services/watchdog/watchdog.py`, a hardcoded
Python dict (`{"Orchestrator": 15.00, "Researcher": 7.50}`) used only to
feed `detect_budget_anomaly` and `detect_spend_burn_rate`. It is a second,
narrower, *runtime-only* declaration of roughly the same idea, and it
covers 2 of the 14 shipped agents. It is deliberately **not** the surface
this gate reads: it's Python source, not schema-validated config, it isn't
complete, and it exists to catch one agent's spend pacing after the fact —
a different job from reconciling the whole roster's config-time ceiling
against a platform commitment. If you're extending agent-level cost caps,
`agents/profiles/*.yaml` is the one to edit; `DEFAULT_CAPS` is watchdog-internal
and out of scope for this gate.

## 3. Exit-code contract

```
0   pass    roster's 30-day ceiling sum <= platform cap
1   fail    roster's 30-day ceiling sum >  platform cap
2   fatal   config missing, unreadable, or malformed -- no verdict possible
```

Exit `2` is deliberately distinct from `1`: a missing profiles directory, an
unparseable YAML file, a profile with no `daily_budget_usd` at all, or a
`docker-compose.yml` with no `PLATFORM_MONTHLY_BUDGET_USD` under
`x-roster-cost-gate` are all config problems the gate cannot render a
pass/fail verdict for. Treating them as a silent pass (skip the file) or a
silent `$0` ceiling would hide exactly the kind of drift this gate exists
to catch, so they're fatal instead.

Every run also prints one machine-parsable summary line first, followed by
a human-readable table:

```
[roster-cost-gate] OK sum=$183.00 cap=$200.00 headroom=$17.00 agents=14
[roster-cost-gate] FAIL sum=$69.00 cap=$50.00 delta=$19.00 agents=3
[roster-cost-gate] FATAL: <reason>
```

## 4. Worked examples

**Pass — the real, shipped roster.** Fourteen agents, tiered
`economy`/`standard`/`frontier`, sum to $6.10/day. Projected across 30 days:

```
$ python scripts/roster-cost-gate.py
[roster-cost-gate] OK sum=$183.00 cap=$200.00 headroom=$17.00 agents=14
[roster-cost-gate] 14 agents' 30-day daily_budget_usd ceiling sums to $183.00,
within PLATFORM_MONTHLY_BUDGET_USD $200.00 ($17.00 headroom).
```

**Fail — a roster that outgrew its cap.** From
`tests/roster-cost-gate/fixtures/fail/`: three agents at $1.00, $0.80, and
$0.50/day ($2.30/day -> $69.00/mo) against a $50.00/mo cap:

```
$ python scripts/roster-cost-gate.py \
    --profiles-dir tests/roster-cost-gate/fixtures/fail/profiles \
    --compose-file tests/roster-cost-gate/fixtures/fail/docker-compose.yml
[roster-cost-gate] FAIL sum=$69.00 cap=$50.00 delta=$19.00 agents=3

[roster-cost-gate] roster's 30-day ceiling sums to $69.00, exceeding
PLATFORM_MONTHLY_BUDGET_USD $50.00 by $19.00.

  These are ceilings, not a spend forecast -- this does not mean the
  platform is currently overspending. It means the roster's combined
  per-agent ceilings and the platform's committed monthly cap are
  configured inconsistently, which nothing else in the deploy path
  checks.

  Top contributors (by daily_budget_usd, descending):
    - agent-a (AgentA): $1.00/day -> $30.00/mo
    - agent-b (AgentB): $0.80/day -> $24.00/mo
    - agent-c (AgentC): $0.50/day -> $15.00/mo

  Fix one of the two: raise PLATFORM_MONTHLY_BUDGET_USD in docker-compose.yml, or lower
  one or more daily_budget_usd values in agents/profiles/*.yaml, until
  the roster's 30-day ceiling sum is <= the platform cap.
```

The fix is a decision, not a re-derivation: someone has to choose whether
the platform commitment was too low or the roster's ceilings were too high
— the gate hands them the arithmetic instead of making them redo it.

**Fatal — a profile with no ceiling declared.** From
`tests/roster-cost-gate/fixtures/missing-field/`, a profile missing
`daily_budget_usd` entirely:

```
$ python scripts/roster-cost-gate.py \
    --profiles-dir tests/roster-cost-gate/fixtures/missing-field/profiles \
    --compose-file tests/roster-cost-gate/fixtures/missing-field/docker-compose.yml
[roster-cost-gate] FATAL: no-budget.yaml: missing 'daily_budget_usd' -- every
agent profile must declare a per-agent cost ceiling for the roster cost gate
to sum it. Add e.g. 'daily_budget_usd: 0.20' and re-run.
```

## 5. Where this sits in AAF's cost story

Three layers, three different moments:

- **Runtime enforcement** (`budget_enforcement.py`, circuit breakers, kill
  switch) — acts while a request is in flight, against one tier's budget.
- **Detection** (watchdog spend burn-rate) — notices after the fact that one
  agent is pacing over its cap, before the billing period ends.
- **Config-time prevention** (this gate) — catches, before any of that ever
  runs, that the roster's combined ceilings and the platform's committed
  cap were never consistent with each other in the first place.

The first two answer "is this agent behaving?" The roster cost gate answers
a question neither of them asks: "even if every agent behaved exactly as
configured, does the math still work?"

## 6. Tests

`tests/roster-cost-gate/test_roster_cost_gate.py` (pytest) covers the pure
functions (`compute_monthly_sum`'s 30-day projection,
`parse_platform_monthly_cap`'s two accepted forms, `format_fail`'s delta and
contributor ranking), the CLI against static fixtures in
`tests/roster-cost-gate/fixtures/` (pass, over-cap fail, malformed YAML,
missing-field), and one integration test that runs the CLI against the real
`agents/profiles/*.yaml` + `docker-compose.yml` and asserts it currently
passes — documenting, in a test, that the shipped roster fits its committed
cap.

```
pytest -q tests/roster-cost-gate
```

## 7. Adding or re-budgeting an agent

See [`agents/README.md` § Adding an agent](../../agents/README.md#adding-an-agent)
step 4: set `daily_budget_usd`, then re-run
`python scripts/roster-cost-gate.py` before opening the PR. CI runs the same
command (`.github/workflows/roster-cost-gate.yml`), scoped to PRs touching
`agents/profiles/**`, `agents/profile.schema.json`, `docker-compose.yml`, the
gate script, or its tests.
