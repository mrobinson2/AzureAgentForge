<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Roadmap

## v1.0: foundation (released)

This stack runs in production on Azure; v1.0 is its sanitized, reusable version. Architecture, decisions, and full Terraform IaC are in the repo. Two cost profiles, cost-optimized (targets under $150/month) and hardened (zone-redundant, private endpoints), and the repo's CI validates and plans both clean. The 13-role agent schema ships with tests. The model-router builds and runs locally: Azure AI Foundry as primary, any OpenAI-compatible endpoint as fallback. PaperClip, Honcho, and the agent-runtime ship as sanitized Dockerfiles and config. Telegram and Discord are each a single Terraform variable. Multi-tenant architecture is designed and partially scaffolded (see [`experimental/multi-tenant/`](experimental/multi-tenant/)).

`docker compose up` runs the working slice: Postgres and the model-router.

## v1.1: shipped

**Forge Console** (`./forge`) is a local web GUI installer that replaced the
originally planned ANSI TUI: preflight checks, an Azure configuration wizard
with tfvars preview, automatic local-state backend handling, and a
live-streamed `init → validate → plan → apply` flow with typed confirmations.
The plan stage is validated against a live subscription (39 resources on the
cost-optimized profile). **Measured cost figures** from real bills landed in
[`docs/cost.md`](docs/cost.md).

**Governance & safety.** Role-scoped toolsets, a dedicated `CostGuardian` role,
and a **destroy-aware approval gate** that lets routine plans apply unattended
but blocks any delete/replace behind explicit human approval, in the
Forge Console and as a **reference CI/CD deploy pipeline**
([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml),
[setup](docs/deploy-pipeline.md)) with OIDC auth and no stored secrets. The
[governance & blast-radius walkthrough](docs/walkthroughs/governance-and-blast-radius.md)
traces a destructive request being refused at every layer, backed by **14 golden
orchestration replay fixtures** ([`tests/replay/`](tests/replay/)).

**Governed memory, shipped (flag-gated off).** The four-plane, six-class
[memory model](docs/design/memory-system.md), with admission control, computed
trust, contradiction detection, hybrid pgvector retrieval, and a
self-improvement loop, now ships as real code under
[`services/memory-governor/`](services/memory-governor/) and
[`services/watchdog/`](services/watchdog/), with ~150 offline tests in CI. Every
flag seeds off, so it stays inert until you enable it. The explicitly-not-built
long tail (reflection pass, inspector UI, in-channel controls, contradiction
auto-resolve) stays design-only.

## v1.2: shipped

Closing the path from "infrastructure provisioned" to "fully running stack in one command".

Shipped as the reference deploy pipeline, now validated end-to-end against a clean subscription (see [deploy-pipeline.md](docs/deploy-pipeline.md) and the [deployment walkthrough](docs/getting-started.md#deployment-walkthrough-forge-console)):

- Service deployment automation: a `build → seed → plan → gate → apply → smoke` pipeline ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)) wrapping the destroy-aware approval gate.
- Image build and push via `az acr build`, no local Docker needed ([`scripts/build-and-push.sh`](scripts/build-and-push.sh)). The self-contained images (model-router, memory-governor, watchdog) build from this repo; the upstream-dependent three (paperclip, honcho, agent-runtime) build once their `apps/` sources are vendored, and the script skips them with a logged reason until then.
- Key Vault secret seeding, idempotent: internal secrets generated, external ones read from the environment ([`scripts/seed-keyvault.sh`](scripts/seed-keyvault.sh)).
- Post-deploy smoke tests with offline unit-tested verdict logic ([`scripts/smoke-test.sh`](scripts/smoke-test.sh) feeding `installer.smoke`).

Also in v1.2:

- ✅ **Done:** vendored the upstream PaperClip/Honcho/Hermes sources so the full image set builds, and shipped the one-command full local stack (`docker compose --profile full up`).
- Microsoft Teams integration: shipped as the `teams-bridge` service (a Bot Framework messaging endpoint that files inbound Teams messages as Orchestrator issues and replies with Adaptive Cards), gated by the `teams_enabled` variable at parity with Discord/Telegram ([`services/teams-bridge`](services/teams-bridge/), [`integrations/teams`](integrations/teams/)). Internal ingress by default; going live needs the Cloudflare-tunnel exposure + Bot Framework JWT validation noted in the service README.
- Secret-expiry monitoring goes live: the watchdog detector that lists Key Vault secret/cert expiry and files an issue before a lapsed credential takes down the agents that depend on it. Detector + watchdog wiring shipped and **now unit-tested** (8 boundary tests in `services/watchdog/tests/test_secret_expiry.py`); opt-in via `WATCHDOG_KEY_VAULT_URI`, activates with the first deploy.
- Model-router test coverage hardened: the gateway's auth, rate limiting, request validation, per-tier budget/fallback, Foundry tier registration, the OpenAI↔Anthropic translation layer, and the chat/messages endpoints now have **121 offline tests** alongside the original routing/embeddings suites (`services/model-router/tests/`, 146 total). These guard the silent-downgrade and budget-exhaustion paths that previously only surfaced in production.
- Observability surface in the monitoring module ([`infrastructure/modules/monitoring`](infrastructure/modules/monitoring/)): three Log Analytics alert rules (watchdog critical findings, Key Vault secret expiry, watchdog run failures) wired to an email action group, plus an Azure Monitor workbook for watchdog activity and gateway health. Queries match the services' existing console-log markers, with no app changes. Both opt-in (`alert_emails`, `enable_observability_workbook`); the default footprint is unchanged. `terraform validate` clean.
- ✅ **Done:** first fully validated end-to-end Azure deploy from a clean subscription; see the [deployment walkthrough](docs/getting-started.md#deployment-walkthrough-forge-console).

## v1.3: shipped

Observability deepened and the agent surface widened. The main features, flag-gated where they touch runtime:

- **GenAI-semconv observability.** Every model call through the [model-router](services/model-router/) emits one OpenTelemetry GenAI-semantic-convention span (model, tokens, cost) to Application Insights, behind `OBSERVABILITY_ENABLED` (default off), content-redacted. The port also **closes the Anthropic cost gap**: Claude-tier calls weren't cost-tracked (the SDK returns no billed cost), so they now carry a list-price estimate and are both tracked and observable. The span export was fixed after release and verified against a live Application Insights workspace. Wired onto the router sidecar in Terraform behind a default-false flag; spans and estimator are offline-tested.
- **ACA Sandboxes: execution seam.** A provider-pluggable sandbox seam ([`apps/paperclip/sandbox.mjs`](apps/paperclip/sandbox.mjs)): the contract, a `local` adapter, and a fail-closed provider factory, shipped **unwired** (importing it changes no runtime behavior). Wiring it into the spawn path and adding an `aca-job` (ACA dynamic-sessions) provider are follow-ons. 16 `node:test` unit tests in CI.
- **Turnkey CI/CD setup page.** The Forge Console gains a **CI/CD Setup** page that runs [`scripts/scaffold-cicd.sh`](scripts/scaffold-cicd.sh) (OIDC app, Terraform state backend, and GitHub variables/secrets/`deploy-destroy` environment) as a live-streamed, **preview-first** operation, with a server-enforced apply-confirmation gate. Provider secrets travel via the subprocess environment, never the command line or logs.
- **Obsidian memory interface.** A two-way `memory ↔ Obsidian vault` CLI ([`governor.vault`](services/memory-governor/src/governor/vault.py)): `export` projects governed memory into a local Obsidian-compatible Markdown+frontmatter vault (the six-class model maps 1:1 onto note frontmatter, so Obsidian *is* the UI, with no frontend to build); `sync` applies operator edits back (delete → forget, frontmatter → confirm/pin/demote/dispute) **conservatively**: it re-fetches server state and skips conflicts, so the governor stays source of truth and nothing is silently clobbered. Local operator CLI, no Azure infra.

Also in v1.3:

- **Upstream security hardening.** The Hermes Python dependency CVEs (the aiohttp, starlette, tornado, and python-multipart DoS cluster on the request path, plus cryptography and pynacl) are remediated by a build-time force-upgrade past Hermes's exact pins, since constraints alone can't override `==` pins. The vulnerable Hermes Node surface (the WhatsApp bridge, which carries the critical `baileys` advisory) is kept out of the agent-runtime image. A `security-checks` CI job gates both, Dependabot runs recurring scans, and the PaperClip auth-proxy gained a fail-closed CSRF Origin guard with a bounded admin-session TTL. The deploy pipeline moved to a two-app OIDC least-privilege split.
- **Cloudflare-managed ingress.** A `cloudflare-tunnel` module (named tunnel, ingress config, proxied DNS record) exposes a chat surface like Teams without a public load balancer ([`infrastructure/modules/cloudflare-tunnel/`](infrastructure/modules/cloudflare-tunnel/)).
- **BYO-VNet.** The network module can deploy into an existing VNet instead of creating its own.
- Bot Framework JWT validation on the Teams inbound endpoint, governor schema migrations (an idempotent schema overlay on Honcho's shared Postgres, applied on startup), and the reference `deploy.yml` validated green end to end against a clean subscription.

## v1.4: shipped

Multi-tenancy, a self-hosted topology, and two more surfaces.

- **Multi-tenant tenant console (reference).** A playbook-driven onboarding control plane ([`experimental/multi-tenant/tenant-console/`](experimental/multi-tenant/tenant-console/)): one tenant contract renders an intake/coordinator agent pack, seeds per-tenant governed memory, provisions an isolated workspace, and enforces a per-tenant daily budget cap. Ships as a badged **reference** (not wired into the single-tenant stack), with a worked field-service example pack.
- **Self-hosted-primary topology.** A `self-hosted-primary` cost profile ([`infrastructure/profiles/self-hosted-primary.tfvars`](infrastructure/profiles/self-hosted-primary.tfvars)) that inverts where compute lives: an always-on machine you own runs the full stack as the primary site ([`deploy/mac-site/`](deploy/mac-site/), [`deploy/windows-site/`](deploy/windows-site/)) with Azure as a dormant warm standby sharing one managed Postgres, so failover is a stateless compute switch. Includes a `scripts/aaf-site` failover/failback helper and an architecture ADR.
- **Vendor-neutral inbound-intake webhook (reference).** A provider-agnostic webhook handler ([`integrations/webhook-intake/`](integrations/webhook-intake/)) that turns an inbound intake/lead payload into a routed Orchestrator issue, with signature verification and a fenced untrusted-content boundary.
- **Slack bridge.** A flag-gated `slack-bridge` service ([`services/slack-bridge/`](services/slack-bridge/)) at parity with Discord/Telegram/Teams: a Slack Events API endpoint that verifies the signing-secret HMAC (with a replay window), turns inbound messages into Orchestrator issues, and replies via `chat.postMessage`. Behind a default-false `slack_enabled` variable; internal ingress; bot token from Key Vault; 20 offline tests.
- **ACA Sandboxes: `aca-job` provider (scaffold).** The v1.3 sandbox seam gains an `aca-job` provider ([`apps/paperclip/sandbox.mjs`](apps/paperclip/sandbox.mjs)) for Azure Container Apps dynamic sessions, with an injectable HTTP transport so the provider is fully unit-tested offline, plus a build-time patch that wires the seam into the adapter spawn path gated on `SANDBOX_PROVIDER`. The single live ACA REST call is isolated and marked **unverified** pending a spike against a real session pool; the default provider stays `local` and `aca-job` is not enabled in any environment.

## v1.5: shipped

Turning the flag-gated differentiators real and closing day-2 operational gaps. Everything is flag-gated / badged for honest maturity, and the default deploy footprint is unchanged.

- **Governed memory, enable-able for real.** The memory-governor now self-provisions its full schema on startup: a `0002` overlay ([`services/memory-governor/migrations/`](services/memory-governor/migrations/)) completes the `documents` governance columns the retrieval planner reads and adds the governor-owned `session_memory` and `skill_candidates` tables, with column set, enum values, and constraint names reconciled to the canonical migrations (`documents.id` confirmed TEXT; `deleted_at`/`sync_state`/`last_sync_at` are Honcho-native). `memory_governor_enabled` is threaded through the deploy pipeline, a `showcase` profile ([`infrastructure/profiles/showcase.tfvars`](infrastructure/profiles/showcase.tfvars)) deploys it with an honest cost + go-live checklist, and every feature flag still seeds **off**. Previously a flag-flip 500'd on missing columns; enabling now still needs an operator-provisioned embedding key + a live validate, both documented.
- **Retrieval observability.** Plane C reports which ranking path ran each turn — `vector` / `trigram` / `trigram_fallback` — surfaced in the retrieval package, the `memory_injected` event, and a `/healthz` `embedding` block (pending embed-queue depth + last sync). A watchdog detector fires a medium finding on sustained vector→trigram degradation, so the otherwise-silent loss of the vector differentiator is observable — the safety net for enabling the governor.
- **`aca-job` sandbox: contract reconciled.** The one Azure-specific REST contract ([`apps/paperclip/sandbox.mjs`](apps/paperclip/sandbox.mjs)) is reconciled to the documented Shell session-pool **executions** API (api-version `2025-10-02-preview`): `/executions` endpoint, `identifier` as a query parameter, a `shellCommand` body with the execution timeout wired through, and a `properties`-nested response mapping. Adds an optional IMDS managed-identity token provider for the `https://dynamicsessions.io` audience. Still marked **unverified against a live pool** (one spike to confirm the response field path); default provider stays `local`.
- **Observability + cost governance.** The model-router emits `gen_ai.usage` metrics (aggregatable token + cost counters by model/tier) alongside the v1.3 spans, threads a `correlation_id` onto the span, and attributes spend per caller (agent/tenant) with an optional per-caller daily cap and a `daily_cost_rollup`. The monitoring module gains an SLO-burn alert on sustained model-router upstream failures/fallbacks and a GenAI cost-per-day workbook tile. All flag-gated and fail-open; 194 offline router tests.
- **Human-in-the-loop action approval.** A provider-pluggable action-approval seam ([`apps/paperclip/approval.mjs`](apps/paperclip/approval.mjs)) extends the v1.1 destroy-aware *infra* gate to runtime *actions* (an outbound message, a destructive tool call). Only kinds in `APPROVAL_REQUIRED_KINDS` are gated (default empty → inert); a gated action with no approver **fails closed**; a `webhook` provider delegates the decision to an external approver and fails closed on any error. Ships **unwired** (mirrors the sandbox seam); 15 offline tests.

## v1.7: shipped

Production feedback folded back into the governor: sweep performance + operator visibility.

- **Contradiction sweep performance hardening.** The sweep's candidate query ([`governor.contradiction`](services/memory-governor/src/governor/contradiction.py)) is a pg_trgm similarity self-join on `documents` — O(n²) pairs, and with no trigram index even ~1k eligible docs (~500k pairs) blow through the pool-wide 30s command timeout, so every pass raised `TimeoutError` and no pair was ever judged (found in production upstream). Migration [`0009`](infrastructure/migrations/0009_contradiction_sweep_perf.sql) adds a `gin_trgm_ops` index on `documents.content` (guarded on `pg_trgm` presence, with an honest built-vs-skipped canary), and the candidate fetch now carries a dedicated per-query timeout (`CONTRADICTION_QUERY_TIMEOUT_S`, default 300s) plus a recency window (`CONTRADICTION_LOOKBACK_DAYS`, default 30; set `0` for a one-off full-corpus pass after the index lands). Regression tests pin the bounds.
- **Read-only memory inspector summary.** `GET /memory/inspector-summary` ([`governor.main`](services/memory-governor/src/governor/main.py)): a workspace-scoped, read-only aggregate for operator inspection — live counts by memory class / verification state / source type (deleted rows excluded), the embedding-sync queue depth + last sync, and a 7-day tally of Plane C ranking modes (`vector` / `trigram` / `trigram_fallback`). No mutation, no new state; same `X-Governor-Key` auth as the rest of the `/memory/*` surface.
- **Daily memory review-queue digest.** `GET /memory-digest` ([`governor.memory_digest`](services/memory-governor/src/governor/memory_digest.py)): a per-workspace, read-only LISTING of what needs operator action — pending pin-candidates, memories the contradiction sweep flagged `needs_review` (with the sweep's suggested resolution from the review note), and `task_scoped` memories expiring within 7 days — so `pc-memory confirm/dispute/supersede` has a worklist instead of the review queue rotting silently. Every section is capped to `limit` (default 10) with an honest "+N more" overflow line, and the raw fetch is bounded (`MEMORY_DIGEST_FETCH_CAP`) so a pathological backlog never loads unbounded rows. Always available for preview; `MEMORY_DIGEST_ENABLED` (seeded **off**, migration [`0010`](infrastructure/migrations/0010_memory_digest_flag.sql)) only gates folding the listing into the daily `/digest` post — with the flag off, `/digest` is byte-for-byte unchanged.
- **Escalation SLA auditor (ship-dark).** `GET /escalation-sla` ([`governor.escalation_sla`](services/memory-governor/src/governor/escalation_sla.py)): an autonomy story ("green acts, yellow drafts, red goes to a human") is only credible if the human side of the handoff is fast — this audits it. An event taxonomy (`escalation_opened`/`escalation_acked`/`escalation_resolved` on the `agent_events` spine, correlated by `payload.escalation_id`) plus a pure pairing/rollup that measures human ack latency against a per-tenant SLA (default 30m, optional business-hours clock, malformed config degrades to the default with a warning — never a crash), where TTL expiry always counts as a breach AND an unresolved escalation — the approval gate's fail-closed posture is made visible, never weakened. The v1.5 approval seam's `autonomy_decision` events serve as retroactive ack+resolution, so historical holds become auditable the moment emitters exist. Read-only throughout: the auditor never approves, extends, or re-routes. No new tables — migration [`0011`](infrastructure/migrations/0011_escalation_sla_flag.sql) is a flag seed only; `ESCALATION_SLA_ENABLED` (seeded **off**) only gates folding the report into the daily `/digest` post — with the flag off, `/digest` is byte-for-byte unchanged. Event emitters land when the HITL approval seam ([`apps/paperclip/approval.mjs`](apps/paperclip/approval.mjs)) is wired for real volume; until then the pipeline is offline-tested and reports the honest zero.

## Later

A second, alternative agent runtime. A voice track: shared infrastructure (streaming STT, low-latency TTS, VAD and barge-in, a persona overlay) that stays provider-agnostic across Microsoft Voice Live and other commercial STT/TTS providers, delivered over three surfaces (Discord voice, a web widget, and a Twilio phone line with a PIN gate, consent, and recording retention). Discord as a control plane: role-gated slash-command operations, in-channel delegation (plan → execute → result), and an audit feed. Complete multi-tenant implementation (the [`experimental/multi-tenant/`](experimental/multi-tenant/) design), including multiple human users per tenant with per-user identity and RBAC. Verifying and enabling the v1.4 `aca-job` sandbox provider against a live Azure Container Apps dynamic-sessions pool. Human-in-the-loop approval of agent actions and outputs, beyond the infrastructure destroy gate. User-defined scheduled agent routines. Synthetic dogfooding: scheduled canary conversations across channels that alert on repeated failure. A skills manager, plus artifacts and work products. More chat surfaces (WhatsApp, a web widget). The rest of the observability pipeline: correlation-id threading, per-agent metric counters, an SLO dashboard, SLO burn-rate alerts, and a `gen_ai.usage` cost metric, building on the v1.3 spans and the v1.2 alert rules and workbook. Cost governance: a daily cost rollup and per-user budget caps on top of the per-tier daily caps. Private enterprise RAG with Azure AI Search, Microsoft Foundry Agent Service alignment, and Microsoft 365 / Agent 365 publishing.
