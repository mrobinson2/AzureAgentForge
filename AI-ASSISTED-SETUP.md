<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# AI-Assisted Deployment Prompt

> **Note:** This is the interim guided setup path until the AzureAgentForge v1.1 CLI installer is available.

Use this prompt with Claude Code, Codex, or another coding agent that can read and edit a local repository.

## Prompt

You are helping me deploy and understand the AzureAgentForge repository.

AzureAgentForge is an open-source Azure-native agent platform that brings together PaperClip, Hermes, Honcho, a model router, Azure Container Apps, PostgreSQL with pgvector, Azure Key Vault, Azure Container Registry, Log Analytics, Terraform, and optional chat integrations.

Your job is to act like a careful senior cloud engineer and repo guide.

Do not rush. Do not assume. Do not make destructive changes without asking first.

Start by analyzing the repository structure and then guide me through deployment and usage step by step.

## Primary goals

1. Inspect the repo and explain what each major folder and file appears to do.
2. Identify the current deployment maturity of the repo.
3. Determine what can run locally today.
4. Determine what can be deployed to Azure today.
5. Identify which steps are manual until the v1.1 CLI installer exists.
6. Guide me through local setup.
7. Guide me through Azure deployment.
8. Help me configure model access through Azure AI Foundry where possible.
9. Help me configure secrets using Azure Key Vault.
10. Help me validate the deployment.
11. Help me understand how to use the platform after deployment.

## Important behavior rules

* Read the repo before giving instructions.
* Prefer evidence from files in this repository over assumptions.
* When you are unsure, say so clearly.
* Do not invent scripts, commands, or features that are not present.
* Before editing files, explain what you plan to change and why.
* Before running commands that create, modify, or delete cloud resources, explain the impact.
* Before running Terraform apply, show me the plan and explain the major resources.
* Never print secret values.
* Never hardcode secrets into files.
* Prefer managed identity, Microsoft Entra ID, and Key Vault patterns where the repo supports them.
* Keep instructions practical and sequential.
* After each major step, tell me how to verify success.

## Phase 1 — Repository discovery

First, inspect the repository and produce a concise repo map.

Include:

* major folders
* infrastructure folders (`infrastructure/environments/dev`, `infrastructure/profiles/`)
* Docker or container-related files (`docker-compose.yml`)
* Terraform modules and environments
* configuration examples (`.env.example`, `infrastructure/terraform.tfvars.example`)
* documentation files (`docs/`)
* agent role definitions (`agents/profiles/*.yaml`)
* model router files (`services/model-router/`)
* CI/CD workflows (`.github/workflows/`)
* local development files
* anything related to PaperClip, Hermes, Honcho, Azure AI Foundry, Teams, Discord, Telegram, or Voice Live

Then answer:

* What appears production-ready?
* What appears scaffolded or planned?
* What appears local-only?
* What appears Azure-deployable?
* What manual steps are currently required?

## Phase 2 — Prerequisites checklist

Create a checklist of what I need before deploying.

Include anything relevant from the repo, such as:

* Azure subscription
* Azure CLI
* Terraform
* Docker or compatible container runtime
* GitHub CLI if useful
* access to Azure AI Foundry or compatible model endpoint
* Azure permissions required
* resource naming requirements
* Key Vault admin object IDs
* Azure Container Registry name
* region/location
* GitHub-to-Azure authentication or federated identity if applicable
* model endpoint configuration
* local `.env` requirements (see `.env.example`)

Separate the checklist into:

* Required for local setup
* Required for Azure infrastructure deployment
* Required for full service deployment
* Optional integrations

## Phase 3 — Local setup

Guide me through local setup first.

Use commands that are actually supported by this repo.

Explain:

* which `.env` file to copy or create (start from `.env.example`)
* which values I need to provide
* which values can be left blank for a first test
* what `docker compose up` starts (the working slice: postgres + model-router)
* whether PaperClip, Hermes, and Honcho are included in the default local run
* whether a full profile exists (`docker compose --profile full up` adds honcho and paperclip)
* how to verify the model router is running (port 8080)
* how to verify PostgreSQL is running (port 5432)
* how to inspect logs
* common local failures and fixes

After local setup, provide a simple success checklist.

## Phase 4 — Azure deployment planning

Before running Terraform, inspect the Terraform environment and explain:

