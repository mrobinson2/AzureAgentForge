# Monitoring Module
# Log Analytics and Application Insights

resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${var.project}-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_in_days
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = var.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${var.project}-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = var.tags
}

output "workspace_id" {
  value = azurerm_log_analytics_workspace.main.id
}

output "connection_string" {
  value     = azurerm_application_insights.main.connection_string
  sensitive = true
}
