# Hardened profile — enterprise security posture (~$250+/mo infra).
postgres_sku_name                       = "B_Standard_B2s"
postgres_storage_mb                     = 65536
postgres_high_availability_enabled      = true
log_retention_in_days                   = 90
log_daily_quota_gb                      = -1
cloudflared_enabled                     = true
key_vault_public_network_access_enabled = false
telegram_enabled                        = false
discord_enabled                         = false
