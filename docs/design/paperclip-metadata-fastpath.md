<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Zero-LLM Metadata Fastpath

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

> **One sentence.** A small, conservative classifier catches assigned issues that are pure questions about the platform itself — not real work — and answers them with a deterministic, server-side comment at run-execute time, skipping the LLM call and the agent spawn entirely.

**Audience.** Cost-conscious cloud engineers learning agentic AI on Azure who want to see one concrete example of a general principle: **the cheapest LLM call is the one you never make.**

---

## 1. The problem this closes

An agent platform like AzureAgentForge's vendored [Paperclip](../../services/paperclip/README.md)
wakes an agent on every assigned issue. Most of those issues are real work —
research a topic, write some code, send an email — and genuinely need a
model's judgment. But a measurable slice of them aren't work at all. They're
questions about the platform, asked *to* an agent because the agent is the
thing sitting in the issue tracker, not because answering them requires any
reasoning:

- "What LLM are you running for this?"
- "Is this run billed as a subscription or metered?"
- "Which run answered this issue?"
- "How long did that last run take?"

Every one of those has its answer sitting in a row the backend already
loaded before the agent was ever spawned — the agent's configured model
(`agent.adapterConfig.model`), the run's own status and timestamps, the
issue's own identifier. None of it requires an LLM. And yet, unpatched, every
one of these wakes pays for the full cost of a real task: one model call
(tokens, latency, a line item on the bill) and one subprocess spawn (a
container exec, a shell, a CLI process) — to produce an answer that was
never in doubt.

This is a small waste by itself. It's also a clean, teachable instance of a
pattern worth internalizing: **before adding intelligence to a system,
check whether the question even needs it.** A router that classifies a
`?`-terminated one-liner in under a millisecond is not "using AI to save on
AI" — it's recognizing that the deterministic answer was always cheaper, and
just needed someone to write it down.

## 2. What qualifies as "metadata"

A question qualifies for the fastpath only if it is asking about the
**platform's own operational facts**, not about anything the agent would
need to think about:

| Kind | What it asks | Where the answer lives |
|---|---|---|
| `model_identity` | Which LLM/model is this agent configured to run? | `agent.adapterConfig.model` |
| `billing_route` | Is this metered or subscription billing? What's the billing route? | Static platform fact (router-routed, not per-seat) |
| `run_provenance` | Which run/tier answered this issue? | `run.id`, `issue.identifier` |
| `duration` | How long did the run take? | The run's own timestamps (recorded elsewhere, not by the agent) |

