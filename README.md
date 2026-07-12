<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/azureagentforge-logo-light.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="560">
  </picture>
</p>

<h3 align="center">An open-source Azure foundation for running AI agent teams with private memory, tool control, cost guardrails, voice/chat interfaces, and observability.</h3>

<p align="center">
  <a href="#platform-status"><img src="https://img.shields.io/badge/status-running%20on%20Azure-brightgreen" alt="Status"></a>
  <a href="ROADMAP.md"><img src="https://img.shields.io/badge/release-v1.7-blue" alt="Release v1.7"></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/IaC-Terraform-623CE4" alt="Terraform"></a>
  <a href="#why-azureagentforge"><img src="https://img.shields.io/badge/cloud-Azure-0078D4" alt="Azure"></a>
  <a href="docs/walkthroughs/governance-and-blast-radius.md"><img src="https://img.shields.io/badge/demo-governance%20refusal-FF6B00" alt="Governance demo"></a>
</p>

<p align="center"><sub>New here? This README is laid out to read top to bottom in about ten minutes. Unfamiliar term? Check the <a href="docs/GLOSSARY.md">glossary</a>.</sub></p>

---

**At a glance:** open source, MIT-licensed, deployed on your own Azure subscription — nothing calls home. The `cost-optimized` Terraform profile targets under $150/month in infrastructure spend; your LLM provider bills token usage separately. It runs in production today; this repository is the sanitized, reusable version of that platform, and [what's not finished yet](#whats-not-finished-yet) is stated plainly below.

Most agent demos look amazing for five minutes.

Then you try to run one for real work and the uncomfortable questions show up:

- Where does memory live?
- Who is allowed to call which tools?
- How do you stop one agent from burning the expensive model budget?
- What happened at 2 a.m. when the agent made that decision?
- How do people use this from Teams, voice, web, or chat without creating four more side projects?
- Can this run in Azure without becoming a weekend science project?

**AzureAgentForge is built for that gap.**

It brings together three open-source agent tools: **PaperClip** for orchestration and UI, **Hermes** for agent execution, and **Honcho** for private memory. It wraps them in an Azure foundation with Terraform, Key Vault, Container Apps, PostgreSQL, budget-aware model routing, and centralized logging.

Most LLM calls route through **Azure AI Foundry**, which keeps model integration simple and aligned with the Azure security model. Where supported, the platform favors Microsoft Entra ID and managed identity over long-lived API keys.

You can deploy these agent teams, watch them, constrain them, talk to them, and improve them. That is the point.

---

## AzureAgentForge is for you if...

You want agents that do more than chat.

You want to give agents goals, tools, memory, and budgets, then see what they did, what they spent, and where they got stuck.

You want to run that system on Azure instead of duct-taping together a laptop demo, a hosted memory service, a mystery dashboard, and a pile of API keys.

You may be building:

- a private agent platform for your own projects
- a small "AI company" made of specialized agents
- an automation lab
- an internal enterprise prototype
- a safer way to experiment with long-running autonomous workflows
- a voice-enabled assistant stack
- an Azure-native agent stack you can actually reason about

AzureAgentForge gives you the starting foundation: orchestration, runtime, memory, model routing, Terraform, secrets, logs, cost controls, and human-facing channels.

---

## The components

Five pieces do the work. Everything else in this repo (Terraform modules, cost profiles, governance layers) configures or protects one of these five.

| Component | What it actually is |
|---|---|
| **PaperClip** | The work-order system: the web UI, the task/issue board, and the dispatcher that hands work to agents and returns their results to you. |
| **Hermes** | The worker. It runs the actual agent loop — reads a task, calls the model, calls tools, reports back. |
| **Honcho** | The filing cabinet. It stores what agents remember about a session or a user, in your own PostgreSQL database. |
| **Model Router** | The spending gate. Every model call passes through it so a budget cap and a fallback plan apply consistently, no matter which agent is asking. |
| **Azure AI Foundry** | The model source. The router's preferred place to get an actual LLM response from, running under your Azure subscription and billing. |

An optional sixth piece, the **Memory Governor**, adds admission control and trust scoring on top of Honcho's memory — see [what it solves](#what-it-solves) below. It ships in the repo but is off until you turn it on.

For the full picture — how these five talk to each other, what Azure resource backs each one, and where secrets and logs flow — see [`docs/architecture.md`](docs/architecture.md), which opens with a "how to read this" guide and expands this table with configuration and maturity detail.

```text
                                  +---------------------------+
                                  |        Human Users         |
                                  | Browser / Teams / Voice    |
                                  | Telegram / Discord / Web   |
                                  +-------------+-------------+
                                                |
                                                v
                    +---------------------------------------------------+
                    |                    PaperClip                      |
                    |          Orchestrator, UI, task dashboard          |
                    |      Optional Telegram / Discord chat bridges      |
                    |      Optional Microsoft Teams chat bridge (v1.2)   |
                    +----------------------+----------------------------+
                                           |
                                           | dispatches work
                                           v
                    +---------------------------------------------------+
                    |                Agent Runtime Layer                 |
                    |                                                   |
                    |   Hermes today                                    |
                    |   second runtime planned                          |
                    |                                                   |
                    |   Role profiles, toolsets, skills, delegation      |
                    +-----------+---------------------------+-----------+
                                |                           |
                                | model calls               | memory calls
                                v                           v
          +--------------------------------+       +-------------------------------+
          |          Model Router          |       |            Honcho             |
          | OpenAI-compatible API facade   |       | Self-hosted agent memory      |
          | Tier budgets, auth, fallback   |       | PostgreSQL + pgvector         |
          +---------------+----------------+       +---------------+---------------+
                          |                                |
                          v                                v
          +--------------------------------+       +-------------------------------+
          |       Azure AI Foundry         |       |      PostgreSQL Flexible      |
          | Preferred model gateway        |       |      Server with pgvector     |
          | Managed identity where possible|       |      Private network path     |
          | OpenAI-compatible fallback     |       +-------------------------------+
          +--------------------------------+


      +-----------------------------------------------------------------------+
      |                              Azure                                    |
      |                                                                       |
      |  Container Apps  |  Private VNet  |  Key Vault  |  ACR  |  Log Analytics |
      |                                                                       |
      |  Terraform provisions the foundation. Key Vault provides secrets.       |
      |  Logs flow into Azure observability. Services run inside one VNet.      |
      +-----------------------------------------------------------------------+
```

For the full architecture, diagrams, component details, and data flow, see [`docs/architecture.md`](docs/architecture.md).

---

## See it in action

A quick tour of the orchestrator UI: the dashboard, agent roster, issue board, and a live org chart of an agent team at work.

<p align="center">
  <img alt="PaperClip orchestrator UI: dashboard, agents, issue board, and org chart" src="docs/assets/paperclip-ui-demo.gif" width="820">
</p>

The part most demos skip is what happens when a request is dangerous. Ask the orchestrator to "delete this resource group" and it refuses: a scope-guard and a forbidden-tool block stop it, with a full audit trail, and a reproducible [replay fixture](tests/replay/) pins the behavior so it isn't a staged screenshot. The [governance and blast-radius walkthrough](docs/walkthroughs/governance-and-blast-radius.md) traces every layer between that request and irreversible damage.

<p align="center">
  <img alt="Destroy-aware approval gate" src="docs/assets/destroy-gate.gif" width="760"><br>
  <em>At the infrastructure layer, the destroy-aware gate lets routine changes apply unattended but blocks any plan that <strong>deletes or replaces</strong> a resource behind an explicit human approval.</em>
</p>

---

## What's new

*This section is a technical changelog — engineering-detail-level, written for readers evaluating exactly what changed. If you just want the pitch, skip to [Why AzureAgentForge](#why-azureagentforge). Terms in brackets link to the [glossary](docs/GLOSSARY.md).*

**Unreleased on `main`, ahead of a v1.8.0 cut** (six merges, one theme: turn a silent failure into a loud one):

- **The only test that proves the product works.** `local-stack-smoke` checked containers and health endpoints but never proved an agent could finish a task. The new **agent-loop [canary](docs/GLOSSARY.md#canary)** (`scripts/canary/`, wired into [`local-stack-smoke.yml`](.github/workflows/local-stack-smoke.yml)) files a real issue, wakes it, spawns the real vendored Hermes runtime, sends a model call through the model-router, executes a real terminal tool call, and checks for a [disposition comment](docs/GLOSSARY.md#disposition-protocol) before the issue closes. Only the LLM reply is stubbed. Building it found six previously invisible defects, all fixed in the same change — headlined by **Hermes could never boot inside the AAF PaperClip image at all**: the `hermes-cli` build stage installed on `python:3.14` while the runtime image runs Debian trixie's `python3.13`, so every agent spawn died with `ModuleNotFoundError` and PaperClip's run-recovery quietly moved the issue to `blocked` — present since the file was first committed, with every health check green the whole time. See [`docs/local-development.md`](docs/local-development.md#agent-loop-canary-the-smoke-that-proves-agents-can-work).
- **A CI job that catches config the app silently stopped reading.** The new `validate-vendored-config` job loads the pinned [vendored source](docs/GLOSSARY.md#vendored-source) of each app (Honcho's actual [pydantic-settings](docs/GLOSSARY.md#pydantic-settings) model, an [AST parse](docs/GLOSSARY.md#ast-parse) of Hermes's config parser, a version-pinned manifest for PaperClip) and checks every key AAF ships against what that source really consumes — a dropped key now fails the build instead of quietly degrading a deployment. Its first run found the failure class alive in this repo: **57 stale flat Honcho keys in `honcho.tf`** and **19 more in the mac-site compose file** (both pre-dating the 3.0.7 nested `MODEL_CONFIG` migration — a live deploy would have silently run every specialist on direct-OpenAI `gpt-5.4-mini`), plus an inert `HERMES_DB_PATH` variable the pinned Hermes build never reads. All fixed in the same PR. Design doc: [`docs/design/vendored-config-schema-guard.md`](docs/design/vendored-config-schema-guard.md).
- **Hard cost-envelope enforcement.** The v1.5 cost-governance layer moves from observe-only to enforceable: `BUDGET_ENFORCE_MODE` (`warn` default / `downgrade` / `block`) now covers every router path, including the native `/v1/messages` Anthropic transport, which — as a side effect of wiring it up — now records spend at all; it previously recorded zero. Embeddings spend is now budgeted too, in its own ledger bucket.
- **Provider-flexible embeddings, with an Azure AI Foundry path.** `/v1/embeddings` no longer assumes OpenAI. A documented Foundry deployment path ([`docs/walkthroughs/azure-foundry-embeddings.md`](docs/walkthroughs/azure-foundry-embeddings.md)) plus a load-bearing `openai/` [LiteLLM](docs/GLOSSARY.md#litellm) provider-detection pin baked into the router means forks don't have to rediscover the `400 unknown_model` failure mode against a Foundry endpoint.
- **One canonical user-peer identity.** `HONCHO_USER_PEER_ID` names the memory peer that represents the human principal, resolved the same way — and defaulted to the same fallback — by every writer and reader: Terraform, compose, the memory-governor, and the Hermes helper scripts. This closes AAF's own seeds of the peer-fragmentation failure mode it warns about elsewhere: Terraform defaulted the peer to `operator`, the governor hardcoded `"user"`, and a helper script sent no `observed` field at all. See [`docs/design/memory-system.md` §18](docs/design/memory-system.md#18-identity-the-canonical-user-peer).
- **Vendored incident-fix defaults.** Three fixes ported from the upstream private deployment's own incident history, so a fresh fork doesn't have to rediscover them: the generated Hermes config now defaults to the router-compatible `chat_completions` + `api_key` shape instead of one that 401s against the router's fail-closed auth; the adapter build patch pins `--provider custom` in both the npm dist *and* the workspace source that PaperClip 707 actually loads at runtime; and every agent template gets an explicit [disposition protocol](docs/GLOSSARY.md#disposition-protocol) — exactly one terminal state per run, never a silent one.

This batch is merged to `main` but **not yet tagged**; see [Roadmap](#roadmap) for the v1.8.0 status.

New in v1.7 (since v1.5):

- **Contradiction sweep performance hardening**: the sweep's candidate query is a pg_trgm similarity self-join on `documents` — without a [trigram index](docs/GLOSSARY.md#trigram-matching), even ~1k eligible docs blow through the pool-wide 30s command timeout, so every pass timed out and no pair was ever judged (found in production upstream). Migration [`0009`](infrastructure/migrations/0009_contradiction_sweep_perf.sql) adds a `gin_trgm_ops` index (guarded on `pg_trgm` presence), and the candidate fetch gains a dedicated per-query timeout (`CONTRADICTION_QUERY_TIMEOUT_S`) plus a recency window (`CONTRADICTION_LOOKBACK_DAYS`; `0` = full-corpus pass).
- **Read-only memory inspector summary**: `GET /memory/inspector-summary` aggregates a workspace's governed memory at a glance — live counts by memory class / verification state / source type, the embedding-sync queue, and a 7-day tally of ranking modes (vector vs trigram). No mutation, no new state.
- **Daily memory review-queue digest**: `GET /memory-digest` lists, per workspace, what needs operator action — pending pin-candidates, memories flagged `needs_review` by the contradiction sweep, and memories expiring within 7 days — each section capped with an honest "+N more" overflow line. Read-only, always available for preview; `MEMORY_DIGEST_ENABLED` (migration [`0010`](infrastructure/migrations/0010_memory_digest_flag.sql), seeded off) only gates folding the listing into the daily `/digest` post.
- **Escalation SLA auditor ([ship-dark](docs/GLOSSARY.md#ship-dark))**: `GET /escalation-sla` audits the human side of the autonomy handoff — an event taxonomy on the `agent_events` spine plus a pure pairing/rollup that measures human ack latency against a per-tenant SLA (default 30m, optional business-hours clock), where TTL expiry always counts as a breach AND unresolved ([fail-closed](docs/GLOSSARY.md#fail-closed-and-fail-open) made visible, never weakened) and the v1.5 approval seam's `autonomy_decision` events serve as retroactive ack+resolution. Read-only; the auditor never acts. `ESCALATION_SLA_ENABLED` (migration [`0011`](infrastructure/migrations/0011_escalation_sla_flag.sql), seeded off) only gates folding the report into the daily `/digest`; emitters land when the [HITL](docs/GLOSSARY.md#hitl) approval seam is wired for real volume.

- **Security remediation batch** ([#97](https://github.com/mrobinson2/AzureAgentForge/pull/97)): ~27 findings remediated across the auth-proxy, the multi-tenant reference design, model-router, chat bridges, memory-governor, the installer/forge-console, and the infrastructure modules. Headline changes: **fail-closed auth** (model-router, memory-governor, slack-bridge, teams-bridge, and the multi-tenant control-plane/memory-store now return `503` when their auth secret is unconfigured instead of silently running open); **tenant isolation** (memory-store derives `tenant_id` from a verified bearer token, Postgres [RLS](docs/GLOSSARY.md#rls) backstops the control-plane and memory-store tables, and the tenant-console `vertical` field is allowlisted + realpath-contained); **prompt-injection fencing** (untrusted Slack/Teams/governed-memory/watchdog text is wrapped in explicit untrusted-data delimiters before reaching a model); plus CSRF/DNS-rebinding guards, error-detail hardening, and secure-by-default Key Vault/storage firewalls (`Deny` + allowlist). Full grouped notes in [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md).
- **Governance examples & samples** (three new self-contained packages, sanitized and flags-off, readable and testable locally with no live Azure subscription):
  - **`examples/governed-ui-patterns/`** — nine themeable UI governance patterns (honesty badge, trust receipt, refusal card, approval gate, pricing-policy engine, autonomy panel, sealed record, movement log, signed charter) + an 11-check conformance linter (`check.js`) with a CI-able exit-code contract + a live demo page.
  - **`samples/foundry-chat-proxy/`** — a minimal Node 24 Flex Consumption Azure Function fronting an AI Foundry chat deployment, with a grounded persona, message clamping, prompt-injection guardrails, Bicep for the function app, and a runbook README of the hard-won Flex/Node-24 gotchas.
  - **`examples/governed-transaction-saga/`** — a compact (~300-line + tests) event-sourced governance core: append-only event log with tenant/correlation/causation IDs + idempotency, a fold/apply state machine, complete-at-write receipts, and an audit walk producing a chronological narrative + receipt-gap report. Pure Python stdlib + pytest.
- **Multi-tenant console demo** ([`demos/tenant-console/`](demos/tenant-console/)): a sanitized, self-contained static demo of the multi-tenant operator console — a tenant list (six fictional tenants with vertical, status, assigned playbook pack, monthly budget cap, and spend-to-date), a per-tenant detail drawer (pack, read-only feature-flags panel with every flag **off**, budget/cost bars, roster, autonomy policy), and a playbook-packs view showing pack-to-tenant assignment. One `index.html`, inline CSS/JS, zero external resources, opens from `file://`, clearly labeled "DEMO — sample data, read-only, no live tenants". The concepts (tenant-as-contract, per-vertical pack bundles, green/yellow/red fail-closed autonomy policy, per-tenant budget ledger) mirror the `experimental/multi-tenant/` design.
- **Deployment experience**: `./forge --check` (alias `--preflight`) runs an offline, dependency-free preflight — a prerequisite table, a per-path readiness verdict (Azure / local Docker), and the operator-gate reference — before you touch the console. The Forge Console gains an "Operator gates — where you sign off" card naming every human-approval gate (subscription/billing, secrets-in-Key-Vault, environment-name confirmation, destroy-approval, CI/CD scaffold-apply), advisory inline validation on the Configure fields (the server stays source of truth), and theming for the CI/CD setup panel. [`AI-ASSISTED-SETUP.md`](AI-ASSISTED-SETUP.md) adds a preconditions checklist, an operator-gates subsection, and per-phase "how to know it worked" verification signals.

New in v1.5 (since v1.4):

- **Governed memory, enable-able for real**: the memory-governor self-provisions its full schema on startup (a `0002` overlay completes the planner's `documents` columns and adds `session_memory` + `skill_candidates`, reconciled to the canonical migrations), `memory_governor_enabled` is threaded through the deploy, and a `showcase` profile deploys it with an honest cost + go-live checklist. Flags still seed off; enabling needs an operator embedding key + a live validate.
- **Retrieval observability**: the memory system reports its ranking path (`vector`/`trigram`/`trigram_fallback`) in the retrieval package, the `memory_injected` event, and a `/healthz` embedding block; a watchdog detector fires on sustained vector→trigram degradation.
- **`aca-job` sandbox contract reconciled**: to the documented ACA dynamic-sessions **executions** API (`/executions`, query-param `identifier`, `shellCommand` body, `properties`-nested response) plus an IMDS managed-identity token provider. Still unverified against a live pool; default provider stays `local`.
- **Observability + cost governance**: `gen_ai.usage` token/cost metrics and a `correlation_id` on the router span, per-caller spend attribution with an optional daily cap and rollup, an SLO-burn alert, and a GenAI cost workbook tile.
- **Human-in-the-loop action approval**: a provider-pluggable seam that gates runtime agent actions (outbound message, destructive tool) behind human approval — inert by default, fails closed for gated actions, with a `webhook` approver. Ships unwired.

New in v1.4 (since v1.3):

- **Multi-tenant tenant console (reference)**: playbook-driven onboarding — one tenant contract renders an intake/coordinator agent pack, seeds per-tenant governed memory, provisions an isolated workspace, and enforces a per-tenant daily budget cap. Ships as a badged reference with a worked field-service pack ([`experimental/multi-tenant/tenant-console/`](experimental/multi-tenant/tenant-console/)).
- **Self-hosted-primary topology**: a cost profile that runs the full stack on an always-on machine you own as the primary site, with Azure as a dormant warm standby sharing one managed Postgres — so failover is a stateless compute switch. Includes a `scripts/aaf-site` failover/failback helper and an ADR ([`deploy/mac-site/`](deploy/mac-site/), [`deploy/windows-site/`](deploy/windows-site/)).
- **Inbound-intake webhook (reference)**: a vendor-neutral handler that turns an inbound intake/lead payload into a routed Orchestrator issue, with signature verification and a fenced untrusted-content boundary ([`integrations/webhook-intake/`](integrations/webhook-intake/)).
- **Slack bridge**: a flag-gated `slack-bridge` service at parity with Discord/Telegram/Teams — a Slack Events API endpoint that verifies the signing-secret HMAC, turns inbound messages into Orchestrator issues, and replies via `chat.postMessage`. Off by default (`slack_enabled`), internal ingress, bot token from Key Vault ([`services/slack-bridge/`](services/slack-bridge/)).
- **ACA `aca-job` sandbox provider (scaffold)**: the v1.3 sandbox seam gains an Azure Container Apps dynamic-sessions provider with an injectable, fully unit-tested transport and a spawn-path patch gated on `SANDBOX_PROVIDER`. The one live REST call is marked unverified and `aca-job` stays disabled (default `local`) pending a spike against a real session pool ([`apps/paperclip/sandbox.mjs`](apps/paperclip/sandbox.mjs)).

New in v1.3 (since v1.2):

- **GenAI observability, now live**: every LLM call through the model-router emits one OpenTelemetry GenAI span (model, token counts, cost) to Application Insights, behind `OBSERVABILITY_ENABLED` (off by default), with content redacted. The same change closes the Anthropic cost gap: Claude-tier calls return no billed cost from the SDK, so they now carry a list-price estimate and show up in both cost tracking and traces. The span export was fixed and verified against a live Application Insights workspace.
- **Sandbox execution seam**: a provider-pluggable sandbox contract in PaperClip with a `local` adapter and a fail-closed factory, shipped unwired so importing it changes nothing at runtime. It is the seam for an Azure Container Apps dynamic-sessions provider later. 16 unit tests in CI ([`apps/paperclip/sandbox.mjs`](apps/paperclip/sandbox.mjs)).
- **Turnkey CI/CD setup**: the Forge Console gains a CI/CD Setup page that runs [`scripts/scaffold-cicd.sh`](scripts/scaffold-cicd.sh), provisioning the [OIDC](docs/GLOSSARY.md#oidc) app, the Terraform state backend, and the GitHub variables the deploy pipeline needs. It runs preview-first and live-streamed, behind an apply-confirmation gate. The reference [`deploy.yml`](.github/workflows/deploy.yml) now runs green end to end against a clean subscription.
- **Obsidian memory interface**: a two-way `memory ↔ vault` CLI in the governor. `export` projects governed memory into an Obsidian-compatible Markdown vault, you curate it in Obsidian, and `sync` applies your edits back conservatively, re-checking server state and skipping conflicts ([`docs/obsidian-memory-interface.md`](docs/obsidian-memory-interface.md)).
- **Upstream security hardening**: the vendored Hermes runtime ships with its Python dependency CVEs remediated, including the aiohttp, starlette, tornado, and python-multipart DoS cluster on the request path plus cryptography and pynacl, force-upgraded past Hermes's exact version pins at build time. The vulnerable Hermes Node surface (the WhatsApp bridge, which carries the critical `baileys` advisory) is kept out of the agent-runtime image, and a `security-checks` CI job enforces both. The PaperClip auth-proxy adds a fail-closed CSRF Origin guard and a bounded admin-session TTL, and recurring dependency scanning runs through Dependabot.
- **Deployment flexibility**: an optional Cloudflare-managed ingress module (named tunnel, ingress config, and proxied DNS record) gives a chat surface like Teams a public endpoint without a public load balancer, and the network module can now deploy into an existing VNet (BYO-VNet) instead of creating its own.

New in v1.2 (since v1.1):

- **End-to-end Azure deploy, now validated**: a full deploy from a clean subscription covering server-side image build and push (`az acr build`), Key Vault seeding, `terraform apply`, and post-deploy smoke. See the [step-by-step walkthrough](docs/getting-started.md#deployment-walkthrough-forge-console).
- **One-command full local stack**: the upstream PaperClip/Honcho/Hermes sources are vendored so the full image set builds and runs with `scripts/local-stack.sh up` (or `docker compose --profile full up`).
- **Microsoft Teams integration**: the `teams-bridge` Bot Framework service files inbound Teams messages as Orchestrator issues and replies with Adaptive Cards, gated by `teams_enabled` at parity with Telegram/Discord ([`services/teams-bridge`](services/teams-bridge/)).
- **Hardened model-router tests**: 146 offline tests covering auth, rate limiting, per-tier budget/fallback, Foundry registration, and the OpenAI↔Anthropic translation layer, guarding the silent-downgrade and budget-exhaustion paths.
- **Observability module**: opt-in Log Analytics alert rules (watchdog findings, secret expiry, run failures) plus an Azure Monitor workbook, no app changes ([`infrastructure/modules/monitoring`](infrastructure/modules/monitoring/)).

Earlier releases (v1.1, v1.0) are in the [roadmap](#roadmap).

---

## Why AzureAgentForge?

Because real agent systems need boring things that are easy to ignore in a demo:

- private memory
- scoped tool access
- budget limits
- deployment repeatability
- identity-aware model access
- secrets management
- logs and traces
- fallback providers
- human review points
- chat and voice channels
- infrastructure that can be rebuilt

The boring stuff is the production stuff.

AzureAgentForge gives you a practical Azure-native base so you can focus on what the agents should do instead of rebuilding the platform plumbing from scratch.

---

## What it solves

### Memory that stays in your network

Honcho stores per-session and per-user memory in PostgreSQL with pgvector. Agent memory stays inside your Azure network instead of disappearing into someone else's hosted black box.

### Governed memory: admission, trust, and a self-improvement loop

Letting agents write unbounded rows into a vector store is how memory rots. The optional **Memory Governor** (`services/memory-governor/`) sits between the agents and the store as a write-time and read-time choke point: a classifier sorts each observation into one of six classes, an admission pipeline decides whether it's worth persisting (and dedupes near-duplicates), trust is *computed* from provenance + verification + usage rather than stored as a single rotting number, and a four-plane retrieval planner injects only what an agent is allowed to see, ranked by a hybrid of pgvector similarity and trigram match. Background loops sweep expired memory, flag contradictions for review (they never auto-resolve; the operator finalizes), and a watchdog turns recurring failures into durable lessons the planner re-injects into the agent that keeps hitting them.

It ships **disabled**. Every feature flag seeds off, so adding it to a running system changes nothing until you turn a flag on. See [enabling it](docs/design/memory-system.md#17-enabling-governed-memory) and the [architecture reference](docs/design/memory-system.md).

### Model access through Azure AI Foundry

Most LLM integrations are designed to go through Azure AI Foundry first.

That gives the platform a cleaner model gateway and a better security posture for Azure-native environments. The goal is to reduce one-off API key sprawl and make model access feel like part of the platform, not an afterthought.

OpenAI-compatible endpoints remain supported as fallback options.

### Cost control with real budget caps

The model router enforces per-tier daily budgets. Agents on the `economy` tier cannot accidentally burn through the `frontier` model budget.

Two Terraform cost profiles are included:

- `cost-optimized`: targets under $150/month in Azure infrastructure
- `hardened`: zone-redundant posture, private endpoints, and longer log retention

LLM token costs are separate and depend on your provider usage.

### Safe tool use across defined roles

The agent team uses role profiles that define model tier and allowed toolsets. Roles do not get broad capability by accident.

A dedicated `CostGuardian` role exists specifically to watch spend.

### Governance you can watch refuse a dangerous task

When a destructive request lands, say *"delete this resource group"*, the controls
are independent and layered: the orchestrator's scope-guard, forbidden-tool
blocks, role→tier routing, and a destroy-aware approval gate at the IaC layer.
Each is designed so the *default* outcome is "nothing destructive happens," and a
request has to defeat all of them. See it traced end to end, with reproducible
replay fixtures, in the [governance &amp; blast-radius walkthrough](docs/walkthroughs/governance-and-blast-radius.md).

### Deployable on Azure

Terraform provisions the Azure foundation, including:

- Azure Container Apps
- Azure Database for PostgreSQL Flexible Server
- Azure Container Registry
- Azure Key Vault
- Log Analytics
- private networking

CI validates and plans clean on every commit.

### Observability: logs and GenAI traces

Every service logs to a shared Log Analytics workspace. As of v1.3, each model-router LLM call also emits an OpenTelemetry GenAI span (model, token counts, estimated cost) to Application Insights when you set `OBSERVABILITY_ENABLED`. Content is redacted, and the flag is off by default.

You can see what an agent did and what it spent without SSHing into a container and reading logs line by line.

### Built for where people already work

Telegram and Discord can be enabled through Terraform variables:

```hcl
telegram_enabled = true
discord_enabled  = true
```

Both are off by default.

Full Microsoft Teams integration shipped in v1.2: the `teams-bridge` Bot Framework service, with Bot Framework JWT validation added on the inbound endpoint in v1.3. Teams joins Telegram and Discord as a place agents can reach people where they already work.

### Voice as a first-class interface

A future release will include first-class integration with **Microsoft Voice Live** for low-cost, low-latency speech-to-text and text-to-speech.

The goal is simple: agents should not be trapped behind a text box. You should be able to talk to them naturally, interrupt them, hear responses, and use voice where voice makes sense.

---

## What is included today

As of v1.7, AzureAgentForge includes:

- Full Terraform IaC for the Azure foundation
- Two infrastructure cost profiles
- 13 predefined agent roles with tests
- Azure AI Foundry-first model routing pattern
- Model router that runs locally
- OpenAI-compatible fallback support
- Sanitized Dockerfiles and service configuration
- Key Vault-based secret loading
- Log Analytics integration plus opt-in GenAI traces to Application Insights
- Private VNet design
- Local working slice with PostgreSQL and the model router
- Optional Telegram, Discord, and Microsoft Teams surfaces
- Governance & blast-radius walkthrough with reproducible replay fixtures
- Destroy-aware approval gate (Forge Console + reference CI/CD pipeline)
- 14 golden orchestration replay fixtures (agent-behavior regression tests)
- Governed memory: governor service, retrieval planner, background loops, hybrid vector retrieval, schema migrations, and self-improvement watchdog (shipped, flag-gated off)
- Obsidian memory interface: two-way memory↔vault CLI for the governor
- Sandbox execution seam in PaperClip (shipped unwired)
- Automated end-to-end Azure deploy: image build/push, Key Vault seeding, and post-deploy smoke tests, with a turnkey CI/CD setup page in the Forge Console
- Optional Cloudflare-managed ingress (named tunnel + proxied DNS) for exposing a chat surface
- Deploy into an existing VNet (BYO-VNet) or a platform-created one
- Upstream Hermes dependency CVEs remediated at build time, with recurring scanning in CI
- Web-research agent tooling (web read, search, and video-transcript wrappers)
- One-command full local stack (`scripts/local-stack.sh up`)
- Multi-tenant architecture design and early scaffolding
- Governor operator endpoints: read-only inspector summary (`/memory/inspector-summary`), review-queue digest (`/memory-digest`, ship-dark), and escalation SLA auditor (`/escalation-sla`, ship-dark), plus contradiction-sweep performance hardening (trigram index + per-query timeout + recency window)
- Security remediation batch: fail-closed auth, bearer-derived tenant isolation with Postgres RLS, prompt-injection fencing, CSRF/DNS-rebinding guards, and secure-by-default Key Vault/storage firewalls
- Governance examples & samples: `examples/governed-ui-patterns/` (UI pattern library + conformance linter), `samples/foundry-chat-proxy/` (minimal AI Foundry chat backend), and `examples/governed-transaction-saga/` (event-sourced governance core) — self-contained, sanitized, no live Azure needed
- Multi-tenant console demo (`demos/tenant-console/`): static, read-only, fictional tenants, all flags off — the operator-console concept made inspectable without a deployment
- Deployment preflight and operator-gate UX: `./forge --check` offline preflight, named operator sign-off gates in the Forge Console and `AI-ASSISTED-SETUP.md`, advisory inline field validation
- Full grouped release notes in [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md)

The local quickstart brings up PostgreSQL and the model router. The full platform runs locally with one command (`scripts/local-stack.sh up`, or `docker compose --profile full up`). The end-to-end Azure deploy is automated (`scripts/build-and-push.sh`, `scripts/seed-keyvault.sh`, the Forge Console, and the reference deploy pipeline, which now runs green end to end).

`main` also carries an incident-hardening batch ahead of the v1.7 tag above — the agent-loop canary smoke, the vendored-config schema guard, hard cost-envelope enforcement, provider-flexible embeddings, canonical user-peer identity, and vendored incident-fix defaults — merged but not yet released. See [What's new](#whats-new) and [Roadmap](#roadmap).

---

## What's not finished yet

AzureAgentForge is not a one-click SaaS product. Standing up your own instance takes real setup: an Azure subscription, GitHub-to-Azure IAM (OIDC), and a handful of environment-specific values.

Through v1.7 you get the architecture, Terraform, model router, role schema, Docker and config scaffolding, the full local stack, the Forge Console, a reference deploy pipeline that runs green end to end, automated image build/push plus Key Vault seeding, the flag-gated governed-memory stack with its operator endpoints, and the security-hardened service and infrastructure surface. The end-to-end Azure deploy is validated against a clean subscription.

What you get is a foundation you can inspect, fork, improve, and run in your own Azure environment.

---


## AI-assisted setup

Alongside the Forge Console (`./forge`), AzureAgentForge includes an AI-assisted setup path.

Use [`AI-ASSISTED-SETUP.md`](AI-ASSISTED-SETUP.md) with Claude Code, Codex, or another coding agent that can inspect your local repo. The prompt walks the agent through repo discovery, local setup, Azure prerequisites, Terraform deployment, container image build and push, Key Vault configuration, Azure AI Foundry model routing, optional integrations, and post-deployment smoke testing.

This is not a replacement for the installer. It is a guided setup assistant for developers who want help understanding and deploying the repo today.

---

## Quickstart

### Forge Console - the turnkey path

```bash
./forge
```

One command starts a local web console that walks the whole deployment:
prerequisite checks (Terraform, `az` login, Docker), a configuration form
that writes your `terraform.tfvars` (with preview), then live-streamed
`init → validate → plan → apply` in a terminal pane. Local Terraform state
is handled automatically, so a first deploy needs zero pre-provisioned
infrastructure. `apply` and `destroy` require typing the environment name,
so there are no accidental clicks. v1.3 adds a CI/CD Setup page that scaffolds
the OIDC app, Terraform state backend, and GitHub variables for the reference
deploy pipeline. Details and the security model:
[`installer/README.md`](installer/README.md).

Prefer a guided walkthrough with an AI assistant instead? Start with
[`AI-ASSISTED-SETUP.md`](AI-ASSISTED-SETUP.md). Prefer plain commands? Both
manual paths follow.

### Local

```bash
cp .env.example .env

# Fill in one of the following:
# - AZURE_FOUNDRY_ENDPOINT + AZURE_FOUNDRY_API_KEY
# - OPENAI_COMPAT_BASE_URL

docker compose up
```

This starts PostgreSQL and the model router.

The router registers an LLM tier on boot if credentials are present. Without credentials, it starts with no tiers.

PaperClip and Honcho currently require:

```bash
docker compose --profile full up
```

plus upstream sources.

A one-command full local stack is available: `scripts/local-stack.sh up` (see [`docs/local-development.md`](docs/local-development.md)).

See [`docs/getting-started.md`](docs/getting-started.md) for the full local walkthrough.

---

### Azure

The Forge Console automates this path end to end. The equivalent manual
sequence:

```bash
# Initialize Terraform
terraform -chdir=infrastructure/environments/dev init
```

Create `terraform.tfvars` for your subscription and environment values, including:

```hcl
subscription_id             = "..."
location                    = "..."
keyvault_admin_object_ids   = ["..."]
container_registry_name     = "..."
```

Apply with the cost-optimized profile:

```bash
terraform -chdir=infrastructure/environments/dev apply \
  -var-file=../../profiles/cost-optimized.tfvars \
  -var-file=terraform.tfvars
```

This provisions infrastructure. It does not yet build and push all service images or seed every runtime secret.

The complete end-to-end deploy flow shipped in v1.2; see the [deployment walkthrough](docs/getting-started.md#deployment-walkthrough-forge-console).

See [`docs/getting-started.md`](docs/getting-started.md) for the full Azure walkthrough, including Key Vault secret seeding.

---

## Roadmap

**AzureAgentForge builds in this order: foundation and Azure hosting first, then agent governance and safety, then more places people can reach the agents from.** The table below is the current snapshot — what's available now, and what just landed on `main` awaiting a version tag. For the full version-by-version history (every feature v1.0 through v1.7 shipped) and the longer-term list, see [`ROADMAP.md`](ROADMAP.md).

### Available now

**Foundation (Terraform + Azure)**
- ✅ Azure-hosted production stack, open-sourced
- ✅ Full Terraform IaC: Container Apps, PostgreSQL Flexible Server (pgvector), ACR, Key Vault, Log Analytics, private VNet
- ✅ Two cost profiles (cost-optimized < $150/mo, hardened), and CI plans both clean
- ✅ Measured Azure costs from real bills

**Agents & models**
- ✅ 13 predefined agent roles + schema with automated tests
- ✅ Model router (local): Azure AI Foundry primary, OpenAI-compatible fallback
- ✅ Per-tier daily budget caps
- ✅ 14 golden orchestration replay fixtures (agent-behavior regression tests)

**Governance & safety**
- ✅ Role-scoped toolsets + a dedicated `CostGuardian` role
- ✅ Destroy-aware approval gate: Forge Console + reference CI/CD pipeline (OIDC, no stored secrets)
- ✅ Governance & blast-radius walkthrough with demos
- ✅ Key Vault secret pattern + private-by-default networking

**Install & operate**
- ✅ Forge Console (`./forge`): local web installer with live-streamed deploy
- ✅ AI-assisted setup path (Claude Code / Codex)
- ✅ Local working slice (PostgreSQL + model router)
- ✅ Log Analytics integration

**Interfaces & scale**
- ✅ Optional Telegram + Discord surfaces
- ✅ Multi-tenant architecture designed + early scaffolding

**Governed memory** *(shipped, flag-gated off; code bundled + unit-tested in CI, not yet deployed end-to-end)*
- 🧠 Governor service + four-plane retrieval planner + six memory classes + computed trust + admission control + background loops + hybrid pgvector retrieval + the self-improvement watchdog ([`services/memory-governor/`](services/memory-governor/), [`services/watchdog/`](services/watchdog/)). Every feature flag seeds OFF. Architecture + the explicitly-not-built long tail: [`docs/design/memory-system.md`](docs/design/memory-system.md).

**Security & multi-tenant hardening (v1.7)**
- ✅ Fail-closed auth, bearer-derived tenant isolation + Postgres RLS, prompt-injection fencing, CSRF/DNS-rebinding guards, secure-by-default infra firewalls
- ✅ Governance examples & samples, multi-tenant console demo, deployment preflight + named operator gates — see [What's new](#whats-new)

### On `main`, unreleased (post-v1.7, pre-v1.8.0)

An incident-hardening batch, each item closing a way this platform (or the vendored apps it ships) could fail silently. Each is detailed in [What's new](#whats-new) above:

- ✅ Agent-loop canary smoke — proves an agent can complete work end to end, not just that containers start
- ✅ Vendored-config schema guard — catches config drift into a vendored app before it ships
- ✅ Hard cost-envelope enforcement — budget caps now block or downgrade, not just warn
- ✅ Provider-flexible `/v1/embeddings` with an Azure AI Foundry path
- ✅ Canonical user-peer identity, closing a memory-fragmentation risk
- ✅ Vendored incident-fix defaults ported from production incident history

Not yet tagged. Full item-by-item detail, the next-steps queue, and the longer-term roadmap (a second agent runtime, voice, Discord-as-control-plane, complete multi-tenant support, and more): **[`ROADMAP.md`](ROADMAP.md)**.

---

## Microsoft ecosystem alignment

AzureAgentForge is intentionally aligned with where Microsoft is moving the agent platform:

- **Azure AI Foundry** for model access and agent development
- **Foundry Agent Service** as a managed path for hosted agents and scale-out runtime patterns
- **Microsoft Teams** as a primary collaboration surface
- **Microsoft Voice Live** for real-time voice agents
- **Azure Container Apps** for containerized agent services
- **Azure Container Apps Sandboxes** as a future path for safer agentic workload execution
- **Azure AI Search** for private RAG over enterprise data
- **Log Analytics and Application Insights** for operations and troubleshooting
- **Microsoft 365 and Agent 365** as future distribution points where agents can meet users where they already work

AzureAgentForge does not chase every new service that ships. It picks the ones that pull their weight, connecting open-source agent tooling to the Microsoft cloud features that make agent systems safer and easier to operate inside real organizations.

---

## Cost

The `cost-optimized` profile targets under **$150/month** in Azure infrastructure spend.

LLM token costs are billed separately by your provider.

Cost estimates are modeled, not guaranteed. Real cost depends on region, usage, log volume, database sizing, redundancy choices, and model consumption.

See [`docs/cost.md`](docs/cost.md) for the per-service breakdown.

---

## Platform status

This repository is the open, sanitized version of a multi-agent platform running on Azure.

The architecture, IaC, and components are tested through day-to-day use. The repo CI validates Terraform, Docker Compose configuration, agent role definitions, and model router behavior.

The single-tenant stack is the current working path.

Multi-tenant support is designed and partially scaffolded.

---

## Documentation

Start with the row that matches what you're trying to do; each doc opens with its own plain-language orientation.

| Doc | What's in it |
|---|---|
| [`docs/GLOSSARY.md`](docs/GLOSSARY.md) | Plain-language definitions for the AI/agent and engineering jargon used across these docs |
| [`docs/architecture.md`](docs/architecture.md) | System context, Azure architecture, components, data flow, maturity |
| [`docs/getting-started.md`](docs/getting-started.md) | Fork, configure, deploy locally, deploy to Azure |
| [`AI-ASSISTED-SETUP.md`](AI-ASSISTED-SETUP.md) | Claude Code / Codex prompt for guided repo analysis, deployment, and usage |
| [`docs/cost.md`](docs/cost.md) | Per-service infrastructure estimates for both profiles |
| [`docs/security.md`](docs/security.md) | Secrets, network posture, and pre-production checklist |
| [`docs/why-azure.md`](docs/why-azure.md) | The case for building agents on Azure |
| [`docs/agents.md`](docs/agents.md) | The 14-role model and how to add your own |
| [`docs/design/memory-system.md`](docs/design/memory-system.md) | Governed-memory architecture (four planes, six classes, trust model, self-improvement loop); shipped flag-gated off; code under [`services/memory-governor/`](services/memory-governor/) + [`services/watchdog/`](services/watchdog/) |
| [`docs/deploy-pipeline.md`](docs/deploy-pipeline.md) | Reference GitHub Actions deploy pipeline with a destroy-aware approval gate (OIDC, no stored secrets) |
| [`docs/obsidian-memory-interface.md`](docs/obsidian-memory-interface.md) | Two-way memory ↔ Obsidian vault CLI: export governed memory, curate in Obsidian, sync edits back |
| [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md) | Full grouped v1.7.0 release notes: platform features, security, examples & samples, docs & dependencies, upgrade notes |
| [`docs/design/vendored-config-schema-guard.md`](docs/design/vendored-config-schema-guard.md) | Why config drift into a vendored app fails silently, the `validate-vendored-config` CI job that closes it, and the per-app validation strategy (Honcho, Hermes, PaperClip) |

---

## Built on

AzureAgentForge is intentionally built on strong open-source projects instead of reinventing every layer.

| Project | Role |
|---|---|
| [PaperClip](https://github.com/paperclipai/paperclip) | Orchestrator UI and agent coordination layer |
| [Hermes](https://github.com/NousResearch/Hermes) | Agent runtime |
| Second runtime | Planned alternative agent runtime |
| [Honcho](https://github.com/plastic-labs/honcho) | Self-hosted agent memory |
| [Cloudflared](https://github.com/cloudflare/cloudflared) | Optional tunnel ingress |

---

## Security notes

AzureAgentForge is designed with a private-by-default posture:

- services run inside a private VNet
- secrets are loaded from Key Vault
- model routing is designed around Azure AI Foundry first
- managed identity and Entra ID patterns are preferred where supported
- memory is stored in PostgreSQL with pgvector
- chat bridges are disabled by default
- Application Insights is opt-in
- hardened profile supports stronger production posture
- upstream Hermes dependency CVEs are remediated at build time, with the vulnerable Hermes Node surface excluded from the image and recurring scanning in CI (Dependabot + a `security-checks` job)

Before using this for sensitive workloads, review [`docs/security.md`](docs/security.md), validate your own Azure policies, and complete your own threat model.

You own and control this infrastructure; the security posture is yours to verify.

---

## Contributing

Issues, ideas, and pull requests are welcome.

Good contributions include:

- cleaner setup paths
- better Azure deployment automation
- cost tuning
- additional observability
- safer default policies
- documentation fixes
- agent role improvements
- Microsoft Teams integration
- Microsoft Voice Live integration
- A second agent runtime
- Azure AI Foundry integration improvements
- AI-assisted setup prompt improvements
- tested integrations

Please keep contributions practical. Skip the buzzword bingo and help people run useful agent systems with less chaos.

---

## License

MIT
