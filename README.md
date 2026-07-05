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
  <a href="ROADMAP.md"><img src="https://img.shields.io/badge/release-v1.3-blue" alt="Release v1.3"></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/IaC-Terraform-623CE4" alt="Terraform"></a>
  <a href="#why-azureagentforge"><img src="https://img.shields.io/badge/cloud-Azure-0078D4" alt="Azure"></a>
  <a href="docs/walkthroughs/governance-and-blast-radius.md"><img src="https://img.shields.io/badge/demo-governance%20refusal-FF6B00" alt="Governance demo"></a>
</p>

---

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

New in v1.3 (since v1.2):

- **GenAI observability, now live**: every LLM call through the model-router emits one OpenTelemetry GenAI span (model, token counts, cost) to Application Insights, behind `OBSERVABILITY_ENABLED` (off by default), with content redacted. The same change closes the Anthropic cost gap: Claude-tier calls return no billed cost from the SDK, so they now carry a list-price estimate and show up in both cost tracking and traces. The span export was fixed and verified against a live Application Insights workspace.
- **Sandbox execution seam**: a provider-pluggable sandbox contract in PaperClip with a `local` adapter and a fail-closed factory, shipped unwired so importing it changes nothing at runtime. It is the seam for an Azure Container Apps dynamic-sessions provider later. 16 unit tests in CI ([`apps/paperclip/sandbox.mjs`](apps/paperclip/sandbox.mjs)).
- **Turnkey CI/CD setup**: the Forge Console gains a CI/CD Setup page that runs [`scripts/scaffold-cicd.sh`](scripts/scaffold-cicd.sh), provisioning the OIDC app, the Terraform state backend, and the GitHub variables the deploy pipeline needs. It runs preview-first and live-streamed, behind an apply-confirmation gate. The reference [`deploy.yml`](.github/workflows/deploy.yml) now runs green end to end against a clean subscription.
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

## The components

- **PaperClip** is the orchestrator: the UI, the issue board, and the work dashboard.
- **Hermes** runs the agents that do the work. A **second agent runtime** is planned as an option.
- **Honcho** is the memory layer that keeps agent context inside your network.
- **Memory Governor** is the optional governance layer over that memory. It decides what's worth remembering, how much to trust it, and what to forget.
- **Model Router** enforces the spending limits.
- **Azure AI Foundry** is the preferred model gateway.
- **Azure** is where all of it runs.

You bring the goals. The platform gives your agents a place to work, remember, call tools, stay within budget, talk to people, and leave an audit trail.

---

## What's under the hood

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

As of v1.3, AzureAgentForge includes:

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

The local quickstart brings up PostgreSQL and the model router. The full platform runs locally with one command (`scripts/local-stack.sh up`, or `docker compose --profile full up`). The end-to-end Azure deploy is automated (`scripts/build-and-push.sh`, `scripts/seed-keyvault.sh`, the Forge Console, and the reference deploy pipeline, which now runs green end to end).

---

## What's not finished yet

AzureAgentForge is not a one-click SaaS product. Standing up your own instance takes real setup: an Azure subscription, GitHub-to-Azure IAM (OIDC), and a handful of environment-specific values.

The v1.3 release gives you the architecture, Terraform, model router, role schema, Docker and config scaffolding, the full local stack, the Forge Console, a reference deploy pipeline that now runs green end to end, and automated image build/push plus Key Vault seeding. The end-to-end Azure deploy is validated against a clean subscription.

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

### Shipped in v1.1

- ✅ Forge Console: local web GUI installer (`./forge`), superseding the planned ANSI TUI
- ✅ AI-assisted setup documentation using Claude Code or Codex
- ✅ Preflight checks (Terraform, `az` login, Docker, subscription detection)
- ✅ Azure configuration wizard (tfvars form with preview + local-state backend handling)
- ✅ Automated Terraform provision flow (live-streamed init/validate/plan/apply with typed confirmations and a destroy-aware apply gate)
- ✅ Reference CI/CD deploy pipeline: GitHub Actions, OIDC auth (no stored secrets), with the same destroy-aware approval gate ([`docs/deploy-pipeline.md`](docs/deploy-pipeline.md))
- ✅ Governance & blast-radius walkthrough with reproducible replay fixtures and demos
- ✅ Governed-memory architecture reference ([`docs/design/memory-system.md`](docs/design/memory-system.md))
- ✅ Measured Azure cost figures based on real bills

