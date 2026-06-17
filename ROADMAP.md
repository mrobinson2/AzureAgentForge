<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Roadmap

## v1.0 — foundation (released)

This stack runs in production on Azure; v1.0 is its sanitized, reusable version. Architecture, decisions, and full Terraform IaC are in the repo. Two cost profiles — cost-optimized (targets under $150/month) and hardened (zone-redundant, private endpoints) — and the repo's CI validates and plans both clean. The 13-role agent schema ships with tests. The model-router builds and runs locally: Azure AI Foundry as primary, any OpenAI-compatible endpoint as fallback. PaperClip, Honcho, and the agent-runtime ship as sanitized Dockerfiles and config. Telegram and Discord are each a single Terraform variable. Multi-tenant architecture is designed and partially scaffolded (see [`roadmap/multi-tenant/`](roadmap/multi-tenant/)).

`docker compose up` runs the working slice: Postgres and the model-router.

## v1.1 — shipped

**Forge Console** (`./forge`) — a local web GUI installer that replaced the
originally planned ANSI TUI: preflight checks, an Azure configuration wizard
with tfvars preview, automatic local-state backend handling, and a
live-streamed `init → validate → plan → apply` flow with typed confirmations.
The plan stage is validated against a live subscription (39 resources on the
cost-optimized profile). **Measured cost figures** from real bills landed in
[`docs/cost.md`](docs/cost.md).

**Governance & safety.** Role-scoped toolsets, a dedicated `CostGuardian` role,
and a **destroy-aware approval gate** that lets routine plans apply unattended
but blocks any delete/replace behind explicit human approval — in the
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

## v1.2 — next

Closing the path from "infrastructure provisioned" to "fully running stack in one command".

Shipped as the reference deploy pipeline (validated offline; see [deploy-pipeline.md](docs/deploy-pipeline.md)):

- Service deployment automation: a `build → seed → plan → gate → apply → smoke` pipeline ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)) wrapping the destroy-aware approval gate.
- Image build and push via `az acr build`, no local Docker needed ([`scripts/build-and-push.sh`](scripts/build-and-push.sh)). The self-contained images (model-router, memory-governor, watchdog) build from this repo; the upstream-dependent three (paperclip, honcho, agent-runtime) build once their `apps/` sources are vendored, and the script skips them with a logged reason until then.
- Key Vault secret seeding, idempotent: internal secrets generated, external ones read from the environment ([`scripts/seed-keyvault.sh`](scripts/seed-keyvault.sh)).
- Post-deploy smoke tests with offline unit-tested verdict logic ([`scripts/smoke-test.sh`](scripts/smoke-test.sh) feeding `installer.smoke`).

Still ahead:

- Vendor the upstream PaperClip/Honcho/Hermes sources so the full image set builds, then the one-command full local stack (`docker compose --profile full up`).
- Microsoft Teams integration: shipped as the `teams-bridge` service (a Bot Framework messaging endpoint that files inbound Teams messages as Orchestrator issues and replies with Adaptive Cards), gated by the `teams_enabled` variable at parity with Discord/Telegram ([`services/teams-bridge`](services/teams-bridge/), [`integrations/teams`](integrations/teams/)). Internal ingress by default; going live needs the Cloudflare-tunnel exposure + Bot Framework JWT validation noted in the service README.
- Secret-expiry monitoring goes live: the watchdog detector that lists Key Vault secret/cert expiry and files an issue before a lapsed credential takes down the agents that depend on it. Detector + watchdog wiring shipped and **now unit-tested** (8 boundary tests in `services/watchdog/tests/test_secret_expiry.py`); opt-in via `WATCHDOG_KEY_VAULT_URI`, activates with the first deploy.
- Model-router test coverage hardened: the gateway's auth, rate limiting, request validation, per-tier budget/fallback, Foundry tier registration, the OpenAI↔Anthropic translation layer, and the chat/messages endpoints now have **121 offline tests** alongside the original routing/embeddings suites (`services/model-router/tests/`, 146 total). These guard the silent-downgrade and budget-exhaustion paths that previously only surfaced in production.
- Observability surface in the monitoring module ([`infrastructure/modules/monitoring`](infrastructure/modules/monitoring/)): three Log Analytics alert rules (watchdog critical findings, Key Vault secret expiry, watchdog run failures) wired to an email action group, plus an Azure Monitor workbook for watchdog activity and gateway health. Queries match the services' existing console-log markers — no app changes. Both opt-in (`alert_emails`, `enable_observability_workbook`); the default footprint is unchanged. `terraform validate` clean.
- First fully validated end-to-end Azure deploy from a clean subscription.

## Later

Multi-tenant implementation (the [`roadmap/multi-tenant/`](roadmap/multi-tenant/) design). More chat surfaces. A fuller observability pipeline (distributed tracing, SLO burn-rate alerts) building on the v1.2 alert rules + workbook.
