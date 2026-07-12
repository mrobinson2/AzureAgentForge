# Honcho Memory Service Container App
# Memory library for building stateful agents
# Production-grade deployment with PostgreSQL

# --- User-Assigned Managed Identity for Honcho ---
resource "azurerm_user_assigned_identity" "honcho" {
  name                = "id-honcho-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

# --- ACR Pull role for Honcho ---
resource "azurerm_role_assignment" "honcho_acr_pull" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.honcho.principal_id
}

# --- Key Vault Secrets User role ---
resource "azurerm_role_assignment" "honcho_kv_reader" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.honcho.principal_id
}

# --- Honcho Container App ---
resource "azurerm_container_app" "honcho" {
  name                         = "ca-honcho-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single" # was "Multiple" — zombie revisions were the #1 cost driver
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.honcho.id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.honcho.id
  }

  # Dapr REMOVED — no service invocation consumers exist.
  # All callers use the internal FQDN directly.

  # Key Vault secret references
  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.honcho.id
  }

  secret {
    name                = "openai-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/openai-api-key"
    identity            = azurerm_user_assigned_identity.honcho.id
  }

  template {
    # Scale-to-zero in dev. Cold start ~5-10s, acceptable for async agent calls.
    # In prod, override honcho_min_replicas=1 to avoid cold-start latency.
    min_replicas = var.honcho_min_replicas # dev=0, prod=1
    max_replicas = var.honcho_max_replicas # dev=1, prod=3

    container {
      name   = "honcho"
      image  = "${var.container_registry_login_server}/honcho:${var.honcho_image_tag}"
      cpu    = var.honcho_cpu    # dev=0.25, prod=0.5
      memory = var.honcho_memory # dev="0.5Gi", prod="1Gi"

      # Database connection
      env {
        name        = "DB_CONNECTION_URI"
        secret_name = "postgres-connection-string"
      }

      # LLM client
      env {
        name        = "LLM_OPENAI_API_KEY"
        secret_name = "openai-api-key"
      }

      # Optional but recommended to reduce surprise defaults
      env {
        name  = "LOG_LEVEL"
        value = "info"
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.honcho.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }

      # Per-specialist model config — Honcho >= 3.0.7 nested MODEL_CONFIG shape.
      # The pre-3.0.7 flat keys (SUMMARY_PROVIDER, SUMMARY_MODEL, …) were
      # REMOVED upstream and are silently ignored (pydantic-settings
      # extra="ignore"): shipping them makes every specialist fall back to
      # direct-OpenAI gpt-5.4-mini. See services/honcho/README.md; drift here
      # is caught by CI (scripts/validate_vendored_config.py).
      env {
        name  = "SUMMARY_MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }

      env {
        name  = "SUMMARY_MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }

      env {
        name  = "DERIVER_MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }

      env {
        name  = "DERIVER_MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }

      # Dialectic: pin all 5 reasoning levels to the same small model. Nested
      # path is env_prefix DIALECTIC_ + "__" delimiter + the MODEL_CONFIG
      # sub-model: DIALECTIC_LEVELS__<level>__MODEL_CONFIG__<field>.
      # MAX_TOOL_ITERATIONS survived 3.0.7 and stays a per-level field (NOT
      # under MODEL_CONFIG). 3.0.11 fills omitted levels from built-in
      # defaults (openai/gpt-5.4-mini), so pinning all five keeps the model
      # choice ours rather than upstream's.
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MAX_TOOL_ITERATIONS"
        value = "1"
      }

      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MAX_TOOL_ITERATIONS"
        value = "5"
      }

      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MAX_TOOL_ITERATIONS"
        value = "2"
      }

      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MAX_TOOL_ITERATIONS"
        value = "4"
      }

      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MAX_TOOL_ITERATIONS"
        value = "10"
      }

      # FastAPI always serves this when the app is actually up.
      # ACA caps failure_count_threshold at 10; use 20s interval → 200s window.
      startup_probe {
        transport               = "HTTP"
        path                    = "/openapi.json"
        port                    = 8000
        interval_seconds        = 20
        failure_count_threshold = 10
      }

      liveness_probe {
        transport               = "HTTP"
        path                    = "/openapi.json"
        port                    = 8000
        interval_seconds        = 30
        failure_count_threshold = 3
      }

      readiness_probe {
        transport               = "HTTP"
        path                    = "/openapi.json"
        port                    = 8000
        interval_seconds        = 15 # relaxed from 10s — internal API, no need for aggressive polling
        failure_count_threshold = 6
      }
    }

    http_scale_rule {
      name                = "http-scaler"
      concurrent_requests = "5" # lowered to wake from zero faster on first request
    }
  }

  ingress {
    external_enabled = false
    target_port      = 8000
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.honcho_acr_pull,
    azurerm_role_assignment.honcho_kv_reader,
  ]
}

