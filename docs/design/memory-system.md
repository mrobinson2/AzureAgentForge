<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Governed memory — architecture reference

![status](https://img.shields.io/badge/status-shipped%20%E2%80%94%20flag--gated%20off-orange)

> **Status — read first.** This document describes the governed-memory
> architecture in enough depth to implement, and the implementing code is now in
> this repository, sanitized and **flag-gated off**, under
> [`services/memory-governor/`](../../services/memory-governor/) and
> [`services/watchdog/`](../../services/watchdog/). With every flag off the
> platform ships basic memory (self-hosted Honcho + pgvector) and behaves exactly
> as before — the layer is safe to add to a running system because off = identical
> to before. The admission pipeline, classifier, trust scoring, four-plane
> planner, background loops, hybrid vector retrieval, and the self-improvement
> watchdog are all ported and unit-tested; it has **not** been deployed or verified
> end-to-end against a live database. The parts deliberately **not** built are
> listed in [§15](#15-whats-designed-but-not-built).

**Audience.** An engineer who wants to add a governance layer over an agent
platform's memory — admission control, provenance, trust, retention, and a
self-improvement loop — rather than letting agents write unbounded rows into a
vector store. The model is deliberately store-agnostic: it governs whatever
table your memory service already owns plus one table it owns outright.

---

## 0. Overview

The governed memory system is a small FastAPI **sidecar** that sits between the
agents and the memory store (here, a self-hosted Honcho on Postgres with
pgvector). It does **not** replace the store; it *governs* the `documents` table
the store already owns, plus a new `session_memory` table it owns outright. Two
endpoints are the choke points:

- `POST /admit` — **write-time admission control**
- `POST /plan-retrieval` — **read-time four-plane retrieval planning**

Around them run background loops (a second-stage classifier "annotator", a
task-scope-close watcher, a contradiction sweep) and out-of-process cron jobs
(a TTL sweeper, a watchdog, a digest poster, a skill curator).

Every governed behavior is gated by a row in a `feature_flags` table, read with
a short in-process cache that **fails closed** (unknown/unreadable flag → off).
With all flags off, the sidecar is an idle low-CPU app and the platform's legacy
ungoverned read/write paths are unchanged. That property — *ship dark, enable per
flag* — is what makes the layer safe to add to a running system.

### Status legend

| Marker | Meaning |
|---|---|
| **DESIGNED** | Specified here as part of the model. |
| **DESIGNED-ONLY** | Called out as intentionally *not* part of the core model (see §15). |

---

## 1. The four planes

The planes are a taxonomy of *where memory lives and how it reaches the prompt*.

| Plane | Name | Physical store | Retrieval |
|---|---|---|---|
| **A** | Always-On Cognitive Frame | `documents` (pinned + always-on candidates) | injected every turn, not ranked, hard token ceiling |
| **B** | Native Context | the memory store's own session/representation memory | store-native; **not duplicated by the governor** |
| **C** | Governed Retrieval Layer | `documents` (`durable_fact`, `user_preference`, `task_scoped`, `decaying`) | relevance-ranked, trust-weighted, scope-filtered, quota-bound |
| **D** | Session Working Memory | a **separate `session_memory` table** | session-scoped, expires, hard-deleted on session close |

Plane B is deliberately empty on the governor side: the runtime's prefetch hook
appends the governed package *after* the native providers have already
contributed their context, so Plane B is "whatever the store already injected"
and is never filtered or duplicated.

### Composition order (`plan-retrieval`)

1. **Gate checks** — planner disabled → return `enabled=false`; agent not in the
   injection allowlist → `enabled=false` (the allowlist is the rollout canary).
2. **Plane A** — pinned + confirmed always-on candidates, workspace/peer-scoped,
   newest-confirmed first, capped at a hard token ceiling (e.g. 500).
3. **Plane C candidates** — hybrid vector+trigram or trigram-only (§7), then
   filtered by the agent's `readClasses` profile.
4. **Diversity penalties** computed by shingle overlap.
5. **Score every Plane C candidate** (§5), skipping any id already in Plane A
   (cross-plane dedup).
6. **Apply per-class budgets** (top-K and token ceiling per class).
7. **Plane D** — session memory, capped at a small ephemeral ceiling (e.g. 300).
8. **Failure lessons** — peer-scoped, **not** similarity-gated, deduped against
   A+C (§10).
9. **Telemetry** — emit a `memory_injected` event listing injected doc ids.

The returned package carries `plane_a`, `plane_c`, `plane_d`, `failure_lessons`,
`total_tokens`, and `enabled`. The runtime renders these as labeled blocks
(`[always-on]`, `[known-issue]`, `[recall]`, `[session]`) under a
`[governed memory]` header.

---

## 2. The six memory classes

Classes are operational (how the system treats a memory), orthogonal to the
store's own derivation level.

| Class | TTL / lifecycle | Default scope | Retrieval behavior | Class weight |
|---|---|---|---|---|
| `pinned` | forever until operator revoke | workspace/peer (never task) | Plane A, injected not ranked | not scored (injected) |
| `durable_fact` | survives until superseded/disputed; stale→`needs_review` at 180d | workspace/peer | Plane C, ranked | 1.00 |
| `user_preference` | survives until superseded/disputed; stale→`needs_review` at 180d | global or scoped | Plane C, elevated | 1.20 |
| `task_scoped` | persists while scope active; `expires_at = close + 14d grace`, then swept | task (required) | Plane C, only in matching scope | 0.95 in-scope / 0.0 out |
| `ephemeral` | hard-deleted on session close or 24h TTL | session (required) | Plane D, in-session only | 0.70 |
| `decaying` | half-life decay, hard-deleted when `decay < 0.05` | any | Plane C with decay penalty | 0.80 × decay |

Per-turn budgets cap each class to a top-K and a token ceiling, e.g.
`(pinned: all/500)`, `(user_preference: 5/300)`, `(durable_fact: 5/400)`,
`(task_scoped: 8/500)`, `(decaying: 3/200)`, `(ephemeral: all/300)`.

**Hard invariant:** the classifier can **never** directly produce `pinned`. A
`"pinned"` model response converts to `durable_fact` with
`is_pinned_candidate=true`; the same conversion happens for an explicit agent
write. Only the operator `pin` action promotes to `pinned`. This is enforced in
three places and covered by golden fixtures — promotion to always-on is a human
decision, never a model one.

A schema consequence: `ephemeral` is **excluded from the `documents` CHECK
constraint** (which allows only `pinned,durable_fact,user_preference,task_scoped,decaying`).
Ephemeral physically cannot be written to `documents` — it routes to
`session_memory` or is dropped.

---

## 3. The admission pipeline (`POST /admit`)

The request accepts content + workspace + observer/observed peers + optional
explicit intent (class, scope, source_type, confidence, ttl, pin_request).

### Pipeline stages (in order)

1. **Flag gate.** Classes disabled → returns `status="disabled"` so callers fall
   back to the legacy ungoverned write. Flag-off = zero behavior change.
2. **`memory_candidate` event** emitted.
3. **Classify.** If the agent supplied an explicit class, honor it (still
   applying safety rules — never pinned, thresholds computed). Otherwise call the
   LLM classifier (an economy model via the router).
4. **`memory_classify` event** emitted with class, confidence, retention action,
   reason, parse_error.
5. **Scope validation:** `task_scoped` requires scope_kind+scope_id; `ephemeral`
   requires a session. Violations → `status="rejected"`.
6. **Write-authority check** against the FINAL class (post any decaying
   demotion). A writer lacking authority → `status="event_only"` with a
   `write_denied` candidate event. *This is where "write authority is not trust
   authority" is enforced.*
7. **Retention decision.** Below the event-only threshold (confidence < 0.50) →
   `status="event_only"`, no write.
8. **Plane D routing.** `ephemeral` → `session_memory` if session separation is
   on, else event_only.
9. **Dedup guard:** trigram `similarity(content, $) > 0.9` against same-class
   docs in a lookback window. A hit does **not** silently drop — it *reconfirms*
   the matched memory (§5).
10. **Write** → `documents` with `sync_state='pending'` (so the store's own
    vector-sync worker generates the embedding — the store keeps embedding
    ownership), plus a `memory_write` event.

### Outcomes

| Status | Meaning |
|---|---|
| `disabled` | classes off — caller uses legacy path |
| `admitted` | document (or session_memory row) written |
| `event_only` | below threshold, write-denied, or ephemeral-with-separation-off |
| `duplicate` | near-duplicate; existing memory reconfirmed or dropped |
| `rejected` | scope validation failed |

### Admission thresholds

| Confidence | Action |
|---|---|
| `>= 0.80` | `PERSIST` as predicted class |
| `0.50 – 0.79` | `PERSIST_DECAYING` **except `task_scoped`**, which persists (already lifecycle-bounded by its scope) |
| `< 0.50` | `EVENT_ONLY` (no write) |

Thresholds are env-configurable.

### Provenance and trust columns written

Every persisted doc carries: `memory_class`, `memory_scope_kind/id`,
`source_type`, `verification_state`, `confidence_score`, `trust_score` (seeded
from a base-source-trust table by source type), `half_life_days`, `expires_at`,
`created_by_peer`, `is_always_on_candidate`, `planner_hint`, and an
`internal_metadata` JSON blob carrying `{governed:true, pin_candidate:bool}`.
Trust at write time is just the source baseline; the full trust modifier is
computed at *retrieval* time (§5).

---

## 4. The classifier

The classifier is kept **pure (stdlib only)** on purpose, so a replay harness can
import it without the web framework or a DB and run golden fixtures offline. The
LLM call itself lives in a separate module; the classifier owns prompt, parsing,
validation, and the admission math.

- **Prompt:** demands JSON-only output, defines the five emittable classes, and
  the rules — `task_scoped` MUST set scope; `ephemeral` MUST set a session;
  `decaying` should set `half_life_days` (7/14/30); **never output "pinned"**;
  low confidence for chatter/speculation.
- **Parsing:** never raises, never returns pinned. Tolerant of fenced JSON, with
  a last-resort first-`{...}` extraction.
- **Fallback:** garbage/unknown/transport-failure → `decaying` + `EVENT_ONLY` +
  `confidence=0.0` + `parse_error` set. *A clean system is created by what it
  refuses to store.*
- **Defaults:** unknown verification state → `inferred`; absent → `unverified`;
  decaying with no half-life → a default (e.g. 14 days).

The classifier model is an **economy tier** reached through the in-pod router
sidecar, so tier routing + a daily budget cap apply for free.

---

## 5. Trust model

Trust is **computed at retrieval time** from columns, not stored as a single
mutable number. The stored `trust_score` is only the source baseline; the live
trust used for ranking is the `trust_modifier`.

### Retrieval-time trust formula

```
trust_modifier = base_source_trust
               × verification_weight       (0.0 short-circuits → disputed/superseded excluded)
               × confirmation_factor        (recency-weighted)
               × usage_success_factor       (earned trust)
               × contradiction_penalty
```

| Component | Values |
|---|---|
| `base_source_trust` | operator_entered 1.00, user_asserted 0.90, external_import 0.80, agent_observed 0.60, derived 0.50 |
| `verification_weight` | confirmed 1.15, inferred 0.95, unverified 0.85, needs_review 0.40, disputed 0.0, superseded 0.0 |
| `confirmation_factor` | `1.0 + 0.1·exp(-age/180d)` — fresh confirm ≈1.1, decays to 1.0 |
| `usage_success_factor` | `min(1.25, 1.0 + 0.05·count)` — climbs slowly, capped |
| `contradiction_penalty` | `max(0.25, 1.0 - 0.15·count)` |

`disputed` and `superseded` short-circuit to 0.0, so the operator `dispute`
action is a hard kill switch — the memory scores 0 and stops being injected.

The full composite:

```
score = semantic_similarity × class_weight × decay_factor × trust_modifier × diversity_penalty
```

Out-of-scope scoped memory returns 0 before any other factor.

### Earned trust — two independent signals

The design wants memories that *earn* trust over time. Two mechanisms:

**(a) Re-observation reconfirm (admission-internal).** When admission finds a
near-duplicate, it bumps `usage_success_count += 1` and refreshes
`last_confirmed_at` on the *existing* memory (skipping disputed/superseded) and
emits `memory_reconfirm`. So re-observing a fact corroborates it and climbs its
trust, instead of writing a duplicate row.

**(b) Use-based attribution (watchdog).** The planner emits a `memory_injected`
event per retrieval listing the durable doc ids it injected, keyed to agent +
time. The watchdog matches *terminal-success* runs against same-agent
`memory_injected` events whose timestamp falls in the run's window, then calls a
`reconfirm` action per credited doc. Runs may carry no task id, so attribution
is approximate (agent + time) — acceptable because `usage_success_factor` is a
slow, capped signal.

Concretely: a memory starts at its source baseline (e.g. `agent_observed` =
0.60). Each re-observation or successful-use reconfirm bumps the usage count
(×1.05 each, capped ×1.25) and refreshes the confirmation factor (≈ ×1.1). An
operator `confirm` sets `verification_state='confirmed'` (×1.15). A contradiction
or dispute drags it down or to zero.

---

## 6. Per-agent memory profiles

A profile is `{read: [...classes], write: [...classes]}`. Admission consults
`write` against the FINAL class; the planner consults `read`. Roles are generic
and map to your own agent slugs.

| Profile | read | write |
|---|---|---|
| `ORCHESTRATOR` | all 6 | all 6 (admission still converts pinned→candidate) |
| `SPECIALIST` (default for unknown writers) | pinned, durable_fact, task_scoped, decaying | task_scoped, ephemeral |
| `SECURITY` | pinned, durable_fact, decaying | ephemeral |
| `MONITOR` | pinned, durable_fact, decaying | decaying |
| `WATCHDOG` (the self-improvement writer) | pinned, durable_fact, decaying | **durable_fact, decaying** |
| `SYSTEM` (operator, annotator, sweeper) | all | all |

The `WATCHDOG` profile is what lets the watchdog write `durable_fact` failure
lessons — least privilege: it may create `durable_fact` + `decaying` and read
facts back, but never `user_preference`/`task_scoped`/`ephemeral`/`pinned`.
Without this grant, `/admit` would return `event_only` and the self-improvement
loop (§10) would never persist.

Profiles are overridable per-deployment via a JSON env var (merged over
defaults; unparseable → defaults). Unknown writers default to `SPECIALIST` —
restrictive by design.

**The planner allowlist is a separate gate from profiles.** Even with the
planner enabled, retrieval returns `enabled=false` for any agent not in the
injection allowlist. So writes and scoring are global, but *injection into the
prompt* is canaried to a named set of agents — widen the allowlist to roll out.

---

## 7. Retrieval planner — Plane C and vector retrieval

### Plane C candidate selection

Plane C pulls same-workspace docs in retrievable states
(`durable_fact|user_preference|task_scoped|decaying`, not
disputed/superseded/needs_review, not expired) via one of two SQL variants:

- **Trigram-only:** `similarity(content, query) > 0.05`, ordered by similarity,
  limited. The default path — works with only the `pg_trgm` extension, no
  embeddings.
- **Hybrid** (when vector retrieval is enabled and the query embeds): the
  blended similarity is

  ```
  sim = 0.7 · (1 − cosine_distance(embedding, query_vec))  +  0.3 · trigram_similarity(content, query)
  ```

  A candidate qualifies if cosine similarity > 0.30 **or** trigram > 0.05. This
  keeps semantic recall while still surfacing exact-token matches and
  not-yet-embedded (pending-sync) docs.

The query embedding comes from an embeddings model (1536-dim) via the router —
**the same vector space as the store's `documents.embedding`** (HNSW-indexed).
Any failure — no embedding key, dimension mismatch, missing extension, slow —
returns `None` and the planner **degrades to trigram**. The vector is a *ranker,
never a gate.*

The `documents.embedding` column belongs to the memory store; the governor never
writes it. New governor-written docs land `sync_state='pending'` and the store's
vector-sync worker fills the embedding.

### Scope filtering

- `task_scoped` retrieves only when the active scope is `task` AND `scope_id`
  matches.
- `peer` scope retrieves only for the matching agent slug.
- `workspace`/null scope always retrieves.

### Diversity guard

Crude shingle-overlap clustering: each candidate's word set is compared to
earlier ones; Jaccard overlap > 0.6 → a multiplicative 0.2 penalty on the later
member. Then per-class top-K and token ceilings apply, preserving score order
and dropping anything scoring ≤ 0.

---

## 8. Background loops and jobs

Two kinds of background work: **in-process asyncio loops** spawned by the app
lifecycle, and **out-of-process scheduled jobs** on cron.

### In-process loops

| Loop | Cadence | Gate | What it does |
|---|---|---|---|
| Annotator | 30s | classes enabled | Second-stage classifier for store-derived `documents` with `memory_class IS NULL` |
| Scope watcher | 300s | code-gated (idle without orchestrator URL + JWT) | Stamps `expires_at` on task-scoped docs whose task has closed |
| Contradiction sweep | 6h | contradiction flag | Flags conflicting durable pairs `needs_review` (§9) |

All three are spawned unconditionally and no-op internally when their gate is off
— "always spawn, gate inside" keeps the lifecycle simple.

**Annotator.** The store's deriver runs hourly and persists observations with no
`memory_class`. Rather than patch the deriver (re-risked on every store bump),
the governor *polls* for unclassified docs and updates governance columns in
place. Conservative retention: an `event_only` verdict can't un-persist someone
else's row, so it demotes to `decaying` with a 3-day half-life and lets the
sweeper age it out. Two robustness features earned in production: **poison-doc
handling** (a per-doc attempt counter so one permanently-failing doc — e.g. a
content-filter 502 — gets the conservative fallback after N tries instead of
stalling the queue) and **startup repair** (resets docs mislabeled by an old
transport-failure fallback so they reclassify; idempotent).

**Scope-close watcher.** Closes the "task_scoped never expires" gap without
patching the orchestrator. Every few minutes it finds task-scoped docs with
`expires_at IS NULL`, mints a short-lived read-only token, asks the orchestrator
API whether the task is closed, and stamps `expires_at = now() + 14d grace` on
the closed ones. The TTL sweeper deletes them after the grace period.

### Out-of-process jobs

| Job | Cron (default) | Gate | What |
|---|---|---|---|
| TTL sweeper | nightly | sweeper flag | deletes expired/decayed rows; marks stale facts `needs_review` |
| Watchdog | every 10 min | events flag + watchdog var | the self-improvement loop's write side (§10) |
| Digest poster | daily | poster var + webhook secret | posts the daily digest to a chat webhook |
| Skill curator | weekly | curator var | materializes operator-approved procedural skills |

The sweeper, watchdog, and digest-poster all run off the **same** governor image
with a command override, reusing its managed identity + secret access — no new
image or identity.

**TTL sweeper.** Runtime-gated; with the flag off it exits 0 without touching
anything, so it can be scheduled unconditionally. Four operations, each emitting
`memory_expire` (or `memory_needs_review`): delete expired `task_scoped`; delete
fully-decayed `decaying`; delete expired `session_memory`; mark stale
`durable_fact`/`user_preference` (last confirmed > 180d ago) as `needs_review`.

---

## 9. Contradiction detection

An in-service loop (not a job — it needs the in-pod router for the LLM judge),
gated by a flag and off by default. **Never auto-resolves.**

### How it works

1. Find topically-similar (but not duplicate), same-scope, active durable pairs:
   both `durable_fact|user_preference`, same workspace + scope, trigram
   `similarity BETWEEN 0.4 AND 0.92`, neither disputed/superseded/needs_review,
   limited per pass.
2. For each pair, ask the LLM judge which of five outcomes applies:

   | Outcome | Meaning | Action |
   |---|---|---|
   | `none` | unrelated/compatible | untouched |
   | `supersede` | new value replaces old | flag loser `needs_review` |
   | `scope_refine` | both true in different contexts | flag loser `needs_review` |
   | `coexist` | competing but both valid | untouched |
   | `needs_review` | conflict but unresolvable | flag loser `needs_review` |

   Any transport failure → `"none"` so an error never flags a memory.
3. Pick the **lower-trust** member as the loser; on a trust tie, the **older**
   one (newer info is more likely current).
4. Set the loser `verification_state='needs_review'`, bump `contradiction_count`,
   write a `review_note` carrying the suggested outcome + the keeper's id, emit
   `memory_needs_review`.

**Critical safety property:** the sweep **never auto-supersedes.**
`supersede`/`scope_refine` are surfaced only as *suggestions* in the
`review_note` + event; the loser is flagged `needs_review` (which the planner
already excludes from injection) and waits for the operator to finalize. The LLM
suggests; the operator decides high-impact resolution.

---

## 10. The self-improvement loop (failure lessons)

This is the governor's payoff: it connects the **watchdog** to the memory layer
so the fleet stops relearning the same outages.

**Write side (watchdog):** every few minutes the watchdog pulls recent runs +
`agent_events`, runs a detector library (adapter-failures, stuck-wakes,
budget-anomaly, fabrication-signals — each encoding a real incident class), files
a deduped tracker issue per fresh finding, AND — for findings that name a
specific agent — writes a `durable_fact` failure lesson via `POST /admit`:

- `memory_class=durable_fact`, `scope_kind=peer`, `scope_id=<agent slug>` (so
  only that agent's planner sees it),
- `source_type=agent_observed` (→ trust 0.60, low until operator confirms),
- `verification_state=unverified`,
- `confidence_score=0.85` (≥ persist threshold, so it persists rather than
  demoting to decaying),
- `planner_hint=failure_lesson`,
- content tagged `[signature: <sig>]` so re-filing the same finding is
  byte-identical and the trigram dedup collapses it.

**Read side (planner):** peer-scoped `durable_fact` docs with
`planner_hint='failure_lesson'` for this agent, **not** similarity-gated — every
still-valid lesson is eligible, bounded only by a small max-count and token
ceiling. They ride their own budget (not Plane C's similarity ranking) so an
agent reliably sees the outages it keeps hitting. Disputed/superseded lessons are
excluded — `dispute` is the operator kill switch. The runtime renders them as
`[known-issue]` blocks.

So the loop: **agent fails → watchdog detects + files an issue + writes a
peer-scoped lesson → planner re-injects the lesson into that same agent next task
→ agent stops relearning the same outage.** If a run succeeds with that lesson
injected, use-based attribution (§5b) reconfirms it and its trust climbs.

---

## 11. The `agent_events` spine

One table, one publisher helper, many consumers — the observability backbone the
self-improvement loop and digest read from.

### Table shape

```sql
agent_events (
  id uuid PK DEFAULT gen_random_uuid(),
  ts timestamptz DEFAULT now(),
  session_id uuid, issue_id text, thread_id text,
  actor_peer text NOT NULL,
  event_type text NOT NULL,
  channel text NOT NULL CHECK (channel IN ('cli','chat','voice','orchestrator','system')),
  payload jsonb NOT NULL DEFAULT '{}'
)
```

Indexes on `ts DESC`, plus partial indexes on session/issue/thread/event_type.

### NOTIFY trigger

An `AFTER INSERT` trigger calls `pg_notify('agent_events_channel', NEW.id::text)`
— the payload is the **event id only**; consumers re-read the row (at-least-once,
idempotent by id). Designed consumers: an event router (LISTEN/NOTIFY), an audit
tail, the daily digest, a future inspector UI.

### Emitter

Gated on the events flag; **never raises into the caller** — the spine is
observability, not control flow.

### Memory event types

`memory_candidate`, `memory_classify`, `memory_write`, `memory_reconfirm`,
`memory_injected` (the keystone of use-based earned trust), `memory_expire`,
`memory_needs_review`, and the operator actions
`memory_promote/demote/confirm/dispute/supersede/delete/reconfirm`.

---

## 12. Operator surface, daily digest, and the CLI

### Admin API

`GET /memory` (filtered list), `GET /memory/audit` (last N memory events),
`GET /memory/{id}` (full row, embedding stripped), and `POST /memory/{id}/action`.
Valid actions: `pin`, `demote`, `confirm`, `dispute`, `supersede`, `rm`,
`reconfirm`. Each is a targeted UPDATE plus an `agent_events` row. `rm` is a soft
delete; `dispute` sets `verification_state='disputed'` (trust → 0) — the kill
switch.

### Auth path (three-layer)

Operator CLI → an auth proxy (scope `memory:admin`) that strips the client
bearer and injects a shared governor key → governor (VNet-internal ingress
only). In-VNet callers (the runtime's planner hook, the watchdog, the CLI) attach
the same key from their mounted secret. The digest endpoint is explicitly
passthrough'd so it's reachable off-mesh even though the governor is
internal-only.

### CLI helper

A small bash helper on the agent container's `$PATH` wraps the endpoints:
`record` (→ `/admit`), `recall` (→ `/plan-retrieval`), `list`/`show`/`audit`, and
the operator actions. It **fails open** to the native store's record command when
the governor is unreachable.

### Daily digest

`GET /digest?window_hours=N` aggregates `memory_*` events over a window:
writes-by-class, classify/confirm/dispute/expire/promote counts, plus the pending
pin-candidate and needs-review queue depths. A pure, unit-tested renderer
produces a one-line summary. The **digest poster** is a cron job that fetches the
digest and POSTs the rendered text to a chat webhook (stdlib HTTP only). It
no-ops cleanly when the webhook is unset, so it's gated on the secret being
present.

---

## 13. Feature flags

All flags live in a `feature_flags` table, read with a short fail-closed cache.
Ship every flag seeded **off**; turn them on per environment as you validate.

| Flag | Purpose |
|---|---|
| `AGENT_EVENTS_ENABLED` | master gate for the event spine + watchdog |
| `MEMORY_CLASSES_ENABLED` | classifier + admission + annotator |
| `MEMORY_PLANNER_ENABLED` | four-plane retrieval planner |
| `MEMORY_SESSION_SEPARATION_ENABLED` | route ephemeral to `session_memory` (Plane D) |
| `MEMORY_TTL_SWEEPER_ENABLED` | nightly TTL sweep actually deletes |
| `MEMORY_VECTOR_RETRIEVAL_ENABLED` | Plane C hybrid pgvector+trigram blend |
| `MEMORY_CONTRADICTION_SWEEP_ENABLED` | contradiction sweep flags `needs_review` |
| `SKILL_AUTOGEN_ENABLED` | mine recurring procedural memory → skill candidates |

Deployment-time toggles (separate from runtime flags) control *whether a piece is
deployed at all*: deploy the governor + sweeper, the planner injection allowlist,
deploy the watchdog, deploy the digest poster, deploy the skill curator, and the
classifier's daily budget cap.

---

## 14. Data model / schema

### `documents` (store-owned, governance columns added)

If your store mints `documents.id` as a short text id (a nanoid) rather than a
uuid, the governor mints matching ids and keeps `promotion_source_doc_id` as
`text`. Governance columns added: `memory_class`, `memory_scope_kind`,
`memory_scope_id`, `source_type`, `verification_state`, `confidence_score`,
`trust_score`, `expires_at`, `half_life_days`, `created_by_peer`,
`last_accessed_at`, `last_confirmed_at`, `reviewed_at`, `superseded_at`,
`promotion_source_doc_id`, `review_note`, `usage_success_count` (default 0),
`contradiction_count` (default 0), `is_always_on_candidate` (default false),
`planner_hint`.

CHECK constraints on `memory_class` (5 values — *excludes ephemeral*),
`source_type`, and `verification_state`, added `NOT VALID` so pre-backfill NULL
rows don't block deploy, then `VALIDATE`d by a backfill migration. Indexes on
memory_class, scope, expires_at, verification_state, superseded_at, source_type,
last_confirmed_at, and always_on_candidate.

### `session_memory` (governor-owned, Plane D)

`id uuid`, `workspace_name`, `session_id`, `peer_id`,
`memory_scope_kind DEFAULT 'session'` (CHECK = 'session'), `memory_scope_id`,
`content`, `source_type`, `confidence_score`, `created_by_peer`, `created_at`,
`updated_at`, `expires_at NOT NULL`, `metadata jsonb`. Rows get a 24h TTL on
write and are hard-deleted on session close or by the sweeper.

### Migration ordering

Seed the spine + flag registry (all OFF) + NOTIFY trigger first; then the
`documents`/`session_memory` columns, indexes, and `NOT VALID` constraints; a
manual backfill that sets defaults for pre-existing rows and `VALIDATE`s the
constraints; the `pg_trgm` extension; then per-feature flag-seed migrations.

> **Managed-Postgres gotcha:** some managed Postgres offerings reject
> `CREATE EXTENSION pg_trgm` even when it's already installed, unless it's in an
> allowed-extensions list — so guard the migration on the extension's presence
> rather than issuing an unconditional `CREATE EXTENSION`. Until `pg_trgm` is
> present, `similarity()` calls throw and the admission dedup guard silently fails
> open.

---

## 15. What's designed but NOT built

Grounded in the absence of implementing code, not any doc's aspiration:

- **Reflection / reasoner pass.** A higher-tier nightly synthesis producing
  `needs_review` candidates and higher-order insights. The only things that write
  `needs_review` are the TTL sweeper (staleness) and the contradiction sweep —
  there is no reasoner.
- **Inspector UI.** The admin *API* exists; there is no visual surface.
- **In-channel chat controls** (`!pin`/`!forget`/`!confirm`/…). Operator curation
  is the CLI through the auth proxy, not chat reactions.
- **Auto-resolution of contradictions.** Deliberately *not* built — the sweep
  only ever flags `needs_review` and suggests an outcome; it never
  auto-supersedes. The operator finalizes.
- **`agent_events` partitioning / retention.** The table exists unpartitioned; no
  retention job.
- **Always-on promotion workflow.** `is_always_on_candidate` is written and read
  (Plane A includes confirmed always-on candidates), but there is no automated
  promotion job — promotion to Plane A is operator `pin` or a manually-confirmed
  candidate.

---

## 16. Design principles, distilled

- **Ship dark, enable per flag, fail closed.** Every behavior is a flag; an
  unreadable flag is off. The layer is safe to add to a running system because
  off = identical to before.
- **The model proposes; the operator promotes.** The classifier can never pin;
  the contradiction sweep never auto-supersedes; skill autogen only produces
  candidates. Every high-impact, irreversible step is a human decision.
- **Trust is computed, not stored.** A single mutable trust number rots; a formula
  over provenance + verification + usage + contradiction columns stays honest and
  is auditable after the fact.
- **The vector is a ranker, never a gate.** Semantic retrieval improves ranking
  but every failure mode degrades to trigram — recall never depends on the
  embedding path being healthy.
- **Govern, don't replace.** The layer adds columns to the store's table and owns
  exactly one new table; the store keeps embedding ownership and its native
  context path. That keeps the blast radius small and the upgrade path clean.

---

## 17. Enabling governed memory

The code is in the repo (`services/memory-governor/`, `services/watchdog/`,
`infrastructure/migrations/`) but ships inert. Bringing it online is two
independent steps — *deploy* the service, then *enable* behaviors per flag —
each reversible and safe to do incrementally.

**1. Apply the migrations.** Against the Postgres that backs Honcho, in filename
order (they are idempotent):

```bash
for f in infrastructure/migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
# then, deliberately, the manual backfill that validates the NOT VALID constraints:
psql "$DATABASE_URL" -f infrastructure/migrations/manual/0003_memory_backfill.sql
```

This creates the `agent_events` spine, the `feature_flags` registry (every flag
seeded **off**), the memory-class columns + `session_memory` table, and
`pg_trgm`. With the flags off nothing reads or writes them, so this step alone is
a no-op behaviorally. See [`infrastructure/migrations/README.md`](../../infrastructure/migrations/README.md)
for the managed-Postgres `pg_trgm` note.

**2. Deploy the service.** Locally, `docker compose --profile full up` starts a
`memory-governor` container alongside Postgres + the model-router. On Azure, set
`memory_governor_enabled = true` (the module also creates the sweeper, digest,
and watchdog jobs). Seed the shared key the callers attach as `X-Governor-Key`:

```bash
az keyvault secret set --vault-name <your-key-vault> \
  --name memory-governor-api-key --value "$(openssl rand -hex 32)"
```

A deployed-but-flag-off governor is an idle, sidecar-class app; `/admit` returns
`disabled` and the platform behaves exactly as before.

**3. Enable behaviors per flag.** Flip flags in the `feature_flags` table, in
canary order, watching `/digest` and `/memory/audit` between each:

```sql
UPDATE feature_flags SET enabled = true, updated_by = 'operator'
 WHERE name = 'AGENT_EVENTS_ENABLED';      -- the event spine + watchdog gate
-- then MEMORY_CLASSES_ENABLED (admission), MEMORY_SESSION_SEPARATION_ENABLED,
-- MEMORY_PLANNER_ENABLED, MEMORY_TTL_SWEEPER_ENABLED, and last the long-tail
-- flags (MEMORY_VECTOR_RETRIEVAL_ENABLED, MEMORY_CONTRADICTION_SWEEP_ENABLED,
-- SKILL_AUTOGEN_ENABLED).
```

**4. Canary retrieval injection.** Even with `MEMORY_PLANNER_ENABLED` on,
`/plan-retrieval` returns `enabled:false` for any agent not in the
`PLANNER_AGENT_ALLOWLIST` (the `memory_planner_agent_allowlist` Terraform
variable). Add agent slugs one at a time so a bad injection is contained to one
agent. Vector retrieval additionally requires an embedding key on the router
sidecar (`EMBEDDING_API_KEY`); without it, Plane C stays trigram-only.

Every step is reversible: set a flag back to `false` and the corresponding
behavior stops on the next flag-cache cycle (≤60s); set `memory_governor_enabled
= false` to remove the service entirely.