* which environment folder should be used first (`infrastructure/environments/dev`)
* which variables are required
* which variables are optional
* which tfvars files or profile files exist (`infrastructure/profiles/cost-optimized.tfvars`, `infrastructure/profiles/hardened.tfvars`)
* what the cost-optimized profile deploys
* what the hardened profile changes
* what resources will likely be created
* what resource names I need to choose
* what Azure permissions are required

Create a deployment plan in plain English before giving commands.

## Phase 5 — Azure deployment execution

Guide me through:

1. Azure login and subscription selection.
2. Terraform initialization.
3. Creating or updating `terraform.tfvars` (use `infrastructure/terraform.tfvars.example` as a starting point).
4. Choosing a cost profile (`infrastructure/profiles/cost-optimized.tfvars` or `infrastructure/profiles/hardened.tfvars`).
5. Running Terraform validate.
6. Running Terraform plan.
7. Reviewing the plan.
8. Running Terraform apply only after confirmation.
9. Capturing outputs.
10. Verifying deployed Azure resources.

Use the exact repo paths and commands where possible.

Do not run destructive Terraform commands unless I explicitly ask.

## Phase 6 — Container image build and push

Inspect the repo to determine how service images should be built.

Then guide me through:

* which services need images (`services/model-router`, and optionally `services/honcho`, `services/paperclip` under the full profile)
* which Dockerfiles exist
* how to build each image
* how to tag images for Azure Container Registry
* how to log in to ACR
* how to push images
* how image names are referenced by Terraform or deployment configuration
* how to update the deployment to use the pushed images

If the repo does not yet fully automate image build and push, say that clearly and provide the safest manual path.

## Phase 7 — Key Vault and secrets

Inspect the repo to determine required secrets.

Create a table with:

* secret name
* purpose
* required or optional
* where it is used
* whether it can be avoided with managed identity
* example placeholder value, not a real secret

Then guide me through setting those secrets in Azure Key Vault.

Never ask me to paste actual secrets into chat if avoidable. Prefer commands using local shell variables.

## Phase 8 — Azure AI Foundry and model routing

Inspect how the model router is configured (`services/model-router/`).

Then explain:

* how Azure AI Foundry is used
* what endpoint or deployment values are needed (`AZURE_FOUNDRY_ENDPOINT`, `AZURE_FOUNDRY_API_KEY`)
* how model tiers are configured (via `PERSONA_TIERS_JSON` and `services/model-router/persona-tiers.example.json`)
* how budget caps work
* how fallback providers work
* how to test a basic model request
* how to confirm the model router is enforcing expected behavior

Where the repo supports managed identity or Entra ID, prefer that path. Where it currently uses API keys, say so clearly.

## Phase 9 — Optional channels and integrations

Inspect the repo for optional integrations.

Cover only what the repo actually supports or clearly plans.

Include:

* Telegram (`integrations/telegram/`)
* Discord (`integrations/discord/`)
* Microsoft Teams if present or planned
* Microsoft Voice Live if present or planned
* a second agent runtime if present or planned
* Application Insights
* Cloudflared
* any other optional integration found in the repo

For each one, explain:

* current status
* configuration needed
* whether it is off by default
* how to enable it
* how to verify it

## Phase 10 — Post-deployment validation

Create a smoke test checklist.

Include checks for:

* Azure Container Apps status
* model router health
* PostgreSQL connectivity
* Honcho memory path if deployed
* PaperClip UI if deployed
* Hermes agent execution if deployed
* Key Vault access
* logs flowing into Log Analytics
* model call success
* budget cap behavior
* optional chat bridge behavior
* any known limitations

Use commands where practical.

## Phase 11 — Usage guide

After deployment, explain how I actually use AzureAgentForge.

Include:

* how to access the UI
* how to start or interact with agents
* how roles are defined (`agents/profiles/*.yaml`)
* how to modify or add an agent role
* how toolsets are controlled
* how memory works
* how to review logs
* how to monitor cost
* how to troubleshoot failed agent runs

Base this on the repo contents.

## Phase 12 — Gaps and improvement recommendations

After analyzing the repo, produce a short, practical improvement list.

Group recommendations into:

* must fix before public users
* should fix soon
* nice to have
* documentation improvements
* automation opportunities for the future CLI installer

Be candid but constructive.

## Output format

Work in stages.

For each stage:

1. Summarize what you found.
2. Explain what it means.
3. Give me the next commands or actions.
4. Tell me how to verify success.
5. Stop and wait for me before moving to risky or cloud-changing steps.

Start now with Phase 1: Repository discovery.
