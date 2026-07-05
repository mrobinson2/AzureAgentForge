# Memory Governor — governed memory layer (see docs/design/memory-system.md).
#
# Resources off the governor + watchdog images:
#   ca-memory-governor-<env>  — FastAPI service: /admit, /plan-retrieval,
#                               /memory/* admin, /session-memory, plus the
#                               in-process loops (annotator, scope-watcher,
#                               contradiction sweep, skill miner).
#   caj-memory-sweeper-<env>  — nightly TTL sweep job (governor image).
#   caj-memory-digest-<env>   — daily digest poster (governor image).
#   caj-watchdog-<env>        — self-improvement-loop watchdog (watchdog image).
#
# Everything is gated OFF by default (var.memory_governor_enabled = false) and
# every behavior is feature-flag-gated in-app; with flags off the container is
# an idle, sidecar-class app.
#
# min_replicas = 1 (NOT scale-to-zero): the background loops must stay resident,
# and the planner hook gives the governor a tight latency budget — a cold start
# would make every governed prefetch miss.
#
# Pre-req (one-time, before first apply): seed the shared-secret the auth-proxy
# and in-network callers attach as X-Governor-Key:
#   az keyvault secret set --vault-name <your-key-vault> \
#     --name memory-governor-api-key --value "$(openssl rand -hex 32)"

resource "azurerm_user_assigned_identity" "memory_governor" {
  count               = var.memory_governor_enabled ? 1 : 0
  name                = "id-memory-governor-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "memory_governor_acr_pull" {
  count                = var.memory_governor_enabled ? 1 : 0
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.memory_governor[0].principal_id
}

resource "azurerm_role_assignment" "memory_governor_kv_reader" {
  count                = var.memory_governor_enabled ? 1 : 0
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.memory_governor[0].principal_id
}

resource "azurerm_container_app" "memory_governor" {
  count                        = var.memory_governor_enabled ? 1 : 0
  name                         = "ca-memory-governor-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.memory_governor[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "governor-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/governor-api-key"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "gpt4o-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/gpt4o-api-key"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  # Key for query-side embeddings. The governor embeds the recall query via THIS
  # pod's router sidecar (localhost:8080 — llm.embed() -> /v1/embeddings) so it
  # lands in Honcho's 1536-dim space. Same KV secret Honcho uses. Without it the
  # sidecar 503s on /embeddings and Plane C silently falls back to trigram.
  secret {
    name                = "openai-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/openai-api-key"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  # Automation JWT signing secret — the scope watcher mints short-lived
  # issues:read tokens to ask PaperClip whether task scopes have closed.
  secret {
    name                = "paperclip-automation-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-jwt-secret"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "phi-base-url"
    key_vault_secret_id = "${var.key_vault_uri}secrets/phi-base-url"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "phi-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/phi-api-key"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    # ── Container 1: governor API + background loops ────────────────────────
    container {
      name   = "memory-governor"
      image  = "${var.container_registry_login_server}/memory-governor:${var.memory_governor_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name        = "DATABASE_URL"
        secret_name = "postgres-connection-string"
      }
      env {
        name        = "GOVERNOR_API_KEY"
        secret_name = "governor-api-key"
      }
      env {
        name  = "ROUTER_BASE_URL"
        value = "http://localhost:8080/v1"
      }
      env {
        name  = "CLASSIFIER_MODEL"
        value = "gpt4o-mini"
      }
      env {
        name  = "HONCHO_BASE_URL"
        value = "http://ca-honcho-${var.environment}"
      }
      env {
        # Planner canary allowlist. Empty = planner answers enabled=false for
        # every agent even with MEMORY_PLANNER_ENABLED on. Add slugs one at a time.
        name  = "PLANNER_AGENT_ALLOWLIST"
        value = var.memory_planner_agent_allowlist
      }
      env {
        name  = "PAPERCLIP_BASE_URL"
        value = "http://ca-paperclip-${var.environment}"
      }
      env {
        name        = "PAPERCLIP_AUTOMATION_JWT_SECRET"
        secret_name = "paperclip-automation-jwt-secret"
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
    }

    # ── Container 2: model-router sidecar (classification + embeddings) ─────
    # Same image and contract as the hermes-pod router; the governor only needs
    # the economy classification tier, and daily budget enforcement comes free.
    container {
      name   = "router"
      image  = "${var.container_registry_login_server}/router:${var.router_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name        = "GPT4O_API_KEY"
        secret_name = "gpt4o-api-key"
      }
      # Embeddings tier: the governor's llm.embed() posts to
      # localhost:8080/v1/embeddings. EMBEDDING_BASE_URL unset -> OpenAI.com,
      # matching Honcho's stored 1536-dim doc embeddings.
      env {
        name        = "EMBEDDING_API_KEY"
        secret_name = "openai-api-key"
      }
      env {
        name  = "MODEL_TIMEOUT_SECONDS"
        value = "30"
      }
      # The router registers the phi tier from these env vars at import time, so
      # they must be present even though the governor only calls the economy
      # tier — otherwise the sidecar fails to start.
      env {
        name        = "PHI_BASE_URL"
        secret_name = "phi-base-url"
      }
      env {
        name        = "PHI_API_KEY"
        secret_name = "phi-api-key"
      }
      env {
        name  = "GPT4O_DAILY_BUDGET_USD"
        value = var.memory_classifier_daily_budget_usd
      }
    }
  }

  ingress {
    external_enabled = false
    target_port      = 8090
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.memory_governor_acr_pull,
    azurerm_role_assignment.memory_governor_kv_reader,
  ]
}

