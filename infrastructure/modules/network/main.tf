# Network Module
# Creates a VNet (or uses an existing one via existing_vnet_name) plus the
# delegated subnets and private DNS zones the platform needs.

locals {
  # Level-1 bring-your-own-VNet: set existing_vnet_name to deploy the platform's
  # subnets into a pre-existing VNet (e.g. an ALZ-vended one) instead of creating
  # one. The app/db subnets are always module-managed because Container Apps and
  # Postgres Flexible Server each require their own delegated subnet.
  create_vnet = var.existing_vnet_name == ""
  vnet_rg     = var.existing_vnet_resource_group != "" ? var.existing_vnet_resource_group : var.resource_group_name
  vnet_name   = local.create_vnet ? one(azurerm_virtual_network.main[*].name) : one(data.azurerm_virtual_network.existing[*].name)
  vnet_id     = local.create_vnet ? one(azurerm_virtual_network.main[*].id) : one(data.azurerm_virtual_network.existing[*].id)
}

resource "azurerm_virtual_network" "main" {
  count               = local.create_vnet ? 1 : 0
  name                = "${var.prefix}-vnet"
  address_space       = var.vnet_address_space
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

data "azurerm_virtual_network" "existing" {
  count               = local.create_vnet ? 0 : 1
  name                = var.existing_vnet_name
  resource_group_name = local.vnet_rg
}

# Subnet for Container Apps (delegated)
resource "azurerm_subnet" "app" {
  name                 = "app-subnet"
  resource_group_name  = local.vnet_rg
  virtual_network_name = local.vnet_name
  address_prefixes     = var.subnet_app_address_prefixes

  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# Subnet for PostgreSQL (delegated)
resource "azurerm_subnet" "database" {
  name                 = "db-subnet"
  resource_group_name  = local.vnet_rg
  virtual_network_name = local.vnet_name
  address_prefixes     = var.subnet_db_address_prefixes

  delegation {
    name = "postgres"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# Subnet for Private Endpoints
resource "azurerm_subnet" "private_endpoint" {
  name                 = "pe-subnet"
  resource_group_name  = local.vnet_rg
  virtual_network_name = local.vnet_name
  address_prefixes     = var.subnet_pe_address_prefixes
}

# Subnet for Admin / Jump Box / Build Agents
resource "azurerm_subnet" "admin" {
  name                 = "admin-subnet"
  resource_group_name  = local.vnet_rg
  virtual_network_name = local.vnet_name
  address_prefixes     = var.subnet_admin_address_prefixes
}

# Private DNS Zone for PostgreSQL
resource "azurerm_private_dns_zone" "postgres" {
  name                = "private.postgres.database.azure.com"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "${var.prefix}-postgres-dns-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = local.vnet_id
}

# Private DNS Zone for Key Vault (only created when KV uses private access)
resource "azurerm_private_dns_zone" "keyvault" {
  count               = var.key_vault_private_access ? 1 : 0
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "keyvault" {
  count                 = var.key_vault_private_access ? 1 : 0
  name                  = "${var.prefix}-kv-dns-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.keyvault[0].name
  virtual_network_id    = local.vnet_id
}

# Network Security Group for Container Apps subnet
resource "azurerm_network_security_group" "app" {
  name                = "${var.prefix}-app-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "AllowHTTPS"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "app" {
  subnet_id                 = azurerm_subnet.app.id
  network_security_group_id = azurerm_network_security_group.app.id
}

# Outputs
output "vnet_id" {
  value = local.vnet_id
}

output "app_subnet_id" {
  value = azurerm_subnet.app.id
}

output "database_subnet_id" {
  value = azurerm_subnet.database.id
}

output "private_endpoint_subnet_id" {
  value = azurerm_subnet.private_endpoint.id
}

output "admin_subnet_id" {
  value = azurerm_subnet.admin.id
}

output "private_dns_zone_id" {
  value = azurerm_private_dns_zone.postgres.id
}