# --- Honcho Deriver Container App ---
# Runs the background worker (message processing, representation building).
# Shares the same managed identity, secrets, and DB as the Honcho API app.
# No ingress — this is a long-running worker, not an HTTP service.
resource "azurerm_container_app" "honcho_deriver" {
  count = var.honcho_deriver_enabled ? 1 : 0

  name                         = "ca-honcho-deriver-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.honcho.id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.honcho.id
  }

  # Share the same secrets as the API app
  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.honcho.id
  }

  secret {
    name                = "openai-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/openai-api-key"
    identity            = azurerm_user_assigned_identity.honcho.id
  }

  template {
    # Scale-to-zero in dev — deriver polls PG so it catches up when it wakes.
    # TODO (medium-term): convert to Container Apps Job triggered on schedule.
    min_replicas = var.honcho_deriver_min_replicas # dev=0, prod=1
    max_replicas = 1

    container {
      name    = "honcho-deriver"
      image   = "${var.container_registry_login_server}/honcho:${var.honcho_image_tag}"
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "src.deriver"]

      env {
        name        = "DB_CONNECTION_URI"
        secret_name = "postgres-connection-string"
      }

      env {
        name        = "LLM_OPENAI_API_KEY"
        secret_name = "openai-api-key"
      }

      env {
        name  = "LOG_LEVEL"
        value = "info" # was "debug" — excessive for deployed environments
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.honcho.client_id
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }

      env {
        name  = "DERIVER_MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }

      env {
        name  = "DERIVER_MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }

      # Force immediate queue processing — bypass the batch token threshold.
      # Without this the deriver waits to accumulate tokens before processing,
      # which means short conversations never trigger representation builds.
      env {
        name  = "DERIVER_FLUSH_ENABLED"
        value = "true"
      }

      # Reduce stale session window so the deriver picks up idle sessions faster.
      env {
        name  = "DERIVER_STALE_SESSION_TIMEOUT_MINUTES"
        value = "1"
      }

      # Summary is also used by the deriver worker
      env {
        name  = "SUMMARY_MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }

      env {
        name  = "SUMMARY_MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }

      # Dialectic: same nested MODEL_CONFIG shape as the API app (the flat
      # pre-3.0.7 keys are removed upstream and silently ignored). Deriver
      # itself doesn't run Dialectic, but shared config init resolves all levels.
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MAX_TOOL_ITERATIONS"
        value = "1"
      }

      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MAX_TOOL_ITERATIONS"
        value = "5"
      }

      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MAX_TOOL_ITERATIONS"
        value = "2"
      }

      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MAX_TOOL_ITERATIONS"
        value = "4"
      }

      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MAX_TOOL_ITERATIONS"
        value = "10"
      }
    }
  }

  # No ingress block — background worker, not an HTTP service

  tags = var.tags

  depends_on = [
    azurerm_container_app.honcho,
    azurerm_role_assignment.honcho_acr_pull,
    azurerm_role_assignment.honcho_kv_reader,
  ]
}

# ── Honcho Deriver as a scheduled Container Apps Job ────────────────────────
# Cost-optimised replacement for the always-on Container App. Runs the same
# deriver process on a cron schedule (default hourly), bounded by a timeout
# so each run is cheap (~10 min of 0.25 vCPU + 0.5 GiB ≈ $0.005/run, ~$0.12/day).
# Trade-off: up to one schedule-interval of recall lag for new sessions.
resource "azurerm_container_app_job" "honcho_deriver" {
  count = var.honcho_deriver_job_enabled ? 1 : 0

  name                         = "caj-honcho-deriver-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  location                     = var.location
  workload_profile_name        = "Consumption"

  replica_timeout_in_seconds = var.honcho_deriver_job_timeout_seconds
  replica_retry_limit        = 1

  schedule_trigger_config {
    cron_expression          = var.honcho_deriver_job_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.honcho.id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.honcho.id
  }

  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.honcho.id
  }

  secret {
    name                = "openai-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/openai-api-key"
    identity            = azurerm_user_assigned_identity.honcho.id
  }

  template {
    container {
      name    = "honcho-deriver"
      image   = "${var.container_registry_login_server}/honcho:${var.honcho_image_tag}"
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python", "-m", "src.deriver"]

      env {
        name        = "DB_CONNECTION_URI"
        secret_name = "postgres-connection-string"
      }
      env {
        name        = "LLM_OPENAI_API_KEY"
        secret_name = "openai-api-key"
      }
      env {
        name  = "LOG_LEVEL"
        value = "info"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.honcho.client_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
      env {
        name  = "DERIVER_MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DERIVER_MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DERIVER_FLUSH_ENABLED"
        value = "true"
      }
      env {
        name  = "DERIVER_STALE_SESSION_TIMEOUT_MINUTES"
        value = "1"
      }
      env {
        name  = "SUMMARY_MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "SUMMARY_MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__minimal__MAX_TOOL_ITERATIONS"
        value = "1"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__low__MAX_TOOL_ITERATIONS"
        value = "5"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__medium__MAX_TOOL_ITERATIONS"
        value = "2"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__high__MAX_TOOL_ITERATIONS"
        value = "4"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__TRANSPORT"
        value = "openai"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__MODEL"
        value = "gpt-4o-mini"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MODEL_CONFIG__THINKING_BUDGET_TOKENS"
        value = "0"
      }
      env {
        name  = "DIALECTIC_LEVELS__max__MAX_TOOL_ITERATIONS"
        value = "10"
      }
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_container_app.honcho,
    azurerm_role_assignment.honcho_acr_pull,
    azurerm_role_assignment.honcho_kv_reader,
  ]
}

# --- Outputs ---
output "honcho_identity_principal_id" {
  value       = azurerm_user_assigned_identity.honcho.principal_id
  description = "Principal ID of Honcho managed identity for Key Vault RBAC"
}

output "honcho_fqdn" {
  value       = azurerm_container_app.honcho.ingress[0].fqdn
  description = "Internal FQDN of Honcho Container App"
}

# honcho_dapr_app_id output removed — Dapr is no longer configured