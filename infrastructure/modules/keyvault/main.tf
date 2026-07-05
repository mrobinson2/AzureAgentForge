# Key Vault Module
# Manages the platform Key Vault. Secrets are created out-of-band (manually or
# via azure-pipelines.yml) — this module only provisions the vault and its RBAC.

data "azurerm_client_config" "current" {}

# Always include whoever is currently running Terraform (your laptop) + any
# additional admins passed in via the variable (pipeline SP, etc.).
# This permanently prevents Terraform from destroying your personal KV access.
locals {
  admin_object_ids = toset(concat(
    var.admin_object_ids,
    [data.azurerm_client_config.current.object_id]
  ))
}

resource "azurerm_key_vault" "main" {
  name                          = "${var.prefix}-kv"
  resource_group_name           = var.resource_group_name
  location                      = var.location
  tenant_id                     = var.tenant_id
  sku_name                      = "standard"
  soft_delete_retention_days    = 90
  purge_protection_enabled      = true
  public_network_access_enabled = var.public_network_access_enabled
  enable_rbac_authorization     = true

  network_acls {
    bypass         = "AzureServices"
    default_action = var.network_default_action
    ip_rules       = var.allowed_ip_ranges
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [soft_delete_retention_days, enable_rbac_authorization]
  }
}

# Grant each admin object Secrets Officer on the vault
resource "azurerm_role_assignment" "admin_kv_officer" {
  for_each             = local.admin_object_ids # ← changed to use the local
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}

# --- Outputs ---
output "id" {
  value = azurerm_key_vault.main.id
}

output "uri" {
  description = "Key Vault vault_uri (includes trailing slash)"
  value       = azurerm_key_vault.main.vault_uri
}

output "name" {
  value = azurerm_key_vault.main.name
}

# Read the postgres admin password that was created manually in Key Vault.
# This allows dev/main.tf to pass the real value to the postgres module.
data "azurerm_key_vault_secret" "postgres_admin_password" {
  name         = "postgres-admin-password"
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.admin_kv_officer]
}

output "postgres_admin_password" {
  description = "PostgreSQL admin password read from Key Vault"
  value       = data.azurerm_key_vault_secret.postgres_admin_password.value
  sensitive   = true
}