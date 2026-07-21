<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/azureagentforge-logo-light.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="560">
  </picture>
</p>

<h3 align="center">An open-source Azure foundation for running AI agent teams with private memory, tool control, cost guardrails, chat interfaces, and observability.</h3>

<p align="center">
  <a href="#platform-status"><img src="https://img.shields.io/badge/status-running%20on%20Azure-brightgreen" alt="Status"></a>
  <a href="ROADMAP.md"><img src="https://img.shields.io/badge/release-v1.8.1-blue" alt="Release v1.8.1"></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="#quickstart"><img src="https://img.shields.io/badge/IaC-Terraform-623CE4" alt="Terraform"></a>
  <a href="#why-azureagentforge"><img src="https://img.shields.io/badge/cloud-Azure-0078D4" alt="Azure"></a>
  <a href="docs/walkthroughs/governance-and-blast-radius.md"><img src="https://img.shields.io/badge/demo-governance%20refusal-FF6B00" alt="Governance demo"></a>
</p>

<p align="center"><sub>New here? This README is laid out to read top to bottom in about ten minutes. Unfamiliar term? Check the <a href="docs/GLOSSARY.md">glossary</a>.</sub></p>

---

## AzureAgentForge at a glance

- Run a complete multi-agent platform on **your own computer, in your own Azure subscription, or across both**
- Combines **PaperClip, Hermes, and Honcho** into one integrated and deployable stack
- Uses **Azure AI Foundry-first model routing** with budgets, fallbacks, cost attribution, and enforceable spending controls
- Keeps agent memory in **your PostgreSQL environment**, with optional admission, trust, retrieval, and review controls
- Deploys the Azure foundation through **Terraform**, including compute, networking, secrets, PostgreSQL, and centralized logging
- Adds **human approval and blast-radius controls** around destructive or high-risk actions
- Provides cost-conscious deployment options for both self-hosted-primary and full-Azure configurations

**Open source · MIT licensed · No required AzureAgentForge-hosted SaaS or control plane.** The platform runs in infrastructure you control. It does call the services you point it at — Azure, Azure AI Foundry, your model providers, and package registries — but there is no vendor control plane in the middle.

---

## The real-world problem

Most agent demos look impressive until you try to operate one for real work. Then the practical questions arrive:

- Where does agent memory actually live?
- Which tools is each agent allowed to call?
- What caps the model budget so one agent can't burn through the expensive tier?
- What happened overnight, and which model made which decision at what cost?
- Which actions need a human to approve them before they run?
- Can the whole thing be torn down and rebuilt consistently?
- Can people reach the agents from the tools they already use?

AzureAgentForge is built for that gap between a demo and something you can run.

---

## What AzureAgentForge is

AzureAgentForge does not invent another orchestration engine, agent runtime, or memory store. It integrates three proven open-source tools and builds an operating foundation around them:

- **PaperClip** for orchestration, task/issue management, and the operator UI
- **Hermes** for agent execution
- **Honcho** for private, self-hosted memory

Around those three it adds Terraform for the Azure foundation, PostgreSQL with pgvector, Key Vault, Foundry-first model routing, cost governance, observability, security controls, human-approval gates, and deployment automation.

AzureAgentForge is a production-minded integration and operating foundation for proven open-source agent tools. The infrastructure costs are grounded in a real deployment: the `cost-optimized` Azure profile came in at **~$83/month** in measured infrastructure spend (Central US, single-tenant, continuous operation, Burstable PostgreSQL with no HA, 30-day / 1 GB-per-day log retention) — comfortably under its $150/month target. A `self-hosted-primary` topology that runs the stack on hardware you own, with Azure holding a dormant warm standby over one shared managed PostgreSQL, models to **~$35–45/month** in cloud spend. These are estimated target profiles, not guaranteed bills: your local computer's power and hardware are excluded, LLM token charges are excluded and billed separately by your provider, and regional pricing, activity level, log volume, and redundancy choices all move the number. See [`docs/cost.md`](docs/cost.md) for the per-service breakdown.