Everything else — including a question that merely *mentions* one of these
topics as part of a larger request ("update the billing docs to reflect the
new pricing model") — is real work and must never be fastpathed.

## 3. Classifier rules and the conservatism rationale

The classifier (`classifyMetadataQuestion` in
[`apps/paperclip/metadata-fastpath.mjs`](../../apps/paperclip/metadata-fastpath.mjs))
is pure, synchronous, and dependency-free: `(title, description) -> {kind} | null`.
It can never reach a network call, a database row, or an environment
variable, because it is never given access to any of those things — that
scoping is itself part of the safety story, not just a testability nicety.

**Why conservative, concretely.** The two ways this classifier can be wrong
are not symmetric:

- **False negative** (a real metadata question doesn't match): the issue
  falls through to the normal LLM-backed path, exactly as if this feature
  didn't exist. Cost: one avoidable model call. Mildly wasteful, otherwise
  harmless.
- **False positive** (a real task incorrectly matches): the fastpath posts a
  canned non-answer, marks the issue `done`, and finalizes the run. Cost: a
  real question goes unanswered, and — worse — the tracking system now
  actively *lies* about it having been handled. Nobody re-opens a `done`
  issue on a hunch.

A false negative costs a few cents. A false positive costs someone's trust
in the issue tracker. That asymmetry is the entire design brief: **every
rule below is written to fail toward the cheap mistake.**

Concretely, the classifier applies two independent guards:

1. **Length + question-shape gate.** Below ~400 combined characters, or when
   the text is clearly question-shaped (ends in `?`), classification is
   allowed to run at all. Above that length and not question-shaped, it
   bails out unconditionally — real task descriptions run long; genuine
   platform-metadata questions are short. This alone rejects the "long
   ticket that happens to mention 'billing' in passing" failure mode before
   any pattern is even evaluated.
2. **Per-kind patterns require an explicit question shape**, not a bare
   keyword. `model_identity` requires "what/which (llm|model)" or "model are
   you" — not a bare "model". `run_provenance` requires a leading question
   word before "run id"/"tier"/"provenance" — so "debug why run id 45
   failed" (a real debugging task) never matches, while "what is the run id
   for this?" does. `billing_route` requires a question word within a short
   span of "billing" — a bare keyword match would also catch "update the
   billing model docs", a real task; requiring the question word keeps it
   honest with the same principle every other pattern already follows. (This
   is a deliberate tightening versus a keyword-only first draft — worth
   naming because it's exactly the kind of gap that's easy to miss when
   porting a pattern set from one codebase to another without re-testing the
   boundary.)

**One documented, accepted trade-off:** "Which model should we use for this
agent?" is a legitimate design discussion, but it matches `model_identity`
under the same pattern the classifier needs to catch "which model are you
running?" There is no cheap way to distinguish those two by regex alone.
This is an intentional false negative in reverse — the pattern set accepts
that a handful of real "which model" design questions will also get the
fastpath's deterministic (and, for that question, *wrong*) answer. It's
flagged here rather than silently accepted because a future contributor
tightening this classifier should know it's a known gap, not an oversight —
and because the size of that gap is exactly what the offline test suite
(§5) is there to keep visible.

The test suite's negative cases (`tests/paperclip/metadata-fastpath.test.mjs`)
exist specifically to keep this asymmetry honest over time: every ambiguous
or mixed-signal case is asserted to fall through to `null`, and any change
that makes one of those cases match again should read as a regression, not a
feature.

## 4. Patch mechanism — where this actually lands in `executeRun`

AzureAgentForge doesn't vendor Paperclip's source in-repo. `services/paperclip/Dockerfile`
clones `paperclipai/paperclip` at a pinned tag
(`PAPERCLIP_VERSION=v2026.707.0`, SHA-verified against
`PAPERCLIP_EXPECTED_SHA` — the build fails outright on tag drift), then
applies a series of small, anchored, fail-loud patch scripts before
compiling. This fastpath follows that exact, pre-existing pattern rather
than introducing a new one:

- **Target:** `/app/server/src/services/heartbeat.ts`, inside `executeRun`,
  right after `issueId` and `issueContext` (which carries `title` and
  `description`) are first resolved — before the auto-checkout branch and
  everything downstream that exists purely to prepare an adapter spawn. This
  is the earliest point in `executeRun` where classification is possible
  with real data, which maximizes the LLM calls (and subprocess spawns) the
  patch actually prevents.
- **Two anchors**, both required, both fail-loud on drift:
  1. The `import { issueService } from "./issues.js";` line (insert the
     fastpath's own import immediately after it).
  2. The two-line block that resolves `issueId` and `issueContext` (insert
     the classify-and-answer block immediately after it).
- **Timing:** applied to the `.ts` source *before* `pnpm --filter
  @paperclipai/server build` — the same stage as this repo's other
  `server/src/*.ts` patches (`patch-plugin-host.mjs`,
  `patch-plugin-secrets-handler.mjs`, `patch-plugin-worker-manager.mjs`).
  Patching after the `tsc` build would be a silent no-op against source that
  was never compiled.
- **Runtime companion:** the classify/answer logic lives in a plain
  `.mjs` sibling (`apps/paperclip/metadata-fastpath.mjs`) so it can be
  unit-tested with zero build step. `tsc` (NodeNext, strict) can't
  type-check a plain `.mjs` import without a declaration file, so the patch
  script also writes `metadata-fastpath.d.mts` next to `heartbeat.ts` at
  build time. The Dockerfile then copies the real runtime `.mjs` into
  `server/dist/services/` after the `tsc` build (the compiled output only
  ever contains files `tsc` itself emitted — anything else has to be copied
  by hand, same reason the plugin-loader and board-mutation-guard patches
  operate on compiled `dist/*.js` rather than source).
- **Fail-safe by construction:** the entire fastpath body runs inside a
  `try/catch`. Any failure inside it — a DB call that errors, an unexpected
  shape — is caught, logged, and falls through to the normal LLM-backed
  execution path below it. The fastpath can only ever save work; it can
  never be the reason a real run fails.
- **Flag gate:** `PAPERCLIP_METADATA_FASTPATH=1` (unset/anything else = the
  `if` is never entered — behavior is byte-identical to unpatched
  `heartbeat.ts`, verified by the idempotency + no-op tests in the offline
  suite). `METADATA_FASTPATH_ROUTE_NOTE` optionally appends an
  operator-set note to every fastpath answer; it flows through
  `process.env` at the patched call site only — `metadata-fastpath.mjs`
  itself never touches `process.env`, so it cannot be the code path that
  leaks a polluted environment into an issue comment (see the offline test
  that pollutes `process.env` with fake secrets and asserts they never
  appear in the output).

### On the upstream pin (a note on process, not just outcome)

This feature was ported from a private reference implementation
(`mrt-ai-agent-platform`, internal-only) that targets the same upstream
project. Rather than assuming the same anchors would hold, this port
verified them directly: a shallow clone of AzureAgentForge's exact pinned
tag (`v2026.707.0`) confirmed the resolved commit matches
`PAPERCLIP_EXPECTED_SHA` (`390627b46eb333309d357004384b220ecf8a65af`), and a
direct read of `server/src/services/heartbeat.ts` at that commit confirmed
both anchor strings, and every symbol the patch calls
(`issuesSvc.addComment`, `issuesSvc.update`, `setRunStatus`,
`setWakeupStatus`, `getRun`, `releaseIssueExecutionAndPromote`, `logger`),
exist at the exact call shapes the patch assumes.

The finding: AzureAgentForge pins the **same** upstream tag the reference
implementation targeted, and none of AAF's own pre-existing patches
(`patch-plugin-host.mjs`, `patch-plugin-secrets-handler.mjs`,
`patch-plugin-worker-manager.mjs`, `patch-adapter.mjs`) touch
`heartbeat.ts` — so the region this fastpath anchors on is untouched
upstream source, byte-identical to the reference's own fixture. That's a
lucky case, not a guarantee: the two platforms' pins can and will drift
independently over time (AAF and the reference repo bump their
`PAPERCLIP_VERSION` on their own schedules). The fail-loud anchor checks in
§4 are exactly the mechanism that turns a future drift into a loud build
break instead of a silently unapplied patch — that protection is why this
was worth re-verifying from the real source rather than trusting the port.

## 5. Tests

`tests/paperclip/metadata-fastpath.test.mjs` — zero network, zero LLM,
Node's built-in test runner (already the convention for every file in
`tests/paperclip/`, auto-discovered by CI's existing
`node --test tests/paperclip/*.test.mjs` step; no CI wiring was needed for
this feature). Covers:

- **Classifier positives** — one case per kind (`model_identity`,
  `billing_route`, `run_provenance`, `duration`), plus a case confirming a
  match works from the description alone (not just the title).
- **Classifier negatives** — the load-bearing block. Real task titles/
  descriptions that must never match, including the documented "which model
  should we use" trade-off (§3) and a long, non-question-shaped ticket that
  incidentally contains "model" and "billing" as ordinary words.
- **Answer composition** — exact wording for each kind, the deterministic/
  no-model disclaimer present on every answer, safe fallbacks for missing
  fields (never prints `"undefined"`), and a direct test that a polluted
  `process.env` (fake API keys and JWT secrets injected before the call)
  never leaks into the composed answer.
- **Patch-application integrity** — the transform against a real, verbatim
  fixture of the pinned `heartbeat.ts` (see §4's note on how that fixture
  was verified): both anchors land in the right place and order, the
  transform is idempotent, and it **fails loudly** (returns
  `applied: false, alreadyPatched: false`, with a `reason`) when either
  anchor is missing, appears more than once, or the source is unrelated
  entirely.

Run: `node --test tests/paperclip/metadata-fastpath.test.mjs`

## 6. How to measure the saving

This feature doesn't ship its own metrics surface — it reuses one that
already exists for exactly this purpose: the model-router's
[flight recorder](./router-flight-recorder.md). Every call through
`services/model-router` gets a bounded, replayable trace with
`requested_model`, `served_tier`, token counts, latency, and cost estimate.
Measuring the fastpath's savings is a before/after read of that trace, not a
new dashboard:

1. **Before** — with `PAPERCLIP_METADATA_FASTPATH` unset, let a normal
   population of issues flow through for a representative window. Query
   `GET /debug/flight-recorder?caller=<paperclip-agent-id>` and count calls
   whose `prompt_fingerprint` corresponds to a metadata-shaped wake (in
   practice: cross-reference against the issue titles that matched
   `classifyMetadataQuestion` offline, since the flight recorder itself has
   no notion of this classifier).
2. **Turn it on** (`PAPERCLIP_METADATA_FASTPATH=1`) for the same class of
   traffic.
3. **After** — the same query should show those calls simply absent from
   the trace: no `chat_completions` event for issues that fastpathed,
   because `executeRun` returned before the adapter was ever resolved. The
   flight recorder's `stats.write_failures` staying at zero across the
   change is a good sanity check that nothing else on the request path
   broke.
4. **The honest number** is "avoided calls × the model-router's own
   `cost_usd` estimate for that agent's configured model" — not a guess.
   Because the flight recorder already estimates cost per call
   (LiteLLM's `response_cost` where available, list-price estimate
   otherwise), the saving is the same unit the router already reports
   spend in, which is what makes it a credible number to put in front of
   someone rather than an assertion.

This is also why the feature is worth teaching, not just shipping: the
"before" trace is the same trace you'd use to catch a runaway retry loop.
Learning to read it for *avoidable* calls, not just *anomalous* ones, is a
transferable skill — the metadata fastpath is just the first target obvious
enough to build a whole feature around.

## 7. Rollout guidance: dark → canary → on

- **Dark (ship default).** `PAPERCLIP_METADATA_FASTPATH` unset. The patch is
  baked into every image (so the code path exists and is exercised by CI),
  but the `if` is never entered — verified by the offline test suite's
  no-op/idempotency assertions, not by hoping. This is the state this PR
  ships in.
- **Canary.** Flip the flag on for a single low-stakes agent or a single
  test company first, not the whole fleet. Watch two things for a full
  business cycle: (a) the flight recorder trace, per §6, to confirm calls
  are actually being avoided and nothing else regressed; (b) the issue
  tracker itself — spot-check a sample of fastpathed issues by eye to
  confirm they really were metadata questions, not classifier false
  positives that slipped through. This second check matters more than the
  first; §3's asymmetry means a quiet failure here is a trust problem, not
  a cost problem.
- **On.** Once canary has run clean for a representative window (a rollout
  long enough to see the full mix of issue types a given agent normally
  handles — a few days of real traffic is a reasonable bar, not a fixed
  number of issues), flip the flag on platform-wide. Keep the flag itself
  in place indefinitely as a kill switch: a single env var, no redeploy, no
  code change, instantly returns to byte-identical pre-patch behavior if a
  false positive ever does slip through in production.

Because the flag is read fresh from `process.env` on every `executeRun`
call (not cached at process start), rolling back mid-incident is a
container-env update, not a rebuild.
