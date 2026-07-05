# Data sources for Key Vault secrets.
# Names match what scripts/seed-keyvault.sh seeds and what the container-apps
# modules reference (bare kebab-case, no platform- prefix). The previous
# platform-<platform>-<purpose> names were never created by the seed script, so
# `terraform plan` failed reading these data sources on a fresh deploy.

data "azurerm_key_vault_secret" "auth_password" {
  name         = "auth-password"
  key_vault_id = module.keyvault.id
}

data "azurerm_key_vault_secret" "gateway_token" {
  name         = "gateway-token"
  key_vault_id = module.keyvault.id
}

data "azurerm_key_vault_secret" "telegram" {
  count        = var.telegram_enabled ? 1 : 0
  name         = "telegram-bot-token"
  key_vault_id = module.keyvault.id
}

data "azurerm_key_vault_secret" "cf_tunnel_token" {
  name         = "cf-tunnel-token"
  key_vault_id = module.keyvault.id
}
