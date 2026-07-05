<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Telegram Integration

## Overview

The Telegram integration enables users to interact with the agent platform through a Telegram bot.
Messages flow from the user to Telegram, then to the agent-runtime, and on to the Orchestrator for
processing. Responses follow the reverse path back to the user. This integration is **disabled by
default** and must be explicitly opted in via the `telegram_enabled` Terraform variable.

## Prerequisites

- A Telegram account with access to BotFather.
- An Azure Key Vault provisioned by the platform (name set in your deployment variables).
- Azure CLI authenticated to the target subscription.

## Setup

### 1. Create a Telegram bot

1. Open Telegram and start a conversation with **@BotFather**.
2. Send `/newbot` and follow the prompts to choose a name and username.
3. Copy the **bot token** that BotFather returns (format: `123456789:ABCdef...`).

### 2. Store the token in Key Vault

```bash
az keyvault secret set --vault-name <your-key-vault-name> \
  --name platform-telegram-bot-token --value "<your-telegram-bot-token>"
```

The agent-runtime reads this secret at startup as `TELEGRAM_BOT_TOKEN`.

### 3. Enable the integration in Terraform

In your `infrastructure/environments/dev/terraform.tfvars` (or the appropriate environment), set:

```hcl
telegram_enabled = true
```

### 4. Apply the configuration

```bash
cd infrastructure/environments/dev
terraform apply
```

Terraform provisions or updates the resources required for the Telegram gateway.

## Verify

1. Open Telegram and send a message to your bot.
2. Check the agent-runtime container logs:
   ```bash
   az container logs --name <agent-runtime-container-name> --resource-group <resource-group>
   ```
3. Confirm an inbound message event appears in the logs and that the bot replies.
