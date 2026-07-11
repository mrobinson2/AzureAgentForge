# Skill: Business AI-Opportunity Assessment

- **Slug:** `business-ai-opportunity-assessment`
- **Used by:** Business and Strategy roles (intake by a customer-facing voice or web-form agent; scoring and synthesis by the Business role)
- **Toolsets:** terminal, file
- **Trust tier:** Customer-facing sandbox (intake) → High-Trust internal (synthesis)

## Purpose

Run a structured AI-opportunity assessment for a small business, then turn the
captured answers into a **scorecard** and a **written recommendation** the
operator can act on. The method separates two jobs on purpose:

1. **Capture** — a short, structured intake (voice or form) that collects raw
   operational facts and never does arithmetic on the call.
2. **Synthesize** — a set of deterministic post-intake prompts that score the
   business across fixed dimensions, model ROI as ranges, and emit one
   machine-readable deliverable the client keeps.

The differentiator is the hand-off: the deliverable is written so it can be
loaded directly as an AI agent's system context on day one. The sales artifact
becomes the product's brain.

## When to use

- A prospect asks "where could AI actually help my business?" and you want a
  repeatable, defensible answer rather than a vibe.
- You are qualifying inbound leads and need a consistent scorecard to compare
  prospects.
- You are producing a paid operational diagnostic and need the intake to be
  rich enough to support a real recommendation.

Do **not** use this to quote a price or promise a capability during intake —
those are the operator's decisions, made after synthesis (see Guardrails).

## Inputs

| Input | Source |
|---|---|
| Intake channel | A voice-agent platform or a web form. Either drives the same question set. |
| `assessment_record` | The structured JSON the intake produces (schema below). |
| `HONCHO_USER_PEER_ID` / memory scope | Optional. On tenant conversion, durable facts seed the tenant's memory via `pc-memory record`. |
| Vertical pack (optional) | A per-industry question overlay matched from the business description (e.g. `field-services`, `legal`, `real-estate`). |

## Procedure

### Part 1 — Capture (the intake)

Ask the questions **in order**. If the caller answers a later question early,
capture it and return to the next un-asked question — do not lose data. Each
question names what to capture.

**Quick intake (~8 min, qualifying wedge)** — seven questions:

1. **Business description** — what the business does, who its customers are.
   `business_description` (free text).
2. **Company size** — headcount including the owner. `headcount` (integer;
   best-effort extraction, "just me" → 1).
3. **Current AI usage** — any tools tried, even casually. `current_ai_usage`
   plus a derived enum `current_ai_maturity ∈ {none, casual, purposeful, embedded}`.
4. **Top time-eater** — the one thing that eats the most time. `top_pain_point`.
5. **Current handling** — how they do that today. `current_handling`.
6. **Budget band** — discrete buckets reduce friction:
   `budget_bucket ∈ {under_5k, 5k_to_25k, 25k_to_100k, over_100k, unknown}`.
7. **Follow-up contact** — email + phone. `follow_up_email`, `follow_up_phone`.

**Deep intake (~20 min, paid diagnostic)** — ten phases that expand the above
into a real operational map. The load-bearing phases:

- **Framing & trust** — disclose the agent is an AI, set expectations, capture
  caller identity. Add the *outcome-coaching beat*: tell the caller to describe
  the **result** they want, not how it would be automated. ("You handle the
  what; the how is our job.") This reframes every later answer toward outcomes.
- **Business shape** — tenure, headcount, caller's role (`owner_operator`,
  `founder_passive`, `manager`, `partner`). Role determines who has authority
  to act on the assessment.
- **Workflow discovery** — force a concrete list of the **top 3–5 time sinks**.
  Do not accept "everything is busy"; probe until each is a named, 3–7-word
  process. Redirect one-off project work ("we built our website") — you want
  things that recur most weeks.
- **Per-task deep-dive** (the most valuable phase) — for each task capture:
  `trigger`, ordered `steps`, `owners`, `tools`, `friction_points`, and
  `scope ∈ {same_every_time, varies}`. Ask the scope question verbatim, once
  per task — it is the automation-generalizability signal. If a task runs long,
  capture the gist and move on; imperfect data beats a 35-minute call.
