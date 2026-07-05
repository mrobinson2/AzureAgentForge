<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Microsoft Teams Integration

## Overview

The Teams integration lets users talk to the agent platform from a Microsoft
Teams channel or chat. Messages flow from Teams → the **teams-bridge** service
([`services/teams-bridge`](../../services/teams-bridge/)) → a PaperClip issue →
the Orchestrator; the agent's reply returns to the channel as an Adaptive Card.
It is at parity with the Discord and Telegram surfaces. This integration is
**disabled by default** and must be explicitly opted in via the `teams_enabled`
Terraform variable.

## Prerequisites

- An Azure subscription where you can create an **Azure Bot** resource.
- Permission to add an app / bot to your Teams tenant.
- An Azure Key Vault provisioned by the platform (name set in your deployment variables).
- Azure CLI authenticated to the target subscription.

## Setup

### 1. Register an Azure Bot

1. In the Azure portal, create an **Azure Bot** resource (single-tenant or
   multi-tenant) and note its **Microsoft App ID** and generate a **client
   secret** (App password).
2. Under **Channels**, add the **Microsoft Teams** channel.
3. Set the bot's **messaging endpoint** to your public bridge URL (see step 4):
   `https://<your-public-host>/api/messages`.

### 2. Store the credentials in Key Vault

```bash
# The bridge authenticates to the PaperClip API with the platform automation JWT
# secret (already provisioned by the platform). The Bot Framework App ID/secret
# are consumed by the messaging-endpoint JWT validation you add before go-live
# (see the service README "Security" section).
az keyvault secret set --vault-name <your-key-vault-name> \
  --name teams-app-id --value "<microsoft-app-id>"
az keyvault secret set --vault-name <your-key-vault-name> \
  --name teams-app-password --value "<client-secret>"
```

### 3. Enable the surface

```hcl
# dev.auto.tfvars (or your environment's tfvars)
teams_enabled               = true
teams_orchestrator_agent_id = ""   # optional — route Teams messages to one agent
```

```bash
terraform plan   # adds ca-teams-bridge-<env> (internal ingress)
terraform apply
```

### 4. Expose the messaging endpoint (required, not automatic)

The bridge ingress is **internal**. Route `/api/messages` to Azure Bot Service
through the platform's Cloudflare tunnel (the same pattern PaperClip uses), and
**add Bot Framework JWT validation** on the endpoint before going live — see the
[service README security note](../../services/teams-bridge/README.md#security--read-before-enabling).
This is deliberate: enabling the variable alone never exposes an unauthenticated
ingest endpoint.

## How it routes

| Teams activity | Bridge behavior |
|---|---|
| `message` (non-empty text) | Files a PaperClip issue (`surface: teams`, the conversation id in metadata) for the Orchestrator. |
| `typing`, `conversationUpdate`, empty text | Acked with `200` and ignored. |
| Downstream PaperClip failure | Acked with `200 {"queued": false}` — never 5xx, which would make Bot Framework retry-storm. |

## Verify

```bash
cd services/teams-bridge && pip install -r requirements-dev.txt && pytest
```
