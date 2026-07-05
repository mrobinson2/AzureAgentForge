<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Discord Integration

## Overview

Discord text integration is provided by the PaperClip Discord plugin. The plugin bridges Discord
text channels to the agent platform, routing messages to the Orchestrator and delivering responses
back to the channel. **Voice and war-room features are intentionally out of scope for this
integration.** This integration is **disabled by default** and must be explicitly opted in via the
`discord_enabled` Terraform variable.

## Prerequisites

- A Discord account with permission to create applications and manage a server.
- An Azure Key Vault provisioned by the platform (name set in your deployment variables).
- Azure CLI authenticated to the target subscription.

## Setup

### 1. Create a Discord application and bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click
   **New Application**.
2. Navigate to the **Bot** tab, click **Add Bot**, and confirm.
3. Under the Bot tab, click **Reset Token** to reveal the bot token. Copy it.
4. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes, then
   select the required bot permissions (at minimum: **Read Messages/View Channels**,
   **Send Messages**, **Read Message History**).
5. Use the generated URL to invite the bot to your Discord server.

### 2. Store the token in Key Vault

```bash
az keyvault secret set --vault-name <your-key-vault-name> \
  --name platform-discord-bot-token --value "<your-discord-bot-token>"
```

The PaperClip Discord plugin reads this secret at startup as `DISCORD_BOT_TOKEN`.

### 3. Enable the integration in Terraform

In your `infrastructure/environments/dev/terraform.tfvars` (or the appropriate environment), set:

```hcl
discord_enabled = true
```

### 4. Apply the configuration

```bash
cd infrastructure/environments/dev
terraform apply
```

Terraform provisions or updates the resources required for the Discord plugin.

## Verify

1. In your Discord server, send a message in a channel the bot has access to.
2. Check the plugin container logs:
   ```bash
   az container logs --name <discord-plugin-container-name> --resource-group <resource-group>
   ```
3. Confirm the message event appears in the logs and that the bot replies in the channel.
