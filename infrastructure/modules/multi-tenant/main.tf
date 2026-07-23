# Multi-tenant module — control-plane + memory-store container apps.
#
# FLAG-GATED: every resource is count-gated on local.enabled. With
# multi_tenant_enabled = false (default) the module is a no-op — zero resources,
# no plan diff on the single-tenant stack.
#
# Both apps are internal-ingress only (no public IP); reachable over the ACA
# environment's internal DNS. They share the platform's managed Postgres (the
# postgres-connection-string Key Vault secret), matching the "one managed
# Postgres" posture in the multi-tenant design.

locals {
  enabled = var.multi_tenant_enabled ? 1 : 0
}

# ── control-plane ────────────────────────────────────────────────────────────

resource "azurerm_user_assigned_identity" "control_plane" {
  count               = local.enabled
  name                = "id-control-plane-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "control_plane_acr_pull" {
  count                = local.enabled
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.control_plane[0].principal_id
}

resource "azurerm_role_assignment" "control_plane_kv_reader" {
  count                = local.enabled
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.control_plane[0].principal_id
}

resource "azurerm_container_app" "control_plane" {
  count                        = local.enabled
  name                         = "ca-control-plane-${var.environment}"
  container_app_environment_id = var.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.control_plane[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.control_plane[0].id
  }

  # Shared managed Postgres — the control-plane reads/writes the tenant registry.
  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.control_plane[0].id
  }

  # Operator key gating the control-plane admin surface. Fail-closed: the app
  # returns 503 when CONTROL_PLANE_OPERATOR_KEY is unset. Seed this secret
  # before enabling the module (scripts/seed-keyvault.sh, follow-on).
  secret {
    name                = "control-plane-operator-key"
    key_vault_secret_id = "${var.key_vault_uri}secrets/control-plane-operator-key"
    identity            = azurerm_user_assigned_identity.control_plane[0].id
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "control-plane"
      image  = "${var.container_registry_login_server}/control-plane:${var.control_plane_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name        = "PG_CONNSTR"
        secret_name = "postgres-connection-string"
      }
      env {
        name        = "CONTROL_PLANE_OPERATOR_KEY"
        secret_name = "control-plane-operator-key"
      }
      env {
        name  = "KV_URI"
        value = var.key_vault_uri
      }
      env {
        name  = "AZURE_SEARCH_ENDPOINT"
        value = var.azure_search_endpoint
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.control_plane[0].client_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
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
    azurerm_role_assignment.control_plane_acr_pull,
    azurerm_role_assignment.control_plane_kv_reader,
  ]
}

# ── memory-store ─────────────────────────────────────────────────────────────

resource "azurerm_user_assigned_identity" "memory_store" {
  count               = local.enabled
  name                = "id-memory-store-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "memory_store_acr_pull" {
  count                = local.enabled
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.memory_store[0].principal_id
}

resource "azurerm_role_assignment" "memory_store_kv_reader" {
  count                = local.enabled
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.memory_store[0].principal_id
}

resource "azurerm_container_app" "memory_store" {
  count                        = local.enabled
  name                         = "ca-memory-store-${var.environment}"
  container_app_environment_id = var.container_app_environment_id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.memory_store[0].id]
  }

  registry {
    server   = var.container_registry_login_server
    identity = azurerm_user_assigned_identity.memory_store[0].id
  }

  secret {
    name                = "postgres-connection-string"
    key_vault_secret_id = "${var.key_vault_uri}secrets/postgres-connection-string"
    identity            = azurerm_user_assigned_identity.memory_store[0].id
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "memory-store"
      image  = "${var.container_registry_login_server}/memory-store:${var.memory_store_image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      # tenant_id is derived per-request from a verified bearer token; RLS on
      # the shared Postgres backstops isolation (see the module's memory-store
      # reference + v1.7 aaf-0007).
      env {
        name        = "DATABASE_URL"
        secret_name = "postgres-connection-string"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.memory_store[0].client_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = var.app_insights_connection_string
      }
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
    azurerm_role_assignment.memory_store_acr_pull,
    azurerm_role_assignment.memory_store_kv_reader,
  ]
}
