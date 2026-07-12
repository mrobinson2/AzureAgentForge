# Slack Integration

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

## Overview

The Slack integration lets users talk to the agent platform from a Slack
channel. Messages flow from Slack → the **slack-bridge** service
([`services/slack-bridge`](../../services/slack-bridge/)) → a PaperClip issue →
the Orchestrator; the agent's reply returns to the channel via
`chat.postMessage`. It is at parity with the Discord, Telegram, and Teams
surfaces. **Disabled by default** — opt in via the `slack_enabled` Terraform
variable.

## Prerequisites

- A Slack workspace where you can create and install a Slack app.
- An Azure Key Vault provisioned by the platform.
- Azure CLI authenticated to the target subscription.

## Setup

### 1. Create a Slack app

1. At <https://api.slack.com/apps> create an app (from scratch).
2. **OAuth & Permissions** → add bot scopes `chat:write` and `channels:history`
   (and `groups:history` for private channels), then **Install to Workspace**
   and copy the **Bot User OAuth Token** (`xoxb-…`).
3. **Basic Information** → copy the **Signing Secret**.
4. **Event Subscriptions** → enable, set the **Request URL** to your public
   bridge URL `https://<your-public-host>/slack/events` (Slack sends the
   `url_verification` challenge here), and subscribe to the bot event
   `message.channels` (and `message.groups` for private channels).

### 2. Store the credentials in Key Vault

```bash
az keyvault secret set --vault-name <your-key-vault-name> \
  --name slack-bot-token --value "<xoxb-...>"
az keyvault secret set --vault-name <your-key-vault-name> \
  --name slack-signing-secret --value "<signing-secret>"
```

### 3. Enable the surface

```hcl
# dev.auto.tfvars (or your environment's tfvars)
slack_enabled               = true
slack_orchestrator_agent_id = ""   # optional — route Slack messages to one agent
```

```bash
terraform plan   # adds ca-slack-bridge-<env> (internal ingress)
terraform apply
```

### 4. Expose the events endpoint (required, not automatic)

The bridge ingress is **internal**. Route `/slack/events` to Slack through the
platform's Cloudflare tunnel (the same pattern PaperClip uses), and make sure
`SLACK_SIGNING_SECRET` is set so the bridge HMAC-verifies every request — see the
[service README security note](../../services/slack-bridge/README.md#security--read-before-enabling).
Enabling the variable alone never exposes an unauthenticated ingest endpoint.

## How it routes

| Slack event | Bridge behavior |
|---|---|
| `url_verification` | Echoes the `challenge` (app-setup handshake). |
| `message` (non-empty text, not bot/self/subtyped) | Files a PaperClip issue (`surface: slack`, the channel + ts in metadata) for the Orchestrator and acks into the channel. |
| reactions, joins, bot/self, subtyped, empty text | Acked with `200` and ignored. |
| Downstream PaperClip failure | Acked with `200 {"queued": false}` — never 5xx, which would make Slack retry-storm. |

## Verify

```bash
cd services/slack-bridge && pip install -r requirements-dev.txt && pytest
```
