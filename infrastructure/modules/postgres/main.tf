# PostgreSQL Flexible Server Module
# Stores tenant registry, sync state, and audit logs

resource "azurerm_postgresql_flexible_server" "main" {
  name                = "${var.prefix}-postgres"
  location            = var.location
  resource_group_name = var.resource_group_name

  sku_name   = var.sku_name
  version    = "15"
  storage_mb = var.storage_mb

  administrator_login    = var.administrator_login
  administrator_password = var.administrator_password

  # Private access only - must disable public network when using VNet
  public_network_access_enabled = false
  delegated_subnet_id           = var.delegated_subnet_id
  private_dns_zone_id           = var.private_dns_zone_id

  # High availability (disabled for dev; zone must remain set when HA is enabled)
  zone = "1"

  dynamic "high_availability" {
    for_each = var.high_availability_enabled ? [1] : []
    content { mode = "ZoneRedundant" }
  }

  backup_retention_days        = var.environment == "prod" ? 35 : 7
  geo_redundant_backup_enabled = var.environment == "prod"

  # Authentication
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = true
    tenant_id                     = var.tenant_id
  }

  tags = var.tags
}

# Create databases
resource "azurerm_postgresql_flexible_server_database" "databases" {
  for_each = var.databases

  name      = each.key
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = each.value.charset
  collation = each.value.collation
}

# Configure PostgreSQL for production workloads
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  # pgvector (vector) for embeddings; pg_trgm + fuzzystrmatch are required by
  # Paperclip's company-search migrations (0079/0080) — Azure Flexible Server
  # rejects CREATE EXTENSION unless the extension is allow-listed here.
  value = "uuid-ossp,pgcrypto,vector,pg_trgm,fuzzystrmatch"
}

# NOTE: Row Level Security setup must be done manually or via a container in the VNet
# The Azure DevOps pipeline agent cannot resolve private DNS zones
# Run the RLS setup script from a VM or container within the VNet after deployment

# Outputs
output "id" {
  value = azurerm_postgresql_flexible_server.main.id
}

output "fqdn" {
  value = azurerm_postgresql_flexible_server.main.fqdn
}

output "administrator_login" {
  value = azurerm_postgresql_flexible_server.main.administrator_login
}