### Shipped in v1.2

- ✅ Image build and push automation
- ✅ Key Vault secret seeding
- ✅ Full service deployment automation
- ✅ Smoke tests after deployment
- ✅ One-command full local stack (`docker compose --profile full up`)
- ✅ Full Microsoft Teams integration
- ✅ First fully validated end-to-end Azure deployment from a clean subscription ([walkthrough](docs/getting-started.md#deployment-walkthrough-forge-console))

### Shipped in v1.3

- ✅ GenAI observability: an OpenTelemetry GenAI span per model-router call to Application Insights (flag-gated off), plus a list-price cost estimate for Claude-tier calls
- ✅ Sandbox execution seam in PaperClip: provider-pluggable contract + `local` adapter, shipped unwired
- ✅ Turnkey CI/CD setup: Forge Console page + `scripts/scaffold-cicd.sh` + `scripts/bootstrap.sh`; the reference `deploy.yml` runs green end to end
- ✅ Obsidian memory interface: a two-way `memory ↔ vault` CLI in the governor
- ✅ Governor schema migrations: an idempotent overlay applied on startup
- ✅ Bot Framework JWT validation on the Teams inbound endpoint
- ✅ Upstream security: Hermes Python CVEs remediated at build time, the vulnerable Hermes Node surface kept out of the image (both CI-gated), the auth-proxy CSRF Origin guard with a bounded admin-session TTL, and recurring Dependabot scanning
- ✅ CI least privilege: a two-app OIDC split in `deploy.yml`
- ✅ Cloudflare-managed ingress module (named tunnel + proxied DNS) for public chat-surface exposure
- ✅ Deploy into an existing VNet (BYO-VNet)

### Future releases

- ⬜ A second, alternative agent runtime
- ⬜ Voice: shared infrastructure (streaming STT, low-latency TTS, VAD + barge-in, persona overlay), provider-agnostic across Microsoft Voice Live and other commercial STT/TTS providers
- ⬜ Voice surfaces: Discord voice, a web chat widget, and a phone line (Twilio, with PIN gate, consent, and recording retention)
- ⬜ Discord as a control plane: role-gated slash-command operations, in-channel delegation (plan → execute → result), and an audit feed
- ⬜ Complete multi-tenant implementation, including multiple human users per tenant (per-user identity and RBAC)
- ⬜ Human-in-the-loop approval of agent actions and outputs (beyond the infra destroy gate)
- ⬜ User-defined scheduled agent routines
- ⬜ Skills manager
- ⬜ Artifacts and work products
- ⬜ Synthetic dogfooding: scheduled canary conversations across channels, alerting on repeated failure
- ⬜ Deeper observability pipeline: correlation-id threading, per-agent metric counters, an SLO dashboard, SLO burn-rate alerts, and a `gen_ai.usage` cost metric on top of the v1.3 spans
- ⬜ Cost governance: a daily cost rollup and per-user budget caps on top of the per-tier daily caps
- ⬜ Agent inventory and operational dashboard patterns
- ⬜ Private enterprise RAG patterns with Azure AI Search
- ⬜ Azure Container Apps dynamic-sessions provider behind the v1.3 sandbox seam
- ⬜ Microsoft Foundry Agent Service alignment
- ⬜ Microsoft 365 and Agent 365 publishing exploration
- ⬜ Work IQ API exploration
- ⬜ More chat surfaces (Slack, WhatsApp, web widget)

See [`ROADMAP.md`](ROADMAP.md) for the full roadmap.

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

| Doc | What's in it |
|---|---|
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
