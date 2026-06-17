# Hermes Agent Container App — Messaging Gateway + Model Router Sidecar
#
# Two containers share localhost:
#   hermes  — upstream NousResearch gateway (Telegram etc.), no HTTP ingress
#   router  — FastAPI sidecar listening on :8080, routes to Phi-4 / Claude / Kimi
#
# Hermes sets OPENAI_BASE_URL=http://localhost:8080/v1 and never knows which
# model answered. All multi-model logic and budget tracking lives in the router.

# --- User-Assigned Managed Identity for Hermes ---
resource "azurerm_user_assigned_identity" "hermes" {
  name                = "id-hermes-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "hermes_acr_pull" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.hermes.principal_id
}

resource "azurerm_role_assignment" "hermes_ai_user" {
  count                = var.ai_foundry_resource_id != "" ? 1 : 0
  scope                = var.ai_foundry_resource_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.hermes.principal_id
}

resource "azurerm_role_assignment" "hermes_kv_reader" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.hermes.principal_id
}

# --- Hermes + Router Container App ---
resource "azurerm_container_app" "hermes" {
  name                         = "ca-hermes-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.hermes.id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.hermes.id
  }

  # ── Key Vault secrets for Telegram + Router ─────────────────────────────────

  dynamic "secret" {
    for_each = var.telegram_enabled ? [1] : []
    content {
      name                = "telegram-bot-token"
      key_vault_secret_id = "${var.key_vault_uri}secrets/telegram-bot-token"
      identity            = azurerm_user_assigned_identity.hermes.id
    }
  }

  # Phi-4 on Azure AI Foundry
  secret {
    name                = "phi-base-url"
    key_vault_secret_id = "${var.key_vault_uri}secrets/phi-base-url"
    identity            = azurerm_user_assigned_identity.hermes.id
  }
  secret {
    name                = "phi-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/phi-api-key"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  # Kimi-K2.5 on Azure AI Foundry
  secret {
    name                = "kimi-base-url"
    key_vault_secret_id = "${var.key_vault_uri}secrets/kimi-base-url"
    identity            = azurerm_user_assigned_identity.hermes.id
  }
  secret {
    name                = "kimi-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/kimi-api-key"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  # grok-4-1-fast-reasoning on Azure AI Foundry (primary default tier)
  secret {
    name                = "grok-base-url"
    key_vault_secret_id = "${var.key_vault_uri}secrets/grok-base-url"
    identity            = azurerm_user_assigned_identity.hermes.id
  }
  secret {
    name                = "grok-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/grok-api-key"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  # Claude Sonnet 4.6 on Azure AI Foundry
  secret {
    name                = "claude-base-url"
    key_vault_secret_id = "${var.key_vault_uri}secrets/claude-base-url"
    identity            = azurerm_user_assigned_identity.hermes.id
  }
  secret {
    name                = "claude-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/claude-api-key"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  # GPT-4o-mini on Azure AI Foundry (primary default tier)
  secret {
    name                = "gpt4o-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/gpt4o-api-key"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  # Google Workspace CLI credentials — never stored in image or plain env vars
  secret {
    name                = "gws-credentials"
    key_vault_secret_id = "${var.key_vault_uri}secrets/gws-credentials"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  # PaperClip automation JWT — pre-generated token that pc-delegate.sh uses to
  # call the PaperClip auth-proxy on ca-orchestrator on behalf of Hermes agents.
  # Token is generated once with PAPERCLIP_AUTOMATION_JWT_SECRET and stored in KV.
  # Create/rotate with: az keyvault secret set --vault-name aaf-dev-kv
  #   --name platform-paperclip-automation-token --value "<jwt>"
  secret {
    name                = "paperclip-automation-token"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-token"
    identity            = azurerm_user_assigned_identity.hermes.id
  }

  template {
    # 1 replica — Telegram bot must always be polling; multiple would race.
    # No scale rules: ACA Consumption does not expose per-pod CPU metrics to
    # KEDA's resource metrics API, so cpu-based custom_scale_rule causes KEDA
    # to fail, remove the ScaledObject, and ManuallyStopped the containers.
    # With min=max=1 and no rules, KEDA maintains exactly 1 replica silently.
    min_replicas = 1
    max_replicas = 1

    # Persistent Azure File Share mounted at /home/appuser/.hermes.
    # Stores SQLite DB, sessions, and config.yaml across restarts.
    volume {
      name         = "hermes-data"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.hermes_data.name
    }

    # Secret volume — mounts all KV-backed Container App secrets as files.
    # gws-credentials lands at /secrets/gws-credentials.json; gws reads it via
    # GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE. No plaintext secret values in env vars.
    volume {
      name         = "kv-secrets"
      storage_type = "Secret"
    }

    # ── Container 1: Hermes gateway ─────────────────────────────────────────
    container {
      name   = "hermes"
      image  = "${var.container_registry_login_server}/hermes:${var.hermes_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      # Router sidecar is on localhost:8080 — Hermes talks only to the router.
      env {
        name  = "OPENAI_BASE_URL"
        value = "http://localhost:8080/v1"
      }
      env {
        name  = "OPENAI_API_KEY"
        value = "router-internal"
      }
      env {
        # Router tier key. Tiers register under their Foundry deployment
        # NAME, not a short alias — `claude-sonnet-4-6` (Anthropic Messages
        # API via the bypass in services/model-router/main.py), `Kimi-K2.5`,
        # `grok-4-1-fast-reasoning`, `gpt4o-mini`, `phi4`. Using a short
        # alias like "claude" misses the registered tier and falls through
        # to the passthrough fallback which uses /openai/v1 — for Anthropic
        # models that 404s and the router silently retries to gpt4o-mini.
        # Telegram gateway model is configurable via env; Claude Sonnet 4.6 is
        # the default for chat-quality parity with Orchestrator. Watch CostGuardian
        # daily; revert to gpt4o-mini if usage runs hot.
        name  = "OPENAI_MODEL"
        value = "claude-sonnet-4-6"
      }
      env {
        name  = "HERMES_MODEL"
        value = "claude-sonnet-4-6"
      }
      dynamic "env" {
        for_each = var.telegram_enabled ? [1] : []
        content {
          name        = "TELEGRAM_BOT_TOKEN"
          secret_name = "telegram-bot-token"
        }
      }
      env {
        name  = "HONCHO_BASE_URL"
        value = "https://${azurerm_container_app.honcho.ingress[0].fqdn}"
      }
      # _honcho_should_activate() requires a truthy api_key even for self-hosted.
      # Self-hosted Honcho runs with AUTH_USE_AUTH=false so the value is not validated.
      # This ensures Honcho activates on cold starts before the file-share config loads.
      env {
        name  = "HONCHO_API_KEY"
        value = "self-hosted"
      }
      env {
        name  = "HONCHO_APP_ID"
        value = "hermes-${var.environment}"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.hermes.client_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
      env {
        name  = "LOG_LEVEL"
        value = "info" # was "debug" — excessive log volume in deployed environments
      }

      # Tells Hermes where its persistent config/memory files live (Azure File Share).
      # The hermes-data share is mounted at /opt/data (see volume_mounts below).
      env {
        name  = "HERMES_HOME"
        value = "/opt/data"
      }

      # Store state.db on the local container filesystem (/tmp) to avoid SQLite
      # locking failures on Azure File Share (SMB/CIFS). Lost on redeploy, which
      # is acceptable — Honcho holds the persistent memory layer.
      env {
        name  = "HERMES_DB_PATH"
        value = "/tmp/hermes-state.db"
      }

      # Disable Hermes' lazy-install dispatch. Read by `tools/lazy_deps.py:216`
      # (`_allow_lazy_installs`) ahead of the `security.allow_lazy_installs`
      # config check, so this takes effect even if /opt/data/config.yaml is
      # missing or corrupt. Defends ACA's read-only-ish container against
      # runtime `pip install` attempts when a feature AzureAgentForge uses gets shifted
      # from an eager extra to LAZY_DEPS-only in a future Hermes version
      # (Hermes v0.14 made lazy-install the default for several backends;
      # `[messaging,honcho,cron]` extras still cover us today, but this knob
      # turns silent runtime-pip failures into loud `FeatureUnavailable`
      # errors that surface in logs).
      env {
        name  = "HERMES_DISABLE_LAZY_INSTALLS"
        value = "1"
      }

      # Google Workspace CLI credentials — path to the KV-injected OAuth JSON file.
      # gws reads this env var; it is NOT GOOGLE_APPLICATION_CREDENTIALS (ADC).
      # ACA secret volumes name the file after the secret's `name` attribute, so
      # `name = "gws-credentials"` mounts as /secrets/gws-credentials (no .json).
      env {
        name  = "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"
        value = "/secrets/gws-credentials"
      }

      # ── PaperClip delegation (pc-delegate.sh) ───────────────────────────
      # Required by the agent-delegate skill so Hermes agents can create child
      # issues and post comments on PaperClip issues via the automation auth-proxy.
      env {
        name        = "PAPERCLIP_API_KEY"
        secret_name = "paperclip-automation-token"
      }
      env {
        name  = "PAPERCLIP_BASE_URL"
        value = "https://${azurerm_container_app.paperclip.ingress[0].fqdn}"
      }
      env {
        name  = "PAPERCLIP_ORIGIN"
        value = var.paperclip_public_url
      }
      env {
        name  = "PAPERCLIP_COMPANY_ID"
        value = var.paperclip_company_id
      }

      # Azure File Share mounted at /opt/data — all Hermes persistent data lives here:
      # config.yaml, sessions/, skills/, memories/, .env, honcho state.
      # SQLite main DB is intentionally on /tmp (HERMES_DB_PATH) to avoid SMB
      # advisory-lock failures; Honcho/PostgreSQL handles the durable memory layer.
      volume_mounts {
        name = "hermes-data"
        path = "/opt/data"
      }
      # Read-only by default — ACA secret volumes cannot be written to.
      volume_mounts {
        name = "kv-secrets"
        path = "/secrets"
      }
    }

    # ── Container 2: Model router sidecar ────────────────────────────────────
    # Shares localhost with Hermes. Listens on :8080.
    # Hermes never knows which downstream model handled the request.
    container {
      name   = "router"
      image  = "${var.container_registry_login_server}/router:${var.router_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      # ── GPT-4o-mini (primary default tier) ──
      env {
        name        = "GPT4O_API_KEY"
        secret_name = "gpt4o-api-key"
      }

      # ── grok-4-1-fast-reasoning (kept for reference, not used by router) ──
      env {
        name        = "GROK_BASE_URL"
        secret_name = "grok-base-url"
      }
      env {
        name        = "GROK_API_KEY"
        secret_name = "grok-api-key"
      }
      env {
        name  = "GROK_MODEL"
        value = var.grok_model_deployment
      }
      env {
        name  = "GROK_DAILY_BUDGET_USD"
        value = "2.00"
      }
      env {
        # 4096 output tokens avoids the 2-minute TTFT that 32768 caused on Azure
        # AI Foundry — the model pre-allocates the full token budget before returning.
        name  = "GROK_MAX_TOKENS"
        value = "4096"
      }

      # Per-model upstream timeout in seconds. Keep it tight: a longer timeout
      # lets a single hung upstream tier stall the whole fallback chain for
      # minutes. 30s allows 3 tiers × 30s = 90s worst-case router round-trip.
      env {
        name  = "MODEL_TIMEOUT_SECONDS"
        value = "30"
      }

      # ── Phi-4 (budget fallback tier) ──
      env {
        name        = "PHI_BASE_URL"
        secret_name = "phi-base-url"
      }
      env {
        name        = "PHI_API_KEY"
        secret_name = "phi-api-key"
      }
      env {
        name  = "PHI_MODEL"
        value = var.phi_model_deployment
      }
      env {
        name  = "PHI_DAILY_BUDGET_USD"
        value = "0.50"
      }
      # Phi-4 context window is 16,384 tokens total (input + output).
      # 2048 output reservation leaves 14,336 tokens of safe input headroom.
      # Without this cap the router defaults to 4096, meaning any request
      # with >12,288 input tokens will always overflow Phi-4.
      env {
        name  = "PHI_MAX_TOKENS"
        value = "2048"
      }

      # ── Kimi-K2.5 (complex coding / technical tier) ──
      env {
        name        = "KIMI_BASE_URL"
        secret_name = "kimi-base-url"
      }
      env {
        name        = "KIMI_API_KEY"
        secret_name = "kimi-api-key"
      }
      env {
        name  = "KIMI_MODEL"
        value = var.kimi_model_deployment
      }
      env {
        name  = "KIMI_DAILY_BUDGET_USD"
        value = "0.25"
      }
      env {
        name  = "KIMI_MAX_TOKENS"
        value = "8192"
      }

      # ── Claude Sonnet 4.6 (advanced reasoning / psychology tier) ──
      env {
        name        = "CLAUDE_BASE_URL"
        secret_name = "claude-base-url"
      }
      env {
        name        = "CLAUDE_API_KEY"
        secret_name = "claude-api-key"
      }
      env {
        name  = "CLAUDE_MODEL"
        value = var.claude_model
      }
      env {
        name  = "CLAUDE_DAILY_BUDGET_USD"
        value = "0.25"
      }
      env {
        name  = "CLAUDE_MAX_TOKENS"
        value = "4096"
      }

      env {
        name  = "LOG_LEVEL"
        value = "info" # was "debug" — excessive log volume in deployed environments
      }
      # LITELLM_LOG=DEBUG removed — dumps full request/response payloads including tokens

      # ── Startup probe ────────────────────────────────────────────────────────
      # Runs INSTEAD of liveness during initial startup. ACA will not kill the
      # container for liveness failures until this probe succeeds once.
      # Budget: 18 attempts × 10 s = 3-minute startup window.
      # litellm imports, Foundry token refresh, and uvicorn worker spin-up all
      # happen here; 3 minutes is generous but safe.
      startup_probe {
        transport               = "HTTP"
        path                    = "/health"
        port                    = 8080
        interval_seconds        = 20 # 10 (max) × 20 s = 200 s startup window
        failure_count_threshold = 10 # provider max
      }

      # ── Liveness probe ───────────────────────────────────────────────────────
      # Takes over once startup_probe passes. Restarts the router container if
      # /health stops responding — catches hung uvicorn workers.
      # Budget: 3 failures × 30 s = 90 s of unresponsiveness before restart.
      liveness_probe {
        transport               = "HTTP"
        path                    = "/health"
        port                    = 8080
        interval_seconds        = 30
        failure_count_threshold = 3
      }

      # ── Readiness probe ──────────────────────────────────────────────────────
      # Hermes has no ingress so readiness doesn't gate traffic, but it does
      # signal to ACA that the replica is healthy and should be kept Running.
      # More lenient than liveness: 6 failures × 15 s = 90 s grace on transients.
      readiness_probe {
        transport               = "HTTP"
        path                    = "/health"
        port                    = 8080
        interval_seconds        = 15
        failure_count_threshold = 6
        success_count_threshold = 1
      }
    }

  }

  # Internal-only ingress on port 8080 (the router sidecar).
  # Allows other container apps in the ACA environment (e.g. Paperclip) to
  # call the model router at http://ca-agent-runtime for multi-model access.
  ingress {
    external_enabled = false
    target_port      = 8080
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.hermes_acr_pull,
    azurerm_role_assignment.hermes_kv_reader,
    azurerm_container_app.honcho,
    azurerm_container_app_environment_storage.hermes_data,
  ]
}

# --- Outputs ---
output "hermes_identity_principal_id" {
  value       = azurerm_user_assigned_identity.hermes.principal_id
  description = "Principal ID of Hermes managed identity"
}
