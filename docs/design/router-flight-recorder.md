<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Router Flight Recorder + Waste Breakers

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../architecture.md).

> **One sentence.** Every call through `services/model-router` gets a bounded, replayable trace (who called, what was requested vs served, tokens, latency, cost estimate, outcome) and is checked against a small set of wasteful-call patterns, so a spend spike or a runaway agent leaves something to inspect other than a bigger number on the bill.

**Audience.** Someone learning agentic AI ops, or an engineer running AzureAgentForge who wants to see *why* a bill moved, not just *that* it moved.

---

## 1. Why a flight recorder matters for an agent platform

An LLM router is a strange kind of infrastructure: it is the one place in an
agent platform that sees every model call before it is billed, and the one
place with the least memory of what happened. A normal web service logs a
request and moves on; the request itself is small and the log line describes
it fully. An agent's request is different — it's one step in a loop the agent
runs on its own, and the loop is exactly where the money goes. A bug that
resends the same prompt, a retry policy that fires faster than the failure
it's retrying, a context window that quietly doubled in size over a long
conversation — none of these look wrong from a single call. They only look
wrong as a *pattern*, and by the time the pattern shows up as a monthly total,
every call that produced it is gone. There is nothing left to inspect, only a
number.

An aircraft's flight recorder exists for the same reason a black box exists
anywhere: not to prevent the incident, but to make sure that after it happens,
there is something other than wreckage to learn from. This feature borrows
that framing directly. It does not stop a bad call — that's what
[budget enforcement](../../services/model-router/README.md#budget-enforcement-budget_enforce_mode)
is for, and it already exists in this router. What the flight recorder adds
is the *replay*: a short, bounded window of exactly what happened per call,
so debugging a spend spike means reading ten trace entries instead of staring
at a dashboard wondering which agent did it.

The waste breakers are the flight recorder's first consumer, and a worked
example of what "something to learn from" is for: three countable patterns
(identical-call repetition, retry storms, oversized prompts) computed from the
trace, reported per request, and — only if you turn it on — enforceable.

## 2. What it records

One event per call to `/v1/chat/completions` or `/v1/messages`:

| Field | What it is |
|---|---|
| `event_id` | Unique id for this event |
| `ts` | UTC timestamp |
| `correlation_id` | From `x-correlation-id` / `x-request-id` / `traceparent`, if the caller sent one |
| `caller` | From `x-agent-id` / `x-tenant-id` / `x-caller-id`, if the caller sent one — the same header precedence the router's per-caller budget ledger already uses |
| `endpoint` | `chat_completions` or `messages` |
| `requested_model` | The `model` field the caller sent |
| `served_tier` / `served_model` | The tier and upstream deployment that actually served the request |
| `input_tokens` / `output_tokens` / `total_tokens` | From the upstream's usage response (or the router's own estimate, for streamed calls — see §3) |
| `latency_ms` | Wall-clock time from request start to this event |
| `cost_usd` | Best-effort per-call cost estimate (LiteLLM's `response_cost` for OpenAI-compatible tiers, list-price estimate for Claude tiers) |
| `outcome` | `success`, `downgraded` (served a fallback or budget-downgraded tier), or `error` |
| `error_class` | Set on `error` outcomes — e.g. `all_tiers_failed`, `budget_blocked`, `waste_breaker:<name>`, or the upstream exception's class name |
| `prompt_fingerprint` | A truncated hash of the request shape — see §4 |
| `prompt_tokens_estimated` | The router's own pre-call token estimate |
| `message_count` | Number of messages in the request |
| `breaker_verdicts` | Every waste breaker's verdict for this call — see §5 |
| `streamed` | Whether this was a streaming call (tokens/cost are estimates on this path — the client, not the router, consumes the stream) |
| `prompt_excerpt` / `response_excerpt` | Only present when redaction is off — see §4 |

## 3. Storage format

The primary store is an **in-memory ring buffer**
(`collections.deque(maxlen=FLIGHT_RECORDER_MAX_EVENTS)`, default 500). The
oldest event is evicted the instant a new one is appended — there is no code
path that lets it grow without bound, so there's no rotation logic to get
wrong and no disk quota to run out of. This is deliberately the *only*
store when `FLIGHT_RECORDER_JSONL_PATH` is unset: a demo, a dev box, or a
container that restarts often doesn't need durability to get value from a
flight recorder, and "in-memory, capped, gone on restart" is an easy
property to reason about.

Setting `FLIGHT_RECORDER_JSONL_PATH` additionally appends each event as one
line of JSON to that file — useful if you want a trace that survives a
restart, or want to `grep`/`jq` it outside the API. That file is
**size-capped** (`FLIGHT_RECORDER_JSONL_MAX_BYTES`, default 10MB) with
single-generation rotation: once the current file would exceed the cap, it
is renamed to `<path>.1` (overwriting any previous backup) and a fresh file
is started. On-disk usage is therefore bounded to roughly
`2 × FLIGHT_RECORDER_JSONL_MAX_BYTES` — never unbounded, at the cost of a
slightly looser bound than the in-memory ring buffer's exact one.

A recorder write can never fail the request it's recording. Every write path
catches its own exceptions and counts them (`write_failures`, `last_error`,
visible in `GET /debug/flight-recorder`) rather than raising — an accounting
gap has to be *visible*, but it must never be the reason a real request
fails.

## 4. Redaction posture

`FLIGHT_RECORDER_REDACT` defaults to `true`, and it is enforced **inside the
recorder itself**, unconditionally, regardless of what a caller passes in.
With redaction on, the trace never contains prompt or response text — only:

- `prompt_fingerprint`: a SHA-256 hash of every message's role + content
  (and every tool's name), truncated to 16 hex characters. Two calls with
  the exact same fingerprint had the exact same request shape; that's
  enough to detect "this caller sent the same prompt five times" without
  the recorder ever holding what was said.
- Token counts and message counts — shape, not content.

This is what makes "safe to leave on" a real guarantee and not a convention
the caller has to honor: even if a future code path accidentally passed raw
prompt text into `record(...)`, the recorder strips `prompt_excerpt` /
`response_excerpt` before the event is ever stored, on every write, no
exceptions.

Setting `FLIGHT_RECORDER_REDACT=false` is a **local-debugging escape
hatch**, not a production posture. It keeps short excerpts (hard-capped at
300 characters, regardless of what's passed in) so you can see what a
specific traced call actually said while developing. Don't run a public
demo instance with it off.

## 5. Waste-breaker taxonomy

Four breakers, each answering one question about the *calling pattern*
around the current request — never about the model's output:

| Breaker | Question | Default threshold |
|---|---|---|
| `repeated_identical_calls` | Has this caller sent the same prompt fingerprint N times in the window? | 5 calls / 60s |
| `retry_storm` | Has this caller made N calls (any prompt) in the window? | 20 calls / 60s |
| `oversized_prompt` | Is *this* request's estimated prompt over N tokens? | 100,000 tokens |
| `consecutive_failures` | Has this caller's last N calls all errored? | 4 calls |

`repeated_identical_calls`, `retry_storm`, and `consecutive_failures` read
their history from the flight recorder's ring buffer (`count_recent` /
`consecutive_failures`), so they degrade gracefully rather than fail when
the recorder is disabled — they simply have no history to evaluate against
and never trip. `oversized_prompt` only needs the current request's token
estimate, so it keeps working either way.

Every breaker is evaluated on every request — including the ones that don't
trip — and every verdict (tripped or not) is attached to that request's
flight event, so a trace answers "what was checked" as well as "what fired."

**Enforcement is env-gated and observe-only by default**
(`WASTE_BREAKER_ENFORCE_MODE=observe`): a tripped breaker is logged and
recorded, and the request is served exactly as if breakers didn't exist.
Setting the mode to `block` makes a tripped breaker refuse the request with
HTTP 429 and a machine-readable `waste_breaker_tripped` body
(`error`, `breaker`, `observed`, `threshold`, `mode`) — evaluated *before*
tier selection or any model spend, so a caller in a retry storm is refused
before the router spends a credential on them, not after.

This mirrors the same graduated posture as
[`budget_enforcement.py`](../../services/model-router/budget_enforcement.py)'s
`warn` → `downgrade` → `block` modes: observe first, on real traffic, and
only flip to enforcement once the thresholds are trusted. The only
enforcement action a waste breaker can take is *refuse the request* — never
reroute, reassign, or silently swap the served model. A refused caller is
loud and safe (they get a 429 and can back off); a silently rerouted one is
neither.

## 6. Config reference

See [`.env.example`](../../.env.example) for the full list with inline
comments, and the model-router
[README § Flight Recorder + Waste Breakers](../../services/model-router/README.md#flight-recorder--waste-breakers)
for the table form. In short:

- `FLIGHT_RECORDER_ENABLED` (default `true`) — the master switch. `false`
  means `main.py` never constructs a recorder at all (`_flight_recorder`
  stays `None`); every call site checks for `None` first, so disabling this
  is a genuine zero-overhead no-op, not just "writes go nowhere."
- `FLIGHT_RECORDER_REDACT` (default `true`), `FLIGHT_RECORDER_MAX_EVENTS`
  (default `500`), `FLIGHT_RECORDER_JSONL_PATH` (default unset),
  `FLIGHT_RECORDER_JSONL_MAX_BYTES` (default `10000000`).
- `WASTE_BREAKERS_ENABLED` (default `true`), `WASTE_BREAKER_ENFORCE_MODE`
  (default `observe`), and one threshold pair per breaker (see §5's table
  for the defaults; each is independently overridable).

## 7. How to replay a trace

The read surface is one endpoint, gated behind the same `ROUTER_API_KEY`
every other `/v1/*` route requires (traces carry caller ids and cost
estimates, so this is not a public route):

```bash
# Most recent calls, newest first
curl -H "Authorization: Bearer $ROUTER_API_KEY" \
  "http://localhost:8080/debug/flight-recorder"

# Filtered to one caller, with recorder + breaker config alongside
curl -H "Authorization: Bearer $ROUTER_API_KEY" \
  "http://localhost:8080/debug/flight-recorder?caller=my-agent&limit=20"

# One event in full
curl -H "Authorization: Bearer $ROUTER_API_KEY" \
  "http://localhost:8080/debug/flight-recorder/<event_id>"
```

The response includes `stats` (buffer size, write failures, JSONL config)
and the live `waste_breakers` config alongside `recent`, so a single request
gives you the trace and the thresholds it was evaluated against — enough to
reconstruct, by eye, why a given call was or wasn't flagged.

## 8. Known gaps and follow-ups

- **`/v1/embeddings` is not wired in.** The flight recorder and waste
  breakers currently cover the two endpoints named in this feature's scope
  (`/v1/chat/completions`, `/v1/messages`). Embeddings calls are still
  budget-tracked (see the router README) but don't appear in the trace.
  Wiring them in would follow the same `_flight_context` /
  `_emit_flight_event` pattern already used on the other two paths.
- **Streaming events are estimates.** For a streaming call, the flight event
  is recorded at stream-open time (tokens estimated, no real cost) because
  the client — not the router — consumes the stream afterward. A follow-up
  could thread a completion callback through `_iter_stream` to record actual
  usage once a stream finishes, at the cost of more invasive wiring.
- **No cross-caller aggregation view.** `/debug/flight-recorder` reports
  individual events; there's no `spend_by_caller`-style rollup over the
  trace the way `daily_cost_rollup()` does for the cost ledger. The ring
  buffer's `count_recent` already computes the building block; a rollup
  endpoint would be a small addition on top of it.
- **Single-process only.** State (the ring buffer, the breaker history it
  feeds) lives in the router process's memory. A router scaled to multiple
  replicas gets independent traces and independent breaker judgment per
  replica — a caller's retry storm is only visible to whichever replica
  happened to receive those calls. Fine for a single-instance deployment or
  a demo; a shared store (Redis, or the same SQLite-ledger pattern the cost
  ledger could eventually adopt) would be needed to unify it across
  replicas.
