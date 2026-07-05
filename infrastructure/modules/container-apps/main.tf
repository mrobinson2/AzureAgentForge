# Container Apps Module
# Hosts the Container Apps Environment plus the shared managed identity used
# by Hermes and Honcho for ACR pulls.
# All legacy services (AzureAgentForge, mem0, ingest, query, writeback, indexer) removed.

data "azurerm_client_config" "current" {}

locals {
  container_app_environment_id = var.existing_environment_id != "" ? var.existing_environment_id : azurerm_container_app_environment.main[0].id

  # Derive Key Vault name and URI from the ARM resource ID
  key_vault_name = element(
    split("/", var.key_vault_id),
    length(split("/", var.key_vault_id)) - 1
  )
  key_vault_uri = "https://${local.key_vault_name}.vault.azure.net/"
}

# ─────────────────────────────────────────────────────────────────────────────
# Container Apps Environment
# ─────────────────────────────────────────────────────────────────────────────

resource "azurerm_container_app_environment" "main" {
  count               = var.existing_environment_id == "" ? 1 : 0
  name                = "${var.prefix}-env"
  location            = var.location
  resource_group_name = var.resource_group_name

  log_analytics_workspace_id = var.log_analytics_workspace_id

  infrastructure_subnet_id       = var.infrastructure_subnet_id
  internal_load_balancer_enabled = var.internal_load_balancer_enabled

  tags = var.tags

  lifecycle {
    ignore_changes = [
      infrastructure_resource_group_name,
      docker_bridge_cidr,
      platform_reserved_cidr,
      platform_reserved_dns_ip_address,
      log_analytics_workspace_id,
    ]
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Shared Managed Identity & ACR Access
# Used by any container app that does not have its own per-service identity.
# Hermes and Honcho each have their own identities (hermes.tf / honcho.tf).
# ─────────────────────────────────────────────────────────────────────────────

resource "azurerm_user_assigned_identity" "apps" {
  name                = "${var.prefix}-apps-identity"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  count                = var.create_acr_pull_role ? 1 : 0
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.apps.principal_id
}

# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────

output "environment_id" {
  value = local.container_app_environment_id
}

output "managed_identity_principal_id" {
  value = azurerm_user_assigned_identity.apps.principal_id
}
