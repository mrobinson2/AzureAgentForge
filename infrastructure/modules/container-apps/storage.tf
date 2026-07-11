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

  # aaf-0014 (AZU-0061): double-encrypt data at rest (infrastructure encryption).
  # ⚠️ FORCE-REPLACE / ForceNew: this is a create-time-only property. Toggling it
  # on an EXISTING account replaces the account and DESTROYS the SMB share data
  # (hermes-data / watchdog-state). Apply ONLY during a deliberate rebuild /
  # data-migration window (e.g. the standby rebuild) — never on a live account.
  infrastructure_encryption_enabled = true

  # aaf-0014 (AZU-0012): default-Deny network rules instead of open access.
  # AzureServices + Logging/Metrics bypass keeps diagnostics/logging working;
  # the operator scopes who else may reach it via the storage_allowed_* vars.
  # ⚠️ This ACA environment mounts the file share over the Azure backbone (SMB),
  # NOT the app subnet — after enabling default-Deny, verify the hermes-data /
  # watchdog-state mounts still attach and add the ACA outbound IP(s) to
  # storage_allowed_ip_ranges if they do not.
  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices", "Logging", "Metrics"]
    ip_rules                   = var.storage_allowed_ip_ranges
    virtual_network_subnet_ids = var.storage_allowed_subnet_ids
  }

  # aaf-0014 (AZU-0057): enable Storage Analytics logging for the blob service so
  # successful/failed requests are auditable.
  queue_properties {
    logging {
      delete                = true
      read                  = true
      write                 = true
      version               = "1.0"
      retention_policy_days = 7
    }
  }

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
