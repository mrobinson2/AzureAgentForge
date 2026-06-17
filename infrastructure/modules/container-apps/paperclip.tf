# Paperclip AI Orchestrator — Container App
#
# Paperclip is the control plane for the AI-agent company.
# It exposes a web UI + REST API on port 3100, coordinates agent tasks,
# enforces budgets / approval gates, and runs Hermes as a managed employee
# via the hermes-paperclip-adapter registered in the container image.
#
# Architecture:
#   - Internal-only ingress on port 3100 (not publicly routable from the internet)
#   - Public access via Cloudflare Tunnel → ca-ingress → this app
#   - Public hostname: https://app.example.com (Cloudflare Tunnel)
#   - Azure File Share at /paperclip for persistent company data
#   - PostgreSQL (shared Honcho instance) for structured data
#   - Managed identity → Key Vault for all secrets

# ── Persistent Storage for Paperclip company data ────────────────────────────

resource "azurerm_storage_share" "paperclip_data" {
  name                 = "paperclip-data"
  storage_account_name = azurerm_storage_account.hermes.name # reuse existing SA
  quota                = 10                                  # GiB — company data, tasks, logs
}

resource "azurerm_container_app_environment_storage" "paperclip_data" {
  name                         = "paperclip-data"
  container_app_environment_id = local.container_app_environment_id
  account_name                 = azurerm_storage_account.hermes.name
  share_name                   = azurerm_storage_share.paperclip_data.name
  access_key                   = azurerm_storage_account.hermes.primary_access_key
  access_mode                  = "ReadWrite"
}

# ── User-Assigned Managed Identity for Paperclip ─────────────────────────────

resource "azurerm_user_assigned_identity" "paperclip" {
  name                = "id-paperclip-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "paperclip_acr_pull" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.paperclip.principal_id
}

resource "azurerm_role_assignment" "paperclip_kv_reader" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.paperclip.principal_id
}

# ── Paperclip Container App ───────────────────────────────────────────────────

