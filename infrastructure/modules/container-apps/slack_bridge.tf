# Slack bridge — Slack chat surface (services/slack-bridge).
#
# A Slack Events API messaging endpoint that turns inbound Slack messages into
# PaperClip issues for the Orchestrator and replies via chat.postMessage — at
# parity with the Discord plugin, the Telegram gateway, and the Teams bridge.
# Gated OFF by default (var.slack_enabled = false); when enabled it's a small
# stateless FastAPI app.
#
# SECURITY: ingress is INTERNAL by design. Expose /slack/events to Slack through
# the platform's Cloudflare tunnel (the same pattern PaperClip uses for public
# ingress), and set SLACK_SIGNING_SECRET so the bridge HMAC-verifies every
# request before going live (called out in services/slack-bridge/README.md).
# Keeping it internal means enabling the variable never exposes an
# unauthenticated event-ingest endpoint on its own.

resource "azurerm_user_assigned_identity" "slack_bridge" {
  count               = var.slack_enabled ? 1 : 0
  name                = "id-slack-bridge-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "slack_bridge_acr_pull" {
  count                = var.slack_enabled ? 1 : 0
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.slack_bridge[0].principal_id
}

resource "azurerm_role_assignment" "slack_bridge_kv_reader" {
  count                = var.slack_enabled ? 1 : 0
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.slack_bridge[0].principal_id
}

resource "azurerm_container_app" "slack_bridge" {
  count                        = var.slack_enabled ? 1 : 0
  name                         = "ca-slack-bridge-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.slack_bridge[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  # The bridge attaches this as the bearer token when creating PaperClip issues.
  secret {
    name                = "paperclip-automation-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-jwt-secret"
    identity            = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  # Slack app signing secret — HMAC-verifies inbound /slack/events requests.
  secret {
    name                = "slack-signing-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/slack-signing-secret"
    identity            = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  # Bot token (xoxb-…) for chat.postMessage replies.
  secret {
    name                = "slack-bot-token"
    key_vault_secret_id = "${var.key_vault_uri}secrets/slack-bot-token"
    identity            = azurerm_user_assigned_identity.slack_bridge[0].id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "slack-bridge"
      image  = "${var.container_registry_login_server}/slack-bridge:${var.slack_bridge_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "PAPERCLIP_API_URL"
        value = "http://ca-paperclip-${var.environment}"
      }
      env {
        name  = "PAPERCLIP_COMPANY_ID"
        value = var.paperclip_company_id
      }
      env {
        name        = "PAPERCLIP_API_KEY"
        secret_name = "paperclip-automation-jwt-secret"
      }
      env {
        name        = "SLACK_SIGNING_SECRET"
        secret_name = "slack-signing-secret"
      }
      env {
        name        = "SLACK_BOT_TOKEN"
        secret_name = "slack-bot-token"
      }
      env {
        # Optional: route Slack messages straight to a specific agent (the
        # Orchestrator). Empty → PaperClip's default routing applies.
        name  = "ORCHESTRATOR_AGENT_ID"
        value = var.slack_orchestrator_agent_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
    }
  }

  ingress {
    external_enabled = false
    target_port      = 3978
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.slack_bridge_acr_pull,
    azurerm_role_assignment.slack_bridge_kv_reader,
  ]
}