- **Quantification** — per task: `minutes_per_occurrence`, `occurrences_per_week`,
  `primary_doer`, `hourly_cost_usd`. **Pin the units** (minutes-per-occurrence,
  occurrences-per-week) — loose units are the number-one way ROI math silently
  goes wrong. Do **one mirror-back, no arithmetic** ("so that's roughly a full
  day a week of someone's time — sound right?") and move on. All math is
  post-call.
- **Pain & frustration** — capture the owner's own words on what is most
  frustrating / repetitive / what they'd stop doing. Quoted verbatim later.
- **Revenue & opportunity** — lead sources, whether leads are missed,
  after-hours handling, `average_ticket_usd`, and a `conversion_rate_estimate`.
  Without a close rate, missed-lead math pretends every missed call was a lost
  job — always ask, or record a stated default.
- **Tools & stack** — current stack **and** abandoned tools (with reasons). A
  second failed implementation of a tool the client already abandoned destroys
  trust — never recommend onto an abandoned tool without addressing why it
  failed.
- **Change readiness** — `tech_comfort` (1–10), `preference`
  (`plug_and_play` / `mid_custom` / `custom_ok`), the `champion` who'd own it,
  a `persona` preference (`by_the_book` / `judgment_calls`), and plain-language
  guardrails: what an assistant must never share (`guardrails.sensitive`) and
  what must never go out without human approval (`guardrails.approval_required`).
- **Close** — set the deliverable expectation (a written action plan, not a
  slide deck), capture any final notes, end.

Load an optional **vertical pack** after the business description is known: it
adds industry-specific tasks to probe, revenue-anchor questions, and auto-flag
triggers, all via prompt-context augmentation (no code branches per vertical).
If no pack matches, run the generic flow — the deliverable is one tier lower but
still useful.

### Part 2 — Synthesize (scoring + deliverable)

Run these prompts **in order** over the captured record. Three hard rules apply
to all of them:

- **Provenance or assumption — nothing else.** Every extracted fact and every
  number cites a verbatim quote, or is explicitly marked `assumption:` with its
  default stated.
- **Capability tiers, never model or vendor names.** Automation fit uses
  `high_volume_low_judgment`, `low_volume_high_judgment`, `human_required`. The
  router maps tiers to models at runtime; that mapping is yours, not the
  client's.
- **Ranges, not points.** Every dollar or count estimate is a low–high range
  with its driving assumptions printed beside it.

**Prompt A — Process Objects.** For each workflow the caller described, emit
one object: `name`, `trigger`, `sequence[]`, `ownership`, `tech_stack[]`,
`friction_points[]`, `scope`, `automation_fit` (one of the three tiers),
`provenance[]`. Extract only workflows actually described; do not invent steps.

**Prompt B — ROI model (units pinned, ranges only).** For each Process Object
with quantification:

```
annual_manual_cost = (minutes_per_occurrence / 60) × occurrences_per_week × 52 × hourly_cost_usd
```

Emit low/high bounds. If the caller gave a range, use it; otherwise apply a
±25% band labeled `assumption: ±25% band`. Where revenue and missed-lead signals
exist:

```
annual_missed_revenue = missed_calls_per_month × close_rate × average_ticket_usd × 12
```

Use the captured conversion rate when present, else `assumption: close_rate=0.25`.
Where `hourly_cost_usd` is null ("my time isn't the bottleneck"), switch to
opportunity-cost framing instead of a dollar figure.

**Prompt C — Opportunity map.** Rank opportunities. For each:
`pain` (the caller's words) → `solution_category` (a category, not a product —
e.g. call & lead capture, field-paperwork drafting, follow-up sequences,
scheduling assist) → `impact_range` (from Prompt B) → `effort_tier`
(days / weeks / months, calibrated to tech comfort and preference) →
`sequence_position`. Sequencing rule (wedge-first): lead-capture opportunities
rank before operations opportunities before everything else, unless the ROI gap
exceeds 5× the other way. Flag anything emotionally loaded — emotional pain
converts even when its dollar range is mid-pack.

**Prompt D — Scorecard + Master Context (the deliverable).** Score the business
across the fixed dimensions below (each 1–5, with the provenance that justifies
the score), compute an overall readiness signal, then synthesize one markdown
file the client keeps.

## Output format

### The scorecard

Score each dimension 1–5. Print the driving evidence beside every score.

| Dimension | What it measures | 1 (low) | 5 (high) |
|---|---|---|---|
| **AI maturity** | Where they are on the adoption curve | No tools used | AI embedded in a production workflow |
| **Process standardization** | Share of top tasks that run the same way every time | Every job is bespoke | Most tasks are `same_every_time` (automatable) |
| **Volume / frequency** | ROI surface area | Low-volume, occasional | High-volume, daily repetition |
| **Revenue leverage** | Speed-to-lead / missed-revenue upside | No lead leakage | Frequent missed leads × high average ticket |
| **Data & tooling readiness** | Whether the inputs exist to automate | Paper / undocumented | Structured systems already in place |
| **Change readiness** | Will they actually adopt it | Low tech comfort, no champion | High comfort, named champion, custom-OK |
| **Budget fit** | Realistic room to invest | `under_5k` / `unknown` | Funded and specific |

Derive an overall recommendation tier from the scorecard:

- **Strong fit** — high standardization + high volume + a champion + budget.
  Recommend a wedge automation now with a fast ROI proof.
- **Qualified** — real pain and volume but a readiness or budget gap. Recommend
  a scoped pilot on the single highest-ROI, most-standardized process.
- **Nurture** — genuine interest, low readiness or no automatable process yet.
  Recommend foundational steps (documenting one workflow) before automation.

### The Master Context deliverable

One markdown file, loadable directly as an AI agent's system context:

```markdown
# <Business Name> — Operations Context
*Prepared from an AI opportunity assessment on <date>. Machine-readable on
purpose: any AI system can run on it from day one.*

## Who we are
<what the business does, tenure, team size, service area, hours, caller's role>

## Durable facts
<average ticket, close rate, emergency categories + rules, service area,
seasonal patterns — each with provenance>

## How our work flows
<the Process Objects from Prompt A, rendered readably>

## What success looks like
<per opportunity: outcome-defined criteria — "X complete, Y included, Z absent"
— plus explicit stopping conditions (what counts as done)>

## Rules for any AI working for us
<never-share list, human-approval-required list, persona preference
(by-the-book vs judgment calls), who owns the system day-to-day>

## The numbers (all ranges, all assumptions visible)
<Prompt B output, formatted for a human reader>
```

Voice: plain, no consulting jargon, no vendor names, the client's own words
wherever possible.

### Intake record schema (abridged)

```json
{
  "schema_version": 2,
  "company": { "name": "...", "description": "...", "tenure_years": 0, "headcount": 0, "caller_role": "owner_operator" },
  "processes": [
    { "id": "t1", "name": "...", "trigger": "...", "scope": "same_every_time",
      "steps": ["..."], "owners": ["..."], "tools": ["..."], "friction_points": ["..."],
      "quantification": { "minutes_per_occurrence": 0, "occurrences_per_week": 0, "primary_doer": "...", "hourly_cost_usd": 0 } }
  ],
  "pain": { "daily_frustration": "...", "would_eliminate": "...", "repetitive_work": "..." },
  "revenue": { "lead_sources": ["..."], "misses_leads": true, "average_ticket_usd": 0, "conversion_rate_estimate": null },
  "stack": { "current": ["..."], "abandoned": ["..."] },
  "guardrails": { "sensitive": "...", "approval_required": "..." },
  "readiness": { "tech_comfort": 0, "preference": "mid_custom", "champion": "...", "persona": "by_the_book" },
  "disposition": "completed"
}
```

**Worked example.** For a fictional field-services business, *Fabrikam Field
Services* — a septic and drain company with 8 staff — the intake surfaces
"job scheduling and dispatch" as a `same_every_time`, high-volume task
(≈12 min × 50/week × a $35/hr dispatcher ≈ $17.5k/yr manual cost), plus frequent
after-hours missed calls against a $4,500 average ticket. The scorecard reads
high on standardization, volume, and revenue leverage; the recommendation is a
lead-capture wedge with a scheduling-assist follow-on, each with a ranged ROI.

## Guardrails

- **Draft only; the operator approves before anything is sent to the client.**
  Intake produces a record; synthesis produces a draft deliverable. A human
  reviews and sends.
- **The intake agent never quotes a price.** Not even a ballpark. Deflect:
  "I can't quote — the owner will follow up with a recommendation that fits your
  budget."
- **The intake agent never promises capabilities** and never invents facts
  (locations, services, awards) about the operator's business.
- **No model or vendor names in the deliverable.** Capability tiers only.
- **No point estimates.** Every number is a range with visible assumptions.
- **Consent + honest AI disclosure** at the top of every voice intake. Never
  skip it, even if the caller speaks first.
- **Sensitive fields never leak into the deliverable** except as the client's
  own stated guardrails.

## Failure handling

- **Caller goes off-script** (asks pricing, asks for a human, asks about
  services beyond the assessment) — deflect or transfer per scope; do not
  improvise a quote.
- **Quantification unknown** — offer anchors ("more than 5 minutes? less than an
  hour?"), capture the midpoint, flag it low-confidence. Never silently invent a
  number.
- **A synthesis input is missing** — mark the affected figure `assumption:` with
  its default and proceed; do not fabricate provenance.
- **Intake ends early** — capture partial state, mark `disposition: early_end`,
  and synthesize from whatever was captured, clearly noting the gaps.
- **Caller is unprepared** — offer to reschedule rather than produce a
  low-quality record that reflects badly on the operator.
- **Platform failure during intake** — post an honest status, hand off cleanly,
  and do not present incomplete data as if it were complete.