# --- Nightly TTL sweeper job (governor image) ---
resource "azurerm_container_app_job" "memory_sweeper" {
  count                        = var.memory_governor_enabled ? 1 : 0
  name                         = "caj-memory-sweeper-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  location                     = var.location
  workload_profile_name        = "Consumption"

  replica_timeout_in_seconds = 600
  replica_retry_limit        = 1

  schedule_trigger_config {
    cron_expression          = var.memory_sweeper_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.memory_governor[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  template {
    container {
      name    = "memory-sweeper"
      image   = "${var.container_registry_login_server}/memory-governor:${var.memory_governor_image_tag}"
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "governor.sweeper"]

      env {
        name        = "DATABASE_URL"
        secret_name = "postgres-connection-string"
      }
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.memory_governor_acr_pull,
    azurerm_role_assignment.memory_governor_kv_reader,
  ]
}

# --- Self-improvement-loop watchdog job (watchdog image) ---
# Runs every 10 minutes; no-ops unless feature_flags.AGENT_EVENTS_ENABLED is on.
resource "azurerm_container_app_job" "watchdog" {
  count                        = var.memory_governor_enabled ? 1 : 0
  name                         = "caj-watchdog-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  location                     = var.location
  workload_profile_name        = "Consumption"

  replica_timeout_in_seconds = 300
  replica_retry_limit        = 0

  schedule_trigger_config {
    cron_expression          = var.watchdog_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.memory_governor[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "paperclip-automation-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-jwt-secret"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "governor-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/governor-api-key"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  template {
    container {
      name   = "watchdog"
      image  = "${var.container_registry_login_server}/watchdog:${var.watchdog_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name        = "AGENT_EVENTS_DSN"
        secret_name = "postgres-connection-string"
      }
      env {
        name  = "WATCHDOG_BASE_URL"
        value = "http://ca-paperclip-${var.environment}"
      }
      env {
        name  = "WATCHDOG_COMPANY_ID"
        value = var.paperclip_company_id
      }
      env {
        name        = "PAPERCLIP_AUTOMATION_JWT_SECRET"
        secret_name = "paperclip-automation-jwt-secret"
      }
      env {
        name  = "GOVERNOR_BASE_URL"
        value = "http://ca-memory-governor-${var.environment}"
      }
      env {
        name        = "GOVERNOR_API_KEY"
        secret_name = "governor-api-key"
      }
      env {
        name  = "GOVERNOR_WORKSPACE"
        value = var.honcho_workspace_name
      }
      # Key Vault secret-expiry monitoring. The watchdog lists secret properties
      # (names + expiry, not values) via its managed identity and files an issue
      # for anything expired or expiring soon. The identity already holds
      # "Key Vault Secrets User". AZURE_CLIENT_ID tells the MSI endpoint which
      # user-assigned identity to mint a token for.
      env {
        name  = "WATCHDOG_KEY_VAULT_URI"
        value = var.key_vault_uri
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.memory_governor[0].client_id
      }
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.memory_governor_acr_pull,
    azurerm_role_assignment.memory_governor_kv_reader,
  ]
}

# --- Daily memory digest poster job (governor image) ---
# No-ops (exit 0) unless DIGEST_WEBHOOK_URL is provided.
resource "azurerm_container_app_job" "memory_digest" {
  count                        = var.memory_governor_enabled && var.memory_digest_webhook_url != "" ? 1 : 0
  name                         = "caj-memory-digest-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  location                     = var.location
  workload_profile_name        = "Consumption"

  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  schedule_trigger_config {
    cron_expression          = var.memory_digest_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.memory_governor[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.memory_governor[0].id
  }

  secret {
    name                = "governor-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/governor-api-key"
    identity            = azurerm_user_assigned_identity.memory_governor[0].id
  }

  template {
    container {
      name    = "memory-digest"
      image   = "${var.container_registry_login_server}/memory-governor:${var.memory_governor_image_tag}"
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "governor.digest_post"]

      env {
        name  = "GOVERNOR_BASE_URL"
        value = "http://ca-memory-governor-${var.environment}"
      }
      env {
        name        = "GOVERNOR_API_KEY"
        secret_name = "governor-api-key"
      }
      env {
        name  = "DIGEST_WEBHOOK_URL"
        value = var.memory_digest_webhook_url
      }
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.memory_governor_acr_pull,
    azurerm_role_assignment.memory_governor_kv_reader,
  ]
}

output "memory_governor_fqdn" {
  value       = var.memory_governor_enabled ? azurerm_container_app.memory_governor[0].ingress[0].fqdn : null
  description = "Internal FQDN of the memory governor (null when disabled)"
}
