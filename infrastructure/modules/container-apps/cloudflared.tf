# Cloudflared Tunnel Connector — Container App
#
# Runs the official cloudflare/cloudflared image as a dedicated container app.
# It connects outbound to the existing Cloudflare Tunnel (dev-azureagentforge) and
# proxies traffic for app.example.com to the internal Paperclip
# service running in the same ACA environment.
#
# Tunnel details:
#   Name:    dev-azureagentforge
#   ID:      (your tunnel UUID — visible in Zero Trust → Networks → Tunnels)
#   Domain:  example.com
#   Hostname: app.example.com
#
# Traffic flow:
#   Internet → Cloudflare Edge
#     → Tunnel (dev-azureagentforge)
#       → cloudflared (this container)
#         → http://ca-orchestrator (ACA internal DNS, port 80)
#           → Paperclip container on :3100
#
# cloudflared makes outbound-only connections — no ingress block required.
# The ingress rule (hostname → origin URL) lives in the Cloudflare dashboard:
#   Zero Trust → Networks → Tunnels → dev-azureagentforge → Configure → Public Hostnames
#   Hostname: app.example.com
#   Service:  http://ca-orchestrator  (ACA resolves this internally to port 3100)
#
# Required KV secret (must be created manually before terraform apply):
#   platform-cloudflared-token
#   Value: the connector token from the Cloudflare dashboard
#     Zero Trust → Networks → Tunnels → dev-azureagentforge → Connectors → Add a connector
#     Copy the token from the `cloudflared tunnel run --token <TOKEN>` command shown

# ── Managed Identity ──────────────────────────────────────────────────────────
# Minimal identity — only needs Key Vault Secrets User to read the tunnel token.
# No AcrPull role needed: cloudflare/cloudflared is a public Docker Hub image.

resource "azurerm_user_assigned_identity" "cloudflared" {
  count               = var.cloudflared_enabled ? 1 : 0
  name                = "id-cloudflared-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "cloudflared_kv_reader" {
  count                = var.cloudflared_enabled ? 1 : 0
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.cloudflared[0].principal_id
}

# ── Cloudflared Container App ─────────────────────────────────────────────────

resource "azurerm_container_app" "cloudflared" {
  count                        = var.cloudflared_enabled ? 1 : 0
  name                         = "ca-cloudflared-${var.environment}"
  container_app_environment_id = local.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cloudflared[0].id]
  }

  # No registry block — cloudflare/cloudflared is pulled from Docker Hub (public image).
  # No ACR credentials required.

  # Tunnel token read from Key Vault at runtime via managed identity.
  # cloudflared reads TUNNEL_TOKEN automatically when running `tunnel run`.
  secret {
    name                = "cf-tunnel-token"
    key_vault_secret_id = "${var.key_vault_uri}secrets/cf-tunnel-token"
    identity            = azurerm_user_assigned_identity.cloudflared[0].id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "cloudflared"
      image  = "cloudflare/cloudflared:latest"
      cpu    = 0.25
      memory = "0.5Gi"

      # TUNNEL_TOKEN is read automatically by cloudflared tunnel run.
      env {
        name        = "TUNNEL_TOKEN"
        secret_name = "cf-tunnel-token"
      }

      # --no-autoupdate: disable built-in self-update (image version is managed
      # by the Terraform variable / ACA revision).
      # Tunnel ingress rules (hostname → origin) are pulled from the Cloudflare
      # dashboard — no local config file required with token-based auth.
      args = ["tunnel", "--no-autoupdate", "run"]

      # cloudflared exposes a built-in metrics and readiness endpoint on :2000.
      liveness_probe {
        transport               = "HTTP"
        path                    = "/ready"
        port                    = 20241
        interval_seconds        = 30
        failure_count_threshold = 3
      }

      readiness_probe {
        transport               = "HTTP"
        path                    = "/ready"
        port                    = 20241
        interval_seconds        = 10
        failure_count_threshold = 3
        success_count_threshold = 1
      }
    }
  }

  # No ingress block — cloudflared makes outbound-only connections to Cloudflare.
  # ACA does not need to receive inbound traffic on this container app.

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.cloudflared_kv_reader[0],
  ]
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "cloudflared_identity_principal_id" {
  value       = try(azurerm_user_assigned_identity.cloudflared[0].principal_id, null)
  description = "Principal ID of the cloudflared managed identity (null when cloudflared_enabled = false)."
}
