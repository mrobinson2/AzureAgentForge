# Hermes Persistent Storage — Azure File Share
#
# Mounted at /home/appuser/.hermes inside the hermes container.
# Stores SQLite DB, session history, and config.yaml.
#
# Benefits:
#   - Data survives container restarts and revision deployments
#   - Config can be edited in-place (hermes config set) without a rebuild
#   - To change the default model: az containerapp update --set-env-vars OPENAI_MODEL=kimi
#     (entrypoint only writes config on first run; subsequent runs preserve existing file)

resource "azurerm_storage_account" "hermes" {
  name                     = substr(replace("${var.prefix}sa", "-", ""), 0, 24)
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  tags = var.tags
}

resource "azurerm_storage_share" "hermes_data" {
  name                 = "hermes-data"
  storage_account_name = azurerm_storage_account.hermes.name
  quota                = 5 # GiB — plenty for SQLite + session files
}

# Register the share with the Container Apps Environment.
# ACA uses the storage account key to mount it via SMB inside the container.
resource "azurerm_container_app_environment_storage" "hermes_data" {
  name                         = "hermes-data"
  container_app_environment_id = local.container_app_environment_id
  account_name                 = azurerm_storage_account.hermes.name
  share_name                   = azurerm_storage_share.hermes_data.name
  access_key                   = azurerm_storage_account.hermes.primary_access_key
  access_mode                  = "ReadWrite"
}
