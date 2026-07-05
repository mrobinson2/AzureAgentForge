# Teams bridge — Microsoft Teams chat surface (services/teams-bridge).
#
# A Bot Framework messaging endpoint that turns inbound Teams messages into
# PaperClip issues for the Orchestrator and replies with Adaptive Cards — at
# parity with the Discord plugin and the Telegram gateway. Gated OFF by default
# (var.teams_enabled = false); when enabled it's a small stateless FastAPI app.
#
# SECURITY: ingress is INTERNAL by design. Expose /api/messages to Azure Bot
# Service through the platform's Cloudflare tunnel (the same pattern PaperClip
# uses for public ingress), and add Bot Framework JWT validation on the
# messaging endpoint before going live — the bridge currently trusts the
# activity body (called out in services/teams-bridge/README.md). Keeping it
# internal means enabling the variable never exposes an unauthenticated
# message-ingest endpoint on its own.

resource "azurerm_user_assigned_identity" "teams_bridge" {
  count               = var.teams_enabled ? 1 : 0
  name                = "id-teams-bridge-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "teams_bridge_acr_pull" {
  count                = var.teams_enabled ? 1 : 0
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.teams_bridge[0].principal_id
}

resource "azurerm_role_assignment" "teams_bridge_kv_reader" {
  count                = var.teams_enabled ? 1 : 0
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.teams_bridge[0].principal_id
}

resource "azurerm_container_app" "teams_bridge" {
  count                        = var.teams_enabled ? 1 : 0
  name                         = "ca-teams-bridge-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.teams_bridge[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.teams_bridge[0].id
  }

  # The bridge attaches this as the bearer token when creating PaperClip issues.
  secret {
    name                = "paperclip-automation-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-jwt-secret"
    identity            = azurerm_user_assigned_identity.teams_bridge[0].id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "teams-bridge"
      image  = "${var.container_registry_login_server}/teams-bridge:${var.teams_bridge_image_tag}"
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
        # Optional: route Teams messages straight to a specific agent (the
        # Orchestrator). Empty → PaperClip's default routing applies.
        name  = "ORCHESTRATOR_AGENT_ID"
        value = var.teams_orchestrator_agent_id
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
    azurerm_role_assignment.teams_bridge_acr_pull,
    azurerm_role_assignment.teams_bridge_kv_reader,
  ]
}
