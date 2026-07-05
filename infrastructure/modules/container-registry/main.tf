# Container Registry Module
# Azure Container Registry for storing application images

resource "azurerm_container_registry" "main" {
  name                = var.registry_name
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.sku
  admin_enabled       = var.admin_enabled

  tags = var.tags
}

# Outputs
output "id" {
  value = azurerm_container_registry.main.id
}

output "name" {
  value = azurerm_container_registry.main.name
}

output "login_server" {
  value = azurerm_container_registry.main.login_server
}

output "admin_username" {
  value = var.admin_enabled ? azurerm_container_registry.main.admin_username : null
}

output "admin_password" {
  value     = var.admin_enabled ? azurerm_container_registry.main.admin_password : null
  sensitive = true
}