This repository is the open, sanitized version of a platform that runs on Azure day to day. What's still unfinished is stated plainly in [what's not finished yet](#whats-not-finished-yet).

---

## AzureAgentForge is for you if…

- You want to move past the chat demo and give agents goals, tools, memory, roles, and budgets
- You want to see what your agents did, what they spent, and where they failed
- You want to experiment in an Azure-aligned way rather than assembling a laptop demo, a hosted memory service, a mystery dashboard, and a pile of API keys
- You are building long-lived or multi-agent workflows that have to survive being operated
- You want to learn from an inspectable open-source stack instead of a black box
- You want to build prototypes without inventing every component yourself

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

You bring the goals. The platform gives your agents a place to work, remember, call tools, stay within budget, talk to people, and leave an audit trail.

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
                    |   Optional Teams / Slack / Telegram / Discord      |
                    |   chat bridges (all off by default)                |
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

## The v1.8 release

**v1.8.1** adds the agent half of memory identity. v1.8.0 gave the human principal one canonical peer; `HONCHO_AGENT_PEER_IDS` now declares which agent slugs are legitimate peers alongside it, and `HONCHO_PEER_ALIASES` rewrites known strays (`operator=user`) at the governor's admission choke point before a write is stored. A peer that is neither the canonical user nor a declared agent is reported, never rejected — losing the memory is worse than storing it where you can see it. Both inputs default to empty, which behaves exactly as before. See [`docs/releases/v1.8.1.md`](docs/releases/v1.8.1.md).

v1.8 is a repair release, not a feature one. v1.7 hardened the platform's security posture; applying that hardening to a real subscription then broke the paths that install and update it. Every item is a defect found by running the thing, plus a guard so it fails loudly next time. No new features, no new flags, no migrations — if your environment is deployed and healthy, v1.8 changes what happens the next time you build an image, seed a vault, or deploy from scratch. Full detail in [`docs/releases/v1.8.0.md`](docs/releases/v1.8.0.md).

**Fresh deploys work again.** The `Deny`-by-default Key Vault and storage firewalls from v1.7 blocked the deploy that creates the platform. Azure Container Apps is not a Key Vault trusted service, so the app subnet now carries service endpoints and is allowlisted alongside the runner's IP; storage rejects `/32`, so single addresses go in bare; and PostgreSQL 15 names its throttling parameter differently than the module assumed.

**The paperclip image builds again.** The build script pinned its own copy of the vendored PaperClip version, which stopped matching the Dockerfile after the upstream bump — so every build silently cloned the wrong upstream and then failed on a workspace package that version doesn't contain. Both values now come from one place.

**A misconfigured database URL fails at seed time instead of six days later.** Postgres reports a wrong *username* as `password authentication failed`, which points at the wrong secret. In the reference deployment that cost a six-day outage. `seed-keyvault.sh` now checks each DSN's user against the server's administrator login — including values kept from an earlier run, which is how the drift actually happened.

**Dependencies.** `services/honcho` moves to Python 3.14, reversing the v1.7 revert now that the smoke suite passes on it. The equivalent `services/paperclip` bump stays open — its smoke job still fails.

## The v1.7 milestone

v1.7 is the release where AzureAgentForge moves from an assembled agent stack toward a validated, governed, observable, and operationally credible platform. It brings together security, governance, memory, deployment, validation, observability, cost control, and agent-loop reliability. The README is not the exhaustive changelog — each area below links to the full detail, and the consolidated technical history lives in [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md).

**End-to-end agent-loop validation.** Earlier smoke tests only confirmed that containers started and health endpoints answered. The new agent-loop canary validates the whole path: issue creation, agent wake, Hermes start, model-router use, terminal tool execution, final disposition, and issue completion. Building it exposed failures the health checks had missed — including an incompatible Python runtime that stopped Hermes from booting inside the shipped image while every health check stayed green. See [`docs/local-development.md`](docs/local-development.md#agent-loop-canary-the-smoke-that-proves-agents-can-work).

**Configuration validation.** CI now checks AAF-supplied config against what the pinned upstream apps actually consume. A renamed or removed upstream setting now fails CI instead of silently changing runtime behavior. Design doc: [`docs/design/vendored-config-schema-guard.md`](docs/design/vendored-config-schema-guard.md).

**Cost governance.** Every model call passes through the router, which supports warn, downgrade, and block enforcement across its supported paths. It attributes model and embedding spend per caller, applies daily budgets, accounts for the native Anthropic path, and enforces spending envelopes. LLM token charges remain separate from infrastructure cost.

**Memory reliability and governance.** The memory story is coherent, and each part is labeled honestly for maturity:

- Canonical human identity resolved the same way across every reader and writer — validated and active
- Governed admission and computed trust, hybrid vector-plus-text retrieval, contradiction review, and expiration — shipped, disabled by default
- Operator summaries and digests: read-only inspector summary (active), review-queue digest and escalation SLA auditor (ship-dark: readable for preview, folded into the daily digest only when a flag is turned on)
- Retrieval-path observability and contradiction-sweep performance hardening — active when governed memory is enabled
- The Obsidian memory interface and self-improvement watchdog — operator-triggered / shipped, flag-gated

Not everything runs automatically. Governed memory ships disabled and must be intentionally enabled. Architecture reference: [`docs/design/memory-system.md`](docs/design/memory-system.md).

**Security and tenant isolation.** Fail-closed authentication (services return `503` rather than run open when an auth secret is unconfigured), tenant identity derived from verified bearer tokens, PostgreSQL row-level security, prompt-injection fencing on untrusted inbound text, CSRF and DNS-rebinding protection, and secure-by-default Key Vault and storage firewalls (`Deny` plus allowlist), alongside dependency remediation and recurring scanning. Grouped detail in [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md).

**Deployment and operations.** A one-command local stack, an Azure deploy validated against a clean subscription, the Forge Console, an offline preflight (`./forge --check`), CI/CD scaffolding with OIDC and Key Vault seeding, image build and push, and operator approval gates. It supports a self-hosted-primary topology with an Azure warm standby and failover tooling, bring-your-own VNet, and optional Cloudflare ingress. Validated paths are distinguished from design references and scaffolds throughout the docs.

**Human oversight and safer autonomy.** A destroy-aware approval gate at the infrastructure layer, an action-approval seam for runtime actions (shipped, not fully wired to live volume), role-based tool access with scope guards and forbidden-tool checks, an escalation SLA auditor (ship-dark), reproducible orchestration fixtures, and an audit trail.

**Integrations and examples.** Optional Teams, Slack, Telegram, and Discord bridges (default off, internal-ingress only), a webhook intake handler, a governed-UI pattern library, a Foundry chat-proxy sample, a governed transaction saga, a multi-tenant console demo, a multi-tenant reference architecture, and a sandbox-provider scaffold. These are labeled as examples and reference implementations, not turnkey production integrations.

For the full item-by-item history — and earlier releases v1.0 through v1.5 — see [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md) and [`ROADMAP.md`](ROADMAP.md).

---

## Why AzureAgentForge?

Real agent systems need more than a model and a prompt. They need memory, identity, scoped tools, budget controls, secrets, logs, approval points, deployment automation, and a way to recover when something fails.

The unglamorous platform work is what makes an agent system operable — and it is exactly the part a five-minute demo skips.

---

## What it solves

### Memory under your control

Honcho stores per-session and per-user memory in PostgreSQL with pgvector, in a database you operate. Your agent memory lives in your environment rather than a hosted black box. Exactly where that database sits — a private Azure network, a machine you own, or a mix — depends on the topology you deploy, so the guarantee is operator control, not a single fixed network.

### Optional governed memory

Left ungoverned, agents write unbounded rows into a vector store and memory rots. The optional Memory Governor (`services/memory-governor/`) sits between the agents and the store and decides what is worth storing, avoids near-duplicates, records where each memory came from and whether it was verified, controls what gets returned to an agent, flags contradictions for a human to review, expires stale memory, and turns repeated failures into reusable lessons.

It ships **disabled**. Every feature flag seeds off, so adding it to a running system changes nothing until you intentionally enable it (and provision an embedding key and pass a live validate). The internals — six memory classes, the four-plane retrieval planner, the scoring model, the SQL, and the ranking — are in the [architecture reference](docs/design/memory-system.md); [enabling it](docs/design/memory-system.md#17-enabling-governed-memory) is documented there too.

### Model access through Azure AI Foundry

Model access is designed to prefer Azure AI Foundry, which gives a cleaner model gateway and a better fit for Azure-native environments. Where a provider supports it, Azure identity is favored over long-lived keys; not every provider does, so this is a preference, not a universal guarantee. OpenAI-compatible endpoints remain supported as fallbacks, and everything reaches models through one consistent router endpoint.

### Budget and cost controls

The router applies per-tier and per-caller budgets and supports warn, downgrade, and block enforcement. An agent on the `economy` tier cannot accidentally spend the `frontier` budget. Spend is attributed per caller, embedding usage is accounted separately, and LLM token charges are kept separate from infrastructure cost. Infrastructure profiles: `cost-optimized` (measured at ~$83/month, under its $150 target), `self-hosted-primary` (~$35–45/month modeled), and `hardened` (~$250+/month, zone-redundant posture and longer retention). See [`docs/cost.md`](docs/cost.md).

### Role and tool controls

Each of the 14 predefined roles gets an intentional model tier and an allowed toolset, so no role gains broad capability by accident. A dedicated `CostGuardian` role exists specifically to watch spend.

### Safer destructive actions

When a destructive request lands — *"delete this resource group"* — the controls are independent and layered: the orchestrator's scope-guard, forbidden-tool blocks, role-to-tier routing, and a destroy-aware approval gate at the infrastructure layer. Multiple independent controls are intended to make the safe outcome the default. See it traced end to end, with reproducible replay fixtures, in the [governance and blast-radius walkthrough](docs/walkthroughs/governance-and-blast-radius.md).

### Repeatable Azure deployment

Terraform provisions the Azure foundation: Azure Container Apps, Azure Database for PostgreSQL Flexible Server, Azure Container Registry, Azure Key Vault, Log Analytics, and private networking. CI validates and plans clean on every commit.

### Logs, traces, and cost visibility

Every service logs to a shared Log Analytics workspace. Optionally, each model-router LLM call emits an OpenTelemetry GenAI trace (model, token usage, estimated cost, correlation ID) to Application Insights, so you can see what ran, which model handled it, what it cost, and where it failed. Content is redacted and observability is off by default (`OBSERVABILITY_ENABLED`).

### Work channels

Agents can be reached from Teams, Slack, Telegram, and Discord, plus a webhook intake path. Teams and Slack are Bot Framework and Events API bridge services; Telegram and Discord are PaperClip bridges. All are off by default and internal-ingress only — going live requires additional exposure (a Cloudflare tunnel) and, for Teams, Bot Framework JWT validation. Treat these as optional, reference-quality bridges rather than fully hardened integrations validated in every environment.

### Voice (planned)

First-class Microsoft Voice Live integration remains planned and is not included as a completed v1.7 capability. See the [Planned and future](#planned-and-future) note below.

---

## Capability status

Maturity labels below are drawn from the tests, flags, and behavior in the repo, not from ambition. Deeper detail for each row lives in the linked docs and in [`docs/releases/v1.7.0.md`](docs/releases/v1.7.0.md).

| Capability | Status |
|---|---|
| Full local agent stack (`scripts/local-stack.sh up`) | Validated |
| Azure Terraform foundation (Container Apps, PostgreSQL + pgvector, ACR, Key Vault, Log Analytics, VNet) | Validated |
| Agent-loop canary (issue → wake → Hermes → router → tool → disposition) | Validated |
| Model routing and cost controls (warn / downgrade / block, per-caller attribution) | Validated |
| 14 predefined agent roles with tests, plus a `CostGuardian` role | Validated |
| 15 golden orchestration replay fixtures (behavior regression tests) | Validated |
| Destroy-aware approval gate (Forge Console + reference CI/CD) | Validated |
| End-to-end Azure deploy (build/push, Key Vault seeding, smoke) against a clean subscription | Validated |
| Self-hosted-primary topology with Azure warm standby + failover tooling | Available |
| Optional Cloudflare ingress, BYO-VNet | Available |
| Security hardening (fail-closed auth, RLS, prompt-injection fencing, secure-by-default firewalls) | Available |
| GenAI observability traces to Application Insights | Shipped, disabled by default |
| Governed memory (governor, retrieval planner, watchdog, migrations) | Shipped, disabled by default |
| Governor ship-dark endpoints (review-queue digest, escalation SLA auditor) | Shipped, disabled by default |
| Obsidian memory interface (two-way memory ↔ vault CLI) | Available (operator-run) |
| Human action-approval seam (`apps/paperclip/approval.mjs`) | Shipped, not fully wired |
| Chat bridges (Teams, Slack, Telegram, Discord) | Available, off by default (internal ingress) |
| Sandbox provider (`local` default; `aca-job` present) | Scaffold (`aca-job` unverified against a live pool) |
| Multi-tenant platform (`experimental/multi-tenant/`, `demos/tenant-console/`) | Reference design and demo |
| Governance examples & samples (UI patterns, chat proxy, transaction saga) | Reference implementations |
| Voice (Microsoft Voice Live and other surfaces) | Planned |

The local quickstart brings up PostgreSQL and the model router; the full platform runs locally with one command; the end-to-end Azure deploy is automated through `scripts/build-and-push.sh`, `scripts/seed-keyvault.sh`, the Forge Console, and the reference deploy pipeline.

<a id="planned-and-future"></a>
**Planned and future.** First-class Microsoft Voice Live integration, a second agent runtime, Discord as a control plane, full multi-tenant support with per-user RBAC, scheduled agent routines, a skills manager, more chat surfaces (WhatsApp, a web widget), the rest of the observability pipeline, private enterprise RAG over Azure AI Search, and Foundry Agent Service / Microsoft 365 publishing are on the roadmap, not shipped. See [`ROADMAP.md`](ROADMAP.md).

---

## What's not finished yet

AzureAgentForge is candid about what it is not:

- not a one-click hosted SaaS
- not a zero-config installer
- not a fully managed commercial platform
- not a finished multi-tenant control plane
- not a guarantee that every optional integration is validated in every environment
- not a system where every governance feature is auto-enabled
- not a replacement for the operator's own Azure, identity, security, and cost decisions

A real deployment still needs an Azure subscription (for the Azure paths), environment-specific configuration, GitHub-to-Azure OIDC (the reference CI/CD path), secrets and model access, a cost review, network and DNS decisions, and operator approval of destructive infrastructure changes.

What AAF provides is an inspectable and reusable foundation that you can run, test, fork, and improve in infrastructure you control.

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

**AzureAgentForge builds in this order: foundation and Azure hosting first, then agent governance and safety, then more places people can reach the agents from.** The table below is the current snapshot of what's available in the v1.7 milestone. For the full version-by-version history and the longer-term list, see [`ROADMAP.md`](ROADMAP.md).

### Available now

**Foundation (Terraform + Azure)**
- ✅ Azure-hosted production stack, open-sourced
- ✅ Full Terraform IaC: Container Apps, PostgreSQL Flexible Server (pgvector), ACR, Key Vault, Log Analytics, private VNet
- ✅ Two cost profiles (cost-optimized < $150/mo, hardened), and CI plans both clean
- ✅ Measured Azure costs from real bills

**Agents & models**
- ✅ 14 predefined agent roles + schema with automated tests
- ✅ Model router (local): Azure AI Foundry primary, OpenAI-compatible fallback
- ✅ Per-tier and per-caller budget caps with warn / downgrade / block enforcement
- ✅ 15 golden orchestration replay fixtures (agent-behavior regression tests)
- ✅ Agent-loop canary — proves an agent can complete work end to end, not just that containers start

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

**Security & multi-tenant hardening**
- ✅ Fail-closed auth, bearer-derived tenant isolation + Postgres RLS, prompt-injection fencing, CSRF/DNS-rebinding guards, secure-by-default infra firewalls
- ✅ Governance examples & samples, multi-tenant console demo, deployment preflight + named operator gates — see [the v1.7 milestone](#the-v17-milestone)

**Reliability hardening**
- ✅ Vendored-config schema guard — catches config drift into a vendored app before it ships
- ✅ Provider-flexible `/v1/embeddings` with an Azure AI Foundry path
- ✅ Canonical user-peer identity, closing a memory-fragmentation risk
- ✅ Vendored incident-fix defaults ported from production incident history

For item-by-item detail, the next-steps queue, and the longer-term roadmap (a second agent runtime, voice, Discord-as-control-plane, complete multi-tenant support, and more), see **[`ROADMAP.md`](ROADMAP.md)**.

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

Three deployment configurations, all excluding LLM token charges (your provider bills those separately) and any local computer's power and hardware:

| Configuration | Azure infra / month | Basis |
|---|---|---|
| `cost-optimized` (Terraform profile) | **~$83** (target < $150) | Measured — Azure Cost Management, Central US, May 2026, single-tenant, continuous operation, Burstable PostgreSQL (no HA), 30-day / 1 GB-per-day logs |
| `self-hosted-primary` (topology) | **~$35–45** | Modeled — stack runs on hardware you own; Azure holds a dormant warm standby over one shared managed PostgreSQL |
| `hardened` (Terraform profile) | **~$250+** | Modeled — zone-redundant posture, longer retention |

These are estimated target profiles, not guaranteed bills. Real cost moves with region, activity level, log volume, database sizing, redundancy choices, and — above all — Azure Files SMB transaction volume. The `cost-optimized` figure is grounded in a live deployment; see [`docs/cost.md`](docs/cost.md) for the per-service breakdown.

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
| [`docs/releases/v1.8.1.md`](docs/releases/v1.8.1.md) | v1.8.1 release notes: agent peer identity, the identity map at admission, and the report-don't-reject contract |
| [`docs/releases/v1.8.0.md`](docs/releases/v1.8.0.md) | v1.8.0 release notes: deploy-path repair after the v1.7 firewall hardening, the DSN username guard, dependency bumps, upgrade notes |
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
