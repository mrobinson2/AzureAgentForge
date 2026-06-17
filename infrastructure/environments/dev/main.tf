# Dev Environment — Main Terraform configuration
# Defines all module instantiations for the AzureAgentForge dev environment

data "azurerm_client_config" "current" {}

locals {
  prefix = "${var.project_name}-${var.environment}"
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
    CostCenter  = var.cost_center
    Owner       = var.owner_email
  }
}

# Resource Group
resource "azurerm_resource_group" "main" {
  name     = "${local.prefix}-rg"
  location = var.location
  tags     = local.common_tags
}

# Network Module
module "network" {
  source                   = "../../modules/network"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  prefix                   = local.prefix
  tags                     = local.common_tags
  key_vault_private_access = !var.key_vault_public_network_access_enabled
}

# Key Vault Module
module "keyvault" {
  source = "../../modules/keyvault"

  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  prefix              = local.prefix
  tags                = local.common_tags
  tenant_id           = data.azurerm_client_config.current.tenant_id

  public_network_access_enabled = var.key_vault_public_network_access_enabled
  network_default_action        = var.key_vault_network_default_action
  allowed_ip_ranges             = var.key_vault_allowed_ip_ranges

  # List the principals that should have Key Vault Secrets Officer.
  # Replace with your own Azure AD object IDs before deploying.
  admin_object_ids = var.keyvault_admin_object_ids
}

# Container Registry Module
module "container_registry" {
  source              = "../../modules/container-registry"
  registry_name       = var.container_registry_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = true
  tags                = local.common_tags
}

# PostgreSQL Module
module "postgres" {
  source                    = "../../modules/postgres"
  resource_group_name       = azurerm_resource_group.main.name
  location                  = azurerm_resource_group.main.location
  prefix                    = local.prefix
  tags                      = local.common_tags
  environment               = var.environment
  tenant_id                 = data.azurerm_client_config.current.tenant_id
  sku_name                  = var.postgres_sku_name
  storage_mb                = var.postgres_storage_mb
  high_availability_enabled = var.postgres_high_availability_enabled
  delegated_subnet_id       = module.network.database_subnet_id
  private_dns_zone_id       = module.network.private_dns_zone_id
  administrator_login       = var.postgres_admin_username
  administrator_password    = module.keyvault.postgres_admin_password

  # Honcho requires its own database and pgvector (already enabled in the module)
  databases = {
    honcho = {
      charset   = "UTF8"
      collation = "en_US.utf8"
    }
  }
}

# Container Apps Module
module "container_apps" {
  source                         = "../../modules/container-apps"
  resource_group_name            = azurerm_resource_group.main.name
  location                       = azurerm_resource_group.main.location
  prefix                         = local.prefix
  tags                           = local.common_tags
  environment_type               = "Consumption"
  infrastructure_subnet_id       = module.network.app_subnet_id
  internal_load_balancer_enabled = false # Public ingress with IP restrictions
  gateway_allowed_ip_addresses   = var.gateway_allowed_ip_addresses
  log_analytics_workspace_id     = module.monitoring.workspace_id
  key_vault_id                   = module.keyvault.id
  key_vault_uri                  = module.keyvault.uri
  postgres_fqdn                  = module.postgres.fqdn
  existing_environment_id        = var.existing_container_app_environment_id

  # PostgreSQL credentials for Honcho
  postgres_admin_username = var.postgres_admin_username
  postgres_admin_password = module.keyvault.postgres_admin_password

  # Azure AI Foundry Configuration
  ai_foundry_endpoint      = var.ai_foundry_endpoint
  ai_foundry_deployment_id = var.ai_foundry_deployment_id
  ai_foundry_resource_id   = var.ai_foundry_resource_id

  # Application Insights telemetry
  app_insights_connection_string = module.monitoring.connection_string

  # Container image tags (set by CI/CD)
  hermes_image_tag    = var.hermes_image_tag
  honcho_image_tag    = var.honcho_image_tag
  router_image_tag    = var.router_image_tag
  paperclip_image_tag = var.paperclip_image_tag

  # Paperclip orchestrator config
  paperclip_public_url        = var.paperclip_public_url
  paperclip_allowed_hostnames = var.paperclip_allowed_hostnames

  # Azure AI Foundry — shared endpoint for all models (project-scoped)
  ai_foundry_openai_endpoint = var.ai_foundry_openai_endpoint

  # Primary model deployment name
  grok_model_deployment = var.grok_model_deployment

  container_registry_id           = module.container_registry.id
  container_registry_login_server = module.container_registry.login_server

  # Cost optimisation: the always-on Honcho Deriver Container App is replaced
  # by a scheduled Container Apps Job that runs the same workload hourly for
  # up to 10 minutes. Saves ~$0.55/day vs always-on. Trade-off: up to 1 hour
  # of recall lag for new sessions before facts are extracted.
  honcho_deriver_enabled     = false
  honcho_deriver_job_enabled = true

  # Chat-surface feature flags
  telegram_enabled = var.telegram_enabled
  discord_enabled  = var.discord_enabled

  teams_enabled               = var.teams_enabled
  teams_orchestrator_agent_id = var.teams_orchestrator_agent_id

  # Ingress: Cloudflared tunnel (hardened) or ACA managed ingress (cost-optimized)
  cloudflared_enabled = var.cloudflared_enabled

  # Governed memory (memory-governor + sweeper/digest/watchdog jobs). Off by
  # default; every behavior is additionally feature-flag-gated in-app.
  memory_governor_enabled        = var.memory_governor_enabled
  memory_planner_agent_allowlist = var.memory_planner_agent_allowlist

}

# Monitoring Module
module "monitoring" {
  source                = "../../modules/monitoring"
  resource_group_name   = azurerm_resource_group.main.name
  location              = azurerm_resource_group.main.location
  project               = var.project_name
  environment           = var.environment
  tags                  = local.common_tags
  log_retention_in_days = var.log_retention_in_days
  log_daily_quota_gb    = var.log_daily_quota_gb

  # Observability (opt-in). No alert_emails → no action group / alert rules;
  # workbook off by default. Defaults keep the deploy footprint unchanged.
  alert_emails                  = var.alert_emails
  watchdog_app_name             = var.watchdog_app_name
  enable_observability_workbook = var.enable_observability_workbook
}

# Outputs
output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "key_vault_name" {
  value = module.keyvault.name
}

output "postgres_fqdn" {
  value = module.postgres.fqdn
}

output "container_apps_environment_id" {
  value = module.container_apps.environment_id
}

output "log_analytics_workspace_id" {
  value = module.monitoring.workspace_id
}

output "container_registry_login_server" {
  value = module.container_registry.login_server
}

output "container_registry_name" {
  value = module.container_registry.name
}

output "container_registry_admin_username" {
  value = module.container_registry.admin_username
}

output "container_registry_admin_password" {
  value     = module.container_registry.admin_password
  sensitive = true
}