resource "azurerm_container_app" "paperclip" {
  name                         = "ca-paperclip-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.paperclip.id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.paperclip.id
  }

  # ── Key Vault secrets ─────────────────────────────────────────────────────
  # DATABASE_URL contains the Postgres connection string with credentials.
  # Never stored in plain env vars.
  secret {
    name                = "paperclip-db-url"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-db-url"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # JWT signing secret for authenticated mode (Better Auth / agent JWT)
  secret {
    name                = "paperclip-auth-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-auth-secret"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # Azure AI Foundry project API key — shared across all models in the project.
  # Used by the Hermes agent adapter when Paperclip spawns agent processes.
  secret {
    name                = "ai-foundry-api-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/ai-foundry-api-key"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # Agent JWT signing secret — enables agents to authenticate with the Paperclip API.
  # Without this, all agent API calls return Unauthorized.
  secret {
    name                = "paperclip-agent-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-agent-jwt-secret"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # Paperclip admin seed credentials (first-run bootstrap only)
  secret {
    name                = "paperclip-admin-email"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-admin-email"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }
  secret {
    name                = "paperclip-admin-password"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-admin-password"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # JWT signing secret for the automation auth proxy.
  # Used by auth-proxy.mjs to validate external JWT bearer tokens from
  # automation scripts, CI/CD pipelines, and other API callers.
  # This is SEPARATE from PAPERCLIP_AGENT_JWT_SECRET (which is for internal
  # agent-to-Paperclip auth). Generate with: openssl rand -base64 48
  secret {
    name                = "paperclip-automation-jwt-secret"
    key_vault_secret_id = "${var.key_vault_uri}secrets/paperclip-automation-jwt-secret"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # Google Workspace CLI credentials — service account JSON for Gmail/Calendar/Drive.
  # Used by the gws binary when agents invoke Google Workspace operations.
  secret {
    name                = "gws-credentials"
    key_vault_secret_id = "${var.key_vault_uri}secrets/gws-credentials"
    identity            = azurerm_user_assigned_identity.paperclip.id
  }

  # Brave Search API key — feeds the brave-search wrapper that replaces ddgs
  # for cloud-IP-blocked DuckDuckGo. Free tier is 2,000 queries/month.
  # Provision the KV secret before flipping brave_search_enabled = true:
  #   az keyvault secret set --vault-name aaf-dev-kv \
  #     --name platform-brave-search-api-key --value "<api key>"
  dynamic "secret" {
    for_each = var.brave_search_enabled ? [1] : []
    content {
      name                = "brave-search-api-key"
      key_vault_secret_id = "${var.key_vault_uri}secrets/brave-search-api-key"
      identity            = azurerm_user_assigned_identity.paperclip.id
    }
  }

  # Discord plugin bot token — injected as DISCORD_BOT_TOKEN env var so the
  # paperclip-plugin-discord worker can read it directly without going through
  # ctx.secrets.resolve() (which is gated behind PaperClip's secret-bindings
  # system that isn't easy to provision programmatically). The Phase 1B
  # plugin code prefers process.env.DISCORD_BOT_TOKEN over the ref path.
  # Gated behind discord_enabled so the KV secret is not required when Discord is off.
  dynamic "secret" {
    for_each = var.discord_enabled ? [1] : []
    content {
      name                = "discord-bot-token"
      key_vault_secret_id = "${var.key_vault_uri}secrets/discord-bot-token"
      identity            = azurerm_user_assigned_identity.paperclip.id
    }
  }

  template {
    min_replicas = 1
    max_replicas = 1

    # Persistent company data (tasks, sessions, config) on Azure File Share
    volume {
      name         = "paperclip-data"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.paperclip_data.name
    }

    # Secret volume — mounts KV-backed secrets as files (read-only).
    # gws-credentials lands at /secrets/gws-credentials for the GWS CLI.
    volume {
      name         = "kv-secrets"
      storage_type = "Secret"
    }

    container {
      name   = "paperclip"
      image  = "${var.container_registry_login_server}/paperclip:${var.paperclip_image_tag}"
      cpu    = var.paperclip_cpu    # dev=0.5, prod=1.0
      memory = var.paperclip_memory # dev="1Gi", prod="2Gi"

      # ── Paperclip server config ─────────────────────────────────────────
      env {
        name  = "NODE_ENV"
        value = "production"
      }
      env {
        name  = "PORT"
        value = "3100"
      }
      env {
        name  = "HOST"
        value = "0.0.0.0"
      }
      env {
        name  = "SERVE_UI"
        value = "true"
      }
      env {
        name  = "PAPERCLIP_HOME"
        value = "/paperclip"
      }
      env {
        name  = "PAPERCLIP_INSTANCE_ID"
        value = var.environment
      }
      env {
        name  = "PAPERCLIP_DEPLOYMENT_MODE"
        value = "authenticated"
      }
      env {
        name  = "PAPERCLIP_DEPLOYMENT_EXPOSURE"
        value = "private"
      }
      # Public URL is the Cloudflare Tunnel hostname, not the ACA FQDN.
      # Set this to: https://app.example.com
      # Update with: terraform apply -var="paperclip_public_url=https://app.example.com"
      env {
        name  = "PAPERCLIP_PUBLIC_URL"
        value = var.paperclip_public_url
      }
      # Set to the Cloudflare Tunnel hostname so Paperclip accepts requests from it.
      # Update with: terraform apply -var="paperclip_allowed_hostnames=app.example.com"
      env {
        name  = "PAPERCLIP_ALLOWED_HOSTNAMES"
        value = var.paperclip_allowed_hostnames
      }

      # ── Auth ──────────────────────────────────────────────────────────
      env {
        name        = "BETTER_AUTH_SECRET"
        secret_name = "paperclip-auth-secret"
      }
      # Better Auth uses this for redirect/callback URLs instead of the Host header.
      # Required because cloudflared sends Host=internal-FQDN for ACA routing,
      # but browser redirects must use the public Cloudflare Tunnel hostname.
      env {
        name  = "BETTER_AUTH_URL"
        value = var.paperclip_public_url
      }
      # Agent JWT signing secret — Paperclip uses this to create/verify JWTs
      # that agents pass when calling the Paperclip API (e.g. managing issues).
      env {
        name        = "PAPERCLIP_AGENT_JWT_SECRET"
        secret_name = "paperclip-agent-jwt-secret"
      }

      # ── Automation Auth Proxy ──────────────────────────────────────────
      # JWT signing secret for the automation auth proxy (auth-proxy.mjs).
      # Enables automation scripts and CI/CD to call the PaperClip API with
      # Authorization: Bearer <jwt> instead of scraping session cookies.
      env {
        name        = "PAPERCLIP_AUTOMATION_JWT_SECRET"
        secret_name = "paperclip-automation-jwt-secret"
      }
      env {
        name  = "PAPERCLIP_AUTOMATION_JWT_ISSUER"
        value = "automation-agent"
      }
      env {
        name  = "PAPERCLIP_AUTOMATION_JWT_AUDIENCE"
        value = "paperclip-api"
      }

      # ── Hermes Agent (adapter) ────────────────────────────────────────
      # These env vars are inherited by the Hermes CLI when Paperclip spawns it.
      # Route through the model router on ca-agent-runtime, which handles all
      # Azure AI Foundry models (known tiers + passthrough for any deployed model).
      # The router uses the project API key internally — agents don't need it directly,
      # but OPENAI_API_KEY must be non-empty for the OpenAI SDK to initialize.
      env {
        name        = "OPENAI_API_KEY"
        secret_name = "ai-foundry-api-key"
      }
      env {
        name  = "OPENAI_BASE_URL"
        value = "http://ca-hermes-${var.environment}/v1"
      }
      # Force Hermes to use the custom OpenAI-compatible provider (OPENAI_API_KEY +
      # OPENAI_BASE_URL) instead of auto-detecting gpt-4o-mini as Copilot/Codex.
      env {
        name  = "HERMES_INFERENCE_PROVIDER"
        value = "custom"
      }
      # Hermes home on the persistent Azure File Share.
      # HERMES_DB_PATH is intentionally omitted here — patch-adapter.mjs injects a
      # unique per-session path (/tmp/hermes-<runId>.db) so concurrent sessions never
      # contend for the same SQLite file. A fixed container-level HERMES_DB_PATH would
      # fight the per-session injection and cause "database is locked" on every start.
      env {
        name  = "HERMES_HOME"
        value = "/paperclip/.hermes"
      }

      # ── Honcho Memory Service ─────────────────────────────────────────
      # Connects Hermes agents to the shared Honcho memory store so they
      # can recall facts about Operator, past decisions, preferences, etc.
      # Same config as the standalone Hermes on ca-agent-runtime.
      env {
        name  = "HONCHO_BASE_URL"
        value = "https://${azurerm_container_app.honcho.ingress[0].fqdn}"
      }
      env {
        name  = "HONCHO_API_KEY"
        value = "self-hosted"
      }
      env {
        name = "HONCHO_APP_ID"
        # Honcho workspace name. The Telegram bot's data lives in workspace
        # "hermes" (no env suffix) — keep this aligned so Orchestrator can read/write
        # the same representation. Override with var.honcho_workspace_name if
        # a per-env split is needed in the future.
        value = var.honcho_workspace_name
      }
      # Peer ID Orchestrator queries when answering "what do you know about Operator" —
      # must match the peer the Telegram bot writes to. Discover the right value
      # by running `pc-honcho list-peers` in the container, then re-applying with
      # -var="honcho_user_peer_id=<id>".
      env {
        name  = "HONCHO_USER_PEER_ID"
        value = var.honcho_user_peer_id
      }

      # ── Database ─────────────────────────────────────────────────────────
      env {
        name        = "DATABASE_URL"
        secret_name = "paperclip-db-url"
      }

      # ── Bootstrap admin (used only on first run) ──────────────────────
      env {
        name        = "PAPERCLIP_ADMIN_EMAIL"
        secret_name = "paperclip-admin-email"
      }
      env {
        name        = "PAPERCLIP_ADMIN_PASSWORD"
        secret_name = "paperclip-admin-password"
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.paperclip.client_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
      env {
        name  = "LOG_LEVEL"
        value = "info"
      }

      volume_mounts {
        name = "paperclip-data"
        path = "/paperclip"
      }
      # Read-only secret volume — GWS credentials at /secrets/gws-credentials
      volume_mounts {
        name = "kv-secrets"
        path = "/secrets"
      }

      # ── Google Workspace CLI ───────────────────────────────────────────
      # Path to the KV-injected credential JSON for the gws CLI.
      # Agents invoke `gws` from terminal to access Gmail, Calendar, Drive.
      env {
        name  = "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"
        value = "/secrets/gws-credentials"
      }

      # ── Brave Search ────────────────────────────────────────────────────
      # Feeds /usr/local/bin/brave-search. When brave_search_enabled = false
      # the wrapper is still on disk (baked into the image) but exits with a
      # clear error if invoked, since BRAVE_SEARCH_API_KEY will be unset.
      dynamic "env" {
        for_each = var.brave_search_enabled ? [1] : []
        content {
          name        = "BRAVE_SEARCH_API_KEY"
          secret_name = "brave-search-api-key"
        }
      }

      # ── Workspace dir tmpfs swap ────────────────────────────────────────
      # When true, the entrypoint symlinks the per-agent workspaces dir into
      # /tmp so node-user file writes get full POSIX semantics (Azure File
      # Share SMB has immutable mount-time mode/uid → EACCES for the node
      # user). See docs/runbooks/paperclip-workspace-permissions.md.
      dynamic "env" {
        for_each = var.paperclip_workspaces_tmpfs ? [1] : []
        content {
          name  = "PAPERCLIP_WORKSPACES_TMPFS"
          value = "1"
        }
      }

      # ── Discord plugin bot token ──────────────────────────────────────────
      # Sourced from KV via the discord-bot-token secret ref above. The
      # paperclip-plugin-discord worker reads process.env.DISCORD_BOT_TOKEN
      # in preference to config.discordBotTokenRef (which requires the
      # platform secret-bindings flow that's awkward to provision today).
      # Gated on discord_enabled — both the secret and this env are
      # conditionally absent together so no dangling secret reference.
      dynamic "env" {
        for_each = var.discord_enabled ? [1] : []
        content {
          name        = "DISCORD_BOT_TOKEN"
          secret_name = "discord-bot-token"
        }
      }

      # ── Liveness probe ───────────────────────────────────────────────────
      liveness_probe {
        transport               = "HTTP"
        path                    = "/api/health"
        port                    = 3100
        interval_seconds        = 30
        failure_count_threshold = 3
      }

      # ── Startup probe ────────────────────────────────────────────────────
      # Paperclip runs migrations on first boot — allow up to 3 min.
      startup_probe {
        transport               = "HTTP"
        path                    = "/api/health"
        port                    = 3100
        interval_seconds        = 20
        failure_count_threshold = 10
      }

      readiness_probe {
        transport               = "HTTP"
        path                    = "/api/health"
        port                    = 3100
        interval_seconds        = 15
        failure_count_threshold = 3
        success_count_threshold = 1
      }
    }
  }

  # Internal-only ingress — Paperclip is NOT publicly exposed via ACA.
  # Public access is provided exclusively through the Cloudflare Tunnel
  # (ca-ingress), which proxies app.example.com to
  # http://ca-orchestrator (ACA internal DNS resolves to this app on port 80→3100).
  ingress {
    external_enabled = false
    target_port      = 3100
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.paperclip_acr_pull,
    azurerm_role_assignment.paperclip_kv_reader,
    azurerm_container_app_environment_storage.paperclip_data,
  ]
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "paperclip_fqdn" {
  value       = azurerm_container_app.paperclip.ingress[0].fqdn
  description = "Internal ACA FQDN of Paperclip (not publicly routable). Public access is via https://app.example.com (Cloudflare Tunnel)."
}

output "paperclip_internal_url" {
  value       = "http://ca-paperclip-${var.environment}"
  description = "ACA-internal origin URL for the Cloudflare Tunnel ingress rule. Set this as the Service URL in: Zero Trust → Networks → Tunnels → dev-azureagentforge → Configure → Public Hostnames → app.example.com"
}

output "paperclip_identity_principal_id" {
  value       = azurerm_user_assigned_identity.paperclip.principal_id
  description = "Principal ID of Paperclip managed identity"
}
